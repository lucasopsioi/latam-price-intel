# -*- coding: utf-8 -*-
"""价格策略推断 Agent —— 从「价格怎么动」推到「厂商想干什么」。

price_move 回答的是"发生了什么"（谁涨了谁跌了多少），这一层回答"为什么"：
是老品清库、给新品腾位置、大促前铺垫、成本上涨转嫁，还是打价格战。

★ 本文件的核心设计取舍：**数字由 SQL 算，意图由模型判**。

  模型拿不到数据库，让它"分析一下某某品牌的策略"必然是编。所以流程是：
    ① SQL 把候选模式连同全部支撑数字捞出来（无 LLM，可复现、可复核）
    ② 把这组数字连同 7 种策略的定义喂给模型，只让它回答"像哪一种"
    ③ 模型的回答再过一遍规则校验（类型白名单 + 置信度封顶）才落库
  模型任何一步失败/胡说，最坏结果是这条候选不出信号，不会产出假数字。

★ 官方渠道分三档，不是二值（用户明确要求区分，这里再细一层）：
    tier 2  品牌官网 / 品牌官方旗舰   → 最能代表厂商定价意图
    tier 1  零售商自营（Falabella 自营等）→ 真实货架价，但可能是零售商自己的促销
    tier 0  第三方卖家                 → 直接排除，个别卖家甩货不是厂商动作
  实测教训就在库里：CL 手机品类 7 个品牌同日集体降价，看着像价格战，
  但 chans=1 —— 全部发生在 Falabella 一家。那是**渠道促销**，不是价格战。
  所以"参与渠道数"必须作为判据交给模型，且 prompt 里写死这条判别规则。

★ 跨国比较只能用百分比：CLP / PEN / MXN / BRL 的绝对值毫无可比性。
  所有阈值、所有喂给模型的口径都是百分比；绝对金额只在同一 track 内部出现。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from .. import db
from .base import BaseAgent
from .llm import as_dict, as_float

log = logging.getLogger("strategy")


# ---------------------------------------------------------------- 常量

# 七种信号的定义。这段文本会原样进 prompt —— 模型的判据必须和落库的枚举严格一致，
# 各写一份迟早会漂移，所以只维护这一份。
SIGNAL_TYPES: dict[str, str] = {
    "clearance": "老品清库：同一款连续多次降价 / 累计降幅很大 / 在售渠道正在减少，"
                 "厂商在把库存清干净，通常意味着这款要退场了",
    "pre_launch_cut": "新品上市前给老款腾位置：同系列有更新一代出现，老款开始降价",
    "promo_prep": "大促前铺垫：同品牌多个 SKU 同时降价，或划线价被抬高（为促销做价差空间）",
    "cost_hike": "成本上涨提价：多个渠道同向涨价，不是个别卖家行为",
    "price_war": "价格战：同一国家同一品类，多个品牌在短期内互相跟降",
    "new_launch_premium": "新品高定价入场：新款首次上架就定在品类价格带上沿",
    "restore": "促销结束回价：先降后涨，涨回到接近促销前的水平",
}

# ---- 粗筛阈值。定得偏松：这一层只负责"值不值得让模型看一眼"，
#      真正的强弱由证据数字和置信度封顶决定，不靠阈值一刀切。
CUM_DROP_PCT = -8.0        # 产品级窗口累计降幅达到多少算候选
BIG_DROP_PCT = -15.0       # 用户口径的"累计降幅很大"
CUM_RISE_PCT = 5.0         # 涨价候选门槛
CLUSTER_MIN_PRODUCTS = 3   # 品牌齐动：至少几款一起动才不算巧合
WAR_MIN_BRANDS = 3         # 价格战：至少几个品牌一起降
POLLUTED_SPREAD = 1.5      # 同日同产品同 SKU 内部价差 >50% ⇒ 归一化桶不干净
MOVE_TOLERANCE_PP = 1.0    # price_move 自洽性容差（百分点）
MAX_LLM_CALLS = 40         # 单次运行的模型调用上限，防跑飞

# 型号名被采集噪声污染的特征（库里实测大量 "Envã Gratis App iPhone" 这类，
# 是列表页把"免运费"角标当成标题抓进来了）。名字不可信时不能让模型
# 拿它当产品结论，只能降置信度并在证据里标出来。
_BROKEN_NAME = re.compile(
    r"(env[ãíi]o?\s*gratis|gratis\s*app|^app\s|\bcuotas?\b|\bdesde\b)", re.I)


# ---------------------------------------------------------------- 大促日历

def _last_friday_of_november(year: int) -> date:
    d = date(year, 11, 30)
    return d - timedelta(days=(d.weekday() - 4) % 7)


def _promo_calendar(year: int, country: str) -> list[tuple[str, date]]:
    """拉美大促的**近似**日历。

    ★ 为什么标"近似"并且只当上下文提示：黑五/圣诞/智利国庆是固定的，
      但 Hot Sale、CyberDay、CyberDays 每年官宣才定档，只能给到月级。
      所以这里算出来的日期**绝不写进 evidence 当观测数据**，只作为
      "距离下一个大促还有多少天（近似）"提示模型，模型也被明确要求
      不许把它当成事实引用。查不到就是查不到，不许编一个精确日期。
    """
    bf = _last_friday_of_november(year)
    common = [
        ("黑色星期五", bf),
        ("网络星期一", bf + timedelta(days=3)),
        ("圣诞季", date(year, 12, 25)),
    ]
    by_country = {
        "MX": [("Buen Fin（月级近似）", date(year, 11, 15)),
               ("Hot Sale（月级近似）", date(year, 5, 20)),
               ("母亲节", date(year, 5, 10))],
        "CL": [("Fiestas Patrias 国庆", date(year, 9, 18)),
               ("CyberDay（月级近似）", date(year, 10, 15))],
        "PE": [("CyberDays（月级近似）", date(year, 7, 15)),
               ("CyberWow（月级近似）", date(year, 4, 15))],
        "BR": [("Dia dos Namorados", date(year, 6, 12))],
        "CO": [("Amor y Amistad", date(year, 9, 19))],
        "AR": [("Hot Sale（月级近似）", date(year, 5, 15))],
    }
    return common + by_country.get(country, [])


def _next_promo(country: str, ref: date, horizon: int = 45) -> dict | None:
    """返回 horizon 天内最近的一个大促（近似）。没有就返回 None ——
    没有就是没有，不能为了凑 promo_prep 硬找一个。"""
    best = None
    for year in (ref.year, ref.year + 1):
        for name, when in _promo_calendar(year, country):
            gap = (when - ref).days
            if 0 <= gap <= horizon and (best is None or gap < best["距今天数"]):
                best = {"名称": name, "距今天数": gap, "口径": "近似日历，非观测数据"}
    return best


# ---------------------------------------------------------------- SQL

# 官方档位。写成 SQL 片段复用，避免两处查询口径不一致 ——
# 口径不一致比查错更难发现，因为两个结果各自看起来都合理。
_TIER_SQL = """
        CASE WHEN ch.kind='brand_store' OR po.seller_kind='brand_official' THEN 2
             WHEN po.seller_kind='self_operated' THEN 1
             ELSE 0 END
