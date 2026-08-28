# -*- coding: utf-8 -*-
"""站点特定适配器。

只在"通用启发式会出错"的地方覆写，其余全部继承基类。
覆写得越少越耐改版 —— 站点改版时，通用 JSON-LD 通道往往还活着。
"""
from __future__ import annotations

import html as _html_mod
import json
import logging
import re

from urllib.parse import quote

from bs4 import BeautifulSoup

from ... import skunorm as _skunorm
from .. import extract
from .base import ChannelAdapter, Listing

log = logging.getLogger("sites")


class GenericAdapter(ChannelAdapter):
    """通用适配器：JSON-LD → 商品卡片启发式 → （上层再决定要不要 LLM 兜底）"""
    name = "generic"


class AmazonAdapter(ChannelAdapter):
    """Amazon 把价格拆成整数/小数两个 span（$1,299 + .00），
    通用启发式会把它读成两个价格并取最小值 → 必须特殊处理。"""
    name = "amazon"
    list_wait_selector = "div[data-component-type='s-search-result']"

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            soup = BeautifulSoup(html, "html.parser")

        out = []
        for card in soup.select("div[data-component-type='s-search-result']")[:60]:
            title_el = (card.select_one("h2 a span") or card.select_one("h2 span")
                        or card.select_one("h2"))
            link_el = card.select_one("h2 a") or card.select_one("a.a-link-normal")
            if not title_el or not link_el:
                continue
            title = title_el.get_text(" ", strip=True)
            href = self.absolutize(link_el.get("href", ""))
            if len(title) < 6 or not href:
                continue

            sale = self._price_from(card.select_one("span.a-price"))
            if sale is None:
                # offscreen 是给读屏器的完整价格串，最可靠
                off = card.select_one("span.a-price > span.a-offscreen")
                sale = extract.parse_price(off.get_text(strip=True), self.currency) if off else None
            if sale is None:
                continue
            list_price = self._price_from(
                card.select_one("span.a-price.a-text-price")) or None
            if list_price and list_price <= sale:
                list_price = None

            item = Listing(
                title=title[:250], url=href, sale_price=sale, list_price=list_price,
                currency=self.currency, source="selector",
                installments=extract.parse_installments(card.get_text(" ", strip=True)),
            )
            item.specs["_card_text"] = card.get_text(" ", strip=True)[:600]
            self._enrich_from_title(item)
            self.apply_seller(item, "", item.specs["_card_text"])
            out.append(item)
        return out or super().parse_listings(html, text)

    def apply_seller(self, lst: Listing, html: str = "", text: str = "") -> None:
        """Amazon 的卖家与配送方是分开的两行，必须分开读。

        实抓详情页（2026-08-10）确认存在这两个标签：
            「Vendido por:」  ← 卖家，决定是不是官方
            「Enviado por:」  ← 配送方，第三方卖家用亚马逊物流时这里也是 Amazon

        ★ 把配送方当卖家会把大量第三方误判成官方 —— "第三方卖家 + 亚马逊配送"
          在墨西哥站非常普遍。用户要求分清 Amazon 官方与非官方，差别就在这。
        """
        from .. import seller as seller_mod

        blob = text or ""
        name, shipper = seller_mod.extract_seller_name(blob, html)
        if not name:
            for sel in self._SELLER_SELECTORS:
                el = self._select_one(html, sel)
                if el:
                    name = el[:60]
                    break
        r = seller_mod.classify(
            name, self.name, blob, lst.brand_guess or "",
            self.channel.get("default_seller_type", "unknown"), shipper=shipper)
        lst.seller_name = r["seller_name"] or name
        lst.seller_kind = r["kind"]
        lst.seller_type = r["coarse"]
        lst.seller_shipper = shipper
        lst.seller_reason = r["reason"] + (
            f"；配送方「{shipper}」（配送方≠卖家，不参与判定）" if shipper else "")

    @staticmethod
    def _select_one(html: str, sel: str) -> str | None:
        if not html:
            return None
        try:
            soup = BeautifulSoup(html, "lxml")
            el = soup.select_one(sel)
            return el.get_text(" ", strip=True) if el else None
        except Exception:  # noqa: BLE001
            return None

    def _price_from(self, price_el) -> float | None:
        if not price_el:
            return None
        off = price_el.select_one("span.a-offscreen")
        if off:
            return extract.parse_price(off.get_text(strip=True), self.currency)
        whole = price_el.select_one("span.a-price-whole")
        frac = price_el.select_one("span.a-price-fraction")
        if whole:
            raw = whole.get_text(strip=True).rstrip(".,")
            if frac:
                raw += "." + frac.get_text(strip=True)
            return extract.parse_price(raw, self.currency)
        return None

    _SELLER_SELECTORS = ["#sellerProfileTriggerId", "#merchant-info",
                         "[id*=tabular-buybox] [class*=tabular-buybox-text]"]


class FalabellaAdapter(ChannelAdapter):
    """Falabella 三国同构（CL/CO/PE）。列表页是 React SPA，
    但服务端会渲染 JSON-LD，所以 JSON-LD 通道通常够用。"""
    name = "falabella"
    list_wait_selector = "[data-pod], [class*=search-results]"

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        items = super().parse_listings(html, text)
        if items:
            return items
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            return []
        out = []
        for pod in soup.select("[data-pod], div[class*=pod-]")[:60]:
            title_el = pod.select_one("[id*=pod-displaySubTitle], b[class*=pod-subTitle], "
                                      "[class*=pod-title]")
            link = pod.select_one("a[href]")
            price_el = pod.select_one("[class*=prices-] li, [data-internet-price], "
                                      "[class*=copy] span")
            if not (title_el and link):
                continue
            title = title_el.get_text(" ", strip=True)
            blob = pod.get_text(" ", strip=True)
            sale = None
            if price_el:
                sale = extract.parse_price(price_el.get_text(" ", strip=True), self.currency)
            if sale is None:
                cands = [extract.parse_price(p, self.currency)
                         for p in self._PRICE_IN_TEXT.findall(blob)]
                cands = [c for c in cands if c and extract.price_is_sane(c, self.currency)]
                sale = min(cands) if cands else None
            if not title or sale is None:
                continue
            item = Listing(title=title[:250], url=self.absolutize(link["href"]),
                           sale_price=sale, currency=self.currency, source="selector",
                           installments=extract.parse_installments(blob))
            self._enrich_from_title(item)
            out.append(item)
        return out

    _SELLER_SELECTORS = ["[class*=seller]", "[data-testid*=seller]", "[class*=vendedor]"]


