# -*- coding: utf-8 -*-
"""品牌动态 Agent —— 追踪友商在拉美的【经营动作】。

用户原话："一个是分析竞品的品牌信息，比如手机厂商 XX 在墨西哥开了一家体验店"。

★ 它和情报 Agent(intel) 的分工，别搞混：
    intel  盯的是【产品】—— 出了什么新机、几号在哪个国家开卖；
    这个   盯的是【公司】—— 开店、发布会、请代言人、跟运营商签约、
                           退出某国、产能与涨价传闻、拿了什么奖。
  同一条新闻两边都可能收到，但抽出来的东西完全不同：
  intel 抽型号进 rival_product，这里抽动作进 strategy_signal。

★ 为什么经营动作值得单独一个 Agent：
  产品参数与价格是「结果」，经营动作是「前因」。友商在墨西哥连开三家
  体验店、又签了 Telcel 的合约机，这两件事发生在降价之前 ——
  等我们从 price_obs 里看到降价，人家的渠道仗已经打完一半了。

★ 反编造纪律（这一条是硬要求，代码里有三道闸）：
  1. 模型只看真实抓回来的条目，用 idx 映射回原文；idx 对不上直接丢。
  2. 模型说的品牌必须在标题/摘要原文里能匹配到别名，否则不认。
     这道闸是被真实噪声逼出来的：西语新闻里
       "Ferran Torres hace el lanzamiento de honor"（棒球开球仪式）
       "Honor of Kings 联动"（游戏）
     都含 "honor" 但跟荣耀手机毫无关系，光靠关键词必然误判。
  3. 摘要里出现的国家名必须在原文里找得到。第一版实测被抓到一条：
     ArchDaily 的《苹果三里屯店建筑图集》（北京）因为是从墨西哥的新闻流
     里取回来的，模型直接写成「苹果在墨西哥开设旗舰店」—— 地点是它自己
     补的。这种错误最危险，因为它读起来完全像一条正经情报。
  4. 某个品牌本周确实没有动态，就在返回值里如实列进 silent_brands，
     绝不让模型"补"一条像模像样的动态出来。

★ 币种纪律：这个 Agent 不做价格比较。给模型的价格上下文只喂
  price_move.change_pct（百分数）与方向，绝不出现绝对金额 ——
  MXN / BRL / CLP 的数字放在一起比大小是没有意义的。
"""
from __future__ import annotations

import json
import logging
import re as _re
import re
from datetime import date, timedelta

from .. import db
from ..scraping import news
from .base import BaseAgent
# RSS 的日期格式五花八门，情报 Agent 里那套已经踩过坑了，不再写第二套
from .intel import _parse_date
from .llm import as_dict, as_dicts

log = logging.getLogger("brandintel")

# 八类经营动作。key 直接进 strategy_signal.signal_type；
# 值 = (dynamics.tag 用的中文标签, 喂给模型的判定要点)
ACTION_TYPES: dict[str, tuple[str, str]] = {
    # 括号里的「不算」都是第一版实测被误判出来的，写进提示词当反例
    "store_open":   ("开店",   "品牌自己新开体验店/旗舰店/专柜/直营店，或首次进入某个销售渠道。"
                               "不算：某款商品在电商或门店上架开卖"),
    "launch_event": ("发布会", "举办发布会、官宣新品、公布上市日期"),
    "campaign":     ("营销",   "品牌官方发起的营销战役、签代言人、赞助赛事/球队/节目/艺人。"
                               "不算：零售商自己的降价、优惠券、导购推荐"),
    "partnership":  ("合作",   "与运营商、连锁零售商、电商平台达成合作或分销协议"),
    "expansion":    ("扩张",   "进入一个新国家，或把产品线扩到新品类"),
    "exit":         ("退出",   "退出某国市场、关闭门店、砍掉某条产品线"),
    "supply":       ("供应链", "产能、建厂、零部件、关税、涨价传闻"),
    "award":        ("获奖",   "拿到评测奖项或进入权威榜单"),
}

# 判定「这条新闻到底说的是哪个国家」用的词根。
# ★ 只收无歧义的词：首都名基本不敢用（lima 在西语里是「锉刀/青柠」，
#   santiago 满世界都是），宁可判成「未点名」也不要判错国家。
COUNTRY_WORDS: dict[str, tuple[str, ...]] = {
    "MX": ("méxico", "mexico", "mexicano", "mexicana", "mexicanos", "cdmx"),
    "BR": ("brasil", "brazil", "brasileiro", "brasileira", "brasileiros"),
    "CO": ("colombia", "colombiano", "colombiana", "bogotá", "bogota"),
    "CL": ("chile", "chileno", "chilena", "chilenos"),
    "PE": ("perú", "peru", "peruano", "peruana", "peruanos"),
    "AR": ("argentina", "argentino", "argentina", "argentinos", "buenos aires"),
}
COUNTRY_ZH = {"MX": "墨西哥", "BR": "巴西", "CO": "哥伦比亚",
              "CL": "智利", "PE": "秘鲁", "AR": "阿根廷"}

# 域外地名：Google News 的「墨西哥版」会大量回灌西班牙本土科技媒体
# （MuyComputer / Compradicción / cincodias.elpais.com / Fuenlabrada Noticias…）。
# 实测被抓到：Cheil **Spain** 给 Samsung 拍的代言片，被写成「三星在哥伦比亚启用代言人」。
# 原文点了西班牙、又一个拉美国家都没点 → 判为域外，不许再退回新闻源国家。
NON_LATAM_WORDS: tuple[str, ...] = (
    "españa", "espanha", "spain", "madrid", "barcelona",
)

