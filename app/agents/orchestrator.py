# -*- coding: utf-8 -*-
"""调度器 + 流水线 —— 把主 Agent、采集、清洗、审计、情报串起来。

用户要求的协作方式（原话组合）：
  "所有的 agent 执行过程要有留痕。所有 agent 在抓取之前要有一个中央的主 agent
   去判断抓的质量。也要有流水线和调度器去调度 agent 去抓取内容。"

落成这样：

    调度器 Orchestrator
        │
        ├─① 中央主 Agent（Chief）研判 → crawl_plan（跳过谁/降频谁/换引擎谁）
        │
        ├─② 采集阶段  Collector 按计划抓（不调 LLM，便宜快）
        │      每个"渠道×品牌"是一个 scrape_unit，状态全部落库
        │
        ├─③ 清洗阶段  CleanerAgent：LLM 兜底抽取 + 型号归一化
        │
        ├─④ 审计阶段  PriceAuditAgent：剔除第三方溢价/配件/翻新/错价
        ├─④b 变动检测  PriceMoveAgent：同SKU同渠道比价（周报的核心事实）
        │
        └─⑤ 情报阶段  IntelAgent：新品发现、动态摘要、上市节奏

每个阶段独立留痕、可单独重跑。某阶段挂了不影响已完成的阶段 ——
这是选流水线而不是"中央大脑动态派活"的核心理由：**可复现、可重跑**。

★ 抓取节奏（用户选定"价格每日 + 产业轮值"）：
    每天：全部产业的【重点型号价格】（型号词搜索，量小、盯价格）
    每天：【一个轮值产业】的全量品牌词扫描（发现新品，量大）
    周一手机 / 周二穿戴 / 周三音频 / 周四平板 / 周五PC / 周六补采 / 周日分析
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from .. import config, db, livelog
from ..matching import CompetitorMatcher
from ..scraping.collector import Collector
from ..scraping.engine import ScrapeEngine
from .chief import ACT_FALLBACK, ACT_SLOW, ChiefAgent
from .cleaner import CleanerAgent
from .intel import IntelAgent
from .llm import LLMClient
from .price_audit import PriceAuditAgent
from .pricemove import PriceMoveAgent
from .spec_filler import SpecFillerAgent
# ★ VocAgent 必须在这里导入。曾经只在函数体里引用而没导入，
#   NameError 被外层宽泛的 except Exception 吞掉 ——
#   表现是"VOC 阶段失败"一行日志，实际是这个阶段从来没跑过。
#   这就是为什么下面的 _stage() 要把异常类型和堆栈都记下来。
from .voc_agent import VocAgent

log = logging.getLogger("orchestrator")

WEEKDAY_ROLE = {0: "rotate", 1: "rotate", 2: "rotate", 3: "rotate",
                4: "rotate", 5: "catchup", 6: "analyze"}


class Orchestrator:
    def __init__(self, mode: str = "daily", categories: list[str] | None = None,
                 countries: list[str] | None = None, brands: list[str] | None = None,
                 max_items: int | None = None, dry_run: bool = False):
        self.mode = mode
        self.cfg = config.load_runtime()
        if max_items:                      # 冒烟/验证时压小，不改配置文件
            self.cfg["scrape"] = {**self.cfg["scrape"],
                                  "max_products_per_query": int(max_items)}
        self.dry_run = dry_run
        self.force_categories = categories
        self.force_countries = countries
        self.force_brands = brands
        self.llm = LLMClient(self.cfg["agents"])
        self.warnings: list[str] = []
        # 实例属性，不能用类属性 —— 类属性会在多次运行间共享，
        # dry_run 早退后下一次运行可能拿到上一次的死引擎
        self.engine_ref: "ScrapeEngine | None" = None

    # ------------------------------------------------ 轮值

    def rotation_category(self, d: date | None = None) -> str | None:
        d = d or date.today()
        enabled = [c["code"] for c in db.q(
            "SELECT code FROM category WHERE enabled=1 ORDER BY sort_order")]
        if not enabled:
            return None
        role = WEEKDAY_ROLE.get(d.weekday(), "rotate")
        if role != "rotate":
            return None
        return enabled[d.weekday() % len(enabled)]

    # ------------------------------------------------ 主流程

    def run_daily(self) -> dict:
        t0 = time.time()
        # ★ 显式指定品类时：只给 1 个 = 当作轮值那一个；
        #   给了多个 = **全都要跑**，rotation 设成 None 表示"不按轮值限制"。
        #   原来无脑取 [0]，导致 categories=['tablet','phone'] 只跑 tablet，
        #   而且不报错 —— 用户以为跑了全品类，实际少一半。
        if self.force_categories:
            rotation = (self.force_categories[0]
                        if len(self.force_categories) == 1 else None)
        else:
            rotation = self.rotation_category()
        run_id = db.start_run(self.mode, {"rotation": rotation,
                                          "countries": self.force_countries})
        log.info("=== 批次 %s 开始（轮值产业=%s）===", run_id, rotation or "无")

        # ① 中央主 Agent 研判
        chief = ChiefAgent(self.llm, self.cfg["agents"])
        plan = chief.run(mode=self.mode, rotation_category=rotation)
        log.info("主 Agent 研判：%s", plan["summary"])
        for s in plan["skipped"]:
            self.warnings.append(f"跳过 {s['channel']}：{s['reason'][:120]}")

        if self.dry_run:
            db.finish_run(run_id, "ok", self.warnings)
            return {"run_id": run_id, "plan": plan, "dry_run": True}

        # ② 采集：**按域名分组并行**，每个 worker 自建自己的浏览器实例
        #    （见 _collect 的说明：组间并行、组内串行，同域并发会触发风控）。
        collect_stats, clean_stats, audit_stats, voc_stats = {}, {}, {}, {}
        collect_stats = self._stage("采集", lambda: self._collect(run_id, plan, rotation))

        # ③ 清洗
        clean_stats = self._stage("清洗", lambda: CleanerAgent(
            self.llm, self.cfg["agents"], plan_id=plan.get("plan_id")).run())

        # ④ 审计
        if self.cfg["agents"].get("enable_price_audit", True):
            audit_stats = self._stage("价格审计", lambda: PriceAuditAgent(
                self.llm, self.cfg["agents"], plan_id=plan.get("plan_id")).run())
            if audit_stats.get("warning"):
                self.warnings.append(audit_stats["warning"])

        # ④b 价格变动检测 —— ★ 2026-08-27 补上：这一步**本来不在流水线里**，
        #     只能靠 /api/task 手动触发。结果 08-13 之后没人手动跑，
        #     price_obs 每天照常入库 4000+ 条，而 price_move 整整 14 天
        #     一条没有新增 —— 周报的核心（谁调了价）静默停更两周，
        #     页面上看不出任何异常，因为"没有变动"和"没检测"长得一模一样。
        #     必须放在审计之后：审计会剔掉第三方溢价/配件/错价，
        #     在脏数据上比价会播报出根本不存在的降价。
        move_stats = self._stage("价格变动检测", lambda: PriceMoveAgent(
            self.llm, self.cfg["agents"], plan_id=plan.get("plan_id")).run())

        # ⑤ VOC：评论抓取需要浏览器，这里才开 —— 采集阶段的浏览器已经各自关闭。
        #
        # ★ 引擎必须用 try/finally 包住所有用到它的阶段。
        #   曾经写成 `try: … finally: pass` + 后面裸调 close()，
        #   中间任一阶段抛异常就永远关不掉浏览器 —— 无人值守跑几天会堆积
        #   孤儿 chrome 进程，还会锁死 user-data-dir 让第二天整轮失败。
        engine = None
        try:
            if self.cfg.get("voc", {}).get("enabled", True):
                engine = ScrapeEngine(self.cfg["scrape"])
                self.engine_ref = engine
                voc_stats = self._stage(
                    "VOC", lambda: self._collect_voc(run_id, plan.get("plan_id")))
        finally:
            if engine is not None:      # VOC 关闭时才创建，可能是 None
                try:
                    engine.close()
                except Exception:  # noqa: BLE001
                    log.warning("关闭抓取引擎时出错（进程可能残留）", exc_info=True)
            self.engine_ref = None

        # ⑥ 规格补全（竞品匹配的必要输入，放在匹配之前）
        spec_stats = {}
        if self.cfg["agents"].get("enable_spec_filler", True):
            spec_stats = self._stage("规格补全", lambda: SpecFillerAgent(
                self.llm, self.cfg["agents"], plan_id=plan.get("plan_id")).run())

        # ⑦ 竞品匹配（规格与价格都齐了才算得准）
        match_stats = self._stage(
            "竞品匹配", lambda: CompetitorMatcher(self.cfg).rebuild_all())

        # ⑧ 情报（新品发现）
        intel_stats = {}
        if self.cfg["agents"].get("enable_intel", True):
            intel_stats = self._stage("情报", lambda: IntelAgent(
                self.llm, self.cfg["agents"],
                plan_id=plan.get("plan_id")).run(rotation_category=rotation))

        # ⑨ 长期存档 —— 必须是**最后一步**，也必须每轮都做。
        #   价格观测不可再生：今天这台机器标多少钱，明天变了就再也抓不回来。
        #   运行库只有一份，而维护脚本确实会删数据
        #   （renorm_skus 合并产品时会删关联评论）。
        #   ★ 放在最后而不是最前：要存的是**这一轮的成果**。
        #   ★ 存档失败绝不能影响本轮结论 —— 它只读不写源库，
        #     出问题就记 warning，采集结果照常返回。
        archive_stats = self._stage("存档", self._archive)

        status = "partial" if self.warnings else "ok"
        db.finish_run(run_id, status, self.warnings)
        elapsed = time.time() - t0
        log.info("=== 批次 %s 结束，耗时 %.1f 分钟 ===", run_id, elapsed / 60)

        return {
            "run_id": run_id, "rotation": rotation, "plan_summary": plan["summary"],
            "collect": collect_stats, "clean": clean_stats,
            "audit": audit_stats, "voc": voc_stats, "specs": spec_stats,
            "match": match_stats, "intel": intel_stats, "archive": archive_stats,
            "elapsed_sec": int(elapsed), "warnings": self.warnings,
        }

    # ------------------------------------------------ 存档阶段

    def _archive(self) -> dict:
        from .. import archive

        keep = int((self.cfg.get("archive") or {}).get("keep_snapshots", 14))
        r = archive.run_all(keep_snapshots=keep)
        f = r["facts"]
        # ★ "行数变少被拒绝"必须冒泡成 warning 让人看见。
        #   存档默默保护住了旧数据是好事，但**为什么会变少**需要有人去查 ——
        #   悄悄记在 manifest 里没人会翻。
        if f.get("refused"):
            detail = "；".join(
                f"{t} 分区 {p}（现 {now} 行 / 存档时 {before} 行）"
                for kind, t, p, now, before in f.get("details", [])
                if kind == "refused")
            self.warnings.append(
                f"★ 存档拒绝覆盖 {f['refused']} 个分区：源库行数比上次存档时**少了**，"
                f"旧存档已保留。请查清是谁删的 —— {detail}")
        log.info("存档完成：新写 %d 个分区 / %d 行，快照 %s",
                 f.get("written", 0), f.get("rows", 0), r.get("snapshot"))
        return {"written": f.get("written", 0), "rows": f.get("rows", 0),
                "skipped": f.get("skipped", 0), "refused": f.get("refused", 0),
                "snapshot": r.get("snapshot"), "dir": r.get("dir")}

    # ------------------------------------------------ VOC 阶段

    def _collect_voc(self, run_id: int, plan_id: int | None) -> dict:
        """抓评论 + 分析。

        抓谁：优先「评论量已知且高」的商品（主销款），其次是本轮新抓到价格的商品。
        用户要求"所有产品都要抓"，但一轮跑不完时先抓主销款 ——
        评论量本身就是销量的公开代理指标，主销款的口碑信息密度最高。
        """
        from ..scraping.voc import VocCollector

        if self.engine_ref is None:
            raise RuntimeError("VOC 阶段需要抓取引擎，但引擎已关闭")

        voc_cfg = self.cfg.get("voc", {})
        max_targets = int(voc_cfg.get("max_products_per_run", 150))
        budget_sec = float(voc_cfg.get("stage_budget_seconds", 6 * 3600))

        # ★ 必须有 LIMIT。之前只有 ORDER BY 没有 LIMIT，
        #   单品上限 240 秒 × 无限个目标 ⇒ VOC 阶段可以吃掉整整一天，
        #   把后面的规格补全/竞品匹配/情报全部挤没 —— 而且看不出是它的问题。
        #
        # ★★ 排序必须**按国家分区**，不能全拉美拉通排 `sale_price DESC`。
        #   原写法等于**按币值大小排名**：同一轮里 COP 均价 168 万、CLP 32 万、
        #   MXN 7922、BRL 2687、PEN 1110 —— 全是本币，量纲差三个数量级。
        #   结果前 150 名 = 哥伦比亚 121 + 智利 29，**墨西哥候选 1028 个一个没进**，
        #   秘鲁、巴西同样归零。这就是评论只有 CL/CO 两国的真因
        #   （不是那几国没抓价格 —— 五国的价格页都在，MX 反而是最多的）。
        #   本项目其他地方（price_audit / strategy）早就立了"跨币种不混算"的规矩，
        #   这条查询是唯一漏网的。sale_price_usd 列全库 14955 行**全是 NULL**、
        #   且无人消费，所以不能靠换算兜底，只能分区。
        #   分区内部再按本币排就是对的 —— 同国同币，量纲一致。
        # ★ 用 rn 轮转而不是固定配额：各国先上第 1 名，再各上第 2 名……
        #   某国候选用完了其他国家自动接着填，不会浪费预算。
        targets = db.q("""
            WITH cand AS (
                SELECT po.url                       AS url,
                       MIN(po.country_code)         AS country_code,
                       MIN(po.channel_id)           AS channel_id,
                       MIN(po.rival_product_id)     AS rival_product_id,
                       MIN(po.title)                AS title,
                       MAX(po.sale_price)           AS sale_price,
                       COALESCE(MAX(rpf.total_reviews), 0) AS known_reviews
                FROM price_obs po
                LEFT JOIN review_profile rpf ON rpf.product_url = po.url
                WHERE po.run_id = ? AND po.url IS NOT NULL AND po.url <> ''
                  AND po.audit_status <> 'rejected'
                  AND (rpf.last_fetched IS NULL
                       OR rpf.last_fetched < datetime('now','-3 day'))
                GROUP BY po.url
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                           -- ★★ 必须**同时按国家与渠道**分区。只按国家分区时，
                           --   组内仍是 `sale_price DESC` 全局排名，而各渠道的
                           --   价格量级不同：墨西哥的 Liverpool 最高 17.1 万、
                           --   Sanborns 13.1 万、Sears 8.9 万 —— 前 30 名被前两家
                           --   占满，**Sears 的 4769 条价格观测一次都没被 VOC 试过**。
                           --   这和当初"按币值大小排名"是同一个病根：
                           --   在不可比的组之间做全局排名。
                           --   加上 channel_id 之后每个渠道都能轮到第 1 名，
                           --   评论区结构不同的渠道才有机会暴露出来。
                           PARTITION BY country_code, channel_id
                           ORDER BY known_reviews DESC, sale_price DESC
                       ) AS rn
                FROM cand
            )
            , yield_rate AS (
                -- 各渠道的历史产出：**每个商品平均抓到多少条评论**。
                -- ★ 不能用"命中率"（有没有出评论）：实测 Falabella 智利
                --   923 条 / 147 个商品 = 6.3 条/个，Alkosto 125/65 = 1.9 条/个 ——
                --   按命中率两家都算"高产"，按量差 3 倍。我们要的是**量**。
                -- ★ 除以 5 只是把常见量级归一到 1 附近，好和"未试过给 1.0"可比；
                --   上限 2.0 防止某个特别高产的渠道把别人全挤掉。
                SELECT channel_id,
                       MIN(MAX(AVG(fetched_reviews) / 5.0, 0.15), 2.0) AS yr
                FROM review_profile GROUP BY channel_id
            )
            SELECT r.url, r.country_code, r.channel_id, r.rival_product_id,
                   r.title, r.known_reviews
            FROM ranked r LEFT JOIN yield_rate y ON y.channel_id = r.channel_id
            -- ★ 先按轮次（保证每个渠道都能轮到第 1 名 —— 这是**发现**），
            --   同一轮次内再按历史产出率降序（把边际名额给真能出评论的渠道
            --   —— 这是**产出**）。只做前者会把预算平摊给 0 产出的渠道：
            --   实测平摊后 Falabella 智利从 45 个目标掉到 17 个，
            --   而它是目前唯一稳定出评论的渠道。
            -- ★★ 产出率要**乘进排名本身**，不能只当同分时的 tiebreaker。
            --   只当 tiebreaker 时 `ORDER BY rn` 占绝对主导：300 个名额 ÷ 18 个渠道
            --   = 每家 17 个，产出率只影响被 LIMIT 切掉的那一两行，等于没用。
            --   改成 rn / 产出率 之后：产出率 1.0 的渠道有效排名 = rn，
            --   产出率 0 的（下限 0.15）= 6.7×rn，名额自然向能出评论的渠道倾斜，
            --   但**每个渠道的第 1 名仍然排得很靠前**，发现能力不丢。
            ORDER BY r.rn / MAX(COALESCE(y.yr, 1.0), 0.15),
                     r.known_reviews DESC, r.country_code
            LIMIT ?
        """, (run_id, max_targets))
        if not targets:
            return {"targets": 0}

        collector = VocCollector(self.engine_ref, run_id, self.cfg)
        t_start = time.time()
        done, skipped = 0, 0
        for t in targets:
            # 阶段时间预算：到点就停，并**如实报告还剩多少没抓**，
            # 不能静默截断让人以为"就这么多商品有评论"
            if time.time() - t_start > budget_sec:
                skipped = len(targets) - done
                log.warning("VOC 阶段已用满 %.1f 小时预算，剩余 %d 个商品下轮再抓",
                            budget_sec / 3600, skipped)
                self.warnings.append(
                    f"VOC 阶段时间预算用尽，本轮跳过 {skipped} 个商品的评论抓取"
                    f"（按评论量排序，跳过的是评论较少的）")
                break
            try:
                collector.collect_for_product(
                    t["url"], country=t["country_code"], channel_id=t["channel_id"],
                    rival_product_id=t["rival_product_id"],
                    product_title=t["title"] or "")
                done += 1
            except Exception as e:  # noqa: BLE001
                log.debug("VOC 单品失败 %s: %s", t["url"][:70], str(e)[:100])
                done += 1

        agent = VocAgent(self.llm, self.cfg["agents"], plan_id=plan_id)
        analysis = agent.run()
        return {"targets": len(targets), "processed": done, "skipped_budget": skipped,
                **collector.stats, "analysis": analysis}

    # ------------------------------------------------ 阶段容错

    def _stage(self, name: str, fn):
        """跑一个阶段，失败不打断流水线，但**必须留下能查的痕迹**。

        ★ 关键教训：曾经用裸 `except Exception` + `str(e)[:200]` 记录，
          结果一个 NameError（VocAgent 忘了 import）被记成
          "VOC 阶段失败: name 'VocAgent' is not defined"，
          看起来像普通的运行时波动，实际是整个阶段从来没跑过。
          所以这里必须记异常**类型**和完整堆栈 —— 拼写/导入错误
          和网络超时长得完全不一样，日志必须能一眼分辨。
        """
        try:
            result = fn()
            log.info("阶段「%s」完成", name)
            return result or {}
        except Exception as e:  # noqa: BLE001
            kind = type(e).__name__
            log.exception("阶段「%s」失败（%s），继续后续阶段", name, kind)
            self.warnings.append(f"阶段「{name}」失败：{kind}: {str(e)[:180]}")
            # NameError / AttributeError / ImportError 几乎必然是代码 bug 而非环境波动，
            # 单独标出来，别让它混在网络错误里被忽略
            if isinstance(e, (NameError, AttributeError, ImportError, TypeError)):
                self.warnings.append(
                    f"★「{name}」的失败是 {kind}，这通常是代码缺陷不是网络问题，请检查日志堆栈")
            return {"error": f"{kind}: {str(e)[:200]}"}

    # ------------------------------------------------ 采集阶段

    def _collect(self, run_id: int, plan: dict, rotation: str | None) -> dict:
        countries = {c["code"]: c for c in db.q(
            "SELECT * FROM country WHERE enabled=1")}
        # ★★ 我方品牌**必须**在列表里，不能只抓竞品。
        #   原因：`my_pricing` 表一直是 0 行，而看板上所有「我方 vs 友商」的图
        #   都卡在这上面。用户 2026-08-11 已定过口径 ——
        #   「商城的价格就是官方定价」，所以不手工录入，直接抓Acme自营商城。
        #   四个Acme商城渠道（MX/CO/CL/PE）也早就配好了、URL 也验证过有价格，
        #   唯独这里 `is_ours=0` 把我方品牌整个滤掉，于是那四个渠道
        #   **一个采集单元都没排过**（scrape_unit 里零记录），配了等于没配。
        #
        #   放进来之后两件事同时成立：
        #   · 品牌商城：collector.py 的 brand_store 闸门保证只有Acme商城会搜Acme
        #     （Samsung 商城搜Acme会被判 skipped）；
        #   · 零售渠道：显式搜我方品牌，拿到的是Acme在第三方的**零售价**，
        #     和商城的官方价是两个口径，正好用来看渠道有没有乱价。
        brands = db.q("SELECT * FROM brand WHERE enabled=1")
        if self.force_brands:
            want = {b.lower() for b in self.force_brands}
            brands = [b for b in brands if b["name"].lower() in want]
        brand_cats = self._brand_categories()

        # 本轮要抓的产业：轮值产业做全量；其余产业只抓重点型号
        all_cats = [c["code"] for c in db.q(
            "SELECT code FROM category WHERE enabled=1 ORDER BY sort_order")]
        if self.force_categories:
            all_cats = [c for c in all_cats if c in self.force_categories]

        scrape_cfg = dict(self.cfg["scrape"])
        meli_token = db.get_setting("meli_access_token", "")
        stats = {"units": 0, "rows": 0, "skipped": 0, "by_status": {}}

        # ★★ 按【域名】分组并行。
        #
        #   并行抓取的瓶颈不是机器性能，是**风控**：同一域名并发请求
        #   在站点看来就是"一个 IP 异常高频访问"，正是反爬要抓的特征。
        #   Liverpool 单线程都会命中验证码，并发只会更快被拉黑。
        #
        #   但**跨域名并行是安全的** —— Liverpool 与 Falabella 是两套
        #   互不相干的风控系统，同时抓不会互相加权。
        #   所以正确的并行粒度是：**组间并行、组内串行**，按域名分组。
        #
        #   每个 worker 必须有**自己的浏览器实例** —— ScrapeEngine 持有
        #   driver 状态（当前国家/设备/Cookie），多线程共用会互相踩。
        host_groups: dict[str, list[dict]] = {}
        for u in plan["units"]:
            cc = u["country"]
            if self.force_countries and cc not in self.force_countries:
                continue
            ch = db.q1("SELECT * FROM channel WHERE id=?", (u["channel_id"],))
            if not ch or cc not in countries:
                continue
            host = (ch["base_url"] or "").split("//")[-1].split("/")[0] or f"ch{ch['id']}"
            host_groups.setdefault(host, []).append({"unit": u, "channel": ch})

        workers = max(1, int(scrape_cfg.get("parallel_workers", 1)))
        workers = min(workers, len(host_groups)) or 1
        stats["workers"] = workers
        stats["host_groups"] = len(host_groups)
        log.info("采集并行度：%d 个 worker / %d 个域名组", workers, len(host_groups))
        livelog.emit("stage", f"并行采集：{workers} 个浏览器同时跑 "
                              f"{len(host_groups)} 个站点（同站内部仍串行，避免触发风控）")

        lock = threading.Lock()

        def run_group(host: str, members: list[dict]) -> None:
            """一个域名组 = 一个独立浏览器实例，组内串行。"""
            eng = ScrapeEngine(scrape_cfg, profile_tag=host)
            try:
                for m in members:
                    if m["unit"]["action"] == ACT_FALLBACK:
                        eng.forced_fallback_hosts.add(host)
                col = Collector(eng, run_id, scrape_cfg, meli_token)
                for m in members:
                    u, channel = m["unit"], m["channel"]
                    cc = u["country"]
                    country = countries[cc]

                    # 降频：主 Agent 判定该渠道近期被拦过
                    if u["action"] == ACT_SLOW:
                        col.cfg = {**scrape_cfg,
                                   "min_delay": scrape_cfg.get("min_delay", 2) * 2,
                                   "max_delay": scrape_cfg.get("max_delay", 5) * 2}
                        col.max_items = max(6, col.max_items // 2)
                    else:
                        col.cfg = scrape_cfg
                        col.max_items = int(scrape_cfg.get("max_products_per_query", 20))

                    # ★ 整站被拦时只跳过【当前渠道】的剩余品牌，继续下一个渠道。
                    try:
                        for cat in all_cats:
                            if rotation is not None and cat != rotation:
                                col.max_items = min(col.max_items, 8)
                            for brand in brands:
                                if cat not in brand_cats.get(brand["name"], set()):
                                    continue
                                try:
                                    rows, st = col.collect_unit(
                                        channel, country, brand, cat)
                                    with lock:
                                        stats["units"] += 1
                                        stats["rows"] += rows
                                        stats["by_status"][st] = \
                                            stats["by_status"].get(st, 0) + 1
                                    if st in ("blocked", "login_wall"):
                                        raise _ChannelBlocked(st)
                                except _ChannelBlocked:
                                    raise
                                except Exception as e:  # noqa: BLE001
                                    log.warning("采集单元异常 %s/%s/%s: %s", cc,
                                                channel["name"], brand["name"],
                                                str(e)[:120])
                                    db.log_unit(run_id, channel_id=channel["id"],
                                                country=cc, brand_id=brand["id"],
                                                category=cat, status="failed",
                                                message=str(e)[:200])
                    except _ChannelBlocked as blocked:
                        reason = str(blocked) or "blocked"
                        log.info("  %s 命中 %s，跳过该渠道剩余品牌", channel["name"], reason)
                        with lock:
                            stats["skipped"] += 1
                            self.warnings.append(
                                f"{channel['name']}（{cc}）命中 {reason}，已跳过该渠道")
                        continue
            finally:
                # 每个 worker 关自己的浏览器。不关会累积孤儿 chrome 进程 ——
                # 并行下这个问题被放大 N 倍。
                try:
                    eng.close()
                except Exception:  # noqa: BLE001
                    log.warning("[%s] 关闭浏览器出错", host, exc_info=True)

        if host_groups:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="scrape") as pool:
                futs = {pool.submit(run_group, h, m): h
                        for h, m in host_groups.items()}
                for fut in as_completed(futs):
                    h = futs[fut]
                    try:
                        fut.result()
                    except Exception as e:  # noqa: BLE001
                        log.exception("域名组 %s 整体失败", h)
                        self.warnings.append(f"站点 {h} 采集整体失败：{type(e).__name__}")
                    else:
                        livelog.emit("stage", f"✓ {h} 抓完")
        return stats

    @staticmethod
    def _brand_categories() -> dict[str, set]:
        cfg = config.load_brands()
        return {b["name"]: set(b.get("categories") or [])
                for b in (cfg.get("brands") or [])}


class _ChannelBlocked(Exception):
    """内部信号：整站被拦，跳到下一个渠道"""


def _safe_collect(orch: Orchestrator, *a, **kw):
    try:
        return orch._collect(*a, **kw)
    except _ChannelBlocked:
        return {}
