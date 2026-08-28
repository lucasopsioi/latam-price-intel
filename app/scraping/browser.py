# -*- coding: utf-8 -*-
"""Playwright 抓取内核 —— 一律以【手机/平板】身份访问，绝不用桌面网页版。

反爬六条（沿用并强化上一代系统的实战结论）：
 1) 只模拟 Android 手机/平板。内核是 Chromium，Android 真机浏览器就是 Chrome，
    UA 与 JS 引擎指纹天然自洽；伪装 iPhone 反而因"Safari 外衣 + Chrome 内核"被识破。
 2) 按国家伪装本地用户：locale / timezone / Accept-Language 三件套与目标国一致。
 3) 每国 Cookie 持久化 → 第二天像"回头客"，比每次全新无痕更不易触发验证。
 4) 行为拟真：先逛首页暖场 → 带自然 Referer 进搜索页 → 触摸滚动 + 随机停顿。
 5) 封锁自愈：命中人机验证 → 冷却 → 换设备 → 全新会话重试一次；仍被拦则如实上报。
 6) 登录墙单独识别：换设备也过不去，不做无谓重试，直接报 login_wall。

★ 与上一代的区别：新增 `engine fallback` —— 同一域名连续 N 次被拦时，
  自动把该域名标记为"需 Selenium 兜底"，由 engine.py 切到 undetected-chromedriver。
"""
from __future__ import annotations

import logging
import random
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from ..config import PROFILE_DIR

log = logging.getLogger("browser")

# ---------------- 设备档案（全部 Android，理由见文件头）----------------
DEVICE_PROFILES = {
    # 手机：选拉美市占高的机型，更像本地真实用户
    "galaxy_s24": {
        "kind": "phone",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 360, "height": 780}, "device_scale_factor": 3,
    },
    "pixel_8": {
        "kind": "phone",
        "user_agent": "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 915}, "device_scale_factor": 2.625,
    },
    "moto_g84": {          # Motorola 在拉美市占极高
        "kind": "phone",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; moto g84 5G) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 393, "height": 873}, "device_scale_factor": 2.75,
    },
    "redmi_note_13": {     # Redmi 在拉美市占极高
        "kind": "phone",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; 23124RA7EO) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 393, "height": 851}, "device_scale_factor": 2.75,
    },
    "galaxy_a55": {
        "kind": "phone",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; SM-A556E) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 384, "height": 854}, "device_scale_factor": 2.8125,
    },
    # 平板：Android 平板 Chrome 的 UA 不带 "Mobile" 字样，这是真实特征
    "galaxy_tab_s9": {
        "kind": "tablet",
        "user_agent": "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "viewport": {"width": 800, "height": 1280}, "device_scale_factor": 2,
    },
    "lenovo_tab_m10": {
        "kind": "tablet",
        "user_agent": "Mozilla/5.0 (Linux; Android 13; Lenovo TB328FU) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        "viewport": {"width": 800, "height": 1280}, "device_scale_factor": 1.5,
    },
}

UA = DEVICE_PROFILES["galaxy_s24"]["user_agent"]

COUNTRY_PROFILE = {
    "MX": {"locale": "es-MX", "tz": "America/Mexico_City",
           "al": "es-MX,es-419;q=0.9,es;q=0.8,en;q=0.4"},
    "CO": {"locale": "es-CO", "tz": "America/Bogota",
           "al": "es-CO,es-419;q=0.9,es;q=0.8,en;q=0.4"},
    "CL": {"locale": "es-CL", "tz": "America/Santiago",
           "al": "es-CL,es-419;q=0.9,es;q=0.8,en;q=0.4"},
    "PE": {"locale": "es-PE", "tz": "America/Lima",
           "al": "es-PE,es-419;q=0.9,es;q=0.8,en;q=0.4"},
    "BR": {"locale": "pt-BR", "tz": "America/Sao_Paulo",
           "al": "pt-BR,pt;q=0.9,es;q=0.5,en;q=0.4"},
    "AR": {"locale": "es-AR", "tz": "America/Argentina/Buenos_Aires",
           "al": "es-AR,es-419;q=0.9,es;q=0.8,en;q=0.4"},
}
_DEFAULT_PROFILE = {"locale": "es-419", "tz": "America/Mexico_City",
                    "al": "es-419,es;q=0.9,en;q=0.5"}

