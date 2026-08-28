# -*- coding: utf-8 -*-
"""渠道适配器基类：定义"搜索 → 列表 → 详情"的通用流程与数据契约。

每个渠道站的 HTML 都不一样，但抓取流程是同一个骨架：
    搜索URL  →  列表页(拿到 N 个商品的名字/价格/链接)
             →  逐个进详情页(拿卖家/库存/规格/分期)
             →  归一化成 Listing

适配器只需要覆写"这个站的列表怎么解析"，其余（反爬、重试、缓存、
去重、留痕）全由框架处理。覆写不了的站直接用 GenericAdapter。

★ 三级抽取，按可靠性排序：
    1. JSON-LD (schema.org Product)  —— 最可靠，拉美主流零售站大多有
    2. 站点选择器 / 通用商品卡片启发式 —— 次之
    3. 页面文本交 LLM 抽取           —— 兜底，慢且花 token，只在前两级都空时用
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from ... import skumap
from .. import extract

log = logging.getLogger("channel")


# ---------------------------------------------------------------- 数据契约

@dataclass
class Listing:
    """一条商品挂牌。列表页先填一部分，详情页再补全。"""
    title: str = ""
    url: str = ""
    sale_price: float | None = None      # 现价（生效价）
    list_price: float | None = None      # 划线原价
    currency: str = ""
    # 卖家与库存
    seller_name: str | None = None
    seller_type: str = "unknown"        # 粗分 official | third_party | unknown
    seller_kind: str = "unknown"        # 细分 self_operated | brand_official | third_party
    seller_shipper: str | None = None   # 配送方（≠卖家；Amazon 上常分离）
    seller_reason: str = ""
    is_in_stock: bool = True
    # 门店铺货信号（方向 14）：None = 页面没有门店模块，不是「无货」
    store_stock: int | None = None
    store_units: int | None = None
    store_name: str | None = None
    condition: str = "new"
    is_bundle: bool = False
    product_kind: str = "unknown"      # device | accessory | unknown
    sku_code: str | None = None        # 权威 SKU（app/skumap.py）
    discount_pct: float | None = None  # off 力度 %，入库前算好
    installments: str | None = None
    # 规格
    brand_guess: str | None = None
    model_guess: str | None = None
    ram_gb: int | None = None
    rom_gb: int | None = None
    color: str | None = None
    screen_size: float | None = None
    specs: dict = field(default_factory=dict)
    # 溯源
    source: str = ""                     # jsonld | selector | llm | api
    detail_fetched: bool = False
    raw_text: str | None = None          # 前两级都失败时留给 LLM

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_text", None)
        return d

    def is_usable(self) -> bool:
        return bool(self.title) and self.sale_price is not None


def _discount(list_price: float | None, sale_price: float | None) -> float | None:
    """off 力度 %。

    ★ 必须区分「没有折扣」(0.0) 与「算不出折扣」(None)：
      没有划线价 → None（不知道原价，谈不上折扣）
      有划线价但 ≤ 售价 → 0.0（明确没打折，不是缺数据）
      查询时现算会把这两种糊成一样，做促销力度分析时得出错误结论。
    """
    if sale_price is None or list_price is None or list_price <= 0:
        return None
    if list_price <= sale_price:
        return 0.0
    return round((list_price - sale_price) / list_price * 100, 2)


# ---------------------------------------------------------------- 基类

class ChannelAdapter:
    """渠道适配器基类。子类通常只需覆写 parse_listings / parse_detail。"""

    name = "base"
    # 该站是否天然需要 Selenium 兜底（如 Shopee 反爬极强）
    force_fallback = False
    # 列表页需要等待的选择器（等到它出现再取内容，减少空页）
    list_wait_selector: str | None = None
    detail_wait_selector: str | None = None

    def __init__(self, channel: dict, country: dict):
        self.channel = channel          # channel 表的一行
        self.country = country          # country 表的一行
        self.currency = country["currency"]
        self.cc = country["code"]
        # 本轮在抓哪个品类。由 list_url() 记下，解析标题时要用它把
        # "只覆盖平板的权威 SKU 表"挡在其他品类之外（详见 skumap.classify）。
        self.current_category: str | None = None

    # ------------------------------------------------ URL

    def list_url(self, query: str, category: str | None = None) -> tuple[str, str]:
        """决定这一轮去抓哪个 URL。返回 (url, 模式)。

        模式 category_page：品牌官方商城没有可用站内搜索，但品类页带完整
                            JSON-LD（实测 samsung.com/mx 品类页有 10 个
                            Product，而搜索页一个都没有）。
        模式 search：常规站内搜索。
        """
        self.current_category = category
        cat_urls = self.channel.get("category_urls")
        if cat_urls and category:
            try:
                mapping = json.loads(cat_urls) if isinstance(cat_urls, str) else cat_urls
            except Exception:  # noqa: BLE001
                mapping = {}
            url = (mapping or {}).get(category)
            if url:
                return url, "category_page"
        return self.build_search_url(query), "search"

    def build_search_url(self, query: str) -> str:
        tpl = self.channel.get("search_url") or ""
        if not tpl:
            return ""
        # MercadoLibre 那种路径式搜索用 - 连接，查询串式用标准编码
        if "{q}" not in tpl:
            return tpl
        if tpl.rstrip("/").endswith("{q}") and "?" not in tpl:
            slug = re.sub(r"\s+", "-", query.strip().lower())
            return tpl.replace("{q}", quote(slug, safe="-"))
        return tpl.replace("{q}", quote(query.strip()))

    def absolutize(self, href: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        return urljoin(self.channel.get("base_url") or "", href)

    # ------------------------------------------------ 列表页

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        """默认实现：JSON-LD → 通用商品卡片启发式。子类可覆写。"""
        items = self._from_jsonld(html)
        if not items:
            items = self._from_cards(html)
        # ★ 列表页也要判卖家：Liverpool 的 `Vendido por` 就长在商品卡片里，
        #   等进详情页才判会漏掉大部分（详情页有配额，不是每条都进）。
        #   卡片自带的局部文本比整页文本准得多 —— 整页文本里页头页脚的
        #   品牌词会污染判定（这正是旧实现全判成官方的原因）。
        # ★ 把"页面框架文案"挡在入库之前。
        #   通用卡片启发式会把 Falabella 的筛选侧栏当成商品卡，
        #   价格取到的是**价格筛选滑块的档位值**（100,000 / 500,000 COP）
        #   甚至滑块最小值 52 —— 一台"手机"52 比索进了价格基线，且不报错。
        kept = [x for x in items if not extract.looks_like_page_chrome(x.title)]
        if len(kept) != len(items):
            log.info("[%s] 丢弃 %d 条页面框架文案（筛选面板/导航被当成商品）",
                     self.name, len(items) - len(kept))
        items = kept

        for lst in items:
            if lst.seller_kind == "unknown":
                self.apply_seller(lst, "", lst.specs.get("_card_text", "") or "")
        return items

    def _from_jsonld(self, html: str) -> list[Listing]:
        out = []
        for p in extract.extract_jsonld_products(html):
            sale = extract.parse_price(p.get("sale_price_raw"), self.currency)
            if sale is None:
                continue
            lst = Listing(
                title=p["title"],
                url=self.absolutize(p.get("url") or ""),
                sale_price=sale,
                list_price=extract.parse_price(p.get("list_price_raw"), self.currency),
                currency=p.get("currency") or self.currency,
                seller_name=p.get("seller_name") or None,
                brand_guess=p.get("brand") or None,
                is_in_stock=extract.detect_in_stock("", p.get("availability", "")),
                source="jsonld",
            )
            self._enrich_from_title(lst)
            out.append(lst)
        return out

    # 通用商品卡片启发式：找"链接 + 其子树含价格"的结构。
    # 电商列表页几乎都是这个形态，比给每个站写 CSS 选择器耐改版得多。
    _PRICE_IN_TEXT = re.compile(r"(?:R\$|S/\.?|\$|MXN|BRL|CLP|COP|PEN|ARS)\s*[\d.,]{3,}")

    def _from_cards(self, html: str) -> list[Listing]:
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        seen_urls, out = set(), []
        for a in soup.find_all("a", href=True):
            href = self.absolutize(a["href"])
            if not href or href in seen_urls:
                continue
            blob = a.get_text(" ", strip=True)
            if len(blob) < 8:
                # 图片型链接：价格常在父级卡片上
                parent = a.find_parent(["li", "div", "article"])
                blob = parent.get_text(" ", strip=True) if parent else blob
            m = self._PRICE_IN_TEXT.search(blob)
            if not m:
                continue
            title = (a.get("title") or a.get("aria-label") or "").strip()
            if not title:
                title = self._guess_title(blob, m.group(0))
            if len(title) < 6:
                continue
            prices = self._PRICE_IN_TEXT.findall(blob)
            parsed = [extract.parse_price(p, self.currency) for p in prices]
            parsed = [p for p in parsed if p and extract.price_is_sane(p, self.currency)]
            if not parsed:
                continue
            # 一个卡片里出现多个价格时：最小的是现价，次大的是划线原价
            sale = min(parsed)
            lst_price = max(parsed) if len(parsed) > 1 and max(parsed) > sale else None
            seen_urls.add(href)
            item = Listing(
                title=title[:250], url=href, sale_price=sale, list_price=lst_price,
                currency=self.currency, source="selector",
                installments=extract.parse_installments(blob),
            )
            # 卡片自身的文本留给卖家判定用 —— 比整页文本准得多：
            # 整页文本里页头页脚的品牌词会让每条商品都命中"官方"
            item.specs["_card_text"] = blob[:600]
            # ★ 门店库存也只从**卡片自身文本**里取，理由同上：
            #   用整页文本会把某一个商品的门店库存记到同页所有商品头上。
            #   （detect_store_stock 内部还有一道"一页只能有一处模块"的闸，
            #     两层都在，因为这里将来可能被别的调用方复用。）
            for _k, _v in extract.detect_store_stock(blob).items():
                if _v is not None:
                    setattr(item, _k, _v)
            self._enrich_from_title(item)
            out.append(item)
        return out

    @staticmethod
    def _guess_title(blob: str, price_str: str) -> str:
        """卡片文本里价格之前的一段通常就是标题"""
        head = blob.split(price_str)[0].strip()
        head = re.sub(r"\s{2,}", " ", head)
        return head[-160:].strip() if head else blob[:160]

    def _enrich_from_title(self, lst: Listing) -> None:
        """从标题解析出规格与成色 —— 列表页也能拿到的部分先拿到"""
        # ★ 先修双重编码，再做任何解析。
        #   乱码的标题会让噪声词、颜色词、配件词全部匹配不上，
        #   一路污染到型号归一化和比价（实测 Falabella 1400 条受影响）。
        lst.title = extract.fix_mojibake(lst.title)
        # ★ 再剥掉渠道界面/促销角标（「Vista Previa」「Envío gratis app」
        #   「… 4.7 (394) 35% 35%」）—— 顺序不能反：乱码的 Envío 存成 EnvÃ­o，
        #   词表根本认不出来。剥完才轮到规格/配件/型号归一化，
        #   否则角标会把配件词挤出主语位置（详见 extract.strip_ui_chrome）。
        _raw_title = lst.title
        lst.title = extract.strip_ui_chrome(lst.title)
        if lst.title != _raw_title:
            # 留一份原文：词表改错了要能回头对账，不然剥掉的东西无处可查
            lst.specs["_title_raw"] = _raw_title[:250]
        lst.ram_gb, lst.rom_gb = extract.parse_ram_rom(lst.title)
        lst.color = extract.parse_color(lst.title)
        lst.screen_size = extract.parse_screen_size(lst.title)
        lst.condition = extract.detect_condition(lst.title)
        lst.is_bundle = extract.detect_bundle(lst.title)
        # ★ 权威 SKU 表优先（用户的 PowerQuery SKU_Short，272 规则 / 131 SKU）。
        #   用户要求"每次抓取都必须包含这些产品"，所以先用权威表判，
        #   它同时给出配件判定 —— 那套配件规则比通用启发式精细得多
        #   （比如 book cover 含 gb/ram 就算整机捆绑）。
        # ★ 必须带上本轮品类：这张表只覆盖平板，不带品类会让它去认领
        #   耳机/手机（实测 AirPods 被判成 Apple iPad Air，评论一路记错人）。
        hit = skumap.classify(lst.title, self.current_category)
        if hit["kind"] == "sku":
            lst.sku_code = hit["sku"]
            lst.product_kind = "device"
            lst.specs["_sku_reason"] = hit["reason"]
        elif hit["kind"] == "accessory":
            lst.product_kind = "accessory"
            lst.specs["_sku_reason"] = f"权威表判定为配件：{hit['reason']}"
        else:
            # 权威表没覆盖的品类（手机/穿戴/音频/PC）走通用启发式
            kind, why = extract.detect_product_kind(lst.title)
            lst.product_kind = kind
            if kind != "unknown":
                lst.specs["_kind_reason"] = why

        # off 力度：入库前算好，并区分"没有折扣"与"算不出折扣"
        lst.discount_pct = _discount(lst.list_price, lst.sale_price)

    # ------------------------------------------------ 详情页

    def parse_detail(self, html: str, text: str, lst: Listing) -> Listing:
        """进详情页补全：卖家、库存、分期、规格。子类可覆写取更准的字段。"""
        lst.detail_fetched = True

        # JSON-LD 在详情页更完整，但★不能盲信第一个：
        # 详情页几乎都有"相关推荐"区块，那些商品也各自带 JSON-LD Product。
        # 取错就会把价格记到别的产品名下，而且不报错、静默算错。
        jl = extract.extract_jsonld_products(html)
        p = self._pick_matching_jsonld(jl, lst)
        if p:
            sale = extract.parse_price(p.get("sale_price_raw"), self.currency)
            if sale and extract.price_is_sane(sale, self.currency):
                lst.sale_price = sale
            if p.get("seller_name"):
                lst.seller_name = p["seller_name"]
            if p.get("brand"):
                lst.brand_guess = p["brand"]
            if p.get("availability"):
                lst.is_in_stock = extract.detect_in_stock(text or "", p["availability"])
            if p.get("title") and len(p["title"]) > len(lst.title):
                lst.title = p["title"]
                self._enrich_from_title(lst)

        self.apply_seller(lst, html, text)
        # ★ 在**剥掉门店文案之前**先把它解析出来 —— 这段文案原本只作为
        #   假朋友被抹掉（不抹会把整条挂牌误判成缺货），抹完不存等于扔掉指标
        for k, v in extract.detect_store_stock(text or "").items():
            if v is not None:
                setattr(lst, k, v)
        if lst.is_in_stock:
            lst.is_in_stock = extract.detect_in_stock(text or "")
        if not lst.installments:
            lst.installments = extract.parse_installments(text or "")
        lst.condition = extract.detect_condition(lst.title, (text or "")[:1500])
        lst.specs.update(self.extract_spec_table(html, text))
        # 详情页规格表比标题准，用它回填
        if lst.ram_gb is None or lst.rom_gb is None:
            r, o = extract.parse_ram_rom(" ".join(str(v) for v in lst.specs.values())[:400])
            lst.ram_gb = lst.ram_gb or r
            lst.rom_gb = lst.rom_gb or o
        return lst

    def apply_seller(self, lst: Listing, html: str = "", text: str = "") -> None:
        """判定并写入卖家身份（自营 / 品牌官方店 / 第三方）。

        用户明确要求分清：Liverpool 的自营 vs marketplace 商家、
        MercadoLibre 与 Amazon 的官方店 vs 非官方。判定逻辑见 seller.py。
        子类若有更硬的证据（如 MELI 的 official_store_id）应覆写此方法。
        """
        from .. import seller as seller_mod

        if not lst.seller_name:
            lst.seller_name = self.extract_seller_name(html, text)
        name, shipper = (lst.seller_name, None)
        if not name:
            name, shipper = seller_mod.extract_seller_name(text or "", html)
        r = seller_mod.classify(
            name, self.name, text or "", lst.brand_guess or "",
            self.channel.get("default_seller_type", "unknown"),
            official_store_id=lst.specs.get("official_store_id"),
            shipper=shipper)
        lst.seller_name = r["seller_name"] or lst.seller_name
        lst.seller_kind = r["kind"]
        lst.seller_type = r["coarse"]
        lst.seller_reason = r["reason"]
        lst.seller_shipper = shipper

    def _pick_matching_jsonld(self, candidates: list[dict], lst: Listing) -> dict | None:
        """从详情页的多个 JSON-LD Product 里挑出「就是当前这个商品」的那个。

        判据（任一成立即认为匹配，按可靠性排序）：
          1. 价格与列表页价格接近（±15%）—— 最硬的证据
          2. 标题词高度重叠（Jaccard ≥ 0.5）
        两条都不满足就返回 None：宁可少补几个字段，也不能把别的商品
        的价格/卖家写到这一条上。
        """
        if not candidates:
            return None
        if len(candidates) == 1 and lst.sale_price is None:
            return candidates[0]

        best, best_score = None, 0.0
        for c in candidates:
            score = 0.0
            price = extract.parse_price(c.get("sale_price_raw"), self.currency)
            if price and lst.sale_price:
                diff = abs(price - lst.sale_price) / max(lst.sale_price, 1)
                if diff <= 0.15:
                    score += 2.0 - diff        # 价格越接近分越高
            sim = self._title_similarity(c.get("title", ""), lst.title)
            if sim >= 0.5:
                score += sim
            if score > best_score:
                best, best_score = c, score
        return best if best_score > 0 else None

    @staticmethod
    def _title_similarity(a: str, b: str) -> float:
        """标题词集 Jaccard 相似度。型号名里的数字/字母混排 token 最有区分度。"""
        def toks(s: str) -> set[str]:
            return {t for t in re.split(r"[^\w]+", (s or "").lower()) if len(t) >= 2}
        ta, tb = toks(a), toks(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    # 卖家名的常见容器（覆盖大多数拉美电商）
    _SELLER_SELECTORS = [
        "[class*=seller-name]", "[class*=sellerName]", "[data-testid*=seller]",
        "[class*=vendedor]", "[class*=vendido-por]", "[class*=store-name]",
        "[id*=sellerProfile]", "[class*=merchant]",
    ]

    def extract_seller_name(self, html: str, text: str) -> str | None:
        if not html:
            return None
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            return None
        for sel in self._SELLER_SELECTORS:
            el = soup.select_one(sel)
            if el:
                name = el.get_text(" ", strip=True)[:80]
                if 2 <= len(name) <= 80:
                    return name
        # 文本兜底："Vendido por XXX" / "Vendido e entregue por XXX"
        m = re.search(r"(?:vendido (?:y enviado )?por|vendido e entregue por|"
                      r"vendedor|sold by)\s*:?\s*([A-Za-z0-9ÁÉÍÓÚÑÇáéíóúñç&.\- ]{2,50})",
                      text or "", re.I)
        return m.group(1).strip() if m else None

    def extract_spec_table(self, html: str, text: str) -> dict:
        """详情页规格表 → dict。大多数站用 <table> 或 <dl> 或 key/value 双列 div。"""
        specs: dict = {}
        if not html:
            return specs
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            return specs
        for table in soup.find_all("table")[:6]:
            for tr in table.find_all("tr")[:60]:
                cells = tr.find_all(["th", "td"])
                if len(cells) == 2:
                    k = cells[0].get_text(" ", strip=True)[:50]
                    v = cells[1].get_text(" ", strip=True)[:120]
                    if k and v and len(specs) < 60:
                        specs[k] = v
        for dl in soup.find_all("dl")[:6]:
            dts, dds = dl.find_all("dt"), dl.find_all("dd")
            for dt, dd in zip(dts[:40], dds[:40]):
                k = dt.get_text(" ", strip=True)[:50]
                v = dd.get_text(" ", strip=True)[:120]
                if k and v and len(specs) < 60:
                    specs[k] = v
        return specs

    # ------------------------------------------------ 详情页是否值得进

    def detail_url_looks_valid(self, url: str) -> bool:
        """过滤掉分类页/搜索页/广告位链接，别把它们当商品页去抓。"""
        if not url or not url.startswith("http"):
            return False
        path = urlparse(url).path.lower()
        bad = ("/search", "/busca", "/buscar", "/categoria", "/category", "/c/",
               "/listado", "/lista", "/help", "/ayuda", "/login", "/cart", "/carrito")
        return not any(b in path for b in bad)