class RipleyAdapter(ChannelAdapter):
    """Ripley（CL/PE）"""
    name = "ripley"
    list_wait_selector = "[class*=catalog-product-item], a[class*=catalog-product]"

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        items = super().parse_listings(html, text)
        if items:
            return items
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            return []
        out = []
        for card in soup.select("a[class*=catalog-product-item], div[class*=catalog-product]")[:60]:
            blob = card.get_text(" ", strip=True)
            name_el = card.select_one("[class*=catalog-product-details__name], "
                                      "[class*=product-name]")
            title = (name_el.get_text(" ", strip=True) if name_el else "")[:250]
            href = card.get("href") or (card.select_one("a[href]") or {}).get("href", "")
            cands = [extract.parse_price(p, self.currency)
                     for p in self._PRICE_IN_TEXT.findall(blob)]
            cands = [c for c in cands if c and extract.price_is_sane(c, self.currency)]
            if not title or not cands:
                continue
            item = Listing(title=title, url=self.absolutize(href), sale_price=min(cands),
                           list_price=max(cands) if len(cands) > 1 else None,
                           currency=self.currency, source="selector",
                           installments=extract.parse_installments(blob))
            self._enrich_from_title(item)
            out.append(item)
        return out


class LiverpoolAdapter(ChannelAdapter):
    """Liverpool 墨西哥 —— 自营 + marketplace 混合站。

    ★ 用户明确要求分清自营与第三方商家。实抓（2026-08-10）得到的判据：
      列表页 56 个商品卡片里，只有 **1 处** `Vendido por` 标签。
      也就是说 —— **marketplace 商品才显式标出商家，自营的什么都不标**。

      所以判据是"标签的存在性"，不是关键词匹配。
      旧实现在整页文本里搜 "liverpool" 当官方标识，而页头页脚 logo alt
      到处是这个词 ⇒ 该站所有商品（含第三方）全判成自营，两类被混成一类。
    """
    name = "liverpool"
    list_wait_selector = "[class*=product-card], [data-testid*=product]"
    _SELLER_SELECTORS = ["[class*=seller]", "[class*=vendido]", "[class*=marketplace]"]

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        items = super().parse_listings(html, text)
        # Liverpool 价格常写成区间（"$23,999.20 - $27,999.20"，不同容量/配色）。
        # 基类取 min 作现价、max 作划线原价 —— 对区间价这是错的：
        # 两个都是现价，把上限当"原价"会算出一个不存在的折扣。
        for it in items:
            blob = it.specs.get("_card_text", "") or ""
            if it.list_price and _RANGE_PRICE.search(blob):
                it.list_price = None
                it.specs["price_is_range"] = True
                it.specs["price_range_note"] = "卡片为价格区间（多配置），取下限为现价"
        return items


# "$ 23,999 . 20 - $ 27,999 . 20" 这种区间价（Liverpool 常见）
_RANGE_PRICE = re.compile(
    r"\$\s?[\d.,]{3,}\s*[-–—]\s*\$\s?[\d.,]{3,}")


class CoppelAdapter(ChannelAdapter):
    """Coppel 墨西哥 —— Next.js 站点，商品列表由客户端注入。

    实抓结论（2026-08-10）：
      · 服务端 HTML 里只有推荐位（搜手机返回工作靴、床垫），真实商品要等 JS
      · 站点是 Next.js（页面里有 __NEXT_DATA__ 与 /_next/data/<buildId>/ 接口）
      · 对 requests 直连不响应（超时）—— 必须走浏览器
    所以：等商品容器出现再取 DOM，并优先从 __NEXT_DATA__ 里读结构化数据。
    """
    name = "coppel"
    list_wait_selector = "[class*=cpl-card], [data-testid*=product], [class*=product-card]"

    # ★ Coppel 的价格不带货币符号：
    #     "Precio de contado con descuento 26999 pesos, precio original: 29999 pesos"
    #   通用价格正则要求 $ / MXN 打头，所以 33 个商品里只认出 1 个
    #   （那条恰好写成 "Precio de contado $5,999"）。
    #   这段文案同时给出了现价与原价的精确位置。
    _CPL_SALE = re.compile(
        r"(?:con\s+descuento|de\s+contado)\s*:?\s*\$?\s*([\d.,]{3,})\s*pesos", re.I)
    _CPL_ORIG = re.compile(r"precio\s+original\s*:?\s*\$?\s*([\d.,]{3,})", re.I)

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        items = self._from_cpl_cards(html)
        if items:
            return items
        items = self._from_next_data(html)
        if items:
            return items
        return super().parse_listings(html, text)

    def _from_cpl_cards(self, html: str) -> list[Listing]:
        """按 Coppel 自己的卡片结构解析（class 前缀 cpl-card）。"""
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            return []
        out, seen = [], set()
        for a in soup.select("a[href*='/pdp/']"):
            href = self.absolutize(a.get("href", ""))
            if not href or href in seen:
                continue
            card = a if len(a.get_text(" ", strip=True)) > 20 else \
                (a.find_parent(["div", "li", "article"]) or a)
            blob = card.get_text(" ", strip=True)
            if len(blob) < 15:
                continue

            sale = orig = None
            m = self._CPL_SALE.search(blob)
            if m:
                sale = extract.parse_price(m.group(1), self.currency)
            m2 = self._CPL_ORIG.search(blob)
            if m2:
                orig = extract.parse_price(m2.group(1), self.currency)
            if sale is None:
                # 回退：带货币符号的写法
                cands = [extract.parse_price(p, self.currency)
                         for p in self._PRICE_IN_TEXT.findall(blob)]
                cands = [c for c in cands if c and extract.price_is_sane(c, self.currency)]
                if not cands:
                    continue
                sale = min(cands)
                orig = max(cands) if len(cands) > 1 and max(cands) > sale else None
            if sale is None or not extract.price_is_sane(sale, self.currency):
                continue

            # 标题：URL slug 比卡片文本干净（卡片里混着"Oferta""Precio de contado"等）
            title = (a.get("title") or a.get("aria-label") or "").strip()
            if len(title) < 6:
                title = self._title_from_slug(href) or self._guess_title(blob, "")
            if len(title) < 6:
                continue

            seen.add(href)
            item = Listing(
                title=title[:250], url=href, sale_price=sale,
                list_price=orig if (orig and orig > sale) else None,
                currency=self.currency, source="selector")
            item.specs["_card_text"] = blob[:600]
            self._enrich_from_title(item)
            self.apply_seller(item, "", blob)
            out.append(item)
        return out

    @staticmethod
    def _title_from_slug(url: str) -> str:
        """/pdp/celular-samsung-galaxy-s26-liberado-512-gb-violeta-pm-2275733
        → Celular Samsung Galaxy S26 Liberado 512 Gb Violeta"""
        m = re.search(r"/pdp/([^/?#]+)", url or "")
        if not m:
            return ""
        slug = re.sub(r"-pm-\d+$|-\d{5,}$", "", m.group(1))
        return " ".join(w.capitalize() for w in slug.split("-") if w)[:200]

    def _from_next_data(self, html: str) -> list[Listing]:
        """Next.js 把页面数据完整塞在 __NEXT_DATA__ 里，比解析 DOM 稳得多。"""
        if not html or "__NEXT_DATA__" not in html:
            return []
        m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            return []
        out, seen = [], set()
        for node in _walk_products(data):
            title = str(node.get("name") or node.get("title") or
                        node.get("productName") or "").strip()
            price = (node.get("price") or node.get("salePrice") or
                     node.get("sellingPrice") or node.get("finalPrice"))
            if not title or price is None:
                continue
            sale = extract.parse_price(price, self.currency)
            if sale is None or not extract.price_is_sane(sale, self.currency):
                continue
            key = title.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            url = node.get("url") or node.get("link") or node.get("slug") or ""
            item = Listing(
                title=title[:250], url=self.absolutize(str(url)),
                sale_price=sale,
                list_price=extract.parse_price(
                    node.get("listPrice") or node.get("originalPrice"), self.currency),
                currency=self.currency, source="jsonld",
            )
            if item.list_price and item.list_price <= item.sale_price:
                item.list_price = None
            self._enrich_from_title(item)
            self.apply_seller(item, "", title)
            out.append(item)
        return out