"""

# price_move 自洽性过滤。
# ★ 实测：库里 80 条 price_move 有 23 条 change_pct 和 (curr-prev)/prev 对不上
#   （pricemove.py 的 ON CONFLICT 只更新 curr_price/change_pct 不更新 prev_price，
#   同一 SKU 当天有多条挂牌时就会拼出一行前后不属于同一对的数据）。
#   把一个错的 "-11%" 喂给模型，模型会一本正经地推断出一个不存在的策略 ——
#   这比不报危险得多。所以这里只用自洽的行，并把剔除数量记进留痕。
_MOVE_SANE = ("pm.prev_price > 0 AND "
              "ABS(pm.change_pct - (pm.curr_price - pm.prev_price) / pm.prev_price * 100) <= ?")

SQL_TRACKS = f"""
WITH src AS (
  SELECT po.obs_date, po.country_code, po.brand_id, po.category_code,
         po.rival_product_id, po.currency, po.sale_price, po.list_price, po.channel_id,
         ch.name AS channel_name,
         IFNULL(po.rom_gb,-1)||'|'||IFNULL(po.ram_gb,-1)||'|'||IFNULL(po.color,'') AS sku_key,
         {_TIER_SQL} AS tier
  FROM price_obs po
  JOIN channel ch ON ch.id = po.channel_id
  WHERE po.obs_date BETWEEN ? AND ?
    AND po.sale_price > 0
    AND po.rival_product_id IS NOT NULL
    AND po.product_kind <> 'accessory'
    AND po.condition = 'new'
    AND po.is_bundle = 0
    AND po.audit_status <> 'rejected'   -- 审计是后置阶段，刚采完全是 pending
),
daily AS (
  -- 同一天同一产品同一 SKU 可能有多条挂牌（多渠道、或同渠道重复列表）。
  -- 取当天最低官方价作为"货架价"：窗口两端用同一口径，
  -- 才不会因为挂牌集合变化凭空造出一个降价。price_hi 留着做污染检测。
  SELECT obs_date, country_code, brand_id, category_code, rival_product_id,
         sku_key, currency,
         MIN(sale_price) AS price, MAX(sale_price) AS price_hi,
         MAX(list_price) AS list_price,
         COUNT(DISTINCT channel_id) AS n_channels,
         MAX(tier) AS top_tier
  FROM src
  WHERE tier >= 1                       -- ★ 第三方卖家不进厂商意图的判据
  GROUP BY 1,2,3,4,5,6,7
),
first_day AS (
  -- SQLite 的 bare column 规则：SELECT 里只有一个 MIN()/MAX() 聚合时，
  -- 同行裸列取自极值所在那一行。这是有文档保证的行为，比自连接便宜。
  SELECT country_code, brand_id, category_code, rival_product_id, sku_key, currency,
         MIN(obs_date) AS d0, price AS p0, list_price AS l0
  FROM daily GROUP BY 1,2,3,4,5,6
),
last_day AS (
  SELECT country_code, brand_id, category_code, rival_product_id, sku_key, currency,
         MAX(obs_date) AS d1, price AS p1, list_price AS l1
  FROM daily GROUP BY 1,2,3,4,5,6
),
span AS (
  SELECT country_code, brand_id, category_code, rival_product_id, sku_key, currency,
         COUNT(*) AS n_days, MIN(price) AS lo, MAX(price) AS hi,
         MAX(top_tier) AS top_tier, MAX(n_channels) AS n_channels,
         MAX(CASE WHEN price_hi > price * ? THEN 1 ELSE 0 END) AS polluted
  FROM daily GROUP BY 1,2,3,4,5,6
),
chans AS (
  -- 渠道名单独聚合：daily 已经把 channel 折叠掉了，但"是哪家渠道在动"
  -- 是判断厂商动作还是渠道促销的关键证据，必须给到模型
  SELECT country_code, brand_id, category_code, rival_product_id, sku_key, currency,
         GROUP_CONCAT(DISTINCT channel_name) AS chan_names
  FROM src WHERE tier >= 1 GROUP BY 1,2,3,4,5,6
)
SELECT s.country_code, s.brand_id, s.category_code, s.rival_product_id, s.sku_key,
       s.currency, s.n_days, s.lo, s.hi, s.top_tier, s.n_channels, s.polluted,
       f.d0, f.p0, f.l0, l.d1, l.p1, l.l1, c.chan_names,
       ROUND((l.p1 - f.p0) / f.p0 * 100, 2) AS cum_pct,
       ROUND((s.lo - s.hi) / s.hi * 100, 2) AS hi_lo_gap_pct,
       b.name AS brand_name, rp.model_name,
       -- 窗口开始之前这款在这个国家有没有被观测到过。
       -- new_launch_premium 全靠它：没有"之前没见过"这个事实，
       -- "新品"两个字就是编的。
       EXISTS(SELECT 1 FROM price_obs po2
              WHERE po2.rival_product_id = s.rival_product_id
                AND po2.country_code = s.country_code
                AND po2.obs_date < ?
                AND po2.audit_status <> 'rejected') AS seen_before
FROM span s
JOIN first_day f USING(country_code, brand_id, category_code, rival_product_id, sku_key, currency)
JOIN last_day  l USING(country_code, brand_id, category_code, rival_product_id, sku_key, currency)
JOIN chans     c USING(country_code, brand_id, category_code, rival_product_id, sku_key, currency)
JOIN brand b ON b.id = s.brand_id
LEFT JOIN rival_product rp ON rp.id = s.rival_product_id
-- ★ 这里**不过滤** d0<d1：只观测到一天的轨迹谈不上"变动"，但正是新品的形态。
--   变动类候选在 Python 里再筛，新品类候选需要这些单日轨迹。
WHERE b.is_ours = 0        -- 我方（Acme）不是推断对象，只是被影响方
  AND f.p0 > 0
"""

SQL_MOVE_BY_PRODUCT = f"""
SELECT pm.country_code, pm.rival_product_id, pm.sku_key,
       SUM(CASE WHEN pm.direction='down' THEN 1 ELSE 0 END) AS down_moves,
       SUM(CASE WHEN pm.direction='up'   THEN 1 ELSE 0 END) AS up_moves,
       ROUND(SUM(CASE WHEN pm.direction='down' THEN pm.change_pct ELSE 0 END), 2) AS down_sum_pct,
       COUNT(DISTINCT pm.channel_id) AS n_channels,
       MIN(pm.move_date) AS first_move, MAX(pm.move_date) AS last_move
