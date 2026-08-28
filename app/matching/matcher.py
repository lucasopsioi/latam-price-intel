# -*- coding: utf-8 -*-
"""竞品匹配引擎 —— 判定"谁是我这款产品在这个国家的竞品"。

用户给的三条判定规则（原话）：
  1. 产品规格要比较相似，也就是产品竞争力要大差不差
  2. 我的产品在本地是有货的，并且在平台上是可以买到的
  3. 本地价格和友商规格差不多的那个产品的价格是差不多的

★ 实现成"硬闸 + 排序分"，而不是简单加权求和：

  三条规则是 AND 关系，不是"可以互相补偿"的关系。
  如果做成加权和，一台规格满分但价格贵三倍的机器仍可能拿到高分被判成竞品 ——
  那不是竞品，那是不同价位段的产品。所以：

    硬闸（不满足直接出局）：
      · 该国有效在售（近 N 天有观测、有货、通过价格审计）
      · 价格落在 ±band 带内
      · 规格分不低于下限
    排序分（决定"最像的前几个"）：
      total = 0.45×规格 + 0.35×价格接近 + 0.20×可得性

  每一条判定都写进 reasons 存档，界面上点开能看到"为什么它是竞品"，
  也能人工推翻（is_confirmed / is_excluded）。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from .. import config, db
from .specs import spec_similarity

log = logging.getLogger("matcher")

FRESH_DAYS = 14           # 价格观测在多少天内算"当前在售"
W_SPEC, W_PRICE, W_AVAIL = 0.45, 0.35, 0.20


class CompetitorMatcher:
    def __init__(self, cfg: dict | None = None):
        m = (cfg or config.load_runtime()).get("matching", {})
        self.spec_min = float(m.get("spec_min", 0.55))
        self.price_band = float(m.get("price_band_pct", 0.30))
        self.total_min = float(m.get("total_min", 0.60))
        self.top_n = int(m.get("top_n_per_country", 8))
        self.stats = {"pairs": 0, "matched": 0, "no_price": 0, "out_of_band": 0,
                      "spec_fail": 0}

    # ------------------------------------------------ 主流程

    def rebuild_all(self, my_product_id: int | None = None) -> dict:
        """重算竞品匹配。传 my_product_id 则只算这一款。"""
        where = "WHERE mp.status='active'"
        params: list = []
        if my_product_id:
            where += " AND mp.id=?"
            params.append(my_product_id)

        my_products = db.q(f"""
            SELECT mp.* FROM my_product mp {where} ORDER BY mp.id
        """, params)
        if not my_products:
            return {"products": 0, "matches": 0,
                    "warning": "还没有录入我方产品 —— 竞品匹配需要先有「我的产品」"}

        countries = db.q("SELECT * FROM country WHERE enabled=1")
        # ★ 按口径跳过的国家要**显式报出来**，不能混进"没匹配上"里。
        #   BR/AR Acme没有自营商城 ⇒ 拿不到官方定价 ⇒ 算不出价差，
        #   这是**口径决定**（用户 2026-08-11 确认），不是数据缺口。
        #   两者必须分开：BR/AR 为空是应该的；MX/CO/CL/PE 为空是出问题了。
        #   混在一起会让真问题被当成"本来就这样"。
        skipped = [c for c in countries if not c["own_pricing"]]
        active = [c for c in countries if c["own_pricing"]]

        total_matches = 0
        for mp in my_products:
            for co in active:
                total_matches += self._match_one(mp, co)

        return {"products": len(my_products), "countries": len(active),
                "matches": total_matches, "stats": self.stats,
                "skipped_countries": [c["code"] for c in skipped],
                "skipped_reason": (
                    f"{'、'.join(c['name_zh'] for c in skipped)} 按口径不做竞品对照"
                    f"（Acme在这两国无自营商城，取不到官方定价）；"
                    f"友商价格照常采集，看板/价格变动/周报不受影响"
                ) if skipped else ""}

    def _match_one(self, mp: dict, country: dict) -> int:
        cc = country["code"]

        # 规则②前半：我方产品在该国得是在售的，否则无所谓竞品
        mine_price = self._our_price(mp, cc)
        if not mine_price or not mine_price["rrp_local"]:
            return 0

        my_specs = self._my_specs(mp)
        # 用我方定价的币种去筛候选，保证两边同币可比
        currency = mine_price.get("currency") or country["currency"]
        candidates = self._candidates(mp["category_code"], cc, currency)
        scored = []

        for rv in candidates:
            self.stats["pairs"] += 1
            result = self._score_pair(mp, my_specs, mine_price, rv, country)
            if result:
                scored.append(result)

        scored.sort(key=lambda x: -x["total_score"])
        top = scored[: self.top_n]
        self._persist(mp["id"], cc, top)
        self.stats["matched"] += len(top)
        return len(top)

    # ------------------------------------------------ 候选池

    def _candidates(self, category: str, cc: str, currency: str) -> list[dict]:
        """规则②后半：友商产品必须在该国"有货且买得到"。

        判据 = 近 FRESH_DAYS 天有通过审计的价格观测、且有货。
        这一条同时挡住了两类噪声：只在新闻里出现过但没上市的产品、
        以及历史上卖过但已经下架的产品。

        ★ 必须同时按币种过滤。price_obs.currency 来自页面（JSON-LD 的
          priceCurrency 可能是 USD，跨境商品也常标外币），只按 country
          分组会把 USD 标价和本币标价混在一起求平均 ——
          算出来的"均价"既不是美元也不是比索，而竞品匹配拿它去比价。
        """
        since = (date.today() - timedelta(days=FRESH_DAYS)).isoformat()
        return db.q("""
            SELECT rp.*, b.name AS brand_name,
                   COUNT(DISTINCT po.channel_id)          AS channel_count,
                   COUNT(po.id)                           AS obs_count,
                   MIN(po.sale_price)                     AS min_price,
                   MAX(po.sale_price)                     AS max_price,
                   AVG(po.sale_price)                     AS avg_price,
                   MAX(po.obs_date)                       AS last_seen,
                   MAX(po.currency)                       AS currency
            FROM rival_product rp
            JOIN brand b        ON b.id = rp.brand_id
            JOIN price_obs po   ON po.rival_product_id = rp.id
            WHERE rp.category_code = ?
              AND po.country_code  = ?
              AND po.currency      = ?
              AND po.obs_date     >= ?
              -- ★ 排除 rejected，而不是「只要 accepted」。
              --   审计是**后置**阶段：采集刚跑完时全是 pending。
              --   写成 = 'accepted' 会让候选恒为 0，竞品匹配永远算不出来 ——
              --   这是把「还没处理」当成了「不合格」。
              AND po.audit_status <> 'rejected'
              AND po.is_in_stock   = 1
              AND po.condition     = 'new'
              AND po.is_bundle     = 0
            GROUP BY rp.id
            HAVING COUNT(po.id) >= 1
        """, (category, cc, currency, since))

    # ------------------------------------------------ 打分

    # 每个国家的「型号键 → 我方商城报价」索引。一次采集内不变，缓存起来，
    # 免得每个产品都全表扫一遍Acme观测。
    _OUR_INDEX: dict[str, dict] = {}

    @staticmethod
    def _our_price_by_model_key(name: str, cc: str) -> dict | None:
        from .modelkey import model_keys
        idx = CompetitorMatcher._OUR_INDEX.get(cc)
        if idx is None:
            idx = {}
            rows = db.q("""
                SELECT po.title, po.sale_price AS rrp_local, po.currency,
                       c.kind AS channel_kind, c.name AS channel_name,
                       po.obs_date, po.seller_kind
                FROM price_obs po
                JOIN brand b ON b.id = po.brand_id
                JOIN channel c ON c.id = po.channel_id
                WHERE b.is_ours = 1 AND po.country_code = ?
                  AND po.sale_price IS NOT NULL
                  AND po.product_kind = 'device'      -- 正面断言，理由见上
                  AND po.condition = 'new'
                  AND po.obs_date >= date('now','-21 day')
                ORDER BY (c.kind = 'brand_store') DESC,
                         (po.seller_kind IN ('self_operated','brand_official')) DESC,
                         po.obs_date DESC
            """, (cc,))
            # 顺序已按「官方商城 > 自营 > 日期新」排好，首次写入的即最优，
            # 后面的同键不覆盖
            for r in rows:
                for k in model_keys(r["title"]):
                    idx.setdefault(k, r)
            CompetitorMatcher._OUR_INDEX[cc] = idx

        for k in model_keys(name):
            r = idx.get(k)
            if r and r["rrp_local"]:
                out = dict(r)
                out["source"] = ("官方商城（型号键）" if r["channel_kind"] == "brand_store"
                                 else f"{r['channel_name']}官方渠道（型号键）")
                return out
        return None

    @staticmethod
    def _our_price(mp: dict, cc: str) -> dict | None:
        """我方产品在该国的价格。

        ★ 优先用**抓回来的商城价**，而不是手工录入的 my_pricing。
          用户说得对：「商城的价格就是官方定价」—— 没道理让人再录一遍，
          而且手工表一定会过期，抓来的天天更新。

        取价顺序：
          1. Acme官方商城（brand_store）的当前价 —— 这就是官方定价
          2. 零售渠道里判定为「自营/官方店」的价 —— 官方授权渠道价
          3. 手工录入的 my_pricing —— 兜底（有些型号还没被抓到）

        只取近 21 天的：太旧的价格不能代表"现在在售"。
        """
        name = (mp.get("marketing_name") or "").strip()
        if name:
            row = db.q1("""
                SELECT po.sale_price AS rrp_local, po.currency,
                       c.kind AS channel_kind, c.name AS channel_name,
                       po.obs_date, po.seller_kind
                FROM price_obs po
                JOIN brand b ON b.id = po.brand_id
                JOIN channel c ON c.id = po.channel_id
                WHERE b.is_ours = 1
                  AND po.country_code = ?
                  AND po.sale_price IS NOT NULL
                  -- ★★ 正面断言 device，不是 <> 'accessory'。
                  --   否定式会把 product_kind='unknown' 放行，而 unknown 里
                  --   混着**页面文案**：Acme商城 MX 首轮 54 条里有 11 条是
                  --   "12 X $666 TIN 0% TAE 0%* Ver más" 这类分期说明，
                  --   月供 666 被解析成价格。它们全是 unknown ⇒ 会被当成
                  --   我方官方价，进而污染每一条价差对比。
                  --   （价格带那边刚踩过一模一样的坑：Acme智利"手机"中位价
                  --     17,683 CLP 其实是副厂屏幕。）
                  AND po.product_kind = 'device'
                  AND po.condition = 'new'
                  AND po.obs_date >= date('now','-21 day')
                  AND (po.title LIKE ? OR po.model_guess LIKE ? OR po.sku_code LIKE ?)
                -- 官方商城优先，其次自营/官方店，最后按日期新的优先
                ORDER BY (c.kind = 'brand_store') DESC,
                         (po.seller_kind IN ('self_operated','brand_official')) DESC,
                         po.obs_date DESC
                LIMIT 1
            """, (cc, f"%{name}%", f"%{name}%", f"%{name}%"))
            if row and row["rrp_local"]:
                row["source"] = ("官方商城" if row["channel_kind"] == "brand_store"
                                 else f"{row['channel_name']}官方渠道")
                return row

            # ★★ 整串 LIKE 匹配不上时，退到**型号键**再试一次。
            #   中西文口径对不上是常态而非例外：
            #     我方  AceBook D 16 16吋 2024 12th Gen Core
            #     商城  ACME Laptop Acebook D16 16" FHD | Intel Core i5…
            #   差在「吋」是中文单位、D 16 与 D16 空格不同、代际后缀商城不写。
            #   实测：电脑 0/11、平板 0/16 完全没有对照，而两边的货都在库里。
            #   接上之后我方产品取到价的比例 48% → 66%（上限 72%，
            #   其余 32 个是拉美商城确实没铺的代际，不该强行匹配）。
            row = CompetitorMatcher._our_price_by_model_key(name, cc)
            if row:
                return row

        # 兜底：手工录入的定价（抓不到时才用）
        row = db.q1("""
            SELECT rrp_local, currency, on_sale FROM my_pricing
            WHERE product_id=? AND country_code=? AND on_sale=1
            ORDER BY sku_id IS NULL DESC LIMIT 1
        """, (mp["id"], cc))
        if row:
            row["source"] = "手工录入"
        return row

    def _score_pair(self, mp: dict, my_specs: dict, mine_price: dict,
                    rv: dict, country: dict) -> dict | None:
        reasons = []

        # —— 规则③：价格接近（硬闸）——
        my_p = mine_price["rrp_local"]
        # 用中位性质更强的均价；同型号在不同渠道有价差是常态
        rival_p = rv["avg_price"]
        if not rival_p or not my_p:
            self.stats["no_price"] += 1
            return None
        gap = (rival_p - my_p) / my_p
        if abs(gap) > self.price_band:
            self.stats["out_of_band"] += 1
            return None
        price_score = max(0.0, 1.0 - abs(gap) / self.price_band)
        reasons.append(
            f"价格 {rival_p:,.0f} vs 我方 {my_p:,.0f} {country['currency']}"
            f"（{gap:+.1%}），落在 ±{self.price_band:.0%} 带内")

        # —— 规则①：规格相似（硬闸）——
        sim = spec_similarity(my_specs, self._rival_specs(rv), mp["category_code"])
        # ★ 一个规格维度都比不了时豁免这道闸。
        #   "我不知道它们像不像" ≠ "它们不像"。规格数据还没补全时
        #   卡死这道闸会让所有候选一律出局，等于匹配功能整个不可用 ——
        #   而使用者只会看到"没有竞品"，不会知道是缺数据导致的。
        spec_unknown = sim["confidence"] <= 0.0
        if spec_unknown:
            reasons.append("⚠ 规格数据缺失，本条**未经规格校验**，"
                           "仅凭价格带与可得性匹配 —— 请跑「补全产品规格」后复核")
        elif sim["score"] < self.spec_min:
            self.stats["spec_fail"] += 1
            return None
        else:
            reasons.append(f"规格相似度 {sim['score']:.2f}（{sim['note']}）")

        # —— 规则②：可得性 ——
        avail_score = self._availability_score(rv)
        reasons.append(
            f"该国 {rv['channel_count']} 个渠道有货、近 {FRESH_DAYS} 天 "
            f"{rv['obs_count']} 条有效观测，最近一次 {rv['last_seen']}")

        if spec_unknown:
            # 规格未知时按剩余两项重新分配权重，而不是拿 0.5 中性值凑数 ——
            # 凑数会让"完全没规格"和"规格中等相似"得到相同的分，看不出差别
            w_sum = W_PRICE + W_AVAIL
            total = (W_PRICE * price_score + W_AVAIL * avail_score) / w_sum
            total *= 0.85     # 依据比完整匹配弱，整体降权，排序时自然靠后
        else:
            total = W_SPEC * sim["score"] + W_PRICE * price_score + W_AVAIL * avail_score
        if total < self.total_min:
            return None

        # 规格置信度低时明说，避免把"没比过几个维度"当成"很像"
        if 0 < sim["confidence"] < 0.5:
            reasons.append(
                f"⚠ 规格数据只覆盖了 {sim['confidence']:.0%} 的权重"
                f"（缺 {sim['missing']}），相似度参考价值有限，建议补齐规格后复核")

        return {
            "rival_product_id": rv["id"], "rival_name": rv["model_name"],
            "brand": rv["brand_name"],
            "spec_score": sim["score"], "price_score": round(price_score, 3),
            "avail_score": round(avail_score, 3), "total_score": round(total, 3),
            "my_price": my_p, "rival_price": rival_p,
            "currency": rv["currency"] or country["currency"],
            # ★ 存**百分数**（-3.85）不是小数（-0.0385）。
            #   字段名带 _pct 却存小数，会和同库的 discount_pct（存百分数）
            #   形成两套单位 —— 谁写查询都得先翻代码确认，翻错就静默差 100 倍。
            "price_gap_pct": round(gap * 100, 2),
            "reasons": reasons, "spec_dims": sim["dims"],
            "spec_confidence": sim["confidence"],
        }

    @staticmethod
    def _availability_score(rv: dict) -> float:
        """可得性评分。

        ★ "能不能买到"本质是二元的 —— 在一个渠道有货就是买得到。
          铺货广度是次要加分项，不该是主项。
          第一版按渠道数线性给分（要 4 个渠道才满分），结果单渠道产品
          只拿 0.49 分，把总分压到门槛以下 —— 于是"只在一个渠道卖但
          确实在卖"的竞品被判成不是竞品，这不符合规则②的本意。
        """
        base = 0.5 if (rv["obs_count"] or 0) > 0 else 0.0   # 买得到 = 基础分
        ch = min(rv["channel_count"] or 0, 4) / 4.0          # 铺货广度
        obs = min(rv["obs_count"] or 0, 10) / 10.0           # 观测密度
        try:
            days = (date.today() - date.fromisoformat(rv["last_seen"])).days
        except Exception:  # noqa: BLE001
            days = FRESH_DAYS
        fresh = max(0.0, 1.0 - days / FRESH_DAYS)            # 数据新鲜度
        return round(base + 0.25 * ch + 0.15 * obs + 0.10 * fresh, 3)

    # ------------------------------------------------ 规格取值

    @staticmethod
    def _my_specs(mp: dict) -> dict:
        sku = db.q1("""SELECT ram_gb, rom_gb FROM my_sku WHERE product_id=?
                       ORDER BY rom_gb DESC LIMIT 1""", (mp["id"],))
        return {
            "chipset": mp.get("chipset"),
            "ram_gb": (sku or {}).get("ram_gb"),
            "rom_gb": (sku or {}).get("rom_gb"),
            "screen_size": _screen_inches(mp.get("screen")),
        }

    @staticmethod
    def _rival_specs(rv: dict) -> dict:
        return {"chipset": rv.get("chipset"), "ram_gb": rv.get("ram_gb"),
                "rom_gb": rv.get("rom_gb"), "screen_size": rv.get("screen_size"),
                "battery_mah": rv.get("battery_mah"),
                "camera_main_mp": rv.get("camera_main_mp")}

    # ------------------------------------------------ 落库

    def _persist(self, my_id: int, cc: str, top: list[dict]) -> None:
        with db.tx() as conn:
            # 人工确认/排除过的不覆盖 —— 人的判断优先于算法
            conn.execute("""DELETE FROM competitor_match
                            WHERE my_product_id=? AND country_code=?
                              AND source='auto' AND is_confirmed=0 AND is_excluded=0""",
                         (my_id, cc))
            for rank, m in enumerate(top, 1):
                conn.execute("""
                    INSERT INTO competitor_match(my_product_id,rival_product_id,
                        country_code,spec_score,price_score,avail_score,total_score,
                        rank_in_country,reasons,my_price_local,rival_price_local,
                        currency,price_gap_pct,source,computed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'auto',datetime('now'))
                    ON CONFLICT(my_product_id,rival_product_id,country_code)
                    DO UPDATE SET
                      spec_score=excluded.spec_score, price_score=excluded.price_score,
                      avail_score=excluded.avail_score, total_score=excluded.total_score,
                      rank_in_country=excluded.rank_in_country,
                      reasons=excluded.reasons, my_price_local=excluded.my_price_local,
                      rival_price_local=excluded.rival_price_local,
                      price_gap_pct=excluded.price_gap_pct,
                      computed_at=datetime('now')
                    WHERE competitor_match.is_confirmed=0
                      AND competitor_match.is_excluded=0
                """, (my_id, m["rival_product_id"], cc, m["spec_score"],
                      m["price_score"], m["avail_score"], m["total_score"], rank,
                      json.dumps({"reasons": m["reasons"], "dims": m["spec_dims"],
                                  "spec_confidence": m["spec_confidence"]},
                                 ensure_ascii=False),
                      m["my_price"], m["rival_price"], m["currency"],
                      m["price_gap_pct"]))


def _screen_inches(text: str | None) -> float | None:
    if not text:
        return None
    import re
    m = re.search(r"(\d{1,2}[.,]\d{1,2}|\d{1,2})", str(text))
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
        return v if 3.0 <= v <= 20.0 else None
    except ValueError:
        return None