class SearsAdapter(ChannelAdapter):
    """Sears 墨西哥（Grupo Sanborns）。

    ★ 实抓定位到的根因（2026-08-10）：**搜索 URL 路径原本就是错的**。
      /busca/?q=…       → HTTP 200 但只有 13KB 空壳、0 个价格
      /resultados/?q=…  → HTTP 200、744KB、24 处品类词命中  ← 正确路径
    商品名是服务端渲染的，价格由 JS 注入，所以仍需浏览器等待渲染。
    """
    name = "sears"
    list_wait_selector = "[class*=product], [class*=producto], [id*=product]"


class SanbornsAdapter(SearsAdapter):
    """Sanborns 与 Sears 同属 Grupo Sanborns，站点结构相同。

    对 requests 直连返回 403，必须走浏览器（Selenium 实测可通）。
    """
    name = "sanborns"


def _walk_products(obj, depth: int = 0):
    """在任意嵌套结构里找出「像商品」的字典（有名字 + 有价格）。"""
    if depth > 12:
        return
    if isinstance(obj, dict):
        has_name = any(k in obj for k in ("name", "title", "productName"))
        has_price = any(k in obj for k in ("price", "salePrice", "sellingPrice",
                                           "finalPrice", "listPrice"))
        if has_name and has_price:
            yield obj
        for v in obj.values():
            yield from _walk_products(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:400]:
            yield from _walk_products(v, depth + 1)


class ShopeeAdapter(ChannelAdapter):
    """Shopee 巴西 —— 反爬最强的一个，且是纯 SPA（商品由 XHR 注入）。

    ★ 直接标记 force_fallback：不浪费一轮 Playwright 去撞墙，
      第一次就走 Selenium(undetected)，并且要求更长的等待与滚动。
    """
    name = "shopee"
    force_fallback = True
    list_wait_selector = "[data-sqe='item'], .shopee-search-item-result__item"

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        items = super().parse_listings(html, text)
        if items:
            return items
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            return []
        out = []
        for card in soup.select("[data-sqe='item'], li.shopee-search-item-result__item")[:60]:
            link = card.select_one("a[href]")
            blob = card.get_text(" ", strip=True)
            if not link or len(blob) < 10:
                continue
            cands = [extract.parse_price(p, self.currency)
                     for p in self._PRICE_IN_TEXT.findall(blob)]
            cands = [c for c in cands if c and extract.price_is_sane(c, self.currency)]
            if not cands:
                continue
            title = self._guess_title(blob, self._PRICE_IN_TEXT.search(blob).group(0))
            if len(title) < 6:
                continue
            item = Listing(title=title[:250], url=self.absolutize(link["href"]),
                           sale_price=min(cands), currency=self.currency, source="selector")
            self._enrich_from_title(item)
            out.append(item)
        return out