# 检索词按「动作」而不是按「产品」组织 —— 用产品词只会捞回一堆降价促销，
# 那是 price_obs 的活。分两组是为了让单条 query 不至于长到被 Google 截断。
ACTION_QUERIES: dict[str, list[str]] = {
    "es": [
        '(tienda OR "experience store" OR inaugura OR apertura OR sucursal OR distribuidor)',
        '(lanzamiento OR evento OR campaña OR patrocinio OR embajador OR alianza OR acuerdo)',
    ],
    "pt": [
        '(loja OR inaugura OR abertura OR quiosque OR revendedor OR parceria)',
        '(lançamento OR evento OR campanha OR patrocínio OR embaixador OR acordo OR fábrica)',
    ],
}

# ══════════════ 确定性预打标（市场动态模块的地基）══════════════
#
# ★ 为什么需要：ACTION_QUERIES 早就在搜开店/发布会（tienda/inaugura/evento…），
#   **采集侧没缺**；缺在打标 —— LLM 逐条打、每轮限量 max_items，
#   实测 4373 条动态里 3774 条（86%）从未被打过标，
#   于是「市场动态」模块无米下锅。这里用确定性规则先把两类最硬的打上：
#   代码打标零成本、全量跑得完；LLM 后续照样可以覆盖改判（_backfill 无条件 UPDATE）。
#
# ★ 口径从实测反例收紧（都真实出现过）：
#   「Google abre las puertas de la Play Store」→ 应用商店政策，不是开店；
#   「inaugura loja Apple Champs-Élysées」→ 巴黎旗舰店，不在拉美 —— 但这条**保留**：
#   品牌动态本来就含全球事件，国家归属由 country_code 管，不在这里判。
_PRETAG_STORE = _re.compile(
    r"(?:inaugur\w+|apertura\s+de|abre\s+(?:su\s+)?(?:nueva\s+|primera\s+)?)"
    r"[^.]{0,50}?(?:tienda|local|flagship|experience\s+store|loja|quiosco|sucursal)"
    r"|(?:tienda|loja)\s+(?:insignia|oficial|f[íi]sica)[^.]{0,40}"
    r"(?:inaugur|abre|apertura|llega)"
    r"|新开.{0,8}(?:门店|旗舰店|体验店)|旗舰店开业|体验店开业", _re.I)
_PRETAG_EVENT = _re.compile(
    r"(?:evento\s+de\s+lanzamiento|galaxy\s+unpacked|keynote|"
    r"present[óo]\s+oficialmente|lanza(?:n)?\s+oficialmente|"
    r"anuncia\s+oficialmente|conferencia\s+de\s+prensa|"
    r"evento\s+global|发布会|官宣发布)", _re.I)
# 假朋友：这些出现时 store 命中无效（应用商店/软件商店的新闻）
_PRETAG_NOT_STORE = _re.compile(r"play\s*store|app\s*store|tienda\s+de\s+aplicaciones",
                                _re.I)


def geotag_dynamics(limit: int | None = None) -> dict:
    """确定性国家点名扫描：原文点名了哪个覆盖国，就把归属改成那个国。

    ★ 为什么需要：country_code 在抓取时写的是**新闻源所在国**（BR 源的全球新闻
      被记成"巴西动态"），按国家看板全是假分类。COUNTRY_WORDS 只收无歧义词根
      （lima/santiago 这类多义词不收），宁可"未点名"也不错判。
    ★ 只在**恰好点名一个**覆盖国时改写并标 geo_named=1；
      点名多个（对比文/汇总文）或零个都不动 country_code，只标 geo_named=0。
      幂等：geo_named 已有值的行跳过。
    """
    rows = db.q("""SELECT id, title, summary_zh, raw_text, country_code
                   FROM dynamics WHERE geo_named IS NULL""")
    n_named = n_rebound = 0
    with db.tx() as conn:
        for r in rows[: limit or len(rows)]:
            t = " ".join(filter(None, [r["title"], r["summary_zh"],
                                       (r["raw_text"] or "")[:500]])).lower()
            hits = {cc for cc, words in COUNTRY_WORDS.items()
                    if any(w in t for w in words)}
            if len(hits) == 1:
                cc = next(iter(hits))
                conn.execute("UPDATE dynamics SET geo_named=1, country_code=? WHERE id=?",
                             (cc, r["id"]))
                n_named += 1
                if cc != r["country_code"]:
                    n_rebound += 1
            else:
                conn.execute("UPDATE dynamics SET geo_named=0 WHERE id=?", (r["id"],))
    return {"scanned": len(rows), "named": n_named, "rebound": n_rebound}


def pretag_dynamics(limit: int | None = None) -> dict:
    """给未打标的 dynamics 行做确定性预打标。幂等、只写空 tag 的行。"""
    rows = db.q("""SELECT id, title, summary_zh, raw_text FROM dynamics
                   WHERE tag IS NULL OR tag=''""")
    n_store = n_event = 0
    with db.tx() as conn:
        for r in rows[: limit or len(rows)]:
            t = " ".join(filter(None, [r["title"], r["summary_zh"],
                                       (r["raw_text"] or "")[:400]]))
            if _PRETAG_EVENT.search(t):
                tag = "发布会"; n_event += 1
            elif _PRETAG_STORE.search(t) and not _PRETAG_NOT_STORE.search(t):
                tag = "开店"; n_store += 1
            else:
                continue
            conn.execute("UPDATE dynamics SET tag=? WHERE id=? AND (tag IS NULL OR tag='')",
                         (tag, r["id"]))
    return {"scanned": len(rows), "store": n_store, "event": n_event}


