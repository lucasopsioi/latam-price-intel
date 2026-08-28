# -*- coding: utf-8 -*-
"""双引擎路由。主引擎可配，另一个自动兜底。

★ 默认 Selenium(undetected) 为主、Playwright 兜底 —— 用户选定，且实测支持：
  Liverpool 在 Playwright 下冷却换设备重试两次仍被拦死（每轮白耗 120 秒），
  Selenium 一次通过拿到 56 条。undetected-chromedriver 直接给 chromedriver
  打补丁抹掉自动化特征，比 Playwright 的 CDP 通道更难被指纹脚本识别。

  代价是慢：Selenium 单页耗时是 Playwright 的 2~4 倍。但用户明确表示
  「每天抓 10~20 小时都不在乎，只要 24 小时内完成、数据准确」，
  所以这笔交易做得过 —— 抓得慢好过抓不到 / 被封号。

路由规则（对上层透明，采集器不需要知道自己在用哪个引擎）：
  1. 默认全部走主引擎
  2. 某域名被主引擎连续拦 3 次 → 记入 needs_fallback，之后该域名走另一个
  3. 兜底引擎懒启动：整轮没有域名需要兜底就永远不开那个进程
  4. 兜底也失败 → 如实上报 blocked，不再往下试（没有第三级了）
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from .. import livelog
from .browser import PlaywrightBrowser
from .selenium_driver import SeleniumBrowser

log = logging.getLogger("engine")


class ScrapeEngine:
    """统一入口。用法与单个 Browser 完全一致。"""

    def __init__(self, cfg: dict, profile_tag: str = ""):
        # ★ profile_tag：同一国家可能有**多个域名组并行**（用户要求的跨域名并行），
        #   而浏览器 profile 目录原本只按国家分 ⇒ 两个 worker 抢同一个
        #   user-data-dir ⇒ 第二个必然报 "cannot connect to chrome"。
        #   实测因此把整轮采集卡成 0 条（一直在退避重试）。
        #   按域名分目录既解决冲突，语义上也更对 —— Cookie 本来就是按域名的。
        self.cfg = dict(cfg or {})
        if profile_tag:
            self.cfg["profile_tag"] = profile_tag
        cfg = self.cfg
        primary_name = str(cfg.get("engine", "selenium")).lower()
        fallback_name = str(cfg.get("fallback_engine", "playwright")).lower()
        self._primary = (SeleniumBrowser(cfg) if primary_name == "selenium"
                         else PlaywrightBrowser(cfg))
        self._fallback_cls = (PlaywrightBrowser if fallback_name == "playwright"
                              else SeleniumBrowser)
        self._fallback = None
        self._primary_dead_logged = False
        self._fallback_enabled = fallback_name in ("playwright", "selenium") \
            and fallback_name != primary_name
        log.info("抓取引擎：主=%s 兜底=%s", primary_name,
                 fallback_name if self._fallback_enabled else "关闭")
        # 强制走兜底引擎的域名。两个来源：
        #   ① 渠道配置里 force_engine: selenium（实测钉死的，如 Liverpool）
        #   ② 运行中被 Playwright 连拦 3 次自动升级的
        self.forced_fallback_hosts: set[str] = set()
        self._load_forced_hosts()
        self.last_status = "ok"
        self.last_engine = self._primary.name
        self.stats = {"playwright": 0, "selenium": 0, "fallback_switches": 0}

    def _load_forced_hosts(self) -> None:
        """渠道配置里 force_engine 指定了「非主引擎」的域名，开跑前就登记，
        省掉一轮必然失败的尝试。"""
        try:
            from .. import db
            other = self._fallback_cls.name
            for row in db.q("""SELECT base_url FROM channel
                               WHERE force_engine=? AND enabled=1""", (other,)):
                host = (row["base_url"] or "").split("//")[-1].split("/")[0]
                if host:
                    self.forced_fallback_hosts.add(host)
            if self.forced_fallback_hosts:
                log.info("按配置钉死走 %s 的域名: %s", other,
                         ", ".join(sorted(self.forced_fallback_hosts)))
        except Exception as e:  # noqa: BLE001
            log.debug("读取 force_engine 配置失败（不影响运行）: %s", e)

    # ---------------- 路由 ----------------
    def _need_fallback(self, url: str) -> bool:
        if not self._fallback_enabled:
            return False
        # ★ 主引擎**整体**不可用（浏览器起不动）时，所有域名都走兜底。
        #   否则每个 fetch 都去撞一个起不来的浏览器，整组单元一路失败到底，
        #   而备胎从未被唤醒 —— 实测一次启动失败废掉一个渠道的全部 24 个单元。
        if getattr(self._primary, "is_unavailable", False):
            if not self._primary_dead_logged:
                log.warning("主引擎 %s 整体不可用，本轮全部改走兜底引擎 %s",
                            self._primary.name, self._fallback_cls.name)
                self._primary_dead_logged = True
            return True
        host = urlparse(url).netloc
        return host in self.forced_fallback_hosts or host in self._primary.needs_fallback

    def _get_fallback(self):
        if self._fallback is None:
            log.info("首次需要兜底引擎，启动 %s…", self._fallback_cls.name)
            self._fallback = self._fallback_cls(self.cfg)
            self.stats["fallback_switches"] += 1
        return self._fallback

    def _pick(self, url: str):
        # ★ 引擎名必须从实例读，不能硬编码。
        #   曾经写死 "selenium"/"playwright"，在主备互换后每条采集留痕
        #   都记反了 —— 界面「运行记录」显示的引擎与实际用的正好相反，
        #   排查被封问题时会把人引到完全错误的方向。
        if self._need_fallback(url):
            drv = self._get_fallback()
            self.last_engine = drv.name
            return drv
        self.last_engine = self._primary.name
        return self._primary

    # ---------------- 对外（与 Browser 同接口）----------------
    def warm_up(self, home_url: str, country: str) -> None:
        # ★ warm_up 绕过了 fetch()，所以事件要单独发 ——
        #   而它恰恰是每个渠道的**第一个**页面请求，也是最慢的那个
        #   （要启动浏览器 + 加载首页，实测 60 秒）。
        #   漏了这一条，界面在每个渠道开头都有整整一分钟毫无动静。
        host = (home_url or "").split("//")[-1].split("/")[0]
        drv = self._pick(home_url)
        livelog.emit("page", f"→ 暖场 {host}（{drv.name}，首次要启浏览器，约需 1 分钟）",
                     url=home_url[:180], engine=drv.name)
        t0 = time.monotonic()
        drv.warm_up(home_url, country)
        livelog.emit("page", f"← 暖场完成 {host} {time.monotonic() - t0:.0f}s",
                     url=home_url[:180])

    def fetch(self, url, **kw):
        drv = self._pick(url)
        # ★ 每个页面请求都发事件：单页要 10~90 秒（有头模式更慢），
        #   没有这一层的话，界面在"某个页面正在加载"期间完全没有动静。
        host = (url or "").split("//")[-1].split("/")[0]
        livelog.emit("page", f"→ 打开 {host}{'' if len(url) < 70 else ''} "
                             f"（{drv.name}）", url=url[:180], engine=drv.name)
        t0 = time.monotonic()
        result = drv.fetch(url, **kw)
        el = time.monotonic() - t0
        self.last_status = drv.last_status
        self.stats[drv.name] = self.stats.get(drv.name, 0) + 1
        got = "有内容" if (result and result[1]) else "无内容"
        livelog.emit("page", f"← {host} {drv.last_status} {got} {el:.0f}s",
                     url=url[:180], status=drv.last_status)

        # Playwright 刚刚把这个域名判成需要兜底 → 本次立即用兜底重试一遍，
        # 不必等到下一个页面才切（否则每次切换都白白丢一页数据）
        if (result == (None, None) and drv is self._primary
                and self._need_fallback(url)):
            fb = self._get_fallback()
            log.info("%s 失手且该域名已标记兜底，立即用 %s 重试：%s",
                     drv.name, fb.name, url[:80])
            result = fb.fetch(url, **kw)
            self.last_status = fb.last_status
            self.last_engine = fb.name
            self.stats[fb.name] = self.stats.get(fb.name, 0) + 1
        return result

    def run_on_page(self, url, fn, **kw):
        drv = self._pick(url)
        result = drv.run_on_page(url, fn, **kw)
        self.last_status = drv.last_status
        self.stats[drv.name] = self.stats.get(drv.name, 0) + 1
        return result

    # ---------------- 汇总 ----------------
    @property
    def block_events(self) -> list:
        ev = list(self._primary.block_events)
        if self._fallback:
            ev += self._fallback.block_events
        return ev

    @property
    def login_wall_hosts(self) -> set:
        s = set(self._primary.login_wall_hosts)
        if self._fallback:
            s |= self._fallback.login_wall_hosts
        return s

    @property
    def fallback_hosts(self) -> set:
        return set(self._primary.needs_fallback) | self.forced_fallback_hosts

    def summary(self) -> dict:
        return {
            "pages_by_engine": dict(self.stats),
            "fallback_hosts": sorted(self.fallback_hosts),
            "blocked_hosts": sorted({h for h, _ in self.block_events}),
            "login_wall_hosts": sorted(self.login_wall_hosts),
        }

    def close(self) -> None:
        try:
            self._primary.save_all_states()
        except Exception:  # noqa: BLE001
            pass
        self._primary.close()
        if self._fallback:
            self._fallback.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
