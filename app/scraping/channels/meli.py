# -*- coding: utf-8 -*-
"""MercadoLibre 适配器 —— **只走官方 API**。

★★ 2026-08-13 起取消了网页通道。原因：
  mercadolibre.com.mx/.com.br/.com.co/.cl/.com.pe/.com.ar 的 robots.txt
  把 ClaudeBot / Claude-User 与 GPTBot、PerplexityBot、Amazonbot 并列在
  同一组下面，共用一条 `Disallow: /`（全站禁止）：

      User-agent: Amazonbot
      User-agent: PerplexityBot
      User-agent: ClaudeBot        ← 我们
      User-agent: Claude-User      ← 我们
      User-agent: GPTBot
      Disallow: /

  www 与 listado 两个主机都禁。站方点名说了不 ——
  再往网页降级就等于把自己伪装成别的东西，本项目不做。

  ★ 官方 API（api.mercadolibre.com）**不受此限**：那是站方主动提供的取数方式。
    所以这个渠道现在的状态是"需要凭据"，不是"抓不到"。
    用户配好 meli_access_token 就自然恢复，六国一起。

  ★ 这条禁令被我们漏了很久：第一版 tools/check_robots.py 用正则找
    "具名段里有没有 Disallow: /"，而这种**多个 User-agent 叠在一起共用一段规则**
    的写法会被看成"空段"从而放行。现已改用 urllib.robotparser 按
    「这个 UA 能不能取这个 URL」判定。

★ 另一条实网事实（与上面无关，仍然成立）：
  ML 对**未登录访客**的搜索页/商品页强制 302 到 /gz/account-verification。
  headless、有头模式全部撞墙 —— 即便没有 robots 这一条，游客网页通道也走不通。
"""
from __future__ import annotations

import logging

import httpx

from .. import extract
from .. import seller as seller_mod
from ..browser import UA
from .base import ChannelAdapter, Listing

log = logging.getLogger("meli")

API_BASE = "https://api.mercadolibre.com"