FROM price_move pm
WHERE pm.is_official = 1 AND pm.move_date BETWEEN ? AND ? AND {_MOVE_SANE}
GROUP BY 1,2,3
"""

SQL_BRAND_CLUSTER = f"""
SELECT pm.country_code, pm.brand_id, b.name AS brand_name, pm.category_code,
       pm.direction,
       COUNT(DISTINCT pm.rival_product_id) AS prods,
       COUNT(*) AS moves,
       COUNT(DISTINCT pm.channel_id) AS chans,
       GROUP_CONCAT(DISTINCT ch.name) AS chan_names,
       ROUND(AVG(pm.change_pct), 2) AS avg_pct,
       ROUND(MIN(pm.change_pct), 2) AS min_pct,
       ROUND(MAX(pm.change_pct), 2) AS max_pct,
       MIN(pm.move_date) AS d0, MAX(pm.move_date) AS d1,
       MAX(CASE WHEN ch.kind='brand_store' THEN 1 ELSE 0 END) AS has_brand_store
FROM price_move pm
JOIN brand b   ON b.id  = pm.brand_id
JOIN channel ch ON ch.id = pm.channel_id
WHERE pm.is_official = 1 AND b.is_ours = 0
  AND pm.move_date BETWEEN ? AND ? AND {_MOVE_SANE}
GROUP BY 1,2,3,4,5
HAVING prods >= ?
"""

# 窗口前的采集覆盖度，按 国家×品类×品牌 统计。
# ★ new_launch_premium 的唯一依据是"窗口前没见过这款"，但"没见过"有两种可能：
#     ① 它真是新上市的  ② 我们当时根本没在抓这个国家/这个品类/这个品牌
#   分不清这两者，就会把"爬虫昨天才开始抓 PE 音频"报成"JBL 在秘鲁高价发新品"。
#   实测就是这样：不加这道闸，一次跑出 15 条全是覆盖度造成的假新品。
#   所以必须先证明"这块货架我们之前一直在看"，才允许说某款是新出现的。
SQL_PRE_COVERAGE = """
SELECT country_code, category_code, brand_id,
       COUNT(DISTINCT obs_date) AS pre_days,
       COUNT(DISTINCT rival_product_id) AS pre_prods
FROM price_obs
WHERE obs_date < ? AND obs_date >= date(?, '-90 day')
  AND audit_status <> 'rejected' AND rival_product_id IS NOT NULL
GROUP BY 1,2,3
"""

# 品牌×国家×品类的同向变动规模（不设门槛版）。
# 单品判 cost_hike 时要用：一款孤零零涨价分不清是成本还是渠道调价，
# 只有"同品牌一片在涨"或"多个渠道一起涨"才谈得上成本转嫁。
SQL_BRAND_SCALE = f"""
SELECT pm.country_code, pm.brand_id, pm.category_code, pm.direction,
       COUNT(DISTINCT pm.rival_product_id) AS prods,
       COUNT(DISTINCT pm.channel_id) AS chans
FROM price_move pm
WHERE pm.is_official = 1 AND pm.move_date BETWEEN ? AND ? AND {_MOVE_SANE}
GROUP BY 1,2,3,4
"""

SQL_CATEGORY_WAR = f"""
SELECT pm.country_code, pm.category_code,
       COUNT(DISTINCT pm.brand_id) AS brands,
       COUNT(DISTINCT pm.channel_id) AS chans,
       COUNT(DISTINCT pm.rival_product_id) AS prods,
       ROUND(AVG(pm.change_pct), 2) AS avg_pct,
       ROUND(MIN(pm.change_pct), 2) AS min_pct,
       MIN(pm.move_date) AS d0, MAX(pm.move_date) AS d1,
       GROUP_CONCAT(DISTINCT b.name) AS brand_names,
       GROUP_CONCAT(DISTINCT ch.name) AS chan_names
FROM price_move pm
JOIN brand b   ON b.id  = pm.brand_id
JOIN channel ch ON ch.id = pm.channel_id
WHERE pm.is_official = 1 AND pm.direction = 'down' AND b.is_ours = 0
  AND pm.move_date BETWEEN ? AND ? AND {_MOVE_SANE}