class ClaroCoAdapter(ChannelAdapter):
    """Claro 哥伦比亚 —— 走站点自带的无鉴权 JSON 接口，全程不用浏览器。

    实测（2026-08-11）根因：
      配置里的 /catalogsearch/result/?q= 是 **Magento 路径，此站根本没有** ——
      站点是 Next.js SPA，把它渲染成西语 404 页（正文 158 字符），
      而 <title> 是全站写死的静态标题。所以"标题正确 + 0 价格"看起来像
      JS 没渲染完，实际是走进死路径，连一次搜索请求都没发出去。

    DOM 路线走不通（已实测，别再试）：
      商品卡片里**没有 a[href]**（"Ver producto" 是 JS 路由按钮），
      通用解析器靠遍历 <a> 起步，直接空转 0 条；
      品类页勉强解出 4/24 且含标题/价格/URL 三者错配的假阳性。

    所以走接口：裸 requests、无 UA、无 Cookie、无 token → 200。
    """
    name = "claro_co"

    API = ("https://tienda.claro.com.co/api/0.1/catalog/product"
           "/searchterm/{q}?pageSize={n}&pageNumber=1")
    CATEGORY_IDS = {
        "phone": "3074457345616680171", "tablet": "3074457345617696747",
        "wearable": "3074457345617696689", "pc": "3074457345617696674",
        "audio": "3074457345616679668",
    }
    CAT_API = ("https://tienda.claro.com.co/api/0.1/catalog/product"
               "/category/{cid}?limit={n}&offset=0")

    def collect(self, engine, query: str, limit: int = 20):
        """collector 看到 adapter 有 collect() 就走这条（与 MeliAdapter 同一机制）。"""
        import httpx
        url = self.API.format(q=quote(query), n=min(limit * 2, 48))
        try:
            r = httpx.get(url, timeout=40)
            if r.status_code != 200:
                return [], f"failed:api_{r.status_code}"
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("[claro_co] 接口失败 %s: %s", url[:80], str(e)[:100])
            return [], "failed:api"

        out = []
        for it in (data.get("contents") or [])[:limit]:
            lst = self._from_api(it)
            if lst:
                out.append(lst)
        return out, ("ok:api" if out else "empty:api")

    def _from_api(self, it: dict) -> Listing | None:
        title = str(it.get("name") or "").strip()
        if not title:
            return None
        # ★ 价格在 price[] 数组里按 usage 取，不能取 min：
        #   usage=="Offer" 是现价；usage=="Display" 是划线原价，
        #   但实测 26 条 Display < Offer（页面上根本不显示），
        #   取 min 会系统性把价格压低约 10%。
        offer = disp = None
        for p in it.get("price") or []:
            try:
                v = float(p.get("value") or 0)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
            if p.get("usage") == "Offer":
                offer = v
            elif p.get("usage") == "Display":
                disp = v
        if offer is None:
            return None

        href = ((it.get("seo") or {}).get("href") or "")
        url = f"https://tienda.claro.com.co/detalle-producto{href}" if href else ""
        lst = Listing(
            title=title[:250], url=url, sale_price=offer,
            list_price=disp if (disp and disp > offer) else None,
            currency=self.currency, source="api",
            brand_guess=it.get("manufacturer") or None,
            detail_fetched=True,          # 接口字段已完整，不必再进详情页
        )
        lst.specs["partNumber"] = it.get("partNumber")
        self._enrich_from_title(lst)
        lst.seller_kind, lst.seller_type = "self_operated", "official"
        lst.seller_reason = "运营商官方商城直营"
        return lst


class AlkostoAdapter(ChannelAdapter):
    """Alkosto 哥伦比亚 —— 商品网格是前端 Algolia 渲染的，HTML 里没有货。

    实测（2026-08-11，两名工程师独立复现）根因：
      站点确实是 SAP Commerce/Hybris（页面里有 ACC.config、/_ui/responsive/theme-alkosto/），
      但**商品列表不走 OCC REST**，而是前端 Algolia InstantSearch 渲染 ——
      服务端 HTML 里只预渲染了 4 个商品，所以通用解析器只能抓到 4/80。
      页面 JS 的 ACC.config.algolia 里明文内嵌着 appId / indexName /
      **search-only** key（这是给浏览器用的公开只读键，不是密钥）。

    两个会在生产里咬人的坑（复验者实测）：
      ① **不打折时 lowestprice == pricevalue**（iPhone 15 Pro Max 两个都是 6,799,010），
         不能假设"原价一定大于现价"，否则会算出 0% 甚至负的折扣；
         percentagediscount_string 除了 None 还会是空字符串 ""。
      ② **facet 写错时是 HTTP 200 + nbHits=0 静默空返回**，不报错。
         所以必须显式断言 nbHits，否则"配置写错"会伪装成"这个品类没货"。
    """
    name = "alkosto"
    wants_category = True   # 品类码当 Algolia facet 用

    APP_ID = "QX5IPS1B1Q"
    # search-only key：Alkosto 自己发给每个访客浏览器的公开只读键。
    # 不是凭据、不能写、不涉及任何账号 —— 放代码里与放页面里等价。
    SEARCH_KEY = "7a8800d62203ee3a9ff1cdf74f99b268"
    INDEX = "alkostoIndexAlgoliaPRD"
    HOST = "https://qx5ips1b1q-dsn.algolia.net"
    BASE = "https://www.alkosto.com"

    FIELDS = ["name_text_es", "lowestprice_double", "pricevalue_cop_double",
              "percentagediscount_string", "url_es_string", "marca_text",
              "code_string", "stocklevelstatus_string"]

    def collect(self, engine, query: str, limit: int = 20, category: str | None = None):
        import httpx

        params = {
            "hitsPerPage": str(max(1, min(int(limit) * 3, 300))),
            "page": "0",
            "attributesToRetrieve": json.dumps(self.FIELDS),
        }
        # 品类页：从配置的 category_urls 里取尾部的品类码（BI_101_ALKOS），
        # 直接当 facet 用 —— 不需要父路径，也不用维护第二份映射表
        facet_code = self._facet_code(category)
        if facet_code:
            params["query"] = ""
            params["facetFilters"] = json.dumps([[f"allcategories_string_mv:{facet_code}"]])
        else:
            params["query"] = query

        url = f"{self.HOST}/1/indexes/{self.INDEX}"
        try:
            r = httpx.get(url, params=params, timeout=40, headers={
                "X-Algolia-Application-Id": self.APP_ID,
                "X-Algolia-API-Key": self.SEARCH_KEY,
            })
            if r.status_code != 200:
                return [], f"failed:api_{r.status_code}"
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("[alkosto] Algolia 失败 %s: %s", url, str(e)[:100])
            return [], "failed:api"

        hits = data.get("hits") or []
        if not hits:
            # ★ nbHits=0 既可能是"真没这个商品"，也可能是"facet 码写错了"。
            #   两者都返回 200，区别只在这里能不能看出来 —— 把 facet 记进日志，
            #   否则配置写错会被当成"该品类没货"永远查不出来。
            log.info("[alkosto] 空结果 nbHits=%s facet=%s query=%r",
                     data.get("nbHits"), facet_code, query)
            return [], "empty:api"

        out = []
        for it in hits[:limit]:
            lst = self._from_hit(it)
            if lst:
                out.append(lst)
        return out, ("ok:api" if out else "empty:api")

    def _facet_code(self, category: str | None) -> str | None:
        """从 category_urls 配置里取品类码。URL 长 .../c/BI_101_ALKOS"""
        if not category:
            return None
        cat_urls = self.channel.get("category_urls")
        try:
            mapping = json.loads(cat_urls) if isinstance(cat_urls, str) else (cat_urls or {})
        except Exception:  # noqa: BLE001
            return None
        u = (mapping or {}).get(category) or ""
        m = re.search(r"/c/([A-Za-z0-9_]+)", u)
        return m.group(1) if m else None

    def _from_hit(self, it: dict) -> Listing | None:
        title = str(it.get("name_text_es") or "").strip()
        if not title:
            return None

        def _num(v):
            try:
                f = float(v)
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        sale = _num(it.get("lowestprice_double"))
        listp = _num(it.get("pricevalue_cop_double"))
        if sale is None:
            return None
        # ★ 不打折时两个字段相等 —— 此时不能当"划线原价"落库，
        #   否则折扣算出来是 0%，与"没有划线价"混为一谈
        if listp is not None and listp <= sale:
            listp = None

        href = str(it.get("url_es_string") or "")
        lst = Listing(
            title=title[:250],
            url=(self.BASE + href) if href.startswith("/") else href,
            sale_price=sale, list_price=listp,
            currency=self.currency, source="api",
            brand_guess=it.get("marca_text") or None,
            sku_code=str(it.get("code_string") or "") or None,
            is_in_stock=(str(it.get("stocklevelstatus_string") or "").lower() != "outofstock"),
            detail_fetched=True,
        )
        self._enrich_from_title(lst)
        # Alkosto 是自营零售商，Algolia 索引里没有第三方卖家
        lst.seller_kind, lst.seller_type = "self_operated", "official"
        lst.seller_reason = "Alkosto 自营（站内索引无第三方卖家）"
        return lst


