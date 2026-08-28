# -*- coding: utf-8 -*-
"""VOC 分析 Agent —— 把抓回来的评论变成 销售团队 能用的情报。

用户要求两件事：
  ① "消费者的评论你要把它都抓出来做一个 agent 的分析"
  ② "某些产品评论数非常多，你要看一下消费者对它的评论多到底是因为什么"

第②件是**归因分析**，不是简单统计。评论多可能是：
  · 真的卖得好（主销款）—— 这是最有价值的情报
  · 上市久（时间累积，不代表当期热销）
  · 大促冲量（短时间集中，评论日期会扎堆）
  · 质量问题引发吐槽潮（差评占比异常高）
  · 平台刷评 / 返现换好评（短评多、内容雷同、五星占比畸高）
这五种成因对 销售团队 的含义完全相反，混为一谈就等于没分析。
所以归因先用**可计算的信号**（星级分布、评论时间集中度、平均长度）
缩小范围，再让模型下判断 —— 而不是直接问模型"为什么评论多"。
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter

from .. import db, voc_aspects
from .base import BaseAgent
from .llm import as_dict, as_dicts, as_text

log = logging.getLogger("voc_agent")

_SENTIMENTS = {"positive", "neutral", "negative"}


def _aspect_pairs(raw, category_code: str | None) -> list[tuple[str, str | None]]:
    """把模型返回的 aspects 归一成 [(固定code, 情感)]。

    ★ 容两种写法：新格式 [{"code":"battery","s":"negative"}]，
      以及模型偷懒退回的老格式 ["电池","相机"]（此时情感未知，留 None）。
      不容错的话，模型一走样这一批的维度就整批丢，而且**不报错**。
    ★ 归一不到固定表的维度**丢掉不猜** —— 塞进错的维度比没有更贵，
      雷达图会显示一个根本没人提过的短板。
    ★ 但**不按品类过滤**。品类只用来生成提示词菜单（见 voc_aspects 里的说明）：
      拿它当入库闸门实测吃掉 19 条真信号，其中包括耳机「App 常崩溃」这种
      最该报给 销售团队 的短板。category_code 参数保留是为了调用方语义清楚。
    """
    out, seen = [], set()
    for a in (raw or []):
        if isinstance(a, dict):
            code = voc_aspects.normalize(as_text(a.get("code")) or "")
            sent = (as_text(a.get("s")) or "").lower()
        else:
            code = voc_aspects.normalize(as_text(a) or "")
            sent = ""
        if not code or code in seen:
            continue
        seen.add(code)
        out.append((code, sent if sent in _SENTIMENTS else None))
    return out


def _norm_head(text: str, n: int = 24) -> str:
    """取一段用于对账的正文指纹：压空白、转小写、取前 n 个字符。

    ★ 为什么要指纹：批量调用里**序号是模型自己写的**，模型丢条目后
      会把后面的重新编号（实测见 _translate_and_tag 的注释），
      于是序号会说谎。而原文片段模型抄不错也编不出来 ——
      拿它当第二把锁，序号错了也能发现、还能找回正主。
    """
    return re.sub(r"\s+", " ", (text or "")).strip().lower()[:n]


class VocAgent(BaseAgent):
    name = "voc"
    role = "voc"
    description = "翻译与情感分析评论，提炼好评/差评点，并对高评论量产品做归因"

    def run(self, days: int = 30, limit_products: int = 40) -> dict:
        self.start(f"VOC 分析（近 {days} 天）")

        translated = self._translate_and_tag()
        insights = self._build_insights(days, limit_products)
        hot = self._explain_hot_products()

        summary = (f"分析评论 {translated} 条，产出 {insights} 份产品洞察，"
                   f"归因高评论量产品 {hot} 个")
        self.finish("ok", summary, translated, insights)
        return {"translated": translated, "insights": insights, "hot_explained": hot}

    # ------------------------------------------------ 翻译 + 情感

    def _translate_and_tag(self, batch: int = 12, max_rows: int = 240) -> int:
        """逐批翻译并打情感标。

        ★★ 这里踩过一个**批量对齐**的坑，代价是库里 19% 的已译行不可信。
          原来的写法是：提示词里把这批评论编号 0~11，让模型回填 `idx`，
          再用 `chunk[int(item["idx"])]` 写回。方向是对的（按 id 匹配、
          不是按顺序 zip），但漏了一件事 ——

          **`idx` 是模型自己写的，模型丢条目后会重新编号。**

          留痕里抓到的现行犯（agent_step id=4401，12 条进、11 条回）：
              prompt  9. Katheryn El equipo se reiniciaba constantemente…（差评）
              prompt 10. ALVARO El celular mejor de lo que espere…（好评）
              模型 idx=9  → "手机比我预期的要好…"    ← 其实是 prompt 10 的
              模型 idx=10 → "产品状况不佳，边角损坏…" ← 其实是 prompt 11 的
          结果差评行拿到 positive、好评行拿到 negative，**全程不报错**。
          实测 262 个批次里 67 个（25.6%）返回条数与 prompt 不符，
          最常见的形态是模型干脆 1-based 返回 idx=[1..11] ⇒ 整批错一格。

        ★ 两把锁，缺一不可：
          ① **用评论的真实 id 当编号**，不用 0~11 的序数。
             序数天生可重排（模型"顺手"重新数一遍很自然），
             而 5455 这种 id 重排不出来、也不会和相邻行撞。
          ② **要求回传一段原文指纹**（head），落库前逐条核对。
             id 与 head 对不上就按 head 找回正主；找不回就**丢弃不写** ——
             宁可这行留空等下轮重译（查询条件就是 content_zh IS NULL，
             自愈），也不能把 A 的情感写到 B 头上。
        """
        rows = db.q("""
            SELECT r.id, r.content, r.product_title, rp.category_code
            FROM review r
            LEFT JOIN rival_product rp ON rp.id = r.rival_product_id
            WHERE (r.content_zh IS NULL OR r.content_zh='') AND length(r.content) >= 15
            ORDER BY r.id DESC LIMIT ?
        """, (max_rows,))
        if not rows:
            return 0
        if not (self.llm and self.llm.available()):
            self.log_step("评论翻译分析", parsed={"待处理": len(rows)},
                          decision="skipped", status="degraded",
                          reason="未配置 API Key，评论原文已入库，配置后可随时重跑")
            return 0

        done = 0
        dropped = 0          # 因对不上账被丢弃的条数（必须报出来，不能静默）
        remapped = 0         # 靠 head 找回正主的条数
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            by_id = {r["id"]: r for r in chunk}
            lines = [f"#{r['id']} [{(r['product_title'] or '')[:34]}] "
                     f"{(r['content'] or '')[:340]}"
                     for r in chunk]
            # ★ 维度必须**从固定表里选**，不能让模型自由发挥。
            #   自由发挥的实测结果：814 条评论跑出 43 种维度名，
            #   音质/音效/音量/隔音 各算一个 ⇒ 雷达图上"音质"被拆散、排名失真。
            #   这批评论可能横跨品类，菜单取并集（单品类过滤在回填/聚合时做）。
            cats = {r["category_code"] for r in chunk if r["category_code"]}
            menu = (voc_aspects.prompt_menu(next(iter(cats))) if len(cats) == 1
                    else voc_aspects.prompt_menu(None))
            prompt = (
                "下面是拉美电商（西语/葡语）上的消费者评论。你服务的对象是Acme拉美 销售团队，"
                "他要用这些评论了解友商产品的真实口碑。\n\n"
                "请逐条：①翻译成简洁中文（不超过60字，保留原意与语气）；"
                "②判断整条的情感；③指出评论具体在夸/骂哪些方面，"
                "**并分别判断每个方面的好坏**。\n\n"
                + "\n".join(lines) + "\n\n"
                "维度只能从下面这张表里选，表里没有的方面就不要报：\n"
                f"{menu}\n\n"
                "只返回 JSON 数组，每条："
                '{"id":原样回填的编号,"head":"该条原文最前面的12个字符，原样复制",'
                '"zh":"中文翻译","sentiment":"positive|neutral|negative",'
                '"aspects":[{"code":"battery","s":"negative"}]}\n'
                "★ id 必须原样回填上面 # 后面那个编号，**不要重新编号、不要用行序号**。\n"
                "★ head 原样抄该条原文开头，用来对账，不要翻译它。\n"
                f"★ 上面共 {len(chunk)} 条，请**逐条都返回**；"
                "看不懂或不像评论的也要返回，把 zh 留空、sentiment 填 neutral。\n"
                "★ aspects 只填评论**真正提到**的方面，没提到就给空数组，不要凑数。\n"
                "★ 每个方面的 s 按**这条评论对这个方面**的态度判，"
                "不要照抄整条的 sentiment —— 一条评论完全可以夸相机同时骂电池。")

            parsed = self.ask_json(f"翻译分析评论 {i}~{i + len(chunk) - 1}", prompt,
                                   system="你是消费者洞察分析师，只输出 JSON。",
                                   input_ref=f"reviews:{i}", default=[])
            items = as_dicts(parsed)
            b_done = b_drop = b_remap = 0
            with db.tx() as conn:
                seen: set = set()
                for item in items:
                    r, how = self._match_review(item, by_id, chunk, seen)
                    if r is None:
                        b_drop += 1
                        continue
                    zh = str(item.get("zh") or "")[:400]
                    if not zh:
                        b_drop += 1     # 没译文就别占位，留空等下轮重译
                        continue
                    if how == "head":
                        b_remap += 1
                    seen.add(r["id"])
                    pairs = _aspect_pairs(item.get("aspects"), r["category_code"])
                    conn.execute("""UPDATE review SET content_zh=?, sentiment=?,
                                    aspects=? WHERE id=?""",
                                 (zh,
                                  str(item.get("sentiment") or "neutral")[:12],
                                  json.dumps([c for c, _ in pairs],
                                             ensure_ascii=False), r["id"]))
                    # 维度落到独立表 —— JSON 列没法在 SQL 里聚合，画不出雷达图
                    conn.execute("DELETE FROM review_aspect WHERE review_id=?", (r["id"],))
                    for code, sent in pairs:
                        conn.execute(
                            "INSERT OR IGNORE INTO review_aspect"
                            "(review_id,aspect_code,sentiment,sentiment_from)"
                            " VALUES(?,?,?,'aspect')", (r["id"], code, sent))
                    b_done += 1
            done += b_done
            dropped += b_drop
            remapped += b_remap
            # ★ 对账结果必须留痕。静默丢弃 = 下次还是查不出来为什么少了几条。
            if b_drop or b_remap or len(items) != len(chunk):
                self.log_step(
                    f"批次对账 {i}~{i + len(chunk) - 1}",
                    parsed={"送入": len(chunk), "返回": len(items), "写入": b_done,
                            "按指纹找回": b_remap, "对不上账丢弃": b_drop},
                    decision="reconciled",
                    status="degraded" if b_drop else "ok",
                    reason="模型返回条数或编号与送入不符；已按原文指纹逐条核对，"
                           "对不上的不写库，留 content_zh=NULL 等下轮重译")
        if dropped or remapped:
            log.warning("VOC 翻译对账：写入 %d 条，指纹找回 %d 条，丢弃 %d 条",
                        done, remapped, dropped)
        return done

    @staticmethod
    def _match_review(item: dict, by_id: dict, chunk: list, seen: set):
        """把模型返回的一条结果对回它真正属于的评论行。

        返回 (行, 依据)；对不上就返回 (None, 原因)——**宁可不写也不写错**。
        """
        head = _norm_head(item.get("head"))
        rid = item.get("id")
        try:
            rid = int(str(rid).lstrip("#").strip())
        except (TypeError, ValueError):
            rid = None
        r = by_id.get(rid)
        cut = min(len(head), 12)

        def head_ok(row) -> bool:
            """head 太短就不足以判定，此时不拿它否决 id。"""
            if len(head) < 6:
                return True
            return _norm_head(row["content"]).startswith(head[:cut])

        if r is not None and r["id"] not in seen and head_ok(r):
            return r, "id"
        # id 对不上（模型重新编号了）—— 按原文指纹找回正主
        if len(head) >= 6:
            cand = [x for x in chunk
                    if x["id"] not in seen
                    and _norm_head(x["content"]).startswith(head[:cut])]
            if len(cand) == 1:
                return cand[0], "head"
        return None, "unmatched"

    # ------------------------------------------------ 产品级洞察

    def _build_insights(self, days: int, limit: int) -> int:
        products = db.q("""
            SELECT r.rival_product_id, r.country_code, COUNT(*) n,
                   AVG(r.rating) avg_rating, rp.model_name, b.name AS brand
            FROM review r
            JOIN rival_product rp ON rp.id = r.rival_product_id
            JOIN brand b ON b.id = rp.brand_id
            WHERE r.rival_product_id IS NOT NULL
              AND date(r.created_at) >= date('now', ?)
            GROUP BY r.rival_product_id, r.country_code
            HAVING n >= 5 ORDER BY n DESC LIMIT ?
        """, (f"-{days} day", limit))
        if not products or not (self.llm and self.llm.available()):
            return 0

        made = 0
        for p in products:
            reviews = db.q("""
                SELECT content_zh, content, sentiment, rating FROM review
                WHERE rival_product_id=? AND country_code=?
                ORDER BY id DESC LIMIT 60
            """, (p["rival_product_id"], p["country_code"]))
            sample = "\n".join(
                f"- [{r['sentiment'] or '?'}{('/' + str(r['rating'])) if r['rating'] else ''}] "
                f"{(r['content_zh'] or r['content'] or '')[:110]}"
                for r in reviews[:45])

            prompt = (
                f"产品：{p['brand']} {p['model_name']}（{p['country_code']}），"
                f"共 {p['n']} 条评论，平均 {p['avg_rating'] or 0:.1f} 星。\n"
                f"以下是评论样本：\n{sample}\n\n"
                "你服务的对象是Acme拉美 销售团队。请提炼：\n"
                "①好评点（消费者最认可什么，按提及频次排序）\n"
                "②差评点（最不满什么）\n"
                "③需关注信号（质量问题、售后投诉、与竞品对比的说法，"
                "尤其是任何提到Acme/ACME 的内容）\n\n"
                '只返回 JSON：{"praise":["…"],"complaint":["…"],"watch":["…"],'
                '"vs_acme":"提到Acme的内容摘要，没有就填空字符串",'
                '"summary":"一句话总结这款产品的口碑，不超过50字"}')

            parsed = as_dict(self.ask_json(
                f"洞察 {p['brand']} {p['model_name']}", prompt,
                system="你是竞品口碑分析师，结论要具体、可行动，不说空话。",
                input_ref=f"rival:{p['rival_product_id']}/{p['country_code']}",
                default={}))
            if not parsed:
                continue
            with db.tx() as conn:
                conn.execute("""
                    INSERT INTO voc_insight(rival_product_id,country_code,period_start,
                        period_end,review_count,avg_rating,praise_points,
                        complaint_points,watch_signals,vs_acme,summary_zh)
                    VALUES(?,?,date('now',?),date('now'),?,?,?,?,?,?,?)
                    ON CONFLICT(rival_product_id,country_code,period_start)
                    DO UPDATE SET review_count=excluded.review_count,
                      avg_rating=excluded.avg_rating,
                      praise_points=excluded.praise_points,
                      complaint_points=excluded.complaint_points,
                      watch_signals=excluded.watch_signals,
                      vs_acme=excluded.vs_acme, summary_zh=excluded.summary_zh
                """, (p["rival_product_id"], p["country_code"], f"-{days} day",
                      p["n"], p["avg_rating"],
                      json.dumps(parsed.get("praise") or [], ensure_ascii=False),
                      json.dumps(parsed.get("complaint") or [], ensure_ascii=False),
                      json.dumps(parsed.get("watch") or [], ensure_ascii=False),
                      str(parsed.get("vs_acme") or "")[:600],
                      str(parsed.get("summary") or "")[:300]))
            made += 1
        return made

    # ------------------------------------------------ ★ 高评论量归因

    def _explain_hot_products(self, limit: int = 20) -> int:
        """回答用户的问题：这个产品评论为什么这么多？

        先算可计算的信号，再让模型在**给定证据**下判断 ——
        直接问"为什么评论多"模型只会编，给了证据才能真判。
        """
        hot = db.q("""
            SELECT rpf.*, rp.model_name, b.name AS brand
            FROM review_profile rpf
            LEFT JOIN rival_product rp ON rp.id = rpf.rival_product_id
            LEFT JOIN brand b ON b.id = rp.brand_id
            WHERE rpf.is_hot=1 AND (rpf.hot_reason IS NULL OR rpf.hot_reason='')
            ORDER BY rpf.total_reviews DESC LIMIT ?
        """, (limit,))
        if not hot:
            return 0

        done = 0
        for h in hot:
            sig = self._signals(h)
            self.log_step(f"归因信号 {h['model_name'] or h['product_url'][:40]}",
                          input_ref=h["product_url"][:150], parsed=sig,
                          decision="computed",
                          reason="先算可计算信号（星级分布/时间集中度/评论长度），"
                                 "再交模型判断成因")

            if not (self.llm and self.llm.available()):
                # 没配 Key 也给一个规则版结论，不留空
                reason = self._rule_explain(sig)
            else:
                prompt = (
                    f"产品：{h['brand'] or '?'} {h['model_name'] or '?'}"
                    f"（{h['country_code']}）\n"
                    f"页面标称评论总数：{h['total_reviews']}，实际抓到 {h['fetched_reviews']} 条，"
                    f"平均 {h['avg_rating'] or 0:.1f} 星\n"
                    f"可计算信号：{json.dumps(sig, ensure_ascii=False)}\n\n"
                    "这款产品的评论量明显高于同类。请判断**主要成因**是哪一种，并说明依据：\n"
                    "A 真的卖得好（主销款）  B 上市久、评论时间累积\n"
                    "C 大促集中冲量        D 质量问题引发吐槽潮\n"
                    "E 刷评/返现换好评（短评多、内容雷同、五星占比畸高）\n\n"
                    "★ 这五种对 销售团队 的含义完全相反，判断要基于上面的信号，不要臆测。\n"
                    '只返回 JSON：{"cause":"A|B|C|D|E","confidence":0到1,'
                    '"reason":"依据，不超过60字","sb_implication":"对Acme销售团队意味着什么，不超过40字"}')
                parsed = as_dict(self.ask_json(
                    f"归因 {h['model_name'] or '产品'}", prompt,
                    system="你是竞品分析师，只根据给出的信号判断，证据不足就降低 confidence。",
                    input_ref=h["product_url"][:150], default={}))
                cause = str(parsed.get("cause") or "?")
                reason = (f"[{cause}] {parsed.get('reason', '')} "
                          f"｜对销售的含义：{parsed.get('sb_implication', '')}"
                          f"（置信度 {parsed.get('confidence', 0)}）")[:500]

            with db.tx() as conn:
                conn.execute("UPDATE review_profile SET hot_reason=? WHERE id=?",
                             (reason, h["id"]))
            done += 1
        return done

    @staticmethod
    def _signals(profile: dict) -> dict:
        """可计算的归因信号"""
        rows = db.q("""SELECT rating, length(content) len, review_date, sentiment
                       FROM review WHERE product_url=?""", (profile["product_url"],))
        if not rows:
            return {"样本": 0}
        ratings = [r["rating"] for r in rows if r["rating"]]
        lens = [r["len"] for r in rows if r["len"]]
        dist = Counter(int(r) for r in ratings) if ratings else {}
        n = len(ratings) or 1
        neg = sum(1 for r in rows if r["sentiment"] == "negative")
        dates = [r["review_date"][:7] for r in rows if r["review_date"]]
        month_conc = (Counter(dates).most_common(1)[0][1] / len(dates)) if dates else None
        return {
            "样本": len(rows),
            "五星占比": round(dist.get(5, 0) / n, 3) if ratings else None,
            "一二星占比": round((dist.get(1, 0) + dist.get(2, 0)) / n, 3) if ratings else None,
            "负面情感占比": round(neg / len(rows), 3),
            "评论中位长度": int(statistics.median(lens)) if lens else None,
            "最集中月份占比": round(month_conc, 3) if month_conc else None,
        }

    @staticmethod
    def _rule_explain(sig: dict) -> str:
        """没配模型时的规则版归因，保证不留空"""
        five = sig.get("五星占比") or 0
        low = sig.get("一二星占比") or 0
        med_len = sig.get("评论中位长度") or 0
        conc = sig.get("最集中月份占比") or 0
        if low > 0.35:
            return "[D] 一二星占比超 35%，指向质量或体验问题引发的吐槽潮（规则判定，未经模型复核）"
        if five > 0.9 and med_len < 40:
            return "[E] 五星占比超 90% 且评论普遍很短，符合刷评/返现换好评特征（规则判定）"
        if conc > 0.6:
            return "[C] 超 60% 评论集中在同一个月，指向大促集中冲量（规则判定）"
        return "[A/B] 星级与时间分布正常，倾向真实热销或长期累积；需模型复核区分（规则判定）"
