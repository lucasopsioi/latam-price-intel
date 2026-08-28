# -*- coding: utf-8 -*-
"""价格时间序列：产品 / 品类 / 品牌三级曲线，多对象对比，关注清单与预警。

用户要的（原话）：
  「我从现在开始抓，那未来的一段时间里它的价格变化是什么样的？每一天的价格变化
    是什么样的？都要有追踪，并且要形成价格曲线。产品的、品类的、品牌的都要有。
    还可以多个产品互相对比，两个品牌互相对比，品类和品类之间互相对比。」

★★ 画一条价格曲线很容易，画一条**不骗人**的价格曲线有四道坎，
   本文件的绝大部分代码是在过这四道坎：

  1. **同一天有很多个价格**（不同渠道、不同配置、不同卖家）。
     取哪个？取均值会被一次闪促拽走。这里的口径是
     「先在渠道内取当天最低价（货架上真正起作用的那个价），
       再对渠道取中位数」—— 一家店促销不会带偏整条曲线。

  2. **构成效应**：品类/品牌曲线如果每天对"当天在售的商品"取中位数，
     那么货盘一换，曲线就动了，而没有任何一件商品调过价。
     实测教训（促销收缩那次）：哥伦比亚跑出全品牌两天内减促 43pp，
     真相是那两天多抓了一家低折扣率的店。
     ⇒ 品类/品牌曲线**必须用固定篮子**：只统计区间内始终可见的那批商品。

  3. **跨币种不可同轴**：六国六币种差三个数量级，绝对价放一张图纵轴会塌。
     ⇒ 跨国/跨币种对比一律**指数化**（基期=100），只比变化不比绝对值。

  4. **缺口不是零**：某天没跑采集 ≠ 那天价格为零，也 ≠ 价格没变。
     ⇒ 缺口返回 None，前端不连线（charts.js 已 connectNulls:false）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import db

log = logging.getLogger("trends")

CAT_ZH = {"phone": "手机", "tablet": "平板", "audio": "音频",
          "wearable": "穿戴", "pc": "电脑"}

# 优先级 → 默认预警阈值（百分点）。
# ★ P0 更敏感是因为它是"重点盯防"：宁可多提醒，也不能漏掉对手的关键动作。
#   P2 只在大动作时出声，避免把通知栏淹掉。
PRIORITY_THRESHOLD = {
    "P0": {"drop": 3.0, "rise": 3.0},
    "P1": {"drop": 5.0, "rise": 5.0},
    "P2": {"drop": 10.0, "rise": 10.0},
}
PRIORITY_ZH = {"P0": "重点", "P1": "常规", "P2": "观察"}

# 单个关注对象每轮扫描的预警上限。见 scan_alerts 里的说明。
MAX_PER_WATCH = 5


def _median(vals: list[float]) -> float | None:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _carry_forward(pts: list) -> tuple[list, list]:
    """缺口日按最近一次观测延续（LOCF）。返回 (值, 是否延续标记)。

    ★ 为什么可以延续：挂牌价在没有新观测时**视为未变**是成立的 ——
      店家不改价，价格就是上次那个。这与"把没采到画成零/断线"不同：
      断线的视觉含义是"没有价格"，而商品明明还挂着。
    ★ 为什么必须带标记：延续值是推定不是观测。前端画成空心小点、
      悬浮标「延续」，读者能分清哪天真采到了、哪天是沿用 ——
      连续但不撒谎。
    ★ 只向前延续：首个观测之前保持 None（那时我们根本没见过这个商品）。
    """
    out, filled, last = [], [], None
    for v in pts:
        if v is not None:
            last = v
            out.append(v); filled.append(False)
        elif last is not None:
            out.append(last); filled.append(True)
        else:
            out.append(None); filled.append(False)
    return out, filled


def _days(days: int) -> list[str]:
    """从数据的最新一天往回列日期。

    ★ 用数据的最新日期而不是系统今天：库里最新是 08-14 而系统时间可能更晚，
      按系统时间取窗口会让曲线右端凭空多出一段空白，看起来像"最近没抓到"。
    """
    r = db.q1("SELECT MAX(obs_date) d FROM price_obs")
    end = date.fromisoformat((r or {}).get("d") or date.today().isoformat())
    return [(end - timedelta(days=i)).isoformat() for i in range(int(days) - 1, -1, -1)]


# ================================================================ 产品曲线

def product_series(rival_product_id: int, days: int = 90, country: str = "",
                   by_channel: bool = True, trim: bool = True) -> dict:
    """单个产品的价格曲线。

    ★ by_channel=True 时按 (国家,渠道) 分线 —— 这是**默认**，
      因为同一个产品在不同店的价格是不同的事实，合成一条会掩盖渠道差异。
    ★ by_channel=False 时给一条合成线：渠道当天最低价的**中位数**。
    """
    xs = _days(days)
    since = xs[0]
    where = ["po.rival_product_id = ?", "po.obs_date >= ?", "po.sale_price IS NOT NULL",
             "po.audit_status <> 'rejected'", "po.product_kind = 'device'",
             "po.condition = 'new'", "po.is_bundle = 0"]
    params: list = [int(rival_product_id), since]
    if country:
        where.append("po.country_code = ?")
        params.append(country.upper())

    rows = db.q(f"""
        SELECT po.obs_date d, po.country_code cc, po.channel_id ch, po.currency,
               c.name AS channel_name,
               MIN(po.sale_price) p          -- ★ 渠道当天最低价 = 货架上起作用的价
        FROM price_obs po JOIN channel c ON c.id = po.channel_id
        WHERE {' AND '.join(where)}
        GROUP BY po.obs_date, po.country_code, po.channel_id, po.currency
        ORDER BY po.obs_date
    """, params)

    prod = db.q1("""SELECT rp.model_name, rp.category_code, b.name AS brand
                    FROM rival_product rp LEFT JOIN brand b ON b.id = rp.brand_id
                    WHERE rp.id = ?""", (int(rival_product_id),))

    if by_channel:
        buckets: dict[tuple, dict] = {}
        for r in rows:
            k = (r["cc"], r["channel_name"], r["currency"])
            buckets.setdefault(k, {})[r["d"]] = r["p"]
        series = []
        for (cc2, ch, cur), pts in sorted(buckets.items()):
            raw = [pts.get(d) for d in xs]
            vals, filled = _carry_forward(raw)
            series.append({
                "name": f"{cc2} · {ch}",
                "country": cc2, "currency": cur,
                "pts": vals, "filled": filled,
                "n_points": sum(1 for v in raw if v is not None),
            })
    else:
        per_day: dict[str, list] = {}
        cur = rows[0]["currency"] if rows else ""
        for r in rows:
            per_day.setdefault(r["d"], []).append(r["p"])
        raw = [_median(per_day.get(d, [])) for d in xs]
        vals, filled = _carry_forward(raw)
        series = [{
            "name": (prod or {}).get("model_name") or "产品",
            "currency": cur,
            "pts": vals, "filled": filled,
            "n_points": sum(1 for v in raw if v is not None),
        }]

    currencies = sorted({s.get("currency") for s in series if s.get("currency")})
    out = {
        "xs": xs, "series": series,
        "product": dict(prod) if prod else None,
        "currencies": currencies,
        # ★ 多币种时前端必须切指数化，否则纵轴会塌
        "mixed_currency": len(currencies) > 1,
        "note": ("每条线 = 该渠道当天最低价；实心点 = 当天真实观测，"
                 "空心小点 = 没采到、按最近一次观测延续（挂牌价未变的推定）"
                 if by_channel else
                 "合成线 = 各渠道当天最低价的中位数；空心小点为延续值"),
    }
    return _trim(out) if trim else out


# ================================================================ 品类 / 品牌曲线

def _basket_series(kind: str, key: str, country: str, days: int,
                   min_days_ratio: float = 0.6,
                   min_coverage: float = 0.5) -> dict:
    """品类或品牌的价格曲线 —— 固定篮子 + **链式配对指数**。

    ★★ 这是本文件最重要的函数，因为它最容易画出骗人的图。踩了两层：

      第一层（朴素做法）：每天对"当天在售商品"取中位数。
        货盘一换曲线就动，而没有任何一件商品调过价。

      第二层（只固定篮子还不够）：篮子固定了，但**每天实际出现的成员不同**。
        实测：Samsung 智利 08-10 篮子里只出现 1/34 件，那天的"中位数"
        就是那一件的价格（1,099,990），次日出现 15 件后中位数变成 249,990
        —— 曲线上是**一天跌 77%**，实际一件商品都没降价。

    ⇒ 两个措施一起上：
      1. **链式配对指数**（价格指数的标准做法，对构成免疫）：
         相邻两天之间，只取**两天都出现**的商品，算它们各自的价格比，
         取中位数当作这一段的环比，然后逐段连乘。
         任何一天进出篮子都不会产生跳变，因为它不参与那一段的配对。
      2. **覆盖率闸门**：某天篮子成员出现不足 min_coverage 就不出绝对价位点
         （返回 None，前端不连线）—— 那天的中位数不具代表性。

    返回两条口径：
      pts   绝对价位（本币，低覆盖天留空）—— 回答"现在什么价位"
      index 链式指数（基期=100）        —— 回答"涨跌了多少"，跨国可比
    """
    xs = _days(days)
    since = xs[0]
    where = ["po.obs_date >= ?", "po.sale_price IS NOT NULL",
             "po.audit_status <> 'rejected'", "po.product_kind = 'device'",
             "po.condition = 'new'", "po.is_bundle = 0",
             "po.url IS NOT NULL", "po.url <> ''"]
    params: list = [since]
    if kind == "category":
        where.append("po.category_code = ?")
        params.append(key)
    elif kind == "brand":
        where.append("po.brand_id IN (SELECT id FROM brand WHERE name = ?)")
        params.append(key)
    if country:
        where.append("po.country_code = ?")
        params.append(country.upper())

    rows = db.q(f"""
        SELECT po.url, po.obs_date d, po.currency, MIN(po.sale_price) p,
               MIN(po.title) title
        FROM price_obs po
        WHERE {' AND '.join(where)}
        GROUP BY po.url, po.obs_date
    """, params)
    return _compose(rows, xs, kind, key, country, min_days_ratio, min_coverage)


def _compose(rows: list, xs: list[str], kind: str, key: str, country: str,
             min_days_ratio: float = 0.6, min_coverage: float = 0.5) -> dict:
    """把观测行装成曲线。**与取数分开**，因为骗人的图都出在这段算术里，
    而不是出在 SQL 里 —— 拆开之后测试可以直接喂构造好的行去钉住性质
    （货盘进出免疫、断点重连、跨币种置空），不用铺一整个库。"""
    seen: dict[str, dict] = {}
    for r in rows:
        e = seen.setdefault(r["url"], {"pts": {}, "cur": r["currency"], "title": None})
        e["pts"][r["d"]] = r["p"]
        if not e["title"]:
            e["title"] = r.get("title") if isinstance(r, dict) else None

    active_days = sorted({r["d"] for r in rows})
    need = max(2, int(len(active_days) * min_days_ratio))
    basket = {u: v for u, v in seen.items() if len(v["pts"]) >= need}
    dropped = len(seen) - len(basket)
    nb = len(basket) or 1

    # ---- 绝对价位：覆盖率不足的天留空 ----
    level, coverage = [], []
    for d in xs:
        have = [v["pts"][d] for v in basket.values() if d in v["pts"]]
        cov = len(have) / nb
        coverage.append(round(cov, 3))
        level.append(_median(have) if cov >= min_coverage else None)

    # ---- 链式配对指数：只用相邻两天都出现的商品 ----
    # 配对数下限：少于这个数的环比只是噪声
    MIN_PAIRS = 3
    idx, pairs, breaks = [], [], []
    prev_day = None
    cur_idx = None
    for i, d in enumerate(xs):
        if prev_day is None:
            # 第一个有数据的日子作为基期
            if any(d in v["pts"] for v in basket.values()):
                cur_idx = 100.0
                prev_day = d
                idx.append(100.0)
                pairs.append(None)
            else:
                idx.append(None)
                pairs.append(None)
            continue
        ratios = [v["pts"][d] / v["pts"][prev_day]
                  for v in basket.values()
                  if d in v["pts"] and prev_day in v["pts"] and v["pts"][prev_day]]
        if len(ratios) < MIN_PAIRS:
            # 配对样本太少，这一段连不上：不出指数点。
            # ★★ 但**基准日必须前移**（只要这一天本身有像样的覆盖）。
            #   第一版写成"基准日不前移"，理由是"怕拿不可比的起点算" ——
            #   那个理由是错的，后果正相反：实测 Samsung·CL 的 08-10 只有
            #   1/34 件商品，链条卡在那一天，之后每天都拿这 1 件去配对，
            #   配对数永远是 1、指数永远断，整条曲线只剩一个孤零零的 100。
            #   正确做法是把基准滚到这一天，下一段从这里重新连 ——
            #   断掉的是**那一段**，不是整条链。
            idx.append(None)
            pairs.append(len(ratios))
            day_cov = sum(1 for v in basket.values() if d in v["pts"]) / nb
            if day_cov >= min_coverage:
                prev_day = d
                breaks.append(d)
            continue
        link = _median(ratios) or 1.0
        cur_idx = (cur_idx or 100.0) * link
        idx.append(round(cur_idx, 2))
        pairs.append(len(ratios))
        prev_day = d

    currencies = sorted({v["cur"] for v in basket.values() if v["cur"]})
    mixed = len(currencies) > 1

    # ★★ 跨币种时绝对价位**不出数**，而不是出了再让前端别画。
    #   实测「全部国家·手机」：篮子横跨 BRL/CLP/COP/MXN/PEN，绝对中位数
    #   16999 → 10148 → 9999，看着像三天崩了 41%，其实一分钱没降 ——
    #   中位数那一件今天落在 PEN 商品上、明天落在 MXN 商品上而已，
    #   换算关系差一个数量级，比的根本不是价格是币种。同期指数是平的 100.0。
    #   这类脏数在源头掐掉，别指望每个调用方都记得判 mixed_currency；
    #   比率是无量纲的，所以指数跨币种依然成立，降级到指数不丢信息。
    if mixed:
        level = [None] * len(xs)
    label = CAT_ZH.get(key, key) if kind == "category" else key
    return {
        "xs": xs,
        "series": [{
            "name": f"{label}{(' · ' + country.upper()) if country else ''}",
            "currency": currencies[0] if len(currencies) == 1 else "",
            "pts": level,
            "index": idx,
            "coverage": coverage,
            "pairs": pairs,
            "n_points": sum(1 for v in level if v is not None),
        }],
        # ★ 篮子成分要能看见。用户实测反馈"没看懂这页在干什么，啥产品都没有" ——
        #   聚合曲线画的是一篮子商品的整体走势，但**篮子是不可见的**，
        #   看的人无从判断这条线代表什么。列出来就不抽象了。
        "basket_items": [
            {"title": (v.get("title") or v_url)[:90],
             "url": v_url,
             "days": len(v["pts"]),
             "last": v["pts"][max(v["pts"])] if v["pts"] else None,
             "cur": v["cur"]}
            for v_url, v in sorted(basket.items(),
                                   key=lambda kv: (-len(kv[1]["pts"]),
                                                   kv[1].get("title") or ""))[:40]
        ],
        "basket": len(basket), "dropped": dropped,
        "min_days": need, "active_days": len(active_days),
        "min_coverage": min_coverage, "breaks": breaks,
        "currencies": currencies, "mixed_currency": mixed,
        "note": (f"固定篮子 {len(basket)} 件（排除 {dropped} 件出现天数不足的）。"
                 f"指数用**逐日配对**算：只比两天都在架的商品，"
                 f"所以商品进出篮子不会造成跳变。"
                 + (f"　⚠ 篮子横跨 {'/'.join(currencies)} {len(currencies)} 个币种，"
                    f"绝对价位已停用（跨币种的中位数比的是币种不是价格），"
                    f"只出指数。" if mixed else
                    f"绝对价位在篮子覆盖率低于 {int(min_coverage * 100)}% 的日子留空 —— "
                    f"那天的中位数只代表一小撮商品，不具代表性。")
                 + (f"　指数在 {'、'.join(breaks)} 处断开（可配对商品不足 "
                    f"{MIN_PAIRS} 件），断点两侧不可直接比。" if breaks else "")),
        "insufficient": len(basket) < 5,
    }


def _trim(res: dict, pad: int = 1) -> dict:
    """把 x 轴裁到**真正有数据的区间**。

    ★ 为什么必须裁：我们才采了 5 天，用户选"近 3 个月"就会得到一根 90 格的轴，
      数据全挤在最右边 5% 里，剩下 95% 是空白 —— 实测用户的原话是
      "没看懂你这页在干什么"。图本身没错，但**没人看得出它没错**。
      裁完之后 5 天的数据就占满整个轴，一眼能看。
    ★ 只裁前面不裁后面：右端留到今天，"最近几天没采到"本身是要看见的信息。
    """
    xs = res.get("xs") or []
    series = res.get("series") or []
    if not xs or not series:
        return res

    first = None
    for i in range(len(xs)):
        for s in series:
            for key in ("pts", "index"):
                arr = s.get(key) or []
                if i < len(arr) and arr[i] is not None:
                    first = i
                    break
            if first is not None:
                break
        if first is not None:
            break

    if first is None or first == 0:
        res["trimmed"] = 0
        return res

    cut = max(0, first - pad)
    res["xs"] = xs[cut:]
    for s in series:
        for key in ("pts", "index", "coverage", "pairs"):
            if isinstance(s.get(key), list):
                s[key] = s[key][cut:]
    res["trimmed"] = cut
    res["requested_days"] = len(xs)
    res["note"] = (res.get("note", "")
                   + f"　x 轴已裁到有数据的区间（请求 {len(xs)} 天，"
                     f"实际有数据 {len(res['xs'])} 天）—— "
                     f"不裁的话数据会全挤在最右边一小条里。")
    return res


def category_series(category: str, country: str = "", days: int = 90,
                    trim: bool = True) -> dict:
    r = _basket_series("category", category, country, days)
    return _trim(r) if trim else r


def brand_series(brand: str, country: str = "", days: int = 90,
                 trim: bool = True) -> dict:
    r = _basket_series("brand", brand, country, days)
    return _trim(r) if trim else r


# ================================================================ 多对象对比

def compare(entities: list[dict], days: int = 90, index_base: bool = None) -> dict:
    """多个对象放一张图对比。

    entities: [{"kind":"product|category|brand", "key":..., "country":...}, ...]

    ★★ 跨币种时**必须指数化**（基期=100）。
      六国六币种差三个数量级，CLP 的线会把 MXN 的线压成一条贴底的直线。
      指数化之后比的是"各自涨跌了百分之多少"，这才是可比的问题。
    ★ index_base=None 时自动判断：币种不止一个就指数化，并在 note 里说明。
    """
    out, currencies, notes = [], set(), []
    mixed_any = False                      # 有没有哪条线**自己内部**就跨币种
    xs: list[str] = []
    for e in entities[:8]:                 # 超过 8 条线就分不清了
        kind, key = e.get("kind"), e.get("key")
        cc = e.get("country") or ""
        if kind == "product":
            r = product_series(int(key), days=days, country=cc, by_channel=False,
                               trim=False)
        elif kind == "category":
            r = category_series(str(key), country=cc, days=days, trim=False)
        elif kind == "brand":
            r = brand_series(str(key), country=cc, days=days, trim=False)
        else:
            continue
        xs = r["xs"]
        mixed_any = mixed_any or bool(r.get("mixed_currency"))
        for s in r["series"]:
            if s.get("currency"):
                currencies.add(s["currency"])
            out.append({**s, "kind": kind, "key": key, "country": cc})
        if r.get("note"):
            notes.append(f"{kind}:{key} —— {r['note']}")

    # ★ 判定要算上「单条线自己内部就跨币种」的情况。
    #   跨币种的对象 currency 字段是空串，压根进不了 currencies 集合，
    #   只看这个集合会把「全部国家的 Samsung vs 全部国家的 Xiaomi」判成同币种，
    #   走绝对价位分支 —— 而那两条线的绝对价位已在后端置空，结果两条线全空。
    do_index = ((len(currencies) > 1 or mixed_any)
                if index_base is None else bool(index_base))
    if do_index:
        for s in out:
            # ★ 优先用**链式配对指数**再归一，而不是拿绝对价位重新求基。
            #   绝对价位对货盘构成敏感（换一批货就跳），链式指数不敏感；
            #   何况跨币种时绝对价位本来就是空的，拿它求基只会得到空线。
            src = s.get("index") or s["pts"]
            base = next((v for v in src if v), None)
            s["pts"] = ([round(v / base * 100, 2) if v else None for v in src]
                        if base else [None] * len(src))
        unit = "指数（各自基期=100）"
    else:
        unit = f"本币（{'/'.join(sorted(currencies))}）" if currencies else "本币"

    return {
        "xs": xs, "series": out, "indexed": do_index, "unit": unit,
        "currencies": sorted(currencies),
        "mixed_currency": mixed_any,
        "note": (("涉及多个币种，已指数化：每条线以自己第一个有数据的日子为 100，"
                  "只比变化幅度、不比绝对价位。曲线用逐日配对的链式指数，"
                  "货盘进出不会造成跳变。") if do_index else
                 "同一币种，直接比绝对价。") + ("　" + "；".join(notes) if notes else ""),
    }


# ================================================================ 关注清单

def watchlist(enabled_only: bool = True) -> list[dict]:
    where = "WHERE w.enabled = 1" if enabled_only else ""
    rows = db.q(f"""
        SELECT w.*, rp.model_name, b.name AS brand_name, co.name_zh AS country_name,
               (SELECT COUNT(*) FROM price_alert a
                 WHERE a.watch_id = w.id AND a.is_read = 0) AS unread
        FROM watchlist w
        LEFT JOIN rival_product rp ON rp.id = w.rival_product_id
        LEFT JOIN brand b ON b.id = w.brand_id
        LEFT JOIN country co ON co.code = w.country_code
        {where}
        ORDER BY CASE w.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,
                 w.created_at DESC
    """)
    for r in rows:
        r["label"] = _watch_label(r)
        th = PRIORITY_THRESHOLD.get(r["priority"], PRIORITY_THRESHOLD["P1"])
        r["eff_drop"] = r["drop_pct"] if r["drop_pct"] is not None else th["drop"]
        r["eff_rise"] = r["rise_pct"] if r["rise_pct"] is not None else th["rise"]
        r["priority_zh"] = PRIORITY_ZH.get(r["priority"], r["priority"])
    return rows


def _watch_label(r: dict) -> str:
    scope = r["scope"]
    cc = f" · {r['country_code']}" if r.get("country_code") else " · 全部国家"
    if scope == "product":
        return f"{r.get('brand_name') or ''} {r.get('model_name') or '?'}".strip() + cc
    if scope == "brand":
        return f"{r.get('brand_name') or '?'}（品牌）" + cc
    return f"{CAT_ZH.get(r.get('category_code'), r.get('category_code'))}（品类）" + cc


def add_watch(scope: str, key, country: str = "", priority: str = "P1",
              drop_pct: float | None = None, rise_pct: float | None = None,
              note: str = "") -> dict:
    """加入关注清单。重复加入视为更新优先级/阈值，不报错。"""
    if scope not in ("product", "brand", "category"):
        raise ValueError(f"scope 只能是 product/brand/category，收到 {scope!r}")
    if priority not in PRIORITY_THRESHOLD:
        priority = "P1"
    pid = bid = None
    cat = None
    if scope == "product":
        pid = int(key)
    elif scope == "brand":
        row = db.q1("SELECT id FROM brand WHERE name = ?", (str(key),))
        if not row:
            raise ValueError(f"没有这个品牌：{key}")
        bid = row["id"]
    else:
        cat = str(key)
    with db.tx() as c:
        c.execute("""
            INSERT INTO watchlist(scope,rival_product_id,brand_id,category_code,
                                  country_code,priority,drop_pct,rise_pct,note)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO UPDATE SET priority=excluded.priority,
                drop_pct=excluded.drop_pct, rise_pct=excluded.rise_pct,
                note=excluded.note, enabled=1
        """, (scope, pid, bid, cat, country.upper() or None, priority,
              drop_pct, rise_pct, note))
    return {"ok": True}


def remove_watch(watch_id: int) -> dict:
    with db.tx() as c:
        c.execute("UPDATE watchlist SET enabled=0 WHERE id=?", (int(watch_id),))
    return {"ok": True}


def suggest_watch(limit: int = 40) -> list[dict]:
    """给用户挑选用的候选清单 —— 按"值得盯"的程度排序。

    ★ 排序依据：观测天数（能画出曲线）优先，其次覆盖渠道数与近期变动次数。
      不按价格高低排 —— 贵的不等于重要。
    """
    rows = db.q("""
        SELECT rp.id, rp.model_name, rp.category_code, b.name AS brand,
               COUNT(DISTINCT po.obs_date) AS obs_days,
               COUNT(DISTINCT po.country_code) AS countries,
               COUNT(DISTINCT po.channel_id) AS channels,
               (SELECT COUNT(*) FROM price_move pm
                 WHERE pm.rival_product_id = rp.id) AS moves,
               (SELECT COUNT(*) FROM watchlist w
                 WHERE w.rival_product_id = rp.id AND w.enabled = 1) AS watched
        FROM rival_product rp
        JOIN brand b ON b.id = rp.brand_id
        JOIN price_obs po ON po.rival_product_id = rp.id
        WHERE b.is_ours = 0 AND po.audit_status <> 'rejected'
          AND po.product_kind = 'device'
        GROUP BY rp.id
        HAVING obs_days >= 2
        ORDER BY obs_days DESC, moves DESC, channels DESC
        LIMIT ?
    """, (int(limit),))
    for r in rows:
        r["cat_zh"] = CAT_ZH.get(r["category_code"], r["category_code"])
    return rows


# ================================================================ 预警

def scan_alerts(days: int = 3) -> dict:
    """扫描关注清单，把够格的价格变动写成预警。

    ★ 三道闸，缺一条通知就会变成噪声：
      1. **只看关注清单里的对象** —— 全量报警等于不报警
      2. **幅度过阈值**（按优先级，可逐条覆盖）
      3. **可信度过关** —— 复用涨价看板那套分档，
         「几乎必错」的直接不报（那批多半是分期月供/变体串档）
    ★ 同对象同日同方向只报一次（唯一索引兜底），否则每轮采集重报一遍。
    """
    from . import boards

    since = (date.fromisoformat(_days(1)[0]) - timedelta(days=int(days))).isoformat()
    watches = watchlist(enabled_only=True)
    if not watches:
        return {"fired": 0, "skipped": 0, "watches": 0,
                "note": "关注清单是空的 —— 预警只针对清单里的对象，先去挑几个。"}

    fired, skipped, details, capped = 0, 0, [], 0
    for w in watches:
        moves = _moves_for_watch(w, since)
        # ★★ 每个关注对象每次扫描最多报 MAX_PER_WATCH 条。
        #   不封顶的后果：一个"品牌"级关注会把旗下**每个产品的每次变动**
        #   都报一遍 —— 实测 5 个关注对象炸出 53 条预警。
        #   通知这件事，**报得越多越没人看**，等于把真正重要的那条淹掉。
        #   moves 已按 |幅度| 降序，所以截断留下的是最值得看的。
        #   被截断了多少要如实报出来，不能让人以为"就这几条"。
        room = MAX_PER_WATCH
        for m in moves:
            pct = m["change_pct"] or 0
            direction = "up" if pct > 0 else "down"
            need = w["eff_rise"] if direction == "up" else w["eff_drop"]
            if abs(pct) < need:
                skipped += 1
                continue
            tier = boards.tier_of(pct, m["category_code"])
            if tier == "implausible":
                skipped += 1
                continue
            if room <= 0:
                capped += 1
                continue
            reason = (f"{PRIORITY_ZH.get(w['priority'], '')}关注对象，"
                      f"{'涨' if direction == 'up' else '降'}{abs(pct):.1f}%"
                      f"（阈值 {need}%）；可信度：{boards.TIER_ZH[tier]}"
                      f" —— {boards.TIER_NOTE[tier]}")
            try:
                with db.tx() as c:
                    c.execute("""
                        INSERT INTO price_alert(watch_id,alert_date,scope,label,
                            country_code,direction,change_pct,prev_price,curr_price,
                            currency,channel_id,rival_product_id,priority,tier,reason)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (w["id"], m["move_date"], w["scope"], _alert_label(w, m),
                          m["country_code"], direction, pct, m["prev_price"],
                          m["curr_price"], m["currency"], m["channel_id"],
                          m["rival_product_id"], w["priority"], tier, reason))
                fired += 1
                room -= 1
                details.append({"label": _alert_label(w, m), "pct": pct, "tier": tier,
                                "priority": w["priority"]})
            except Exception:  # noqa: BLE001
                skipped += 1          # 唯一索引冲突 = 这条已经报过了
    return {"fired": fired, "skipped": skipped, "capped": capped,
            "watches": len(watches), "details": details[:20],
            "note": (f"扫描 {len(watches)} 个关注对象，新增预警 {fired} 条"
                     f"（{skipped} 条因幅度不足/已报过/可信度不过关而跳过"
                     + (f"；另有 {capped} 条因单个对象每轮上限 {MAX_PER_WATCH} 条被折叠，"
                        f"保留的是幅度最大的那些" if capped else "") + "）")}


