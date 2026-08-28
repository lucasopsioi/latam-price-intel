# -*- coding: utf-8 -*-
"""Telegram 每日简报推送。

简报内容（按用户要求）：今天哪些国家、哪个厂商发布了新产品，
外加价格异动与促销，以及运行健康度（哪些渠道没抓到，别让人以为"友商没动静"）。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from . import db

log = logging.getLogger("notify")

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3900          # Telegram 单条上限 4096，留余量


def send_telegram(text: str, chat_id: str = "") -> tuple[bool, str]:
    token = db.get_setting("telegram_bot_token", "")
    chat_id = chat_id or db.get_setting("telegram_chat_id", "")
    if not token:
        return False, "未配置 telegram_bot_token（设置页填）"
    if not chat_id:
        return False, "未配置 telegram_chat_id（设置页填）"

    for chunk in _split(text):
        try:
            r = httpx.post(API.format(token=token), timeout=30,
                           json={"chat_id": chat_id, "text": chunk,
                                 "parse_mode": "HTML",
                                 "disable_web_page_preview": True})
            if r.status_code != 200:
                detail = r.json().get("description", r.text[:150]) \
                    if r.headers.get("content-type", "").startswith("application/json") \
                    else r.text[:150]
                return False, f"HTTP {r.status_code}: {detail}"
        except Exception as e:  # noqa: BLE001
            return False, f"请求失败：{str(e)[:150]}"
    return True, "已发送"


def _split(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_LEN:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def build_daily_brief(for_date: str | None = None) -> str:
    d = for_date or db.today()
    yesterday = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
    lines = [f"<b>📊 拉美竞品日报 · {d}</b>", ""]

    # —— 新品 ——
    new_products = db.q("""
        SELECT rp.model_name, rp.category_code, rp.global_launch_date,
               b.name AS brand
        FROM rival_product rp JOIN brand b ON b.id=rp.brand_id
        WHERE date(rp.created_at)>=? ORDER BY rp.created_at DESC LIMIT 15
    """, (yesterday,))
    if new_products:
        lines.append(f"<b>🆕 新发现产品（{len(new_products)}）</b>")
        for p in new_products:
            gl = f"，全球首发 {p['global_launch_date']}" if p["global_launch_date"] else ""
            lines.append(f"· {p['brand']} <b>{p['model_name']}</b>"
                         f"（{_cat_zh(p['category_code'])}{gl}）")
        lines.append("")

    # —— 各国上市 ——
    launches = db.q("""
        SELECT le.event_date, le.days_after_global, le.country_rank,
               rp.model_name, b.name AS brand, co.name_zh AS country
        FROM launch_event le
        JOIN rival_product rp ON rp.id=le.rival_product_id
        JOIN brand b ON b.id=le.brand_id
        JOIN country co ON co.code=le.country_code
        WHERE le.event_type='country_available' AND le.event_date>=?
        ORDER BY le.event_date DESC LIMIT 12
    """, (yesterday,))
    if launches:
        lines.append(f"<b>🚀 拉美上市动态（{len(launches)}）</b>")
        for l in launches:
            lag = (f"，距全球首发 {l['days_after_global']} 天"
                   if l["days_after_global"] is not None else "")
            rank = f"，拉美第 {l['country_rank']} 国" if l["country_rank"] else ""
            lines.append(f"· {l['country']}：{l['brand']} <b>{l['model_name']}</b>{lag}{rank}")
        lines.append("")

    # —— 价格异动 ——
    moves = db.q("""
        SELECT t.country_code, t.model_name, t.brand, t.channel, t.rom_gb,
               t.today_price, t.prev_price, t.currency,
               ROUND((t.today_price - t.prev_price) * 100.0 / t.prev_price, 1) AS pct
        FROM (
          SELECT po.country_code, rp.model_name, b.name AS brand, c.name AS channel,
                 po.currency, po.rom_gb, po.sale_price AS today_price,
                 -- ★ 必须按同一 SKU 比价：容量与颜色都要对齐。
                 --   只按 rival_product_id + channel 比，会拿今天的 128G
                 --   去减昨天的 512G，播报出一个根本不存在的"降价 40%"。
                 --   这种错报比不报危险 —— 它看起来像一条真情报。
                 (SELECT p2.sale_price FROM price_obs p2
                   WHERE p2.rival_product_id=po.rival_product_id
                     AND p2.country_code=po.country_code
                     AND p2.channel_id=po.channel_id
                     AND IFNULL(p2.rom_gb,-1)=IFNULL(po.rom_gb,-1)
                     AND IFNULL(p2.ram_gb,-1)=IFNULL(po.ram_gb,-1)
                     AND IFNULL(p2.color,'')=IFNULL(po.color,'')
                     AND p2.currency=po.currency
                     AND p2.obs_date < po.obs_date
                     AND p2.audit_status<>'rejected'
                   ORDER BY p2.obs_date DESC LIMIT 1) AS prev_price
          FROM price_obs po
          JOIN rival_product rp ON rp.id=po.rival_product_id
          JOIN brand b ON b.id=po.brand_id
          JOIN channel c ON c.id=po.channel_id
          WHERE po.obs_date=? AND po.audit_status<>'rejected'
        ) t
        WHERE t.prev_price IS NOT NULL
          AND ABS(t.today_price - t.prev_price) * 1.0 / t.prev_price >= 0.05
        ORDER BY ABS(t.today_price - t.prev_price) * 1.0 / t.prev_price DESC LIMIT 12
    """, (d,))
    if moves:
        lines.append(f"<b>💰 价格异动 ≥5%（{len(moves)}）</b>")
        for m in moves:
            arrow = "🔻" if m["pct"] < 0 else "🔺"
            cap = f" {m['rom_gb']}G" if m["rom_gb"] else ""
            lines.append(f"{arrow} {m['country_code']} {m['brand']} {m['model_name']}{cap}"
                         f" @{m['channel']}：{m['prev_price']:,.0f} → "
                         f"{m['today_price']:,.0f} {m['currency']}（{m['pct']:+.1f}%）")
        lines.append("")

    # —— 消费者口碑 ——
    hot = db.q("""
        SELECT rpf.total_reviews, rpf.avg_rating, rpf.hot_reason,
               rp.model_name, b.name AS brand, rpf.country_code
        FROM review_profile rpf
        JOIN rival_product rp ON rp.id=rpf.rival_product_id
        JOIN brand b ON b.id=rp.brand_id
        WHERE rpf.is_hot=1 AND date(rpf.last_fetched)>=?
        ORDER BY rpf.total_reviews DESC LIMIT 6
    """, (yesterday,))
    if hot:
        lines.append(f"<b>🔥 主销款信号（评论量高，{len(hot)}）</b>")
        for h in hot:
            rating = f" {h['avg_rating']:.1f}★" if h["avg_rating"] else ""
            reason = f"\n   ↳ {h['hot_reason'][:80]}" if h["hot_reason"] else ""
            lines.append(f"· {h['country_code']} {h['brand']} <b>{h['model_name']}</b>："
                         f"{h['total_reviews']:,} 条评论{rating}{reason}")
        lines.append("")

    voc = db.q("""
        SELECT vi.summary_zh, vi.complaint_points, vi.vs_acme,
               rp.model_name, b.name AS brand, vi.country_code
        FROM voc_insight vi
        JOIN rival_product rp ON rp.id=vi.rival_product_id
        JOIN brand b ON b.id=rp.brand_id
        WHERE date(vi.created_at)>=? ORDER BY vi.review_count DESC LIMIT 5
    """, (yesterday,))
    if voc:
        lines.append(f"<b>💬 竞品口碑要点（{len(voc)}）</b>")
        for v in voc:
            lines.append(f"· {v['country_code']} {v['brand']} <b>{v['model_name']}</b>："
                         f"{(v['summary_zh'] or '')[:70]}")
            if v["vs_acme"]:
                lines.append(f"   ⚠ 提到Acme：{v['vs_acme'][:80]}")
        lines.append("")

    # —— 重要情报 ——
    intel = db.q("""
        SELECT d.title, d.summary_zh, d.tag, d.importance, b.name AS brand,
               co.name_zh AS country
        FROM dynamics d
        LEFT JOIN brand b ON b.id=d.brand_id
        LEFT JOIN country co ON co.code=d.country_code
        WHERE date(d.created_at)>=? AND d.importance>=4
        ORDER BY d.importance DESC LIMIT 10
    """, (yesterday,))
    if intel:
        lines.append(f"<b>📰 重要情报（{len(intel)}）</b>")
        for i in intel:
            who = f"{i['brand']} " if i["brand"] else ""
            where = f"[{i['country']}] " if i["country"] else ""
            txt = i["summary_zh"] or i["title"][:70]
            lines.append(f"· {where}{who}{txt}")
        lines.append("")

    # —— 运行健康度 ——
    # ★ 这一段不能省：抓取失败时如果不说，日报就变成"友商今天很安静"，
    #   而真相是我们的采集挂了。宁可报丑话，也不能让人误判形势。
    run = db.q1("SELECT * FROM scrape_run ORDER BY id DESC LIMIT 1")
    if run:
        bad = (run["pages_blocked"] or 0) + (run["pages_failed"] or 0)
        total = (run["pages_ok"] or 0) + bad
        lines.append(f"<b>⚙️ 采集健康度</b>")
        lines.append(f"· 成功 {run['pages_ok']}／失败 {bad}"
                     f"（共 {total} 个采集单元，写入 {run['rows_written']} 条）")
        if bad:
            broken = db.q("""
                SELECT c.name, co.name_zh AS country, su.status, COUNT(*) n
                FROM scrape_unit su
                JOIN channel c ON c.id=su.channel_id
                JOIN country co ON co.code=su.country_code
                WHERE su.run_id=? AND su.status<>'ok'
                GROUP BY c.id, su.status ORDER BY n DESC LIMIT 6
            """, (run["id"],))
            for b in broken:
                lines.append(f"  ⚠ {b['country']}/{b['name']}：{_status_zh(b['status'])}")

    if len(lines) <= 3:
        lines.append("今日无新增情报。")
    return "\n".join(lines)


def send_daily_brief(for_date: str | None = None) -> dict:
    text = build_daily_brief(for_date)
    ok, msg = send_telegram(text)
    if ok:
        with db.tx() as conn:
            conn.execute("""UPDATE dynamics SET is_pushed=1
                            WHERE is_pushed=0 AND importance>=4""")
    return {"ok": ok, "message": msg, "preview": text[:1500], "length": len(text)}


_CAT = {"phone": "手机", "wearable": "穿戴", "audio": "音频",
        "tablet": "平板", "pc": "PC"}
_STATUS = {"blocked": "被人机验证拦截", "login_wall": "强制登录墙",
           "empty": "解析不到商品（疑似选择器失效）",
           "irrelevant": "搜索未生效（抓回来的不是目标品类）",
           "failed": "页面打不开", "skipped": "已跳过"}


def _cat_zh(c: str) -> str:
    return _CAT.get(c, c or "")


def _status_zh(s: str) -> str:
    return _STATUS.get(s, s)
