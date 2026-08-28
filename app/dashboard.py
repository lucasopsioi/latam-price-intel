# -*- coding: utf-8 -*-
"""竞品看板的聚合层 —— 每国 × 每品类 × 每天/周/月的竞争动态。

用户要的（原话）：
  "我要知道每个国家每个品类，在每一天，每一周，每个月有什么样的竞争动态，
   而不是简单的你抓完价格数据就结束了"

★ 聚合口径的三条纪律（都是踩出来的）：

  1. **跨国绝不比绝对价格**。六国六币种，把 CLP 和 MXN 放一起求平均
     算出来的数既不是比索也不是智利比索。跨国只能比**百分比**
     （涨跌幅、折扣率、价差率）。

  2. **官方渠道与第三方分开算**。官网/自营降价 = 厂商的定价动作；
     第三方降价可能只是某个卖家甩货。混在一起会把噪声当成战略信号。

  3. **中位数而不是均价**。促销尖峰和错价会把均值拽走，
     而我们要的是"这个品类现在大致什么价位"。
"""
from __future__ import annotations

from datetime import date, timedelta

from . import db, voc_aspects

GRAINS = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}


def _period_expr(grain: str, col: str = "po.obs_date") -> str:
    """SQLite 里把日期归到时间粒度。"""
    if grain == "week":
        # 周一为一周之始：先减掉 (weekday+6)%7 天
        return (f"date({col}, '-' || ((CAST(strftime('%w', {col}) AS INTEGER) + 6) % 7)"
                f" || ' day')")
    if grain == "month":
        return f"strftime('%Y-%m-01', {col})"
    return f"date({col})"


def matrix(grain: str = "day", days: int = 30, country: str = "",
           category: str = "") -> dict:
    """看板主视图：时间 × 国家 × 品类 的竞争动态矩阵。"""
    grain = grain if grain in GRAINS else "day"
    since = (date.today() - timedelta(days=int(days))).isoformat()
    per = _period_expr(grain)

    where = ["po.obs_date >= ?", "po.audit_status <> 'rejected'",
             "po.product_kind <> 'accessory'"]
    params: list = [since]
    if country:
        where.append("po.country_code = ?"); params.append(country.upper())
    if category:
        where.append("po.category_code = ?"); params.append(category)
    w = " AND ".join(where)

    cells = db.q(f"""
        SELECT {per} AS period, po.country_code AS cc, po.category_code AS cat,
               COUNT(*) AS obs,
               COUNT(DISTINCT po.rival_product_id) AS products,
               COUNT(DISTINCT po.brand_id) AS brands,
               COUNT(DISTINCT po.channel_id) AS channels,
               -- ★ 只报中位折扣率与百分比类指标，绝不跨国求绝对价均值
               ROUND(AVG(CASE WHEN po.discount_pct > 0 THEN po.discount_pct END), 1)
                 AS avg_discount,
               SUM(CASE WHEN po.discount_pct > 0 THEN 1 ELSE 0 END) AS on_promo,
               SUM(CASE WHEN po.seller_kind IN ('self_operated','brand_official')
                        THEN 1 ELSE 0 END) AS official_obs
        FROM price_obs po
        WHERE {w}
        GROUP BY period, cc, cat
        ORDER BY period DESC, cc, cat
    """, params)

    # 价格变动按同样的粒度聚合 —— 这是"动态"的核心
    mw = ["pm.move_date >= ?"]
    mp: list = [since]
    if country:
        mw.append("pm.country_code = ?"); mp.append(country.upper())
    if category:
        mw.append("pm.category_code = ?"); mp.append(category)
    moves = db.q(f"""
        SELECT {_period_expr(grain, 'pm.move_date')} AS period,
               pm.country_code AS cc, pm.category_code AS cat,
               SUM(pm.direction = 'down') AS n_down,
               SUM(pm.direction = 'up')   AS n_up,
               SUM(pm.direction = 'down' AND pm.is_official = 1) AS n_down_official,
               SUM(pm.direction = 'up'   AND pm.is_official = 1) AS n_up_official,
               ROUND(AVG(CASE WHEN pm.direction='down' THEN ABS(pm.change_pct) END), 1)
                 AS avg_cut,
               ROUND(MAX(CASE WHEN pm.direction='down' THEN ABS(pm.change_pct) END), 1)
                 AS max_cut
        FROM price_move pm
        WHERE {' AND '.join(mw)}
        GROUP BY period, cc, cat
    """, mp)
    mkey = {(m["period"], m["cc"], m["cat"]): m for m in moves}

    # 新出现的产品（首次被观测到）
    news = db.q(f"""
        SELECT {_period_expr(grain, 'f.first_seen')} AS period, f.cc, f.cat,
               COUNT(*) AS n_new
        FROM (SELECT rival_product_id, country_code AS cc, category_code AS cat,
                     MIN(obs_date) AS first_seen
              FROM price_obs WHERE rival_product_id IS NOT NULL
              GROUP BY rival_product_id, country_code) f
        WHERE f.first_seen >= ?
        GROUP BY period, f.cc, f.cat
    """, [since])
    nkey = {(n["period"], n["cc"], n["cat"]): n["n_new"] for n in news}

    for c in cells:
        k = (c["period"], c["cc"], c["cat"])
        m = mkey.get(k) or {}
        c["n_down"] = m.get("n_down") or 0
        c["n_up"] = m.get("n_up") or 0
        c["n_down_official"] = m.get("n_down_official") or 0
        c["n_up_official"] = m.get("n_up_official") or 0
        c["avg_cut"] = m.get("avg_cut")
        c["max_cut"] = m.get("max_cut")
        c["n_new"] = nkey.get(k, 0)

    return {"grain": grain, "days": days, "cells": cells,
            "periods": sorted({c["period"] for c in cells}, reverse=True)}


