# -*- coding: utf-8 -*-
"""中央主 Agent（Chief）—— 抓取之前先研判，产出本轮抓取计划。

用户要求原话："所有的 agent 在抓取之前要有一个中央的主 agent 去判断抓的质量。"

它做四件事：
  1. 读渠道健康档案（channel_health）与上几轮的采集单元记录
  2. 研判每个渠道当前什么状态：健康 / 选择器失效 / 被风控 / 需登录 / 已死
  3. 决定本轮怎么抓：正常抓 / 降频 / 直接走兜底引擎 / 本轮跳过
  4. 把计划与研判理由写进 crawl_plan，全程留痕

★ 为什么规则先行、模型只做补充：
  "连续 3 天 empty" 这种判断用规则又快又准又可复现，调模型纯属浪费。
  模型的价值在于解释成因、以及规则没覆盖的组合情况（比如"这个渠道
  昨天好好的今天突然全空，但同国其它渠道都正常" —— 指向站点改版）。
"""
from __future__ import annotations

import json
import logging

from .. import db
from .base import BaseAgent
from .llm import as_dict

log = logging.getLogger("chief")

# 决策动作
ACT_NORMAL = "normal"          # 正常抓
ACT_SLOW = "slow_down"         # 降频（拉长间隔、减少页数）
ACT_FALLBACK = "use_fallback"  # 直接上 Selenium，不浪费一轮 Playwright
ACT_SKIP = "skip"              # 本轮跳过
ACT_NEED_LOGIN = "need_login"  # 需要登录态/官方 token，跳过并提示用户

LOOKBACK_DAYS = 7


