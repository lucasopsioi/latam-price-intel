# -*- coding: utf-8 -*-
"""周报汇总 Agent —— 把一周散落的事实汇成一份 销售团队 能直接读的中文周报。

用户原话：
  「还要有一个 Agent 来汇总，出周报」
  「我要知道每个国家每个品类，在每一天、每一周、每个月有什么样的竞争动态」

四条设计原则，每一条都是为了让这份报告**能被业务信任**：

  1. **数字全部由 SQL 算，模型只负责措辞**。
     让模型自己去数"降价几个"，它一定会数错；而错一个数字，
     整份报告在 销售团队 眼里就作废了（比没有报告更糟：会照着错数字定价）。
     模型拿到的是算好的结构化事实，职责只有一个 —— 把它们讲成人话。
     提示词里明写"不得新增或改写任何数字"。

  2. **分段生成**。一次让模型写完整篇（概览＋六国＋信号＋建议）必然超长，
     后半段质量断崖下跌且常被 max_tokens 截断。拆成四类调用后，
     单段失败只丢那一段，其余照常出稿 —— 周报是每周固定物料，
     不能因为一次 API 抖动就整份没有。

  3. **缺口必须写进正文**。某个渠道本周没抓到，报告里必须点名说"没抓到"。
     否则读者会把"我们没数据"读成"友商没动静" —— 这是情报产品最贵的一种错，
     它会让人放心地什么都不做。

  4. **无 Key / 模型全挂时仍然出报告**。所有 LLM 段落都有纯事实兜底文案，
     退化成一份"没有点评的数据周报"，而不是一个异常堆栈。

比价纪律沿用 pricemove.py：
  · 不同币种的绝对价格永远不放在一起比大小，跨国只比百分比；
  · 官方渠道（品牌官网/渠道自营）与第三方分开统计 —— 前者是厂商定价动作，
    后者可能只是某个卖家甩货。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import date, timedelta

from .. import db
from .base import BaseAgent

log = logging.getLogger("weekly")

# 注：本 Agent 不做任何 JSON 结构化调用，因此没有 import as_dicts/as_dict。
# 唯一需要结构化输出的地方是「本周要点」，而它已被改成纯规则生成 ——
# 原因见 _extract_highlights 的注释（模型把涨价改写成了降价）。
# 一旦以后新增 JSON 调用，务必从 .llm 引入 as_dicts/as_dict 归一化，不许直接信任结构。

TOP_MOVES = 6          # 每个国家最多列几条"幅度最大"的变动
TOP_NEW = 6            # 每个国家最多列几个新出现的产品
NEAR_BAND_PCT = 12.0   # |价差| ≤ 12% 视为"贴身对位"，会咬到我方价位带
MOVE_ALERT_PCT = 3.0   # 对位竞品动价超过这个幅度才值得写进建议（以下是噪声）

# 模型的角色与铁律。每一条都对应一种已知的翻车方式，删一条就会翻回去。
PROSE_SYSTEM = (
    "你是Acme拉美 销售团队的竞争情报分析师。读者是要据此做定价、铺货、"
    "促销排期决策的一线业务人员，他们没时间读废话。\n"
    "铁律：\n"
    "1. 只能使用我给你的数字，一个字都不许改，更不许自己造新数字、算新比例；\n"
    "2. 我没给的事实就是没有 —— 直接说「本周未观测到」，"
    "严禁用行业常识/记忆补全任何品牌动作、发布会、促销活动；\n"
    "3. 各国币种不同，绝对价格跨国不可比，跨国比较只谈百分比；\n"
    # ★ 实测：is_official 的口径是「品牌官网 or 渠道自营」（见 pricemove.py），
    #   而这一周 63 条「官方」变动全部来自 Falabella Chile 自营 —— 那是零售商
    #   自己在调价，不是Acme/三星在调价。模型据此写出过"厂商正在积极调整定价策略"，
    #   把零售商促销升格成了厂商战略动作，会直接误导定价决策。
    "4. 「官方渠道」= 品牌官网 或 渠道自营（含零售商自营），"
    "「第三方」= 平台上的第三方卖家。前者比后者可信得多，但**渠道自营降价"
    "只能说成「该渠道在调价」，不得断言是厂商/品牌的定价动作** —— "
    "只有品牌官网（brand_store）的价格才代表厂商本身；两者绝不可混为一谈；\n"
    "5. 中文，直给结论，不写客套话，不写小标题（标题由程序生成），"
    "不要复述我给你的表格；\n"
    # ★ 实测：模型写出过"根据铁律4，第三方促销参考价值较低"这种句子，
    #   把提示词内部编号原样甩进了给业务看的正文里。必须显式禁止。
    "6. 正文是给业务读的，绝不能提及本段指令本身 —— 不许出现「铁律」「规则N」"
    "「按要求」「根据上述指令」这类字眼，也不许解释你为什么这么写。"
)


# ---------------------------------------------------------------- 小工具

def _week_bounds(week_start: str | None) -> tuple[str, str]:
    """周一为一周之首，返回 (周一, 周日)。

    传入任意一天都会被吸附到所在周的周一 —— 周报常在周内随手重跑，
    吸附后 UNIQUE(week_start, scope) 才能稳定命中同一行做覆盖更新，
    否则周二跑一次、周四跑一次会留下两份互相矛盾的"本周周报"。
    """
    d = date.today()
    if week_start:
        try:
            d = date.fromisoformat(str(week_start)[:10])
        except (ValueError, TypeError):
            log.warning("week_start 无法解析，回退到今天: %r", week_start)
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


# ══════════════════ 报告周期（用户 2026-08-18 指定的口径）══════════════════
#
#   自然周：周一 00:00 → 下周一（右端开区间），每周一期「周报」。
#
# 历史：2026-08-18 用户定的是 5 日/20 日双段制（周报+双周报，全年 26 期）；
# 2026-08-27 改为**每周一次**，双周报废除。kind 恒为 weekly，
# PERIOD_ZH 里保留 biweekly 词条只为老报告的展示兼容。
# ★ 右端开区间：周一属于新一期的第一天，不属于上一期的最后一天 ——
#   边界重叠会让同一天的变动被两期都算一遍。
PERIOD_ZH = {"weekly": "周报", "biweekly": "双周报"}

# 配件标题词（葡语/西语，一律带 para/de 等介词形态防误杀）：
# 上游 product_kind 分类漏掉的壳/膜/键盘/笔/充电器，ASP 口径必须挡在 SQL 层
ACCESSORY_WORDS = (
    "capa para", "capa de", "capinha", "película", "pelicula para",
    "funda para", "funda de", "mica para", "estuche para", "carcasa para",
    "protector de", "cargador para", "carregador para", "correa para",
    "pulseira para", "teclado para", "case para", "cover para",
    "caneta para", "lápiz para", "stylus")
_ACC_SQL = "".join(
    f"\n              AND instr(lower(po.title), '{w}') = 0"
    for w in ACCESSORY_WORDS)


def _period_bounds(anchor: str | None = None) -> tuple[str, str, str, str]:
    """任意一天 → 它所属的报告周（周一起算的自然周）。

    返回 (kind, start, end_exclusive, end_inclusive)。
    ★ 传入期内任意一天都吸附到同一个 start —— 期内随手重跑时，
      UNIQUE(week_start, scope) 才能稳定命中同一行做覆盖更新，
      否则周二跑一次、周五跑一次会留下两份互相矛盾的"本期报告"。
    """
    d = date.today()
    if anchor:
        try:
            d = date.fromisoformat(str(anchor)[:10])
        except (ValueError, TypeError):
            log.warning("period anchor 无法解析，回退到今天: %r", anchor)

    start = d - timedelta(days=d.weekday())        # 本周一
    end = start + timedelta(days=7)                # 下周一（不含）
    return ("weekly", start.isoformat(), end.isoformat(),
            (end - timedelta(days=1)).isoformat())


def _parse_scope(scope: str) -> tuple[str, str]:
    """scope: all | 国家码 | 国家码:品类 → (country_code, category_code)"""
    s = (scope or "all").strip()
    if not s or s.lower() == "all":
        return "", ""
    if ":" in s:
        cc, cat = s.split(":", 1)
        # ★ ":phone" 这种写法 = **所有国家 + 单一产业**（用户要的"自行选择产业"）。
        #   国家段留空不是笔误，是刻意的：周报要覆盖全部国家。
        return cc.strip().upper(), cat.strip().lower()
    return s.upper(), ""


# ══════════════════ 简报模式（用户 2026-08-18 指定：不超过 500 字）══════════════════
#
# ★★ 这一段的难点不是"写得短"，是**选得准**。本期有 242 条价格变动、
#   32 条策略信号、15 个对位威胁 —— 500 字里能放的不到 3%。
#   选错了比不写更糟：读者会以为"最重要的就这些"。
#
# 打分口径（每一项都有业务理由，不是拍脑袋的权重）：
#   · 幅度         —— 基础分，10% 的动作比 3% 的值钱。
#   · 打到我方对位  —— ×2.5。友商随便降价与"降到我方对位机型身上"是两回事，
#                      后者直接威胁我们的价盘，是 销售团队 唯一必须当期知道的事。
#   · 官方渠道      —— ×1.5。品牌官网/渠道自营调价是**厂商定价动作**；
#                      第三方卖家降价可能只是某个店在甩货，不代表价盘变了。
#   · 幅度过大惩罚  —— 打 0.3 折。分期月供被当成整机价、变体串档都会造出
#                      50%+ 的假变动，让它们占据头条最浪费版面。
# ★ 2026-08-18 用户修订口径：「字数限制也没有那么严格，就是要一份详尽的 15 天周报」。
#   ⇒ 不再砍字数，但**「一定只选最重点的」这条没有撤销** ——
#   区别在于：重点的仍然排在最前面、仍然按重要度打分，只是后面把细节铺开，
#   而不是把细节丢掉。摘要仍然短（一眼能读完），详尽体现在分国分述与表格上。
MAX_SUMMARY_CHARS = 400        # 只约束**开头摘要**，正文其余部分不设限
HEADLINE_N = 6                 # 摘要里点名几条
TABLE_N = 20                   # 重点变化表的行数
CHART_N = 10                   # 图上最多几款
COUNTRY_MOVES_N = 8            # 每个国家分述里列几条

# 各品类"一次调价"的合理幅度上限；超过多半是脏数据而不是真降价。
# 低单价品类（音频/穿戴）本身波动就大，阈值放宽。
_SANE_MOVE = {"phone": 45.0, "tablet": 45.0, "pc": 40.0,
              "wearable": 60.0, "audio": 65.0}
# ★ 主力产品口径（用户 2026-08-20：「上市 2-3 年的就不要放了，最近一年的更有意义」）。
#   实测近 30 天有变动的 178 个产品：42 个 ≤13 个月（主力）、46 个确认 >26 个月（老品）、
#   67 个**无上市日期** —— 而无日期的里有 Galaxy Buds Core 这种当红品
#   （gsmarena 不覆盖音频/穿戴）。所以：确认老品剔除、主力加权、无日期保留不加权 ——
#   按"无日期=剔除"会把音频、穿戴两个板块误杀成空。
MAINLINE_DAYS = 400        # ≤13 个月 = 主力，加权
STALE_DAYS = 800           # >26 个月 = 确认老品，直接不进榜
MAINLINE_BOOST = 1.3

# 硬闸（不分品类）：见 _score_move 里的实测分布说明
_HARD_DROP = 60.0
_HARD_RISE = 40.0


def _label(brand: str | None, model: str | None) -> str:
    """品牌 + 型号，去掉重复。

    ★ 型号名里常常已经含品牌（"Honor Pad 10"、"Galaxy S25"），
      直接拼会得到「Honor Honor Pad 10」。归一化那边保留品牌前缀是有理由的
      （跨品牌撞车），所以去重放在**展示层**做。
    """
    b, m = (brand or "").strip(), (model or "").strip()
    if not b:
        return m
    if not m:
        return b
    if m.lower().startswith(b.lower()):
        return m
    return f"{b} {m}"


def _zh_len(text: str) -> int:
    """正文字数。★ 只数**正文**：表格、标题、图注不计入 ——
    用户要的是"读的内容不超过 500 字"，把表格算进去会逼着砍掉最有用的数字。"""
    body = []
    for line in (text or "").splitlines():
        t = line.strip()
        if not t or t.startswith(("#", "|", "-", ">", "```", "!")):
            continue
        body.append(t)
    return len(re.sub(r"[\s*_`\[\]()]", "", "".join(body)))


def _score_move(mv: dict, matched: set) -> float:
    """返回重要度分；0 表示不进榜。"""
    pct = abs(float(mv.get("change_pct") or 0))
    if pct < MOVE_ALERT_PCT:
        return 0.0
    cap = _SANE_MOVE.get(mv.get("cat") or "", 50.0)
    # ★★ 涨价与降价的可信区间**不对称**，必须分开卡。实测近 15 天：
    #     降价 215 条：中位 5.4%、P90 12.3%、最大 54.1%，超 40% 的只有 1 条
    #     涨价  27 条：中位 5.7%、**P90 高达 140.9%**、最大 236%，超 40% 的有 5 条
    #   涨价那条尾巴全是脏数据 —— 样例 89→299、99→300、66→159（同一个
    #   "Smokin Buds" 反复以 89 当基准），是低价侧挂牌解析错了，不是真调价。
    #   业务上也讲得通：清仓可以真降 50%，但没人在 15 天里把价格提 50%。
    #   ⇒ 降价放宽到 60%，涨价卡死在 40%。超出的**直接不进榜**，光打折不够
    #   （+236% 打 0.3 折仍有 106 分，会压过真实的 20% 官方降价）。
    #   ★ 挡下多少条会写进报告末尾，不静默丢弃。
    if float(mv.get("change_pct") or 0) > 0:
        if pct > _HARD_RISE:
            return 0.0
    elif pct > _HARD_DROP:
        return 0.0
    # 主力口径：确认老品不进榜；最近一年的加权；无日期保留不加权
    age = _launch_age_days(mv.get("launch_date"))
    if age is not None and age > STALE_DAYS:
        return 0.0
    score = pct * (0.3 if pct > cap else 1.0)
    if mv.get("rival_product_id") in matched:
        score *= 2.5
    if mv.get("is_official"):
        score *= 1.5
    if age is not None and age <= MAINLINE_DAYS:
        score *= MAINLINE_BOOST
    return score


def _launch_age_days(launch_date) -> int | None:
    if not launch_date:
        return None
    try:
        return (date.today() - date.fromisoformat(str(launch_date)[:10])).days
    except (ValueError, TypeError):
        return None


def _age_tag(launch_date) -> str:
    """给表格用的年龄标注：主力/一年+/未知。"""
    age = _launch_age_days(launch_date)
    if age is None:
        return ""
    if age <= MAINLINE_DAYS:
        return "★新"
    if age <= STALE_DAYS:
        return "一年+"
    return "老品"


def _safe_q(sql: str, params: tuple | list = ()) -> list[dict]:
    """查询失败返回空列表而不是抛异常。

    price_move / strategy_signal / weekly_report 都是后加的表，
    老库可能还没迁移到位；周报是只读汇总，缺一张表应当降级成
    "该章节无数据"，而不是让整个 Agent 崩在一条 SELECT 上。
    """
    try:
        return db.q(sql, params)
    except sqlite3.Error as e:
        log.warning("周报取数失败（当作无数据继续）: %s | %s", str(e)[:120], sql[:80])
        return []


def _pct(v) -> str:
    return "—" if v is None else f"{float(v):+.1f}%"


def _money(v, currency: str | None) -> str:
    """价格一律带币种打印 —— 少了币种的数字在六国报告里就是错误信息源。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):,.0f} {currency or ''}".strip()
    except (TypeError, ValueError):
        return str(v)


def _cell(v) -> str:
    """表格单元格转义。

    ★ 实测踩到：我方产品名里就有竖线（"Astra XT | ULTIMATE DESIGN"），
      电商标题里更是什么都有。竖线不转义会给这一行凭空多切出一列，
      整张表从那行起全部错位 —— 而且错得很像"数据本身是这样"，很难被发现。
      换行同理，会把一行拆成两行。
    """
    if v is None:
        return "—"
    return str(v).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _table(headers: list[str], rows: list[list]) -> str:
    """生成 Markdown 表格。空行集返回空串，由调用方决定写什么替代文案。"""
    if not rows:
        return ""
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(out)


def _clean_prose(text: str) -> str:
    """去掉模型爱加的代码围栏和自作主张的小标题（标题层级由程序统一控制，
    模型插一个 ## 进来会把整篇目录搅乱）。"""
    if not text:
        return ""
    lines = []
    for ln in text.strip().splitlines():
        s = ln.strip()
        if s.startswith("```"):
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
            if not s:
                continue
        lines.append(s)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- Agent