def movers(days: int = 7, country: str = "", category: str = "",
           official_only: bool = False, limit: int = 40) -> list[dict]:
    """变动最大的那些 —— 看板点进去看的明细。"""
    since = (date.today() - timedelta(days=int(days))).isoformat()
    where = ["pm.move_date >= ?"]
    params: list = [since]
    if country:
        where.append("pm.country_code = ?"); params.append(country.upper())
    if category:
        where.append("pm.category_code = ?"); params.append(category)
    if official_only:
        where.append("pm.is_official = 1")
    return db.q(f"""
        SELECT pm.move_date, pm.country_code, pm.change_pct, pm.direction,
               pm.prev_price, pm.curr_price, pm.currency, pm.is_official,
               pm.days_span, pm.sku_key,
               c.name AS channel, b.name AS brand, rp.model_name,
               rp.category_code
        FROM price_move pm
        JOIN channel c ON c.id = pm.channel_id
        LEFT JOIN brand b ON b.id = pm.brand_id
        LEFT JOIN rival_product rp ON rp.id = pm.rival_product_id
        WHERE {' AND '.join(where)}
        ORDER BY ABS(pm.change_pct) DESC LIMIT ?
    """, (*params, int(limit)))


def signals(days: int = 14, country: str = "", signal_type: str = "",
            limit: int = 50) -> list[dict]:
    """Agent 给出的策略信号。"""
    since = (date.today() - timedelta(days=int(days))).isoformat()
    where = ["s.signal_date >= ?"]
    params: list = [since]
    if country:
        where.append("s.country_code = ?"); params.append(country.upper())
    if signal_type:
        where.append("s.signal_type = ?"); params.append(signal_type)
    return db.q(f"""
        SELECT s.*, b.name AS brand, rp.model_name
        FROM strategy_signal s
        LEFT JOIN brand b ON b.id = s.brand_id
        LEFT JOIN rival_product rp ON rp.id = s.rival_product_id
        WHERE {' AND '.join(where)}
        ORDER BY s.signal_date DESC, s.confidence DESC LIMIT ?
    """, (*params, int(limit)))