class VtexAdapter(ChannelAdapter):
    """VTEX 平台通用适配器（Fast Shop 巴西在用）。

    实测（2026-08-11）根因：Fast Shop 迁到了 VTEX FastStore
    （HTML 里 window.VTEX_METADATA = {account:'fastshopbr', renderer:'faststore'}）。
    site.fastshop.com.br 完全可达、HTTP 200、**没有任何人机验证** ——
    之前解析为空是因为搜索页是纯客户端渲染：402KB 的 HTML 里 "R$" 出现 0 次。
    （老域名 www.fastshop.com.br 才是那个 Akamai 403 死域名，已弃用。）

    ★ 复验者实测出的三个坑，每个都会污染价格库：
      ① `hideUnavailableItems=true` **不生效** —— 第 2 页几乎全是 Price=0 的僵尸 SKU。
         必须客户端强制过滤 Price>0，否则零价数据会把价格基线整体拽向 0。
      ② `commertialOffer.IsAvailable` 这个字段**根本不存在**，判有货要用 AvailableQuantity。
      ③ `recordsFiltered` 被截顶在 10000，不是真实总数，不能拿来当总量。
    """
    name = "vtex"

    def collect(self, engine, query: str, limit: int = 20, category: str | None = None):
        import httpx

        base = (self.channel.get("base_url") or "").rstrip("/")
        if not base:
            return [], "failed:no_url"
        url = f"{base}/api/io/_v/api/intelligent-search/product_search/"
        try:
            r = httpx.get(url, timeout=40, params={
                "query": query, "count": str(max(1, min(int(limit) * 2, 50))),
                "page": "1", "locale": self.channel.get("locale") or "pt-BR",
            }, headers={"Accept": "application/json"})
            if r.status_code != 200:
                return [], f"failed:api_{r.status_code}"
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("[vtex] 接口失败 %s: %s", url, str(e)[:100])
            return [], "failed:api"

        out, zero_price = [], 0
        for p in (data.get("products") or []):
            lst = self._from_product(p, base)
            if lst is None:
                zero_price += 1
                continue
            out.append(lst)
            if len(out) >= limit:
                break
        if zero_price:
            log.info("[vtex] 过滤掉 %d 个零价/无货 SKU（接口的 hideUnavailableItems 不生效）",
                     zero_price)
        return out, ("ok:api" if out else "empty:api")

    def _from_product(self, p: dict, base: str) -> Listing | None:
        title = str(p.get("productName") or "").strip()
        if not title:
            return None
        offer, avail = None, 0
        for item in (p.get("items") or []):
            for s in (item.get("sellers") or []):
                co = s.get("commertialOffer") or {}
                try:
                    price = float(co.get("Price") or 0)
                except (TypeError, ValueError):
                    continue
                # ★ 零价僵尸 SKU 必须挡在这里
                if price <= 0:
                    continue
                try:
                    qty = int(co.get("AvailableQuantity") or 0)
                except (TypeError, ValueError):
                    qty = 0
                if offer is None or price < offer[0]:
                    offer, avail = (price, co), qty
        if offer is None:
            return None

        price, co = offer
        try:
            listp = float(co.get("ListPrice") or 0) or None
        except (TypeError, ValueError):
            listp = None
        if listp is not None and listp <= price:
            listp = None

        link = str(p.get("link") or p.get("linkText") or "")
        if link and not link.startswith("http"):
            link = f"{base}/{link.lstrip('/')}"
        lst = Listing(
            title=title[:250], url=link, sale_price=price, list_price=listp,
            currency=self.currency, source="api",
            brand_guess=p.get("brand") or None,
            sku_code=str(p.get("productReference") or "") or None,
            is_in_stock=avail > 0, detail_fetched=True,
        )
        self._enrich_from_title(lst)
        lst.seller_kind, lst.seller_type = "self_operated", "official"
        lst.seller_reason = "VTEX 自营店（默认卖家）"
        return lst


def _raw_num(v) -> float | None:
    """接口里的原始数值字段（"999990.000000"）直读成 float。

    ★ 不能拿 extract.parse_price 去解这种值：那个函数是给**页面显示串**用的，
      会按国家猜千分位/小数点（CLP 无小数位 ⇒ 点一律当千分位）。
      机器数一旦被它解析，999990.000000 会变成 999,990,000,000。
    """
    if v is None:
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


