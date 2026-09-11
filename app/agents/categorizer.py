# -*- coding: utf-8 -*-
"""品类判定 Agent —— 规则层不敢判时，用 LLM 兜底（2026-08-31 用户要求）。

为什么需要这一层：
  price_obs.category_code 的原始来源是「我们在搜哪个品类」，不是商品本身
  （collector.py 把采集计划的 category 直接盖在每条结果上）。搜"Samsung 穿戴"
  返回的手机，就被盖成穿戴。
  extract.crosscheck_category 是第一道规则闸，但它**明确拒绝猜测**：
  「标题无任何品类证据 → 不动」。实测 3501 个产品里有 706 个落在这个分支，
  它们保留着可能错误的搜索意图品类。
  ⇒ 这一层专门接手规则层弃权的部分，交给 LLM 按标题判断。

纪律：
  ★ 只处理**规则层弃权**的产品，不与规则层抢判定 —— 规则确定的不许 LLM 翻案，
    否则两层互相覆盖，出了错查不出是谁改的。
  ★ 每次判定写 category_source（llm:模型名 / rule / search-intent），
    并留痕到 reclass_audit —— 3500 条改动没有痕迹就无法追溯。
  ★ LLM 不可用时**什么都不做**（保持现状），不猜、不清空。
"""
from __future__ import annotations

import json
import logging

from .. import db
from ..scraping import extract as _ex
from .base import BaseAgent

log = logging.getLogger("categorizer")

ANALYSIS_CATS = ("phone", "tablet", "wearable", "audio", "pc")
VALID = set(ANALYSIS_CATS) | {"other", "accessory"}
BATCH = 40

SYSTEM = "你是消费电子品类判定专家。只输出 JSON，不要解释。"

RULES = """判定纪律：
1. 套装按**主商品**判：「vivo V70 5G Gratis Smartwatch」是 phone（手表是赠品）；
   「Xiaomi Watch 5 con Bocina」是 wearable。看谁贵、谁是标题主语。
2. 忽略促销词：Envío gratis / Vista Previa / 4 cuotas sin interés / Por FALABELLA
   / HOT PRICE / 折扣百分比 / 评分括号。
3. 配件与维修件 → accessory：保护壳、贴膜、数据线、充电器、表带、屏幕总成
   （「Pantalla completa para X」是维修件不是手机）。
4. 不属于五类的整机 → other：电视、路由器、投影仪、游戏机、家电。
5. 标题说不清就用 brand+model 常识（Galaxy Fit3=wearable，Slate Tab=tablet，
   SonicBuds=audio，AceBook=pc）。
6. confidence: high=标题明确/型号众所周知；medium=常识推断；low=真不确定。"""