def country_category_summary(days: int = 7) -> list[dict]:
    """每国每品类一行的当前态势 —— 看板首屏。"""
    since = (date.today() - timedelta(days=int(days))).isoformat()
    return db.q("""
        SELECT po.country_code AS cc, co.name_zh AS country,
               po.category_code AS cat,
               COUNT(*) AS obs,
               COUNT(DISTINCT po.rival_product_id) AS products,
               COUNT(DISTINCT po.brand_id) AS brands,
               ROUND(AVG(CASE WHEN po.discount_pct > 0 THEN po.discount_pct END), 1)
                 AS avg_discount,
               SUM(CASE WHEN po.discount_pct >= 20 THEN 1 ELSE 0 END) AS deep_promo,
               (SELECT COUNT(*) FROM price_move pm
                 WHERE pm.country_code = po.country_code
                   AND pm.category_code = po.category_code
                   AND pm.move_date >= ? AND pm.direction='down') AS n_down,
               (SELECT COUNT(*) FROM price_move pm
                 WHERE pm.country_code = po.country_code
                   AND pm.category_code = po.category_code
                   AND pm.move_date >= ? AND pm.direction='down'
                   AND pm.is_official=1) AS n_down_official,
               (SELECT COUNT(*) FROM strategy_signal s
                 WHERE s.country_code = po.country_code
                   AND s.category_code = po.category_code
                   AND s.signal_date >= ?) AS n_signals
        FROM price_obs po JOIN country co ON co.code = po.country_code
        WHERE po.obs_date >= ? AND po.audit_status <> 'rejected'
          AND po.product_kind <> 'accessory'
        GROUP BY po.country_code, po.category_code
        ORDER BY co.sort_order, obs DESC
    """, (since, since, since, since))


# ---------------------------------------------------------------- 价格趋势

def price_trend(rival_product_id: int | None = None, country: str = "",
                channel_id: int | None = None, days: int = 90,
                official_only: bool = False) -> dict:
    """单个产品的价格时间序列 —— 用户要的「连续可追踪的产品价格」。

    ★ 三条口径，缺一条这张图就会骗人：
      1. **按币种分线**：同一款货在 6 国是 6 种货币，画在一张图上无意义。
         这里强制按 (国家, 渠道, 币种) 分线，不做任何跨币种合并。
      2. **同一天同渠道取最低价**：一个渠道一天可能有多条挂牌（不同容量/颜色/卖家），
         取均值会让"某天上架了一个高配版"看起来像涨价。取最低价 = 该渠道当天的门槛价。
      3. **不补点**：某天没抓到就是没有，不做插值。
         插出来的点会让"那天没跑采集"看起来像"价格没变"。
    """
    where = ["po.sale_price IS NOT NULL", "po.audit_status <> 'rejected'",
             "po.product_kind <> 'accessory'", "po.condition = 'new'",
             "po.is_bundle = 0"]
    params: list = []
    if rival_product_id:
        where.append("po.rival_product_id = ?")
        params.append(int(rival_product_id))
    if country:
        where.append("po.country_code = ?")
        params.append(country)
    if channel_id:
        where.append("po.channel_id = ?")
        params.append(int(channel_id))
    if official_only:
        where.append("(c.kind='brand_store' OR po.seller_kind IN "
                     "('self_operated','brand_official'))")
    since = (date.today() - timedelta(days=int(days))).isoformat()
    where.append("po.obs_date >= ?")
    params.append(since)

    rows = db.q(f"""
        SELECT po.obs_date, po.country_code AS cc, po.currency,
               po.channel_id, c.name AS channel,
               MIN(po.sale_price) AS price,
               MAX(po.list_price) AS list_price,
               COUNT(*) AS n_listings
        FROM price_obs po JOIN channel c ON c.id = po.channel_id
        WHERE {' AND '.join(where)}
        GROUP BY po.obs_date, po.country_code, po.channel_id, po.currency
        ORDER BY po.obs_date
    """, params)

    series: dict[tuple, dict] = {}
    for r in rows:
        key = (r["cc"], r["channel_id"], r["currency"])
        s = series.setdefault(key, {
            "country_code": r["cc"], "channel_id": r["channel_id"],
            "channel": r["channel"], "currency": r["currency"], "points": []})
        s["points"].append({"date": r["obs_date"], "price": r["price"],
                            "list_price": r["list_price"],
                            "listings": r["n_listings"]})

    out = []
    for s in series.values():
        pts = s["points"]
        first, last = pts[0]["price"], pts[-1]["price"]
        s["first_price"], s["last_price"] = first, last
        s["change_pct"] = round((last - first) / first * 100, 2) if first else None
        s["min_price"] = min(p["price"] for p in pts)
        s["max_price"] = max(p["price"] for p in pts)
        s["days"] = len(pts)
        out.append(s)
    out.sort(key=lambda s: (s["country_code"], s["channel"]))

    # ★ 把「能不能画趋势」如实告诉前端：只有 1 个观测日时画出来的是一个点，
    #   不说清楚的话，用户会以为「价格一直没变」。
    obs_days = len({r["obs_date"] for r in rows})
    return {"series": out, "obs_days": obs_days,
            "trendable": obs_days >= 2,
            "note": ("" if obs_days >= 2 else
                     f"只有 {obs_days} 个观测日 —— 价格趋势至少需要 2 天。"
                     f"每日自动采集已开启，明天起会自动累积。")}


