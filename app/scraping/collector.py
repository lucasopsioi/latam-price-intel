# -*- coding: utf-8 -*-
"""采集主流程：搜索 → 列表 → 详情 → 入库。

流程（对应用户选定的"全部进详情页"+"品牌词与型号词都要"）：

  对每个 渠道 × 品牌：
      1. 品牌词搜索（"celular Samsung"）—— 负责发现新品
      2. 型号词搜索（"Galaxy S25 Ultra"）—— 负责盯死重点竞品价格
      3. 列表结果去重合并
      4. 逐个进详情页拿卖家/库存/规格/分期
         ★ 详情页缓存：规格抓过就不再抓（规格不随时间变），只刷价格
      5. 归一化 → 写 price_obs（row_hash 幂等，重跑不产生重复行）

留痕：每个"渠道×品牌"是一个 scrape_unit，状态 ok/empty/blocked/login_wall/failed
      全部落库，主 Agent 下一轮据此研判该渠道健不健康。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta

from .. import db, livelog, skumap
from ..config import load_brands
from .channels import build_adapter
from .channels.base import Listing
from .relevance import check_batch, check_yield

log = logging.getLogger("collector")

# 规格重抓周期：规格不随时间变，但商品会改版/改标题，30 天刷一次够了
SPEC_REFRESH_DAYS = 30

# ★ 详情页整体重抓周期。这是"全部进详情页"能不能跑得完的关键闸。
#   实测：Liverpool 抓 3 个商品（含详情页）耗时 107 秒 ——
#   按 20 商品 × 48 单元算，墨西哥一国一个产业就要 9.6 小时。
#   但详情页给的是卖家、库存、规格、分期，这些不会天天变；
#   价格列表页就有。所以详情页结果缓存 N 天，之后同一商品只刷价格，
#   请求量降一个数量级。第一轮仍然全量抓，保证数据完整。
DETAIL_REFRESH_DAYS = 7

# ★ 卖家/规格判定逻辑的版本号。**改动判定规则时必须 +1。**
#
# 详情页缓存存的是【判定结论】，而判定逻辑会演进 —— 逻辑改了旧缓存就是错的，
# 却会一直被沿用，表现为"修复没生效"，极难察觉。
# 真实事故：修好「授权经销商 ≠ 品牌官方店」后，Coppel 仍有 8 条判成
# brand_official，因为它们命中缓存、沿用的是修复之前的结论。
#
# 版本历史：
#   1  初版
#   2  卖家三分 + 「授权经销商≠品牌官方店」+ 标题级配件识别
#   3  孤立徽章不足以定性（须与品牌名紧邻），修掉 Coppel 8 条误判
SELLER_LOGIC_VERSION = 3


def _cache_logic_version(cached: dict) -> int:
    """读缓存里的逻辑版本号。旧缓存没有这个字段，当成版本 1。"""
    try:
        return int(json.loads(cached.get("specs_json") or "{}").get("_logic_version", 1))
    except Exception:  # noqa: BLE001
        return 1


class Collector:
    def __init__(self, engine, run_id: int | None, cfg: dict, meli_token: str = ""):
        # run_id 为空时自建一个临时批次：单渠道调试/体检也要留痕，
        # 否则 scrape_unit 的 NOT NULL 约束会让整次采集在入库时炸掉
        if run_id is None:
            run_id = db.start_run(mode="adhoc", scope={"note": "单渠道调试/体检"})
        self.engine = engine
        self.run_id = run_id
        self.cfg = cfg or {}
        self.meli_token = meli_token
        self.max_items = int(cfg.get("max_products_per_query", 20))
        self._brand_cfg = load_brands()
        self._brand_rows = {b["name"]: b for b in db.q("SELECT * FROM brand")}
        self._brand_alias_index = self._build_alias_index()
        self.stats = {"units": 0, "listings": 0, "details": 0,
                      "detail_cached": 0, "rows": 0}

    # ------------------------------------------------ 品牌识别

    def _build_alias_index(self) -> list[tuple[str, int, str]]:
        """(小写别名, brand_id, 品牌名)，按别名长度倒序 —— 先匹配长的，
        否则 "Galaxy Watch" 会先被 "Galaxy" 抢走判成手机品牌条目。"""
        idx = []
        for row in self._brand_rows.values():
            try:
                aliases = json.loads(row["aliases"] or "[]")
            except Exception:  # noqa: BLE001
                aliases = []
            for a in set([row["name"]] + list(aliases)):
                if a:
                    idx.append((a.lower(), row["id"], row["name"]))
        idx.sort(key=lambda x: -len(x[0]))
        return idx

    def guess_brand(self, title: str) -> tuple[int | None, str | None]:
        low = (title or "").lower()
        for alias, bid, name in self._brand_alias_index:
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", low):
                return bid, name
        return None, None

    # ------------------------------------------------ 搜索词

    def build_queries(self, channel: dict, country: dict, brand: dict,
                      category: str) -> list[tuple[str, str]]:
        """返回 [(搜索词, 用途)]。用途: discover(发现新品) / track(盯价格)"""
        cats = self._brand_cfg.get("categories") or {}
        terms = (cats.get(category) or {}).get("search_terms") or {}
        cat_word = terms.get(country["lang"]) or terms.get("es") or ""

        queries: list[tuple[str, str]] = []
        # ① 品牌词：发现新品
        base = f"{cat_word} {brand['name']}".strip()
        queries.append((base, "discover"))

        # ② ★ 权威 SKU 表里的型号词 —— 用户要求「保证每次抓取都包含这些产品」。
        #   只靠"品类词+品牌名"搜不全：一次搜索只返回二三十条，
        #   而该品牌在该品类下可能有几十个在售型号，热销的会把长尾挤掉。
        #   拿具体型号名去搜是唯一能保证覆盖的办法。
        #   ★ 传 category：这张表只覆盖平板，手机/穿戴/音频/PC 传进来会返回空，
        #     不会白跑 8 次无关查询。
        n_sku = int(self.cfg.get("sku_queries_per_unit", 12))
        for sku in skumap.search_terms(brand["name"], category)[:n_sku]:
            queries.append((sku, "sku"))

        # ③ 型号词：盯死已入库的重点竞品（权威表没覆盖的品类靠这个）
        tracked = db.q("""
            SELECT DISTINCT rp.model_name
            FROM rival_product rp
            JOIN price_obs po ON po.rival_product_id = rp.id
            WHERE rp.brand_id = ? AND rp.category_code = ?
              AND po.country_code = ? AND po.obs_date >= date('now','-21 day')
            ORDER BY rp.updated_at DESC LIMIT 6
        """, (brand["id"], category, country["code"]))
        seen = {q for q, _ in queries}
        for t in tracked:
            if t["model_name"] not in seen:
                queries.append((t["model_name"], "track"))
        return queries

    # ------------------------------------------------ 单元采集

    def collect_unit(self, channel: dict, country: dict, brand: dict,
                     category: str) -> tuple[int, str]:
        """采集一个"渠道×品牌×产业"单元。返回 (写入行数, 状态)"""
        t0 = time.time()
        # 品牌官方商城只卖自己的货：拿 Samsung 商城去搜 Apple 是白跑一趟
        if channel["kind"] == "brand_store" and \
                brand["name"].lower() not in (channel["code"] or "").lower():
            return 0, "skipped"

        adapter = build_adapter(channel, country, meli_token=self.meli_token)

        # Shopee 这类站点直接走兜底引擎，不浪费一轮 Playwright 去撞墙
        if getattr(adapter, "force_fallback", False):
            host = (channel.get("base_url") or "").split("//")[-1].split("/")[0]
            if host:
                self.engine.forced_fallback_hosts.add(host)

        queries = self.build_queries(channel, country, brand, category)
        aliases = self._aliases_of(brand)
        livelog.emit("search", f"{country['code']} · {channel['name']} · "
                               f"{brand['name']} · {category}",
                     country=country["code"], channel=channel["name"],
                     brand=brand["name"])
        all_items: dict[str, Listing] = {}
        statuses: list[str] = []
        relevance_notes: list[str] = []

        for qi, (query, purpose) in enumerate(queries, 1):
            # ★ 每个查询都要发事件。一个单元有 1~9 个查询、每个 30~90 秒，
            #   只在单元首尾发事件的话，界面上会连续三五分钟毫无动静 ——
            #   用户据此认为程序死了（实际在正常跑）。
            livelog.emit("query",
                         f"[{qi}/{len(queries)}] {channel['name']} 搜「{query}」",
                         channel=channel["name"], query=query, purpose=purpose)
            items, status = self._search(adapter, query, category)
            livelog.emit("query_done",
                         f"[{qi}/{len(queries)}] {channel['name']}「{query}」"
                         f"→ {status} {len(items)} 条",
                         channel=channel["name"], count=len(items), status=status)

            # ★ 相关性闸：挡住"搜索没生效但看起来成功"的污染。
            #   实测 Coppel 搜「celular samsung」返回的是首页推荐位
            #   （运动鞋/床垫），报告显示 OK 11 条 —— 不校验就会进价格基线。
            #   品类页模式不做这个校验：那本来就是整页同品类商品，
            #   不是搜索结果，用搜索词去比对会误杀。
            if items and not status.endswith("category_page"):
                rel = check_batch([i.title for i in items], query, aliases,
                                  query_purpose=purpose)
                if rel["verdict"] == "search_ineffective":
                    log.warning("[%s/%s] %s", country["code"], channel["name"],
                                rel["reason"])
                    relevance_notes.append(rel["reason"][:300])
                    items, status = [], "irrelevant:filtered"
                elif rel["verdict"] == "brand_absent":
                    # ★ 该渠道不卖这个牌子 —— 是事实不是故障。
                    #   保留匹配到的（可能有零星几条），不报警、不算渠道不健康。
                    kept = set(rel["kept"])
                    items = [it for i, it in enumerate(items) if i in kept]
                    log.info("[%s/%s] %s", country["code"], channel["name"],
                             rel["reason"][:160])
                    if not items:
                        status = "empty:brand_absent"
                elif rel["verdict"] == "low_relevance":
                    kept = set(rel["kept"])
                    items = [it for i, it in enumerate(items) if i in kept]
                    relevance_notes.append(rel["reason"][:200])

            statuses.append(status.split(":")[0])
            for it in items:
                key = it.url or f"{it.title}|{it.sale_price}"
                if key not in all_items:
                    it.specs.setdefault("_query_purpose", purpose)
                    all_items[key] = it
            if status.startswith("login_wall"):
                break   # 登录墙对整站生效，别再拿其它词去撞

        listings = list(all_items.values())[: self.max_items]
        self.stats["listings"] += len(listings)

        # 详情页：用户要求全部进。
        # ★ 但必须有整体时间预算。单页有超时，"逐条进详情页"这个循环却没有闸 ——
        #   一个渠道可以吃掉半小时（20 条 × 被拦后冷却 90 秒 + 换设备重试），
        #   跑 10~20 小时无人值守时会把后面的渠道全挤掉，而且日志上看不出是它。
        #   到点就停：已抓到的详情保留，剩下的用列表页数据入库（价格照样有），
        #   并**如实记录跳过了多少**，不静默截断。
        budget = float(self.cfg.get("detail_budget_seconds", 420))
        t0 = time.monotonic()
        enriched, skipped_detail = [], 0
        if listings:
            livelog.emit("detail",
                         f"{channel['name']}：{len(listings)} 个商品，开始逐个进详情页",
                         channel=channel["name"], count=len(listings))
        for di, it in enumerate(listings, 1):
            # 详情页逐个发事件（每个 5~30 秒），否则这一段又是几分钟的黑屏
            if di == 1 or di % 3 == 0 or di == len(listings):
                livelog.emit("detail",
                             f"{channel['name']} 详情页 {di}/{len(listings)}："
                             f"{it.title[:40]}"
                             f"{f' {it.sale_price:,.0f}' if it.sale_price else ''}",
                             channel=channel["name"])
            if time.monotonic() - t0 > budget:
                skipped_detail += 1
                enriched.append(it)      # 列表页数据仍然可用，只是没有详情增强
                continue
            r = self._fetch_detail(adapter, it, channel, country)
            if r is not None:
                enriched.append(r)
        if skipped_detail:
            log.warning("[%s/%s] 详情页预算 %.0f 秒用尽，%d 条只用列表页数据入库",
                        country["code"], channel["name"], budget, skipped_detail)
            livelog.emit("warn",
                         f"{channel['name']}：详情页超时预算，{skipped_detail} 条"
                         f"仅用列表页数据（价格有，卖家/规格可能不全）",
                         channel=channel["name"])

        rows = self._persist(enriched, channel, country, brand, category)
        status = self._merge_status(statuses, len(listings))

        # 把这一单元的结果推到界面：能看到抓了什么、价格多少
        if enriched:
            sample = "；".join(f"{e.title[:34]} {e.sale_price:,.0f}"
                              for e in enriched[:3] if e.sale_price)
            livelog.emit("found",
                         f"{channel['name']}／{brand['name']}：{len(enriched)} 条 → {sample}",
                         count=len(enriched), channel=channel["name"])
        elif status in ("blocked", "login_wall", "irrelevant"):
            livelog.emit("block", f"{channel['name']}／{brand['name']}：{status}"
                                  + (f" — {relevance_notes[0][:90]}" if relevance_notes else ""),
                         channel=channel["name"], status=status)
        db.log_unit(self.run_id, channel_id=channel["id"], country=country["code"],
                    brand_id=brand["id"], category=category,
                    query=" | ".join(q for q, _ in queries)[:200],
                    status=status, engine=self.engine.last_engine,
                    items=rows, duration_ms=int((time.time() - t0) * 1000),
                    message=" ｜ ".join(relevance_notes)[:500] or None)
        self.stats["units"] += 1
        self.stats["rows"] += rows
        return rows, status

    def _search(self, adapter, query: str,
                category: str | None = None) -> tuple[list[Listing], str]:
        # MeliAdapter 自带三通道逻辑，走它自己的入口
        if hasattr(adapter, "collect"):
            # category 只传给声明支持的适配器：Alkosto 靠它把品类码当 Algolia facet 用，
            # 而 Meli/Claro 的 collect() 没有这个参数，无脑传会 TypeError ——
            # 而 TypeError 会被外层宽泛的 except 吃掉，表现成"这个渠道抓不到东西"。
            if getattr(adapter, "wants_category", False):
                return adapter.collect(self.engine, query, self.max_items,
                                       category=category)
            return adapter.collect(self.engine, query, self.max_items)

        url, url_mode = adapter.list_url(query, category)
        if not url:
            return [], "failed:no_url"
        adapter_base = adapter.channel.get("base_url")
        if adapter_base:
            self.engine.warm_up(adapter_base, adapter.cc)
        text, html = self.engine.fetch(
            url, country=adapter.cc, referer=adapter_base,
            wait_selector=adapter.list_wait_selector)
        if html is None:
            return [], f"{self.engine.last_status}:web"
        items = adapter.parse_listings(html, text or "")

        # ★ 低产率检测：页面上一堆商品，解析器只认出几条。
        #   这与「搜索没生效」是两种故障，指错方向会让人去修一个正确的 URL。
        low = check_yield(html, len(items))
        if low:
            log.warning("[%s/%s] %s", adapter.cc, adapter.channel.get("name", ""),
                        low["reason"])
            livelog.emit("warn",
                         f"{adapter.channel.get('name','')}：解析低产 "
                         f"{low['parsed']}/{low['links_on_page']} —— 需专用适配器",
                         channel=adapter.channel.get("name", ""))
            self._stash_raw_page(url, adapter, text or "")

        if not items and text:
            # 前两级都空 → 把文本留给 LLM 兜底（由清洗 Agent 在 analyze 阶段处理）
            self._stash_raw_page(url, adapter, text)
        return items[: self.max_items], (f"ok:{url_mode}" if items else f"empty:{url_mode}")

    def _stash_raw_page(self, url: str, adapter, text: str) -> None:
        try:
            with db.tx() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO raw_page(url,channel_id,country_code,
                                                   page_kind,text_hash,text)
                    VALUES(?,?,?,?,?,?)
                """, (url, adapter.channel["id"], adapter.cc, "search",
                      db.row_hash(text[:5000]), text[:20000]))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------ 详情页（带缓存）

    def _fetch_detail(self, adapter, lst: Listing, channel: dict,
                      country: dict) -> Listing | None:
        if lst.detail_fetched:          # API 通道已经是详情级数据
            return lst
        if not adapter.detail_url_looks_valid(lst.url):
            return lst

        cached = db.q1("SELECT * FROM product_page_cache WHERE url=?", (lst.url,))
        fresh_specs = False
        age = 10_000
        if cached and cached.get("specs_fetched_at"):
            try:
                age = (datetime.now()
                       - datetime.fromisoformat(cached["specs_fetched_at"])).days
                fresh_specs = age < SPEC_REFRESH_DAYS
            except Exception:  # noqa: BLE001
                fresh_specs = False

        # ★ 缓存命中 → 跳过这次详情页请求。
        #   价格仍取列表页的当日值（价格必须新鲜），缓存的只是卖家/规格这些
        #   不天天变的字段。这是"全部进详情页"能在可接受时间内跑完的关键。
        #
        # ★★ 但缓存的是【判定结论】，而判定逻辑会演进。
        #    真实事故：修好"授权经销商≠品牌官方店"之后，Coppel 仍有 8 条判成
        #    brand_official —— 因为它们命中了缓存，沿用的是修复**之前**的结论。
        #    表现是"修复没生效"，而且会一直沿用下去。
        #    所以缓存必须带逻辑版本号，版本对不上就作废重判。
        if (cached and age < DETAIL_REFRESH_DAYS and cached.get("specs_json")
                and _cache_logic_version(cached) == SELLER_LOGIC_VERSION):
            try:
                blob = json.loads(cached["specs_json"])
                lst.specs.update({k: v for k, v in blob.items()
                                  if not k.startswith("_cached_")})
                lst.seller_name = blob.get("_cached_seller_name") or lst.seller_name
                lst.seller_type = blob.get("_cached_seller_type") or lst.seller_type
                lst.seller_kind = blob.get("_cached_seller_kind") or lst.seller_kind
                lst.seller_shipper = (blob.get("_cached_seller_shipper")
                                      or lst.seller_shipper)
                # age 用 max(0,…)：同日重跑时时间差不足一天会算出 -1，
                # 显示成"沿用 -1 天前的判定"很费解
                lst.seller_reason = (f"沿用 {max(age, 0)} 天前的详情页判定："
                                     f"{blob.get('_cached_seller_reason', '')}")
                # ★ 标题必须一并回填：它参与 row_hash 计算。
                #   只缓存卖家/规格而漏掉标题，会让「详情页补全过的长标题」
                #   在缓存命中日退回成「列表页的短标题」，row_hash 随之改变
                #   → 同一商品每天新增一行，幂等性静默失效、数据翻倍。
                cached_title = blob.get("_cached_title")
                if cached_title and len(cached_title) > len(lst.title):
                    lst.title = cached_title
                    self._enrich_from_title_via(adapter, lst)
                lst.detail_fetched = True
                self.stats["detail_cached"] += 1
                self._touch_cache(lst.url)
                return lst
            except Exception:  # noqa: BLE001
                pass          # 缓存坏了就当没有，照常抓

        # Referer 用站内搜索页而非空串：从搜索结果点进商品页才是真人动线
        text, html = self.engine.fetch(
            lst.url, country=country["code"], referer=channel.get("base_url"),
            wait_selector=adapter.detail_wait_selector, extra_wait=2.0)
        if html is None:
            # 详情页打不开不算整条失败：列表页的价格仍然有效，标注一下即可
            lst.specs["_detail_status"] = self.engine.last_status
            return lst

        lst = adapter.parse_detail(html, text or "", lst)
        self.stats["details"] += 1

        if fresh_specs and cached.get("specs_json"):
            try:                        # 复用缓存规格，省掉重复解析
                lst.specs.update(json.loads(cached["specs_json"]))
                self.stats["detail_cached"] += 1
            except Exception:  # noqa: BLE001
                pass

        self._update_cache(lst, channel, country)
        return lst

    def _update_cache(self, lst: Listing, channel: dict, country: dict) -> None:
        try:
            # 卖家判定也进缓存：下次命中缓存时不用再开详情页就能填回来
            blob = dict(lst.specs)
            blob["_logic_version"] = SELLER_LOGIC_VERSION   # 判定逻辑版本，见上方说明
            blob["_cached_title"] = lst.title      # 参与 row_hash，必须缓存
            blob["_cached_seller_name"] = lst.seller_name
            blob["_cached_seller_type"] = lst.seller_type
            blob["_cached_seller_kind"] = lst.seller_kind
            blob["_cached_seller_shipper"] = lst.seller_shipper
            blob["_cached_seller_reason"] = lst.seller_reason
            with db.tx() as conn:
                conn.execute("""
                    INSERT INTO product_page_cache(url,channel_id,country_code,
                        specs_json,specs_fetched_at,last_price_at,fetch_count,last_status)
                    VALUES(?,?,?,?,datetime('now'),datetime('now'),1,'ok')
                    ON CONFLICT(url) DO UPDATE SET
                      specs_json=excluded.specs_json,
                      specs_fetched_at=datetime('now'),
                      last_price_at=datetime('now'),
                      fetch_count=product_page_cache.fetch_count+1,
                      last_status='ok'
                """, (lst.url, channel["id"], country["code"],
                      json.dumps(blob, ensure_ascii=False)[:8000]))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _enrich_from_title_via(adapter, lst: Listing) -> None:
        """标题变了就重解析规格（RAM/ROM/颜色/成色都从标题来）"""
        try:
            adapter._enrich_from_title(lst)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _touch_cache(url: str) -> None:
        """缓存命中时只更新"最近用到"时间，不动规格与抓取时间"""
        try:
            with db.tx() as conn:
                conn.execute("""UPDATE product_page_cache
                                SET last_price_at=datetime('now'),
                                    fetch_count=fetch_count+1 WHERE url=?""", (url,))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------ 入库

    def _persist(self, items: list[Listing], channel: dict, country: dict,
                 brand: dict, category: str) -> int:
        obs_date = db.today()
        written = 0
        with db.tx() as conn:
            for it in items:
                if not it.is_usable():
                    continue
                bid, bname = self.guess_brand(it.title)
                # 品牌词搜出来的结果里混着别家的，按标题判定实际品牌
                brand_id = bid or (brand["id"] if it.brand_guess and
                                   brand["name"].lower() in (it.brand_guess or "").lower()
                                   else None)
                h = db.row_hash(obs_date, channel["id"], country["code"],
                                it.url or it.title, it.title, it.sale_price,
                                it.ram_gb, it.rom_gb, it.color)
                try:
                    cur = conn.execute("""
                        INSERT OR IGNORE INTO price_obs(
                          obs_date,country_code,channel_id,brand_id,category_code,
                          title,model_guess,ram_gb,rom_gb,color,
                          list_price,sale_price,currency,installments,
                          seller_name,seller_type,seller_kind,seller_shipper,
                          product_kind,sku_code,discount_pct,
                          is_in_stock,condition,is_bundle,
                          store_stock,store_units,store_name,
                          url,row_hash,run_id,audit_reason)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (obs_date, country["code"], channel["id"], brand_id, category,
                          it.title[:250], it.model_guess, it.ram_gb, it.rom_gb, it.color,
                          it.list_price, it.sale_price, it.currency or country["currency"],
                          it.installments, it.seller_name,
                          it.seller_type, it.seller_kind, it.seller_shipper,
                          it.product_kind, it.sku_code, it.discount_pct,
                          1 if it.is_in_stock else 0,
                          it.condition, 1 if it.is_bundle else 0,
                          it.store_stock, it.store_units, it.store_name,
                          it.url, h, self.run_id, it.seller_reason))
                    # rowcount 才是本次插入行数；total_changes 是连接累计值，
                    # 用它会把"曾经插过任何行"误判成"这行插进去了"
                    if cur.rowcount > 0:
                        written += 1
                except Exception as e:  # noqa: BLE001
                    log.debug("入库失败: %s", str(e)[:120])
        return written

    def _aliases_of(self, brand: dict) -> list[str]:
        try:
            return [brand["name"]] + json.loads(
                self._brand_rows.get(brand["name"], {}).get("aliases") or "[]")
        except Exception:  # noqa: BLE001
            return [brand["name"]]

    @staticmethod
    def _merge_status(statuses: list[str], n_items: int) -> str:
        if not statuses:
            return "failed"
        if n_items > 0:
            return "ok"
        # irrelevant 必须与 empty 分开：
        #   empty      = 页面正常但该渠道确实没有这个品牌的货
        #   irrelevant = 搜索没生效，抓回来的是别的东西 → 需要修 URL/适配器
        # 混成一个状态，主 Agent 就没法区分"该修"和"正常无货"
        if "irrelevant" in statuses:
            return "irrelevant"
        if "login_wall" in statuses:
            return "login_wall"
        if "blocked" in statuses:
            return "blocked"
        if all(s == "empty" for s in statuses):
            return "empty"
        return "failed"