MIN_POOL = 30            # 窗口内存量少于这个数才去打扰新闻源
REFETCH_AFTER_H = 20     # 本 Agent 自己上次抓取超过这么久，就算存量够也要重抓
MAX_BRANDS = 8           # 每国检索的品牌数上限（6国×8品牌×2组 ≈ 96 次请求）
MAX_ITEMS = 300          # 单次送进模型分类的条目上限
BATCH = 12               # 每批喂给模型的条数
IMPORTANCE_FLOOR = 2     # 低于这个重要度不生成策略信号（1 分基本是水文）
MAX_SIGNAL_GROUPS = 40   # 二阶段解读的分组上限


class BrandIntelAgent(BaseAgent):
    name = "brand_intel"
    role = "品牌动态"
    description = "追踪友商在拉美的经营动作：开店、发布会、代言赞助、渠道合作、进退市场"

    # 放成类属性而不是 run() 参数：run() 的签名是对外契约，
    # 但排障时想缩小范围跑一轮，改这几个数比改签名干净。
    max_brands = MAX_BRANDS
    max_items = MAX_ITEMS
    max_signal_groups = MAX_SIGNAL_GROUPS
    _geo_scrubbed = 0

    # ------------------------------------------------------------ 主流程

    def run(self, days: int = 7, fetch_news: bool = True) -> dict:
        since = (date.today() - timedelta(days=days)).isoformat()
        self._geo_scrubbed = 0        # 二阶段被地理闸抹过国名的信号数
        self.start(f"品牌经营动作扫描 近{days}天 fetch_news={fetch_news}")

        # 确定性预处理（零成本、幂等）：LLM 只做规则打不动的那部分
        pre = pretag_dynamics()
        geo = geotag_dynamics()
        if pre["store"] or pre["event"] or geo["named"]:
            self.log_step("确定性预打标", parsed={**pre, **geo}, decision="ok",
                          reason=f"规则打标 开店{pre['store']}/发布会{pre['event']}，"
                                 f"点名国家 {geo['named']}（改判 {geo['rebound']}）")

        brands = self._tracked_brands()
        countries = db.q("SELECT * FROM country WHERE enabled=1 ORDER BY sort_order")
        if not brands or not countries:
            self.finish("degraded", "维表为空（brand/country），先跑 init", 0, 0)
            return {"error": "brand 或 country 表为空，请先 python main.py init"}

        # ① 决定要不要抓。
        # ★ 不能只看 dynamics 存量：dynamics 是 intel Agent 也在写的公共表，
        #   它抓的是【产品】新闻。只要 intel 跑过，存量就永远 > MIN_POOL，
        #   于是本 Agent 的动作检索一次之后再也不会触发 —— fetch_news=True
        #   变成永久空转，而 run() 依然返回 status=ok。实测就是这样：
        #   806 条全部来自唯一一次抓取，之后三轮全是 skip_fetch。
        #   所以真正的判据是「我自己上次抓是多久以前」。
        pool = self._pool_size(since)
        age_h = self._last_fetch_age_h()
        fetched = stored = 0
        need = pool < MIN_POOL or age_h is None or age_h >= REFETCH_AFTER_H
        if fetch_news and need:
            fetched, stored = self._fetch(brands, countries, days)
            pool = self._pool_size(since)
        else:
            self.log_step("盘点存量",
                          parsed={"窗口内条数": pool, "起始日": since,
                                  "本Agent上次抓取距今小时": round(age_h, 1) if age_h is not None else None},
                          decision="skip_fetch",
                          reason=(f"本 Agent {round(age_h or 0, 1)} 小时前刚抓过"
                                  f"（阈值 {REFETCH_AFTER_H}h），窗口内存量 {pool} 条，不重复抓取"
                                  if fetch_news else "调用方指定 fetch_news=False，只分析存量"))

        rows = self._window_items(since)
        if not rows:
            warn = ("窗口内没有任何新闻条目。若 fetch_news=True 仍为空，"
                    "多半是网络需要代理（设置页填 proxy），而不是友商真的没动作")
            self.log_step("无可分析条目", decision="empty", status="degraded", reason=warn)
            self.finish("degraded", warn, 0, 0)
            return {"window_days": days, "fetched": fetched, "stored": stored,
                    "analyzed": 0, "actions": 0, "signals": 0,
                    "silent_brands": [b["name"] for b in brands], "warning": warn}

        # ② 逐条判定动作类型（一阶段：贴着原文，只做归类）
        actions = self._classify(rows, brands)

        # ③ 按「品牌 × 国家 × 动作类型」聚合后解读（二阶段：对Acme意味着什么）
        signals = self._interpret(actions, brands, days)

        by_type: dict[str, int] = {}
        for a in actions:
            by_type[a["action"]] = by_type.get(a["action"], 0) + 1

        # ④ 如实交代哪些品牌本周真的没有动静
        active = {a["brand_id"] for a in actions}
        silent = [b["name"] for b in brands if b["id"] not in active]
        self.log_step("无动态品牌",
                      parsed={"跟踪品牌无动态": silent,
                              "出现动作的品牌数（含跟踪清单外的）": len(active)},
                      decision="ok",
                      reason="这些品牌在本窗口的真实新闻里没有可归类的经营动作 —— "
                             "如实标记为「无」，不要求模型补内容")

        geo_ok = sum(1 for s in signals if s.get("geo_confirmed"))
        summary = (f"分析 {len(rows)} 条新闻，识别经营动作 {len(actions)} 条，"
                   f"生成策略信号 {len(signals)} 条（其中 {geo_ok} 条国家有原文依据）；"
                   f"{len(silent)} 个品牌本周无动态")
        self.finish("ok", summary, len(rows), len(signals))
        return {
            "window_days": days, "since": since,
            "fetched": fetched, "stored": stored,
            "analyzed": len(rows), "actions": len(actions),
            "signals": len(signals), "by_type": by_type,
            # 地域可信度必须跟着结果一起返回，否则调用方无从判断这批信号能不能按国家用
            "signals_geo_confirmed": geo_ok,
            "signals_geo_inferred": len(signals) - geo_ok,
            "geo_scrubbed": self._geo_scrubbed,
            "silent_brands": silent,
            "top": [{"brand": s["brand"], "country": s["country"],
                     "type": s["signal_type"], "summary": s["summary_zh"],
                     "impact": s["impact_zh"]} for s in signals[:8]],
        }

    # ------------------------------------------------------------ 品牌与存量

    def _tracked_brands(self) -> list[dict]:
        """按「货架存在感」排序取前 N 个友商品牌。

        为什么用 price_obs 的条数排而不是按 id：只有真在拉美货架上铺开的品牌，
        它开不开店、签不签运营商才对我们有意义。给一个在本地几乎没有出货的
        品牌发 96 次检索请求，是拿配额换噪声。
        """
        rows = db.q("""
            SELECT b.id, b.name, b.aliases,
                   (SELECT COUNT(*) FROM price_obs po
                     WHERE po.brand_id = b.id AND po.audit_status <> 'rejected') AS obs_n
            FROM brand b
            WHERE b.enabled = 1 AND b.is_ours = 0
            ORDER BY obs_n DESC, b.id
        """)
        return rows[: self.max_brands]

    def _last_fetch_age_h(self) -> float | None:
        """本 Agent 上一次【真正抓取】距今多少小时。None = 从来没抓过。

        判据落在自己的 agent_step 上，而不是 dynamics 存量 ——
        dynamics 是几个 Agent 共用的表，用它判断等于把别人的产出当自己的。
        """
        r = db.q1("""
            SELECT (julianday('now') - julianday(MAX(r.started_at))) * 24.0 AS h
              FROM agent_run r JOIN agent_step s ON s.run_id = r.id
             WHERE r.agent_name = ? AND s.step_name = '检索品牌经营动作'
        """, (self.name,))
        h = (r or {}).get("h")
        return float(h) if h is not None else None

    def _pool_size(self, since: str) -> int:
        r = db.q1("""SELECT COUNT(*) c FROM dynamics
                     WHERE IFNULL(published_at, date(created_at)) >= ?""", (since,))
        return (r or {}).get("c") or 0

    def _window_items(self, since: str) -> list[dict]:
        return db.q("""
            SELECT d.id, d.title, d.raw_text, d.summary_zh, d.url, d.source_name,
                   d.published_at, d.brand_id, d.country_code, d.tag, d.importance
            FROM dynamics d
            WHERE IFNULL(d.published_at, date(d.created_at)) >= ?
              AND d.title IS NOT NULL AND d.title <> ''
            ORDER BY IFNULL(d.published_at, date(d.created_at)) DESC
            LIMIT ?
        """, (since, self.max_items))

    # ------------------------------------------------------------ ① 抓取

    def _fetch(self, brands: list[dict], countries: list[dict], days: int) -> tuple[int, int]:
        proxy = db.get_setting("proxy", "") or ""
        got: list[dict] = []
        empty_queries = 0
        total_queries = 0

        for co in countries:
            terms = ACTION_QUERIES.get(co.get("lang") or "es", ACTION_QUERIES["es"])
            for b in brands:
                aliases = _search_aliases(b)
                for term in terms:
                    total_queries += 1
                    try:
                        items = news.search_brand_news(
                            aliases, term, co, proxy, days=days, limit=25)
                    except Exception as e:  # noqa: BLE001
                        # 单个国家/品牌的检索失败不能拖垮整轮
                        log.warning("检索失败 %s/%s: %s", co["code"], b["name"], str(e)[:120])
                        items = []
                    if not items:
                        empty_queries += 1
                    for it in items:
                        it["country_code"] = co["code"]
                        it["brand_id"] = b["id"]
                    got.extend(items)

        stored = self._store(got)
        # 全空要显式报警：否则"代理挂了"会被读成"友商这周很安静"，
        # 这是最危险的一种误报。
        all_empty = empty_queries == total_queries and total_queries > 0
        self.log_step("检索品牌经营动作", parsed={
            "查询数": total_queries, "空返回": empty_queries,
            "抓回": len(got), "新入库": stored,
            "品牌": [b["name"] for b in brands],
            "国家": [c["code"] for c in countries]},
            decision="ok" if not all_empty else "empty",
            status="ok" if not all_empty else "degraded",
            reason=("按动作关键词（开店/发布会/代言/合作…）而非产品词检索，"
                    f"抓回 {len(got)} 条、去重后新入库 {stored} 条"
                    if not all_empty else
                    "所有查询均空返回 —— 极可能是网络需要代理，不是友商没动作"))
        return len(got), stored

    def _store(self, items: list[dict]) -> int:
        n = 0
        with db.tx() as conn:
            for it in items:
                url = it.get("url") or ""
                title = (it.get("title") or "").strip()
                if not title:
                    continue
                try:
                    cur = conn.execute("""
                        INSERT OR IGNORE INTO dynamics(brand_id,country_code,source_type,
                            source_name,title,url,published_at,raw_text,url_hash)
                        VALUES(?,?,'news',?,?,?,?,?,?)
                    """, (it.get("brand_id"), it.get("country_code"), it.get("source"),
                          title[:300], url, _parse_date(it.get("published_raw")),
                          (it.get("summary") or "")[:2000],
                          db.row_hash(url or title)))
                    n += cur.rowcount
                except Exception as e:  # noqa: BLE001
                    log.debug("入库失败: %s", str(e)[:100])
        return n

    # ------------------------------------------------------------ ② 归类

    def _classify(self, rows: list[dict], brands: list[dict]) -> list[dict]:
        """一阶段：逐条判定属于哪类经营动作、主角是谁、在哪个国家、多重要。

        这一步刻意只做「贴着原文的归类」，不让模型发挥 —— 发挥留给二阶段。
        分开的好处是：归类错了能一眼看出是哪条新闻错了（agent_step 里有原文）。
        """
        alias_map = _alias_map()                      # brand_id -> [用于原文核对的别名]
        name2id = {b["name"].lower(): b["id"] for b in db.q("SELECT id,name FROM brand")}
        valid_cc = {c["code"] for c in db.q("SELECT code FROM country")}
        cat_desc = "\n".join(f"  - {k}：{v[1]}" for k, v in ACTION_TYPES.items())

        out: list[dict] = []
        dropped: dict[str, int] = {}          # 反编造闸拦下的条数，按原因分
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            lines = []
            for j, r in enumerate(chunk):
                extra = (r.get("raw_text") or "")[:200].replace("\n", " ")
                lines.append(f"{j}. [{r.get('source_name') or '?'}|"
                             f"{r.get('country_code') or '?'}] {r['title']}"
                             + (f" — {extra}" if extra else ""))

            prompt = (
                "你是消费电子行业的竞品情报分析师，服务对象是Acme在拉美六国"
                "（墨西哥MX/巴西BR/哥伦比亚CO/智利CL/秘鲁PE/阿根廷AR）的 销售团队。\n\n"
                "下面是真实抓取到的西语/葡语新闻标题（含少量摘要）。请逐条判断："
                "它是不是某个品牌的【经营动作】。经营动作分八类：\n"
                + cat_desc + "\n\n"
                "★ 铁律：\n"
                "  1. 只根据下面给出的文字判断，绝不补充你记忆里的任何信息，"
                "更不许编造新闻。\n"
                "  2. 不属于上面八类的（纯降价促销、导购推荐、参数评测、"
                "谣言爆料、与消费电子无关的），action 一律填 none。\n"
                "  3. 同名词陷阱：西语 honor / apple 等词有普通词义"
                "（如 \"lanzamiento de honor\" 是棒球开球，\"Honor of Kings\" 是游戏），"
                "这类 brand 填 null、action 填 none。\n"
                "  4. brand 必须是新闻里真正出现的品牌名，不确定就填 null。\n"
                "  5. **地点不许猜**：country 只有在原文文字里点出国家时才填，"
                "原文没写就填 null。方括号里的国家码只是这条新闻从哪个国家的"
                "新闻流取回来的，不代表事情发生在那儿。\n"
                "  6. summary_zh 里不许出现原文没有的地点、数字、金额、日期。\n"
                "  7. 事情发生在拉美六国之外、且对拉美市场没有直接影响的"
                "（例如美国的合资公司、中国的门店），in_latam 填 false。\n\n"
                + "\n".join(lines) + "\n\n"
                "只返回 JSON 数组，每条：\n"
                '{"idx":序号,"action":"上面八类之一或none","brand":"品牌名或null",'
                '"country":"两位国家码或null","in_latam":true或false,'
                '"importance":1到5的整数,'
                '"summary_zh":"一句话中文说明这家公司做了什么，不超过40字，'
                '只能复述原文里有的事实"}\n'
                "importance 参考：5=改变竞争格局（进新国家/退出/大规模开店）；"
                "4=明确的渠道或营销投入；3=常规发布会或合作；2=小动作；1=边缘。")

            try:
                parsed = self.ask_json(f"动作归类 {i}~{i + len(chunk) - 1}", prompt,
                                       system="你是严谨的情报分析师，只输出 JSON，不编造事实。",
                                       input_ref=f"dynamics:{chunk[0]['id']}..{chunk[-1]['id']}",
                                       default=[])
            except Exception as e:  # noqa: BLE001
                # 单批失败跳过，继续下一批 —— 不能因为一批崩掉整个 Agent
                log.warning("归类批次 %d 失败: %s", i, str(e)[:150])
                self.log_step(f"动作归类 {i} 失败", decision="error", status="degraded",
                              reason=f"该批异常已跳过：{str(e)[:200]}")
                continue

            for item in as_dicts(parsed):
                rec = self._accept(item, chunk, alias_map, name2id, valid_cc)
                if rec is None:
                    continue
                if isinstance(rec, str):      # 被反编造闸拦下
                    dropped[rec] = dropped.get(rec, 0) + 1
                    continue
                out.append(rec)

        self._backfill(out)
        inferred = sum(1 for a in out if a["geo_inferred"])
        self.log_step("动作归类汇总", parsed={
            "送检": len(rows), "识别为经营动作": len(out),
            "被反编造闸丢弃": dropped,
            "国家按新闻源推断（原文未点名）": inferred},
            decision="ok",
            reason="三道闸：品牌必须在原文里匹配到别名、摘要里的国家名必须原文有据、"
                   "拉美域外事件剔除。对不上的整条丢弃，宁可漏报也不能把噪声写成情报")
        return out

    def _accept(self, item: dict, chunk: list[dict], alias_map: dict,
                name2id: dict, valid_cc: set):
        """把模型返回的一条校验成可用记录。任何一关不过就丢。

        返回 dict = 采纳；None = 不是经营动作；字符串 = 被反编造闸拦下（字符串是原因）。
        """
        try:
            j = int(item.get("idx"))
            src = chunk[j]
        except (TypeError, ValueError, IndexError):
            return None                       # idx 对不上原文 —— 模型自己编的行

        action = str(item.get("action") or "none").strip().lower()
        if action not in ACTION_TYPES:
            return None                       # none 或胡填的类型

        # 字段缺失时默认「在拉美」，不能因为模型漏填就把真动作全丢了
        if str(item.get("in_latam", True)).strip().lower() in ("false", "0", "no"):
            return "拉美域外事件"

        text = f"{src.get('title') or ''} {src.get('raw_text') or ''}"
        in_text = {bid for bid, al in alias_map.items() if _hit(text, al)}

        # 品牌解析优先级：模型判定（须原文可核对） > 抓取时的检索品牌（同样须可核对）
        claimed = str(item.get("brand") or "").strip().lower()
        cand = name2id.get(claimed)
        if cand is None and claimed:
            for bid, al in alias_map.items():
                if any(claimed == a.lower() for a in al):
                    cand = bid
                    break

        # ★ 判定顺序不能反过来：先看模型认没认出品牌，认出了就必须原文可核对。
        #   写反过一次（先看抓取品牌），后果是模型把「苹果发布新品」的摘要
        #   挂到了 Samsung 头上 —— 因为这条是从 Samsung 的检索里抓回来的，
        #   抓取品牌在原文里当然找得到，于是编造被"洗"成了一条看似合理的记录。
        #   这比直接丢掉危险得多：品牌对、摘要错，人眼根本看不出来。
        if cand is not None:
            if cand not in in_text:
                return "品牌原文无据"          # 模型说的品牌原文里根本没提 —— 不采信
            brand_id = cand
        elif src.get("brand_id") in in_text:
            # 模型没认出品牌（或报了个我们库里没有的公司名，如 Netlist/Adyen），
            # 退回抓取时的检索品牌 —— 但它同样必须在原文里找得到
            brand_id = src["brand_id"]
        else:
            return None

        # —— 地理归属：原文点名了才算「有据」，否则只能算「按新闻源推断」——
        named = _countries_named(text)
        # ★ 规则闸（不依赖模型的 in_latam）：原文点了西班牙、却一个拉美国家都没点
        #   —— 这是西班牙本土新闻被 Google News 拉美版回灌，不是拉美市场动作。
        if not named and _hit(text, list(NON_LATAM_WORDS)):
            return "西班牙/域外新闻"
        claimed_cc = str(item.get("country") or "").strip().upper()
        if claimed_cc in named:
            cc, geo_inferred = claimed_cc, False
        elif len(named) == 1:
            cc, geo_inferred = next(iter(named)), False
        else:
            # 原文没点名（或点了好几个）：退回抓取时的新闻源国家，但打上推断标记，
            # 二阶段解读时会告诉模型「这个国家是推断的，别当事实断言」
            cc = src.get("country_code") if src.get("country_code") in valid_cc else None
            geo_inferred = True

        summary = str(item.get("summary_zh") or "")[:300]
        # ★ 最后一道闸：摘要里写了某个国家，原文却没提过 → 整条丢弃。
        #   地理被编造说明这条的判断整体不可信，不是只把国家抹掉就完事。
        for code, zh in COUNTRY_ZH.items():
            if zh in summary and code not in named:
                return "摘要地点无据"

        try:
            imp = max(1, min(5, int(item.get("importance") or 3)))
        except (TypeError, ValueError):
            imp = 3

        return {
            "dyn_id": src["id"], "action": action, "brand_id": brand_id,
            "country_code": cc, "geo_inferred": geo_inferred, "importance": imp,
            # 原文真正点到名的国家，要带到二阶段去当「可以写进结论的国家」白名单，
            # 否则二阶段又会凭 country_code 把推断当事实（见 _one_signal 的地理闸）
            "named_cc": sorted(named),
            "summary_zh": summary,
            "title": src.get("title") or "", "url": src.get("url") or "",
            "source": src.get("source_name") or "",
            "published_at": src.get("published_at"),
            # 原来挂的品牌在原文里对不上、而新判定的对得上时才改写 dynamics.brand_id
            "rebind": brand_id if (src.get("brand_id") not in in_text
                                   and brand_id in in_text) else None,
        }

    def _backfill(self, actions: list[dict]) -> None:
        """回填 dynamics 的 tag / importance / summary_zh。

        summary_zh 用 COALESCE 保护：情报 Agent 可能已经写过更好的摘要，
        我们只在它为空时补，不做覆盖。
        """
        with db.tx() as conn:
            for a in actions:
                conn.execute("""
                    UPDATE dynamics
                       SET tag = ?, importance = ?,
                           summary_zh = COALESCE(NULLIF(summary_zh,''), NULLIF(?,''))
                     WHERE id = ?
                """, (ACTION_TYPES[a["action"]][0], a["importance"],
                      a["summary_zh"], a["dyn_id"]))
                if a.get("rebind"):
                    conn.execute("UPDATE dynamics SET brand_id=? WHERE id=?",
                                 (a["rebind"], a["dyn_id"]))
                # 国家只在【原文点名】时才改写。抓取时写进去的是新闻源所在国，
                # 实测有《Llega a Argentina el Galaxy A27》从哥伦比亚新闻流取回，
                # 存着 CO 就把一条阿根廷上市说成了哥伦比亚。推断出来的不敢覆盖。
                if not a["geo_inferred"] and a["country_code"]:
                    conn.execute("UPDATE dynamics SET country_code=? WHERE id=?",
                                 (a["country_code"], a["dyn_id"]))

    # ------------------------------------------------------------ ③ 解读

    def _interpret(self, actions: list[dict], brands: list[dict], days: int) -> list[dict]:
        """二阶段：按「品牌 × 国家 × 动作类型」聚合后问模型「对Acme意味着什么」。

        为什么聚合而不是逐条问：一条"开了家店"没什么，同一个品牌一周在同一国
        开了三家店才是信号。逐条问既贵又看不出密度。
        """
        today = db.today()
        bname = {b["id"]: b["name"] for b in db.q("SELECT id,name FROM brand")}
        cname = {c["code"]: c["name_zh"] for c in db.q("SELECT code,name_zh FROM country")}

        groups: dict[tuple, list[dict]] = {}
        for a in actions:
            if a["importance"] < IMPORTANCE_FLOOR:
                continue
            groups.setdefault((a["brand_id"], a["country_code"], a["action"]), []).append(a)

        ranked = sorted(groups.items(),
                        key=lambda kv: (max(x["importance"] for x in kv[1]), len(kv[1])),
                        reverse=True)[: self.max_signal_groups]

        # 重跑当天必须覆盖而不是叠加：strategy_signal 没有唯一键，
        # 不删的话跑三次看板上就出现三份一模一样的信号。
        with db.tx() as conn:
            conn.execute("DELETE FROM strategy_signal WHERE agent=? AND signal_date=?",
                         (self.name, today))

        out: list[dict] = []
        for (brand_id, cc, action), items in ranked:
            try:
                sig = self._one_signal(brand_id, cc, action, items, bname, cname, today, days)
            except Exception as e:  # noqa: BLE001
                log.warning("信号生成失败 %s/%s/%s: %s", brand_id, cc, action, str(e)[:150])
                self.log_step("信号生成失败", decision="error", status="degraded",
                              input_ref=f"brand:{brand_id} {cc} {action}",
                              reason=f"该组异常已跳过，不影响其它组：{str(e)[:200]}")
                continue
            if sig:
                out.append(sig)
        return out

    def _one_signal(self, brand_id, cc, action, items, bname, cname, today, days):
        brand = bname.get(brand_id, "?")
        country = cname.get(cc, "跨国/未定")
        label, hint = ACTION_TYPES[action]
        evidence = [{"dynamics_id": x["dyn_id"], "title": x["title"][:200],
                     "url": x["url"], "source": x["source"],
                     "published_at": x["published_at"],
                     "importance": x["importance"],
                     "geo_inferred": x["geo_inferred"]} for x in items[:10]]

        # ★ 这一组里「原文真的点过名」的国家。二阶段结论里只允许出现这些国家。
        #   实测教训：一阶段有「摘要地点无据」闸，二阶段却没有，结果 31 条信号里
        #   25 条在结论/影响里写了原文根本没提的国家（Netlist 与三星的全球专利协议
        #   被写成「三星在墨西哥强化技术优势」）。二阶段才是进看板的那一份，
        #   闸开在一阶段等于没开。
        named_all: set[str] = set()
        for x in items:
            named_all |= set(x.get("named_cc") or ())
        geo_confirmed = bool(cc) and cc in named_all

        # 关键：国家没被原文证实时，压根不要把国家名写进提示词。
        # 老写法是 f"市场：{country}"，等于先把推断当事实喂给模型，
        # 再用一句软提示求它别当真 —— 模型 25/31 次都当真了。
        if geo_confirmed:
            market_line = f"市场：{country}。"
            geo_note = ""
        else:
            market_line = "市场：未确认。"
            geo_note = ("\n★ 地理警告：这批新闻的原文没有点名任何拉美国家，"
                        f"它们只是从{cc or '某国'}的新闻流里取回来的。"
                        "结论、影响、建议里【一律不许出现任何国家名】，"
                        "只能说「该市场」。写了国家名的回答会被系统丢弃。")

        prompt = (
            f"竞品：{brand}。{market_line}动作类型：{label}（{hint}）。\n"
            f"最近 {days} 天真实抓到的相关新闻（共 {len(items)} 条）：\n"
            + "\n".join(f"- {x['title']}"
                        + (f"（{x['summary_zh']}）" if x["summary_zh"] else "")
                        for x in items[:8]) + geo_note + "\n\n"
            + self._price_context(brand_id, cc, days) + "\n\n"
            "请以Acme拉美 销售团队 的视角回答：这组动作说明了什么、对Acme意味着什么、"
            "我们该做什么。\n"
            "★ 只能基于上面列出的事实推断。不许编造门店数量、销量、金额、日期；"
            "信息不足就直说「现有信息不足以判断」，并把 confidence 打低。\n\n"
            '只返回 JSON：{"summary_zh":"一句话结论，不超过50字",'
            '"impact_zh":"对Acme意味着什么，不超过80字",'
            '"suggested_action":"建议动作，不超过60字",'
            '"confidence":0到1之间的小数}')

        parsed = as_dict(self.ask_json(
            f"解读 {brand}/{cc or 'X'}/{label}", prompt,
            system="你是Acme拉美 销售团队 的竞品分析顾问，务实、不空话、不编造。",
            input_ref=f"brand:{brand_id} {cc} {action}", default={}))
        if not parsed:
            return None

        summary = str(parsed.get("summary_zh") or "").strip()[:300]
        impact = str(parsed.get("impact_zh") or "")[:500]
        action_txt = str(parsed.get("suggested_action") or "")[:300]
        if not summary:
            # 模型没给结论就用原文兜底 —— 事实是真的，只是少了解读，
            # 总好过丢掉一条真实动作。
            summary = (f"{brand} 在{country}有 {len(items)} 条{label}相关动态"
                       if geo_confirmed else
                       f"{brand} 有 {len(items)} 条{label}相关动态（地域未确认）")

        # —— 硬闸：把原文没点过名的国家从结论里抹掉。提示词是软的，这一层是硬的。——
        summary, n1 = _scrub_geo(summary, named_all)
        impact, n2 = _scrub_geo(impact, named_all)
        action_txt, n3 = _scrub_geo(action_txt, named_all)
        scrubbed = n1 + n2 + n3
        if scrubbed:
            self._geo_scrubbed += 1
            self.log_step("地理闸·抹除无据国名", decision="scrubbed", status="degraded",
                          input_ref=f"brand:{brand_id} {cc} {action}",
                          parsed={"抹除次数": scrubbed,
                                  "原文点名的国家": sorted(named_all) or "无",
                                  "抹后结论": summary[:120]},
                          reason="模型在结论里写了原文没有依据的国家名，已替换为「该市场」；"
                                 "国家推断来自新闻源，不能当事实断言")

        # 地域没被原文证实的，落库时明确标注，别让看板把「墨西哥新闻流」读成「发生在墨西哥」
        if not geo_confirmed:
            summary = f"【地域未证实·源自{cc or '?'}新闻流】{summary}"[:300]

        try:
            conf = float(parsed.get("confidence"))
            conf = max(0.0, min(1.0, conf))
        except (TypeError, ValueError):
            conf = 0.5
        if not geo_confirmed:
            # 地域是猜的，置信度不该和「原文点名」的信号平起平坐
            conf = min(conf, 0.5)

        with db.tx() as conn:
            conn.execute("""
                INSERT INTO strategy_signal(signal_date,country_code,brand_id,
                    signal_type,confidence,summary_zh,evidence,impact_zh,
                    suggested_action,agent)
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (today, cc, brand_id, action, conf, summary,
                  json.dumps(evidence, ensure_ascii=False),
                  impact, action_txt, self.name))

        return {"brand": brand, "country": country, "signal_type": action,
                "label": label, "summary_zh": summary, "impact_zh": impact,
                "geo_confirmed": geo_confirmed,
                "confidence": conf, "items": len(items)}

    def _price_context(self, brand_id: int, cc: str | None, days: int) -> str:
        """给模型补一段真实的价格上下文 —— 只给百分比，绝不给金额。

        跨国比较必须用百分比：MXN / BRL / CLP / ARS 的数字放一起比大小毫无意义，
        更别提阿根廷的通胀会让绝对值一周变个样。
        """
        if not cc:
            return "（未定位到具体国家，未附价格上下文）"
        since = (date.today() - timedelta(days=days)).isoformat()
        r = db.q1("""
            SELECT COUNT(*) n,
                   SUM(CASE WHEN direction='down' THEN 1 ELSE 0 END) down_n,
                   ROUND(AVG(CASE WHEN direction='down' THEN change_pct END),1) avg_down
            FROM price_move
            WHERE brand_id=? AND country_code=? AND move_date>=? AND is_official=1
        """, (brand_id, cc, since))
        n = (r or {}).get("n") or 0
        if not n:
            return "同期该品牌在该国官方渠道无价格变动记录（数据库确实没有，不是没查）。"
        return (f"同期该品牌在该国官方渠道有 {n} 个 SKU 价格变动，其中降价 "
                f"{r.get('down_n') or 0} 个，平均降幅 {r.get('avg_down')}%（百分比，不含金额）。")


# ---------------------------------------------------------------- 工具

def _search_aliases(brand: dict) -> list[str]:
    """给 Google News 用的检索别名：去重、去掉过短的词、最多 3 个。

    ★ 必须去掉 2 字母别名：小米的别名里有 "Mi"，而 "mi" 在西语里是「我的」，
      带上它整个新闻流会被无关内容淹没。
    """
    try:
        raw = json.loads(brand.get("aliases") or "[]")
    except Exception:  # noqa: BLE001
        raw = []
    out, seen = [], set()
    for a in [brand["name"], *raw]:
        a = str(a or "").strip()
        if len(a) < 3 or a.lower() in seen:
            continue
        seen.add(a.lower())
        out.append(a)
    return out[:3] or [brand["name"]]


def _alias_map() -> dict[int, list[str]]:
    """brand_id -> 用于「原文核对」的别名表。比检索别名更全，但同样剔除短词。"""
    out: dict[int, list[str]] = {}
    for b in db.q("SELECT id,name,aliases FROM brand"):
        try:
            raw = json.loads(b["aliases"] or "[]")
        except Exception:  # noqa: BLE001
            raw = []
        al = {str(a).strip() for a in [b["name"], *raw] if len(str(a).strip()) >= 3}
        out[b["id"]] = sorted(al)
    return out


def _countries_named(text: str) -> set[str]:
    """原文里明确点到名的拉美六国。空集 = 原文根本没说是哪个国家。"""
    return {code for code, words in COUNTRY_WORDS.items() if _hit(text, list(words))}


def _scrub_geo(text: str, named: set[str]) -> tuple[str, int]:
    """把原文没点过名的国家名从模型结论里抹掉，替换成「该市场」。

    返回 (处理后文本, 抹除次数)。抹除次数 > 0 说明模型又在编地点。

    只抹「无据」的：如果原文确实点了巴西，结论里写巴西是对的，不动。
    """
    if not text:
        return text, 0
    n = 0
    for code, zh in COUNTRY_ZH.items():
        if code in named or zh not in text:
            continue
        n += text.count(zh)
        text = text.replace(zh, "该市场")
    if n:
        # "墨西哥市场" → "该市场市场"；"在墨西哥的" → "在该市场的"，前者要收拾一下
        text = re.sub(r"(该市场)(?:市场)+", r"\1", text)
        text = re.sub(r"(该市场)(?:、?该市场)+", r"\1", text)
    return text, n


def _hit(text: str, aliases: list[str]) -> bool:
    """别名是否在原文里出现（词边界匹配，避免 Sony 命中 sonyclub 这类子串）。"""
    low = (text or "").lower()
    for a in aliases:
        a = a.lower()
        if not a:
            continue
        if re.search(r"(?<![0-9a-záéíóúñãõçü])" + re.escape(a)
                     + r"(?![0-9a-záéíóúñãõçü])", low):
            return True
    return False