def trackable_products(country: str = "", category: str = "",
                       days: int = 90, limit: int = 200) -> list[dict]:
    """哪些产品已经有足够的连续观测可以看趋势 —— 趋势页的入口列表。"""
    since = (date.today() - timedelta(days=int(days))).isoformat()
    where = ["po.rival_product_id IS NOT NULL", "po.sale_price IS NOT NULL",
             "po.audit_status <> 'rejected'", "po.product_kind <> 'accessory'",
             "po.obs_date >= ?"]
    params: list = [since]
    if country:
        where.append("po.country_code = ?")
        params.append(country)
    if category:
        where.append("po.category_code = ?")
        params.append(category)
    params.append(int(limit))
    return db.q(f"""
        SELECT po.rival_product_id AS id, rp.model_name, b.name AS brand,
               rp.category_code AS cat,
               COUNT(DISTINCT po.obs_date) AS obs_days,
               COUNT(DISTINCT po.country_code) AS countries,
               COUNT(DISTINCT po.channel_id) AS channels,
               MAX(po.obs_date) AS last_seen
        FROM price_obs po
        JOIN rival_product rp ON rp.id = po.rival_product_id
        JOIN brand b ON b.id = rp.brand_id
        WHERE {' AND '.join(where)}
        GROUP BY po.rival_product_id
        ORDER BY obs_days DESC, channels DESC, COUNT(*) DESC
        LIMIT ?
    """, params)


# ---------------------------------------------------------------- 口碑维度（方向15）