class ChiefAgent(BaseAgent):
    name = "chief"
    role = "chief"
    description = "抓取前研判渠道质量，产出本轮抓取计划"

    def run(self, mode: str = "daily", rotation_category: str | None = None) -> dict:
        self.start(f"研判抓取计划 mode={mode} 轮值产业={rotation_category}")

        channels = db.q("""
            SELECT c.*, co.name_zh AS country_name, co.sort_order
            FROM channel c JOIN country co ON co.code = c.country_code
            WHERE c.enabled = 1 AND co.enabled = 1
            ORDER BY co.sort_order, c.priority
        """)
        history = self._load_history()

        decisions = []
        for ch in channels:
            d = self._judge(ch, history.get(ch["id"], {}))
            decisions.append(d)
            self.log_step(
                f"研判 {ch['country_code']}/{ch['name']}",
                input_ref=f"channel:{ch['id']}",
                parsed={"verdict": d["verdict"], "action": d["action"],
                        "evidence": d["evidence"]},
                decision=d["action"], reason=d["reason"])

        # 模型只用来解释"规则说不清"的那部分，并给全局建议
        anomalies = [d for d in decisions if d["action"] != ACT_NORMAL]
        advice = ""
        if anomalies and self.llm and self.llm.available():
            advice = self._ask_advice(anomalies)

        plan = self._build_plan(mode, rotation_category, decisions, advice)
        plan_id = self._save_plan(plan)

        n_norm = sum(1 for d in decisions if d["action"] == ACT_NORMAL)
        summary = (f"共 {len(decisions)} 个渠道：正常 {n_norm}，"
                   f"降频 {sum(1 for d in decisions if d['action'] == ACT_SLOW)}，"
                   f"直接兜底 {sum(1 for d in decisions if d['action'] == ACT_FALLBACK)}，"
                   f"跳过 {sum(1 for d in decisions if d['action'] in (ACT_SKIP, ACT_NEED_LOGIN))}")
        self.finish("ok", summary, len(channels), n_norm)
        plan["plan_id"] = plan_id
        plan["summary"] = summary
        return plan

    # ------------------------------------------------ 历史

    def _load_history(self) -> dict:
        """每个渠道近 N 天的采集表现"""
        rows = db.q(f"""
            SELECT su.channel_id,
                   COUNT(*) AS attempts,
                   SUM(CASE WHEN su.status='ok' THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN su.status='empty' THEN 1 ELSE 0 END) AS empty,
                   SUM(CASE WHEN su.status='irrelevant' THEN 1 ELSE 0 END) AS irrelevant,
                   SUM(CASE WHEN su.status='blocked' THEN 1 ELSE 0 END) AS blocked,
                   SUM(CASE WHEN su.status='login_wall' THEN 1 ELSE 0 END) AS login_wall,
                   SUM(CASE WHEN su.status='failed' THEN 1 ELSE 0 END) AS failed,
                   AVG(su.items) AS avg_items,
                   MAX(su.created_at) AS last_at
            FROM scrape_unit su
            JOIN scrape_run r ON r.id = su.run_id
            WHERE r.run_date >= date('now','-{LOOKBACK_DAYS} day')
            GROUP BY su.channel_id
        """)
        hist = {r["channel_id"]: r for r in rows}

        # 体检结果（doctor 命令写的）也算证据
        for r in db.q(f"""
            SELECT channel_id, verdict, suggested_action, avg_items, check_date
            FROM channel_health
            WHERE check_date >= date('now','-{LOOKBACK_DAYS} day')
            ORDER BY check_date DESC
        """):
            hist.setdefault(r["channel_id"], {}).setdefault("health_verdict", r["verdict"])
            hist[r["channel_id"]].setdefault("health_action", r["suggested_action"])
        return hist

    # ------------------------------------------------ 规则研判

    def _judge(self, ch: dict, h: dict) -> dict:
        cid, cname = ch["id"], f"{ch['country_code']}/{ch['name']}"
        attempts = h.get("attempts") or 0
        ok = h.get("ok") or 0
        empty = h.get("empty") or 0
        irrelevant = h.get("irrelevant") or 0
        blocked = h.get("blocked") or 0
        wall = h.get("login_wall") or 0
        failed = h.get("failed") or 0
        hv = h.get("health_verdict")
        ha = h.get("health_action")

        base = {"channel_id": cid, "channel": cname, "country": ch["country_code"],
                "adapter": ch["adapter"], "priority": ch["priority"],
                "evidence": {"近7天次数": attempts, "成功": ok, "空": empty,
                             "串味": irrelevant, "被拦": blocked, "登录墙": wall,
                             "失败": failed, "体检结论": hv}}

        # 没有任何历史 → 首次抓，正常走
        if attempts == 0 and not hv:
            return {**base, "verdict": "unknown", "action": ACT_NORMAL,
                    "reason": "无历史记录，首轮按正常策略抓取并观察"}

        # 登录墙：换设备重试也过不去，必须人工介入
        if wall > 0 or hv == "need_login" or (hv == "rate_limited"
                                              and ha == "need_login"):
            return {**base, "verdict": "need_login", "action": ACT_NEED_LOGIN,
                    "reason": (f"{cname} 命中强制登录墙 {wall} 次。换设备/换引擎都无效，"
                               f"需配置官方 API token 或保存一次登录态。本轮跳过，"
                               f"避免无谓请求把 IP 拉黑。")}

        # ★ 串味（搜索没生效，抓回来的是别的品类）—— 优先级仅次于登录墙。
        #   这是最危险的一类失败：它**报告成功**（页面 200、解析出商品、有价格），
        #   但抓的是首页推荐位的运动鞋和床垫。不拦住就会污染价格基线，
        #   而且下游看到的是"这个渠道很健康"。
        # ★ 判据是**比例**不是"出现过一次"。
        #   原来写成 irrelevant > 0：24 次里错 1 次就永久跳过整个渠道
        #   （实测 Alkosto 1/24、Falabella CO 1/18 因此被停掉，而它们是好的）。
        #   偶发一次串味可能只是某个搜索词恰好没货、站点返回了推荐位 ——
        #   拿它判死整个渠道，代价是那个国家整段数据消失，比放进来几条脏数据大得多。
        #   Hiraoka 11/37（30%）才是真的搜索坏了，那种必须拦。
        irr_rate = irrelevant / attempts if attempts else 0
        if irrelevant >= 3 and irr_rate >= 0.25:
            return {**base, "verdict": "selector_broken", "action": ACT_SKIP,
                    "reason": (f"{cname} 近 {attempts} 次里有 {irrelevant} 次"
                               f"（{irr_rate:.0%}）判定为「搜索未生效」——"
                               f"页面能打开、也解析出了带价格的商品，"
                               f"但抓回来的不是目标品类（典型是首页推荐位）。"
                               f"这类数据进库会污染价格基线且不报错，"
                               f"必须先修 search_url。本轮跳过。")}
        if irrelevant > 0:
            # 偶发串味：照抓，但记一笔，方便回头看是不是在恶化
            base = {**base, "note": (f"近 {attempts} 次里有 {irrelevant} 次串味"
                                     f"（{irr_rate:.0%}），未到跳过线，继续观察")}

        # 连续空 = 选择器/URL 失效，不是"友商没上新"
        if attempts >= 2 and empty == attempts:
            return {**base, "verdict": "selector_broken", "action": ACT_SKIP,
                    "reason": (f"{cname} 近 {attempts} 次全部解析为空。页面能打开却一条"
                               f"都解析不出 —— 这是选择器/URL 失效的典型特征，"
                               f"不是该渠道真的没商品。需要修适配器，本轮跳过。")}
        if hv == "selector_broken":
            return {**base, "verdict": "selector_broken", "action": ACT_SKIP,
                    "reason": f"{cname} 体检判定选择器失效，需修适配器后再启用"}
        # ★ 体检说"空但查不出原因"时不要跳过。
        #   empty 是三义的（选择器坏 / 没认出来的拦截 / 这个词真没货），
        #   拿不准就跳过等于把一个可能健康的渠道judged死 ——
        #   实测 Ripley 秘鲁被误判成选择器坏，白白停抓。让它照常跑一轮再看。
        if hv == "empty":
            return {**base, "verdict": "unknown", "action": ACT_NORMAL,
                    "reason": (f"{cname} 体检解析为空但未检出拦截特征，原因待查"
                               f"（可能是选择器、也可能是探测词恰好无货）。"
                               f"本轮照常抓取以获取更多证据，不预先跳过。")}

        # 被风控：先降频，严重的直接上兜底引擎
        if attempts > 0 and blocked / max(attempts, 1) >= 0.5:
            return {**base, "verdict": "rate_limited", "action": ACT_FALLBACK,
                    "reason": (f"{cname} 近 {attempts} 次里 {blocked} 次被人机验证拦截"
                               f"（{blocked / attempts:.0%}）。Playwright 已难以通过，"
                               f"本轮直接走 Selenium 兜底，省掉一轮必然失败的尝试。")}
        if blocked > 0:
            return {**base, "verdict": "degraded", "action": ACT_SLOW,
                    "reason": (f"{cname} 近期有 {blocked} 次被拦。本轮降频抓取"
                               f"（拉长间隔、减少页数），避免升级成持续封锁。")}

        if failed > 0 and failed >= attempts * 0.5:
            return {**base, "verdict": "dead", "action": ACT_SKIP,
                    "reason": (f"{cname} 近 {attempts} 次有 {failed} 次连页面都打不开。"
                               f"多半是 URL 变了或该站需要代理，本轮跳过待核。")}

        avg = h.get("avg_items") or 0
        if ok > 0 and avg < 2:
            return {**base, "verdict": "degraded", "action": ACT_SLOW,
                    "reason": (f"{cname} 能抓通但平均只有 {avg:.1f} 条/次，偏少。"
                               f"本轮正常抓但标记观察，若持续偏低需核对搜索词。")}

        return {**base, "verdict": "healthy", "action": ACT_NORMAL,
                "reason": f"{cname} 近 {attempts} 次成功 {ok} 次、平均 {avg:.0f} 条，健康"}

    # ------------------------------------------------ 模型补充

    def _ask_advice(self, anomalies: list[dict]) -> str:
        lines = [f"- {d['channel']}（{d['verdict']}）：{json.dumps(d['evidence'], ensure_ascii=False)}"
                 for d in anomalies[:20]]
        prompt = (
            "你是竞品数据采集系统的调度负责人。下面是今天各渠道的异常情况统计。\n"
            "请判断：①这些异常之间有没有共同成因（比如同一天多国同时被拦，"
            "更可能是出口 IP 被风控，而不是各站点各自改版）；"
            "②给出本轮最重要的 2~3 条处置建议。\n\n"
            + "\n".join(lines) +
            '\n\n只返回 JSON：{"root_cause":"共同成因判断，没有就写无",'
            '"advice":["建议1","建议2"]}')
        parsed = as_dict(self.ask_json("研判共同成因", prompt,
                                       system="你是严谨的数据工程负责人，说话简洁具体。",
                                       default={}))
        root = parsed.get("root_cause") or ""
        advice = parsed.get("advice") or []
        return (f"共同成因：{root}\n" if root else "") + "\n".join(
            f"- {a}" for a in advice if isinstance(a, str))

    # ------------------------------------------------ 计划

    def _build_plan(self, mode: str, rotation: str | None,
                    decisions: list[dict], advice: str) -> dict:
        run_list = [d for d in decisions
                    if d["action"] in (ACT_NORMAL, ACT_SLOW, ACT_FALLBACK)]
        skipped = [d for d in decisions
                   if d["action"] in (ACT_SKIP, ACT_NEED_LOGIN)]
        reasoning = "\n".join(f"[{d['action']}] {d['reason']}" for d in decisions
                              if d["action"] != ACT_NORMAL)
        if advice:
            reasoning += f"\n\n【主 Agent 综合建议】\n{advice}"
        return {
            "mode": mode, "rotation_category": rotation,
            "units": [{"channel_id": d["channel_id"], "channel": d["channel"],
                       "country": d["country"], "action": d["action"]}
                      for d in run_list],
            "skipped": [{"channel": d["channel"], "action": d["action"],
                         "reason": d["reason"]} for d in skipped],
            "health": {d["channel"]: d["verdict"] for d in decisions},
            "reasoning": reasoning or "全部渠道健康，按正常策略抓取",
        }

    def _save_plan(self, plan: dict) -> int:
        with db.tx() as conn:
            cur = conn.execute("""
                INSERT INTO crawl_plan(plan_date,mode,rotation_category,health_summary,
                                       strategy,unit_count,reasoning)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(plan_date,mode,rotation_category) DO UPDATE SET
                  health_summary=excluded.health_summary, strategy=excluded.strategy,
                  unit_count=excluded.unit_count, reasoning=excluded.reasoning,
                  created_at=datetime('now')
            """, (db.today(), plan["mode"], plan.get("rotation_category") or "",
                  json.dumps(plan["health"], ensure_ascii=False),
                  json.dumps({"units": plan["units"], "skipped": plan["skipped"]},
                             ensure_ascii=False),
                  len(plan["units"]), plan["reasoning"]))
            if cur.lastrowid:
                return cur.lastrowid
        row = db.q1("""SELECT id FROM crawl_plan WHERE plan_date=? AND mode=?
                       AND rotation_category=?""",
                    (db.today(), plan["mode"], plan.get("rotation_category") or ""))
        return row["id"] if row else 0