# ★★ 拦截页分两类，处理方式完全不同 —— 混在一起会把「等一下就能过的」
#     和「不该去过的」用同一种方式放弃掉。
#
#   ① JS 挑战页（CHALLENGE_MARKERS）：Imperva / Cloudflare 之类的
#      "Un momento…" / "Checking your browser"。浏览器自动执行 JS 后
#      会自行跳转 —— **真实用户访问时也是等几秒自动过**。
#      正确处理是**原地多等一会儿**，不是冷却换设备重试。
#      实测 Ripley 秘鲁：等待不足时被判 blocked 放弃，其实等得够就能过。
#
#   ② 人机验证（CAPTCHA_MARKERS）：要人点选图片、勾"我不是机器人"。
#      **不做任何破解尝试**，如实记录并跳过 —— 硬闯只会把 IP 拉黑，
#      而且那属于攻破验证系统，不在本工具的边界内。
CHALLENGE_MARKERS = [
    "just a moment", "checking your browser", "cf-browser-verification",
    "un momento", "aguarde um momento", "um momento", "espere un momento",
    "estamos verificando", "verificando su navegador", "verificando seu navegador",
    "por favor espera", "aguarde enquanto", "pardon our interruption",
    "one moment", "please wait while", "redirecting",
]

#   ③ 人机验证：要人点选图片、勾"我不是机器人"。不做任何破解尝试。
#      ★ 这里**只放真的会出现验证控件**的标记。
#        实测教训：原来把 "access denied" 放在这里，结果 Fast Shop 的
#        Akamai 边缘 403（301 字节静态页、0 iframe、0 输入框、0 验证控件）
#        被判成"需人机验证"，还据此建议用户去人工过验证 ——
#        而那个页面上根本没有任何可供人操作的东西。
#        边缘拒绝属于 ④ 限流/封禁，不是验证码。
CAPTCHA_MARKERS = [
    "no soy un robot", "não sou um robô", "not a robot", "captcha",
    "px-captcha", "perimeterx", "are you a human", "robot check",
    "demuestra que eres", "prove que você", "verifique que você",
    "verifica que eres", "hemos detectado", "detectamos um",
    "cf-turnstile", "recaptcha", "hcaptcha",
]

#   ④ 限流 / 边缘拒绝：站点暂时不想理我们，但**没有任何验证可做**。
#      正确处置是降频、退避、换时段 —— 不是去"过验证"，也不是判"选择器坏了"。
#      实测两例：
#        · Amazon BR 软限流 → 1.7KB「Algo deu errado」错误壳（正文 0 字），
#          旧代码判 none → 记 empty → 体检误报 selector_broken，
#          而它其实已经正常入库 423 条，只是偶尔被限流。
#        · Fast Shop 旧域名 → Akamai 301 字节静态 403「Access Denied」。
THROTTLE_MARKERS = [
    "algo deu errado", "algo salió mal", "something went wrong",
    "unusual traffic", "tráfico inusual", "access denied", "acceso denegado",
    "acesso negado", "attention required", "rate limit", "too many requests",
    "temporarily unavailable", "servicio no disponible",
    "cachorros da amazon", "dogs of amazon",
]

# 兼容旧调用：三类合起来就是"这一页不是正常内容"
BLOCK_MARKERS = CHALLENGE_MARKERS + CAPTCHA_MARKERS + THROTTLE_MARKERS


def classify_block(text: str) -> str:
    """判定拦截类型。

    challenge —— JS 挑战，等它自己跑完（真实用户也是等几秒）
    captcha   —— 真人机验证，有验证控件；不碰，如实跳过
    throttled —— 限流/边缘拒绝，没有验证可做；退避重试，别误报成"选择器坏了"
    none      —— 正常页面
    """
    low = (text or "").lower()[:4000]
    for m in CAPTCHA_MARKERS:
        if m in low:
            return "captcha"
    for m in THROTTLE_MARKERS:
        if m in low:
            return "throttled"
    for m in CHALLENGE_MARKERS:
        if m in low:
            return "challenge"
    return "none"