class CategoryAgent(BaseAgent):
    name = "categorizer"
    role = "品类判定"
    description = "规则层弃权时用 LLM 判定产品真实品类"

    def run(self, limit: int = 400, rematch: bool = True) -> dict:
        """rematch=False 用于夜间流水线 —— orchestrator 的 ⑦ 紧接着就会
        rebuild_all，这里再算一遍是纯浪费。脱离流水线单跑（/api/task、
        手工调用）时必须保持 True，否则改完品类没人重算匹配。"""
        self.start("品类判定（LLM 兜底）")
        self._revoked = 0
        if not (self.llm and self.llm.available()):
            self.log_step("品类判定", decision="skipped", status="degraded",
                          reason="未配置 API Key —— 保持现状，不猜品类")
            self.finish("degraded", "LLM 不可用，本轮不判品类", 0, 0)
            return {"checked": 0, "changed": 0, "skipped": "no_llm"}

        rows = db.q("""
            SELECT rp.id, rp.category_code cat, rp.model_name model,
                   COALESCE(b.name,'') brand,
                   (SELECT po.title FROM price_obs po WHERE po.rival_product_id=rp.id
                     ORDER BY LENGTH(po.title) DESC LIMIT 1) title,
                   (SELECT COUNT(*) FROM price_obs po WHERE po.rival_product_id=rp.id) obs
            FROM rival_product rp LEFT JOIN brand b ON b.id=rp.brand_id
            WHERE COALESCE(rp.category_source,'') NOT LIKE 'llm:%'
            ORDER BY obs DESC LIMIT ?""", (limit * 3,))

        # 只接手**规则层弃权**的：规则能判的交给规则，两层不抢
        todo = []
        rule_ok = []   # 规则层已从明确设备名确认品类的：只补记来源，不送 LLM
        for r in rows:
            if not r["title"]:
                todo.append(r)
                continue
            verdict, _tgt, why = _ex.crosscheck_category(
                r["title"], r["cat"], brand=r["brand"])
            # ★ 规则层「弃权」有两种形态，两种都该由 LLM 接手：
            #   ① ok + 无任何品类证据 —— 标题说不出话；
            #   ② pending + 品类冲突 —— 标题同时指向多个品类，规则层明说"人工定夺"。
            #     2026-09-07 加：②原来没接，于是「Tablet Samsung Galaxy S10 Lite」
            #     （tablet@0 与 phone@15 冲突）既不被规则层改、也进不了 LLM 层，
            #     错误品类永久固化 —— 实测 299 个产品卡在这个缝里。
            #   ★ pending 的另两种理由（玩具/教具、蓝牙防丢器）是规则层的**明确判定**
            #     不是弃权，不许 LLM 翻案，所以按理由文本区分。
            if verdict == "ok" and "无任何品类证据" in why:
                todo.append(r)
            elif verdict == "pending" and ("位置优先" in why or "同时指向多个品类" in why):
                todo.append(r)
            elif verdict == "ok" and "本品类证据" in why:
                # ★ 规则层从标题的**明确设备名**确认了品类（"Smartphone …"→phone、
                #   "Caixa de Som …"→audio）。这类不该再花 token 让 LLM 复核，
                #   但**必须记来源** —— 否则它和"从没判过"在库里长得一样，
                #   每轮都被当成待判取出来（实测 295 个产品卡在这个状态，
                #   且让"品类有据可查"这条健康指标永远到不了 95%）。
                #   ★ 只写来源，不动 category_code：规则层的判定本身没变。
                rule_ok.append(r["id"])
            if len(todo) >= limit:
                break

        # 规则层确认的批量补记来源（一条 UPDATE，不花 token）
        if rule_ok:
            with db.tx() as conn:
                conn.executemany(
                    "UPDATE rival_product SET category_source='rule:crosscheck' "
                    "WHERE id=? AND COALESCE(category_source,'')=''",
                    [(i,) for i in rule_ok])
            log.info("[categorizer] 规则层已确认品类，补记来源 %d 条（未调模型）", len(rule_ok))

        if not todo:
            self.finish("ok", "规则层已覆盖，无需 LLM 判定", 0, 0)
            return {"checked": 0, "changed": 0}

        changed = 0
        confirmed = 0   # 模型确认「品类没错」的条数（也要记来源，否则永远重判）
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            payload = [{"id": r["id"], "brand": r["brand"], "model": r["model"],
                        "title": (r["title"] or "")[:180]} for r in chunk]
            prompt = (
                "为下面每个商品判定品类。可选值只有："
                + " / ".join(sorted(VALID)) + "\n\n" + RULES + "\n\n"
                "商品列表（JSON）：\n" + json.dumps(payload, ensure_ascii=False) + "\n\n"
                '只输出 JSON：{"items":[{"id":123,"category":"phone",'
                '"confidence":"high","reason":"标题写明 Smartphone"}]}\n'
                "每个 id 都要有判定，不得遗漏。")
            parsed, _raw, _tk = self.llm.chat_json(prompt, SYSTEM, default=None)
            items = (parsed or {}).get("items") or []
            by_id = {r["id"]: r for r in chunk}
            for it in items:
                r = by_id.get(it.get("id"))
                cat = it.get("category")
                if not r or cat not in VALID:
                    continue
                # ★★ other / accessory 作为**禁用品类**入库（category 表里 enabled=0），
                #   不是置 NULL —— rival_product.category_code 有 NOT NULL 约束，
                #   置 NULL 会 IntegrityError 把整轮判定炸掉（2026-09-07 实测），
                #   而且丢掉"它到底是什么"这条信息。报告只遍历 enabled=1 的品类，
                #   所以它们自动被排除出分析。
                #   ★ tools/apply_reclass.py 早就这么做了，这里一直是旧写法 ——
                #     同一条规则两处实现，差集没人发现，直到今天崩了才暴露。
                tgt = cat
                if tgt == r["cat"] and cat != "accessory":
                    # ★★ 模型确认「品类没错」也**必须记来源**（2026-09-07 修）。
                    #   原来这里直接 continue，category_source 永不写入 ⇒ 这批产品
                    #   下一轮又被当成"未判定"取出来，**永远循环、永远花 token、
                    #   覆盖率永远上不去**：实测连续 8 轮每轮都是「判定 37 改判 0」，
                    #   覆盖率钉死在 94.1%。
                    #   「判过且结论是不变」与「没判过」是两件事，库里必须能分开。
                    with db.tx() as conn:
                        conn.execute(
                            "UPDATE rival_product SET category_source=? WHERE id=?",
                            (f"llm:{getattr(self.llm, 'model', '?')}", r["id"]))
                    confirmed += 1
                    continue
                self._apply(r, cat, tgt, it.get("confidence"), it.get("reason", ""))
                changed += 1

        rebuilt = self._rematch(changed) if rematch else 0

        self.log_step("品类判定",
                      parsed={"送审": len(todo), "改判": changed,
                              "作废的竞品匹配": self._revoked,
                              "重算出的匹配": rebuilt},
                      decision="ok",
                      reason=f"规则层弃权的 {len(todo)} 个产品交 LLM 判定，"
                             f"改判 {changed} 个（全部留痕 reclass_audit）；"
                             f"改判会让下游竞品匹配失效，已作废 {self._revoked} 条"
                             + (f"并重算出 {rebuilt} 条" if rematch else
                                "（重算交给流水线的匹配阶段）"))
        self.finish("ok", f"送审 {len(todo)}，改判 {changed}", len(todo), changed)
        return {"checked": len(todo), "changed": changed, "confirmed": confirmed,
                "revoked_matches": self._revoked, "rematched": rebuilt}

    # ------------------------------------------------ 下游失效

    def _rematch(self, changed: int) -> int:
        """改过品类就重算竞品匹配。

        ★ 这是 2026-09-03「SonicBuds 5 的竞品里出现小米手表」事件的根治点。
          competitor_match 的候选池 SQL 有 rp.category_code=? 硬约束，匹配器
          本身没漏；漏的是**改品类的路径不触发重算** —— 匹配落库时两边同品类
          （合法），之后品类被改，那行就成了跨品类脏行，而且没有任何东西会
          发现它：它不报错、不变色、computed_at 也不动，只会等用户截图。
          同 SpecFillerAgent._revoke_stale_inferences 的道理：上游变了，
          基于旧上游算出来的下游结论必须作废，不能留着一份过期答案。
        """
        if not changed:
            return 0
        try:
            from ..matching.matcher import CompetitorMatcher
            return int(CompetitorMatcher().rebuild_all().get("matches") or 0)
        except Exception:                              # noqa: BLE001
            # 重算失败不能拖垮品类判定本身 —— 失效动作（_apply 里的 DELETE）
            # 已经落库了，最坏结果是"竞品少了"，不会是"竞品是错的"。
            log.warning("品类改判后重算竞品匹配失败，本轮匹配将偏少", exc_info=True)
            return 0

    def _apply(self, row: dict, cat: str, tgt: str | None,
               confidence: str | None, reason: str) -> None:
        src = f"llm:{getattr(self.llm, 'model', '?')}"
        with db.tx() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS reclass_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rival_product_id INTEGER NOT NULL,
                old_category TEXT, new_category TEXT,
                old_kind TEXT, new_kind TEXT,
                confidence TEXT, source TEXT, reason TEXT,
                obs_affected INTEGER,
                applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")
            conn.execute("""INSERT INTO reclass_audit(rival_product_id, old_category,
                            new_category, new_kind, confidence, source, reason,
                            obs_affected) VALUES(?,?,?,?,?,?,?,?)""",
                         (row["id"], row["cat"], cat,
                          "accessory" if cat == "accessory" else None,
                          confidence, src, reason[:200], row["obs"]))
            # ★ 必须 bump updated_at。原来改品类不动时间戳 ⇒ 谁都无法从
            #   updated_at 看出"这条的品类在匹配之后被改过"，陈旧性在时间戳上
            #   完全隐形，排查只能靠 reclass_audit 一张表反查。
            # ★★ 改品类会撞 UNIQUE(brand_id, model_key, category_code)：目标品类下
            #   已有同名产品时不能直接改，要**合并**过去，否则整轮判定被
            #   IntegrityError 炸掉（2026-09-07 实测）。tools/apply_reclass.py 早有这段，
            #   Agent 这条路径一直没有 —— 同一条规则两处实现的第二个差集。
            #   ★ competitor_match **不改指、只删除**：匹配是可重算的派生数据，
            #     改指会把它从「同品类那一行」指到「另一品类那一行」，当场变跨品类脏行
            #     （这正是「SonicBuds 5 的竞品是小米手表」的根因）。
            key = conn.execute("SELECT brand_id, model_key FROM rival_product WHERE id=?",
                               (row["id"],)).fetchone()
            dup = None
            if key:
                dup = conn.execute(
                    "SELECT id FROM rival_product WHERE brand_id IS ? AND model_key=? "
                    "AND category_code=? AND id<>?",
                    (key[0], key[1], tgt, row["id"])).fetchone()
            if dup:
                dup_id = dup[0]
                for t in ("price_obs", "launch_event", "product_page_cache", "review",
                          "review_profile", "voc_insight", "price_move",
                          "strategy_signal", "watchlist", "price_alert"):
                    conn.execute(f"UPDATE OR IGNORE {t} SET rival_product_id=? "
                                 f"WHERE rival_product_id=?", (dup_id, row["id"]))
                    conn.execute(f"DELETE FROM {t} WHERE rival_product_id=?", (row["id"],))
                conn.execute("DELETE FROM competitor_match WHERE rival_product_id IN (?,?)",
                             (row["id"], dup_id))
                conn.execute("DELETE FROM rival_product WHERE id=?", (row["id"],))
                conn.execute("UPDATE rival_product SET category_source=?, "
                             "updated_at=datetime('now') WHERE id=?", (src, dup_id))
                conn.execute("UPDATE price_obs SET category_code=? WHERE rival_product_id=?",
                             (tgt, dup_id))
                if cat == "accessory":
                    conn.execute("UPDATE price_obs SET product_kind='accessory' "
                                 "WHERE rival_product_id=?", (dup_id,))
                self._merged = getattr(self, "_merged", 0) + 1
                return
            conn.execute("UPDATE rival_product SET category_code=?, category_source=?, "
                         "updated_at=datetime('now') WHERE id=?",
                         (tgt, src, row["id"]))
            conn.execute("UPDATE price_obs SET category_code=? WHERE rival_product_id=?",
                         (tgt, row["id"]))
            if cat == "accessory":
                conn.execute("UPDATE price_obs SET product_kind='accessory' "
                             "WHERE rival_product_id=?", (row["id"],))

            # ★ 品类是竞品匹配的**输入**。输入改了，基于旧输入算出的匹配
            #   当场作废 —— 否则它就变成一条跨品类脏行（耳机的竞品是手表）。
            #   守卫与 matcher._persist 的 DELETE 完全一致：只清算法自己写的
            #   （source='auto' 且未被人工确认/排除），人的判断优先于算法。
            cur = conn.execute("""DELETE FROM competitor_match
                                  WHERE rival_product_id=? AND source='auto'
                                    AND is_confirmed=0 AND is_excluded=0""",
                               (row["id"],))
            self._revoked = getattr(self, "_revoked", 0) + (cur.rowcount or 0)
            # 人工锁定的行不删，但要报出来 —— 它们现在可能跨品类了，
            # 只有人能决定是"故意跨品类对标"还是"该撤"。
            kept = conn.execute("""SELECT COUNT(*) FROM competitor_match
                                   WHERE rival_product_id=?
                                     AND (source<>'auto' OR is_confirmed=1
                                          OR is_excluded=1)""",
                                (row["id"],)).fetchone()[0]
            if kept:
                log.info("友商 #%s 改判 %s→%s，另有 %d 条人工锁定的匹配未动，"
                         "请人工复核是否已跨品类", row["id"], row["cat"], cat, kept)