class WeeklyReportAgent(BaseAgent):
    name = "weekly"
    role = "周报"
    description = ("汇总一周的价格变动、策略信号、品牌动态与数据缺口，"
                   "分段调用模型写成给 销售团队 看的中文竞品周报")

    def run(self, week_start: str | None = None, scope: str = "all",
            category: str = "", brief: bool = True) -> dict:
        """生成一期报告。

        ★ 周期口径（2026-08-27 用户改为**每周一次**，废除双周报）：
          周一起算的自然周，week_start 传期内任意一天都吸附到该周周一。
        ★ brief=True（默认）出 ≤500 字的简报；False 保留原来的长报告。
        ★ category 单独传：报告要覆盖**所有国家**，只在产业上做选择。
        """
        kind, ws, we_excl, we = _period_bounds(week_start)
        cc, cat = _parse_scope(scope)
        if category:
            cat = category.strip().lower()
        scope_norm = ("all" if not (cc or cat)
                      else f"{cc}:{cat}" if cat else cc)
        self.start(f"{PERIOD_ZH[kind]} {ws}~{we}（范围 {scope_norm}）")

        # 本周可能还没过完：所有"缺了哪天"的判断都必须以今天为界，
        # 否则会把还没到来的周四周五也算成"漏抓"，报告一开头就先自曝一个假故障。
        today = db.today()
        eff_end = min(we, today)
        facts = self._collect_facts(ws, we, eff_end, cc, cat)

        self.log_step(
            "汇总本周事实", input_ref=f"{ws}~{eff_end}", parsed=facts["metrics"],
            decision="ok" if facts["metrics"]["obs_rows"] else "empty",
            reason=("SQL 聚合完成，数字全部落定，后续模型只负责措辞"
                    if facts["metrics"]["obs_rows"]
                    else "本周无任何价格观测，报告将如实说明而不是编造动态"))

        # 四类分段生成。任何一段失败都只影响那一段（内部已 try/except + 兜底文案）
        # ★ 简报模式只需要一段正文，这四段一次都不跑 ——
        #   否则每期白白多花四次模型调用，产物还用不上。
        prose = {} if brief else {
            "overview": self._write_overview(facts),
            "countries": self._write_countries(facts),
            "signals": self._write_signals(facts),
            "advice": self._write_advice(facts),
        }
        highlights = self._extract_highlights(facts)

        cat_zh = facts["category_name"].get(cat, "全产业") if cat else "全产业"
        title = (f"拉美竞品{PERIOD_ZH[kind]} {ws} ~ {we}"
                 + (f"（{cat_zh}）" if cat else ""))
        if brief:
            top = self._pick_top(facts)
            alerts = self._brief_alerts(facts, top)
            charts = self._brief_charts(facts, top)
            dropped = sum(
                1 for mv in (facts.get("moves") or [])
                if (mv.get("change_pct") or 0) > _HARD_RISE
                or -(mv.get("change_pct") or 0) > _HARD_DROP)
            content = self._assemble_brief(title, kind, ws, we, scope_norm,
                                           cat_zh, facts, top, alerts,
                                           charts, dropped)
            charts = charts + getattr(self, "_extra_charts", [])
            facts["metrics"]["charts"] = charts
            facts["metrics"]["period_kind"] = kind
            facts["metrics"]["brief_chars"] = _zh_len(content)
        else:
            content = self._assemble(title, ws, we, eff_end, scope_norm,
                                     facts, prose)
        report_id = self._save(ws, we, scope_norm, title, content,
                               highlights, facts["metrics"])

        m = facts["metrics"]
        degraded = not (self.llm and self.llm.available())
        summary = (f"周报 {ws}~{we}：观测 {m['obs_rows']} 条 / "
                   f"{m['countries']} 国 {m['channels']} 渠道，"
                   f"价格变动 {m['moves_total']} 个（官方降价 {m['moves_down_official']} 个），"
                   f"新品 {m['new_products']} 个，策略信号 {m['signals']} 条，"
                   f"静默渠道 {m['silent_channels']} 个")
        self.finish("degraded" if degraded else "ok", summary,
                    m["obs_rows"], 1,
                    "未配置 API Key，已输出纯事实版周报" if degraded else "")
        return {"report_id": report_id, "week_start": ws, "week_end": we,
                "scope": scope_norm, "title": title, "content_md": content,
                "highlights": highlights, "metrics": m,
                "chars": len(content), "tokens": self.tokens}

    # ============================================================ 取数

    def _collect_facts(self, ws: str, we: str, eff_end: str,
                       cc: str, cat: str) -> dict:
        """一次性把本周的**事实**全部聚合出来。

        这里是整个 Agent 唯一产生数字的地方 —— 数字只有一个来源，
        报告正文与 metrics JSON 用的是同一批值，两处永远不会打架。
        """
        def sc(alias: str) -> tuple[str, list]:
            """按 scope 拼国家/品类过滤条件"""
            sql, p = "", []
            if cc:
                sql += f" AND {alias}.country_code = ?"
                p.append(cc)
            if cat:
                sql += f" AND {alias}.category_code = ?"
                p.append(cat)
            return sql, p

        f: dict = {}
        po_f, po_p = sc("po")
        pm_f, pm_p = sc("pm")

        # ---- 维表：国家中文名（报告里只写代码，业务读起来费劲）
        f["country_name"] = {r["code"]: r["name_zh"]
                             for r in _safe_q("SELECT code,name_zh FROM country")}
        f["category_name"] = {r["code"]: r["name_zh"]
                              for r in _safe_q("SELECT code,name_zh FROM category")}

        # ---- ① 概览与数据健康度
        # 这里**不过滤 audit_status**：健康度本身就要看有多少被审计剔除。
        # 后面所有做分析的查询才加 audit_status <> 'rejected'。
        ov = _safe_q(f"""
            SELECT COUNT(*) obs_rows,
                   COUNT(DISTINCT po.obs_date)      obs_days,
                   COUNT(DISTINCT po.country_code)  countries,
                   COUNT(DISTINCT po.channel_id)    channels,
                   COUNT(DISTINCT po.brand_id)      brands,
                   COUNT(DISTINCT po.rival_product_id) products,
                   SUM(CASE WHEN po.sale_price IS NULL THEN 1 ELSE 0 END) no_price,
                   SUM(CASE WHEN po.rival_product_id IS NULL THEN 1 ELSE 0 END) unlinked,
                   SUM(CASE WHEN po.seller_kind IN ('self_operated','brand_official')
                            THEN 1 ELSE 0 END) official_rows,
                   SUM(CASE WHEN po.seller_kind='third_party' THEN 1 ELSE 0 END) third_rows,
                   SUM(CASE WHEN po.audit_status='rejected' THEN 1 ELSE 0 END) rejected,
                   SUM(CASE WHEN po.audit_status='pending'  THEN 1 ELSE 0 END) pending,
                   SUM(CASE WHEN po.audit_status='accepted' THEN 1 ELSE 0 END) accepted
            FROM price_obs po
            WHERE po.obs_date BETWEEN ? AND ?{po_f}
        """, [ws, we] + po_p)
        f["overview"] = ov[0] if ov else {}

        # ---- ② 国家 × 品类 的观测密度（分母：没有它，"没降价"分不清是真没动还是没抓到）
        f["obs_grid"] = _safe_q(f"""
            SELECT po.country_code, COALESCE(po.category_code,'unknown') cat,
                   COUNT(*) n, COUNT(DISTINCT po.channel_id) ch_n,
                   COUNT(DISTINCT po.rival_product_id) prod_n,
                   COUNT(DISTINCT po.brand_id) brand_n
            FROM price_obs po
            WHERE po.obs_date BETWEEN ? AND ? AND po.audit_status <> 'rejected'{po_f}
            GROUP BY 1,2 ORDER BY 1, n DESC
        """, [ws, we] + po_p)

        # ---- ③ 国家 × 品类 的价格变动
        #
        # ★ 用 change_pct 的正负判方向，不用 direction 列：
        #   实测见过两者不一致的行，混用会让"降价个数"与"最大降幅"来自两套口径。
        #
        # ★★ SELF_CONSISTENT：只统计"三个数自洽"的变动记录。
        #    price_move 的 upsert（pricemove.py）冲突时只更新 curr_price/change_pct，
        #    **不更新 prev_price** —— 同一天同键被写两次，就会留下
        #    prev 来自第一次、pct 来自第二次的行。实测抓到过一条
        #    prev=239990 / curr=389990 却标着 -48.68% down 的记录。
        #    周报的明细表同时打印「原价 → 现价」和「幅度」，这种行会当着 销售团队 的面
        #    自相矛盾 —— 一次就足以让整份报告失去信任。
        #    所以这里统一按"重算的百分比与存储值相差 ≤0.5pp"过滤，
        #    被排除的条数如实写进数据健康度，不偷偷丢数据。
        consistent = ("""
            AND pm.prev_price > 0
            AND ABS(pm.change_pct
                    - (pm.curr_price - pm.prev_price) * 100.0 / pm.prev_price) <= 0.5
        """)
        bad = _safe_q(f"""
            SELECT COUNT(*) n FROM price_move pm
            WHERE pm.move_date BETWEEN ? AND ?{pm_f}
              AND NOT (pm.prev_price > 0
                       AND ABS(pm.change_pct
                               - (pm.curr_price - pm.prev_price) * 100.0
                                 / pm.prev_price) <= 0.5)
        """, [ws, we] + pm_p)
        f["moves_inconsistent"] = int(bad[0]["n"]) if bad else 0

        f["move_grid"] = _safe_q(f"""
            SELECT pm.country_code, COALESCE(pm.category_code,'unknown') cat,
                   COUNT(*) n,
                   SUM(CASE WHEN pm.change_pct<0 THEN 1 ELSE 0 END) down_n,
                   SUM(CASE WHEN pm.change_pct>0 THEN 1 ELSE 0 END) up_n,
                   SUM(CASE WHEN pm.change_pct<0 AND pm.is_official=1 THEN 1 ELSE 0 END) down_off,
                   SUM(CASE WHEN pm.change_pct<0 AND pm.is_official=0 THEN 1 ELSE 0 END) down_3p,
                   SUM(CASE WHEN pm.change_pct>0 AND pm.is_official=1 THEN 1 ELSE 0 END) up_off,
                   SUM(CASE WHEN pm.change_pct>0 AND pm.is_official=0 THEN 1 ELSE 0 END) up_3p,
                   ROUND(MIN(pm.change_pct),2) max_down_pct,
                   ROUND(MAX(pm.change_pct),2) max_up_pct
            FROM price_move pm
            WHERE pm.move_date BETWEEN ? AND ?{pm_f}{consistent}
            GROUP BY 1,2 ORDER BY 1, n DESC
        """, [ws, we] + pm_p)

        # ---- ④ 变动明细（供"幅度最大的是哪些"与对位竞品交叉用）
        f["moves"] = _safe_q(f"""
            SELECT pm.country_code, COALESCE(pm.category_code,'unknown') cat,
                   pm.rival_product_id, pm.change_pct, pm.is_official, pm.currency,
                   pm.prev_price, pm.curr_price, pm.days_span, pm.move_date,
                   COALESCE(b.name,'未知品牌') brand,
                   COALESCE(NULLIF(rp.model_name,''),'未归一化产品') model,
                   rp.global_launch_date launch_date,
                   COALESCE(ch.name, '') channel
            FROM price_move pm
            LEFT JOIN brand b  ON b.id  = pm.brand_id
            LEFT JOIN rival_product rp ON rp.id = pm.rival_product_id
            LEFT JOIN channel ch ON ch.id = pm.channel_id
            WHERE pm.move_date BETWEEN ? AND ?{pm_f}{consistent}
            ORDER BY ABS(pm.change_pct) DESC LIMIT 400
        """, [ws, we] + pm_p)

        # ---- ⑤ 本周新出现的产品
        # 判据是"该产品×该国的**历史首次**观测日落在本周"，
        # 所以 WHERE 不能限定日期（那样每个产品都会"本周首次出现"），
        # 必须全量 GROUP BY 后再用 HAVING 卡首见日。
        f["new_products"] = _safe_q(f"""
            SELECT po.country_code, COALESCE(po.category_code,'unknown') cat,
                   MIN(po.obs_date) first_seen, COUNT(*) obs_n,
                   COALESCE(b.name,'未知品牌') brand,
                   COALESCE(NULLIF(rp.model_name,''), po.model_guess, po.title) name
            FROM price_obs po
            LEFT JOIN brand b ON b.id = po.brand_id
            LEFT JOIN rival_product rp ON rp.id = po.rival_product_id
            WHERE po.rival_product_id IS NOT NULL
              AND po.audit_status <> 'rejected'
              AND po.product_kind <> 'accessory'{po_f}
            GROUP BY po.rival_product_id, po.country_code
            HAVING MIN(po.obs_date) BETWEEN ? AND ?
            ORDER BY po.country_code, obs_n DESC
        """, po_p + [ws, we])

        # 建库第一周里"人人都是新品"。拿全库最早观测日一比就能识别，
        # 不标出来的话，第一份周报会把 200 个存量机型当成"友商本周新上市"。
        hist = _safe_q("SELECT MIN(obs_date) d, COUNT(DISTINCT obs_date) n FROM price_obs")
        f["history_start"] = (hist[0]["d"] if hist else None)
        f["history_days"] = (hist[0]["n"] if hist else 0)
        f["new_is_reliable"] = bool(f["history_start"] and f["history_start"] < ws)

        # ---- ⑥ 策略信号（按 confidence 排序，这是用户点名的口径）
        f["signals"] = _safe_q(f"""
            SELECT ss.signal_date, ss.country_code, ss.signal_type, ss.confidence,
                   ss.summary_zh, ss.impact_zh, ss.suggested_action,
                   COALESCE(ss.category_code,'') cat,
                   COALESCE(b.name,'') brand,
                   COALESCE(NULLIF(rp.model_name,''),'') model
            FROM strategy_signal ss
            LEFT JOIN brand b ON b.id = ss.brand_id
            LEFT JOIN rival_product rp ON rp.id = ss.rival_product_id
            WHERE ss.signal_date BETWEEN ? AND ?
              {'AND ss.country_code = ?' if cc else ''}
              {'AND ss.category_code = ?' if cat else ''}
            ORDER BY ss.confidence DESC NULLS LAST, ss.signal_date DESC
            LIMIT 25
        """, [ws, we] + ([cc] if cc else []) + ([cat] if cat else []))
        # ★ 明细有 LIMIT，总数必须单独 COUNT。
        #   直接拿 len(列表) 当"本周信号条数"，一旦信号超过 25 条，
        #   周报就会稳定地少报 —— 而且越到后期（信号越多）少报得越厉害。
        n = _safe_q(f"""
            SELECT COUNT(*) n FROM strategy_signal ss
            WHERE ss.signal_date BETWEEN ? AND ?
              {'AND ss.country_code = ?' if cc else ''}
              {'AND ss.category_code = ?' if cat else ''}
        """, [ws, we] + ([cc] if cc else []) + ([cat] if cat else []))
        f["signals_total"] = int(n[0]["n"]) if n else len(f["signals"])

        # ---- ⑦ 品牌动态（新闻流）。published_at 可能为空，回落到入库时间
        f["dynamics"] = _safe_q(f"""
            SELECT d.title, d.summary_zh, d.tag, d.importance, d.url,
                   COALESCE(d.published_at, d.created_at) at,
                   COALESCE(b.name,'') brand, COALESCE(d.country_code,'') country_code
            FROM dynamics d
            LEFT JOIN brand b ON b.id = d.brand_id
            WHERE date(COALESCE(d.published_at, d.created_at)) BETWEEN ? AND ?
              {'AND (d.country_code = ? OR d.country_code IS NULL)' if cc else ''}
            ORDER BY d.importance DESC, at DESC LIMIT 20
        """, [ws, we] + ([cc] if cc else []))
        n = _safe_q(f"""
            SELECT COUNT(*) n FROM dynamics d
            WHERE date(COALESCE(d.published_at, d.created_at)) BETWEEN ? AND ?
              {'AND (d.country_code = ? OR d.country_code IS NULL)' if cc else ''}
        """, [ws, we] + ([cc] if cc else []))
        f["dynamics_total"] = int(n[0]["n"]) if n else len(f["dynamics"])

        # ---- ⑧ 我方对位关系（competitor_match）＋ 本周这些竞品动了没有
        matches = _safe_q(f"""
            SELECT cm.country_code, cm.rival_product_id, cm.rank_in_country,
                   cm.total_score, cm.price_gap_pct, cm.currency,
                   cm.my_price_local, cm.rival_price_local,
                   mp.marketing_name my_name, mp.category_code cat,
                   COALESCE(NULLIF(rp.model_name,''),'未归一化产品') rival_name,
                   COALESCE(b.name,'未知品牌') brand
            FROM competitor_match cm
            JOIN my_product mp   ON mp.id = cm.my_product_id
            JOIN rival_product rp ON rp.id = cm.rival_product_id
            LEFT JOIN brand b ON b.id = rp.brand_id
            WHERE cm.is_excluded = 0
              {'AND cm.country_code = ?' if cc else ''}
              {'AND mp.category_code = ?' if cat else ''}
            ORDER BY cm.country_code, cm.rank_in_country
        """, ([cc] if cc else []) + ([cat] if cat else []))
        f["matches"] = matches
        f["threats"], f["chances"] = self._cross_match_moves(matches, f["moves"])

        # ---- ⑨ 数据缺口
        f["gaps"] = self._collect_gaps(ws, we, eff_end, cc, f["obs_grid"],
                                       po_f, po_p)

        # ---- ⑩ 口碑（本周新增评论，作为"哪些产品在被讨论"的旁证）
        f["reviews"] = _safe_q(f"""
            SELECT r.country_code, COUNT(*) n, ROUND(AVG(r.rating),2) avg_rating
            FROM review r
            WHERE date(r.created_at) BETWEEN ? AND ?
              {'AND r.country_code = ?' if cc else ''}
            GROUP BY 1 ORDER BY n DESC
        """, [ws, we] + ([cc] if cc else []))

        f["metrics"] = self._build_metrics(ws, we, eff_end, f)
        return f

    def _cross_match_moves(self, matches: list[dict],
                           moves: list[dict]) -> tuple[list[dict], list[dict]]:
        """把"我方对位竞品"与"本周价格变动"交叉，得出威胁与机会。

        威胁 = 对位竞品降价（价差被拉开，它更便宜了）；
        机会 = 对位竞品涨价（我方相对变便宜，是抢份额的窗口）。

        ★ 估算新价差只在**币种一致**时才算，且明确标注"估算"：
          match 里的价差是匹配时点的快照，而 price_move 的现价可能来自另一个渠道，
          两者不是同一口径。宁可标估算，也不能把它当成实测价差发给业务。
        """
        idx: dict[tuple, list[dict]] = {}
        for m in moves:
            if m.get("rival_product_id") is None:
                continue
            idx.setdefault((m["rival_product_id"], m["country_code"]), []).append(m)

        threats, chances = [], []
        for cm in matches:
            key = (cm["rival_product_id"], cm["country_code"])
            for mv in idx.get(key, []):
                pct = mv.get("change_pct") or 0
                if abs(pct) < MOVE_ALERT_PCT:
                    continue
                item = dict(cm)
                item.update({"move_pct": pct, "is_official": mv.get("is_official"),
                             "channel": mv.get("channel"),
                             "curr_price": mv.get("curr_price"),
                             "move_currency": mv.get("currency"),
                             "est_gap_pct": None})
                my_p = cm.get("my_price_local")
                if (my_p and my_p > 0 and mv.get("curr_price")
                        and mv.get("currency") == cm.get("currency")):
                    item["est_gap_pct"] = round(
                        (mv["curr_price"] - my_p) / my_p * 100, 2)
                (threats if pct < 0 else chances).append(item)

        threats.sort(key=lambda x: x["move_pct"])            # 跌得最狠的排前面
        chances.sort(key=lambda x: -x["move_pct"])
        return threats[:15], chances[:15]

    def _collect_gaps(self, ws: str, we: str, eff_end: str, cc: str,
                      obs_grid: list[dict], po_f: str = "", po_p: list | None = None) -> dict:
        """数据缺口 —— 报告里最重要的一节，它决定读者会不会误读沉默。"""
        gaps: dict = {}
        po_p = list(po_p or [])

        # ---- 「快照渠道」：本周只抓到 1 天的渠道。
        #
        # ★ 这是实测发现的、比"零采集"更阴险的一种缺口：
        #   price_move 的检出前提是**同一渠道、同一 SKU、至少两个观测日**
        #   （见 pricemove.py 的 cur/prev JOIN）。只抓到一天的渠道，
        #   无论它贡献了多少条观测，都在结构上不可能产出任何价格变动。
        #   而报告的国家×品类表把「观测数」和「降价数/涨价数」并排放，
        #   读者会自然把观测数当成分母 —— 于是「1748 条观测、降价 0」
        #   被读成"这个国家一周没人动价"，实际是"我们只拍了一张照片，
        #   两张照片才能看出动没动"。
        #
        #   实测这一周：14 个活跃渠道里 9 个只有 1 天；秘鲁 83.5% 的观测
        #   来自单日渠道，哥伦比亚可比 SKU 对为 0 —— 而模型据此写出了
        #   "本周秘鲁市场各品类价格无变动""因此没有厂商定价动作"。
        #   这正是本模块设计原则 3 要杜绝的错误，只是漏了这一种形态。
        #
        # 不复用下面的 channel 表查询：那条带 enabled=1，
        # 而"被停用但本周仍有数据"的渠道同样会污染分母，必须从观测侧直接数。
        obs_chans = _safe_q(f"""
            SELECT po.channel_id, po.country_code,
                   COALESCE(ch.name, '渠道#' || po.channel_id) name,
                   COALESCE(ch.kind, '') kind,
                   COUNT(DISTINCT po.obs_date) days, COUNT(*) n
            FROM price_obs po
            LEFT JOIN channel ch ON ch.id = po.channel_id
            WHERE po.obs_date BETWEEN ? AND ?
              AND po.audit_status <> 'rejected'{po_f}
            GROUP BY po.channel_id, po.country_code
            ORDER BY po.country_code, n DESC
        """, [ws, we] + po_p)
        gaps["obs_channels"] = obs_chans
        gaps["snapshot_channels"] = [c for c in obs_chans if (c["days"] or 0) < 2]

        # 逐国汇总：这个国家有多少观测坐在"测不出变动"的渠道上
        by_country: dict[str, dict] = {}
        for c in obs_chans:
            d = by_country.setdefault(c["country_code"],
                                      {"obs": 0, "snap_obs": 0,
                                       "ch": 0, "snap_ch": 0})
            d["obs"] += c["n"] or 0
            d["ch"] += 1
            if (c["days"] or 0) < 2:
                d["snap_obs"] += c["n"] or 0
                d["snap_ch"] += 1
        for d in by_country.values():
            d["snap_pct"] = round(d["snap_obs"] * 100.0 / d["obs"], 1) if d["obs"] else 0.0

        # ---- 真正的分母：本周有多少个 SKU 拿得出「两个观测日」可以比。
        #
        # 上面的"渠道只抓到 1 天"只挡得住最粗的一种情况。实测更常见的是：
        # 渠道抓了两天，但两天抓到的**不是同一批 SKU**（翻页顺序变了、
        # 搜索结果变了、规格没解析出来），可比对数照样接近 0。
        #   实测本周：巴西 Amazon 两天都有数据、423 条观测，
        #   可比 SKU 只有 7 个 —— 而模型据此写出了"市场整体较为平稳"；
        #   哥伦比亚 583 条观测、可比 SKU 为 0，模型写了"市场较为平静"。
        # 口径与 pricemove.py 完全对齐（同渠道+同产品+同 sku_key+同币种，
        # 30 天回溯窗，规格全缺的 '-1|-1|' 不算可比），这样报告里的
        # "可比 N 个"就是"本周最多可能检出 N 个变动"的硬上界。
        cmp_rows = _safe_q(f"""
            WITH k AS (
              SELECT po.country_code cc,
                     IFNULL(po.rom_gb,-1) || '|' || IFNULL(po.ram_gb,-1)
                       || '|' || IFNULL(po.color,'') sku,
                     COUNT(DISTINCT po.obs_date) days,
                     SUM(CASE WHEN po.obs_date BETWEEN ? AND ? THEN 1 ELSE 0 END) in_week
              FROM price_obs po
              WHERE po.obs_date BETWEEN date(?, '-30 day') AND ?
                AND po.sale_price IS NOT NULL
                AND po.rival_product_id IS NOT NULL
                AND po.product_kind <> 'accessory'
                AND po.condition = 'new' AND po.is_bundle = 0
                AND po.audit_status <> 'rejected'{po_f}
              GROUP BY po.channel_id, po.rival_product_id, sku, po.currency
            )
            SELECT cc, COUNT(*) sku_total,
                   SUM(CASE WHEN days >= 2 AND sku <> '-1|-1|'
                            THEN 1 ELSE 0 END) comparable
            FROM k WHERE in_week > 0 GROUP BY cc
        """, [ws, we, ws, we] + po_p)
        for r in cmp_rows:
            d = by_country.setdefault(r["cc"], {"obs": 0, "snap_obs": 0, "ch": 0,
                                                "snap_ch": 0, "snap_pct": 0.0})
            d["sku_total"] = int(r["sku_total"] or 0)
            d["comparable"] = int(r["comparable"] or 0)
        gaps["comparability"] = by_country
        gaps["comparable_skus"] = int(sum(r["comparable"] or 0 for r in cmp_rows))
        gaps["total_skus"] = int(sum(r["sku_total"] or 0 for r in cmp_rows))

        # 已启用但本周颗粒无收的渠道。带上"上次抓到是哪天"，
        # 便于区分"这周恰好没排到"和"这个渠道已经彻底抓不动了"。
        chans = _safe_q(f"""
            SELECT ch.id, ch.code, ch.name, ch.country_code, ch.kind,
                   (SELECT COUNT(*) FROM price_obs po
                     WHERE po.channel_id = ch.id
                       AND po.obs_date BETWEEN ? AND ?) week_n,
                   (SELECT MAX(po.obs_date) FROM price_obs po
                     WHERE po.channel_id = ch.id) last_seen
            FROM channel ch
            WHERE ch.enabled = 1 {'AND ch.country_code = ?' if cc else ''}
            ORDER BY ch.country_code, ch.priority
        """, [ws, we] + ([cc] if cc else []))
        gaps["silent_channels"] = [c for c in chans if not c["week_n"]]
        gaps["active_channels"] = [c for c in chans if c["week_n"]]

        # 本周哪几天完全没有观测（只算到今天为止）
        have = {r["d"] for r in _safe_q(
            "SELECT DISTINCT obs_date d FROM price_obs WHERE obs_date BETWEEN ? AND ?",
            (ws, eff_end))}
        days, d0 = [], date.fromisoformat(ws)
        for i in range(7):
            day = (d0 + timedelta(days=i)).isoformat()
            if day > eff_end:
                break
            if day not in have:
                days.append(day)
        gaps["missing_days"] = days

        # 历史上有过、本周却一条没有的"国家×品类"组合
        seen_now = {(r["country_code"], r["cat"]) for r in obs_grid}
        hist = _safe_q(f"""
            SELECT po.country_code, COALESCE(po.category_code,'unknown') cat,
                   MAX(po.obs_date) last_seen
            FROM price_obs po
            WHERE po.obs_date < ? {'AND po.country_code = ?' if cc else ''}
            GROUP BY 1,2
        """, [ws] + ([cc] if cc else []))
        gaps["missing_pairs"] = [h for h in hist
                                 if (h["country_code"], h["cat"]) not in seen_now]

        # 启用了却整周没有任何观测的国家
        countries_now = {r["country_code"] for r in obs_grid}
        gaps["silent_countries"] = [
            r["code"] for r in _safe_q(
                f"SELECT code FROM country WHERE enabled=1 "
                f"{'AND code = ?' if cc else ''} ORDER BY sort_order",
                [cc] if cc else [])
            if r["code"] not in countries_now]
        return gaps

    def _build_metrics(self, ws: str, we: str, eff_end: str, f: dict) -> dict:
        ov = f["overview"] or {}
        g = f["gaps"]
        mv = f["move_grid"]

        def s(key: str) -> int:
            return int(sum(r.get(key) or 0 for r in mv))

        obs_rows = int(ov.get("obs_rows") or 0)
        return {
            "week_start": ws, "week_end": we, "counted_through": eff_end,
            "obs_rows": obs_rows,
            "obs_days": int(ov.get("obs_days") or 0),
            "countries": int(ov.get("countries") or 0),
            "channels": int(ov.get("channels") or 0),
            "brands": int(ov.get("brands") or 0),
            "products": int(ov.get("products") or 0),
            "official_rows": int(ov.get("official_rows") or 0),
            "third_party_rows": int(ov.get("third_rows") or 0),
            "no_price_rows": int(ov.get("no_price") or 0),
            "unlinked_rows": int(ov.get("unlinked") or 0),
            "audit_pending": int(ov.get("pending") or 0),
            "audit_accepted": int(ov.get("accepted") or 0),
            "audit_rejected": int(ov.get("rejected") or 0),
            # 归一化率：挂上 rival_product 的比例，低了说明后续所有按产品的分析都不可信
            "link_rate_pct": (round((obs_rows - int(ov.get("unlinked") or 0))
                                    / obs_rows * 100, 1) if obs_rows else 0.0),
            "moves_total": s("n"),
            # 被"三数不自洽"规则挡掉的变动记录数，如实上报而不是悄悄消失
            "moves_inconsistent": f["moves_inconsistent"],
            "moves_down": s("down_n"), "moves_up": s("up_n"),
            "moves_down_official": s("down_off"), "moves_down_third": s("down_3p"),
            "moves_up_official": s("up_off"), "moves_up_third": s("up_3p"),
            "new_products": len(f["new_products"]),
            "new_products_reliable": f["new_is_reliable"],
            # 总数用 COUNT 的结果，不用被 LIMIT 截断的列表长度
            "signals": f["signals_total"],
            "signals_listed": len(f["signals"]),
            "dynamics": f["dynamics_total"],
            "dynamics_listed": len(f["dynamics"]),
            "matches": len(f["matches"]),
            "threats": len(f["threats"]), "chances": len(f["chances"]),
            "silent_channels": len(g["silent_channels"]),
            "active_channels": len(g["active_channels"]),
            # 只抓到 1 天的渠道 —— 它们的「降价 0」是测不出来，不是没发生
            "snapshot_channels": len(g["snapshot_channels"]),
            "snapshot_obs_rows": int(sum(c["n"] or 0 for c in g["snapshot_channels"])),
            "comparable_channels": len([c for c in g["obs_channels"]
                                        if (c["days"] or 0) >= 2]),
            # 本周实际拿得出两天数据可比的 SKU 数 = 变动检出的硬上界
            "comparable_skus": g["comparable_skus"],
            "total_skus": g["total_skus"],
            "missing_days": len(g["missing_days"]),
            "silent_countries": len(g["silent_countries"]),
            "reviews": int(sum(r.get("n") or 0 for r in f["reviews"])),
        }

    # ============================================================ 分段生成

    def _ask_prose(self, step_name: str, prompt: str, *,
                   input_ref: str = "", fallback: str = "") -> str:
        """散文段落走 chat 而不是 chat_json。

        原因：几百字的中文段落塞进 JSON 字符串里极易被模型写坏转义
        （换行、中文引号、破折号），一坏就整段丢失、只能回退默认值；
        纯文本没有这个失败面。结构化的 highlights 才需要 JSON，那边字段短、风险低。

        无论模型报什么错都不往外抛 —— 缺一段点评比整份报告不出要好得多。
        """
        if not (self.llm and self.llm.available()):
            self.log_step(step_name, input_ref=input_ref, decision="skipped",
                          status="degraded",
                          reason="未配置 API Key，本段退化为纯事实文案")
            return fallback
        t0 = time.time()
        try:
            text, tokens = self.llm.chat(prompt, PROSE_SYSTEM)
        except Exception as e:  # noqa: BLE001
            self.log_step(step_name, input_ref=input_ref, prompt=prompt,
                          decision="error", status="degraded",
                          reason=f"模型调用异常，已用事实兜底: {str(e)[:120]}")
            return fallback
        text = _clean_prose(text)
        ok = len(text) >= 20
        self.log_step(step_name, input_ref=input_ref, prompt=prompt, raw=text,
                      decision="ok" if ok else "empty",
                      reason="" if ok else "模型返回空或过短，已用事实兜底",
                      tokens=tokens, duration_ms=int((time.time() - t0) * 1000),
                      status="ok" if ok else "degraded")
        return text if ok else fallback

    def _write_overview(self, f: dict) -> str:
        m = f["metrics"]
        if not m["obs_rows"]:
            # 无事实就不调模型（给模型一个空壳，它只会开始编）。这一步照样留痕，
            # 否则事后看 agent_step 会以为这一步"漏跑了"。
            self.log_step("撰写本周概览", input_ref="overview", decision="skipped",
                          reason="本周零观测，不调用模型（无事实可依，调了只会编）")
            return ("本周（截至 " + m["counted_through"] + "）没有任何价格观测入库，"
                    "以下所有「无变动」均为**数据缺失**，不是友商没有动作。")
        facts = (
            f"统计区间：{m['week_start']} ~ {m['counted_through']}\n"
            f"价格观测 {m['obs_rows']} 条，覆盖 {m['countries']} 个国家、"
            f"{m['channels']} 个渠道、{m['brands']} 个品牌、{m['products']} 个产品，"
            f"有观测的天数 {m['obs_days']} 天\n"
            f"官方渠道（自营/品牌官方店）{m['official_rows']} 条，"
            f"第三方卖家 {m['third_party_rows']} 条\n"
            f"产品归一化率 {m['link_rate_pct']}%（未挂接产品 {m['unlinked_rows']} 条）；"
            f"缺价格 {m['no_price_rows']} 条\n"
            f"价格审计状态：待审 {m['audit_pending']} / 通过 {m['audit_accepted']} /"
            f" 剔除 {m['audit_rejected']}\n"
            f"价格变动事件 {m['moves_total']} 个：降价 {m['moves_down']}"
            f"（官方 {m['moves_down_official']}，第三方 {m['moves_down_third']}），"
            f"涨价 {m['moves_up']}（官方 {m['moves_up_official']}，"
            f"第三方 {m['moves_up_third']}）\n"
            f"本周新出现产品 {m['new_products']} 个"
            + ("" if m["new_products_reliable"]
               else "（★ 全库历史仅 %d 天、最早 %s，本周包含建库首日，"
                    "这些「新出现」多半是首次入库而非新上市，措辞必须留有余地）"
                    % (f["history_days"], f["history_start"]))
            + f"\n策略信号 {m['signals']} 条，品牌动态 {m['dynamics']} 条\n"
            f"数据缺口：{m['silent_channels']} 个已启用渠道本周零采集，"
            f"{m['missing_days']} 天完全没有观测，"
            f"{m['silent_countries']} 个国家整周无数据\n"
            + (f"★★ 可比性（变动数的真分母）：本周 {m['total_skus']} 个 SKU×渠道 组合中，"
               f"只有 {m['comparable_skus']} 个拿得出两个观测日、能比出价格变动 —— "
               f"即本周最多只可能检出 {m['comparable_skus']} 个变动。"
               f"其中 {m['snapshot_channels']} 个渠道只抓到 1 天"
               f"（共 {m['snapshot_obs_rows']} 条观测，结构上不可能有变动记录）。"
               "概览必须点出这一点：变动数少主要是**测不出来**，"
               "不许解读成「市场平静」「厂商没动作」。"
               if m["comparable_skus"] < m["total_skus"] else
               "可比性：本周所有 SKU×渠道 组合均有 2 天以上数据，变动检出无结构性盲区。"))
        prompt = ("下面是本周竞品监测的全部统计口径数字。请写一段 150~220 字的"
                  "「本周概览」，让 销售团队 一眼知道：本周数据覆盖到什么程度、"
                  "整体价格动作是多还是少、以及这份数据有多可信"
                  "（覆盖不足/归一化率低/审计未跑完都要点出来）。\n\n"
                  + facts + "\n\n只输出这一段正文。")
        # 兜底文案与喂给模型的事实简报**必须分开写**：简报里含"措辞必须留有余地"
        # 这类给模型的指令，直接当正文发出去会让读者看见提示词内脏。
        fallback = (
            f"本周（{m['week_start']} ~ {m['counted_through']}）共入库价格观测 "
            f"{m['obs_rows']} 条，覆盖 {m['countries']} 个国家、{m['channels']} 个渠道、"
            f"{m['products']} 个产品；官方渠道 {m['official_rows']} 条，"
            f"第三方 {m['third_party_rows']} 条。检测到价格变动 {m['moves_total']} 个"
            f"（降价 {m['moves_down']}，其中官方渠道 {m['moves_down_official']}；"
            f"涨价 {m['moves_up']}，其中官方渠道 {m['moves_up_official']}）。"
            f"产品归一化率 {m['link_rate_pct']}%，价格审计待审 {m['audit_pending']} 条。"
            f"另有 {m['silent_channels']} 个已启用渠道本周零采集"
            + (f"、{m['snapshot_channels']} 个渠道本周只抓到 1 天"
               f"（{m['snapshot_obs_rows']} 条观测无法检出变动）"
               if m["snapshot_channels"] else "")
            + f"。本周 {m['total_skus']} 个 SKU×渠道 组合中仅 {m['comparable_skus']} 个"
            f"具备两日可比数据，即变动检出的上限只有 {m['comparable_skus']} 个 —— "
            "变动数少首先是测不出来，不能读作市场平静。详见文末数据缺口。"
            "（本段为无模型时的纯事实兜底，未做解读。）")
        return self._ask_prose("撰写本周概览", prompt, input_ref="overview",
                               fallback=fallback)

    def _write_countries(self, f: dict) -> dict[str, str]:
        """逐国生成。一国一次调用 —— 六国合起来写，后面几个国家必被压缩成套话。"""
        out: dict[str, str] = {}
        countries = [r["country_code"] for r in f["obs_grid"]]
        countries += [r["country_code"] for r in f["move_grid"]]
        for code in sorted(set(countries)):
            zh = f["country_name"].get(code, code)
            obs = [r for r in f["obs_grid"] if r["country_code"] == code]
            mvg = [r for r in f["move_grid"] if r["country_code"] == code]
            mvs = [r for r in f["moves"] if r["country_code"] == code][:TOP_MOVES * 2]
            new = [r for r in f["new_products"] if r["country_code"] == code][:TOP_NEW]

            lines = [f"国家：{zh}（{code}）", "各品类观测量："]
            for r in obs:
                lines.append(f"  · {f['category_name'].get(r['cat'], r['cat'])}："
                             f"{r['n']} 条 / {r['ch_n']} 个渠道 / {r['prod_n']} 个产品")
            if mvg:
                lines.append("各品类价格变动：")
                for r in mvg:
                    lines.append(
                        f"  · {f['category_name'].get(r['cat'], r['cat'])}："
                        f"降价 {r['down_n']} 个（官方 {r['down_off']}，第三方 {r['down_3p']}），"
                        f"涨价 {r['up_n']} 个（官方 {r['up_off']}，第三方 {r['up_3p']}），"
                        f"最大降幅 {_pct(r['max_down_pct'])}，最大涨幅 {_pct(r['max_up_pct'])}")
            else:
                lines.append("各品类价格变动：本周该国无价格变动事件入库。")
            if mvs:
                lines.append("幅度最大的变动明细：")
                for r in mvs:
                    lines.append(
                        f"  · [{'官方' if r['is_official'] else '第三方'}] "
                        f"{r['brand']} {r['model']} @{r['channel']}："
                        f"{_money(r['prev_price'], r['currency'])} → "
                        f"{_money(r['curr_price'], r['currency'])}（{_pct(r['change_pct'])}）")
            if new:
                lines.append("本周首次出现的产品："
                             + "；".join(f"{r['brand']} {r['name']}"[:60] for r in new))
            if not f["new_is_reliable"]:
                lines.append("★ 注意：全库历史很短，「新出现」很可能只是首次入库，"
                             "不要说成新上市。")

            # ★ 把"这个国家有多少观测根本测不出变动"直接喂给模型。
            #   不喂的话它只看到「观测 1748 / 降价 0」，必然写出
            #   "本周该国价格无变动、市场平静" —— 那是把没测量说成没发生。
            comp = f["gaps"]["comparability"].get(code) or {}
            n_cmp = comp.get("comparable")
            if n_cmp is not None:
                lines.append(
                    f"★★ 可比性（这才是「降价数」的真分母）："
                    f"本国本周 {comp.get('obs', 0)} 条观测、"
                    f"{comp.get('sku_total', 0)} 个 SKU×渠道 组合中，"
                    f"只有 {n_cmp} 个拿得出两个观测日、能够比出价格变动。"
                    + (f"另有 {comp.get('snap_obs', 0)} 条观测"
                       f"（{comp.get('snap_pct', 0)}%）来自 {comp.get('snap_ch', 0)} 个"
                       "「本周只抓到 1 天」的渠道。" if comp.get("snap_ch") else "")
                    + f"也就是说本周该国最多只可能检出 {n_cmp} 个变动。")
                if n_cmp < 30:
                    lines.append(
                        "★★★ 上面这个可比数很小，「降价 0 / 涨价 0」几乎全部是"
                        "**没测出来**，不是没发生。你必须写成"
                        "「本周可比数据太少，不足以判断该国价格是否变动」，"
                        "**严禁**写成「市场平静」「价格平稳」「无降价趋势」"
                        "「未发现降价行为」「没有厂商定价动作」这一类结论 —— "
                        "把没测量说成没发生，是这份报告最严重的错误。")
                elif comp.get("snap_ch"):
                    # 可比数够写结论，但只覆盖了一部分渠道。实测 MX：50 个可比
                    # 组合全在 Sanborns 一家，模型却写成"墨西哥市场整体价格稳定"，
                    # 把一家店的观察扩张成了一个国家的结论。
                    lines.append(
                        "★★★ 可比数据只覆盖本国一部分渠道。若你要下"
                        "「价格稳定 / 无变动」这类结论，**必须把它限定到"
                        "真正比对过的那些渠道**（写成「在本周有两日数据的渠道上未见调价」），"
                        "**不许**说成「本国市场整体价格稳定」「全国无降价」—— "
                        "剩下那些渠道本周根本没被测量。")

            prompt = ("下面是某个国家本周的竞争数据。请写一段 120~200 字的分析，"
                      "回答三件事：①这个国家本周哪个品类在动、动得多不多；"
                      "②降价是官方渠道在做（渠道自营/品牌官方店的定价动作）"
                      "还是第三方在甩货；③有没有值得盯的单品动作。"
                      "观测量太小的品类要明说「样本不足，不足以判断」。\n"
                      "★ 若下面出现「可比性警告」，那部分观测量不能当作"
                      "「没有降价」的证据，必须写成「本周数据不足以判断」。\n\n"
                      + "\n".join(lines) + "\n\n只输出这一段正文。")
            out[code] = self._ask_prose(f"撰写国家动态 {code}", prompt,
                                        input_ref=f"country:{code}",
                                        fallback="")   # 兜底：正文只留表格，不编点评
        return out

    def _write_signals(self, f: dict) -> str:
        sigs, dyns = f["signals"], f["dynamics"]
        if not sigs and not dyns:
            # 没料就不调模型：既省 token，更重要的是杜绝"无中生有"的风险
            self.log_step("撰写策略信号解读", input_ref="signals", decision="skipped",
                          reason="本周无策略信号、无品牌动态，不调用模型")
            return ("本周 strategy_signal 表无信号入库、dynamics 表无品牌动态入库。"
                    "这**不代表友商没有动作**，只说明信号 Agent 与情报 Agent "
                    "本周没有产出（见文末数据缺口）。")
        n_sig, n_dyn = f["signals_total"], f["dynamics_total"]
        lines = [f"本周共入库策略信号 {n_sig} 条、品牌动态 {n_dyn} 条；"
                 f"下面只列出置信度/重要度最高的部分，不是全部。"]
        if sigs:
            lines.append("策略信号（按置信度降序）：")
            for s in sigs[:12]:
                lines.append(
                    f"  · [{s['signal_date']}] {s['country_code']} {s['brand']} "
                    f"{s['model']} 类型={s['signal_type']} "
                    f"置信度={s['confidence']} 结论：{s['summary_zh']}"
                    + (f" 影响：{s['impact_zh']}" if s.get("impact_zh") else ""))
        else:
            lines.append("策略信号：本周无信号入库。")
        if dyns:
            lines.append("品牌动态（新闻/情报流）：")
            for d in dyns[:12]:
                lines.append(f"  · [{str(d['at'])[:10]}] {d['country_code'] or '—'} "
                             f"{d['brand'] or '—'} 标签={d['tag'] or '未分类'} "
                             f"重要度={d['importance']} "
                             f"{d['summary_zh'] or d['title']}")
        else:
            lines.append("品牌动态：本周无新闻情报入库。")
        prompt = ("下面是本周的策略信号与品牌动态原始条目。请写一段 150~250 字的解读："
                  "哪几条最值得 销售团队 关注、它们之间有没有互相印证的地方"
                  "（比如某品牌既有官方降价又有开店/发布动作）。"
                  "置信度低于 0.6 的信号要标注为待验证。"
                  "如果某一类本周为空，直接说空，不要脑补。\n\n"
                  + "\n".join(lines) + "\n\n只输出这一段正文。")
        # 兜底不复用上面的条目清单：那是给模型看的格式（带 类型= 置信度= 字段名），
        # 而正文下方已经有同样内容的表格，重复一遍只会让读者以为是两批数据。
        fallback = (
            f"本周入库策略信号 {n_sig} 条、品牌动态 {n_dyn} 条，明细见下表。"
            "（无模型可用，本段未做解读；表内 summary_zh 为各来源 Agent 的原始结论。）"
            + ("" if sigs else "本周没有策略信号入库，"
                              "这不代表友商没有策略动作，只说明信号 Agent 未产出。"))
        return self._ask_prose("撰写策略信号解读", prompt, input_ref="signals",
                               fallback=fallback)

    def _write_advice(self, f: dict) -> str:
        m = f["metrics"]
        if not f["matches"]:
            self.log_step("撰写对我方影响与建议", input_ref="advice", decision="skipped",
                          reason="competitor_match 无可用对位关系，"
                                 "无法判断竞品是否逼近我方价位带，不调用模型")
            return ("competitor_match 表内本周范围没有可用的对位关系"
                    "（我方产品尚未与友商产品建立匹配，或全部被人工排除），"
                    "因此无法判断竞品是否逼近我方价位带。"
                    "建议先跑竞品匹配，再看这一节。")
        lines = [f"我方已建立对位关系 {m['matches']} 条。",
                 "★ 各国币种不同，以下只看百分比价差，绝不跨国比绝对价格。"]
        near = [c for c in f["matches"]
                if c.get("price_gap_pct") is not None
                and abs(c["price_gap_pct"]) <= NEAR_BAND_PCT][:12]
        if near:
            lines.append(f"贴身对位（价差在 ±{NEAR_BAND_PCT:.0f}% 以内，直接咬我方价位带）：")
            for c in near:
                lines.append(f"  · {c['country_code']} 我方 {c['my_name']} vs "
                             f"{c['brand']} {c['rival_name']}："
                             f"竞品比我方 {_pct(c['price_gap_pct'])}"
                             f"（rank {c['rank_in_country']}）")
        if f["threats"]:
            lines.append("本周对位竞品**降价**（价差被拉开，威胁）：")
            for t in f["threats"][:10]:
                est = (f"，按该渠道现价估算价差变为 {_pct(t['est_gap_pct'])}"
                       if t["est_gap_pct"] is not None else "，币种不一致故不估算新价差")
                lines.append(
                    f"  · {t['country_code']} {t['brand']} {t['rival_name']} "
                    f"[{'官方渠道' if t['is_official'] else '第三方'}] "
                    f"降 {_pct(t['move_pct'])}，对位我方 {t['my_name']}，"
                    f"原价差 {_pct(t['price_gap_pct'])}{est}")
        else:
            lines.append("本周没有对位竞品降价超过阈值。")
        if f["chances"]:
            lines.append("本周对位竞品**涨价**（我方相对变便宜，机会窗口）：")
            for c in f["chances"][:10]:
                lines.append(
                    f"  · {c['country_code']} {c['brand']} {c['rival_name']} "
                    f"[{'官方渠道' if c['is_official'] else '第三方'}] "
                    f"涨 {_pct(c['move_pct'])}，对位我方 {c['my_name']}，"
                    f"原价差 {_pct(c['price_gap_pct'])}")
        else:
            lines.append("本周没有对位竞品涨价超过阈值。")

        prompt = ("下面是我方（Acme）与友商的对位关系，以及本周这些对位竞品的价格动作。"
                  "请写 200~300 字的「对Acme的影响与建议」：\n"
                  "① 哪些竞品已经咬进我方价位带、本周还在往下压；\n"
                  "② 出现了哪些机会窗口（竞品涨价 / 缺货 / 我方相对变便宜）；\n"
                  "③ 给出 2~4 条**可执行**的动作建议（针对具体国家和具体产品），"
                  "每条都要能追溯到上面的某个数字。\n"
                  "官方渠道的降价才算厂商定价动作，第三方降价要降权处理。"
                  "带「估算」字样的价差必须在建议里保留「估算」二字。\n\n"
                  + "\n".join(lines) + "\n\n只输出正文，可用短横线列表。")
        # 兜底同样不复用模型简报（它带 ★ 提示与 rank= 字段名）。
        # 建议本身不能编，所以无模型时只陈述事实并把判断交回给人。
        fallback = (
            f"我方已建立对位关系 {m['matches']} 条，其中价差在 ±{NEAR_BAND_PCT:.0f}% "
            f"以内的贴身对位 {len(near)} 条。本周对位竞品降价 {len(f['threats'])} 项"
            f"（价差被拉开），涨价 {len(f['chances'])} 项（我方相对变便宜），"
            "明细见下表。（无模型可用，本段不给建议 —— 建议必须有人读过明细再下。）")
        return self._ask_prose("撰写对我方影响与建议", prompt, input_ref="advice",
                               fallback=fallback)

    def _extract_highlights(self, f: dict) -> list[str]:
        """要点列表 —— **纯规则生成，不经过模型**。

        ★ 这是实测踩出来的：原先让模型把要点"提炼得更短"，它把
          「CL Xiaomi Redmi A5 +12.5%（涨价）」写成了
          「官方渠道最大降价幅度为 CL Xiaomi Redmi A5 -12.5%」——
          涨价被改写成降价，符号反了。要点会被推到 Telegram、会被贴进汇报，
          是全篇传播最广、最没人回头核对的部分，绝不能让模型碰数字。
          模型的发挥空间留在正文各段（那里有上下文，读者也在读表格）。
        """
        m = f["metrics"]
        out: list[str] = []
        if not m["obs_rows"]:
            out.append(f"本周（截至 {m['counted_through']}）零观测入库，报告无事实基础")
        else:
            out.append(f"本周观测 {m['obs_rows']} 条，覆盖 {m['countries']} 国 "
                       f"{m['channels']} 渠道 {m['products']} 个产品")
            if m["moves_total"]:
                out.append(f"价格变动 {m['moves_total']} 个：降价 {m['moves_down']}"
                           f"（官方渠道 {m['moves_down_official']}）、"
                           f"涨价 {m['moves_up']}（官方渠道 {m['moves_up_official']}）")
            # 涨、跌各取一条最大的：只报"绝对值最大"会让读者误以为那就是降价冠军
            downs = [r for r in f["moves"] if (r["change_pct"] or 0) < 0]
            ups = [r for r in f["moves"] if (r["change_pct"] or 0) > 0]
            if downs:
                b = min(downs, key=lambda r: r["change_pct"])
                out.append(f"最大降价：{b['country_code']} {b['brand']} {b['model']} "
                           f"降 {abs(b['change_pct']):.1f}%"
                           f"（{'官方渠道' if b['is_official'] else '第三方'}）")
            if ups:
                b = max(ups, key=lambda r: r["change_pct"])
                out.append(f"最大涨价：{b['country_code']} {b['brand']} {b['model']} "
                           f"涨 {b['change_pct']:.1f}%"
                           f"（{'官方渠道' if b['is_official'] else '第三方'}）")
            if m["new_products"]:
                out.append(f"新出现产品 {m['new_products']} 个"
                           + ("" if m["new_products_reliable"] else "（疑为首次入库，非新上市）"))
            if m["threats"]:
                out.append(f"{m['threats']} 项对位竞品本周降价，价差被拉开")
            if m["chances"]:
                out.append(f"{m['chances']} 项对位竞品本周涨价，出现机会窗口")
        if m["signals"]:
            # ★ 实测崩过：这里的判据是 m["signals"]（COUNT 查询的结果），
            #   而 max() 迭代的是 f["signals"]（带 LIMIT 的明细列表）。
            #   明细查询多了两个 LEFT JOIN（brand / rival_product），
            #   一旦它被 _safe_q 降级成 []（缺表、缺列都会），COUNT 那条却照常成功，
            #   就会 max() 空序列 → ValueError，整个 run 死在 start() 之后、
            #   finish() 之前，在 agent_run 里留下一条永远 running 的孤儿记录。
            #   _safe_q 的全部意义就是"缺数据也要出报告"，绝不能在这里被一个
            #   聚合函数抵消掉 —— 判据必须是被迭代的那个列表本身。
            confs = [s["confidence"] for s in f["signals"]
                     if s.get("confidence") is not None]
            out.append(f"策略信号 {m['signals']} 条入库"
                       + (f"，最高置信度 {max(confs):.2f}" if confs
                          else "（明细未取到，置信度无法统计）"))
        else:
            out.append("本周无策略信号入库（信号 Agent 未产出，非友商无动作）")
        if m["silent_channels"]:
            out.append(f"{m['silent_channels']} 个已启用渠道本周零采集，"
                       f"沉默不等于友商没动作")
        return out[:8]

    # ============================================================ 拼装

    # ══════════════ 简报：选材 → 图 → 组装（≤500 字）══════════════

    def _matched_rival_ids(self, f: dict) -> set:
        """我方有对位的友商产品 id。打到这些产品身上的调价权重最高。"""
        ids = set()
        for m in (f.get("matches") or []):
            v = m.get("rival_product_id")
            if v:
                ids.add(v)
        return ids

    def _pick_top(self, f: dict) -> list[dict]:
        """按重要度挑出本期最该说的几条变动。**这是整份简报的取舍所在**。"""
        matched = self._matched_rival_ids(f)
        scored = []
        for mv in (f.get("moves") or []):
            sc = _score_move(mv, matched)
            if sc <= 0:
                continue
            scored.append({**mv, "_score": sc,
                           "_hits_us": mv.get("rival_product_id") in matched})
        scored.sort(key=lambda x: -x["_score"])
        # 同一款机型只留最大的那一条 —— 同机型多渠道会把榜单刷满
        seen, out = set(), []
        for mv in scored:
            k = (mv.get("model"), mv.get("country_code"))
            if k in seen:
                continue
            seen.add(k)
            out.append(mv)
        return out

    def _brief_charts(self, f: dict, top: list[dict]) -> list[dict]:
        """图的数据。★ 走 charts.js 的语义层：问题决定图形，不在这里挑图元。
        「前→后两点比较」= compare（哑铃图），与涨价看板同一套语法。"""
        charts = []
        rows = []
        for mv in top[:CHART_N]:
            prev, cur = mv.get("prev_price"), mv.get("curr_price")
            if prev is None or cur is None:
                continue
            rows.append({
                "label": f"{_label(mv.get('brand'), mv.get('model')) or '?'} · {mv.get('country_code')}",
                "from": prev, "to": cur,
                "pct": round(float(mv.get("change_pct") or 0), 1),
                "note": (f"{mv.get('brand') or ''} {mv.get('cat') or ''} · "
                         f"{mv.get('channel') or ''} · "
                         f"{'官方渠道' if mv.get('is_official') else '第三方'}"
                         + ("　⚠ 打到我方对位" if mv.get("_hits_us") else "")),
            })
        if rows:
            # ★★ 这张图**只能画变动幅度，不能画绝对价**。
            #   第一版画的是「前→后」的绝对价哑铃图，结果 5 个币种同轴：
            #   哥伦比亚的 210 万把智利、秘鲁、墨西哥全压成贴底的一个点，
            #   整张图只看得出"有一条很长的线"——而这恰恰是本项目反复纠正的错
            #   （跨币种的绝对值不可同轴，量纲差三个数量级）。
            #   轴标签写"跨国不可比"只是**承认**问题，没有解决问题。
            #   幅度是无量纲的，六国可以放心同屏；而且读者真正要的就是"动了多少"。
            charts.append({
                "question": "deviation", "el": "wk-moves",
                "title": "本期重点机型的变动幅度",
                "xlab": "相对上一价格的变动",
                # ★ 选材按**重要度**，但图上按**幅度**排序 —— 两者不冲突：
                #   选谁上榜是业务判断（打到我方对位的优先），
                #   而条形图不按数值排就很难读（大条夹在小条中间）。
                #   标题也跟着写"变动幅度"，不写"变动最大"，免得名实不符。
                "opt": {"rows": sorted(
                            [{"label": r["label"], "v": r["pct"],
                              "note": r["note"]} for r in rows],
                            key=lambda x: x["v"]),
                        "upIsBad": True},
            })

        # 各国变动条数：构成问题 → share（100% 堆叠），涨跌分开
        per = {}
        for mv in (f.get("moves") or []):
            cc = mv.get("country_code") or "?"
            d = per.setdefault(cc, {"label": cc, "down": 0, "up": 0})
            if (mv.get("change_pct") or 0) < 0:
                d["down"] += 1
            else:
                d["up"] += 1
        if per:
            charts.append({
                "question": "share", "el": "wk-mix",
                "title": "各国降价 / 涨价条数构成",
                "opt": {"rows": list(per.values()),
                        "order": [{"name": "降价", "k": "down"},
                                  {"name": "涨价", "k": "up"}]},
            })
        return charts

    def _brief_alerts(self, f: dict, top: list[dict]) -> list[dict]:
        """价格预警：只报**打到我方对位**的变动。
        ★ 不打到我们身上的降价不是预警，是背景噪声 —— 全报等于没报。"""
        out = []
        for mv in top:
            if not mv.get("_hits_us"):
                continue
            out.append({
                "model": mv.get("model"), "cc": mv.get("country_code"),
                "pct": mv.get("change_pct"), "brand": mv.get("brand"),
                "channel": mv.get("channel"), "cat": mv.get("cat"),
                "prev": mv.get("prev_price"), "curr": mv.get("curr_price"),
                "currency": mv.get("currency"),
            })
            if len(out) >= 4:
                break
        return out

    def _brief_prose(self, f: dict, top: list[dict], alerts: list[dict],
                     kind: str, cat_zh: str, cat_scope: str = "") -> str:
        """正文。★ 硬性 ≤500 字，且**一个数字都不许模型自己造**。

        ★ 事实必须给两个窗口：报告期（可能才开了几天，检出为 0 很正常）
          和近 30 天累计（正文分国分析用的就是它）。只喂报告期会写出
          「本期未观测到任何变动」，而三行之下就是 56 个变动的表 ——
          同一页自相矛盾（2026-08-25 用户原话：「怎么可能呢？」）。
        """
        m = f["metrics"]
        lines = []
        for mv in top[:HEADLINE_N]:
            lines.append(
                f"- {mv.get('brand') or ''} {mv.get('model') or ''}"
                f"（{mv.get('country_code')}·{f['category_name'].get(mv.get('cat'), mv.get('cat') or '')}）"
                f"{'降' if (mv.get('change_pct') or 0) < 0 else '涨'}"
                f"{abs(mv.get('change_pct') or 0):.1f}%，"
                f"{'官方渠道' if mv.get('is_official') else '第三方卖家'}"
                f"{'，打到我方对位机型' if mv.get('_hits_us') else ''}")
        facts_txt = "\n".join(lines) or "（报告期开始至今暂无新增达标变动）"

        # 近 30 天累计（与下文分国分析同一窗口、同一口径）
        m30 = self._moves30(cat_scope)
        cn = f.get("country_name") or {}
        catn = f.get("category_name") or {}
        l30 = [
            f"- {_label(mv['brand'], mv['model'])}"
            f"（{cn.get(mv['cc'], mv['cc'])}·{catn.get(mv['cat'], mv['cat'])}）"
            f"{mv['pct']:+.1f}%，{mv['channel'] or '未知渠道'}"
            f"{'' if mv['off'] else '（第三方）'}，{str(mv['d'])[5:]}"
            for mv in m30[:5]]
        down30 = sum(1 for mv in m30 if (mv["pct"] or 0) < 0)

        prompt = (
            f"下面是拉美六国{cat_zh}竞品情报的**已算好的事实**，分两个窗口。\n\n"
            f"【报告期】{PERIOD_ZH[kind]}，仍在进行中：新增价格变动 "
            f"{m['moves_total']} 个（官方降价 {m['moves_down_official']}、"
            f"官方涨价 {m['moves_up_official']}）；打到我方对位的 {len(alerts)} 个。\n"
            f"{facts_txt}\n\n"
            f"【近 30 天累计】达标变动 {len(m30)} 个（降价 {down30} 个），"
            f"最重要的几条：\n" + "\n".join(l30 or ["（无）"]) + "\n\n"
            "请写一段给Acme拉美 销售团队 的简报正文，要求：\n"
            "1. **总共不超过 300 个汉字**，宁可少写不许超。\n"
            "2. 先一句话给结论（最该关注什么），再用 2~3 条短句说清最重点的动作。\n"
            "3. **不得新增、改写、推算任何数字** —— 只能用上面出现过的数。\n"
            "4. 主体讲近 30 天格局（与下文分国分析一致）；报告期若新增为 0，"
            "用一句书面语说明「报告期开始至今暂无新增达标变动」即可，"
            "**不得写成整个市场没有变动**。\n"
            "5. 不要复述覆盖量、不要写套话；禁止「没动静」这类口语。\n"
            "6. 如果有'打到我方对位'的变动，必须放在最前面说。\n")
        out = self._ask_prose("写简报正文", prompt, fallback="")
        if not out:
            # 兜底：没有模型也要出报告，退化成纯事实版
            head = (f"近 30 天累计 {len(m30)} 个达标价格变动（降价 {down30} 个）；"
                    f"报告期开始至今新增 {m['moves_total']} 个。")
            if alerts:
                head += f"有 {len(alerts)} 个打到我方对位机型，需优先处理。"
            out = head + "\n" + "\n".join(l30)
        return _clean_prose(out)

    # ══════ 品牌周度 ASP（美元）图 + 分国 VOC 模块（用户 2026-08-25 要求）══════

    REPORT_CATS = ("phone", "tablet", "wearable", "audio")   # 用户点名的四个品类
    # 分国模块的固定国家顺序（用户 2026-08-25 指定），不按数据量排
    COUNTRY_ORDER = ("MX", "CO", "CL", "PE", "AR", "BR")

    def _cc_sort(self, codes) -> list[str]:
        pos = {c: i for i, c in enumerate(self.COUNTRY_ORDER)}
        return sorted(codes, key=lambda c: (pos.get(c, 99), c))

    def _active_countries(self) -> list[str]:
        """启用国家全集（用户 2026-08-27：「所有国家所有产业，不要挑着放」）。

        ★ 从库里的 enabled 取，不写死 —— 停采的国家（AR）自动不出现，
          恢复启用当期自动补回，不用改代码。
        """
        rows = _safe_q("SELECT code FROM country WHERE enabled=1")
        return self._cc_sort([r["code"] for r in rows]) or list(self.COUNTRY_ORDER)

    def _active_cats(self) -> list[str]:
        """启用产业全集，按库里的 sort_order。同样不写死。"""
        rows = _safe_q("SELECT code FROM category WHERE enabled=1 "
                       "ORDER BY sort_order, code")
        return [r["code"] for r in rows] or list(self.REPORT_CATS)

    def _coverage30(self) -> dict:
        """近 30 天各 国家×品类 的观测底子。

        ★ 没有变动时必须能区分「友商没动」与「我们没抓到」——
          这是本项目最贵的一类错（见文件头）。空小节靠这个数字说话。
        """
        rows = _safe_q("""
            SELECT country_code cc, category_code cat,
                   COUNT(*) obs, COUNT(DISTINCT rival_product_id) sku,
                   MAX(obs_date) last_d
            FROM price_obs
            WHERE obs_date >= date('now','-30 day') AND category_code IS NOT NULL
            GROUP BY cc, cat""")
        return {(r["cc"], r["cat"]): r for r in rows}

    def _moves30(self, cat: str) -> list[dict]:
        """近 30 天价格变动（比报告期更长的回看窗口，供趋势分析）。
        15 天里每品类只轮到采集 2~3 次，样本太薄谈不上趋势；30 天能看出方向。"""
        cat_f = "AND pm.category_code=?" if cat else ""
        rows = _safe_q(f"""
            SELECT pm.country_code cc, COALESCE(pm.category_code,'unknown') cat,
                   pm.change_pct pct, pm.prev_price a, pm.curr_price b,
                   pm.currency cur, pm.is_official off, pm.move_date d,
                   pm.rival_product_id pid, pm.channel_id chid,
                   COALESCE(b2.name,'') brand,
                   COALESCE(NULLIF(rp.model_name,''),'?') model,
                   rp.global_launch_date launch_date,
                   COALESCE(ch.name,'') channel
            FROM price_move pm
            LEFT JOIN brand b2 ON b2.id=pm.brand_id
            LEFT JOIN rival_product rp ON rp.id=pm.rival_product_id
            LEFT JOIN channel ch ON ch.id=pm.channel_id
            WHERE pm.move_date >= date('now','-30 day')
              AND ABS(pm.change_pct) >= ? {cat_f}
            ORDER BY ABS(pm.change_pct) DESC
        """, ([MOVE_ALERT_PCT, cat] if cat else [MOVE_ALERT_PCT]))
        out, dropped_old = [], 0
        for r in rows:
            pct = abs(float(r["pct"] or 0))
            if (r["pct"] or 0) > 0 and pct > _HARD_RISE:
                continue
            if (r["pct"] or 0) < 0 and pct > _HARD_DROP:
                continue
            age = _launch_age_days(r["launch_date"])
            if age is not None and age > STALE_DAYS:
                dropped_old += 1
                continue
            out.append(dict(r))
        self._moves30_dropped_old = dropped_old
        return out

    # ★ 配件词防线：product_kind='device' 挡不住葡语/西语配件 ——
    #   实测 Fast Shop 巴西的「Capa para Tablet Acme」（保护壳）、
    #   「Película para Slate Tab」（贴膜）全被标成 device，把Acme平板
    #   ASP 线拉到 20 美元。词表只收「配件词 + para/de」形态，
    #   不收裸词：西语 "Pulsera Inteligente" 是真手环，杀不得。
    #   2026-08-27 采集端分类器已补同形态规则（extract.accessory_para_form，
    #   skumap 前置闸同用），本层按用户裁定**保留为双保险**——
    #   它还兼着挡历史上没回填干净的行。
    # 干净整机行的公共过滤（配件/翻新/套装/被审计否决的一律排除）
    _CLEAN_SQL = f"""
            FROM price_obs po
            JOIN brand b ON b.id=po.brand_id
            LEFT JOIN rival_product rp ON rp.id=po.rival_product_id
            LEFT JOIN channel ch ON ch.id=po.channel_id
            WHERE po.country_code=? AND po.category_code=?
              AND po.currency=? AND po.sale_price>0
              AND po.product_kind='device' AND po.condition='new'
              AND po.is_bundle=0 AND po.audit_status<>'rejected'{_ACC_SQL}
    """

    def _mover_series(self, mover: dict, days_back: int = 35):
        """单品×渠道的逐日挂牌价（LOCF 延续）+「变动后续」判定。

        ★ 用户点破的盲区（2026-08-27）：只报"某品 -7.6%"看不出这是
          真降价还是一天闪促 —— 必须拉历史线并量化**新价维持了几天**。
        返回 (dates, vals, filled, verdict)；查不到序列时 verdict 也要给
        （用变动日期到今天的日历天数兜底），表格里不能开天窗。
        """
        pid, chid = mover.get("pid"), mover.get("chid")
        if not (pid and chid):
            return None, None, None, ""
        rows = _safe_q("""
            SELECT po.obs_date d, AVG(po.sale_price) p
            FROM price_obs po
            WHERE po.rival_product_id=? AND po.channel_id=?
              AND po.sale_price>0 AND po.audit_status<>'rejected'
              AND po.obs_date >= date('now', ?)
            GROUP BY po.obs_date ORDER BY po.obs_date
        """, (pid, chid, f"-{days_back} day"))
        if len(rows) < 2:
            return None, None, None, ""
        import datetime as _dt
        d0 = _dt.date.fromisoformat(rows[0]["d"])
        dn = _dt.date.today()
        by_d = {r["d"]: r["p"] for r in rows}
        dates, vals, filled = [], [], []
        cur = None
        d = d0
        while d <= dn:
            iso = d.isoformat()
            obs = by_d.get(iso)
            if obs is not None:
                cur = obs
                filled.append(False)
            else:
                filled.append(True)          # LOCF 延续：挂牌价未变的推定
            dates.append(iso)
            vals.append(round(cur, 0) if cur is not None else None)
            d += _dt.timedelta(days=1)

        # —— 变动后续：新价维持中 or 已回升（短促）——
        a, b = float(mover.get("a") or 0), float(mover.get("b") or 0)
        move_d = str(mover.get("d") or "")[:10]
        verdict = ""
        if b > 0 and move_d:
            after = [(dt, v) for dt, v in zip(dates, vals)
                     if dt >= move_d and v is not None]
            if after:
                revert_day = next((dt for dt, v in after
                                   if a > 0 and abs(v - a) / a < 0.02), None)
                last_v = after[-1][1]
                held = sum(1 for _, v in after if abs(v - b) / b < 0.02)
                if revert_day:
                    k = ( _dt.date.fromisoformat(revert_day)
                          - _dt.date.fromisoformat(move_d)).days
                    verdict = f"{k}天后回升（短促）"
                elif abs(last_v - b) / b < 0.02:
                    verdict = f"新价已维持{held}天"
                else:
                    verdict = f"现价 {_money(last_v, mover.get('cur'))}"
        return dates, vals, filled, verdict

    def _top_products(self, cc: str, cat: str, currency: str,
                      exclude: set, limit: int) -> list[dict]:
        """该国该品类**跟踪最稳的主力在售产品**（给单品走势图补位用）。

        ★ 2026-08-27 用户砍掉品牌均价图后，单品线成为唯一的价格视图，
          所以没有达标变动的小节也必须有产品可看 —— 否则整格空白。
        选取标准：观测天数多（线才画得出来）→ 近一年新品优先 → 价高优先
        （旗舰更有代表性，也更贴近我方对位机型）。
        """
        rows = _safe_q(f"""
            SELECT po.rival_product_id pid, po.channel_id chid,
                   COALESCE(b.name,'') brand,
                   COALESCE(NULLIF(rp.model_name,''), po.title) model,
                   COALESCE(ch.name,'') channel, po.currency cur,
                   rp.global_launch_date launch_date,
                   COUNT(DISTINCT po.obs_date) dz, AVG(po.sale_price) avgp
            {self._CLEAN_SQL}
              AND po.obs_date >= date('now','-35 day')
              AND po.rival_product_id IS NOT NULL
            GROUP BY po.rival_product_id, po.channel_id
            HAVING dz >= 5
        """, (cc, cat, currency))
        out = []
        for r in rows:
            if (r["pid"], r["chid"]) in exclude:
                continue
            age = _launch_age_days(r["launch_date"])
            if age is not None and age > STALE_DAYS:
                continue                      # 老品不占版面
            r = dict(r)
            r["_fresh"] = 0 if (age is not None and age <= MAINLINE_DAYS) else 1
            out.append(r)
        out.sort(key=lambda r: (r["_fresh"], -r["dz"], -(r["avgp"] or 0)))
        # 同一机型只取一个渠道，避免一张图上全是同一台机器
        seen_model, picked = set(), []
        for r in out:
            key = (r["brand"], str(r["model"])[:24])
            if key in seen_model:
                continue
            seen_model.add(key)
            picked.append(r)
            if len(picked) >= limit:
                break
        return picked

    _PROD_LINES = 5

    def _product_trend_chart(self, cc: str, cat: str, movers: list,
                             cat_zh: str, cname: str,
                             currency: str = "") -> dict | None:
        """小节主图：**具体产品**的 35 天价格走势，本币计价。

        ★ 2026-08-27 用户砍掉品牌均价图：「所有的友商均价肯定都不太准，
          你就举例看具体产品的价格就行了」—— 实测确实不可信：墨西哥三星
          手机 134 个 SKU 均值 624 美元、中位数仅 376，被折叠屏的多个存储/
          颜色变体拉飞；没有销量权重时它衡量的是货架构成，不是成交价。
        ★ 有达标变动的产品优先上图（那是本期的新闻），不足 5 条用主力
          在售产品补位，保证每个小节都有真实价格可看。
        """
        picked = []
        seen = set()
        for m in movers:
            k = (m.get("pid"), m.get("chid"))
            if not all(k) or k in seen:
                continue
            # 一张图一个币种：混币种的轴没有意义
            if picked and m.get("cur") != picked[0].get("cur"):
                continue
            seen.add(k)
            picked.append(m)
            if len(picked) >= self._PROD_LINES:
                break
        if len(picked) < self._PROD_LINES:
            cur = picked[0].get("cur") if picked else currency
            for r in self._top_products(cc, cat, cur, seen,
                                        self._PROD_LINES - len(picked)):
                seen.add((r["pid"], r["chid"]))
                picked.append(r)
        series_raw = []
        d_min, d_max = None, None
        for m in picked:
            dates, vals, filled, verdict = self._mover_series(m)
            if not dates:
                continue
            m["_verdict"] = verdict
            series_raw.append((m, dates, vals, filled))
            d_min = min(d_min or dates[0], dates[0])
            d_max = max(d_max or dates[-1], dates[-1])
        if not series_raw:
            return None
        import datetime as _dt
        axis = []
        d = _dt.date.fromisoformat(d_min)
        while d <= _dt.date.fromisoformat(d_max):
            axis.append(d.isoformat())
            d += _dt.timedelta(days=1)
        ai = {x: i for i, x in enumerate(axis)}
        series = []
        for m, dates, vals, filled in series_raw:
            pts = [None] * len(axis)
            fl = [False] * len(axis)
            for dt_, v, f2 in zip(dates, vals, filled):
                pts[ai[dt_]] = v
                fl[ai[dt_]] = f2
            ch = (m.get("channel") or "")[:10]
            # 商品标题里常带未解码的 HTML 实体（&quot / &amp），
            # 直接上图会显示成 'ASUS 14&quot Snapdragon' —— 解码后再截断
            import html as _html
            nm = _html.unescape(str(_label(m["brand"], m["model"])))
            nm = re.sub(r"&\w+;?", "", nm).strip()
            series.append({"name": f"{nm[:22]}·{ch}", "pts": pts, "filled": fl})
        cur = picked[0].get("cur") or currency or ""
        return {"question": "change", "el": f"prod-{cc}-{cat}",
                "title": f"{cname}·{cat_zh}：重点产品价格走势（{cur}）",
                "xlab": cur,
                "opt": {"xs": [x[5:] for x in axis], "series": series,
                        "ylab": f"挂牌价 {cur}（未观测日延续上次价）",
                        "indexed": True}}

    def _brief_voc_country(self, f: dict) -> str:
        """分国 VOC：**先统计、再原声**，且覆盖所有产业。

        ★ 用户 2026-08-27：「每个国家不能只放手机，或者只放一个产业，
          要多去总结、统计 VOC，而不是单纯罗列几条放在那里」。
          所以本段结构 = 国家总量 → 分品类统计表（评论量/差评率/抱怨维度）
          → 跨品类抱怨维度汇总 → 品牌口碑对比 → 代表性原声。
        ★ 维度统计来自 review_aspect（VOC Agent 标注的 code×情感），
          不是关键词猜的；原声仍一字不改（翻译会磨掉语气）。
        """
        from .. import voc_aspects
        A_ZH = getattr(voc_aspects, "ASPECT_ZH", {})
        rows = _safe_q("""
            SELECT rv.id, rv.country_code cc, rv.sentiment, rv.lang,
                   rv.content, rv.content_zh,
                   COALESCE(rp.category_code,'') cat,
                   COALESCE(NULLIF(rp.model_name,''),'?') model,
                   COALESCE(b.name,'') brand, rp.global_launch_date launch,
                   COALESCE(c.name,'') channel
            FROM review rv
            JOIN rival_product rp ON rp.id=rv.rival_product_id
            LEFT JOIN brand b ON b.id=rp.brand_id
            LEFT JOIN channel c ON c.id=rv.channel_id
            WHERE rv.created_at >= datetime('now','-30 day')
              AND rv.content IS NOT NULL AND LENGTH(TRIM(rv.content)) >= 30
              AND rv.sentiment IS NOT NULL
        """)
        if not rows:
            return ""
        # 维度标注：review_id → [(code, sentiment)]
        asp_rows = _safe_q("""
            SELECT ra.review_id rid, ra.aspect_code code, ra.sentiment s
            FROM review_aspect ra JOIN review rv ON rv.id=ra.review_id
            WHERE rv.created_at >= datetime('now','-30 day')""")
        asp_of: dict[int, list] = {}
        for a in asp_rows:
            asp_of.setdefault(a["rid"], []).append((a["code"], a["s"]))

        names = f.get("country_name") or {}
        cat_zh = f.get("category_name") or {}
        by_cc: dict[str, list] = {}
        for r in rows:
            age = _launch_age_days(r["launch"])
            if age is not None and age > STALE_DAYS:
                continue                     # 老品口碑不进周报
            by_cc.setdefault(r["cc"], []).append(r)
        if not by_cc:
            return ""

        from collections import Counter

        def _asp_rank(rs, sent, n=3):
            """这批评论里出现最多的 sent 向维度 → 「屏幕 12、续航 8」"""
            cnt = Counter()
            for r in rs:
                for code, s in asp_of.get(r["id"], []):
                    if s == sent:
                        cnt[code] += 1
            return "、".join(f"{A_ZH.get(c, c)} {k}"
                             for c, k in cnt.most_common(n)) or "—"

        LANG_ZH = {"es": "西语", "pt": "葡语", "en": "英语"}
        out = ["## 🗣 分国 VOC（口碑统计 + 原声）", ""]
        from ..scraping.voc import looks_like_review, strip_page_head

        def _voice(text: str) -> str:
            """原声清洗：只去掉页面拼接的抬头，正文一字不改。

            ★ 站点把「评分 日期 标题 作者」和正文拼在一个块里，
              直接引用会以「5.0 11 Ago 2026 alfonso f.」开头 —— 读者会当成乱码。
            ★ 复用采集侧的 strip_page_head，**不再本地维护一份正则**：
              原来这里只剥「评分 日期」、剥不掉作者名，与采集侧行为不一致；
              同一件事两处实现迟早会各自漂移（这次就漂了）。
            """
            return strip_page_head((text or "").strip().replace("\n", " "))

        def _is_real_voice(text: str) -> bool:
            """是不是真的「消费者说的话」。

            ★ 实测抓到评分分布控件被当评论入库并引进周报：
              「5 estrellas 91 % 4 estrellas 9 % 3 estrellas 0 %…」。
              采集侧（looks_like_review）已修好并清了历史脏数据，
              报告层这一道仍然保留：**引用错了比少引一条贵得多**，
              而且判据直接复用采集侧的，避免两份规则各自漂移。
            ★ 这里比采集侧多一条长度要求（25 > 15）：能进周报被直接引用的
              原声要有信息量，「Muy bueno」这种虽是真评论但引了等于没引。
            """
            t = _voice(text)
            return len(t) >= 25 and looks_like_review(t)
        for cc in self._cc_sort(by_cc):
            rs = by_cc[cc]
            neg_all = [r for r in rs if r["sentiment"] == "negative"]
            out.append(f"### {names.get(cc, cc)}（{cc}）")
            out.append(f"本期 **{len(rs):,}** 条新品评论，差评 **{len(neg_all)}** 条"
                       f"（{len(neg_all) / len(rs) * 100:.1f}%）；"
                       f"覆盖 {len({r['cat'] for r in rs if r['cat']})} 个产业。")
            out.append("")

            # ① 分品类统计表 —— 用户要的「每个国家不能只放一个产业」
            tbl = []
            for c in self._active_cats():
                cr = [r for r in rs if r["cat"] == c]
                if not cr:
                    continue
                cneg = [r for r in cr if r["sentiment"] == "negative"]
                tbl.append([cat_zh.get(c, c), f"{len(cr):,}",
                            f"{len(cneg)}（{len(cneg) / len(cr) * 100:.1f}%）",
                            _asp_rank(cneg, "negative"),
                            _asp_rank(cr, "positive")])
            other = [r for r in rs if r["cat"] not in set(self._active_cats())]
            if other:
                oneg = [r for r in other if r["sentiment"] == "negative"]
                tbl.append(["未归类", f"{len(other):,}",
                            f"{len(oneg)}（{len(oneg) / len(other) * 100:.1f}%）",
                            _asp_rank(oneg, "negative"), _asp_rank(other, "positive")])
            if tbl:
                out.append(_table(["产业", "评论数", "差评数（率）",
                                   "主要抱怨（维度·条数）", "主要好评"], tbl))
                out.append("")

            # ② 品牌口碑对比：评论量前 6 的品牌，差评率排序 —— 谁在挨骂一目了然
            bcnt: dict[str, list] = {}
            for r in rs:
                if r["brand"]:
                    bcnt.setdefault(r["brand"], []).append(r)
            btbl = []
            for b in sorted(bcnt, key=lambda x: -len(bcnt[x]))[:6]:
                br = bcnt[b]
                bneg = [r for r in br if r["sentiment"] == "negative"]
                btbl.append([b, f"{len(br):,}",
                             f"{len(bneg)}（{len(bneg) / len(br) * 100:.1f}%）",
                             _asp_rank(bneg, "negative", 2)])
            if btbl:
                btbl.sort(key=lambda x: -float(x[2].split("（")[1].rstrip("%）")))
                out.append(_table(["品牌", "评论数", "差评数（率）", "主要抱怨"], btbl))
                out.append("")

            # ③ 代表性原声：跨品类挑，每个品类最多 1 条差评，补好评
            def _prio(r):
                age = _launch_age_days(r["launch"])
                return (0 if (age is not None and age <= MAINLINE_DAYS) else 1,
                        -len(r["content"] or ""))

            seen_txt, picks = set(), []

            def _take(pool, limit, per_cat_cap):
                per_cat: Counter = Counter()
                for r in sorted(pool, key=_prio):
                    k = (r["content"] or "")[:80]
                    if k in seen_txt or per_cat[r["cat"]] >= per_cat_cap:
                        continue
                    if not _is_real_voice(r["content"]):
                        continue          # 控件文本不是消费者的话，绝不引用
                    seen_txt.add(k)
                    per_cat[r["cat"]] += 1
                    picks.append(r)
                    if sum(1 for _ in picks) >= limit:
                        break

            _take(neg_all, 3, 1)                       # 差评：每品类最多 1 条
            _take([r for r in rs if r["sentiment"] == "positive"],
                  len(picks) + 2, 1)                   # 再补 2 条好评
            for r in picks:
                mark = "👎 差评" if r["sentiment"] == "negative" else "👍 好评"
                age = _launch_age_days(r["launch"])
                tag = "★新" if (age is not None and age <= MAINLINE_DAYS) else ""
                asp = "、".join(A_ZH.get(c, c) for c, s in asp_of.get(r["id"], [])
                                if s == r["sentiment"])[:24]
                raw = _voice(r["content"])[:200]
                zh = (r["content_zh"] or "").strip().replace("\n", " ")[:150]
                out.append(f"**{_label(r['brand'], r['model'])}**{tag} · {mark}"
                           f"{('（' + asp + '）') if asp else ''} · "
                           f"{cat_zh.get(r['cat'], r['cat'] or '未归类')} · {r['channel']}")
                out.append(f"> 「{raw}」（{LANG_ZH.get(r['lang'], r['lang'] or '原文')}）")
                if zh:
                    out.append(f"> 译：{zh}")
                out.append("")
        out.append("> 口径：近 30 天收集、上市 ≤26 个月的产品；维度统计来自 VOC Agent "
                   "对每条评论的标注（review_aspect），不是关键词猜测；"
                   "差评率分母是本期该切片的评论总数；原声一字未改。")
        out.append("")
        return "\n".join(out)

    def _brief_country_cat(self, f: dict, cat_scope: str) -> str:
        """分产业 × 分国家（2026-08-27 用户把纲目倒过来：产业为纲、国家为目）：
        每个产业先一段跨国 Agent 分析，再按国家固定序列出
        ASP 图 + Top 主力机型变动表。"""
        moves = self._moves30(cat_scope)
        names = f.get("country_name") or {}
        cat_zh = f.get("category_name") or {}
        by_cc: dict[str, list] = {}
        for m in moves:
            by_cc.setdefault(m["cc"], []).append(m)

        # ── 第一遍：把**全矩阵**（所有启用国家 × 所有启用产业）的图、表、
        #    事实先算出来（不产出文本）。用户 2026-08-27：
        #    「我需要的是所有国家所有产业，而不是让你挑着放」——
        #    没有达标变动的格子也要出现，用观测底子说明是「没动」还是「没抓到」。
        #    标题要 Agent 写且一次 LLM 调用批量出，所以先集齐事实再拼 markdown。
        cats = ([cat_scope] if cat_scope else self._active_cats())
        ccs = self._active_countries()
        cov = self._coverage30()
        cur_of = {r["code"]: r["currency"]
                  for r in _safe_q("SELECT code, currency FROM country")}
        cells: dict[tuple, dict] = {}          # (cat, cc) → 小节数据
        for cc in ccs:
            per_cat: dict[str, list] = {}
            for m in by_cc.get(cc, []):
                per_cat.setdefault(m["cat"], []).append(m)
            currency = cur_of.get(cc) or ""
            for c in cats:
                ms = per_cat.get(c) or []
                # 同机型同渠道去重，取幅度最大
                seen, uniq = set(), []
                for m in ms:
                    k = (m["model"], m["channel"])
                    if k in seen:
                        continue
                    seen.add(k)
                    uniq.append(m)
                # ★ 主图 = 具体产品价格走势（品牌均价图已废弃，见
                #   _product_trend_chart 的说明）。没有达标变动的小节
                #   也画，用主力在售产品补位。
                pchart = self._product_trend_chart(
                    cc, c, uniq, cat_zh.get(c, c), names.get(cc, cc), currency)
                for m in uniq[:5]:
                    if not m.get("_verdict"):
                        *_, m["_verdict"] = self._mover_series(m)
                cells[(c, cc)] = {"cat": c, "cc": cc, "uniq": uniq, "ms": ms,
                                  "pchart": pchart, "cov": cov.get((cc, c))}
        if not any(X["ms"] or X["pchart"] for X in cells.values()):
            return ""

        # ── 一次调用给所有小节要结论式标题（LLM 挂了就用确定性兜底）──
        heads = self._cat_headlines(cells, names, cat_zh)

        # ── 第二遍：分产业 → 分国家。#### 标题本身就是结论 ——
        #    导出 PPT 时它直接成为页标题（用户：「每页我都不知道你要表达什么」）。
        out = ["## 分产业 × 分国家竞争分析（近 30 天）", ""]
        for c in cats:
            out.append(f"### {cat_zh.get(c, c)}")
            out.append(self._cat_analysis(c, ccs, cells, names, cat_zh))
            out.append("")
            for cc in ccs:
                X = cells[(c, cc)]
                head = heads.get(f"{cc}-{c}") or "本期无达标变动"
                out.append(f"#### {names.get(cc, cc)} ｜ {head}")
                # ★ 主图：具体产品的价格走势（结论式图题）。
                #   台阶=真调价、脉冲=短促，一眼可辨。
                if X.get("pchart") is not None:
                    X["pchart"]["title"] = (f"{names.get(cc, cc)}·"
                                            f"{cat_zh.get(c, c)}：{head}")
                    self._extra_charts.append(X["pchart"])
                    out.append(f"![chart:{X['pchart']['el']}]")
                    out.append("")
                if X["uniq"]:
                    out.append(_table(
                        ["机型", "上市", "变动", "价格（前 → 后）", "渠道", "日期", "变动后续"],
                        [[_label(m["brand"], m["model"]),
                          _age_tag(m["launch_date"]) or "未知",
                          _pct(m["pct"]),
                          f"{_money(m['a'], m['cur'])} → {_money(m['b'], m['cur'])}",
                          (m["channel"] or "—") + ("" if m["off"] else "（第三方）"),
                          str(m["d"])[5:],
                          m.get("_verdict") or "—"] for m in X["uniq"][:5]]))
                else:
                    # ★ 空格子也要说话：用观测底子区分「友商没动」与「我们没抓到」
                    cv = X.get("cov")
                    if cv and cv["obs"]:
                        out.append(
                            f"本期无达标变动（阈值 ±{MOVE_ALERT_PCT:.0f}%）。"
                            f"近 30 天该国该品类有 {cv['obs']:,} 条观测、"
                            f"{cv['sku']} 个在架 SKU（最近 {cv['last_d']}）——"
                            f"**是价格确实平稳，不是没有数据**。")
                    else:
                        out.append("⚠ 近 30 天**没有采到该国该品类的价格观测**，"
                                   "本格空白属于数据缺口，不代表友商没有动作。")
                out.append("")
        out.append(f"> 口径：回看 30 天 · 只列主力与近代机型 —— 上市超过 26 个月的老品"
                   f"已剔除 {getattr(self, '_moves30_dropped_old', 0)} 条变动；"
                   f"「★新」= 上市 ≤13 个月，「未知」= 拿不到上市日期（多为音频/穿戴，"
                   f"规格源不覆盖），保留不加权。")
        if self._extra_charts:
            out.append("> 曲线口径：图上是**具体产品在具体渠道的挂牌价**（本币），"
                       "未观测日延续上次价格（店家不改价，挂牌价就是上次那个），"
                       "延续点画成空心小点。**不再提供品牌均价** —— "
                       "无销量权重的挂牌均价衡量的是货架构成而非成交价"
                       "（实测墨西哥三星手机 134 个 SKU 均值 624 美元、"
                       "中位数仅 376，被折叠屏的多个变体拉飞），容易误导。")
        out.append("")
        return "\n".join(out)

    def _cat_analysis(self, c: str, ccs: list, cells: dict,
                      names: dict, cat_zh: dict) -> str:
        """一个产业的跨国分析段。事实先算好，模型只负责措辞。

        ★ 反混淆纪律（2026-08-27 用户点名：「说整体降幅多少，
          但实际就是下面那个单独的产品有降幅」）：所有幅度都属于**具体产品**，
          必须点名机型，绝不许写成品牌或品类的整体降幅。
        ★ 同日用户砍掉品牌均价（「友商均价肯定都不太准」），所以事实里
          **不再有任何"整体均价"口径的数字**，模型想混都没有原料。"""
        from collections import Counter
        all_ms = [m for cc in ccs for m in cells[(c, cc)]["ms"]]
        down = sum(1 for m in all_ms if (m["pct"] or 0) < 0)
        ch_cnt = Counter(m["channel"] for m in all_ms if m["channel"])
        top_ch, top_n = ch_cnt.most_common(1)[0] if ch_cnt else ("", 0)
        per_cc = []
        for cc in ccs:
            X = cells[(c, cc)]
            mv = X["uniq"][0] if X["uniq"] else None
            # 图上在跟踪的具体产品（供分析点名引用，不是任何均价口径）
            pr = [sr["name"] for sr in ((X.get("pchart") or {})
                                        .get("opt", {}).get("series") or [])][:3]
            fx_bits = ("在跟踪产品：" + "、".join(pr)) if pr else ""
            if not X["ms"]:
                # 空格子如实入账：区分「价格平稳」与「没抓到」，
                # 否则模型会把数据缺口写成"友商没有动作"
                cv = X.get("cov")
                per_cc.append(
                    f"{names.get(cc, cc)}：无达标单品变动"
                    + (f"（{cv['obs']:,} 条观测、{cv['sku']} 个 SKU，价格平稳）"
                       if cv and cv["obs"] else "（**无观测数据，属采集缺口**）")
                    + (f"；{fx_bits}" if fx_bits else ""))
                continue
            per_cc.append(
                f"{names.get(cc, cc)}：{len(X['ms'])} 个单品变动"
                + (f"，最大单品 {_label(mv['brand'], mv['model'])} "
                   f"{mv['pct']:+.1f}%（{mv['channel'] or '?'}"
                   + (f"，{mv['_verdict']}" if mv.get("_verdict") else "")
                   + "）" if mv else "")
                + (f"；{fx_bits}" if fx_bits else ""))
        prompt = (
            f"以下是拉美各国{cat_zh.get(c, c)}品类近 30 天的已算好事实。\n"
            f"★ 所有幅度都是**单件商品在单个渠道**的调价，"
            f"不存在任何品牌或品类的整体均价口径。\n"
            f"共 {len(all_ms)} 个达标单品变动（降价 {down} 个），"
            f"变动最集中的渠道：{top_ch}（{top_n} 个）。\n"
            + "\n".join("- " + t for t in per_cc) +
            "\n\n请写一段不超过 140 字的分析：这个品类当前哪国、哪个品牌的动作"
            "最值得关注？\n硬性纪律：\n"
            "1. **禁止出现「均价」「整体降幅」「普降 X%」这类整体口径** —— "
            "我们没有销量权重，算不出可信的均价；\n"
            "2. 每个幅度都必须点名是哪款机型、哪个渠道；\n"
            "3. 只用上面出现过的数字，不得新增或推算。不要写套话；\n"
            "4. 括号里的「新价已维持N天」= 真调价（台阶），「回升（短促）」= "
            "促销脉冲 —— 判断威胁性时必须区别对待，短促不要当成战略降价；\n"
            "5. 标「无观测数据」的国家要点名说是**我们的采集缺口**，"
            "绝不能写成「该国友商没有动作」。")
        ana = self._ask_prose(f"分析品类{c}", prompt, fallback="")
        if not ana:
            mvs = sorted(all_ms, key=lambda m: -abs(m["pct"] or 0))
            w = mvs[0] if mvs else None
            ana = (f"本品类近 30 天 {len(all_ms)} 个单品达标变动（降价 {down} 个）"
                   + (f"，最大单品 {_label(w['brand'], w['model'])} "
                      f"{w['pct']:+.1f}%（{names.get(w['cc'], w['cc'])}·"
                      f"{w['channel'] or '未知渠道'}）" if w else "")
                   + (f"；变动集中在 {top_ch}。" if top_ch else "。"))
        return _clean_prose(ana)

    def _det_headline(self, uniq: list, cov: dict = None) -> str:
        """确定性兜底标题：点名产品的调价。LLM 不可用时也得有结论。

        ★ 2026-08-27 起不再有任何均价口径（用户砍掉品牌均价），
          标题只讲**具体产品**。
        空格子（无达标变动）也要给一个说人话的标题 —— 全矩阵出报告后
        这类格子会常态存在，标题不能空着或写"无显著变化"了事。
        """
        bits = []
        for m in uniq[:2]:
            bits.append(f"{_label(m['brand'], m['model'])} {m['pct']:+.0f}%"
                        + (f"（{m['_verdict']}）" if m.get("_verdict")
                           else f"（{m['channel'] or '第三方'}）"))
        if bits:
            return "；".join(bits)
        if cov and cov.get("obs"):
            return f"{cov['sku']} 个在架 SKU 本期无达标调价"
        return "本期无观测数据（采集缺口）"

    def _cat_headlines(self, cells: dict, names: dict,
                       cat_zh: dict) -> dict[str, str]:
        """给每个「国家×品类」小节写一行结论式标题。

        ★ 一次 LLM 调用批量写（20 个小节逐个调太慢），输出走**行格式**而非
          JSON —— 中文标题塞 JSON 字符串容易被转义写坏，行格式没有失败面。
        ★ 每个 key 都先备好确定性兜底；模型漏写/写坏哪行，哪行回退，
          绝不因为标题让整节开天窗。
        """
        det: dict[str, str] = {}
        lines = []
        for (c, cc), X in cells.items():
            key = f"{cc}-{c}"
            det[key] = self._det_headline(X["uniq"], X.get("cov"))
            # 图上在跟踪的具体产品（供模型点名，不含任何均价口径）
            pr = [sr["name"] for sr in ((X.get("pchart") or {})
                                        .get("opt", {}).get("series") or [])][:4]
            fx_bits = ("在跟踪产品：" + "、".join(pr)) if pr else ""
            mv = X["uniq"][0] if X["uniq"] else None
            cv = X.get("cov")
            mv_txt = (f"单品最大 {_label(mv['brand'], mv['model'])} "
                      f"{mv['pct']:+.1f}%（{mv['channel'] or '第三方'}"
                      + (f"，{mv['_verdict']}" if mv.get("_verdict") else "")
                      + "）" if mv else
                      (f"无达标单品变动（{cv['sku']} 个 SKU 价格平稳）"
                       if cv and cv.get("obs") else "无观测数据（采集缺口）"))
            lines.append(f"{key} ｜ {names.get(cc, cc)}·"
                         f"{cat_zh.get(c, c)} ｜ "
                         f"{fx_bits or '无跟踪产品'} ｜ {mv_txt}")
        if not lines:
            return det
        order_keys = [ln.split(" ｜ ")[0].strip() for ln in lines]
        prompt = (
            "你在为竞品周报的每个「品类×国家」小节写标题。标题必须直接说结论："
            "谁、什么方向、多大动作 —— 读者只看标题就知道这一节发生了什么。\n"
            "★ 硬性纪律：「均价/整体/普降」只能用于标注为「篮子均价」的数字；"
            "单品的调价必须点名机型，绝不许写成品牌或品类的整体降幅"
            "（用户点名过这种错：单品降 54% 被写成「联想平板整体降 54%」）。\n"
            "禁止「概览」「分析」「情况」这类空词；只准用给到的数字"
            "（把首末均价换算成涨跌幅百分比是允许的）。\n"
            "每行输出一个，行首**原样抄回该行开头的编号**，冒号后写标题"
            "（不超过 26 个字）。例如输入行以「MX-phone ｜ …」开头，就输出：\n"
            "MX-phone: 三星旗舰降价8%，Acme均价守稳中端\n\n"
            + "\n".join(lines))
        raw = self._ask_prose("写小节标题", prompt, fallback="")
        heads = dict(det)
        parsed = []
        for ln in (raw or "").splitlines():
            m = re.match(r"^\s*(\S+?)\s*[:：]\s*(.+?)\s*$", ln)
            if m:
                parsed.append((m.group(1), m.group(2).strip().strip("「」\"'")))
        # 先按编号对上；模型把字面 KEY 抄下来时（实测发生过），
        # 行数对得上就按输入顺序兜底 —— 它是逐行照写的，顺序可信
        by_pos = len(parsed) == len(order_keys)
        for k, (key, t) in enumerate(parsed):
            real = key if key in det else (order_keys[k] if by_pos else None)
            # 上限按混排放宽：拉美品牌名 + 百分号一算，26 个"字"轻松超过
            # 40 个 len() —— 曾把整批合格标题全误杀回兜底
            if real and 4 <= len(t) <= 64:
                heads[real] = t
        return heads

    def _brief_focus(self, ws: str, we: str) -> str:
        """重点关注模块：关注清单里的对象本期发生了什么。

        ★ 与「价格预警」的分工：预警是**打分算出来**的重点；
          这里是**用户自己勾选**的重点（P0/P1/P2）—— 用户点名要盯的东西，
          期内没动作也要如实报告，否则他不知道是没动还是没盯。
        ★ 期内为空时必须回看近 30 天补最近一次动作 —— 报告期刚开始几天时
          全表"无变动"而下文 30 天分析全是变动，读者只会认为报告错了
          （2026-08-25 用户原话：「怎么可能呢？」）。措辞用书面语。
        """
        watches = _safe_q("""SELECT w.id, w.priority,
                               COALESCE(rp.model_name, b.name, w.category_code, '全部') obj,
                               b.name brand, w.country_code cc
                             FROM watchlist w
                             LEFT JOIN rival_product rp ON rp.id=w.rival_product_id
                             LEFT JOIN brand b ON b.id=w.brand_id
                             ORDER BY w.priority, w.id""")
        if not watches:
            return ""
        alerts = _safe_q("""SELECT pa.watch_id, pa.alert_date, pa.direction,
                              pa.change_pct, pa.prev_price, pa.curr_price, pa.currency,
                              rp.model_name m, c.name ch
                            FROM price_alert pa
                            LEFT JOIN rival_product rp ON rp.id=pa.rival_product_id
                            LEFT JOIN channel c ON c.id=pa.channel_id
                            WHERE pa.alert_date BETWEEN ? AND ?
                            ORDER BY ABS(pa.change_pct) DESC""", (ws, we))
        by_watch: dict[int, list] = {}
        for a in alerts:
            by_watch.setdefault(a["watch_id"], []).append(a)
        # 近 30 天每个 watch 最近一次动作（DESC 取首条），期内为空时兜底
        last30: dict[int, dict] = {}
        for a in _safe_q("""SELECT pa.watch_id, pa.alert_date, pa.change_pct,
                              rp.model_name m, c.name ch
                            FROM price_alert pa
                            LEFT JOIN rival_product rp ON rp.id=pa.rival_product_id
                            LEFT JOIN channel c ON c.id=pa.channel_id
                            WHERE pa.alert_date >= date('now','-30 day')
                            ORDER BY pa.alert_date DESC,
                                     ABS(pa.change_pct) DESC"""):
            last30.setdefault(a["watch_id"], a)

        out = ["## 🎯 重点关注（用户清单）", ""]
        rows = []
        for w in watches:
            al = by_watch.get(w["id"], [])
            if al:
                worst = al[0]
                what = (f"{_label(None, worst['m']) or ''} "
                        f"{_pct(worst['change_pct'])}"
                        f"（{_money(worst['prev_price'], worst['currency'])} → "
                        f"{_money(worst['curr_price'], worst['currency'])}，"
                        f"{worst['ch'] or '—'}）"
                        + (f"，另有 {len(al) - 1} 条" if len(al) > 1 else ""))
            else:
                a30 = last30.get(w["id"])
                if a30:
                    what = (f"报告期内无新变动；近 30 天最近一次 "
                            f"{str(a30['alert_date'])[5:]}："
                            f"{_label(None, a30['m']) or ''} "
                            f"{_pct(a30['change_pct'])}（{a30['ch'] or '—'}）")
                else:
                    what = "报告期内及近 30 天均无达标价格变动"
            rows.append([w["priority"], w["obj"], w["cc"] or "全部", what])
        out.append(_table(["优先级", "对象", "国家", "本期动态"], rows))
        out.append("")
        return "\n".join(out)

    def _brief_market_dynamics(self, ws: str, we: str) -> str:
        """市场动态模块：开店 / 发布会 / 合作 / 扩张 / 退出。

        ★ 数据来源是 brandintel 的动态库。采集侧早就在搜
          （ACTION_QUERIES 里有 tienda/inaugura/evento…），
          之前缺的是打标吞吐 —— 现在生成报告前先跑一遍确定性预打标，
          保证存量也能进来。
        ★ 每条都带信息源，可点回原文核对（上市看板同款纪律）。
        """
        try:
            from .brandintel import pretag_dynamics
            pretag_dynamics()
        except Exception as e:                        # noqa: BLE001
            log.warning("预打标失败（模块仍出，只用已有标签）: %s", str(e)[:80])

        MARKET_TAGS = ("开店", "发布会", "合作", "扩张", "退出", "供应链")
        qs = ",".join("?" * len(MARKET_TAGS))
        rows = _safe_q(f"""SELECT d.tag, d.title, d.summary_zh, d.url,
                             d.published_at, b.name brand, d.country_code cc,
                             co.name_zh cname, d.source_name src
                           FROM dynamics d
                           LEFT JOIN brand b ON b.id=d.brand_id
                           LEFT JOIN country co ON co.code=d.country_code
                           WHERE d.tag IN ({qs})
                             AND IFNULL(d.published_at, date(d.created_at))
                                 BETWEEN ? AND ?
                           ORDER BY d.tag, d.published_at DESC
                           LIMIT 30""", (*MARKET_TAGS, ws, we))
        out = ["## 🏪 市场动态（开店 / 发布会 / 合作）", ""]
        if not rows:
            out.append("本期没有识别到开店、发布会、合作类的市场动态。"
                       "⚠ 采集侧在持续搜索这类信息（每品牌 × 每国），"
                       "空白代表信源里没出现，不代表一定没发生。")
            out.append("")
            return "\n".join(out)
        tbl = []
        for r in rows:
            when = str(r["published_at"] or "")[:10]
            what = (r["summary_zh"] or r["title"] or "")[:64]
            src = f"[{r['src'] or '来源'}]({r['url']})" if r["url"] else (r["src"] or "—")
            tbl.append([r["tag"], r["brand"] or "—", r["cname"] or r["cc"] or "全球",
                        when, what, src])
        out.append(_table(["类型", "品牌", "国家", "日期", "内容", "信息源"], tbl))
        out.append("")
        return "\n".join(out)

    def _assemble_brief(self, title: str, kind: str, ws: str, wei: str,
                        scope_norm: str, cat_zh: str, f: dict, top: list[dict],
                        alerts: list[dict], charts: list[dict],
                        dropped: int) -> str:
        m = f["metrics"]
        self._extra_charts = []          # 分国段产生的 ASP 图挂在这，run() 里并回 charts
        cat_scope = scope_norm.split(":")[-1] if ":" in scope_norm else ""
        prose = self._brief_prose(f, top, alerts, kind, cat_zh, cat_scope)

        # ★ 超了就按句截断，而不是硬切字符 —— 半句话结尾比少一句更难读
        if _zh_len(prose) > MAX_SUMMARY_CHARS:
            keep = []
            for seg in re.split(r"(?<=[。！？\n])", prose):
                if _zh_len("".join(keep + [seg])) > MAX_SUMMARY_CHARS:
                    break
                keep.append(seg)
            prose = "".join(keep).rstrip() or prose[:MAX_SUMMARY_CHARS]

        parts = [f"# {title}", ""]
        parts.append(f"> {PERIOD_ZH[kind]} · {ws} ~ {wei} · "
                     f"{m['countries']} 国 {m['channels']} 渠道 · {cat_zh}")
        parts.append("")
        parts.append(prose)
        parts.append("")

        if alerts:
            # 标题带结论：几起、最狠的是谁 —— 导出 PPT 时这行就是页标题
            worst = max(alerts, key=lambda a: abs(a.get("pct") or 0))
            parts.append(f"## ⚠ 价格预警：{len(alerts)} 起打到我方对位，"
                         f"最大 {_label(worst.get('brand'), worst.get('model'))} "
                         f"{_pct(worst['pct'])}（{worst['channel'] or '第三方'}）")
            parts.append(_table(
                ["机型", "国家", "变动", "前 → 后", "渠道"],
                [[_label(a.get("brand"), a.get("model")), a["cc"],
                  _pct(a["pct"]),
                  f"{_money(a['prev'], a['currency'])} → {_money(a['curr'], a['currency'])}",
                  a["channel"] or "—"] for a in alerts]))
            parts.append("")

        # 重点关注：预警表之后、其余变化之前 —— 用户点名要盯的东西优先级最高
        focus = self._brief_focus(ws, wei)
        if focus:
            parts.append(focus)

        rest = [mv for mv in top[:TABLE_N] if not mv.get("_hits_us")]
        if rest:
            big = max(rest, key=lambda mv: abs(mv.get("change_pct") or 0))
            parts.append(f"## 其余重点变化（{len(rest)} 条，最大 "
                         f"{_label(big.get('brand'), big.get('model'))} "
                         f"{_pct(big.get('change_pct'))}）")
            parts.append(_table(
                ["机型", "国家", "产业", "变动", "渠道类型"],
                [[_label(mv.get("brand"), mv.get("model")),
                  mv.get("country_code"),
                  f["category_name"].get(mv.get("cat"), mv.get("cat") or "—"),
                  _pct(mv.get("change_pct")),
                  "官方" if mv.get("is_official") else "第三方"] for mv in rest]))
            parts.append("")

        parts.append(self._brief_country_cat(f, cat_scope=cat_scope))
        parts.append("")
        parts.append(self._brief_voc_country(f))
        parts.append(self._brief_market_dynamics(ws, wei))

        for c in charts:
            parts.append(f"## {c['title']}")
            parts.append(f"![chart:{c['el']}]")
            parts.append("")

        # 口径脚注：不计入字数，但必须有 —— 读者要知道这 500 字是怎么选出来的
        foot = [f"---",
                f"口径：正文只讲重要度最高的 {HEADLINE_N} 条。重要度 = 幅度 × "
                f"打到我方对位(×2.5) × 官方渠道(×1.5)；"
                f"低于 {MOVE_ALERT_PCT:.0f}% 的变动视为噪声不计。"]
        if dropped:
            foot.append(f"本期有 **{dropped}** 条变动因幅度超出该品类合理区间被排除"
                        f"（多为分期月供或变体串档），未计入以上任何数字。")
        if m.get("silent_channels"):
            foot.append(f"另有 {m['silent_channels']} 个渠道本期无数据 —— "
                        f"其空白是我们没抓到，不代表友商没动作。")
        parts.extend(foot)
        return "\n".join(parts)

    def _assemble(self, title: str, ws: str, we: str, eff_end: str,
                  scope: str, f: dict, prose: dict) -> str:
        m, g = f["metrics"], f["gaps"]
        cn, catn = f["country_name"], f["category_name"]
        p: list[str] = [f"# {title}", ""]

        head = [f"**统计区间**：{ws} ~ {we}",
                f"**范围**：{'全部国家与品类' if scope == 'all' else scope}",
                f"**生成时间**：{db.now()}"]
        if eff_end < we:
            head.append(f"**本周尚未结束，数据统计截至 {eff_end}**")
        p.append(" ｜ ".join(head))
        p.append("")
        p.append("> 口径说明：各国币种不同，绝对价格跨国不可比，"
                 "跨国比较一律只看百分比；价格变动区分**官方渠道**"
                 "（品牌官网 / 渠道自营，含零售商自营）与**第三方卖家**"
                 " —— 前者可信度高得多，但渠道自营调价是**该渠道**的动作，"
                 "只有品牌官网价才代表厂商本身。")
        p.append("")
        p.append("> 读数须知：「降价数 / 涨价数」的分母不是「观测数」。"
                 "价格变动需要同一渠道、同一 SKU 至少两个观测日才能检出，"
                 "本周只抓到一天的渠道无论观测量多大都记 0 —— "
                 "**0 不等于没动价**，逐国表格下方与文末数据缺口会点名这些渠道。")
        p.append("")

        # ---- 一、本周概览
        p += ["## 一、本周概览", "", prose["overview"] or "（本段点评生成失败，数据见下表）", ""]
        p.append(_table(["指标", "数值"], [
            ["价格观测", f"{m['obs_rows']} 条 / {m['obs_days']} 天"],
            ["覆盖", f"{m['countries']} 国 · {m['channels']} 渠道 · "
                     f"{m['brands']} 品牌 · {m['products']} 产品"],
            ["官方渠道 : 第三方", f"{m['official_rows']} : {m['third_party_rows']} 条"],
            ["价格变动", f"{m['moves_total']} 个（降 {m['moves_down']} / "
                        f"涨 {m['moves_up']}）"],
            ["其中官方渠道", f"降 {m['moves_down_official']} / 涨 {m['moves_up_official']}"],
            ["其中第三方", f"降 {m['moves_down_third']} / 涨 {m['moves_up_third']}"],
            # 0 个的时候不挂"疑为首次入库"的旗 —— 那句提醒只对有条目时才有意义
            ["新出现产品", f"{m['new_products']} 个"
                          + ("" if m["new_products_reliable"] or not m["new_products"]
                             else "（★ 疑为首次入库）")],
            ["策略信号 / 品牌动态", f"{m['signals']} 条 / {m['dynamics']} 条"],
            ["本周新增评论", f"{m['reviews']} 条"],
        ]))
        p += ["", "**数据健康度**", ""]
        p.append(_table(["检查项", "数值", "含义"], [
            ["产品归一化率", f"{m['link_rate_pct']}%",
             f"未挂接产品 {m['unlinked_rows']} 条，这部分进不了按产品的分析"],
            ["缺价格", f"{m['no_price_rows']} 条", "页面未取到售价"],
            ["价格审计", f"待审 {m['audit_pending']} / 通过 {m['audit_accepted']} /"
                        f" 剔除 {m['audit_rejected']}",
             "待审 = 价格审计 Agent 还没跑到；本报告用的是未审数据"
             # 概览的 obs_rows 故意不过滤 audit_status（要能看见剔除了多少），
             # 而逐国表格过滤了 rejected。剔除数 >0 时两者必然对不上，
             # 不说明白就会变成"表格加起来和总数不一样"的信任问题。
             + (f"；上方「价格观测 {m['obs_rows']} 条」为未过滤总数，"
                f"逐国表格已剔除 {m['audit_rejected']} 条被审计判否的观测，"
                f"故各国相加为 {m['obs_rows'] - m['audit_rejected']} 条"
                if m["audit_rejected"] else "")],
            ["采集天数", f"{m['obs_days']} 天", f"缺 {m['missing_days']} 天"],
            ["活跃 / 静默渠道", f"{m['active_channels']} / {m['silent_channels']}",
             "静默渠道的品类在本报告中无法判断动向"],
            ["可比 / 单日快照渠道",
             f"{m['comparable_channels']} / {m['snapshot_channels']}",
             f"单日快照渠道本周只抓到 1 天（{m['snapshot_obs_rows']} 条观测），"
             "同 SKU 无前一天可比，结构上不可能检出价格变动 —— "
             "它们的「降价 0」是没测出来，不是没发生"],
            ["可比 SKU（变动数的真分母）",
             f"{m['comparable_skus']} / {m['total_skus']}",
             f"只有 {m['comparable_skus']} 个 SKU×渠道 组合拿得出两个观测日，"
             f"**本周最多只可能检出 {m['comparable_skus']} 个价格变动**"
             f"（实检出 {m['moves_total']} 个）。"
             "拿「观测数」当降价数的分母会严重高估覆盖"],
        ] + ([["自相矛盾的变动记录", f"{m['moves_inconsistent']} 条（已剔除）",
               "原价/现价/幅度三个数对不上（price_move 重复写入所致），"
               "留着会让明细表当场自打脸，故不计入本报告"]]
             if m["moves_inconsistent"] else [])))
        p.append("")

        # ---- 二、按国家 × 品类
        p += ["## 二、按国家 × 品类的竞争动态", ""]
        codes = sorted({r["country_code"] for r in f["obs_grid"]}
                       | {r["country_code"] for r in f["move_grid"]})
        if not codes:
            p += ["本周没有任何国家有观测数据。**这是数据缺口，不是市场没有动作。**", ""]
        for code in codes:
            p.append(f"### {cn.get(code, code)} {code}")
            p.append("")
            if prose["countries"].get(code):
                p += [prose["countries"][code], ""]
            grid_rows = []
            mv_by_cat = {r["cat"]: r for r in f["move_grid"]
                         if r["country_code"] == code}
            for r in [x for x in f["obs_grid"] if x["country_code"] == code]:
                mvr = mv_by_cat.get(r["cat"], {})
                grid_rows.append([
                    catn.get(r["cat"], r["cat"]), r["n"], r["ch_n"], r["prod_n"],
                    f"{mvr.get('down_n', 0)}（官{mvr.get('down_off', 0)}/三{mvr.get('down_3p', 0)}）",
                    f"{mvr.get('up_n', 0)}（官{mvr.get('up_off', 0)}/三{mvr.get('up_3p', 0)}）",
                    _pct(mvr.get("max_down_pct")), _pct(mvr.get("max_up_pct"))])
            # 只有变动、没有观测的品类（跨周比价会出现）也要补进表里，否则会漏
            for cat_code, mvr in mv_by_cat.items():
                if not any(x["cat"] == cat_code for x in f["obs_grid"]
                           if x["country_code"] == code):
                    grid_rows.append([
                        catn.get(cat_code, cat_code), 0, 0, 0,
                        f"{mvr.get('down_n', 0)}（官{mvr.get('down_off', 0)}/三{mvr.get('down_3p', 0)}）",
                        f"{mvr.get('up_n', 0)}（官{mvr.get('up_off', 0)}/三{mvr.get('up_3p', 0)}）",
                        _pct(mvr.get("max_down_pct")), _pct(mvr.get("max_up_pct"))])
            p.append(_table(["品类", "观测", "渠道", "产品", "降价数", "涨价数",
                             "最大降幅", "最大涨幅"], grid_rows)
                     or "本周该国无观测数据。")
            # ★ 警告必须紧贴表格。读者是顺着「观测 1748 / 降价 0」这一行读的，
            #   把提醒放到文末数据缺口，等于没提醒。
            comp = g["comparability"].get(code) or {}
            n_cmp = comp.get("comparable")
            if n_cmp is not None and (n_cmp < 30 or comp.get("snap_ch")):
                p.append("")
                p.append(f"> ⚠ **「降价数/涨价数」的分母不是「观测」列**："
                         f"该国 {comp.get('sku_total', 0)} 个 SKU×渠道 组合里，"
                         f"本周只有 **{n_cmp} 个**拿得出两个观测日、能比出变动"
                         + (f"；{comp.get('snap_obs', 0)} 条观测（{comp.get('snap_pct', 0)}%）"
                            f"来自 {comp.get('snap_ch', 0)} 个只抓到 1 天的渠道"
                            if comp.get("snap_ch") else "")
                         + f"。本周该国最多只可能检出 {n_cmp} 个变动，"
                         "上表的 0 是**没测出来**，不能读成「友商没动价」。")
            p.append("")

            mvs = [r for r in f["moves"] if r["country_code"] == code][:TOP_MOVES]
            if mvs:
                p += ["**幅度最大的价格变动**", ""]
                p.append(_table(
                    ["品类", "渠道属性", "品牌 / 产品", "渠道", "原价 → 现价", "幅度", "间隔"],
                    [[catn.get(r["cat"], r["cat"]),
                      "官方" if r["is_official"] else "第三方",
                      f"{r['brand']} {r['model']}"[:42], (r["channel"] or "—")[:22],
                      f"{_money(r['prev_price'], r['currency'])} → "
                      f"{_money(r['curr_price'], r['currency'])}",
                      _pct(r["change_pct"]),
                      f"{r['days_span']} 天" if r["days_span"] is not None else "—"]
                     for r in mvs]))
                p.append("")

            new = [r for r in f["new_products"] if r["country_code"] == code][:TOP_NEW]
            if new:
                p += ["**本周新出现的产品**", ""]
                if not f["new_is_reliable"]:
                    p += [f"> ⚠ 全库最早观测日为 {f['history_start']}（共 {f['history_days']} 天），"
                          "本周包含建库首日 —— 下列产品多半是**首次入库**而非新上市，"
                          "需要更长的历史才能区分。", ""]
                p.append(_table(["品类", "品牌 / 产品", "首见日", "本周观测数"],
                                [[catn.get(r["cat"], r["cat"]),
                                  f"{r['brand']} {r['name']}"[:48],
                                  r["first_seen"], r["obs_n"]] for r in new]))
                p.append("")

        # ---- 三、策略信号
        p += ["## 三、策略信号", "", prose["signals"] or "（本段生成失败）", ""]
        if f["signals"]:
            p.append(_table(["日期", "国家", "品牌 / 产品", "信号", "置信度", "结论", "建议动作"],
                            [[s["signal_date"], s["country_code"],
                              f"{s['brand']} {s['model']}".strip()[:32],
                              s["signal_type"],
                              "—" if s["confidence"] is None else f"{s['confidence']:.2f}",
                              (s["summary_zh"] or "")[:60],
                              (s["suggested_action"] or "—")[:40]]
                             for s in f["signals"][:15]]))
            # 表格是截断的，必须说清楚 —— 否则读者会把"15 条"当成本周信号总数
            if m["signals"] > min(15, m["signals_listed"]):
                p.append("")
                p.append(f"> 本周共 {m['signals']} 条策略信号，上表按置信度降序只列前 "
                         f"{min(15, m['signals_listed'])} 条。")
        else:
            p.append("本周 `strategy_signal` 表内没有落在 "
                     f"{ws}~{we} 的信号。**没有信号 ≠ 友商没有策略动作**，"
                     "只说明信号 Agent 本周没有产出。")
        p.append("")

        # ---- 四、品牌动态
        p += ["## 四、品牌动态", ""]
        if f["dynamics"]:
            p.append(_table(["日期", "国家", "品牌", "标签", "重要度", "内容"],
                            [[str(d["at"])[:10], d["country_code"] or "—",
                              d["brand"] or "—", d["tag"] or "—", d["importance"],
                              (d["summary_zh"] or d["title"] or "")[:70]]
                             for d in f["dynamics"][:15]]))
            if m["dynamics"] > min(15, m["dynamics_listed"]):
                p.append("")
                p.append(f"> 本周共 {m['dynamics']} 条品牌动态，上表按重要度降序只列前 "
                         f"{min(15, m['dynamics_listed'])} 条。")
        else:
            p.append("本周 `dynamics` 表内没有开店 / 发布会 / 营销类情报入库。"
                     "这是**情报源缺口**（情报 Agent 本周未产出），"
                     "不能据此判断友商没有市场动作。")
        p.append("")

        # ---- 五、对Acme的影响与建议
        p += ["## 五、对Acme的影响与建议", "", prose["advice"] or "（本段生成失败）", ""]
        if f["threats"] or f["chances"]:
            p += ["**对位竞品本周动作明细**", ""]
            rows = []
            for t in (f["threats"][:8] + f["chances"][:8]):
                rows.append([
                    "威胁" if t["move_pct"] < 0 else "机会",
                    t["country_code"], t["my_name"][:20],
                    f"{t['brand']} {t['rival_name']}"[:34],
                    "官方" if t["is_official"] else "第三方",
                    _pct(t["move_pct"]), _pct(t["price_gap_pct"]),
                    (_pct(t["est_gap_pct"]) + "（估算）"
                     if t["est_gap_pct"] is not None else "币种不一致，不估算")])
            p.append(_table(["性质", "国家", "我方产品", "对位竞品", "渠道属性",
                             "本周变动", "原价差", "现价差"], rows))
            p += ["", "> 价差为「竞品相对我方」：正值＝竞品更贵，负值＝竞品更便宜。"
                  "「现价差」按变动后的渠道现价估算，与原价差可能来自不同渠道口径，"
                  "仅供判断方向。"]
        p.append("")

        # ---- 六、数据缺口
        p += ["## 六、数据缺口（本周没抓到的地方）", "",
              "**这一节的意义：下面列出的范围本周没有数据，"
              "任何「没有动静」的结论都不适用于它们。**", ""]
        if g["silent_channels"]:
            p += [f"已启用但本周零采集的渠道（{len(g['silent_channels'])} 个）：", ""]
            p.append(_table(["国家", "渠道", "类型", "上次抓到"],
                            [[c["country_code"], c["name"], c["kind"],
                              c["last_seen"] or "从未抓到过"]
                             for c in g["silent_channels"]]))
            p.append("")
        if g["snapshot_channels"]:
            p += [f"本周只抓到 1 天的渠道（{len(g['snapshot_channels'])} 个，"
                  f"共 {m['snapshot_obs_rows']} 条观测）—— "
                  "**有数据，但测不出价格变动**：", ""]
            p.append(_table(["国家", "渠道", "类型", "本周观测", "本周采集天数"],
                            [[cn.get(c["country_code"], c["country_code"]),
                              c["name"], c["kind"] or "—", c["n"], c["days"]]
                             for c in g["snapshot_channels"]]))
            p += ["", "> 价格变动的检出前提是同一渠道、同一 SKU 至少两个观测日"
                  "（见 `pricemove.py`）。上列渠道本周只有一个观测日，"
                  "无论观测量多大，都不可能产出任何变动记录。"
                  "报告中与这些渠道相关的「降价 0 / 涨价 0」一律只能读作"
                  "**本周未测量**，不能读作「价格没动」。", ""]
        if g["silent_countries"]:
            p.append("整周无任何数据的国家："
                     + "、".join(f"{cn.get(c, c)}({c})" for c in g["silent_countries"]))
            p.append("")
        if g["missing_days"]:
            p.append("完全没有观测的日期：" + "、".join(g["missing_days"]))
            p.append("")
        if g["missing_pairs"]:
            p += ["历史上抓到过、本周却一条都没有的「国家 × 品类」：", ""]
            p.append(_table(["国家", "品类", "上次抓到"],
                            [[cn.get(x["country_code"], x["country_code"]),
                              catn.get(x["cat"], x["cat"]), x["last_seen"]]
                             for x in g["missing_pairs"][:25]]))
            p.append("")
        if not (g["silent_channels"] or g["silent_countries"] or g["snapshot_channels"]
                or g["missing_days"] or g["missing_pairs"]):
            p.append("本周所有已启用渠道均有数据入库，无明显缺口。")
            p.append("")

        p.append("---")
        p.append("*本报告的全部数字由 SQL 直接聚合，模型只负责文字组织；"
                 "每一步的提示词与原始返回都留痕在 `agent_step` 表，可逐条复盘。*")
        return "\n".join(p)

    # ============================================================ 落库

    def _save(self, ws: str, we: str, scope: str, title: str, content: str,
              highlights: list[str], metrics: dict) -> int | None:
        """同一周同一范围只保留一份（周内重跑就覆盖），避免出现两份互相矛盾的周报。"""
        try:
            with db.tx() as conn:
                conn.execute("""
                    INSERT INTO weekly_report(week_start,week_end,scope,title,
                        content_md,highlights,metrics)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(week_start,scope) DO UPDATE SET
                      week_end=excluded.week_end, title=excluded.title,
                      content_md=excluded.content_md,
                      highlights=excluded.highlights, metrics=excluded.metrics,
                      created_at=datetime('now')
                """, (ws, we, scope, title, content,
                      json.dumps(highlights, ensure_ascii=False),
                      json.dumps(metrics, ensure_ascii=False)))
            row = db.q1("SELECT id FROM weekly_report WHERE week_start=? AND scope=?",
                        (ws, scope))
            rid = row["id"] if row else None
        except sqlite3.Error as e:
            # 落库失败也要把正文还给调用方（界面/Telegram 仍能用），只是无法回溯
            self.log_step("周报落库", decision="error", status="degraded",
                          reason=f"写 weekly_report 失败: {str(e)[:150]}")
            return None
        self.log_step("周报落库", input_ref=f"weekly_report:{rid}",
                      parsed={"id": rid, "chars": len(content),
                              "highlights": len(highlights)},
                      decision="ok",
                      reason="同周同范围覆盖写入，保证「本周周报」只有一份")
        return rid