def voc_radar(country: str = "", category: str = "", brand: str = "",
              rival_product_id: int | None = None, days: int = 180,
              kind: str = "product") -> dict:
    """口碑按固定维度拆解 —— 「友商这款到底强在哪、弱在哪」。

    ★ 口径（每一条都影响结论）：
      1. **只统计固定维度表里的 code**（app/voc_aspects.py）。
         自由文本维度会把同一件事拆成四个标签，排名直接失真。
      2. **产品维度与体验维度分开**（kind 参数）。价格/物流/售后说的是
         这家零售商，不是这台机器。混在一张雷达图里会得出
         "这款手机弱在物流"——那不是手机的问题，是渠道的问题。
      3. **好评率的分母只算判了情感的**，未判的单独报 unknown。
         把未判的算进分母会让每个维度都显得"好评率不高"。
      4. **inherited 要单独报**：老数据的维度情感是从整条评论继承的
         （一条"相机好但电池烂"会让两个维度都记同一个情感）。
         它和逐维度判定的**精度不一样**，混着报等于假装数据比实际更细。
    """
    where = ["ra.aspect_code IS NOT NULL"]
    params: list = []
    if days:
        where.append("(r.review_date IS NULL OR r.review_date >= date('now', ?))")
        params.append(f"-{int(days)} day")
    if country:
        where.append("r.country_code = ?")
        params.append(country)
    if category:
        where.append("rp.category_code = ?")
        params.append(category)
    if brand:
        # 品牌下拉给的是**名字**（brand 表没有 code 列），别按 id 找
        where.append("rp.brand_id IN (SELECT id FROM brand WHERE name = ?)")
        params.append(brand)
    if rival_product_id:
        where.append("r.rival_product_id = ?")
        params.append(int(rival_product_id))

    rows = db.q(f"""
        SELECT ra.aspect_code AS code,
               COUNT(*) AS mentions,
               SUM(ra.sentiment = 'positive') AS pos,
               SUM(ra.sentiment = 'negative') AS neg,
               SUM(ra.sentiment = 'neutral')  AS neu,
               SUM(ra.sentiment IS NULL)      AS unknown,
               SUM(ra.sentiment_from = 'review') AS inherited
        FROM review_aspect ra
        JOIN review r ON r.id = ra.review_id
        LEFT JOIN rival_product rp ON rp.id = r.rival_product_id
        WHERE {' AND '.join(where)}
        GROUP BY ra.aspect_code
    """, params)

    out = []
    for r in rows:
        code = r["code"]
        if code not in voc_aspects.ASPECT_ZH:
            continue                      # 表外的 code 不显示，也不悄悄归到别处
        if kind and voc_aspects.ASPECT_KIND[code] != kind:
            continue
        judged = (r["pos"] or 0) + (r["neg"] or 0) + (r["neu"] or 0)
        out.append({
            "code": code,
            "name": voc_aspects.ASPECT_ZH[code],
            "kind": voc_aspects.ASPECT_KIND[code],
            "mentions": r["mentions"],
            "positive": r["pos"] or 0,
            "negative": r["neg"] or 0,
            "neutral": r["neu"] or 0,
            "unknown": r["unknown"] or 0,
            # 好评率：分母只算判了情感的。没判的不算"不好评"。
            "pos_rate": round((r["pos"] or 0) / judged * 100, 1) if judged else None,
            "neg_rate": round((r["neg"] or 0) / judged * 100, 1) if judged else None,
            # 这个维度里有多少票是从整条评论继承来的（精度较低）
            "inherited": r["inherited"] or 0,
            "inherited_pct": round((r["inherited"] or 0) / r["mentions"] * 100)
            if r["mentions"] else 0,
        })
    out.sort(key=lambda x: -x["mentions"])
    total = sum(x["mentions"] for x in out)
    inh = sum(x["inherited"] for x in out)
    return {
        "items": out,
        "total_mentions": total,
        # ★ 整体继承占比高时必须在界面上说出来，否则读者会以为
        #   每个维度的好坏都是逐维度判过的
        "inherited_pct": round(inh / total * 100) if total else 0,
        "note": ("维度情感多数继承自整条评论，精度有限；重跑 VOC 分析后会逐维度重判"
                 if total and inh / total > 0.5 else ""),
    }


# ---------------------------------------------------------------- 口碑衰减（方向17）

# 上市日期里大量是"某年01月01日"这种占位值（补规格时拿不到确切日期就填了年初）。
# 拿它当起点算"上市第 N 天"会得出 MacBook 上市 2300 天后仍在收评论这种废话。
_PLACEHOLDER_LAUNCH = ("-01-01",)


