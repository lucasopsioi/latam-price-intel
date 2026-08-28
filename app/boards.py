# -*- coding: utf-8 -*-
"""三块图形看板的聚合层：价格分布 / 涨价 / 口碑维度。

★ 为什么和 dashboard.py 分开：
  dashboard.py 是「竞争动态矩阵」那一套（时间×国家×品类的表格聚合），
  已经 500 多行。这里是**给图表用的**聚合 —— 输出结构直接对应图元
  （区间条要 p25/med/p75、热力图要 cells + center、哑铃图要 from/to），
  两者的消费者不同，混在一个文件里以后谁都不敢改。

★ 三条与 dashboard.py 一致的纪律，这里同样成立：
  1. 跨国绝不比绝对价格 —— 价格带图**锁单一国家、用本币**
  2. 官方渠道与第三方分开算
  3. 分位数而不是均值（促销尖峰会把均值拽走）
"""
from __future__ import annotations

from datetime import date, timedelta

from . import db, voc_aspects

CAT_ZH = {"phone": "手机", "tablet": "平板", "audio": "音频",
          "wearable": "穿戴", "pc": "电脑"}


def _since(days: int) -> str:
    return (date.today() - timedelta(days=int(days))).isoformat()


def _data_now() -> str:
    """数据里的最新一天。

    ★★ 时间窗一律以**数据的最新日期**为"现在"，不用系统时间。
      实测教训：库里只有 08-10~08-14 五天，而按系统时间取"近 14 天的前半窗"
      落在 07-31~08-07 —— 那段一行数据都没有，于是所有环比**全空**，
      看板一片空白，看起来像功能坏了，其实只是窗口取错了地方。
    """
    r = db.q1("SELECT MAX(obs_date) d FROM price_obs")
    return (r or {}).get("d") or date.today().isoformat()


def _back(days: int) -> str:
    """从数据的最新日期往回数 N 天。"""
    return (date.fromisoformat(_data_now()) - timedelta(days=int(days))).isoformat()


def _available_span() -> int:
    """实际有观测的日期跨度（天）。窗口自适应用它当上限。"""
    r = db.q1("SELECT MIN(obs_date) a, MAX(obs_date) b FROM price_obs")
    if not r or not r["a"] or not r["b"]:
        return 0
    return (date.fromisoformat(r["b"]) - date.fromisoformat(r["a"])).days


