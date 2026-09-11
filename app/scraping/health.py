# -*- coding: utf-8 -*-
"""渠道体检：实测每个渠道的搜索 URL 还能不能用、能解析出多少商品。

为什么必须有这个：
  26 个渠道的搜索 URL 是按站点当前结构写的，站点改版就会失效。
  失效的表现不是报错，而是"今天这个渠道 0 条数据" —— 静默的、
  看起来像"友商今天没上新"。体检把这种情况**显式**报出来。

判定：
  ok            搜到了商品，解析正常
  empty         页面打开了但一个商品都解析不出 → 大概率选择器/URL 失效
  blocked       命中人机验证
  login_wall    强制登录墙
  failed        打不开
"""
from __future__ import annotations

import logging
import time

from .. import db
from .channels import build_adapter
from .relevance import check_batch

log = logging.getLogger("health")

# 体检用的搜索词：各国都有、结果一定不为空的大众词
PROBE_QUERY = {"es": "celular samsung", "pt": "celular samsung"}


def check_channel(engine, channel: dict, country: dict, meli_token: str = "",
                  probe_category: str = "phone") -> dict:
    t0 = time.time()
    adapter = build_adapter(channel, country, meli_token=meli_token)
    query = PROBE_QUERY.get(country["lang"], "celular samsung")

    if getattr(adapter, "force_fallback", False):
        host = (channel.get("base_url") or "").split("//")[-1].split("/")[0]
        if host:
            engine.forced_fallback_hosts.add(host)

    # 用 list_url 而不是 build_search_url：品牌官方商城要走品类页
    # （实测 samsung.com/mx 品类页有 10 个 JSON-LD Product，搜索页 0 个）
    url, url_mode = adapter.list_url(query, probe_category)
    result = {
        "channel_id": channel["id"], "country": country["code"],
        "name": channel["name"], "url": url, "items": 0,
        "status": "failed", "engine": "", "ms": 0, "note": "",
        "samples": [], "url_mode": url_mode,
    }
    if not url:
        result["note"] = "未配置 search_url"
        return result

    try:
        if hasattr(adapter, "collect"):
            items, status = adapter.collect(engine, query, 10)
            result["status"] = status.split(":")[0]
            result["note"] = f"通道={status.split(':')[-1]}"
        else:
            if channel.get("base_url"):
                engine.warm_up(channel["base_url"], country["code"])
            text, html = engine.fetch(url, country=country["code"],
                                      referer=channel.get("base_url"),
                                      wait_selector=adapter.list_wait_selector)
            if html is None:
                result["status"] = engine.last_status
                items = []
            else:
                items = adapter.parse_listings(html, text or "")
                result["status"] = "ok" if items else "empty"
                if not items:
                    # ★ 解析不到商品**不等于**选择器坏了。
                    #   实测 Ripley 秘鲁：Cloudflare Managed Challenge 返回的是一个
                    #   有正文的挑战页（不是 None），解析自然 0 条 —— 旧逻辑直接
                    #   判成 selector_broken/fix_selector，把"没过挑战"伪装成
                    #   "选择器坏了"，于是有人（我）真的去改了半天选择器。
                    #   下结论前先问一句：这页到底是不是商品页。
                    from .browser import classify_block
                    kind = classify_block(text or html or "")
                    if kind != "none":
                        result["status"] = {"captcha": "captcha",
                                            "throttled": "throttled",
                                            "challenge": "blocked"}[kind]
                        result["note"] = (f"返回的是拦截/挑战页而非商品页"
                                          f"（{kind}，HTML {len(html) // 1024}KB）")
                    else:
                        result["note"] = (f"页面打开了但解析不到商品"
                                          f"（HTML {len(html) // 1024}KB，未检出拦截特征）")
    except Exception as e:  # noqa: BLE001
        result["status"] = "failed"
        result["note"] = str(e)[:150]
        items = []

    # ★ 相关性校验：搜出来的东西必须真的是我们搜的东西。
    #   没有这一层，Coppel 那种「返回首页推荐位的运动鞋」会被报成 OK。
    #   品类页模式跳过：那是整页同品类商品，不是搜索结果。
    if items and url_mode != "category_page":
        rel = check_batch([i.title for i in items], query, ["Samsung", "Galaxy"])
        result["relevance"] = round(rel["relevance"], 2)
        if rel["verdict"] == "search_ineffective":
            result["status"] = "irrelevant"
            result["note"] = rel["reason"]
            items = []
        elif rel["verdict"] == "low_relevance":
            items = [it for i, it in enumerate(items) if i in set(rel["kept"])]
            result["note"] = rel["reason"]

    result["items"] = len(items)
    result["engine"] = engine.last_engine
    result["ms"] = int((time.time() - t0) * 1000)
    result["samples"] = [
        {"title": i.title[:70], "price": i.sale_price, "cur": i.currency}
        for i in items[:3]
    ]
    return result