def _alert_label(w: dict, m: dict) -> str:
    """预警标题要能**独立看懂**：是哪个产品、哪个国家、哪个渠道。

    ★ 只写关注对象的名字不够 —— 品牌级关注下的预警全都叫「Samsung（品牌）」，
      收到通知也不知道是哪台机器动了价，还得回系统里翻。
    """
    who = m.get("model") or m.get("sku_key") or ""
    if not who and w["scope"] == "product":
        who = w.get("model_name") or ""
    parts = [p for p in (who, m.get("country_code"), m.get("channel_name")) if p]
    base = " · ".join(parts) or w["label"]
    if w["scope"] != "product":
        base += f"（{w['label'].split('（')[0]}）"
    return base[:120]


def _moves_for_watch(w: dict, since: str) -> list[dict]:
    where = ["pm.move_date >= ?"]
    params: list = [since]
    if w["scope"] == "product":
        where.append("pm.rival_product_id = ?")
        params.append(w["rival_product_id"])
    elif w["scope"] == "brand":
        where.append("pm.brand_id = ?")
        params.append(w["brand_id"])
    else:
        where.append("pm.category_code = ?")
        params.append(w["category_code"])
    if w.get("country_code"):
        where.append("pm.country_code = ?")
        params.append(w["country_code"])
    return db.q(f"""
        SELECT pm.*, rp.model_name AS model, c.name AS channel_name
        FROM price_move pm
        LEFT JOIN rival_product rp ON rp.id = pm.rival_product_id
        LEFT JOIN channel c ON c.id = pm.channel_id
        WHERE {' AND '.join(where)}
        ORDER BY ABS(pm.change_pct) DESC LIMIT 50
    """, params)