class EntelClAdapter(ChannelAdapter):
    """Entel 智利 —— 网店在子域 miportal.entel.cl，数据在内嵌 JSON 里。

    实测（2026-08-11）根因：
      **配的是错的域名。** www.entel.cl 在 Cloudflare 后面，只有资费/客服内容，
      整个 /tienda/* 路径不存在 —— 任何参数名都是 404。
      真正的电商站是 miportal.entel.cl（Oracle Commerce/Endeca），
      站内搜索参数是 Endeca 的 Ntt。

    ★ 两个解析陷阱（都实测踩过）：
      1. 数据在 <var id="rootProductInfo_JSON"> 里，但**不能用 BeautifulSoup 取** ——
         BS4 会把 JSON 字符串里被转义的 <script> 当成真标签解析，
         内容直接破碎（get_text 只剩 342 字符 / 0 个 attributes 块，
         而原始 HTML 里有 25 个）。必须对**原始 html 字符串**正则+括号配对。
      2. 整块 json.loads 在 Selenium page_source 上必然失败（浏览器重排过 footer
         那段），但坏的只是那一段 —— 所以逐块解析、坏块跳过，实测 bad=0。
    """
    name = "entel_cl"
    list_wait_selector = None      # 商品网格永远不渲染，等选择器只会白等超时

    _ATTR = re.compile(r'"attributes"\s*:\s*\{')

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        out = []
        for blk in self._iter_attr_blocks(html or ""):
            lst = self._from_block(blk)
            if lst:
                out.append(lst)
        # ★ 内嵌 JSON 抠不到时**宁可报空，也绝不回落通用解析器**。
        #   实测通用解析器在这个站上产出 40 条垃圾：
        #     标题 = 页面顶部的营销话术（"Cobertura satelital Este equipo es
        #            compatible con SMS satelitales…"，还被切得缺头少尾）
        #     价格 = **24 期月供**（10,833 CLP ≈ US$11 的"Galaxy"），
        #            而 JSON 里同款的真实价是 999,990 CLP —— 差 92 倍
        #   这些数进了库不会报错，只会把智利的价格基线整体拽到地板上，
        #   而界面上看这个渠道是"健康 40 条"。
        #   本项目的原则：抓不到你知道没数据，抓错了你以为有数据 —— 后者危险得多。
        if not out:
            log.info("[entel_cl] 内嵌 JSON 未命中，报空而不回落通用解析"
                     "（该站通用解析会把营销话术+月供当成商品）")
        return out

    def _iter_attr_blocks(self, html: str):
        """在原始 HTML 里逐个抠出 "attributes":{...} 并单独解析。"""
        for m in self._ATTR.finditer(html):
            start = m.end() - 1              # 指向 '{'
            depth, i, in_str, esc = 0, start, False, False
            while i < len(html):
                c = html[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            try:
                yield json.loads(html[start:i + 1])
            except Exception:  # noqa: BLE001
                continue          # 坏块跳过 —— footer 那段浏览器重排过

    @staticmethod
    def _one(v):
        """Endeca 每个字段都是单元素列表，拆出来。"""
        if isinstance(v, list):
            return v[0] if v else None
        return v

    def _from_block(self, a: dict) -> Listing | None:
        title = str(self._one(a.get("displayName")) or "").strip()
        if not title:
            return None
        # ★ 接口给的是**机器数**（"999990.000000"），不能过按国家猜格式的
        #   显示串解析器 —— CLP 无小数位，那个解析器把点当千分位，
        #   999990.000000 被读成 999,990,000,000，price_is_sane 全部否掉 ⇒
        #   `_from_block` 一条都返不出来 ⇒ 整个渠道回落通用解析器，
        #   于是营销话术和 24 期月供进了库，而界面显示"健康 40 条"。
        #   规则：**原始数值字段按 float 直读，只有显示串才走本地化解析。**
        sale = _raw_num(self._one(a.get("listPrice")))
        if sale is None:
            sale = extract.parse_price(self._one(a.get("price.formatted")),
                                       self.currency)
        if sale is None or not extract.price_is_sane(sale, self.currency):
            return None
        # ★ ATG 命名很坑：listPrice 是**现价**，referencePrice 才是划线原价
        ref = (_raw_num(self._one(a.get("referencePrice")))
               or extract.parse_price(self._one(a.get("referencePriceFormatted")),
                                      self.currency))

        seo = str(self._one(a.get("seoUrl")) or "")
        lst = Listing(
            title=title[:250],
            url=self.absolutize(seo),
            sale_price=sale,
            list_price=ref if (ref and ref > sale) else None,
            currency=self.currency, source="jsonld",
            brand_guess=self._one(a.get("brand")),
            is_in_stock=str(self._one(a.get("inventoryStatus")) or
                            "IN_STOCK").upper() != "OUT_OF_STOCK",
            detail_fetched=True,      # 列表页字段已完整；PDP 是 JS 渲染的
        )
        rom = self._one(a.get("internal"))
        if rom:
            lst.specs["rom_raw"] = rom
        self._enrich_from_title(lst)
        lst.seller_kind, lst.seller_type = "self_operated", "official"
        lst.seller_reason = "运营商官方商城直营"
        return lst


class BrandStoreAdapter(ChannelAdapter):
    """品牌官方商城（Samsung / Apple / 其它品牌官网）。

    共同特征：卖家恒为官方（不需要判卖家类型），且规格页结构规整。
    """
    name = "brand_store"

    def parse_detail(self, html: str, text: str, lst: Listing) -> Listing:
        lst = super().parse_detail(html, text, lst)
        lst.seller_kind = "self_operated"
        lst.seller_type = "official"
        lst.seller_reason = "品牌官方商城直营，卖家恒为官方"
        return lst


class SamsungAdapter(BrandStoreAdapter):
    name = "samsung"
    list_wait_selector = "[class*=product-card], [class*=pd-g-product]"


# Apple 的价格文案（实抓 2026-08-10）：
#   "Desde $28,499 de contado o 18 MSI desde $1,583.28"
#   ★ `de contado`（一次性付清）前面的是整机价；
#     `MSI desde`（免息月供）后面的是**每月**要付的钱。
#   两个数字都是合法价格串，PRICE_SANITY 的 MXN 下界 300 挡不住 1583 ——
#   不按文案区分就会把月供当成整机价入库，把 iPhone 记成 1583 比索。
_APPLE_CASH = re.compile(
    r"(?:desde\s*)?\$\s*([\d.,]+)\s*(?:de\s+contado|al\s+contado)", re.I)
_APPLE_MSI = re.compile(
    r"(\d{1,2})\s*(?:MSI|meses sin intereses|pagos)\s*(?:de|desde)?\s*\$\s*([\d.,]+)", re.I)


class AppleAdapter(BrandStoreAdapter):
    """Apple 官网 —— 价格与分期月供混在同一句文案里。

    实抓（2026-08-10）：品类页 0 个 JSON-LD，可见文本 23 个价格串，
    其中混着 24 期分期月供 $1,583.28。文案格式固定：
        "Desde $28,499 de contado o 18 MSI desde $1,583.28"
    """
    name = "apple"
    list_wait_selector = "[class*=rf-serp-product], [class*=as-producttile], [class*=rf-hcard]"

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        items = self._from_apple_cards(html)
        if not items:
            items = super().parse_listings(html, text)
        for it in items:
            blob = it.specs.get("_card_text", "") or ""
            self._fix_installment_price(it, blob)
            if re.search(r"\b(desde|a partir de|from)\b", blob[:400], re.I):
                it.specs.setdefault("_price_note", "官网起价（非具体SKU，为该系列最低配）")
        return items

    def _from_apple_cards(self, html: str) -> list[Listing]:
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            return []
        out, seen = [], set()
        for card in soup.select("[class*=rf-hcard-content], [class*=rf-recommcard-content],"
                                "[class*=as-producttile], [class*=rf-serp-product]")[:80]:
            blob = card.get_text(" ", strip=True)
            if len(blob) < 12:
                continue
            title_el = card.select_one("h2, h3, [class*=title], [class*=name]")
            title = (title_el.get_text(" ", strip=True) if title_el else "")[:250]
            if len(title) < 4:
                continue
            key = title.lower()[:70]
            if key in seen:
                continue
            price = self._cash_price(blob)
            if price is None:
                continue
            seen.add(key)
            link = card.select_one("a[href]")
            item = Listing(
                title=title, url=self.absolutize(link["href"]) if link else "",
                sale_price=price, currency=self.currency, source="selector")
            item.specs["_card_text"] = blob[:600]
            self._enrich_from_title(item)
            item.seller_kind, item.seller_type = "self_operated", "official"
            item.seller_reason = "Apple 官网直营"
            out.append(item)
        return out

    def _cash_price(self, blob: str) -> float | None:
        """只取 `de contado` 的那个价，绝不取月供。"""
        m = _APPLE_CASH.search(blob)
        if m:
            v = extract.parse_price(m.group(1), self.currency)
            if v and extract.price_is_sane(v, self.currency):
                return v
        # 没有 "de contado" 文案时：取所有价格里**最大**的那个。
        # 同一张卡片里，整机价必然大于月供（月供 = 整机价 / 期数）。
        cands = [extract.parse_price(p, self.currency)
                 for p in self._PRICE_IN_TEXT.findall(blob)]
        cands = [c for c in cands if c and extract.price_is_sane(c, self.currency)]
        return max(cands) if cands else None

    def _fix_installment_price(self, it: Listing, blob: str) -> None:
        """已经取到价的，再核对一遍是不是把月供当成了整机价。"""
        m = _APPLE_MSI.search(blob)
        if not m:
            return
        months = int(m.group(1))
        monthly = extract.parse_price(m.group(2), self.currency)
        it.installments = f"{months}期免息×{m.group(2)}"[:60]
        if not monthly or not it.sale_price:
            return
        if abs(it.sale_price - monthly) < 0.01:
            cash = self._cash_price(blob)
            if cash and cash > monthly:
                it.specs["_corrected_from_installment"] = (
                    f"原取到 {monthly}（这是{months}期月供），已改用整机价 {cash}")
                it.sale_price = cash
            else:
                # 只有月供、拿不到整机价 —— 用月供×期数只是估算，不能当挂牌价。
                # 宁可丢这条也不入一个错的价：错价会污染基线且看不出来。
                it.sale_price = None
                it.specs["_dropped"] = f"只解析到{months}期月供 {monthly}，无整机价"


# ---------------------------------------------------------------- Acme官方商城

# ★ 商品数据全在页面内嵌的 JSON 里，**不要去解析 DOM**。
#   实测（2026-08-14）通用解析器在这四个站上取到的是**分期月供**：
#     智利 Astra X7 记成 183,333 CLP，而 183,333 × 12 = 2,199,990 才是真价；
#     秘鲁、墨西哥同样中招（Slate Tab 记成 583~866 MXN，约 $30）。
#   页面上月供数字比总价更醒目，靠 DOM 选择器几乎必然抓错。
#   而内嵌 JSON 里两者是**分开的字段**，一取即准：
#     "installmentAmount":"183333.00", "num":12,
#     "totalAmount":"2199990.00"
_HW_CONTAINERS = ("productList", "flashSaleProductList", "singleProductList",
                  "tabProductList")


def _hw_arrays(raw: str, key: str):
    """把 "key":[ ... ] 整段按括号配平取出来。

    ★ 必须配平，不能用正则截断：数组里嵌着对象、对象里还嵌着数组
      （hotSelling / cornerGroup / installmentInfos），
      正则的非贪婪匹配会在第一个 ] 处切断，得到半截 JSON 直接解析失败。
    ★ 要跳过字符串里的括号与转义，否则商品名里一个 "]" 就把边界算错。
    """
    for m in re.finditer(rf'"{key}"\s*:\s*\[', raw):
        i = m.end() - 1
        depth, j, instr, esc = 0, i, False, False
        while j < len(raw):
            ch = raw[j]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    yield raw[i:j + 1]
                    break
            j += 1


# Acme商城上的配件词（西/葡语）。只在品牌商城这个上下文里用 ——
# 在综合电商上「funda」「cargador」也可能是别家的货。
_HW_ACCESSORY = re.compile(
    r"\b(funda|cover|case|correa|strap|cargador|charger|adaptador|cable|"
    r"protector|mica|teclado|keyboard|m-pen|stylus|soporte|base|"
    r"router|mesh|wifi)\b", re.I)

# 套装：标题里用 + 连接两个产品名。
# ★ 要求 + 两侧都有字母，避免命中 "8+256G" 这种容量写法。
_HW_BUNDLE = re.compile(r"[A-Za-z]{3,}[^+]{0,24}\+\s*[A-Za-z]{3,}")


def _hw_num(*vals) -> float | None:
    """接口原始数字直读。按给定顺序取第一个能转成正数的。

    ★ 这里**故意不做任何千分位/小数点的本地化处理** ——
      JSON 字段里的 "9999900.00" 就是九百九十九万九千九百点零零，
      与页面上显示的 "$9.999.900" 是两套完全不同的写法。
      拿显示口径的解析器去读接口数字，在 COP/CLP 上会放大 100 倍。
    """
    for v in vals:
        if v is None or v == "":
            continue
        try:
            f = float(str(v).strip())
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


class AcmeStoreAdapter(BrandStoreAdapter):
    """Acme自营商城 consumer.acme.com/{cc}/offer/ —— 我方官方定价基准。

    用户口径（2026-08-11）：「商城的价格就是官方定价」，
    所以我方价不靠手工录入 my_pricing，直接从这里抓。

    ★ 价格字段口径（四国实测一致，2026-08-14）：
        salePrice / unitPrice / currentPrice   = 现价（成交价）
        specialPrice / streetPrice / orderPrice = 原价（划线价）
        savePrice                              = 折扣额
      交叉验证：MX Vega 90s Pro Max 现价 22,999 + 省 10,000 = 原价 32,999 ✓

    ★ 品类**必须从型号名推断**，不能用采集时的品类：
      四个品类的 category_urls 全指向同一个 /offer/ 页面，
      一页上手机、平板、手表、耳机、路由器混在一起。
      沿用采集上下文会把整页都记成 phone（项目里既有教训：
      「品类来自产品线名比采集上下文可靠」）。
    """
    name = "acme_store"
    # 页面是服务端渲染的，JSON 直接在 HTML 里，不必等前端
    list_wait_selector = None

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        raw = _html_mod.unescape(html or "")
        out: list[Listing] = []
        seen: set = set()
        for key in _HW_CONTAINERS:
            for arr in _hw_arrays(raw, key):
                try:
                    items = json.loads(arr)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(items, list):
                    continue
                for it in items:
                    lst = self._from_json(it, seen)
                    if lst is not None:
                        out.append(lst)
        log.info("[acme_store] 内嵌 JSON 抽出 %d 个商品", len(out))
        return out

    def _from_json(self, it, seen: set) -> Listing | None:
        if not isinstance(it, dict):
            return None
        title = str(it.get("productTitle") or it.get("skuName") or "").strip()
        if not title:
            return None
        sku = str(it.get("skuCode") or it.get("sbomCode") or "").strip()
        key = (title, sku)
        if key in seen:
            return None
        seen.add(key)

        # ★★ JSON 里的数字**不能过本地化显示解析器**。
        #   实测：CO 的 streetPrice 是字符串 "9999900.00"，而 COP/CLP 的显示
        #   格式用 "." 当千分位 —— 显示解析器把它读成 999,990,000，大了 100 倍。
        #   （salePrice 恰好是 JSON 数字 9999900 所以没中招，
        #     于是现价对、原价错，图上会显示"打了 99% 折"。）
        #   这与 Entel 智利那次是同一个坑：接口原始数字 vs 页面显示文本
        #   是两套口径，前者一律直读 float。
        sale = _hw_num(it.get("salePrice"), it.get("unitPrice"), it.get("currentPrice"))
        listp = _hw_num(it.get("streetPrice"), it.get("specialPrice"), it.get("orderPrice"))
        # ★ 不打折时原价 == 现价，此时**不要**填 list_price，
        #   否则下游会算出 0% 折扣并当成"有促销"（Alkosto 那次踩过）。
        if listp is not None and sale is not None and listp <= sale:
            listp = None

        url = str(it.get("linkUrl") or "").strip()
        if url and url.startswith("/"):
            base = (self.channel.get("base_url") or "").rstrip("/")
            url = base + url

        lst = Listing(
            title=title[:250], url=url, sale_price=sale, list_price=listp,
            currency=self.currency, sku_code=sku or None,
            source="embedded_json",
            seller_name="ACME", seller_type="official",
            seller_kind="brand_official",
            seller_reason="Acme自营商城，卖家恒为品牌官方",
            is_in_stock=not bool(it.get("disabled")),
        )
        self._enrich_from_title(lst)

        # ★★ 品牌自营商城上架的**就是整机或配件**，不存在第三方杂货。
        #   通用的 detect_product_kind 认不出Acme的型号名（Astra X7、Vega 80 Ultra
        #   这些都被判成 unknown，实测 211 条里 137 条 unknown），
        #   而 product_kind='device' 是"我方官方价"的闸门 ——
        #   等于把自家旗舰机全挡在门外。
        #   在这个渠道上，判据换成：能不能推断出一个整机品类。
        if lst.product_kind == "unknown":
            # ★ 判据不是"能不能认出型号"，而是"是不是配件"。
            #   试过先用 skunorm.guess_category 认型号，81 个仍判不出来 ——
            #   Vega 90s Pro Max、Astra X7、WATCH Apex、Band 11 这些
            #   产品线名它都不认识，而它们全都是货真价实的整机。
            #   与其去穷举Acme的产品线（每代新品都要维护一次），
            #   不如用这个渠道本身的性质：**品牌自营商城不卖别人的东西**，
            #   所以"不是配件"就是整机，没有第三种可能。
            #   反过来做（认出来才算整机）会漏掉每一款新机 —— 而新机恰恰最该看。
            if _HW_ACCESSORY.search(lst.title):
                lst.product_kind = "accessory"
                lst.specs["_kind_reason"] = "品牌商城 + 标题含配件词"
            else:
                lst.product_kind = "device"
                lst.specs["_kind_reason"] = "品牌自营商城只卖自家货，非配件即整机"

        # ★ 捆绑包要标出来：Acme商城常卖 "nimbus 15 Max + SonicBuds Pro 5" 这类套装，
        #   它的价格既不是手机价也不是耳机价。不标的话会污染价格带 ——
        #   实测秘鲁的 SonicBuds Pro 5 因此显示成 $506，而单卖只要 $186。
        if _HW_BUNDLE.search(lst.title):
            lst.is_bundle = True
            lst.specs["_bundle_reason"] = "标题里用 + 连接了两个产品名，属套装价"

        # 分期信息留痕，但**绝不当价格**
        n = it.get("installmentNum") or it.get("num")
        amt = it.get("installmentAmount")
        if n and amt:
            lst.installments = f"{n}期×{amt}"
        return lst

    def parse_detail(self, html: str, text: str, lst: Listing) -> Listing:
        # 列表页的 JSON 已经给全了价格与卖家，不需要再进详情页
        lst.detail_fetched = True
        return lst