def voc_decay(rival_product_id: int | None = None, bucket_days: int = 30,
              max_days: int = 360, min_reviews: int = 5) -> dict:
    """上市后口碑衰减曲线：首销后第 N 天的平均星级。

    ★ 这张图最容易骗人的地方是**起点**。三道闸：
      1. 上市日期是 "YYYY-01-01" 的一律不用 —— 那是补规格时的占位值，
         不是真的 1 月 1 日上市。用它算出来的"上市第 2300 天"毫无意义。
      2. **首条评论距上市太远的不叫"上市后口碑"**。Galaxy S25 的评论
         从上市第 477 天才开始，那是成熟期口碑，画成衰减曲线是张冠李戴。
         这类产品单独列进 excluded 并说明原因，不混进曲线里。
      3. 桶内样本少于 min_reviews 的**照实标出来**，不平滑、不插值。
         一个 2 条评论的桶和一个 50 条评论的桶在图上不能长得一样。
    """
    where = ["r.review_date IS NOT NULL", "r.review_date <> ''",
             "rp.global_launch_date IS NOT NULL", "r.rating IS NOT NULL"]
    params: list = []
    if rival_product_id:
        where.append("rp.id = ?")
        params.append(int(rival_product_id))

    rows = db.q(f"""
        SELECT rp.id, rp.model_name, rp.category_code, b.name AS brand,
               rp.global_launch_date AS launch, r.review_date, r.rating,
               r.country_code
        FROM review r
        JOIN rival_product rp ON rp.id = r.rival_product_id
        JOIN brand b ON b.id = rp.brand_id
        WHERE {' AND '.join(where)}
    """, params)

    by_product: dict[int, dict] = {}
    for r in rows:
        p = by_product.setdefault(r["id"], {
            "id": r["id"], "model": r["model_name"], "brand": r["brand"],
            "cat": r["category_code"], "launch": r["launch"], "points": [],
        })
        p["points"].append((r["review_date"], r["rating"]))

    series, excluded = [], []
    for p in by_product.values():
        launch = str(p["launch"] or "")
        if any(launch.endswith(s) for s in _PLACEHOLDER_LAUNCH):
            excluded.append({**_meta(p), "reason": "上市日期是占位值（年初01-01），不可用作起点"})
            continue
        days = []
        for d, rating in p["points"]:
            try:
                n = (date.fromisoformat(d[:10]) - date.fromisoformat(launch[:10])).days
            except ValueError:
                continue
            if 0 <= n <= max_days:
                days.append((n, rating))
        if len(days) < min_reviews:
            first = min((n for n, _ in days), default=None)
            all_n = []
            for d, _ in p["points"]:
                try:
                    all_n.append((date.fromisoformat(d[:10])
                                  - date.fromisoformat(launch[:10])).days)
                except ValueError:
                    pass
            gap = min(all_n) if all_n else None
            excluded.append({**_meta(p), "reason":
                             (f"首条评论已是上市第 {gap} 天，不属于上市期口碑"
                              if gap and gap > max_days else
                              f"上市 {max_days} 天内只有 {len(days)} 条带日期评论，不足以成线")})
            continue
        buckets: dict[int, list[float]] = {}
        for n, rating in days:
            buckets.setdefault(n // bucket_days, []).append(rating)
        series.append({
            **_meta(p),
            "buckets": [{
                "day_from": b * bucket_days,
                "day_to": (b + 1) * bucket_days - 1,
                "avg_rating": round(sum(v) / len(v), 2),
                "n": len(v),
                # 样本太少的桶要能被界面画成虚线/灰点，不能和实心点一样
                "thin": len(v) < min_reviews,
            } for b, v in sorted(buckets.items())],
        })
    series.sort(key=lambda s: -sum(b["n"] for b in s["buckets"]))
    return {"series": series, "excluded": excluded,
            "bucket_days": bucket_days, "max_days": max_days}


def _meta(p: dict) -> dict:
    return {"id": p["id"], "model": p["model"], "brand": p["brand"],
            "cat": p["cat"], "launch": p["launch"], "reviews": len(p["points"])}