def alerts(unread_only: bool = False, limit: int = 100) -> list[dict]:
    where = "WHERE a.is_read = 0" if unread_only else ""
    return db.q(f"""
        SELECT a.*, rp.model_name, c.name AS channel_name
        FROM price_alert a
        LEFT JOIN rival_product rp ON rp.id = a.rival_product_id
        LEFT JOIN channel c ON c.id = a.channel_id
        {where}
        ORDER BY CASE a.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,
                 a.fired_at DESC
        LIMIT ?
    """, (int(limit),))


def mark_read(alert_ids: list[int] | None = None) -> dict:
    with db.tx() as c:
        if alert_ids:
            qs = ",".join("?" * len(alert_ids))
            n = c.execute(f"UPDATE price_alert SET is_read=1 WHERE id IN ({qs})",
                          tuple(alert_ids)).rowcount
        else:
            n = c.execute("UPDATE price_alert SET is_read=1 WHERE is_read=0").rowcount
    return {"marked": n}


def push_pending(limit: int = 10) -> dict:
    """把未推送的高优先级预警推到 Telegram。

    ★ 只推 P0/P1：P2 是"观察"，攒着在界面上看就行。
      通知这件事，**推得越多越没人看**。
    """
    from . import notify

    rows = db.q("""SELECT * FROM price_alert
                   WHERE is_pushed = 0 AND priority IN ('P0','P1')
                   ORDER BY CASE priority WHEN 'P0' THEN 0 ELSE 1 END, fired_at DESC
                   LIMIT ?""", (int(limit),))
    if not rows:
        return {"pushed": 0}
    lines = ["*价格预警*"]
    for r in rows:
        arrow = "📈" if r["direction"] == "up" else "📉"
        lines.append(f"{arrow} [{r['priority']}] {r['label']}　"
                     f"{r['change_pct']:+.1f}%　"
                     f"{r['prev_price']:,.0f}→{r['curr_price']:,.0f} {r['currency'] or ''}")
    ok = notify.send("\n".join(lines))
    if ok:
        with db.tx() as c:
            c.execute(f"UPDATE price_alert SET is_pushed=1 WHERE id IN "
                      f"({','.join('?' * len(rows))})", tuple(r["id"] for r in rows))
    return {"pushed": len(rows) if ok else 0, "ok": bool(ok),
            "message": msg if not ok else ""}
