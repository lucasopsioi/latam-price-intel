# -*- coding: utf-8 -*-
"""情报 Agent —— 每天扫全球与拉美信息源，发现新品、判定重要度、记录上市节奏。

用户要求：
  "扫描一下全网有没有新增的产品，全球有没有新增的产品，这个 agent 要一个 list，
   去扫全球 top 级别的网站"
  "有一个 agent 专门待在情报分析板块里分析友商出了哪些产品"

四步：
  ① 采集：全球科技站 RSS + 各国新闻 RSS（+ 可选 X 官方 API）
  ② 抽取：LLM 从标题/摘要里抽出「品牌 / 产品 / 事件类型 / 国家 / 日期」
  ③ 新品发现：抽到的型号与 rival_product 比对，没见过的建档并标记
                「全球已发布」；对照价格观测判断它在拉美哪些国家已经上市
  ④ 上市节奏：写 launch_event（全球首发 / 各国开卖），算出相对首发的滞后天数
                与国家上市顺序 —— 这就是上市看板的数据来源

★ 为什么"全球已发布但拉美没上市"这件事有价值：
  它是 销售团队 最想要的前瞻信息 —— 友商的新品在别处已经开卖，说明拉美上市
  已进入倒计时，可以据此提前准备对位机型与定价。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from .. import db
from ..config import load_brands
from ..scraping import news
from .base import BaseAgent
from .llm import as_dicts, as_text, batch_key, pick_by_key

log = logging.getLogger("intel")

EVENT_NEW_PRODUCT = "new_product"
IMPORTANCE_LABEL = {5: "重大", 4: "重要", 3: "一般", 2: "次要", 1: "边缘", 0: "无关"}


class IntelAgent(BaseAgent):
    name = "intel"
    role = "intel"
    description = "扫全球与拉美信息源，发现新品、判定重要度、记录上市节奏"

    def run(self, rotation_category: str | None = None,
            max_per_source: int = 40) -> dict:
        self.start(f"情报扫描 轮值={rotation_category}")
        proxy = db.get_setting("proxy", "") or ""
        x_token = db.get_setting("x_bearer_token", "")

        raw = self._collect_sources(proxy, x_token, rotation_category, max_per_source)
        if not raw:
            self.finish("ok", "所有情报源均无返回（可能需要配置代理）", 0, 0)
            return {"collected": 0, "new_products": 0,
                    "warning": "情报源无返回：Google News 与多数海外站点在国内网络"
                               "通常需要代理，请在设置页填 proxy"}

        stored = self._store_raw(raw)
        analyzed = self._analyze(raw)
        new_products, launches = self._discover_new_products(analyzed)

        summary = (f"采集 {len(raw)} 条（新增入库 {stored}），"
                   f"识别新品 {len(new_products)} 个，写入上市事件 {launches} 条")
        self.finish("ok", summary, len(raw), len(new_products))
        return {"collected": len(raw), "stored": stored,
                "new_products": len(new_products), "launch_events": launches,
                "products": new_products[:20]}

    # ------------------------------------------------ ① 采集

    def _collect_sources(self, proxy: str, x_token: str,
                         rotation: str | None, limit: int) -> list[dict]:
        out: list[dict] = []

        g = news.fetch_global_tech(proxy, limit)
        # ★ 品类原本只在「品牌×品类」检索循环里赋值，全球源与本地源一律为空 ——
        #   实测 2399 条动态里 1184 条（49%）没有品类，
        #   一加品类筛选就会把一半情报藏起来。从标题推一个，推不出就留空。
        for it in g:
            it["category_code"] = _guess_category_from_text(
                f"{it.get('title', '')} {it.get('summary', '')}")
        out.extend(g)
        self.log_step("扫全球科技站", parsed={"条数": len(g),
                                              "站点": [n for n, _ in news.GLOBAL_TECH_FEEDS]},
                      decision="ok" if g else "empty",
                      reason=f"从 {len(news.GLOBAL_TECH_FEEDS)} 个全球站点取回 {len(g)} 条"
                             + ("" if g else "；全为空通常是网络需要代理"),
                      status="ok" if g else "degraded")

        countries = db.q("SELECT * FROM country WHERE enabled=1 ORDER BY sort_order")
        brands = db.q("SELECT * FROM brand WHERE enabled=1 AND is_ours=0")
        cats = load_brands().get("categories") or {}
        cat_codes = [rotation] if rotation else list(cats.keys())

        # ★ 这个双重循环是 国家 × 品类 × 品牌 的**串行** HTTP 检索：
        #   不轮值时是 6×5×12 = 360 个请求，实测要跑二十多分钟。
        #   老代码整段跑完才写一次 log_step —— 界面上二十分钟一动不动，
        #   和"卡死了"完全分不出来（这正是 VOC 那次踩过的坑，只是深了一层）。
        #   所以按国家逐个上报进度，并把预计请求数先摆出来。
        planned = len(countries) * len(cat_codes) * min(len(brands), 12)
        self.log_step("规划新闻检索", parsed={
            "国家": len(countries), "品类": len(cat_codes),
            "品牌": min(len(brands), 12), "预计请求数": planned},
            decision="ok",
            reason=f"国家×品类×品牌 串行检索共 {planned} 个请求"
                   + ("（未指定轮值品类，本轮扫全部品类，耗时较长）"
                      if not rotation else f"（轮值品类 {rotation}）"))

        import json as _json
        for ci, co in enumerate(countries, 1):
            local = news.fetch_latam_local(co["code"], proxy)
            for it in local:
                # ★ 只有**本土**媒体的文章才能标成这个国家。
                #   FayerWayer / Xataka 是泛拉美媒体，把它们的文章记成
                #   "智利的动态"是伪造归属 —— 看板会显示"智利本周 X 条品牌动态"，
                #   而那些事根本没发生在智利。区域源留空，除非正文点了国名。
                if it.get("feed_scope") == "regional":
                    it["country_code"] = _country_in_text(
                        f"{it.get('title', '')} {it.get('summary', '')}")
                else:
                    it["country_code"] = co["code"]
                it["category_code"] = _guess_category_from_text(
                    f"{it.get('title', '')} {it.get('summary', '')}")
            out.extend(local)

            for cat in cat_codes:
                term = ((cats.get(cat) or {}).get("search_terms") or {}).get(
                    co["lang"], "")
                for b in brands[:12]:      # 控制请求量：每国每产业最多 12 个品牌
                    try:
                        aliases = _json.loads(b["aliases"] or "[]")
                    except Exception:  # noqa: BLE001
                        aliases = [b["name"]]
                    items = news.search_brand_news(
                        aliases or [b["name"]], term, co, proxy, days=7, limit=20)
                    for it in items:
                        it["country_code"] = co["code"]
                        it["brand_id"] = b["id"]
                        it["brand_name"] = b["name"]
                        it["category_code"] = cat
                    out.extend(items)

            if x_token:
                for b in brands[:6]:
                    tw = news.fetch_x_posts(x_token, b["name"], co["lang"])
                    for it in tw:
                        it["country_code"] = co["code"]
                        it["brand_id"] = b["id"]
                    out.extend(tw)

            # 每个国家扫完报一次：进度看得见，也能看出是哪个国家慢
            self.log_step(f"扫完 {co['name_zh']}（{ci}/{len(countries)} 国）",
                          parsed={"累计条数": len(out)}, decision="ok",
                          reason=f"{co['name_zh']} 的本地媒体 + 品牌检索已完成")

        self.log_step("扫拉美与各国新闻", parsed={"总条数": len(out)},
                      decision="ok" if out else "empty",
                      reason=f"六国新闻 + 品牌检索共 {len(out)} 条")
        return out

    # ------------------------------------------------ 入库

    def _store_raw(self, items: list[dict]) -> int:
        n = 0
        with db.tx() as conn:
            for it in items:
                url = it.get("url") or ""
                h = db.row_hash(url or it.get("title", ""))
                cur = conn.execute("""
                    INSERT OR IGNORE INTO dynamics(brand_id,country_code,category_code,
                        source_type,source_name,title,url,published_at,raw_text,url_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                """, (it.get("brand_id"), it.get("country_code"),
                      it.get("category_code"),
                      "x" if it.get("source") == "X" else "news",
                      it.get("source"), it.get("title", "")[:300], url,
                      _parse_date(it.get("published_raw")), it.get("summary", "")[:2000], h))
                if cur.rowcount:
                    n += 1
        return n

    # ------------------------------------------------ ② 抽取

    def _analyze(self, items: list[dict], batch: int = 15) -> list[dict]:
        """LLM 从新闻里抽结构化情报。没配 Key 时退回关键词规则。"""
        if not (self.llm and self.llm.available()):
            rule = [x for x in items if _looks_like_launch(x.get("title", ""))]
            self.log_step("情报抽取", parsed={"规则命中": len(rule)},
                          decision="degraded", status="degraded",
                          reason="未配置 API Key，退回关键词规则识别新品发布"
                                 "（准确率低于模型，建议配置）")
            return [{"title": x.get("title"), "brand": None, "product": None,
                     "event": EVENT_NEW_PRODUCT, "country": x.get("country_code"),
                     "importance": 3, "summary_zh": "", "_src": x} for x in rule]

        results = []
        for i in range(0, min(len(items), 150), batch):
            chunk = items[i:i + batch]
            keys = [_line_key(x) for x in chunk]
            lines = [_prompt_line(j, x, keys[j]) for j, x in enumerate(chunk)]
            prompt = (
                "你是消费电子行业竞品情报分析师，服务对象是Acme在拉美的 销售团队。\n"
                "下面是一批科技新闻（标题 — 正文摘录）。请逐条判断并抽取结构化信息。\n\n"
                "重点关注：新品发布、上市开卖、价格调整、大促活动、渠道合作、广告投放。\n"
                "与消费电子（手机/穿戴/音频/平板/PC）无关的，importance 给 0。\n\n"
                + "\n".join(lines) + "\n\n"
                '只返回 JSON 数组，每条：{"idx":序号,"k":"该条括号里的 k 值原样抄回",'
                '"brand":"品牌名或null",'
                '"product":"具体产品型号或null","event":"new_product|launch|price|promo|ad|other",'
                '"country":"两位国家码或global","importance":0到5的整数,'
                '"summary_zh":"一句话中文摘要，不超过40字"}\n'
                "★ product 只填明确的型号名（如 Galaxy S25 Ultra），泛指的填 null；"
                "型号名要从**正文**里找，标题常常不写。\n"
                "★ summary_zh 的主语必须是**做事的那家公司**，宾语必须是**它自己的具体产品**。"
                "参照物只能放括号里，且只在原文本身拿别家产品做对比时才写，不要自己加。"
                "标题经常拿竞争对手当参照物（三星站写"
                "「Xiaomi unveils its Galaxy Z Fold 8 rival」），正确写法是"
                "「小米发布 Xiaomi 18 Fold 折叠屏（对标三星 Galaxy Z Fold 8）」，"
                "禁止写成「小米发布 Galaxy Z Fold 8 竞品」—— 读者会以为小米发布了三星的机器。\n"
                "★ brand 填做事的那家公司，不是被拿来对比的那家；"
                "写原文里的英文品牌名（Apple / Xiaomi / Acme），不要译成中文。\n"
                "★ k 必须一字不差抄回，用于核对序号没有错位；抄错的整条作废。")

            parsed = self.ask_json(f"情报抽取 {i}~{i + len(chunk) - 1}", prompt,
                                   system="你是严谨的情报分析师，只输出 JSON。",
                                   input_ref=f"batch:{i}", default=[])
            # ★ 序号核对。2026-09-04 实测一批 15 条里模型把 #9168（小米 18 Fold）的
            #   序号配上了另一条（Apple Watch Series 12）的内容 —— 品牌核对拦不住，
            #   因为原文里恰好有 "Apple's event"。品牌对、内容错，人眼看不出来。
            #   核对码抄错 = 内容不属于这条，整条拒收；同一序号回了两次只收第一条。
            #   定位**只认核对码，不用模型写的序号**（knowledge/lessons/
            #   llm-batch-result-alignment：序号是模型自己写的，1-based、丢条目后
            #   重编号都实测见过；VOC 翻译那处早已按这一课修过，这里一直没接）。
            picked, kstats = pick_by_key(parsed, keys)
            for j, item in picked:
                item["_src"] = chunk[j]
                results.append(item)
            rejected, dropped = kstats["rejected"], kstats["dropped"]
            if rejected or dropped:
                log.warning("[intel] 批次 %d：核对码不符拒收 %d 条，模型未返回 %d 条（保留原摘要）",
                            i, rejected, dropped)
                self.log_step("抽取序号核对", input_ref=f"batch:{i}", parsed=kstats,
                              decision="rejected" if rejected else "partial",
                              status="degraded" if rejected else "ok",
                              reason=("核对码抄错 = 内容属于别的条目，整条不采信"
                                      if rejected else "模型漏答的条目保留原摘要"))

        # 回写摘要与重要度 —— 以及**品牌**。
        # ★ 原来这里只写 summary/tag/importance，模型抽出的 brand 直接扔掉；
        #   而全球科技站的条目入库时 brand_id 就是空的（只有「品牌×品类」检索
        #   循环会赋值，且赋的是**检索词**的品牌，不是内容的品牌 —— 和品类那次
        #   是同一个病根：属性来自搜索意图而非内容本身）。
        #   实测 30 天 3566 条无品牌（33%），其中 1306 条正文明写着品牌名。
        #   纪律：模型报的品牌必须原文可核对才写；已挂的品牌若原文根本没提、
        #   而模型报的品牌原文有，才改绑（同 brandintel 的 rebind 规则）。
        from .brandintel import _alias_map, _hit
        alias_map = _alias_map()
        name2id = {b["name"].lower(): b["id"] for b in db.q("SELECT id,name FROM brand")}
        bound = rebound = 0
        with db.tx() as conn:
            for r in results:
                src = r.get("_src") or {}
                h = db.row_hash(src.get("url") or src.get("title", ""))
                conn.execute("""
                    UPDATE dynamics SET summary_zh=?, tag=?, importance=?
                    WHERE url_hash=?
                """, (str(r.get("summary_zh") or "")[:300],
                      _tag_zh(str(r.get("event") or "other")),
                      int(r.get("importance") or 0), h))
                text = f"{src.get('title') or ''} {src.get('summary') or ''}"
                bid = _verified_brand(r.get("brand"), text, alias_map, name2id)
                if bid is None:
                    continue
                row = conn.execute("SELECT brand_id FROM dynamics WHERE url_hash=?",
                                   (h,)).fetchone()
                cur_b = row[0] if row else None
                if cur_b is None:
                    conn.execute("UPDATE dynamics SET brand_id=? WHERE url_hash=?", (bid, h))
                    bound += 1
                elif cur_b != bid and not _hit(text, alias_map.get(cur_b) or []):
                    conn.execute("UPDATE dynamics SET brand_id=? WHERE url_hash=?", (bid, h))
                    rebound += 1
        if bound or rebound:
            log.info("[intel] 按原文核对挂品牌：新挂 %d 条，改绑 %d 条（原品牌原文无据）",
                     bound, rebound)
        return results

    # ------------------------------------------------ ③④ 新品与上市节奏

    def _discover_new_products(self, analyzed: list[dict]) -> tuple[list[dict], int]:
        brands = {b["name"].lower(): b for b in db.q("SELECT * FROM brand")}
        new_products, launch_rows = [], 0

        for r in analyzed:
            # ★ 过 as_text：模型对"没提到具体型号"的回答常是字符串 "null"，
            #   长度 4 能过下面的门槛，结果看板上真出现过一台叫 "null" 的 Honor。
            model = as_text(r.get("product"))
            brand_name = as_text(r.get("brand"))
            if not model or len(model) < 3 or not brand_name:
                continue
            if _is_software(model):
                # 系统/服务照常留在情报流里（发布会本身是有效情报），
                # 只是不建产品、不记上市事件
                log.info("[intel] 跳过系统/服务，不建产品：%s %s", brand_name, model)
                continue
            b = brands.get(brand_name.lower())
            if not b or b["is_ours"]:
                continue
            if int(r.get("importance") or 0) < 3:
                continue

            model_key = re.sub(r"[^a-z0-9]", "", model.lower())
            cat = _guess_category(model, r.get("_src", {}).get("category_code"))
            existing = db.q1("""SELECT id, global_launch_date FROM rival_product
                                WHERE brand_id=? AND model_key=?""",
                             (b["id"], model_key))
            src = r.get("_src") or {}
            pub = _parse_date(src.get("published_raw")) or db.today()

            if existing:
                pid = existing["id"]
                if not existing["global_launch_date"] and r.get("event") in (
                        "new_product", "launch"):
                    with db.tx() as conn:
                        conn.execute("""UPDATE rival_product SET global_launch_date=?,
                                        updated_at=datetime('now') WHERE id=?""", (pub, pid))
            else:
                with db.tx() as conn:
                    cur = conn.execute("""
                        INSERT INTO rival_product(brand_id,category_code,model_name,
                            model_key,global_launch_date,spec_source,spec_confidence)
                        VALUES(?,?,?,?,?,?,?)
                    """, (b["id"], cat, model[:80], model_key, pub, "agent:intel", 0.6))
                    pid = cur.lastrowid
                new_products.append({"brand": b["name"], "model": model,
                                     "category": cat, "date": pub,
                                     "source": src.get("source")})
                self.log_step("发现新品", input_ref=f"rival:{pid}",
                              parsed={"品牌": b["name"], "型号": model,
                                      "产业": cat, "首见日期": pub},
                              decision="created",
                              reason=f"{b['name']} {model} 在 {src.get('source')} "
                                     f"首次出现，此前数据库无记录")

            # ★ 单条写失败不能带走整个阶段。
            #   本轮实测：一条越南新闻的国家码撞外键，直接把"识别新品 + 上市节奏"
            #   整个阶段炸掉，前面 150 条分析的成果一条都没落地。
            #   逐条兜住，但**把异常类型打出来** —— 外键/类型错是代码或数据缺陷，
            #   不是网络波动，不能让它悄悄退化成"今天没有新品"。
            try:
                launch_rows += self._record_launch(pid, b["id"], r, pub, src)
            except Exception as e:  # noqa: BLE001
                log.warning("[intel] 写上市事件失败（%s）product=%r country=%r: %s",
                            type(e).__name__, r.get("product"), r.get("country"),
                            str(e)[:120])
                self.log_step("写上市事件失败", input_ref=f"rival:{pid}",
                              decision="error", status="degraded",
                              reason=f"{type(e).__name__}: {str(e)[:200]}"
                                     f"（单条跳过，不影响其余；外键/类型错属代码缺陷）")

        return new_products, launch_rows

    def _record_launch(self, pid: int, brand_id: int, r: dict,
                       pub: str, src: dict) -> int:
        """写上市事件 + 算相对全球首发的滞后天数与国家顺序"""
        # ★ 模型返回的国家码不能直接落库。
        #   情报源是全球科技站，新闻里全是 España / Vietnam / China / India ——
        #   模型如实返回 ES/VN/CN/IN，而 launch_event.country_code 有外键指向
        #   我们只有 6 个拉美国家的 country 表 ⇒ FOREIGN KEY constraint failed
        #   **整个情报阶段当场挂掉**（实测：本轮 0 条上市事件，异常吞在阶段级）。
        #   这是"lowercase mx 撞外键"的同一类错，只是这次值来自模型而非配置。
        country = str(r.get("country") or "").strip().upper()
        cc = country if (country and country != "GLOBAL" and len(country) == 2) else None
        if cc and cc not in _covered_countries():
            # 非我们覆盖的国家：对"全球首发→拉美滞后"这块看板没有用，
            # 但**不能记成 global_launch** —— 那会把"越南开卖"伪装成全球首发，
            # 后面所有 days_after_global 都跟着错。宁可不记，并留一行日志。
            log.info("[intel] 跳过非覆盖国家的上市事件：%s（%s）", cc, r.get("product"))
            return 0

        # ★★ 事件类型必须看**新闻讲的是什么**，不能只看有没有解析出国家码。
        #   原来写的是 `global_launch if cc is None else country_available` ——
        #   于是任何没带国家的新闻都成了"全球首发"。实测 12 条里 9 条是噪声：
        #   降价促销（Bose 降 50 美元）、评测（Moto Watch 评测）、
        #   传闻（苹果"或"采用 Ultra 命名）、公测固件……75% 噪声。
        #   ⇒ 只有真发布/开卖才进上市表，其余留在情报流里就好。
        #
        # ★ 判定放在**确定性代码**里而不是让模型自己报类型：
        #   本项目已验证「让模型自我否决」不可靠（自评不达标仍照交 54~62%），
        #   而"照抄/格式类"任务模型近乎百分百可靠 —— 所以模型只负责给摘要，
        #   分类由这里的规则做。
        kind = _event_kind(f"{r.get('product') or ''} {r.get('summary_zh') or ''} "
                           f"{src.get('title') or ''}")
        if kind != "launch":
            log.info("[intel] 非上市新闻不进上市表（判为 %s）：%s",
                     kind, str(r.get("summary_zh"))[:40])
            return 0

        event_type = ("global_launch" if cc is None else "country_available")

        gl = db.q1("SELECT global_launch_date FROM rival_product WHERE id=?", (pid,))
        days_after = None
        if cc and gl and gl["global_launch_date"]:
            try:
                days_after = (date.fromisoformat(pub)
                              - date.fromisoformat(gl["global_launch_date"])).days
            except Exception:  # noqa: BLE001
                days_after = None

        rank = None
        if cc:
            prior = db.q1("""SELECT COUNT(*) c FROM launch_event
                             WHERE rival_product_id=? AND event_type='country_available'
                               AND event_date < ?""", (pid, pub))
            rank = (prior["c"] or 0) + 1

        with db.tx() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO launch_event(rival_product_id,brand_id,country_code,
                    event_type,event_date,evidence_url,days_after_global,country_rank,
                    detected_by,confidence,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (pid, brand_id, cc, event_type, pub, src.get("url"),
                  days_after, rank, "agent:intel", 0.6,
                  str(r.get("summary_zh") or "")[:200]))
            return 1 if cur.rowcount else 0


# 判定顺序有讲究：先排除"不是上市"的，最后才认上市。
# 「Galaxy Z Fold 8 上市，市场反应平淡」既含"上市"也含评价词 —— 它是上市。
# 但「iPhone 18 Pro 将带来三项相机升级」含"将"，是传闻，不能算。
_PROMO = re.compile(r"降价|降至|促销|优惠|折扣|特价|现售|立减|补贴|"
                    r"\bdeal\b|\boferta\b|\bdescuento\b|off\b", re.I)
_RUMOR = re.compile(r"传闻|爆料|曝光|或将|或为|可能|据悉|有望|预计将|"
                    r"疑似|渲染图|\brumor|\bleak|reportedly|allegedly", re.I)
_REVIEW = re.compile(r"评测|测评|体验|上手|对比评|\breview\b|hands.?on", re.I)
# ★ 软件/固件的"发布"不是产品上市。实测假阳性：
#   「AirPods Pro 3 等设备发布新公测固件」含"发布"被判成上市 ——
#   而它是固件更新，塞进上市表会让「距全球首发多少天」算错。
_SOFTWARE = re.compile(r"固件|系统更新|公测|内测|beta|测试版|补丁|驱动|"
                       r"\bfirmware\b|\bupdate\b|\bios\s*\d|watchos|ipados|harmonyos|"
                       r"one\s?ui|emui|\bapp\b|应用|软件版本", re.I)
_LAUNCH = re.compile(r"发布|上市|开卖|开售|正式发售|首销|亮相|推出|"
                     r"\blaunch|\blanzamiento|\bdebut|now available|sale starts", re.I)


def _event_kind(text: str) -> str:
    """把一条情报判成 launch / promo / rumor / review / other。

    ★ 只有 launch 才进上市表。其余留在情报流里 ——
      上市看板混进降价新闻，会让"距全球首发多少天"这类推算全部失真。
    """
    t = text or ""
    if _PROMO.search(t):
        return "promo"
    if _RUMOR.search(t):
        return "rumor"
    if _REVIEW.search(t):
        return "review"
    if _SOFTWARE.search(t):
        return "software"
    if _LAUNCH.search(t):
        return "launch"
    return "other"


def _covered_countries() -> set[str]:
    """我们真正覆盖的国家码。外键就是按这张表卡的。"""
    try:
        return {r["code"] for r in db.q("SELECT code FROM country")}
    except Exception:  # noqa: BLE001
        return set()


# ---------------------------------------------------------------- 工具

# ★ 系统/服务不是竞品硬件，不能建成 rival_product。
#   实测情报 Agent 建出了 iOS 27 / iPadOS 27 / macOS 27 / watchOS 27 / iCloud+
#   这些"产品"，还全被猜成 phone 品类。它们会：
#     · 污染友商产品表与"覆盖机型数"
#     · 混进上市看板，把"iOS 27 发布"当成一次硬件首发
#     · 被竞品匹配拿去和Acme手机比规格
#   苹果发布会新闻里系统和硬件永远一起出现，所以这个过滤是必需的，不是可选的。
_SOFTWARE_PAT = re.compile(
    r"^(?:ios|ipados|macos|watchos|tvos|visionos|audioos|harmonyos|hyperos|"
    r"android|wear\s*os|one\s*ui|emui|magicos|coloros|funtouch|realme\s*ui|"
    r"windows|chromeos)\b"
    r"|^(?:icloud|apple\s*(?:music|tv|arcade|pay|care|intelligence|one)|"
    r"galaxy\s*ai|gemini|chatgpt|copilot|siri|bixby|alexa)\b",
    re.I)


def _is_software(name: str) -> bool:
    return bool(_SOFTWARE_PAT.search((name or "").strip()))


_LAUNCH_WORDS = re.compile(
    r"\b(launch|unveil|announce|debut|introduc|lanza|lanzamiento|presenta|"
    r"anuncia|estreia|lança|apresenta|发布|上市)\b", re.I)


# ★ 喂给模型的正文长度。原来是 [:120] —— 2026-09-04 用户截图
#   「小米在 IFA 2026 发布 Galaxy Z Fold 8 竞品」：RSS 正文第 128 字才写
#   「The Xiaomi 18 Fold was showcased…」，前 120 字只说到「小米发布了一款
#   对标 Fold 8 的折叠屏」，模型根本看不到机型名，只能照搬三星站的标题框架。
#   RSS 摘录一般 300~500 字，600 足够覆盖整段；批 15 条也就 9k 字。
_BODY_CHARS = 600


def _line_key(x: dict) -> str:
    """每条新闻的核对码（url_hash 前 5 位）。模型必须原样抄回，抄错 = 序号错位。"""
    return batch_key(x.get("url") or x.get("title", ""))


def _prompt_line(j: int, x: dict, key: str = "") -> str:
    """一条新闻在抽取 prompt 里的样子：序号 (k=核对码) [来源] 标题 — 正文摘录。"""
    body = (x.get("summary") or "").replace("\n", " ").strip()
    return (f"{j}." + (f" (k={key})" if key else "")
            + f" [{x.get('source')}] {x.get('title')}"
            + (f" — {body[:_BODY_CHARS]}" if body else ""))


def _verified_brand(claimed, text: str, alias_map: dict[int, list[str]],
                    name2id: dict[str, int]) -> int | None:
    """模型报的品牌 → brand_id，**且必须在原文里核对得到**，否则 None。

    与 brandintel._accept 同一条纪律：模型说的品牌原文里没提，就不采信 ——
    品牌对、摘要错比直接丢掉危险得多，人眼根本看不出来。
    """
    from .brandintel import _hit, resolve_brand   # 延迟导入：brandintel 已 import 本模块
    bid = resolve_brand(claimed, name2id, alias_map)   # 英文名/别名/中文名 → id
    if bid is None:
        return None
    return bid if _hit(text, alias_map.get(bid) or []) else None


def _looks_like_launch(title: str) -> bool:
    return bool(_LAUNCH_WORDS.search(title or ""))


_TAG_MAP = {"new_product": "新品", "launch": "上市", "price": "价格",
            "promo": "促销", "ad": "广告", "other": "其他"}


def _tag_zh(event: str) -> str:
    return _TAG_MAP.get(event, "其他")


_CAT_HINTS = [
    ("wearable", r"\b(watch|band|smartwatch|reloj|pulsera|relógio)\b"),
    ("audio", r"\b(buds|airpods|headphone|earbud|audifon|fone|speaker|soundbar)\b"),
    ("tablet", r"\b(tab|ipad|pad)\b"),
    ("pc", r"\b(laptop|notebook|macbook|thinkpad|ideapad|vivobook|zenbook|inspiron)\b"),
]


def _guess_category(model: str, hint: str | None) -> str:
    low = (model or "").lower()
    for cat, pat in _CAT_HINTS:
        if re.search(pat, low):
            return cat
    return hint or "phone"


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# ---------------------------------------------------------------- 文本推断

# 产业词（西/葡/英）。只在**明确**命中时才给品类 —— 推不出宁可留空，
# 猜错的品类会让"墨西哥手机动态"里混进冰箱新闻，比没有品类更糟。
_CAT_WORDS = {
    "phone": (r"celular|smartphone|tel[ée]fono|m[óo]vil|iphone|galaxy s|galaxy a|"
              r"galaxy z|redmi|poco |moto g|moto e|moto edge|reno\d|nimbus \d|"
              r"magic\d|pixel \d|one plus|oneplus"),
    "tablet": r"tablet|ipad|galaxy tab|slate|redmi pad|tableta",
    "wearable": (r"smartwatch|reloj intelig|rel[óo]gio intelig|apple watch|"
                 r"galaxy watch|watch gt|pulsera|smartband|mi band|amazfit|garmin|fitbit"),
    "audio": (r"aud[íi]fon|auricular|fone de ouvido|headphone|earbuds|airpods|"
              r"galaxy buds|sonicbuds|parlante|caixa de som|soundbar|barra de sonido"),
    "pc": (r"notebook|laptop|port[áa]til|macbook|ultrabook|chromebook|"
           r"computador port|pc gamer"),
}
_CAT_RE = {k: __import__("re").compile(v, __import__("re").I)
           for k, v in _CAT_WORDS.items()}

# 国名/国族词 → 国家码。用于给**区域媒体**的文章找归属。
_COUNTRY_WORDS = {
    "MX": r"m[ée]xico|mexicano|azteca|cdmx",
    "BR": r"brasil|brasileir|s[ãa]o paulo",
    "CO": r"colombia|colombian|bogot[áa]",
    "CL": r"chile|chileno|santiago de chile",
    "PE": r"per[úu]\b|peruano|lima\b",
    "AR": r"argentina|argentino|buenos aires",
}
_COUNTRY_RE = {k: __import__("re").compile(v, __import__("re").I)
               for k, v in _COUNTRY_WORDS.items()}


def _guess_category_from_text(text: str) -> str | None:
    """从标题/摘要推产业。推不出返回 None —— 不猜。"""
    t = (text or "").strip()
    if not t:
        return None
    hits = [c for c, pat in _CAT_RE.items() if pat.search(t)]
    # 命中多个说明这条同时谈了几个品类（如"发布会推出手机和手表"）—— 不强行归一个
    return hits[0] if len(hits) == 1 else None


def _country_in_text(text: str) -> str | None:
    """区域媒体的文章：正文点名了某国才归它，否则留空。"""
    t = (text or "").strip()
    if not t:
        return None
    hits = [c for c, pat in _COUNTRY_RE.items() if pat.search(t)]
    return hits[0] if len(hits) == 1 else None