class MeliAdapter(ChannelAdapter):
    name = "meli"

    def __init__(self, channel: dict, country: dict, access_token: str = ""):
        super().__init__(channel, country)
        self.access_token = (access_token or "").strip()
        self.site_id = country.get("meli_site") or ""
        self.last_channel_used = ""

    # ------------------------------------------------ 通道① 官方 API

    def api_available(self) -> bool:
        return bool(self.access_token and self.site_id)

    def search_api(self, query: str, limit: int = 20) -> tuple[list[Listing], str]:
        """返回 (结果, 状态)。状态: ok / empty / unauthorized / failed"""
        if not self.api_available():
            return [], "no_token"
        url = f"{API_BASE}/sites/{self.site_id}/search"
        try:
            r = httpx.get(url, params={"q": query, "limit": min(limit, 50)},
                          headers={"Authorization": f"Bearer {self.access_token}",
                                   "User-Agent": UA},
                          timeout=30, follow_redirects=True)
        except Exception as e:  # noqa: BLE001
            log.warning("[meli-api] 请求失败 %s: %s", query, str(e)[:120])
            return [], "failed"

        if r.status_code in (401, 403):
            # ★★ 这里**不能**降级到网页通道。
            #   mercadolibre 的 robots.txt 把 ClaudeBot / Claude-User 和
            #   GPTBot、PerplexityBot 并列在同一组下面，共用一条 `Disallow: /`：
            #       User-agent: ClaudeBot
            #       User-agent: Claude-User
            #       ...
            #       Disallow: /
            #   （这种"多个 User-agent 叠在一起共用一段规则"的写法，
            #     用正则找"具名段里有没有 Disallow"会看成空段而放行 ——
            #     我们的第一版检查工具就是这么漏掉它的，见 tools/check_robots.py）
            #   站方点名说了不，官方 API 才是它许可的取数方式。
            #   token 无效就如实报"没有可用的取数通道"，不要绕到网页去。
            log.warning("[meli-api] token 无效或过期(HTTP %s)。"
                        "MercadoLibre 网页通道被 robots.txt 具名禁止，"
                        "**不降级**——请更新 meli_access_token。", r.status_code)
            return [], "unauthorized_no_fallback"
        if r.status_code != 200:
            log.warning("[meli-api] HTTP %s", r.status_code)
            return [], "failed"

        try:
            results = (r.json() or {}).get("results") or []
        except Exception:  # noqa: BLE001
            return [], "failed"

        out = []
        for it in results[:limit]:
            lst = self._from_api_item(it)
            if lst.is_usable():
                out.append(lst)
        self.last_channel_used = "api"
        return out, ("ok" if out else "empty")

    def _from_api_item(self, it: dict) -> Listing:
        cur = it.get("currency_id") or self.currency
        sale = extract.parse_price(it.get("price"), cur)
        original = extract.parse_price(it.get("original_price"), cur)

        seller = it.get("seller") or {}
        seller_name = (seller.get("nickname") or seller.get("seller_reputation", {})
                       .get("nickname") or "") or None
        # ★ 官方店在 API 里有明确字段 —— 这是全系统最硬的卖家证据，
        #   比任何网页猜测都准。用户要求分清 MELI 官方店与非官方，靠的就是它。
        #   MELI 自己不卖货，所以只有 brand_official / third_party 两类，没有自营。
        store_id = it.get("official_store_id")
        if store_id:
            kind = seller_mod.BRAND_OFFICIAL
            reason = (f"官方API：official_store_id={store_id}"
                      f"{'，店铺名「' + seller_name + '」' if seller_name else ''}"
                      f" → 品牌官方店")
        elif seller_name:
            kind = seller_mod.THIRD_PARTY
            reason = f"官方API：卖家「{seller_name}」无 official_store_id → 第三方卖家"
        else:
            kind = seller_mod.UNKNOWN
            reason = "官方API未返回卖家信息"
        seller_type = seller_mod._COARSE.get(kind, "unknown")

        inst = it.get("installments") or {}
        inst_str = None
        if inst.get("quantity"):
            free = "免息" if (inst.get("rate") == 0) else ""
            inst_str = f"{inst['quantity']}期×{inst.get('amount', '')}{free}"[:60]

        attrs = {a.get("id"): a.get("value_name")
                 for a in (it.get("attributes") or []) if a.get("id")}

        lst = Listing(
            title=str(it.get("title") or "")[:250],
            url=it.get("permalink") or "",
            sale_price=sale,
            list_price=original if (original and sale and original > sale) else None,
            currency=cur,
            seller_name=seller_name,
            seller_type=seller_type,
            seller_kind=kind,
            seller_reason=reason,
            is_in_stock=(it.get("available_quantity") or 0) > 0
                        or it.get("buying_mode") == "buy_it_now",
            condition="new" if it.get("condition") == "new" else "used",
            brand_guess=attrs.get("BRAND"),
            model_guess=attrs.get("MODEL"),
            installments=inst_str,
            source="api",
            detail_fetched=True,     # API 已含详情级字段，无需再进详情页
        )
        self._enrich_from_title(lst)
        # API 属性表比标题准，覆盖回去
        if attrs.get("RAM"):
            r, _ = extract.parse_ram_rom(str(attrs["RAM"]) + "GB+1GB")
            lst.ram_gb = lst.ram_gb or r
        if attrs.get("INTERNAL_MEMORY"):
            _, o = extract.parse_ram_rom("1GB+" + str(attrs["INTERNAL_MEMORY"]))
            lst.rom_gb = o or lst.rom_gb
        if attrs.get("COLOR"):
            lst.color = str(attrs["COLOR"])[:30]
        lst.specs = {k: v for k, v in attrs.items() if v}
        return lst

    # ------------------------------------------------ 通道②③ 网页

    def build_search_url(self, query: str) -> str:
        import re
        from urllib.parse import quote
        slug = re.sub(r"\s+", "-", query.strip().lower())
        base = (self.channel.get("search_url") or "").replace("{q}", quote(slug, safe="-"))
        return base

    def parse_listings(self, html: str, text: str) -> list[Listing]:
        items = super().parse_listings(html, text)
        # ML 搜索页的商品链接特征：/p/ML... 或 articulo. 或 produto.
        keep = []
        for it in items:
            u = it.url.lower()
            if ("/p/ml" in u or "articulo." in u or "produto." in u
                    or "/MLM" in it.url or "/MLB" in it.url):
                keep.append(it)
        result = keep or items
        for it in result:
            it.source = "selector_web"
        self.last_channel_used = "web"
        return result

    # ------------------------------------------------ 统一入口

    def collect(self, engine, query: str, limit: int = 20) -> tuple[list[Listing], str]:
        """只走官方 API。返回 (结果, 状态说明)。

        ★★ 这里原本是"三通道自动降级：API 不行就爬网页"。**已经取消**。
          mercadolibre.com.* 的 robots.txt 把 ClaudeBot / Claude-User
          与 GPTBot、PerplexityBot、Amazonbot 并列在一组下，共用 `Disallow: /`
          —— 全站禁止，六国站点一致（www 与 listado 两个主机都禁）。

          所以对这个渠道，"降级"降的不是质量，是**越过站方明确的拒绝**。
          没有 token 就是没有合法通道，如实返回空 + 原因，让它显示成
          "缺凭据"而不是悄悄从网页把数据爬回来。

          ★ 官方 API 不受此限：那是站方主动提供的取数方式。
            用户配好 meli_access_token 后这个渠道自然恢复。
        """
        if not self.api_available():
            return [], "no_token:api_only"
        items, status = self.search_api(query, limit)
        if status == "ok":
            return items, "ok:api"
        if status == "empty":
            return [], "empty:api"
        return [], f"{status}:api_only"

