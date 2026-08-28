# -*- coding: utf-8 -*-
"""价格变动检测 —— 竞争动态看板的底层事实。

这一层**不调 LLM**：检测"哪个 SKU 在哪个渠道涨了跌了多少"是纯计算，
交给模型既慢又不可靠。模型的价值在下一层（解读意图），不在这里。

★ 三条必须守住的比价纪律（每一条都是踩出来的）：

  1. **同 SKU 比价**：容量/内存/颜色都要对齐。
     只按「产品+渠道」比，会拿今天的 128G 去减昨天的 512G，
     播报出一个根本不存在的「降价 40%」—— 这种错报比不报危险，
     因为它看起来像一条真情报。

  2. **同币种比价**：price_obs.currency 来自页面，跨境商品可能标 USD。

  3. **区分官方与第三方**：官网/自营降价 = 厂商的定价动作；
     第三方降价可能只是某个卖家甩货。用户明确要盯官方渠道，
     两者混在一起会把噪声当成战略信号。
"""
from __future__ import annotations

import logging

from .. import db
from .base import BaseAgent

log = logging.getLogger("pricemove")

MIN_CHANGE_PCT = 2.0        # 小于这个幅度当噪声（页面取整、汇率抖动）


class PriceMoveAgent(BaseAgent):
    name = "price_move"
    role = "价格变动"
    description = "检测同SKU同渠道的价格变动，区分官方渠道与第三方"

    def run(self, obs_date: str | None = None, lookback_days: int = 30) -> dict:
        d = obs_date or db.today()
        self.start(f"检测 {d} 的价格变动")

        rows = db.q("""
            WITH cur AS (
              SELECT po.id, po.obs_date, po.country_code, po.channel_id,
                     po.brand_id, po.category_code, po.rival_product_id,
                     po.sale_price, po.currency,
                     -- ★★ SKU 身份优先用 **URL**：同一个商品页就是同一个 SKU，
                     --   这是唯一不会随解析质量漂移的身份。
                     --   规格拼出来的 key 会跳组：同一台 Moto G06 的 RAM
                     --   在 08-10 没解析出来、08-11 解析出来了，于是它从
                     --   "128|-1|" 跳到 "128|4|"，MIN() 拿两条**不同挂牌**去比，
                     --   报出 +44.5% 的假涨价（按 URL 比真相是 -5.3%）。
                     CASE WHEN po.url IS NOT NULL AND po.url <> ''
                          THEN 'u:' || po.url
                          ELSE 's:' || IFNULL(po.rom_gb,-1) || '|'
                               || IFNULL(po.ram_gb,-1) || '|'
                               || IFNULL(po.color,'') END AS sku_key,
                     po.seller_kind, c.kind AS channel_kind
              FROM price_obs po
              JOIN channel c ON c.id = po.channel_id
              WHERE po.obs_date = ?
                AND po.sale_price IS NOT NULL
                AND po.rival_product_id IS NOT NULL
                AND po.product_kind <> 'accessory'
                AND po.condition = 'new'
                AND po.is_bundle = 0
                AND po.audit_status <> 'rejected'
            )
            -- ★ prev 必须先聚合成**每个 SKU 一行**再 JOIN。
            --   直接 JOIN price_obs 会在"上一次观测日有多条同 SKU 记录"时
            --   产生多行（同款不同颜色、或同一页被抓到两次），
            --   prev_price 取到哪一条是**不确定的** —— 有时恰好取到与今天
            --   相同的价，就产生了「价格没变却报变动」。
            --   聚合查询里最容易漏掉这条：JOIN 另一侧不唯一时结果不确定，且不报错。
            --   同理 cur 也要聚合：今天同 SKU 也可能有多条。
            , cur_agg AS (
              SELECT country_code, channel_id, brand_id, category_code,
                     rival_product_id, sku_key, currency, channel_kind,
                     MIN(sale_price) AS sale_price,
                     MAX(seller_kind) AS seller_kind
              FROM cur GROUP BY channel_id, rival_product_id, sku_key, currency
            ),
            prev_agg AS (
              SELECT po.rival_product_id, po.channel_id, po.country_code, po.currency,
                     CASE WHEN po.url IS NOT NULL AND po.url <> ''
                          THEN 'u:' || po.url
                          ELSE 's:' || IFNULL(po.rom_gb,-1) || '|'
                               || IFNULL(po.ram_gb,-1) || '|'
                               || IFNULL(po.color,'') END AS sku_key,
                     po.obs_date,
                     MIN(po.sale_price) AS sale_price
              FROM price_obs po
              WHERE po.obs_date < ? AND po.obs_date >= date(?, ?)
                AND po.sale_price IS NOT NULL
                AND po.rival_product_id IS NOT NULL
                AND po.audit_status <> 'rejected'
                AND po.product_kind <> 'accessory'
                AND po.condition = 'new'
              GROUP BY po.channel_id, po.rival_product_id, sku_key,
                       po.currency, po.obs_date
            )
            SELECT cur_agg.*, prev_agg.sale_price AS prev_price,
                   prev_agg.obs_date AS prev_date
            FROM cur_agg
            JOIN prev_agg ON prev_agg.rival_product_id = cur_agg.rival_product_id
                 AND prev_agg.channel_id = cur_agg.channel_id
                 AND prev_agg.country_code = cur_agg.country_code
                 AND prev_agg.currency = cur_agg.currency
                 AND prev_agg.sku_key = cur_agg.sku_key
            WHERE prev_agg.obs_date = (
              SELECT MAX(p2.obs_date) FROM prev_agg p2
              WHERE p2.rival_product_id = cur_agg.rival_product_id
                AND p2.channel_id = cur_agg.channel_id
                AND p2.sku_key = cur_agg.sku_key)
        """, (d, d, d, f"-{int(lookback_days)} day"))

        found = 0
        with db.tx() as conn:
            for r in rows:
                prev, curr = r["prev_price"], r["sale_price"]
                if not prev or prev <= 0:
                    continue
                # ★ 规格全缺失时不比价。
                #   sku_key 是 rom|ram|color，三者都没解析出来时 key 恒为 "-1|-1|"，
                #   于是同一渠道下所有"规格未知"的同型号变体被当成同一个 SKU 互比 ——
                #   实测出现「1,359,990 → 1,359,990 却报降价 8.1%」。
                #   **缺失不等于相同**：确认不了是同一个 SKU 就不该比。
                if r["sku_key"] == "s:-1|-1|":
                    continue
                pct = (curr - prev) / prev * 100
                if abs(pct) < MIN_CHANGE_PCT:
                    continue
                is_official = int(
                    r["channel_kind"] == "brand_store"
                    or r["seller_kind"] in ("self_operated", "brand_official"))
                try:
                    conn.execute("""
                        INSERT INTO price_move(move_date,country_code,channel_id,
                          brand_id,category_code,rival_product_id,sku_key,
                          prev_price,curr_price,change_pct,direction,prev_date,
                          days_span,is_official,currency)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,
                               CAST(julianday(?)-julianday(?) AS INTEGER),?,?)
                        ON CONFLICT(move_date,channel_id,rival_product_id,sku_key)
                        DO UPDATE SET curr_price=excluded.curr_price,
                                      change_pct=excluded.change_pct,
                                      direction=excluded.direction
                    """, (d, r["country_code"], r["channel_id"], r["brand_id"],
                          r["category_code"], r["rival_product_id"], r["sku_key"],
                          prev, curr, round(pct, 2),
                          "down" if pct < 0 else "up",
                          r["prev_date"], d, r["prev_date"], is_official,
                          r["currency"]))
                    found += 1
                except Exception as e:  # noqa: BLE001
                    log.debug("写入变动失败: %s", str(e)[:100])

        off = db.q1("""SELECT COUNT(*) c FROM price_move
                       WHERE move_date=? AND is_official=1""", (d,))["c"]
        down = db.q1("""SELECT COUNT(*) c FROM price_move
                        WHERE move_date=? AND direction='down'""", (d,))["c"]
        self.log_step("价格变动检测",
                      parsed={"变动": found, "官方渠道": off, "降价": down,
                              "涨价": found - down},
                      decision="ok",
                      reason=f"同SKU同渠道同币种比价，过滤 {MIN_CHANGE_PCT}% 以下噪声")
        self.finish("ok", f"检测到 {found} 个价格变动（官方渠道 {off} 个）", found, found)
        return {"moves": found, "official": off, "down": down, "up": found - down}