GROUP BY 1,2
HAVING brands >= ?
"""

# 同系列更新款：rival_product.series / global_launch_date 目前全库为空
# （实测 1768 条产品两列全 NULL），只能从型号名里抠"代数"。
# 抠不出来就返回 None —— 宁可判不出 pre_launch_cut，也不瞎猜同系列关系。
_GEN = re.compile(
    r"^(?P<family>.*?)(?P<gen>\d{1,3})"
    r"(?P<suffix>(\s*(pro|ultra|plus|max|lite|fe|se|c|s|5g|4g))*)\s*$", re.I)

# ★ 代数前面那一个字符，决定了这串数字到底是不是"代数"。
#   \d{1,3} 会从一串更长的数字里咬下尾巴，也会把小数、料号后缀当成代数。
#   下面三类反例全部来自本库实测：
#     "PARLANTE GRIP Código 139242"    → 咬到 "242"，其实是货号 139242（前一位是数字）
#     "IOWODO Relógio Unissex 1.91"    → 咬到 "91"，其实是 1.91 英寸屏（前一位是小数点）
#     "IA Aspire Lite Al16-53p-578y-1" → 咬到 "1"，其实是料号后缀（前一位是连字符）
#   这三类混进来会凭空造出"同系列更新款"，进而喂出一条不存在的 pre_launch_cut。
_NOT_GEN_PREFIX = re.compile(r"[\d.\-]$")

# 代数相差多少以内才算"下一代"。
GEN_MAX_STEP = 2


def _family_gen(model_name: str | None) -> tuple[str, int] | None:
    """把 "Galaxy S25 Ultra" 拆成 ("galaxy s|ultra", 25)。拆不出返回 None。

    后缀允许多个词（iPhone 17 Pro Max），否则 Apple 全系都抠不出代数。
    后缀进 key 是有意的：S25 Ultra 的对手是 S24 Ultra，不是 S24 标准版。
    """
    if not model_name:
        return None
    name = re.sub(r"\s+", " ", model_name.strip().lower())
    name = re.sub(r"\b(\d+\s*\+\s*\d+\s*gb?|\d+gb|\d+tb)\b", "", name).strip()
    m = _GEN.match(name)
    if not m:
        return None
    raw_fam = m.group("family")
    if _NOT_GEN_PREFIX.search(raw_fam):   # 这串数字是货号/尺寸/料号的一截，不是代数
        return None
    fam = raw_fam.strip(" -")
    if len(fam) < 3:                      # "17 pro" 这种没有家族名的抠了也没用
        return None
    suffix = re.sub(r"\s+", " ", (m.group("suffix") or "").strip())
    return f"{fam}|{suffix}", int(m.group("gen"))


def _is_successor(old_gen: int, new_gen: int) -> bool:
    """new_gen 是不是 old_gen 的**下一代**（而不是同代的高档位 / 尺寸 / 货号）。

    ★ 这一条是 pre_launch_cut 的命门，也是本文件唯一一处"数字大就是更新"的地方，
      所以必须写死判据：**数字大 ≠ 更新一代**。本库实测反例：
        Galaxy A07 / A17 / A27 / A37 / A57 —— 同一代的五个档位，A57 不是 A07 的后继
        Moto G15 与 Moto G56               —— 同理，G56 的前代是 G55 不是 G15
        Galaxy Watch 8 L320 / L325 / L330  —— 表壳货号
        Watch Series 11 42 / 46            —— 表壳毫米数
      不加这道闸，本库 28 个"有代际对比"的家族里一多半是假的，
      并且真的产出过一条 pre_launch_cut（Moto G15 ← Moto G56）。

    判据取两条，都成立才算下一代：
      ① 位数相同 —— A9 与 A11 位数不同，宁可判不出也不猜（漏判只是少一条信号，
         错判会写出一条"新品已上市"的假结论）
      ② 差值不超过 GEN_MAX_STEP —— 相邻代号通常 +1（S25→S26），
         偶有跳号（Pad 7→Pad 8、Honor Pad X7→X9）故留到 2
    """
    if new_gen <= old_gen:
        return False
    if len(str(old_gen)) != len(str(new_gen)):
        return False
    return new_gen - old_gen <= GEN_MAX_STEP


# ---------------------------------------------------------------- Agent

class StrategyAgent(BaseAgent):
    name = "strategy"
    role = "策略推断"
    description = ("从价格变动模式推断厂商意图：老品清库、新品上市前降价、大促前铺垫、"
                   "成本上涨提价、价格战。数字由 SQL 算，模型只判定'这组数字像哪种策略'")

    # ------------------------------------------------ 主流程

    def run(self, days: int = 14) -> dict:
        end = db.today()
        start = (date.fromisoformat(end) - timedelta(days=int(days))).isoformat()
        self.start(f"推断 {start}~{end} 的价格策略信号")

        health = self._data_health(start, end)
        self.log_step("数据体检", parsed=health, decision="ok",
                      reason="先确认这批数据能支撑哪些判断；支撑不了的信号类型直接关掉，"
                             "不让模型在空数据上编结论")

        if not health["窗口内官方观测天数"]:
            self.log_step("终止", decision="skipped",
                          reason=f"{start}~{end} 窗口内没有任何官方渠道价格观测，无从推断",
                          status="degraded")
            self.finish("ok", "窗口内无官方渠道观测数据，未产出信号", 0, 0)
            # 返回结构与正常路径保持一致：调用方按固定 key 取值，
            # 少一个 key 就是给上游埋一个 KeyError
            return {"candidates": 0, "signals": 0, "skipped": 0, "failed": 0,
                    "by_type": {}, "window": f"{start}~{end}",
                    "note": "窗口内无官方渠道价格观测"}

        cands = self._build_candidates(start, end, days, health)
        self.log_step("候选模式提取",
                      parsed={"候选数": len(cands),
                              "分布": _count_by(cands, "candidate_kind")},
                      decision="ok",
                      reason="全部来自 SQL 聚合，无模型参与；每个候选都带完整支撑数字")

        if not cands:
            self.finish("ok", "窗口内没有达到阈值的价格模式，未产出信号",
                        health["窗口内官方价格轨迹数"], 0)
            return {"candidates": 0, "signals": 0, "skipped": 0, "failed": 0,
                    "by_type": {}, "window": f"{start}~{end}",
                    "note": "无达阈值的价格模式"}

        signals, skipped, failed, answered = [], 0, 0, 0
        for cand in cands[:MAX_LLM_CALLS]:
            try:
                sig, ok = self._judge(cand, health)
            except Exception as e:                     # noqa: BLE001
                # 单条判定失败不能拖垮整个 Agent —— 记一笔继续下一条
                failed += 1
                log.warning("候选 %s 判定失败: %s", cand["ref"], str(e)[:150])
                self.log_step("判定异常", input_ref=cand["ref"], decision="error",
                              reason=str(e)[:300], status="degraded")
                continue
            answered += ok
            if sig is None:
                skipped += 1
                continue
            signals.append(sig)

        # ★ 一条都没答上来 = 模型根本没参与（没配 Key / API 挂了 / 全部返回乱码），
        #   而不是"模型看过之后认为今天没信号"。这两者对落库的正确动作完全相反：
        #   后者应该把当天旧信号清掉，前者绝不能 —— _persist 是先 DELETE 再写，
        #   空跑一次就会把上一轮的好结果清干净，还报 status=ok。
        #   实测（本次核查）：不配 Key 跑一遍，已落库的 2 条信号被清成 0。
        if cands and answered == 0:
            self.log_step("放弃落库", decision="skipped",
                          reason=f"{len(cands)} 个候选没有任何一条拿到模型判定"
                                 f"（未配 Key / 模型不可用 / 返回无法解析），"
                                 f"保留当天已有信号不动，避免空跑清库",
                          status="degraded")
            self.finish("degraded", "模型不可用，未产出信号，也未改动已有信号",
                        len(cands), 0)
            return {"candidates": len(cands), "signals": 0, "skipped": skipped,
                    "failed": failed, "by_type": {}, "window": f"{start}~{end}",
                    "note": "模型不可用，未落库；当天已有信号保持不变"}

        written = self._persist(end, signals)
        self.log_step("落库", parsed={"写入": written, "模型判为无信号": skipped,
                                      "判定异常": failed},
                      decision="ok",
                      reason="同日同 agent 的旧信号先清后写，保证重跑幂等")

        by_type = _count_by(signals, "signal_type")
        self.finish("ok",
                    f"{len(cands)} 个候选模式 → {written} 条策略信号（{by_type}）",
                    len(cands), written)
        return {"candidates": len(cands), "signals": written, "skipped": skipped,
                "failed": failed, "by_type": by_type, "window": f"{start}~{end}"}

    # ------------------------------------------------ 数据体检

    def _data_health(self, start: str, end: str) -> dict:
        """先看清楚数据能撑什么，撑不住的判断当场关掉并写进留痕。

        new_launch_premium 天生需要时间纵深：没有窗口前的历史，"这款以前没见过"
        就只是"我们以前没在抓"，据此报新品等于编。restore 同理，两个数据点看不出
        "先降后涨"（那一条按每条轨迹的观测天数单独把关）。
        与其让模型在 2 个点上想象一条曲线，不如如实说这批数据判不了。
        """
        depth = db.q1("""
            SELECT COUNT(DISTINCT obs_date) AS total_days,
                   MIN(obs_date) AS first_day, MAX(obs_date) AS last_day
            FROM price_obs WHERE audit_status <> 'rejected'
        """) or {}
        before = db.q1("""
            SELECT COUNT(DISTINCT obs_date) AS d FROM price_obs
            WHERE obs_date < ? AND audit_status <> 'rejected'
        """, (start,)) or {"d": 0}
        inwin = db.q1("""
            SELECT COUNT(DISTINCT po.obs_date) AS d, COUNT(*) AS rows
            FROM price_obs po JOIN channel ch ON ch.id = po.channel_id
            WHERE po.obs_date BETWEEN ? AND ? AND po.audit_status <> 'rejected'
              AND (ch.kind='brand_store' OR po.seller_kind IN ('self_operated','brand_official'))
        """, (start, end)) or {"d": 0, "rows": 0}
        moves = db.q1("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN prev_price > 0 AND
                        ABS(change_pct - (curr_price - prev_price) / prev_price * 100) <= ?
                       THEN 1 ELSE 0 END) AS sane
            FROM price_move WHERE move_date BETWEEN ? AND ?
        """, (MOVE_TOLERANCE_PP, start, end)) or {"total": 0, "sane": 0}

        tracks = db.q1("""
            SELECT COUNT(*) AS c FROM (
              SELECT 1 FROM price_obs po JOIN channel ch ON ch.id = po.channel_id
              WHERE po.obs_date BETWEEN ? AND ? AND po.sale_price > 0
                AND po.rival_product_id IS NOT NULL AND po.audit_status <> 'rejected'
                AND (ch.kind='brand_store' OR po.seller_kind IN ('self_operated','brand_official'))
              GROUP BY po.country_code, po.rival_product_id,
                       IFNULL(po.rom_gb,-1), IFNULL(po.ram_gb,-1), IFNULL(po.color,''),
                       po.currency
              HAVING COUNT(DISTINCT po.obs_date) >= 2)
        """, (start, end)) or {"c": 0}

        dropped = (moves.get("total") or 0) - (moves.get("sane") or 0)
        return {
            "全库观测天数": depth.get("total_days") or 0,
            "观测起止": f"{depth.get('first_day')}~{depth.get('last_day')}",
            "窗口前历史天数": before.get("d") or 0,
            "窗口内官方观测天数": inwin.get("d") or 0,
            "窗口内官方观测条数": inwin.get("rows") or 0,
            "窗口内官方价格轨迹数": tracks.get("c") or 0,
            "窗口内price_move": moves.get("total") or 0,
            "剔除的不自洽price_move": dropped,
            # 窗口前至少 3 天历史，才敢说某款是"新出现的"
            "可判新品高定价": (before.get("d") or 0) >= 3,
            "剔除说明": ("price_move 中 change_pct 与 (curr-prev)/prev 对不上的行已剔除，"
                         "这类行的前后价不属于同一次变动，喂给模型会推出不存在的策略"),
        }

    # ------------------------------------------------ 候选提取（纯 SQL）

    def _build_candidates(self, start: str, end: str, days: int, health: dict) -> list[dict]:
        tracks = db.q(SQL_TRACKS, (start, end, POLLUTED_SPREAD, start))
        moves = {(m["country_code"], m["rival_product_id"], m["sku_key"]): m
                 for m in db.q(SQL_MOVE_BY_PRODUCT, (start, end, MOVE_TOLERANCE_PP))}
        scale = {(s["country_code"], s["brand_id"], s["category_code"], s["direction"]): s
                 for s in db.q(SQL_BRAND_SCALE, (start, end, MOVE_TOLERANCE_PP))}

        # 同品牌同品类里是否已经出现更新一代 —— pre_launch_cut 的必要前提。
        # 只在本次窗口的观测集合内比，不去查外部资料，查不到就是没有。
        newer_gen = self._newer_generation_map(tracks)
        pct_rank = _price_percentiles(tracks)
        coverage = {(c["country_code"], c["category_code"], c["brand_id"]): c
                    for c in db.q(SQL_PRE_COVERAGE, (start, start))}

        cands: list[dict] = []
        launches: list[dict] = []
        for t in tracks:
            t["price_pct_rank"] = pct_rank.get(_track_key(t))
            if t["d0"] < t["d1"]:            # 有变动才谈得上"策略动作"
                mv = moves.get((t["country_code"], t["rival_product_id"], t["sku_key"])) or {}
                cands.extend(self._product_candidates(t, mv, scale, newer_gen, days, health))
            launches.extend(self._new_launch_candidates(t, coverage, days, health))

        # 新品候选按价格分位取前若干条：真正的新品高定价是稀有事件，
        # 一次跑出几十条只说明闸门没关严，而不是市场上真有几十款新旗舰。
        launches.sort(key=lambda c: -c["weight"])
        cands.extend(launches[:10])

        moved = [t for t in tracks if t["d0"] < t["d1"]]
        cands.extend(self._brand_candidates(start, end, days, moved))
        cands.extend(self._war_candidates(start, end, days))

        # 影响面 × 幅度 排序：模型调用有上限，先看最像真事的
        cands.sort(key=lambda c: -c["weight"])
        return cands

    def _newer_generation_map(self, tracks: list[dict]) -> dict[tuple, list[str]]:
        """(国家, 品牌, 品类, 家族) → 该窗口内观测到的更高代型号名列表。"""
        seen: dict[tuple, dict[int, str]] = {}
        for t in tracks:
            fg = _family_gen(t.get("model_name"))
            if not fg:
                continue
            fam, gen = fg
            key = (t["country_code"], t["brand_id"], t["category_code"], fam)
            seen.setdefault(key, {})[gen] = t["model_name"]
        return {k: v for k, v in seen.items() if len(v) >= 2}  # 只留有代际对比的

    def _product_candidates(self, t: dict, mv: dict, scale: dict, newer_gen: dict,
                            days: int, health: dict) -> list[dict]:
        cum = t["cum_pct"]
        down_moves = mv.get("down_moves") or 0
        up_moves = mv.get("up_moves") or 0
        name_broken = bool(_BROKEN_NAME.search(t.get("model_name") or ""))
        bkey = (t["country_code"], t["brand_id"], t["category_code"])
        peers_down = (scale.get(bkey + ("down",)) or {}).get("prods") or 0
        peers_up = (scale.get(bkey + ("up",)) or {}).get("prods") or 0
        peers_up_chans = (scale.get(bkey + ("up",)) or {}).get("chans") or 0

        # 同系列更新款（只在窗口观测里找，找不到就 None）
        newer = None
        fg = _family_gen(t.get("model_name"))
        if fg:
            gens = newer_gen.get((t["country_code"], t["brand_id"], t["category_code"], fg[0]))
            if gens:
                # 取**最近**的下一代，不是随便一个更大的号：dict 的插入顺序不是代数顺序，
                # 原来的 higher[0] 会随扫描顺序给出 S26 或 S28，结论跟着数据顺序漂。
                nxt = sorted(g for g in gens if _is_successor(fg[1], g))
                newer = gens[nxt[0]] if nxt else None

        base_facts = {
            "国家": t["country_code"], "品牌": t["brand_name"],
            "品类": t["category_code"], "型号": t.get("model_name") or "(未归一化)",
            "SKU(容量|内存|颜色)": t["sku_key"],
            "币种": t["currency"],
            "窗口首日": t["d0"], "窗口首日价": t["p0"],
            "窗口末日": t["d1"], "窗口末日价": t["p1"],
            "累计涨跌幅%": cum,
            "窗口内最高价": t["hi"], "窗口内最低价": t["lo"],
            # 只是最高价与最低价的落差，不含先后顺序：n_days=2 时它就是累计幅度的镜像，
            # 单独看会把"涨了 20%"读成"跌了 20%"。标题里写清楚，别让模型误读。
            "最高最低价落差%(不含先后)": t["hi_lo_gap_pct"],
            "有观测的天数": t["n_days"],
            "官方渠道档位": {2: "品牌官网/官方旗舰", 1: "零售商自营"}.get(t["top_tier"], "未知"),
            "参与的官方渠道数": t["n_channels"], "渠道": t["chan_names"],
            "窗口内官方降价次数": down_moves,
            "窗口内官方涨价次数": up_moves,
            "同品牌同品类同期降价的产品数": peers_down,
            "同品牌同品类同期涨价的产品数": peers_up,
            "同系列更新款(窗口内观测到)": newer or "未观测到",
            "划线价首日": t["l0"], "划线价末日": t["l1"],
            "在同国同品类同币种的价格分位%": t.get("price_pct_rank"),
        }
        if t["polluted"]:
            base_facts["数据告警"] = ("同日同SKU内部价差超过50%，该产品的归一化分桶可能混入了"
                                      "不同配置，价格轨迹不可全信")
        if name_broken:
            base_facts["数据告警2"] = "型号名疑似采集噪声（含'免运费/App'等角标文本），产品身份存疑"

        out = []
        # ---- 降价候选
        # ★ "峰谷落差大"只有在末价确实低于首价时才是降价证据。
        #   少了 p1 < p0 这一条，一个 23999→29999 的**涨价**轨迹会因为
        #   落差 -20% 被当成降价候选送进模型（实跑时真的发生了）。
        dropped = t["p1"] < t["p0"]
        if cum <= CUM_DROP_PCT or down_moves >= 2 or (dropped and t["hi_lo_gap_pct"] <= BIG_DROP_PCT):
            allowed = ["clearance", "promo_prep"]
            if newer:
                # 没在数据里看见更新一代，就没有"给新品腾位置"这回事可谈。
                # 允许它反而是在邀请模型脑补一场没观测到的发布会。
                allowed.append("pre_launch_cut")
            if t["n_channels"] >= 2 or peers_down >= CLUSTER_MIN_PRODUCTS:
                allowed.append("price_war")  # 单渠道单品降价不构成"品牌互相跟降"
            if t["n_days"] >= 3:             # 先降后涨至少要 3 个点才看得出来
                allowed.append("restore")
            out.append(self._mk_cand(
                "产品降价轨迹", t, base_facts, allowed,
                weight=abs(cum) * (2.0 if down_moves >= 2 else 1.0)
                       * (1.4 if t["top_tier"] == 2 else 1.0)
                       * (0.4 if (t["polluted"] or name_broken) else 1.0),
                days=days))
        # ---- 涨价候选
        if cum >= CUM_RISE_PCT:
            allowed = []
            # cost_hike 的定义里就写着"多个渠道同向涨价、不是个别卖家"。
            # 一款孤零零在一家渠道涨价，无法和渠道自己调价区分 —— 规则上就不给这个选项，
            # 否则模型会顺着"涨价"两个字判成本上涨，还写出"多渠道同向"这种数据里没有的话。
            if t["n_channels"] >= 2 or peers_up >= CLUSTER_MIN_PRODUCTS or peers_up_chans >= 2:
                allowed.append("cost_hike")
            if t["n_days"] >= 3:
                allowed.append("restore")
            if health["可判新品高定价"]:
                allowed.append("new_launch_premium")
            else:
                base_facts["能力说明"] = (
                    f"窗口前只有 {health['窗口前历史天数']} 天历史，无法判断这款是不是"
                    f"新上架，因此本次不允许判 new_launch_premium")
            if allowed:                      # 一个选项都不剩就别浪费一次模型调用
                out.append(self._mk_cand(
                    "产品涨价轨迹", t, base_facts, allowed,
                    weight=abs(cum) * (1.4 if t["top_tier"] == 2 else 1.0)
                           * (0.4 if (t["polluted"] or name_broken) else 1.0),
                    days=days))
        # ---- 回价候选（先降后涨）
        # ★ 促销结束回价的累计幅度≈0：降 20% 再涨回去，cum_pct 就是 0。
        #   上面两个触发条件（cum<=-8 / cum>=+5）天生抓不到它，restore 这一类
        #   会永远出不来。必须按"窗口内探过底又回到高位"单列一条。
        if (t["n_days"] >= 3 and down_moves >= 1 and up_moves >= 1
                and t["lo"] < t["hi"] * 0.95
                and t["p1"] >= t["hi"] * 0.97 and t["p1"] > t["lo"] * 1.03):
            allowed = ["restore"]
            if t["n_channels"] >= 2 or peers_up >= CLUSTER_MIN_PRODUCTS:
                allowed.append("cost_hike")
            facts = dict(base_facts)
            facts["形态"] = (f"窗口内先跌到 {t['lo']} 再回到 {t['p1']}，"
                             f"最高 {t['hi']}；降价 {down_moves} 次、涨价 {up_moves} 次")
            out.append(self._mk_cand(
                "产品回价轨迹", t, facts, allowed,
                weight=abs(t["hi_lo_gap_pct"]) * (1.4 if t["top_tier"] == 2 else 1.0)
                       * (0.4 if (t["polluted"] or name_broken) else 1.0),
                days=days))
        return out

    def _new_launch_candidates(self, t: dict, coverage: dict, days: int,
                               health: dict) -> list[dict]:
        """新品高定价入场。

        ★ 这一类和其它六类的形态完全不同：新品**没有价格变动**，
          它的特征是"窗口前从没见过 + 一上架就定在品类价格带上沿"。
          只看变动的话这一类永远出不来，所以单开一条通道。

        ★ 但"没见过"必须先排除采集覆盖度：只有当"这个国家+这个品类+这个品牌"
          在窗口前就被持续观测过，"没见过这一款"才等于"这一款是新的"。
          否则报出来的全是爬虫刚扩品类造成的假新品。
        """
        if not health["可判新品高定价"] or t["seen_before"]:
            return []
        cov = coverage.get((t["country_code"], t["category_code"], t["brand_id"]))
        if not cov or (cov["pre_days"] or 0) < 3 or (cov["pre_prods"] or 0) < 3:
            return []                        # 这块货架窗口前没在看，"新品"无从判断
        rank = t.get("price_pct_rank")
        if rank is None or rank < 75:        # 不在价格带上沿就不叫"高定价入场"
            return []
        facts = {
            "国家": t["country_code"], "品牌": t["brand_name"],
            "品类": t["category_code"], "型号": t.get("model_name") or "(未归一化)",
            "SKU(容量|内存|颜色)": t["sku_key"], "币种": t["currency"],
            "首次观测日": t["d0"], "首次观测价": t["p0"],
            "最新观测日": t["d1"], "最新价": t["p1"],
            "窗口前是否在该国被观测过": "否（窗口前 "
                                        f"{health['窗口前历史天数']} 天历史里没有这款）",
            "该国该品类该品牌窗口前的采集覆盖": (
                f"已观测 {cov['pre_days']} 天、{cov['pre_prods']} 款 —— "
                f"这块货架窗口前一直在看，所以'没见过这一款'可以当作新上市的依据"),
            "在同国同品类同币种的价格分位%": rank,
            "官方渠道档位": {2: "品牌官网/官方旗舰", 1: "零售商自营"}.get(t["top_tier"], "未知"),
            "参与的官方渠道数": t["n_channels"], "渠道": t["chan_names"],
            "有观测的天数": t["n_days"],
            "划线价": t["l1"],
        }
        if t["polluted"]:
            facts["数据告警"] = "同日同SKU内部价差超过50%，归一化分桶可能混了不同配置"
        if _BROKEN_NAME.search(t.get("model_name") or ""):
            facts["数据告警2"] = "型号名疑似采集噪声，产品身份存疑"
        return [self._mk_cand("新品入场定价", t, facts, ["new_launch_premium"],
                              weight=rank / 10.0, days=days)]

    def _mk_cand(self, kind: str, t: dict, facts: dict, allowed: list[str],
                 *, weight: float, days: int) -> dict:
        return {
            "candidate_kind": kind,
            "ref": f"{t['country_code']}/{t['brand_name']}/{t.get('model_name')}/{t['sku_key']}",
            "country_code": t["country_code"], "brand_id": t["brand_id"],
            "category_code": t["category_code"],
            "rival_product_id": t["rival_product_id"],
            "facts": facts, "allowed": allowed, "weight": weight,
            "promo": _next_promo(t["country_code"], date.fromisoformat(t["d1"])),
            "days": days,
        }

    def _brand_candidates(self, start: str, end: str, days: int,
                          tracks: list[dict]) -> list[dict]:
        """品牌×国家×品类的齐动簇：多个 SKU 同时动才谈得上"策略"。"""
        # 划线价被抬高的 SKU 数（大促前铺垫的典型手法：抬划线价做出价差）
        list_up: dict[tuple, int] = {}
        for t in tracks:
            if t["l0"] and t["l1"] and t["l1"] > t["l0"] * 1.02 and t["p1"] <= t["p0"]:
                k = (t["country_code"], t["brand_id"], t["category_code"])
                list_up[k] = list_up.get(k, 0) + 1

        rows = db.q(SQL_BRAND_CLUSTER,
                    (start, end, MOVE_TOLERANCE_PP, CLUSTER_MIN_PRODUCTS))
        out = []
        for r in rows:
            is_down = r["direction"] == "down"
            facts = {
                "国家": r["country_code"], "品牌": r["brand_name"],
                "品类": r["category_code"],
                "方向": "降价" if is_down else "涨价",
                "同向变动的产品数": r["prods"], "变动条数": r["moves"],
                "涉及官方渠道数": r["chans"], "渠道": r["chan_names"],
                "含品牌官网/官方旗舰": "是" if r["has_brand_store"] else "否",
                "平均幅度%": r["avg_pct"], "最大降幅%": r["min_pct"],
                "最大涨幅%": r["max_pct"],
                "起止": f"{r['d0']}~{r['d1']}",
                "划线价被抬高的SKU数": list_up.get(
                    (r["country_code"], r["brand_id"], r["category_code"]), 0),
            }
            if r["chans"] == 1:
                facts["判别提示"] = ("全部变动只发生在 1 个渠道 —— 更可能是这家渠道自己的"
                                     "促销/调价，而不是厂商的全国性动作")
            allowed = (["promo_prep", "clearance", "price_war", "pre_launch_cut"]
                       if is_down else ["cost_hike", "restore"])
            out.append({
                "candidate_kind": f"品牌齐{'降' if is_down else '涨'}",
                "ref": f"{r['country_code']}/{r['brand_name']}/{r['category_code']}",
                "country_code": r["country_code"], "brand_id": r["brand_id"],
                "category_code": r["category_code"], "rival_product_id": None,
                "facts": facts, "allowed": allowed,
                "weight": r["prods"] * abs(r["avg_pct"] or 1)
                          * (1.5 if r["has_brand_store"] else 1.0)
                          * (0.6 if r["chans"] == 1 else 1.0),
                "promo": _next_promo(r["country_code"], date.fromisoformat(r["d1"])),
                "days": days,
            })
        return out

    def _war_candidates(self, start: str, end: str, days: int) -> list[dict]:
        """国家×品类的多品牌同期降价 —— 价格战的唯一候选来源。"""
        out = []
        for r in db.q(SQL_CATEGORY_WAR, (start, end, MOVE_TOLERANCE_PP, WAR_MIN_BRANDS)):
            facts = {
                "国家": r["country_code"], "品类": r["category_code"],
                "同期降价的品牌数": r["brands"], "品牌": r["brand_names"],
                "降价产品数": r["prods"],
                "涉及官方渠道数": r["chans"], "渠道": r["chan_names"],
                "平均降幅%": r["avg_pct"], "最大降幅%": r["min_pct"],
                "起止": f"{r['d0']}~{r['d1']}",
            }
            if r["chans"] == 1:
                facts["判别提示"] = (
                    "★ 多品牌同时降价，但全部集中在 1 个渠道内 —— 这是该渠道的促销活动，"
                    "不是品牌之间互相跟降。判 price_war 需要多个渠道同时出现跟降。")
            out.append({
                "candidate_kind": "品类多品牌齐降",
                "ref": f"{r['country_code']}/{r['category_code']}/多品牌",
                "country_code": r["country_code"], "brand_id": None,
                "category_code": r["category_code"], "rival_product_id": None,
                "facts": facts, "allowed": ["price_war", "promo_prep"],
                "weight": r["brands"] * r["prods"] * (0.5 if r["chans"] == 1 else 2.0),
                "promo": _next_promo(r["country_code"], date.fromisoformat(r["d1"])),
                "days": days,
            })
        return out

    # ------------------------------------------------ 模型判定

    _SYSTEM = (
        "你是Acme拉美竞品情报中枢的价格策略分析师。\n"
        "你的唯一任务：看一组**已经算好的价格数字**，判断它最像哪一种价格策略。\n\n"
        "铁律（违反即作废）：\n"
        "1. 只能使用我给你的数字。禁止引入任何我没给的数字、日期、销量、发布会信息。\n"
        "2. 不同国家币种不同，绝不比较绝对金额，只看百分比。\n"
        "3. 变动只发生在 1 个渠道 = 大概率是那家渠道自己的促销，不是厂商动作，"
        "更不能判成价格战。\n"
        "4. 数字撑不起任何一种策略，就返回 signal_type=\"none\"。宁可不报，不许硬凑。\n"
        "5. 数据告警字段出现时，必须调低 confidence。\n"
        "6. 型号名以我给的原文为准，即使它看起来是错的也照抄，不许替我'修正'成"
        "你认为对的型号 —— 那会让结论对不上库里的产品。\n"
        "只输出 JSON，不要解释文字。"
    )

    def _judge(self, cand: dict, health: dict) -> tuple[dict | None, bool]:
        """返回 (信号 或 None, 模型是否真的答了话)。

        ★ 第二个返回值不是冗余：`None` 有两种完全不同的来源 ——
          "模型看过数字后认为没信号" 与 "模型压根没答话（没配 Key / API 挂了 / 返回乱码）"。
          落库那一步对这两者的正确动作是相反的（见 _persist 的调用处），
          所以必须在这里就把它们分开，不能都压成 None。
        """
        defs = "\n".join(f"- {k}：{v}" for k, v in SIGNAL_TYPES.items()
                         if k in cand["allowed"])
        facts = "\n".join(f"- {k}：{v}" for k, v in cand["facts"].items())
        promo = ""
        if cand["promo"]:
            promo = (f"\n【大促上下文（近似日历，不是观测数据，不许当事实引用）】\n"
                     f"- 最近的大促：{cand['promo']['名称']}，"
                     f"距末次变动约 {cand['promo']['距今天数']} 天\n")

        prompt = (
            f"【候选模式】{cand['candidate_kind']}（观察窗口 {cand['days']} 天）\n\n"
            f"【全部支撑数字】\n{facts}\n{promo}\n"
            f"【本次允许选择的策略类型】\n{defs}\n"
            f"- none：以上都不像\n\n"
            f"请判断这组数字最像哪一种，并输出 JSON：\n"
            "{\n"
            '  "signal_type": "上面列出的类型之一，或 none",\n'
            '  "confidence": 0.0~1.0,\n'
            '  "summary_zh": "一句话结论，必须点出关键数字，不超过60字",\n'
            '  "impact_zh": "对Acme在该国该品类意味着什么，一到两句",\n'
            '  "suggested_action": "一句具体可执行的建议动作",\n'
            '  "why": "你依据了哪几个数字，一句话"\n'
            "}"
        )

        parsed = self.ask_json(f"判定:{cand['candidate_kind']}", prompt,
                               system=self._SYSTEM, input_ref=cand["ref"], default=None)
        d = as_dict(parsed)                       # ★ 绝不直接信任模型返回的结构
        if not d:
            return None, False                    # 没答话 / 答的东西没法用

        st = str(d.get("signal_type") or "").strip().lower()
        if st in ("none", "", "null"):
            return None, True                     # 答了，判的是"没信号"
        if st not in cand["allowed"]:
            # 模型选了本次不允许的类型（通常是它想判 new_launch_premium 但数据撑不住）。
            # 不做"猜他想说什么"的纠正，直接丢弃 —— 纠正等于替模型编结论。
            self.log_step("判定作废", input_ref=cand["ref"], decision="rejected",
                          reason=f"模型返回类型 {st!r} 不在本候选允许的 {cand['allowed']} 内",
                          status="degraded")
            return None, True

        summary = str(d.get("summary_zh") or "").strip()
        if not summary:
            return None, True                      # 没有结论就不落库，不自动补一句空话

        conf = as_float(d.get("confidence"))
        conf = 0.5 if conf is None else max(0.0, min(1.0, conf))
        conf = min(conf, self._confidence_cap(cand, health))

        return {
            "country_code": cand["country_code"], "brand_id": cand["brand_id"],
            "category_code": cand["category_code"],
            "rival_product_id": cand["rival_product_id"],
            "signal_type": st, "confidence": round(conf, 2),
            "summary_zh": summary[:300],
            "impact_zh": str(d.get("impact_zh") or "").strip()[:500],
            "suggested_action": str(d.get("suggested_action") or "").strip()[:300],
            "evidence": {
                "候选模式": cand["candidate_kind"],
                "支撑数字": cand["facts"],
                "模型依据": str(d.get("why") or "")[:300],
                "模型原始置信度": as_float(d.get("confidence")),
                "置信度上限": self._confidence_cap(cand, health),
                "数据窗口": f"{cand['days']}天",
                "大促上下文": cand["promo"],
                "口径": "仅统计官方渠道（品牌官网/官方旗舰/零售商自营），"
                        "第三方卖家挂牌已剔除；跨国仅比百分比",
            },
        }, True

    @staticmethod
    def _confidence_cap(cand: dict, health: dict) -> float:
        """置信度封顶：模型对着 2 个数据点也能说得斩钉截铁，得由规则按住。

        每一条封顶都对应一个"这个结论有多容易是假的"的具体理由。
        """
        cap = 1.0
        f = cand["facts"]
        if f.get("数据告警") or f.get("数据告警2"):
            cap = min(cap, 0.45)                          # 分桶脏 / 型号名不可信
        if f.get("涉及官方渠道数") == 1 or f.get("参与的官方渠道数") == 1:
            cap = min(cap, 0.65)                          # 单渠道，分不清厂商还是渠道
        if (f.get("有观测的天数") or 99) <= 2:
            cap = min(cap, 0.70)                          # 只有两个点，谈不上"趋势"
        if health["窗口内官方观测天数"] <= 2:
            cap = min(cap, 0.75)                          # 全窗口纵深不足
        if f.get("官方渠道档位") == "零售商自营":
            cap = min(cap, 0.80)                          # 自营价不等于厂商指导价
        return round(cap, 2)

    # ------------------------------------------------ 落库

    def _persist(self, signal_date: str, signals: list[dict]) -> int:
        n = 0
        with db.tx() as conn:
            # 重跑幂等：同一天同一 agent 的旧信号先清掉。
            # strategy_signal 没有唯一键，不清会越跑越多份重复结论。
            conn.execute("DELETE FROM strategy_signal WHERE signal_date=? AND agent=?",
                         (signal_date, self.name))
            for s in signals:
                conn.execute("""
                    INSERT INTO strategy_signal(signal_date,country_code,brand_id,
                        category_code,rival_product_id,signal_type,confidence,
                        summary_zh,evidence,impact_zh,suggested_action,agent)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """, (signal_date, s["country_code"], s["brand_id"], s["category_code"],
                      s["rival_product_id"], s["signal_type"], s["confidence"],
                      s["summary_zh"],
                      json.dumps(s["evidence"], ensure_ascii=False),
                      s["impact_zh"], s["suggested_action"], self.name))
                n += 1
        return n


def _track_key(t: dict) -> tuple:
    return (t["country_code"], t["rival_product_id"], t["sku_key"], t["currency"])


def _price_percentiles(tracks: list[dict]) -> dict[tuple, float]:
    """每条轨迹的最新价在「同国 + 同品类 + 同币种」里的分位。

    ★ 分组必须带币种：CLP 和 PEN 的数字放一起排序毫无意义，
      会得出"智利手机全部处在价格带顶端"这种纯粹由汇率造成的假结论。
      这也是全文件唯一一处对绝对金额排序的地方，所以口径必须锁死。
    """
    cohorts: dict[tuple, list[float]] = {}
    for t in tracks:
        cohorts.setdefault((t["country_code"], t["category_code"], t["currency"]),
                           []).append(t["p1"])
    for prices in cohorts.values():
        prices.sort()
    out = {}
    for t in tracks:
        prices = cohorts[(t["country_code"], t["category_code"], t["currency"])]
        if len(prices) < 8:                 # 样本太少，分位数没有意义
            continue
        below = sum(1 for p in prices if p < t["p1"])
        out[_track_key(t)] = round(below / len(prices) * 100, 1)
    return out


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