def _quantile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = (len(sorted_vals) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


# ================================================================ 涨价看板

# ★★ 涨价必须**按品类分档**，不能全品类拉通排名。
#   实测：27 条"涨价"里 5 条 >50% 全是秘鲁音频配件
#   （Skullcandy Smokin Buds 89→299 PEN = +236%，Lenovo Clip TA140 66→159 PEN）。
#   注意这**不是配件漏网** —— 耳机在音频品类里本来就是整机，
#   `product_kind <> 'accessory'` 那道闸拦不住它，拦了反而是错的。
#   真正的成因是**单价量纲**：几十块的波动在低价品上就是百分之几十，
#   会把真正该看的整机涨价全部挤出榜单。
#   佐证：降价平均只有 7.4%，"涨价"平均 36.7% —— 这个不对称本身就是污染的证据。
_TIER_BANDS = {
    "phone": (8, 20), "tablet": (8, 20), "pc": (8, 20),
    "wearable": (12, 30), "audio": (15, 40),
}
_TIER_DEFAULT = (10, 25)

TIER_ZH = {"credible": "可信", "suspect": "存疑", "implausible": "几乎必错"}
TIER_NOTE = {
    "credible": "幅度在该品类的正常调价区间内",
    "suspect": "幅度偏大，可能是促销结束回弹，建议人工核实",
    "implausible": "幅度远超正常调价 —— 多半是闪促回弹 / 变体串档 / 解析错误",
}


def tier_of(pct: float | None, cat: str | None) -> str:
    lo, hi = _TIER_BANDS.get(cat or "", _TIER_DEFAULT)
    a = abs(pct or 0)
    return "credible" if a < lo else ("suspect" if a < hi else "implausible")


def price_moves_tiered(direction: str = "up", days: int = 30, country: str = "",
                       category: str = "", official_only: bool = False,
                       tier: str = "") -> dict:
    """涨价（或降价）清单 + 可信度分档。

    ★ 这个函数的第一职责是**自证清白**，不是排名。
      看板上任何"涨价 TOP"都要先经过这里分档，
      否则一上线就是在用几条配件噪声讲故事。
    """
    where = ["pm.move_date >= ?", "pm.direction = ?"]
    params: list = [_since(days), direction]
    if country:
        where.append("pm.country_code = ?")
        params.append(country.upper())
    if category:
        where.append("pm.category_code = ?")
        params.append(category)
    if official_only:
        where.append("pm.is_official = 1")

    rows = db.q(f"""
        SELECT pm.id, pm.move_date, pm.country_code, pm.category_code,
               pm.change_pct, pm.prev_price, pm.curr_price, pm.currency,
               pm.days_span, pm.is_official, pm.sku_key, pm.rival_product_id,
               c.name AS channel, b.name AS brand,
               COALESCE(rp.model_name, pm.sku_key) AS model,
               -- ★ 拿不到销量，用评论量当代理（项目里既有口径：评论量是销量的
               --   公开代理指标）。取不到的留 NULL，**不要默认成 0** ——
               --   那会让"没抓过评论"和"真的没人买"在图上长得一样。
               (SELECT MAX(rpf.total_reviews) FROM review_profile rpf
                 WHERE rpf.rival_product_id = pm.rival_product_id) AS proxy_volume
        FROM price_move pm
        LEFT JOIN channel c ON c.id = pm.channel_id
        LEFT JOIN brand b ON b.id = pm.brand_id
        LEFT JOIN rival_product rp ON rp.id = pm.rival_product_id
        WHERE {' AND '.join(where)}
        ORDER BY ABS(pm.change_pct) DESC
    """, params)

    items, tiers = [], {"credible": 0, "suspect": 0, "implausible": 0}
    for r in rows:
        t = tier_of(r["change_pct"], r["category_code"])
        tiers[t] += 1
        if tier and t != tier:
            continue
        items.append({**r, "tier": t, "tier_zh": TIER_ZH[t], "tier_note": TIER_NOTE[t],
                      "cat_zh": CAT_ZH.get(r["category_code"], r["category_code"] or "—")})

    # 幅度分档直方图 —— 看板的第一张图，先看噪声有多少
    bins = []
    for lab, lo, hi in [("0-5%", 0, 5), ("5-10%", 5, 10), ("10-15%", 10, 15),
                        ("15-25%", 15, 25), ("25-50%", 25, 50), (">50%", 50, 1e9)]:
        hit = [r for r in rows if lo <= abs(r["change_pct"] or 0) < hi]
        cats = sorted({CAT_ZH.get(r["category_code"], r["category_code"])
                       for r in hit if r["category_code"]})
        bins.append({
            "label": lab, "n": len(hit),
            # 整档全是"几乎必错"才标红 —— 混着可信的就不能一竿子打死
            "hot": bool(hit) and all(
                tier_of(r["change_pct"], r["category_code"]) == "implausible" for r in hit),
            "note": ("集中在 " + "/".join(cats)) if cats and len(cats) <= 3 else "",
        })

    avg = round(sum(abs(r["change_pct"] or 0) for r in rows) / len(rows), 1) if rows else None
    # ★ 对照方向的平均幅度：两者差得越远，污染越重。这是给人看的自检数。
    opp = db.q1("""SELECT ROUND(AVG(ABS(change_pct)),1) a, COUNT(*) n FROM price_move
                   WHERE move_date >= ? AND direction = ?""",
                (_since(days), "down" if direction == "up" else "up")) or {}
    return {
        "items": items, "tiers": tiers, "bins": bins, "direction": direction,
        "total": len(rows), "shown": len(items), "avg_abs_pct": avg,
        "opposite_avg_pct": opp.get("a"), "opposite_n": opp.get("n"),
        "bands": {k: list(v) for k, v in _TIER_BANDS.items()},
    }


def promo_shrink(days: int = 14, country: str = "", min_basket: int = 12) -> dict:
    """促销收缩 —— 涨价的**先行指标**。

    ★ 厂商通常先减促、再提价，促销收缩往往比成交价上涨早 2~4 周出现。
      等看到涨价再反应就晚了 —— 所以这张图比"已经涨了"那张更值钱。

    ★★ 篮子必须下沉到**商品**层（按 URL），不能只到渠道层。踩过两层坑：

      第一层（渠道构成）：哥伦比亚跑出 vivo −43pp、Apple −37.9pp、OPPO −33.8pp，
      全国所有品牌两天内一起大幅减促 —— 不合常理。查下来前窗只抓到
      Falabella（有折扣率 89%）553 条，后窗多出 Alkosto 975 条与 Claro 405 条
      （Claro 的有折扣率只有 31%）。那个 −43pp 绝大部分是"换了抓哪些店"。

      第二层（商品构成）：改成按渠道做固定篮子之后，多数格子的篮子里
      **只剩 1 个渠道** —— 一个渠道的篮子等于没有篮子，
      因为同一个渠道在不同天跑的搜索词不同，货盘本身就换了。
      Motorola MX 仍报 −50pp，两天内不可能。

      所以最终口径：**只统计两个窗口都观测到的同一批商品 URL**。
      URL 是站方给的稳定身份，同一个商品页就是同一件货（项目里既有结论）。

    ★ 宁可不出图也不出错图：篮子小于 min_basket 件商品就返回 insufficient，
      并说清楚缺什么。一个由 3 件商品算出的"品牌减促 50%"比空白危险得多。

    ★ 语义反转：这张图上「在促占比下降」才是要警惕的（前端 upIsBad=false）。
    """
    span = _available_span()
    if span < 4:
        return {"items": [], "insufficient": True, "window_days": 0,
                "note": f"只有 {span + 1} 天连续观测，前后半窗各不足 2 天，环比无意义。"
                        f"促销收缩至少需要 4 天历史。"}
    days = min(int(days), span)
    since, mid, now = _back(days), _back(days // 2 or 1), _data_now()

    where = ["po.obs_date >= ?", "po.audit_status <> 'rejected'",
             "po.product_kind = 'device'", "po.brand_id IS NOT NULL",
             "po.url IS NOT NULL", "po.url <> ''"]
    params: list = [since]
    if country:
        where.append("po.country_code = ?")
        params.append(country.upper())

    # 逐商品取两窗的在促状态；只有两窗都出现过的商品才进篮子
    rows = db.q(f"""
        SELECT b.name AS brand, po.country_code AS cc, po.url,
               COALESCE(SUM(po.obs_date <  ?), 0) AS n_pre,
               COALESCE(SUM(po.obs_date >= ?), 0) AS n_cur,
               COALESCE(SUM(po.obs_date <  ? AND po.discount_pct > 0), 0) AS p_pre,
               COALESCE(SUM(po.obs_date >= ? AND po.discount_pct > 0), 0) AS p_cur
        FROM price_obs po JOIN brand b ON b.id = po.brand_id
        WHERE {' AND '.join(where)}
        GROUP BY b.name, po.country_code, po.url
    """, [mid, mid, mid, mid] + params)

    agg: dict[tuple, dict] = {}
    for r in rows:
        d = agg.setdefault((r["brand"], r["cc"]),
                           {"basket": [], "all_pre": [0, 0], "all_cur": [0, 0]})
        d["all_pre"][0] += r["p_pre"]; d["all_pre"][1] += r["n_pre"]
        d["all_cur"][0] += r["p_cur"]; d["all_cur"][1] += r["n_cur"]
        if r["n_pre"] > 0 and r["n_cur"] > 0:          # 两窗都见过 = 进篮子
            d["basket"].append(r)

    out, thin = [], []
    for (brand, cc), d in agg.items():
        bk = d["basket"]
        if len(bk) < min_basket:
            if bk:
                thin.append(f"{brand}·{cc}（篮子仅 {len(bk)} 件）")
            continue
        pre = sum(1 for c in bk if c["p_pre"] > 0) / len(bk) * 100
        cur = sum(1 for c in bk if c["p_cur"] > 0) / len(bk) * 100
        raw_pre = d["all_pre"][0] / d["all_pre"][1] * 100 if d["all_pre"][1] else 0
        raw_cur = d["all_cur"][0] / d["all_cur"][1] * 100 if d["all_cur"][1] else 0
        dl, draw = cur - pre, raw_cur - raw_pre
        out.append({
            "label": f"{brand} · {cc}", "brand": brand, "cc": cc,
            "from": round(pre, 1), "to": round(cur, 1),
            "delta_pp": round(dl, 1), "raw_delta_pp": round(draw, 1),
            "composition": abs(draw - dl) > 5,
            "basket": len(bk), "size": len(bk),
            "note": (f"同一批 {len(bk)} 件商品两期对比"
                     + (f"；不控篮子会算成 {draw:+.1f}pp，"
                        f"差额来自货盘/渠道构成变化" if abs(draw - dl) > 5 else "")),
        })

    out.sort(key=lambda x: x["delta_pp"])
    if not out:
        return {"items": [], "insufficient": True, "window_days": days,
                "window": {"from": since, "mid": mid, "to": now},
                "thin": thin[:8],
                "note": (f"没有任何「品牌×国家」凑得出 {min_basket} 件"
                         f"两期都观测到的商品。当前只有 {span + 1} 天数据，"
                         f"且渠道按品类轮转，两期货盘几乎不重叠。"
                         f"这张图需要连续采集 2~3 周才有意义 —— "
                         f"现在给数字等于编。")}
    # ★ 控住篮子之后如果**全都没动**，那本身就是结论，要说出来而不是画一排零柱。
    #   实测：当前 5 天数据下所有格子都是 ±0.0pp，而不控篮子会跑出
    #   Bose·MX +26.3pp、Motorola·CL −13.9pp 这种大数 —— 那些全是构成效应。
    #   画一排零柱会让人以为"图坏了"；直说"没有可检出的变动"才是事实。
    # ★ 这张图叫"促销收缩"，有意义的只有**下降**的那些。
    #   按 delta 升序取前 N 会在没人收缩时画出一排 0，看着像图坏了。
    #   所以先问"有没有人在收缩"，没有就直说，并把反方向的最大值报出来
    #   —— "大家都在加大促销"同样是结论，而且和"没数据"完全是两回事。
    shrinkers = [i for i in out if i["delta_pp"] <= -1.0]
    if not shrinkers:
        top = max(out, key=lambda x: x["delta_pp"], default=None)
        return {"items": [], "window_days": days, "insufficient": True,
                "no_movement": True,
                "window": {"from": since, "mid": mid, "to": now}, "thin": thin[:8],
                "note": (f"控住商品篮子后，{len(out)} 个「品牌×国家」里"
                         f"**没有一个在减促**（降幅都不到 1pp）。"
                         + (f"变动最大的是 {top['label']} {top['delta_pp']:+.1f}pp，"
                            f"方向是**加大**促销。" if top else "")
                         + f"窗口 {since}~{now}。"
                           f"注意：不控篮子时会跑出 ±26pp 这种数字，那是货盘构成变化。")}
    out = shrinkers

    biggest = max((abs(i["delta_pp"]) for i in out), default=0)
    if biggest < 1.0:
        n_comp = sum(1 for i in out if i["composition"])
        return {"items": out, "window_days": days, "insufficient": True,
                "no_movement": True,
                "window": {"from": since, "mid": mid, "to": now}, "thin": thin[:8],
                "note": (f"控住商品篮子后，{len(out)} 个「品牌×国家」的在促占比"
                         f"变动全部小于 1pp —— 在 {since}~{now} 这个窗口内"
                         f"**没有可检出的促销收缩**。"
                         f"（其中 {n_comp} 个格子在不控篮子时会跑出较大数字，"
                         f"那是货盘/渠道构成变化，不是商家行为。）"
                         f"这个指标要看出趋势需要 2~3 周连续采集。")}
    return {"items": out, "window_days": days, "insufficient": False,
            "no_movement": False,
            "window": {"from": since, "mid": mid, "to": now}, "thin": thin[:8],
            "note": (f"在促商品占比：{since}~{mid} 对比 {mid}~{now}。"
                     f"只统计两期都观测到的同一批商品，已剔除货盘与渠道构成变化。"
                     f"下降 = 减促，可能在为涨价铺垫")}


# ================================================================ 价格看板

def price_band(country: str = "", category: str = "", days: int = 7,
               min_n: int = 5, limit: int = 12) -> dict:
    """价格带卡位：每个品牌的 P25 / 中位 / P75。

    ★ **锁单一国家、用本币**。六国六币种，CLP 和 MXN 差三个数量级，
      放同一根轴上纵轴会塌。跨国比价是另一个问题（要 USD，目前无汇率数据）。
    ★ 排除配件 / 翻新 / 捆绑：它们和整机不是一个计价口径，
      混进来会把价格带整体拉低**而且看不出来**。
    ★ 用分位数不用均值：一个横跨入门到旗舰的品牌，它的均价落在的那个价位上
      往往一台机器都没有。
    """
    if not country:
        top = db.q1("""SELECT country_code cc FROM price_obs
                       WHERE obs_date >= ? AND sale_price IS NOT NULL
                       GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1""", (_since(days),))
        country = (top or {}).get("cc") or "CL"

    # ★★ 这里用**正面断言** product_kind='device'，不是 <> 'accessory'。
    #   实测：Acme智利"手机"价格带跑出 P25=中位=P75=17,683 CLP（约 18 美元），
    #   查下来那 6 行全是 `GENERICO PANTALLA COMPATIBLE CON ACME` —— **副厂屏幕**，
    #   它的 product_kind 是 'unknown'，被 `<> 'accessory'` 这个**否定式**条件放了进来。
    #   代价是丢掉两千多条 unknown 行；但价格带是用来定位价格段的，
    #   混进一个配件就把整条带拉到地板上，**而且图上完全看不出来**。
    #   宁可少报几个品牌，不可报一个错的价格段。
    where = ["po.obs_date >= ?", "po.country_code = ?", "po.sale_price IS NOT NULL",
             "po.audit_status <> 'rejected'", "po.product_kind = 'device'",
             "po.condition = 'new'", "po.is_bundle = 0", "po.brand_id IS NOT NULL"]
    params: list = [_back(days), country.upper()]
    if category:
        where.append("po.category_code = ?")
        params.append(category)

    rows = db.q(f"""
        SELECT b.name AS brand, b.is_ours, po.sale_price AS p, po.currency
        FROM price_obs po JOIN brand b ON b.id = po.brand_id
        WHERE {' AND '.join(where)} ORDER BY b.name, po.sale_price
    """, params)

    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["brand"], {"v": [], "ours": bool(r["is_ours"]),
                                            "cur": r["currency"]})
        g["v"].append(r["p"])

    # 量纲哨兵：拿全体中位数当尺子，识别"某个品牌的价格段低到不可能"
    all_v = sorted(r["p"] for r in rows)
    overall_med = _quantile(all_v, .5) if all_v else 0

    out, thin, flagged = [], [], []
    for brand, g in grouped.items():
        v = sorted(g["v"])
        rec = {"label": brand, "ours": g["ours"], "n": len(v), "currency": g["cur"],
               "p25": round(_quantile(v, .25)), "med": round(_quantile(v, .5)),
               "p75": round(_quantile(v, .75))}
        # ★ 哨兵而不是静默丢弃：低于大盘中位 15% 的"价格带"几乎必然是
        #   配件或错价混了进来。标出来让人去查，不要假装它不存在。
        if overall_med and rec["med"] < overall_med * 0.15:
            rec["suspect"] = (f"中位价只有大盘的 {rec['med'] / overall_med * 100:.0f}%，"
                              f"疑似混入配件或错价")
            flagged.append(rec)
            continue
        # ★ 我方即使样本不足也要进榜 —— 我方全被门槛滤掉时，
        #   图上只剩一排竞品，那张图不解决任何问题。
        (out if len(v) >= min_n or g["ours"] else thin).append(rec)

    out.sort(key=lambda x: (not x["ours"], -x["med"]))
    kept = out[:limit]
    return {"items": kept, "country": country.upper(), "category": category,
            "currency": kept[0]["currency"] if kept else "",
            "hidden_thin": len(thin), "min_n": min_n, "flagged": flagged,
            "dropped": max(0, len(out) - len(kept))}


def discount_heat(days: int = 7, min_n: int = 10) -> dict:
    """折扣力度：国家 × 品类。

    ★ 中性点钉在**大盘中位折扣**，配发散色阶 —— 折扣率有天然中性点，
      顺序色阶会把中性值画成中等深浅的某个颜色，读者无从知道分界在哪。
    ★ 样本不足的格子返回 None（前端画"·"），**不返回 0**：
      "没折扣"和"没观测到"是两件事，混在一起会让缺口看起来像结论。
    """
    rows = db.q("""
        SELECT country_code cc, category_code cat, COUNT(*) n, AVG(discount_pct) d
        FROM price_obs
        WHERE obs_date >= ? AND audit_status <> 'rejected'
          AND product_kind <> 'accessory' AND discount_pct > 0
          AND country_code IS NOT NULL AND category_code IS NOT NULL
        GROUP BY 1, 2
    """, (_back(days),))
    base = db.q1("""SELECT AVG(discount_pct) d FROM price_obs
                    WHERE obs_date >= ? AND discount_pct > 0
                      AND audit_status <> 'rejected'""", (_back(days),)) or {}

    ccs = [r["code"] for r in db.q(
        "SELECT code FROM country WHERE enabled=1 ORDER BY sort_order")]
    cats = [r["code"] for r in db.q("SELECT code FROM category ORDER BY sort_order")]
    got = {(r["cc"], r["cat"]): r for r in rows}
    cells = []
    for cat in cats:
        for cc in ccs:
            r = got.get((cc, cat))
            cells.append({"x": cc, "y": CAT_ZH.get(cat, cat), "cat": cat,
                          "v": round(r["d"], 1) if r and r["n"] >= min_n else None,
                          "n": (r or {}).get("n", 0)})
    return {"xs": ccs, "ys": [CAT_ZH.get(c, c) for c in cats], "cells": cells,
            "center": round(base.get("d") or 0, 1), "days": days, "min_n": min_n}


def seller_spread(country: str = "", category: str = "", days: int = 7,
                  limit: int = 12) -> dict:
    """自营 vs 第三方价差：同一个产品在同一国的两类卖家报价。

    ★ 线朝左（三方更便宜）= 渠道管控问题；
      朝右（三方更贵）= 通常意味着**官方渠道缺货**。
    """
    where = ["po.obs_date >= ?", "po.sale_price IS NOT NULL", "po.rival_product_id IS NOT NULL",
             "po.audit_status <> 'rejected'", "po.product_kind = 'device'",
             "po.condition = 'new'", "po.is_bundle = 0",
             # 配置未知的不参与：拿"未知配置"去和"8G/256G"比，等于没分组
             "po.ram_gb IS NOT NULL", "po.rom_gb IS NOT NULL"]
    params: list = [_back(days)]
    if country:
        where.append("po.country_code = ?")
        params.append(country.upper())
    if category:
        where.append("po.category_code = ?")
        params.append(category)

    rows = db.q(f"""
        SELECT po.rival_product_id pid, po.country_code cc, po.currency,
               po.ram_gb, po.rom_gb, rp.model_name, b.name AS brand,
               MIN(CASE WHEN po.seller_kind IN ('self_operated','brand_official')
                        THEN po.sale_price END) AS official,
               MIN(CASE WHEN po.seller_kind = 'third_party'
                        THEN po.sale_price END) AS third
        FROM price_obs po
        JOIN rival_product rp ON rp.id = po.rival_product_id
        LEFT JOIN brand b ON b.id = po.brand_id
        WHERE {' AND '.join(where)}
        -- ★★ 必须按**配置**分组，不能只按型号。
        --   实测：Acer Aspire Lite 三方 309,990（8G/128G）vs 自营 479,990（16G/512G），
        --   算出 +162% 的"三方加价" —— 那根本是两台不同的机器。
        --   同名不同配置在 rival_product 里是同一个 id，
        --   不按 ram/rom 分开就是在比苹果和橘子。
        GROUP BY po.rival_product_id, po.country_code, po.currency,
                 po.ram_gb, po.rom_gb
        HAVING official IS NOT NULL AND third IS NOT NULL
    """, params)

    out = []
    for r in rows:
        gap = (r["third"] - r["official"]) / r["official"] * 100 if r["official"] else 0
        spec = f"{r['ram_gb']}+{r['rom_gb']}G" if r["ram_gb"] else ""
        out.append({"label": f"{r['model_name']} {spec} · {r['cc']}".strip(),
                    "from": r["official"],
                    "to": r["third"], "gap_pct": round(gap, 1), "brand": r["brand"],
                    "currency": r["currency"], "cc": r["cc"],
                    "note": "空心=自营 实心=第三方最低"})
    out.sort(key=lambda x: -abs(x["gap_pct"]))
    return {"items": out[:limit], "total": len(out), "currency_mixed":
            len({r["currency"] for r in rows}) > 1}


def own_vs_rivals(country: str = "", category: str = "", days: int = 21,
                  limit: int = 16) -> dict:
    """我方 vs 友商价差（发散条，单位是 %）。

    ★★ 这张图**必须用百分比，不能用绝对价**。
      六国六币种，CLP 和 MXN 差三个数量级 —— 绝对价放一张图上纵轴会塌。
      而"我方比对标机贵/便宜百分之多少"是跨国可比的，
      这也是 dashboard.py 开篇第一条纪律（跨国只能比百分比）。

    ★ 我方价从哪来：matcher._our_price 的取价顺序是
      Acme官方商城 > 零售渠道的自营/官方店 > 手工录入的 my_pricing。
      也就是说**不依赖手工录入** —— 用户 2026-08-11 定的口径
      「商城的价格就是官方定价」。my_pricing 是空的不影响这张图。

    ★ 规格未校验的匹配要**标出来**：spec_confidence=0 表示这条只凭价格带
      和可得性配上的，没有规格佐证。拿它当"对标机"下结论要打折扣，
      所以图上画成空心，不和已校验的混在一起。
    """
    where = ["m.is_excluded = 0", "m.price_gap_pct IS NOT NULL"]
    params: list = []
    if country:
        where.append("m.country_code = ?")
        params.append(country.upper())
    if category:
        where.append("rp.category_code = ?")
        params.append(category)

    rows = db.q(f"""
        SELECT m.country_code cc, m.price_gap_pct gap, m.currency,
               m.my_price_local mine, m.rival_price_local theirs,
               m.total_score, m.is_confirmed, m.reasons,
               mp.marketing_name AS my_name, rp.model_name AS rival_name,
               rp.category_code AS cat, b.name AS rival_brand
        FROM competitor_match m
        JOIN my_product mp ON mp.id = m.my_product_id
        JOIN rival_product rp ON rp.id = m.rival_product_id
        LEFT JOIN brand b ON b.id = rp.brand_id
        WHERE {' AND '.join(where)}
        ORDER BY ABS(m.price_gap_pct) DESC
    """, params)

    items, unverified = [], 0
    for r in rows[:limit]:
        # reasons 里带着规格是否参与比较的说明；没有规格佐证的画成空心
        no_spec = "规格数据缺失" in (r["reasons"] or "")
        if no_spec:
            unverified += 1
        items.append({
            "label": f"{r['my_name']} vs {r['rival_brand'] or ''} {r['rival_name']} · {r['cc']}"[:52],
            # ★ 正号 = 友商比我方贵（我方更便宜）；负号 = 我方更贵
            "v": r["gap"],
            "hollow": no_spec,
            "note": (f"我方 {r['mine']:,.0f} vs 对标 {r['theirs']:,.0f} {r['currency']}"
                     f" · 匹配分 {r['total_score']:.2f}"
                     + ("（★未经规格校验，仅凭价格带匹配）" if no_spec else "")),
        })
    return {"items": items, "total": len(rows), "unverified": unverified,
            "note": ("正数 = 对标机比我方贵（我方价格有优势）；"
                     "负数 = 我方更贵。单位是百分比，所以六国可以放一张图。"
                     "空心 = 该匹配没有规格佐证，只凭价格带配上的。")}


# ================================================================ 口碑看板

def voc_dimension_rank(country: str = "", category: str = "", brand: str = "",
                       days: int = 365, kind: str = "product") -> dict:
    """口碑维度排行：相对该品类基线的好评率偏离。

    ★ **不做雷达图**。维度数会随筛选变化（耳机没有相机、手表没有键盘），
      而雷达图的面积在维度数不同时完全不可比 —— 6 边形和 12 边形的面积
      含义不同，读者却会本能地比面积。发散条没有这个问题。
    ★ **必须看相对基线的偏离，不能看绝对好评率**：
      零售评论天然正面（库里 positive 705 : negative 40），
      绝对值下每个维度都是"90% 好评"，什么也看不出来。
    """
    from . import dashboard
    raw = dashboard.voc_radar(country, category, brand, None, days, kind)
    items = raw.get("items") or []
    # ★ 提及数太少的维度不进排行：3 次提及全是好评就成了"100% 好评率"，
    #   会和一个 300 次提及、95% 好评的维度并列榜首 —— 后者才是真结论。
    MIN_MENTIONS = 8
    judged = [i for i in items
              if i["pos_rate"] is not None and i["mentions"] >= MIN_MENTIONS]
    dropped_thin = [i["name"] for i in items
                    if i["pos_rate"] is not None and i["mentions"] < MIN_MENTIONS]
    if not judged:
        return {"items": [], "baseline": None, "total_mentions": raw.get("total_mentions", 0),
                "inherited_pct": raw.get("inherited_pct", 0), "note": raw.get("note", "")}

    # 基线 = 按提及量加权的整体好评率（不是简单平均：
    # 一个 2 次提及的维度不该和 300 次的等权决定基线）
    tot = sum(i["mentions"] for i in judged)
    base = sum(i["pos_rate"] * i["mentions"] for i in judged) / tot

    out = [{"label": i["name"], "code": i["code"], "v": round(i["pos_rate"] - base, 1),
            "pos_rate": i["pos_rate"], "mentions": i["mentions"],
            "negative": i["negative"], "inherited_pct": i["inherited_pct"],
            "note": f"{i['mentions']} 次提及 · 好评率 {i['pos_rate']}%"}
           for i in judged]
    out.sort(key=lambda x: -x["v"])
    return {"items": out, "baseline": round(base, 1), "kind": kind,
            "min_mentions": MIN_MENTIONS, "dropped_thin": dropped_thin,
            "total_mentions": raw.get("total_mentions", 0),
            "inherited_pct": raw.get("inherited_pct", 0), "note": raw.get("note", "")}


def voc_sentiment_source(country: str = "", category: str = "", days: int = 365,
                         limit: int = 8) -> dict:
    """每个维度里，有多少票是**逐维度判定**的、多少是从整条评论继承的。

    ★ 这是诚实性视图，不是好看度视图。
      一条"相机很棒但电池垃圾"的评论，整条判 negative 会让**相机也记一票差评**。
      而维度图恰恰是用来区分维度好坏的 —— 这么记等于把要看的信号抹平。
      所以每张口碑图旁边都要能看到"这里面有多少是继承的"。
    """
    where = ["ra.aspect_code IS NOT NULL"]
    params: list = []
    if days:
        where.append("(r.review_date IS NULL OR r.review_date >= date('now', ?))")
        params.append(f"-{int(days)} day")
    if country:
        where.append("r.country_code = ?")
        params.append(country.upper())
    if category:
        where.append("rp.category_code = ?")
        params.append(category)

    rows = db.q(f"""
        SELECT ra.aspect_code code,
               SUM(ra.sentiment_from = 'aspect') AS by_aspect,
               SUM(ra.sentiment_from = 'review') AS inherited,
               COUNT(*) n
        FROM review_aspect ra
        JOIN review r ON r.id = ra.review_id
        LEFT JOIN rival_product rp ON rp.id = r.rival_product_id
        WHERE {' AND '.join(where)}
        GROUP BY ra.aspect_code ORDER BY n DESC LIMIT ?
    """, params + [limit])
    return {"items": [{"label": voc_aspects.ASPECT_ZH.get(r["code"], r["code"]),
                       "aspect": r["by_aspect"] or 0, "review": r["inherited"] or 0,
                       "n": r["n"]} for r in rows]}


def voc_coverage() -> dict:
    """评论覆盖漏斗：这个看板上的结论，究竟基于多大的样本。

    ★ 覆盖率本身就是最该先看的一张图。现在 VOC 页顶部是四张数字卡，
      但没有**分母** —— 读者不知道 1576 条是覆盖了全部商品还是 4%。
    """
    pages = db.q1("""SELECT COUNT(DISTINCT url) n FROM price_obs
                     WHERE url IS NOT NULL AND url <> ''""")["n"]
    prof = db.q1("SELECT COUNT(*) n FROM review_profile")["n"]
    revs = db.q1("SELECT COUNT(*) n FROM review")["n"]
    trans = db.q1("SELECT COUNT(*) n FROM review WHERE content_zh IS NOT NULL AND content_zh<>''")["n"]
    tagged = db.q1("SELECT COUNT(DISTINCT review_id) n FROM review_aspect")["n"]
    steps = [("有价格的商品页", pages), ("抓过评论画像", prof), ("真抓到评论", revs),
             ("已翻译分析", trans), ("有维度标注", tagged)]
    return {"items": [{"label": k, "v": v,
                       "pct": round(v / pages * 100, 1) if pages else 0}
                      for k, v in steps], "base": pages}


# ================================================================ 我的位置

# 定价偏离的分档。★ 阈值按**价格带**而不是一刀切：
#   低价机型 5% 只是几十块，旗舰 5% 是好几百 —— 但用户的决策语义是一致的
#   （"我明显贵了"），所以这里按百分比分档，绝对值差异在明细里体现。
POSITION_BANDS = [
    (-1e9, -12.0, "明显偏低", "low_hard"),
    (-12.0, -4.0, "略低", "low_soft"),
    (-4.0, 4.0, "基本持平", "even"),
    (4.0, 12.0, "略高", "high_soft"),
    (12.0, 1e9, "明显偏高", "high_hard"),
]


# 下"明显偏高/偏低"这种判断至少要几个对位机型。
# ★ 实测教训：不设闸门时 6 个"明显偏高"里有 4 个建立在 1~2 个样本上 ——
#   最极端的是折叠屏 Astra X7 只匹到 1 款 iPhone 17 Pro Max（非折叠），
#   拿它当"对位中位数"得出"贵 28%"，而折叠屏本来就该更贵。
#   **一个样本的中位数就是那个样本**，这种数字看着精确，其实什么也没说。
MIN_FIELD = 3


def _band_of(pct: float) -> tuple[str, str]:
    for lo, hi, zh, key in POSITION_BANDS:
        if lo <= pct < hi:
            return zh, key
    return "基本持平", "even"


def my_position(country: str = "", category: str = "") -> dict:
    """一屏看完：我的每款产品在该国相对对位竞品的价格站位。

    ★ 为什么单独做这个视图：原来的「竞品对照」页**必须先选一个产品**才有内容，
      要判断"哪几款定价偏了"只能一款一款点 —— 70 款要点 70 次。
      而 销售团队 的实际问题是"我该关注哪几款"，那是个**组合层面**的问题。

    ★ 符号约定写死在字段名里：`my_vs_field_pct` = (我的价 − 对位中位价) / 对位中位价。
      **正数 = 我更贵**。价差的正负方向极易在传递中被弄反，而弄反之后
      图还是照画、数还是照显示，所以这里把方向写进名字，并在返回里带上 `sign_note`。

    ★ 只在同币种内比较（匹配本身已按币种筛过），跨国不合并。
    """
    where = ["cm.is_excluded=0", "mp.status='active'",
             "cm.my_price_local IS NOT NULL", "cm.rival_price_local IS NOT NULL"]
    params: list = []
    if country:
        where.append("cm.country_code=?"); params.append(country.upper())
    if category:
        where.append("mp.category_code=?"); params.append(category)

    rows = db.q(f"""
        SELECT cm.my_product_id, cm.country_code, cm.currency,
               mp.marketing_name AS my_name, mp.category_code,
               cm.my_price_local, cm.rival_price_local,
               rp.model_name AS rival_name, b.name AS rival_brand,
               cm.total_score, cm.is_confirmed
        FROM competitor_match cm
        JOIN my_product mp ON mp.id=cm.my_product_id
        JOIN rival_product rp ON rp.id=cm.rival_product_id
        JOIN brand b ON b.id=rp.brand_id
        WHERE {' AND '.join(where)}
        ORDER BY cm.my_product_id, cm.country_code, cm.total_score DESC
    """, params)

    groups: dict[tuple, dict] = {}
    for r in rows:
        k = (r["my_product_id"], r["country_code"])
        g = groups.setdefault(k, {
            "my_product_id": r["my_product_id"], "country_code": r["country_code"],
            "my_name": r["my_name"], "category_code": r["category_code"],
            "currency": r["currency"], "my_price": r["my_price_local"],
            "rivals": [],
        })
        g["rivals"].append({
            "brand": r["rival_brand"], "model": r["rival_name"],
            "price": r["rival_price_local"], "score": r["total_score"],
            "confirmed": bool(r["is_confirmed"]),
        })

    items = []
    for g in groups.values():
        prices = sorted(x["price"] for x in g["rivals"] if x["price"])
        if not prices or not g["my_price"]:
            continue
        med = _quantile(prices, 0.5)
        if not med:
            continue
        pct = (g["my_price"] - med) / med * 100.0
        if len(prices) < MIN_FIELD:
            # 样本不足：价差照给（可参考），但**不给站位判断**
            zh, key = f"对位仅 {len(prices)} 款，不下判断", "thin"
        else:
            zh, key = _band_of(pct)
        g["rivals"].sort(key=lambda x: -(x["score"] or 0))
        items.append({
            **{k: v for k, v in g.items() if k != "rivals"},
            "field_median": round(med, 2),
            "field_low": prices[0], "field_high": prices[-1],
            "rival_n": len(prices),
            "my_vs_field_pct": round(pct, 1),
            "band": key, "band_zh": zh,
            # 只带前 5 个最像的，明细里能展开
            "top_rivals": g["rivals"][:5],
        })

    # 先按"有没有结论"分层，再按偏离排序 ——
    # 样本不足的排在后面，免得它们凭着大数字霸占屏幕顶部
    items.sort(key=lambda x: (x["band"] == "thin", -abs(x["my_vs_field_pct"])))

    dist: dict[str, int] = {}
    for it in items:
        dist[it["band"]] = dist.get(it["band"], 0) + 1

    return {
        "items": items,
        "total": len(items),
        "distribution": dist,
        "bands": [{"key": k, "zh": zh, "lo": lo, "hi": hi}
                  for lo, hi, zh, k in POSITION_BANDS],
        "min_field": MIN_FIELD,
        "thin_n": sum(1 for x in items if x["band"] == "thin"),
        "sign_note": "正数 = 我方更贵；负数 = 我方更便宜。基准是对位竞品价格的中位数。",
        "note": ("对位竞品来自竞品匹配（同国同币种、规格与价位带都通过闸门）。"
                 "中位数比均值稳：一台离群的高价机不会把整组拽偏。"
                 f"★ 对位不足 {MIN_FIELD} 款的只给价差、不给站位判断 —— "
                 "一个样本的中位数就是那个样本，看着精确其实什么也没说。"),
    }