LOGIN_WALL_URL_MARKERS = [
    "/gz/account-verification", "/account-verification",
    "/jms/", "/lgz/login", "/login?", "/registration?", "/signin?",
]


def locale_for(country_code: str) -> str:
    return COUNTRY_PROFILE.get((country_code or "").upper(), _DEFAULT_PROFILE)["locale"]


def polite_sleep(cfg: dict) -> None:
    time.sleep(random.uniform(cfg.get("min_delay", 2.0), cfg.get("max_delay", 5.0)))


class PlaywrightBrowser:
    """整个运行周期共用一个浏览器进程；每国一个上下文。"""

    OVERLAY_SELECTORS = [
        ".download-app-banner-container", ".download-app-bottom-banner-opacity",
        "[class*=download-app]", ".andes-bottom-sheet", "[class*=cookie-consent]",
        "[id*=onetrust-banner]", ".cookie-banner", "[class*=newsletter-modal]",
        "[data-testid*=modal-close]",
    ]

    name = "playwright"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self._pw = None
        self._browser = None
        self._contexts: dict[str, dict] = {}
        self._pool = self._build_pool()
        random.shuffle(self._pool)
        self._device_idx = 0
        self.block_events: list[tuple[str, str]] = []
        self.login_wall_hosts: set[str] = set()
        self.last_status = "ok"
        self._warmed: set[str] = set()
        # 域名连续被拦计数 → 触发 Selenium 兜底
        self.host_block_streak: dict[str, int] = defaultdict(int)
        self.needs_fallback: set[str] = set()
        if self.cfg.get("persist_cookies", True):
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    def _build_pool(self) -> list[str]:
        want = str(self.cfg.get("device_pool", "mixed")).lower()
        names = list(DEVICE_PROFILES.keys())
        if want in ("phone", "tablet"):
            names = [n for n in names if DEVICE_PROFILES[n]["kind"] == want]
        return names or list(DEVICE_PROFILES.keys())

    # ---------------- 内部 ----------------
    def _ensure_browser(self) -> bool:
        if self._browser:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("未安装 playwright：pip install playwright && playwright install chromium")
            return False
        try:
            self._pw = sync_playwright().start()
            kw = {
                "headless": self.cfg.get("headless", True),
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                ],
            }
            proxy = (self.cfg.get("proxy") or "").strip()
            if proxy:
                kw["proxy"] = {"server": proxy}
            self._browser = self._pw.chromium.launch(**kw)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("浏览器启动失败: %s（首次使用请运行 playwright install chromium）", e)
            return False

    def _next_device(self):
        name = self._pool[self._device_idx % len(self._pool)]
        self._device_idx += 1
        return name, DEVICE_PROFILES[name]

    def _state_file(self, cc: str) -> Path:
        return PROFILE_DIR / f"{cc}.json"

    def _open_context(self, cc: str, fresh: bool = False) -> dict:
        prof = COUNTRY_PROFILE.get(cc, _DEFAULT_PROFILE)
        name, dev = self._next_device()
        kw = dict(
            user_agent=dev["user_agent"], viewport=dev["viewport"],
            device_scale_factor=dev["device_scale_factor"],
            is_mobile=True, has_touch=True,
            locale=prof["locale"], timezone_id=prof["tz"],
            extra_http_headers={"Accept-Language": prof["al"]},
        )
        state = self._state_file(cc)
        if not fresh and self.cfg.get("persist_cookies", True) and state.exists():
            kw["storage_state"] = str(state)
        ctx = self._browser.new_context(**kw)
        # 抹掉最常见的自动化指纹
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome=window.chrome||{runtime:{}};"
            "Object.defineProperty(navigator,'maxTouchPoints',{get:()=>5});"
        )
        self._contexts[cc] = {"ctx": ctx, "pages": 0, "dev": name}
        log.info("[%s] 设备=%s(%s) locale=%s tz=%s%s", cc, name, dev["kind"],
                 prof["locale"], prof["tz"], " ·全新会话" if fresh else "")
        return self._contexts[cc]

    def _close_context(self, cc: str, save: bool = True) -> None:
        ent = self._contexts.pop(cc, None)
        if not ent:
            return
        if save and self.cfg.get("persist_cookies", True):
            try:
                ent["ctx"].storage_state(path=str(self._state_file(cc)))
            except Exception:  # noqa: BLE001
                pass
        try:
            ent["ctx"].close()
        except Exception:  # noqa: BLE001
            pass

    def _get_context(self, cc: str, fresh: bool = False) -> dict:
        cc = (cc or "XX").upper()
        rotate = int(self.cfg.get("rotate_device_every", 8))
        ent = self._contexts.get(cc)
        if fresh and ent:
            self._close_context(cc, save=False)   # 被拦：丢弃可疑 Cookie
            ent = None
        if ent and rotate > 0 and ent["pages"] >= rotate:
            self._close_context(cc, save=True)    # 正常轮换：保留 Cookie
            ent = None
        return ent or self._open_context(cc, fresh=fresh)

    @staticmethod
    def _looks_login_wall(final_url: str) -> bool:
        u = (final_url or "").lower()
        return any(m in u for m in LOGIN_WALL_URL_MARKERS)

    @staticmethod
    def _looks_blocked(status_code: int, title: str, text: str | None) -> bool:
        if status_code in (403, 429, 503):
            return True
        blob = f"{title}\n{(text or '')[:4000]}".lower()
        return any(m in blob for m in BLOCK_MARKERS)

    def _note_block(self, host: str) -> None:
        self.host_block_streak[host] += 1
        if self.host_block_streak[host] >= 3:
            self.needs_fallback.add(host)
            log.warning("域名 %s 连续被拦 %d 次 → 标记为需 Selenium 兜底",
                        host, self.host_block_streak[host])

    def _note_ok(self, host: str) -> None:
        self.host_block_streak[host] = 0

    # ---------------- 对外 ----------------
    def warm_up(self, home_url: str, country: str) -> None:
        """首次访问某站前先逛一次首页：拿基础 Cookie、让来路更像真人。每域名一次。"""
        host = urlparse(home_url).netloc
        if host in self._warmed:
            return
        self._warmed.add(host)
        self.fetch(home_url, extra_wait=1.5, scroll=False, country=country, _is_warmup=True)

    def fetch(self, url, wait_selector=None, extra_wait=3.0, scroll=True,
              country=None, referer=None, _attempt=0, _is_warmup=False):
        """返回 (可见文本, 完整HTML)；失败返回 (None, None)。
        self.last_status ∈ ok / blocked / login_wall / failed"""
        if not self._ensure_browser():
            self.last_status = "failed"
            return None, None
        cc = (country or "XX").upper()
        ent = self._get_context(cc, fresh=(_attempt > 0))
        page = ent["ctx"].new_page()
        host = urlparse(url).netloc
        title, text, html, status_code = "", None, None, 0
        final_url = url
        try:
            resp = page.goto(url, timeout=self.cfg.get("request_timeout", 45) * 1000,
                             wait_until="domcontentloaded", referer=referer)
            status_code = resp.status if resp else 0
            final_url = page.url or url
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
            self.dismiss_overlays(page)
            if scroll:
                for _ in range(random.randint(2, 4)):
                    page.mouse.wheel(0, random.randint(900, 1700))
                    page.wait_for_timeout(random.randint(450, 1100))
            page.wait_for_timeout(int(extra_wait * 1000))
            title = page.title() or ""
            text = page.inner_text("body")
            html = page.content()
        except Exception as e:  # noqa: BLE001
            log.warning("页面抓取失败 %s: %s", url, str(e)[:160])
            self.last_status = "failed"
            self._safe_close(page, ent)
            polite_sleep(self.cfg)
            return None, None
        self._safe_close(page, ent)

        if not _is_warmup and self._looks_login_wall(final_url):
            log.warning("被强制登录墙拦截(%s → %s)，已跳过。", host, urlparse(final_url).path[:40])
            self.login_wall_hosts.add(host)
            self.last_status = "login_wall"
            polite_sleep(self.cfg)
            return None, None

        if not _is_warmup and self._looks_blocked(status_code, title, text):
            if _attempt == 0:
                cooldown = float(self.cfg.get("block_cooldown", 90))
                wait = random.uniform(cooldown * 0.7, cooldown * 1.4)
                log.warning("命中人机验证(%s，设备%s)。冷却 %.0f 秒后换设备重试…",
                            host, ent["dev"], wait)
                time.sleep(wait)
                return self.fetch(url, wait_selector=wait_selector, extra_wait=extra_wait,
                                  scroll=scroll, country=country, referer=referer, _attempt=1)
            log.warning("重试后仍被 %s 拦截，跳过并记录警告。", host)
            self.block_events.append((host, ent["dev"]))
            self._note_block(host)
            self.last_status = "blocked"
            polite_sleep(self.cfg)
            return None, None

        self._note_ok(host)
        self.last_status = "ok"
        polite_sleep(self.cfg)
        return text, html

    def run_on_page(self, url, fn, country=None, referer=None, extra_wait=3.0,
                    scroll=True, _attempt=0):
        """打开页面后把 page 交给 fn(page) 做站点特定交互（翻页/点"加载更多"）。
        封锁语义与 fetch 完全一致。"""
        if not self._ensure_browser():
            self.last_status = "failed"
            return None
        cc = (country or "XX").upper()
        ent = self._get_context(cc, fresh=(_attempt > 0))
        page = ent["ctx"].new_page()
        host = urlparse(url).netloc
        try:
            resp = page.goto(url, timeout=self.cfg.get("request_timeout", 45) * 1000,
                             wait_until="domcontentloaded", referer=referer)
            status_code = resp.status if resp else 0
            final_url = page.url or url

            if self._looks_login_wall(final_url):
                log.warning("被强制登录墙拦截(%s)。需保存登录态后重试。", host)
                self.login_wall_hosts.add(host)
                self.last_status = "login_wall"
                return None

            self.dismiss_overlays(page)
            if scroll:
                for _ in range(random.randint(2, 4)):
                    page.mouse.wheel(0, random.randint(900, 1700))
                    page.wait_for_timeout(random.randint(450, 1100))
            page.wait_for_timeout(int(extra_wait * 1000))

            if self._looks_blocked(status_code, page.title() or "", page.inner_text("body")):
                if _attempt == 0:
                    cooldown = float(self.cfg.get("block_cooldown", 90))
                    wait = random.uniform(cooldown * 0.7, cooldown * 1.4)
                    log.warning("命中人机验证(%s)。冷却 %.0f 秒后换设备重试…", host, wait)
                    self._safe_close(page, ent)
                    time.sleep(wait)
                    return self.run_on_page(url, fn, country=country, referer=referer,
                                            extra_wait=extra_wait, scroll=scroll, _attempt=1)
                log.warning("重试后仍被 %s 拦截，跳过。", host)
                self.block_events.append((host, ent["dev"]))
                self._note_block(host)
                self.last_status = "blocked"
                return None

            result = fn(page)
            self._note_ok(host)
            self.last_status = "ok"
            return result
        except Exception as e:  # noqa: BLE001
            log.warning("页面交互失败 %s: %s", url, str(e)[:160])
            self.last_status = "failed"
            return None
        finally:
            self._safe_close(page, ent)
            polite_sleep(self.cfg)

    def dismiss_overlays(self, page) -> None:
        """移除会拦截点击的浮层（下载App横幅 / cookie 条 / 弹窗）。失败无所谓。"""
        try:
            page.evaluate(
                "(sels)=>{for(const s of sels){document.querySelectorAll(s)"
                ".forEach(e=>{try{e.remove()}catch(_){}})}}",
                self.OVERLAY_SELECTORS)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _safe_close(page, ent) -> None:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        ent["pages"] += 1

    def save_all_states(self) -> None:
        for cc in list(self._contexts.keys()):
            ent = self._contexts[cc]
            if self.cfg.get("persist_cookies", True):
                try:
                    ent["ctx"].storage_state(path=str(self._state_file(cc)))
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        for cc in list(self._contexts.keys()):
            self._close_context(cc, save=True)
        try:
            if self._browser:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