def save_health(results: list[dict]) -> None:
    """写进 channel_health，供中央主 Agent 下一轮研判"""
    today = db.today()
    with db.tx() as conn:
        for r in results:
            ok = 1 if r["status"] == "ok" else 0
            verdict, action = _verdict(r)
            conn.execute("""
                INSERT INTO channel_health(channel_id,check_date,attempts,ok_count,
                    empty_count,blocked_count,failed_count,avg_items,avg_duration_ms,
                    health_score,verdict,suggested_action)
                VALUES(?,?,1,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel_id,check_date) DO UPDATE SET
                  attempts=channel_health.attempts+1,
                  ok_count=channel_health.ok_count+excluded.ok_count,
                  empty_count=channel_health.empty_count+excluded.empty_count,
                  blocked_count=channel_health.blocked_count+excluded.blocked_count,
                  failed_count=channel_health.failed_count+excluded.failed_count,
                  avg_items=excluded.avg_items, avg_duration_ms=excluded.avg_duration_ms,
                  health_score=excluded.health_score, verdict=excluded.verdict,
                  suggested_action=excluded.suggested_action
            """, (r["channel_id"], today, ok,
                  1 if r["status"] == "empty" else 0,
                  1 if r["status"] in ("blocked", "login_wall") else 0,
                  1 if r["status"] == "failed" else 0,
                  r["items"], r["ms"], 1.0 if ok else 0.0, verdict, action))


def _verdict(r: dict) -> tuple[str, str]:
    """把采集状态翻译成健康判定。

    ★ 「要你做一件事才能通」和「站点真的在拦我们」必须分开 ——
      处置完全不同，混成一个标签会让人以为都是反爬问题：
        login_wall  → 配 token / 登录一次，跟反爬无关
        captcha     → 需你本人过一次人机验证
        blocked     → 真被限流，降频或换时段
      实测：4 个 MercadoLibre 都只是没配 token，界面却显示"被风控"。
    """
    s = r["status"]
    if s == "ok":
        return ("healthy", "continue") if r["items"] >= 3 else ("degraded", "check_selector")
    if s == "irrelevant":
        return "selector_broken", "fix_search_url"
    if s == "brand_absent":
        # 该渠道不卖这个品牌 —— 是选品事实，不是故障
        return "healthy", "continue"
    if s == "empty":
        # ★ 上面已经先做过拦截特征判定：能认出来的挑战/限流页不会走到这里。
        #   剩下的 empty 是**真·三义**：选择器坏了 / 没认出来的拦截 / 这个词确实没货。
        #   旧代码一律断言 selector_broken —— 一个自信的错误诊断比"不知道"有害得多，
        #   它会让人去改根本没坏的选择器（Ripley PE 就是这么被误导的）。
        return "empty", "investigate"
    if s == "login_wall":
        return "need_login", "need_login"
    if s == "captcha":
        return "need_captcha", "user_verify"
    if s in ("blocked", "throttled"):
        return "rate_limited", "slow_down"
    return "dead", "fix_url"


STATUS_ICON = {"ok": "OK  ", "empty": "空  ", "blocked": "拦截", "irrelevant": "串味",
               "login_wall": "登录", "failed": "失败", "unauthorized": "无权"}


def print_report(results: list[dict]) -> None:
    print("\n" + "=" * 78)
    print(f"{'国':<3}{'渠道':<24}{'状态':<6}{'条数':>4}{'耗时':>7}  说明")
    print("-" * 78)
    for r in sorted(results, key=lambda x: (x["country"], -x["items"])):
        icon = STATUS_ICON.get(r["status"], r["status"][:4])
        note = r["note"][:32]
        if r["samples"]:
            s = r["samples"][0]
            note = f"{s['title'][:26]} {s['price']}"
        print(f"{r['country']:<3}{r['name'][:23]:<24}{icon:<6}{r['items']:>4}"
              f"{r['ms']/1000:>6.1f}s  {note}")
    print("-" * 78)
    n_ok = sum(1 for r in results if r["status"] == "ok")
    print(f"可用 {n_ok}/{len(results)}")
    bad = [r for r in results if r["status"] != "ok"]
    if bad:
        print("\n需要处理：")
        for r in bad:
            _, action = _verdict(r)
            hint = {
                "fix_selector": "页面能开但解析不出 → 该站需要写专用适配器",
                "fix_search_url": "★搜索没生效，抓回来的是别的品类 → 必须修 search_url",
                "fix_url": "打不开 → 核对 search_url 或该站需代理",
                "slow_down": "被风控 → 降频/换时段/走兜底引擎",
                "need_login": "强制登录墙 → 需保存登录态或用官方 API",
                "check_selector": "结果偏少 → 建议核对搜索词与解析",
            }.get(action, action)
            print(f"  [{r['country']}] {r['name']}: {hint}")
            if r["note"]:
                print(f"        {r['note'][:70]}")
