# -*- coding: utf-8 -*-
"""Selenium 抓取引擎（undetected-chromedriver 优先）—— ★当前的【主引擎】。

为什么它是主引擎（2026-08-10 实测 + 用户拍板）：
  Liverpool 在 Playwright 下冷却换设备重试两次仍被人机验证拦死（每轮白耗 120 秒）；
  Selenium 一次通过拿到 56 条真实商品，只用 24 秒。

  机制差异：Playwright 走 CDP（Chrome DevTools Protocol），痕迹较明显；
  undetected-chromedriver 直接给 chromedriver 打补丁，抹掉 `cdc_` 系列变量等
  Selenium 专属特征，并用真实用户资料目录启动 —— 用 CDP 检测的站点拦不住它。

  代价是单页耗时是 Playwright 的 2~4 倍。但目标是**24 小时内跑完且不被封**，
  不是跑得快，所以这笔交易做得过。

  配置见 app/config.py：engine="selenium"，fallback_engine="playwright"。

接口与 PlaywrightBrowser 完全一致（fetch / run_on_page / warm_up / close），
engine.py 可以无缝互换主备，上层不需要知道自己在用哪个引擎。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import threading
import time
from urllib.parse import urlparse

from pathlib import Path

from ..config import PROFILE_DIR
from .browser import (BLOCK_MARKERS, COUNTRY_PROFILE, DEVICE_PROFILES,
                      LOGIN_WALL_URL_MARKERS, _DEFAULT_PROFILE, classify_block,
                      polite_sleep)

log = logging.getLogger("selenium")

# 连续启动失败多少次后本轮放弃（每次之间有退避）
MAX_LAUNCH_FAILURES = 3

_CHROME_MAJOR: int | None = None

# ★ undetected-chromedriver 会把 chromedriver 解压/打补丁到一个**固定路径**
#   （%APPDATA%\undetected_chromedriver\...）。多个 worker 同时首次启动时，
#   它们会同时往同一个文件写 → FileExistsError: [WinError 183]。
#   实测：并行度 5 时七个渠道各失败 41 次，看起来像"被风控"，实际是这个竞态。
#   用全局锁把「创建 driver」串起来：只有首次解压需要互斥，
#   之后各 worker 各跑各的，不影响并行度。
_LAUNCH_LOCK = threading.Lock()


def chrome_major_version() -> int | None:
    """探测本机 Chrome 主版本号（结果缓存）。

    undetected-chromedriver 自己的探测在 Windows 上不可靠，探不到就假设 108，
    于是下载了一个差几十个大版本的 driver，启动直接失败。
    Windows 两条途径都试：注册表 → 安装目录下的版本号文件夹。
    Linux/macOS 直接问二进制自己（`--version`）。

    ★ Linux 上这个探测同样省不掉，而且更隐蔽：原来只有 Windows 那两条路径，
      在服务器上双双落空 → 返回 None → uc 照样静默假设 chrome 108 →
      "cannot connect to chrome"。跟 Windows 上那个坑是同一个，只是触发条件不同。
    """
    global _CHROME_MAJOR
    if _CHROME_MAJOR is not None:
        return _CHROME_MAJOR or None

    ver = ""
    if sys.platform == "win32":
        # ① 注册表（最快且最准）
        try:
            import winreg
            for hive, path in ((winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon")):
                try:
                    with winreg.OpenKey(hive, path) as k:
                        ver = winreg.QueryValueEx(k, "version")[0]
                        if ver:
                            break
                except OSError:
                    continue
        except Exception:  # noqa: BLE001
            pass

        # ② 安装目录下会有一个以版本号命名的子目录
        if not ver:
            import os
            import re as _re
            for base in (r"C:\Program Files\Google\Chrome\Application",
                         r"C:\Program Files (x86)\Google\Chrome\Application",
                         os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application")):
                try:
                    names = [d for d in os.listdir(base) if _re.fullmatch(r"\d+(\.\d+)+", d)]
                    if names:
                        ver = sorted(names, key=lambda s: [int(x) for x in s.split(".")])[-1]
                        break
                except OSError:
                    continue
    else:
        # ③ Linux/macOS：直接问二进制。发行版和安装方式太多（apt / snap /
        #    chromium-browser / 各种符号链接），逐个 which 比硬猜路径可靠。
        #    顺序有意义：优先真 Chrome，chromium 排后面 ——
        #    undetected-chromedriver 的补丁是按 Chrome 的版本线走的。
        import shutil
        import subprocess
        for exe in ("google-chrome", "google-chrome-stable", "chrome",
                    "chromium", "chromium-browser"):
            path = shutil.which(exe)
            if not path:
                continue
            try:
                # 不用 text=True：真出现非 UTF-8 输出时会抛在读取线程里，
                # 外面只看到"探测失败"。取 bytes 自己抠数字最稳。
                out = subprocess.run([path, "--version"],
                                     capture_output=True, timeout=15).stdout or b""
            except (OSError, subprocess.SubprocessError):
                continue
            # 形如 "Google Chrome 151.0.7258.66" / "Chromium 140.0.1234.5"
            m = re.search(rb"(\d+(?:\.\d+)+)", out)
            if m:
                ver = m.group(1).decode("ascii")
                log.info("[selenium] 用 %s 探测到版本 %s", path, ver)
                break

    try:
        _CHROME_MAJOR = int(str(ver).split(".")[0]) if ver else 0
    except (TypeError, ValueError):
        _CHROME_MAJOR = 0
    if _CHROME_MAJOR:
        log.info("[selenium] 检测到 Chrome 主版本 %d", _CHROME_MAJOR)
    else:
        log.warning("[selenium] 未能探测 Chrome 版本，交给 undetected-chromedriver 自己猜"
                    "（可能因版本不匹配启动失败）")
    return _CHROME_MAJOR or None


class SeleniumBrowser:
    """与 PlaywrightBrowser 同接口的兜底实现。懒启动：不用就不开进程。"""

    name = "selenium"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self._driver = None
        self._current_cc = None
        self._pages = 0
        self._launch_failures = 0
        # 每抓 N 页换一个新会话。0 = 不轮换。
        # 长跑（用户要求 10~20 小时）必须换，否则单个 Chrome 进程内存与
        # session 状态一路累积，跑到后半程性能塌方且更容易被风控盯上。
        self._rotate_every = int((cfg or {}).get("rotate_session_every", 60))
        self.block_events: list[tuple[str, str]] = []
        self.login_wall_hosts: set[str] = set()
        self.last_status = "ok"
        self._warmed: set[str] = set()
        self.needs_fallback: set[str] = set()   # 兜底引擎没有下一级了
        self._unavailable_reason = None

    # ---------------- 启动 ----------------
    def _ensure_driver(self, cc: str) -> bool:
        """按国家启动。切国家时重建（locale/tz 要跟着变）。

        ★ 两个曾经的坑：
          1. 启动失败后 _unavailable_reason 被**永久钉死**，此后每次都提前
             返回 False —— 一次瞬时失败（比如上一轮残留的 user-data-dir 锁）
             就拖垮整轮，而且没有任何重试。Selenium 现在是主引擎，
             这个字段等于决定了整轮抓取的生死。改成带退避的有限重试。
          2. driver 从不回收：_pages 只加不读，一个 Chrome 进程扛完整个国家
             上万次导航，内存与 session 状态一路累积。改成按页数轮换。
        """
        if self._driver is not None and self._current_cc == cc:
            # 按页数轮换：长跑时定期换新会话，既省内存也更不像机器人
            if self._rotate_every > 0 and self._pages >= self._rotate_every:
                log.info("[selenium] 已抓 %d 页，轮换新会话", self._pages)
                self._quit_driver()
            elif not self._session_alive():
                # ★★ 只判断"driver 对象还在"是不够的 —— Chrome 进程可能已经死了
                #   （崩溃 / 被 OOM / 被孤儿回收器误杀 / 用户手动关掉），
                #   而 driver 对象**照样存在**。此时每次 fetch 都报
                #   "not connected to DevTools"，且**永远不会自愈** ——
                #   实测Acme商城那轮：每个国家第一个单元 ok，
                #   之后同国家的 3 个单元全 failed，直到换国家才重建。
                #   一次瞬时进程死亡因此变成整国采集全灭。
                log.warning("[selenium] 会话已失效（进程可能已退出），重建")
                self._quit_driver()
            else:
                return True

        if self._unavailable_reason:
            if self._launch_failures >= MAX_LAUNCH_FAILURES:
                return False
            # 退避后再给一次机会：瞬时失败不该判死刑
            wait = min(30 * self._launch_failures, 120)
            log.info("[selenium] 上次启动失败（%s），退避 %d 秒后重试",
                     self._unavailable_reason[:60], wait)
            time.sleep(wait)
            self._unavailable_reason = None

        self._quit_driver()
        if self._launch_failures >= 1:
            # 启动失败最常见的原因是上一轮残留的进程占着 user-data-dir。
            # 重试前先清掉锁文件 —— 实测系统里堆过 28 个孤儿 chrome，
            # 每个都占着自己的资料目录，不清就一直起不来。
            self._clear_profile_lock(cc)

        prof = COUNTRY_PROFILE.get(cc, _DEFAULT_PROFILE)
        dev = DEVICE_PROFILES["moto_g84"]   # 兜底固定用拉美市占最高的机型

        try:
            driver = self._launch(prof, dev, cc)
        except Exception as e:  # noqa: BLE001
            self._launch_failures += 1
            self._unavailable_reason = f"{type(e).__name__}: {str(e)[:180]}"
            log.error("Selenium 启动失败（第 %d/%d 次）：%s",
                      self._launch_failures, MAX_LAUNCH_FAILURES,
                      self._unavailable_reason)
            if self._launch_failures >= MAX_LAUNCH_FAILURES:
                log.error("Selenium 连续 %d 次启动失败，本轮不再尝试。"
                          "常见原因：上一轮残留的 chrome 进程占着 user-data-dir，"
                          "到任务管理器里结束 chrome.exe / chromedriver.exe 后重试",
                          MAX_LAUNCH_FAILURES)
            return False
        self._launch_failures = 0      # 启动成功即清零，不累积历史失败

        self._driver = driver
        self._current_cc = cc
        self._pages = 0
        # 时区/语言注入必须在任何页面加载前生效
        try:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                   {"timezoneId": prof["tz"]})
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders",
                                   {"headers": {"Accept-Language": prof["al"]}})
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                           "Object.defineProperty(navigator,'maxTouchPoints',{get:()=>5});"})
        except Exception:  # noqa: BLE001
            pass
        log.info("[%s] Selenium 兜底引擎已启动（设备 moto_g84，locale=%s）", cc, prof["locale"])
        return True

    def _launch(self, prof: dict, dev: dict, cc: str):
        """优先 undetected-chromedriver，装不上退回原生 selenium。"""
        opts_args = [
            f"--user-agent={dev['user_agent']}",
            f"--window-size={dev['viewport']['width']},{dev['viewport']['height']}",
            f"--lang={prof['locale']}",
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-gpu", "--mute-audio",
        ]
        proxy = (self.cfg.get("proxy") or "").strip()
        if proxy:
            opts_args.append(f"--proxy-server={proxy}")
        headless = self.cfg.get("headless", True)
        # 每国独立 profile 目录 → Cookie 持久化，与 Playwright 的策略一致
        profile_dir = self._profile_dir(cc)
        profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            import undetected_chromedriver as uc

            opts = uc.ChromeOptions()
            for a in opts_args:
                opts.add_argument(a)
            opts.add_argument(f"--user-data-dir={profile_dir}")
            # ★ eager：只等 DOMContentLoaded，不等 load 事件。
            #   默认的 normal 要等所有图片/广告 SDK/追踪像素加载完 ——
            #   拉美电商挂着一堆第三方广告脚本，任何一个慢就拖到超时
            #   （实测 Liverpool 首页在 normal 下 45 秒超时）。
            #   商品数据在 DOMContentLoaded 时已经在 DOM 里了。
            opts.page_load_strategy = "eager"
            # ★ 必须显式传 version_main。
            #   undetected-chromedriver 在 Windows 上探测 Chrome 版本不可靠
            #   （翻的注册表键/路径常与实际安装位置对不上），失败时会**静默假设
            #   chrome 108** 并去下载对应 driver。本机 Chrome 是 151，
            #   差 43 个大版本 ⇒ "cannot connect to chrome"。
            #   症状是时好时坏（缓存里恰好有可用 driver 就成功），最难查。
            kw = {"options": opts, "headless": headless, "use_subprocess": True}
            major = chrome_major_version()
            if major:
                kw["version_main"] = major
            # ★ 首次创建要解压/patch chromedriver 到固定路径，多 worker 并发
            #   会撞 FileExistsError。用全局锁串起创建动作即可 ——
            #   只有这一小段互斥，抓取本身仍然完全并行。
            with _LAUNCH_LOCK:
                driver = uc.Chrome(**kw)
        except ImportError:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            log.warning("未装 undetected-chromedriver，退回原生 selenium（反爬能力较弱）")
            opts = Options()
            for a in opts_args:
                opts.add_argument(a)
            opts.add_argument(f"--user-data-dir={profile_dir}")
            if headless:
                opts.add_argument("--headless=new")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            # 移动端模拟：与 Playwright 的 is_mobile/has_touch 对齐
            opts.add_experimental_option("mobileEmulation", {
                "deviceMetrics": {
                    "width": dev["viewport"]["width"],
                    "height": dev["viewport"]["height"],
                    "pixelRatio": dev["device_scale_factor"],
                    "touch": True,
                },
                "userAgent": dev["user_agent"],
            })
            opts.page_load_strategy = "eager"
            driver = webdriver.Chrome(options=opts)

        driver.set_page_load_timeout(self.cfg.get("request_timeout", 45))
        return driver

    def _stop_loading(self) -> None:
        """超时后强行停止加载：此时 DOM 往往已经可用，
        直接放弃整页会白白丢掉已经到手的数据。"""
        try:
            self._driver.execute_script("window.stop();")
        except Exception:  # noqa: BLE001
            pass

    @property
    def is_unavailable(self) -> bool:
        """浏览器**整体**起不来（不是某个站点被拦）。

        ★ 这两件事必须分开：
          「某个域名被拦」→ 只有那个域名要换引擎；
          「浏览器根本启动不了」→ **所有**域名都得换引擎。
          实测漏掉后者的代价：一次启动失败让整个域名组的 24 个采集单元
          全部失败，而 Playwright 兜底引擎全程没被唤醒过一次 ——
          明明有备胎却眼看着整组数据丢掉。
        """
        return self._launch_failures >= MAX_LAUNCH_FAILURES

    def _profile_dir(self, cc: str) -> Path:
        """浏览器资料目录。

        ★ 按「国家 + 域名」分，不能只按国家：
          同一国家的多个域名组是**并行**跑的（用户要求的跨域名并行），
          共用一个 user-data-dir 会让后启动的实例连不上 Chrome。
        ★ 这个函数必须是启动与清锁的**唯一**来源 —— 两处各算一次的话，
          清锁会清到另一个目录去，重试永远清不掉真正卡住的那把锁。
        """
        tag = re.sub(r"[^a-z0-9]+", "_", str(self.cfg.get("profile_tag") or "").lower())
        return PROFILE_DIR / (f"selenium_{cc}_{tag}" if tag else f"selenium_{cc}")

    def _clear_profile_lock(self, cc: str) -> None:
        """清掉 Chrome 资料目录里的锁文件（进程已死但锁还在的情况）。"""
        prof = self._profile_dir(cc)
        for pat in ("SingletonLock", "SingletonSocket", "SingletonCookie",
                    "lockfile", "Default/LOCK"):
            f = prof / pat
            try:
                if f.exists():
                    f.unlink()
                    log.info("[selenium] 已清除残留锁文件 %s", f.name)
            except Exception:  # noqa: BLE001
                pass

    def _session_alive(self) -> bool:
        """会话还能用吗。

        ★ 用最轻的一次真实往返来探活（读 current_url），不要用
          `self._driver is not None` —— 那只证明**对象**还在。
          Chrome 进程死了之后对象依然存在，后续每次操作都抛
          "not connected to DevTools"，而调用方看到的只是"这个单元失败了"。
        """
        try:
            _ = self._driver.current_url
            return True
        except Exception:  # noqa: BLE001
            return False

    def _quit_driver(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001
                log.debug("driver.quit() 出错，进程可能残留", exc_info=True)
        self._driver = None
        self._current_cc = None
        self._pages = 0      # 轮换计数必须归零，否则新会话一上来就被判定该换

    # ---------------- 检测（语义与 Playwright 侧完全一致）----------------
    @staticmethod
    def _looks_login_wall(final_url: str) -> bool:
        u = (final_url or "").lower()
        return any(m in u for m in LOGIN_WALL_URL_MARKERS)

    @staticmethod
    def _looks_blocked(title: str, text: str | None) -> bool:
        # 注：Selenium 拿不到 HTTP 状态码，只能靠文本特征
        blob = f"{title}\n{(text or '')[:4000]}".lower()
        return any(m in blob for m in BLOCK_MARKERS)

    def load_saved_cookies(self, host: str, cc: str) -> int:
        """把 tools/site_login.py 存下的会话注入当前 driver。

        ★ 这些 Cookie 是**用户本人**过完人机验证/登录后导出的。
          带上它们，站点认得这是已验证过的会话，就不会再拦。
          工具从不代替用户完成验证，只复用其结果。
        """
        if not self._driver:
            return 0
        loaded = 0
        for f in PROFILE_DIR.glob(f"*_{cc}_cookies.json"):
            try:
                blob = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for ck in blob.get("cookies") or []:
                dom = (ck.get("domain") or "").lstrip(".")
                if dom and dom not in host and host not in dom:
                    continue          # 只注入本域的，否则 Selenium 会报错
                ck.pop("sameSite", None)      # Selenium 对该字段挑剔
                ck.pop("expiry", None) if ck.get("expiry") in (None, 0) else None
                try:
                    self._driver.add_cookie(ck)
                    loaded += 1
                except Exception:  # noqa: BLE001
                    continue
        if loaded:
            log.info("[selenium] %s 注入了 %d 个已保存的会话 Cookie", host, loaded)
        return loaded

    def _safe_title(self) -> str:
        try:
            return self._driver.title or ""
        except Exception:  # noqa: BLE001
            return ""

    def _wait_out_challenge(self, url: str, host: str):
        """守着 JS 挑战页，等它自己跑完。

        ★ 这不是"破解"，是**等待浏览器完成站点要求的 JS 校验** ——
          真实用户访问 Imperva/Cloudflare 保护的站点时也是等几秒自动跳转。
          我们只是给足时间，不解题、不伪造凭据。

        每 2 秒查一次，最长等 `challenge_wait_seconds`（默认 25 秒）。
        一旦页面不再是挑战页就返回内容；到点还没过就交回上层按拦截处理。
        """
        budget = float(self.cfg.get("challenge_wait_seconds", 25))
        log.info("[selenium] %s 是 JS 挑战页，原地等它跑完（最多 %.0f 秒）…", host, budget)
        t0 = time.monotonic()
        text = html = None
        while time.monotonic() - t0 < budget:
            time.sleep(2.0)
            try:
                html = self._driver.page_source
                text = self._driver.execute_script(
                    "return document.body ? document.body.innerText : ''") or ""
            except Exception:  # noqa: BLE001
                continue
            if classify_block(f"{self._safe_title()}\n{text[:4000]}") == "none":
                el = time.monotonic() - t0
                log.info("[selenium] %s 挑战已通过（等了 %.0f 秒）", host, el)
                return text, html, True
        log.warning("[selenium] %s 等满 %.0f 秒仍未通过挑战", host, budget)
        return text, html, False

    def _human_scroll(self) -> None:
        """拟真滚动：变速、会回滚、会停下来"看"。

        ★ 机器人最明显的特征不是速度，是**规律**：每次都滚一样的距离、
          间隔一样、从不回头、看完就走。真人会快速划过不感兴趣的部分、
          在感兴趣处停下、偶尔往回翻、鼠标会乱动。
          用户明确要求加强随机停留防封号，这里就是落点。
        """
        try:
            rounds = random.randint(3, 7)
            for i in range(rounds):
                # 变速：偶尔一大步（快速略过），偶尔几小步（细看）
                if random.random() < 0.25:
                    dist = random.randint(1600, 2800)
                else:
                    dist = random.randint(400, 1200)
                self._driver.execute_script(
                    f"window.scrollBy({{top:{dist},behavior:'smooth'}});")
                # 停留时间偏对数正态：多数很短，偶尔很长（真人被内容吸引）
                pause = random.lognormvariate(-0.4, 0.7)
                time.sleep(min(max(pause, 0.25), 4.5))

                # 偶尔往回翻一点 —— 真人常有的"刚才那个是什么"
                if random.random() < 0.18:
                    self._driver.execute_script(
                        f"window.scrollBy({{top:-{random.randint(200, 600)},"
                        f"behavior:'smooth'}});")
                    time.sleep(random.uniform(0.4, 1.6))

            # 页面浏览完的"思考停顿"
            if random.random() < 0.35:
                time.sleep(random.uniform(1.0, 3.5))
        except Exception:  # noqa: BLE001
            pass

    def _human_mouse(self) -> None:
        """随机移动几下鼠标。有些风控脚本会统计 mousemove 事件，
        一次都没有的会话很可疑。"""
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            body = self._driver.find_element("tag name", "body")
            actions = ActionChains(self._driver)
            for _ in range(random.randint(1, 3)):
                actions.move_to_element_with_offset(
                    body, random.randint(30, 300), random.randint(30, 400))
                actions.pause(random.uniform(0.1, 0.5))
            actions.perform()
        except Exception:  # noqa: BLE001
            pass

    def dismiss_overlays(self, _page=None) -> None:
        from .browser import PlaywrightBrowser
        try:
            self._driver.execute_script(
                "const sels=arguments[0];for(const s of sels){"
                "document.querySelectorAll(s).forEach(e=>{try{e.remove()}catch(_){}})}",
                PlaywrightBrowser.OVERLAY_SELECTORS)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 对外 ----------------
    def warm_up(self, home_url: str, country: str) -> None:
        host = urlparse(home_url).netloc
        if host in self._warmed:
            return
        self._warmed.add(host)
        self.fetch(home_url, extra_wait=1.5, scroll=False, country=country, _is_warmup=True)
        # ★ 暖场落地后注入已保存的会话 —— Cookie 必须在**同域页面已打开**
        #   之后才能 add_cookie（浏览器安全策略），所以只能放在这里。
        #   这些 Cookie 来自 tools/site_login.py：用户本人过完人机验证后导出的。
        try:
            n = self.load_saved_cookies(host, (country or "XX").upper())
            if n:
                self._driver.get(home_url)      # 带着会话重新加载一次
        except Exception:  # noqa: BLE001
            log.debug("[selenium] 注入会话 Cookie 失败", exc_info=True)

    def fetch(self, url, wait_selector=None, extra_wait=3.0, scroll=True,
              country=None, referer=None, _attempt=0, _is_warmup=False):
        cc = (country or "XX").upper()
        if not self._ensure_driver(cc):
            self.last_status = "failed"
            return None, None
        host = urlparse(url).netloc
        try:
            try:
                self._driver.get(url)
            except Exception as nav_err:  # noqa: BLE001
                # 超时不等于失败：eager 策略下 DOM 通常已经可用，
                # 停掉剩余请求继续解析，比整页丢弃划算得多
                if "timeout" not in str(nav_err).lower():
                    raise
                log.info("[selenium] %s 加载超时，停止剩余请求后继续解析", host)
                self._stop_loading()
            self._pages += 1
            if wait_selector:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.support.ui import WebDriverWait
                try:
                    WebDriverWait(self._driver, 8).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector)))
                except Exception:  # noqa: BLE001
                    pass
            self.dismiss_overlays()
            self._human_mouse()
            if scroll:
                self._human_scroll()
            # 停留时间也随机化，不要每页都停一样长
            time.sleep(extra_wait * random.uniform(0.7, 1.8))
            final_url = self._driver.current_url or url
            title = self._driver.title or ""
            text = self._driver.find_element("tag name", "body").text
            html = self._driver.page_source
        except Exception as e:  # noqa: BLE001
            log.warning("[selenium] 抓取失败 %s: %s", url, str(e)[:160])
            self.last_status = "failed"
            polite_sleep(self.cfg)
            return None, None

        if not _is_warmup and self._looks_login_wall(final_url):
            log.warning("[selenium] 登录墙拦截 %s", host)
            self.login_wall_hosts.add(host)
            self.last_status = "login_wall"
            polite_sleep(self.cfg)
            return None, None

        if not _is_warmup:
            kind = classify_block(f"{title}\n{(text or '')[:4000]}")

            # ★ ① JS 挑战页：**原地等它自己跑完**，不要冷却换设备。
            #   Imperva/Cloudflare 的挑战由浏览器执行 JS 后自动跳转，
            #   真实用户也是等几秒 —— 等待是正常浏览行为，不是破解。
            #   之前一律当 blocked 处理，把本来能过的白白放弃了。
            if kind == "challenge":
                text, html, passed = self._wait_out_challenge(url, host)
                if passed:
                    self.last_status = "ok"
                    polite_sleep(self.cfg)
                    return text, html
                kind = classify_block(f"{self._safe_title()}\n{(text or '')[:4000]}")

            # ★ ② 限流/边缘拒绝：没有验证可做，退避后重试一次。
            #   实测 Amazon BR 软限流 1~3 分钟自解 —— 之前判成 none 记 empty，
            #   体检又翻译成"选择器坏了"，把一个临时状态误报成永久故障。
            if kind == "throttled":
                if _attempt == 0:
                    wait = random.uniform(45, 90)
                    log.warning("[selenium] %s 被限流（非验证码），退避 %.0f 秒后重试",
                                host, wait)
                    time.sleep(wait)
                    return self.fetch(url, wait_selector=wait_selector,
                                      extra_wait=extra_wait, scroll=scroll,
                                      country=country, referer=referer, _attempt=1)
                log.warning("[selenium] %s 仍被限流，本轮跳过（下轮会自动重试）", host)
                self.block_events.append((host, "throttled"))
                self.last_status = "throttled"
                polite_sleep(self.cfg)
                return None, None

            # ★ ③ 人机验证：不做任何破解尝试，如实记录并跳过。
            #   硬闯只会把 IP 拉黑，而且攻破验证系统不在本工具边界内。
            if kind == "captcha":
                log.warning("[selenium] %s 要求人机验证（需真人操作），跳过不硬闯", host)
                self.block_events.append((host, "captcha"))
                self.last_status = "blocked"
                polite_sleep(self.cfg)
                return None, None

            if kind != "none":
                if _attempt == 0:
                    cooldown = float(self.cfg.get("block_cooldown", 90))
                    wait = random.uniform(cooldown * 0.7, cooldown * 1.4)
                    log.warning("[selenium] %s 拦截，冷却 %.0f 秒后重开会话重试…", host, wait)
                    self._quit_driver()
                    time.sleep(wait)
                    return self.fetch(url, wait_selector=wait_selector,
                                      extra_wait=extra_wait, scroll=scroll,
                                      country=country, referer=referer, _attempt=1)
                log.warning("[selenium] 重试后仍被 %s 拦截 —— 这已是最后一道引擎，跳过。", host)
                self.block_events.append((host, "selenium"))
                self.last_status = "blocked"
                polite_sleep(self.cfg)
                return None, None

        self.last_status = "ok"
        polite_sleep(self.cfg)
        return text, html

    def run_on_page(self, url, fn, country=None, referer=None, extra_wait=3.0,
                    scroll=True, _attempt=0):
        """把 driver 交给 fn 做站点特定交互。
        注意：fn 收到的是 Selenium driver 而非 Playwright page，
        站点适配器需要判断 engine 名再决定怎么操作（见 channels/base.py）。"""
        cc = (country or "XX").upper()
        if not self._ensure_driver(cc):
            self.last_status = "failed"
            return None
        try:
            self._driver.get(url)
            self._pages += 1
            self.dismiss_overlays()
            self._human_mouse()
            if scroll:
                self._human_scroll()
            # 停留时间也随机化，不要每页都停一样长
            time.sleep(extra_wait * random.uniform(0.7, 1.8))
            if self._looks_login_wall(self._driver.current_url or url):
                self.login_wall_hosts.add(urlparse(url).netloc)
                self.last_status = "login_wall"
                return None
            body = self._driver.find_element("tag name", "body").text
            if self._looks_blocked(self._driver.title or "", body):
                self.block_events.append((urlparse(url).netloc, "selenium"))
                self.last_status = "blocked"
                return None
            result = fn(self._driver)
            self.last_status = "ok"
            return result
        except Exception as e:  # noqa: BLE001
            log.warning("[selenium] 页面交互失败 %s: %s", url, str(e)[:160])
            self.last_status = "failed"
            return None
        finally:
            polite_sleep(self.cfg)

    def save_all_states(self) -> None:
        pass   # user-data-dir 自动持久化，无需手动导出

    def close(self) -> None:
        self._quit_driver()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _scan_processes_win() -> dict[int, tuple[int, str]]:
    """Windows 进程表：{pid: (ppid, 命令行小写)}。"""
    import subprocess

    # ★ 本机 ANSI 是 cp936，PowerShell 的输出不是 UTF-8。
    #   用 text=True + encoding="utf-8" 会在命令行含中文路径时抛
    #   UnicodeDecodeError（抛在读取线程里，外面只看到"扫描到 0 个"）——
    #   回收器会**静默失效**，而僵尸浏览器照样堵住下一轮。
    #   所以取 bytes 自己解，容错解码。
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
         "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
         "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, timeout=30)
    raw = out.stdout or b""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    data = json.loads(text or "[]")
    if isinstance(data, dict):
        data = [data]
    return {int(p["ProcessId"]): (int(p.get("ParentProcessId") or 0),
                                  (p.get("CommandLine") or "").lower())
            for p in data if p.get("ProcessId")}


def _scan_processes_proc() -> dict[int, tuple[int, str]]:
    """Linux 进程表：{pid: (ppid, 命令行小写)}，直接读 /proc。

    比 Windows 那条 PowerShell + JSON 的路子简单可靠得多，也没有编码地雷 ——
    /proc/<pid>/cmdline 就是 NUL 分隔的原始字节。
    """
    procs: dict[int, tuple[int, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            if not cmdline.strip():
                continue                      # 内核线程，没有命令行
            # ppid 是 /proc/<pid>/stat 的第 4 个字段，但第 2 个字段（进程名）
            # 可能含空格和括号，直接 split() 会被带偏 —— 从最后一个 ')' 之后再切。
            tail = (entry / "stat").read_bytes().rsplit(b")", 1)[-1].split()
            ppid = int(tail[1]) if len(tail) > 1 else 0
        except (OSError, ValueError, IndexError):
            continue                          # 进程刚退出很常见，跳过就好
        procs[int(entry.name)] = (
            ppid, cmdline.decode("utf-8", errors="replace").lower())
    return procs


def reap_orphan_browsers() -> int:
    """回收上个进程遗留的抓取用 Chrome。

    ★ 与 db.reconcile_dangling_runs 同一个道理，只是对象是进程不是数据行：
      服务被杀 / 崩溃时，Selenium 起的 Chrome **不会跟着退出**，
      它继续占着 user-data-dir。下次启动时新 Chrome 连不上，报
      「cannot connect to chrome / chrome not reachable」，退避重试到天荒地老 ——
      实测就是这样把一整轮采集耗成 0 条。

    ★ 只杀**命令行里 user-data-dir 指向本项目 profile 目录**的进程树，
      绝不按进程名一刀切 —— 用户自己开着的 Chrome 不能碰。
      依据与回收僵尸轮次一致：新服务进程启动这一刻，本项目不该有活着的抓取浏览器。

    ★ 进程枚举按平台分两条，其余判定/杀树/清锁完全共用 ——
      并行 5 个 worker 时僵尸进程问题被放大 5 倍，这个回收器在服务器上
      比在本机更重要，绝不能因为"PowerShell 不存在"就静默返回 0。
    """
    target = str(PROFILE_DIR).lower()
    try:
        procs = (_scan_processes_win() if sys.platform == "win32"
                 else _scan_processes_proc())
    except Exception as e:  # noqa: BLE001
        log.debug("[selenium] 扫描残留浏览器失败（不影响启动）: %s", str(e)[:80])
        return 0

    roots = {pid for pid, (_, cl) in procs.items()
             if "--user-data-dir" in cl and target in cl}
    if not roots:
        return 0

    # 连同渲染子进程一起收（它们的命令行里没有 user-data-dir）
    tree, changed = set(roots), True
    while changed:
        changed = False
        for pid, (ppid, _) in procs.items():
            if ppid in tree and pid not in tree:
                tree.add(pid)
                changed = True

    killed = 0
    for pid in tree:
        try:
            os.kill(pid, 9)
            killed += 1
        except Exception:  # noqa: BLE001
            pass
    # 锁文件也要清，否则新实例仍会认为目录被占用。
    # ★ 大小写字符类不是洁癖：Windows 文件系统不区分大小写，`*LOCK*` 顺手就匹配到了
    #   `SingletonLock`；Linux **区分大小写**，同一个 glob 一个都匹配不到 ——
    #   杀了进程却留着锁，新 Chrome 照样起不来，而且看起来"回收成功了 N 个"。
    #   （Linux 上 SingletonLock 是符号链接，unlink 删链接本身，正是要的效果。）
    try:
        for lock in Path(PROFILE_DIR).rglob("*[Ll][Oo][Cc][Kk]*"):
            lock.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    if killed:
        log.info("[selenium] 回收上个进程遗留的抓取浏览器 %d 个（含子进程）", killed)
    return killed
