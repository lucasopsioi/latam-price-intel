# -*- coding: utf-8 -*-
"""渠道适配器注册表。

配置里 adapter 字段填的名字在这里查表。查不到就回落 GenericAdapter ——
新增渠道时先不写适配器也能跑（JSON-LD + 通用卡片启发式），
真跑出来不理想再补一个专用适配器。
"""
from __future__ import annotations

from .base import ChannelAdapter, Listing
from .meli import MeliAdapter
from .sites import (AlkostoAdapter, AmazonAdapter, AppleAdapter,
                    BrandStoreAdapter, ClaroCoAdapter, CoppelAdapter,
                    EntelClAdapter, FalabellaAdapter, GenericAdapter,
                    AcmeStoreAdapter,
                    LiverpoolAdapter, RipleyAdapter, SamsungAdapter,
                    SanbornsAdapter, SearsAdapter, ShopeeAdapter,
                    VtexAdapter)

REGISTRY: dict[str, type[ChannelAdapter]] = {
    "generic": GenericAdapter,
    "meli": MeliAdapter,
    "amazon": AmazonAdapter,
    "falabella": FalabellaAdapter,
    "ripley": RipleyAdapter,
    "liverpool": LiverpoolAdapter,
    "shopee": ShopeeAdapter,
    "samsung": SamsungAdapter,
    "apple": AppleAdapter,
    "coppel": CoppelAdapter,
    "sears": SearsAdapter,
    "sanborns": SanbornsAdapter,
    "claro_co": ClaroCoAdapter,
    "entel_cl": EntelClAdapter,
    "alkosto": AlkostoAdapter,
    "vtex": VtexAdapter,
    "brand_store": BrandStoreAdapter,
    "acme_store": AcmeStoreAdapter,
}


def build_adapter(channel: dict, country: dict, **kw) -> ChannelAdapter:
    cls = REGISTRY.get((channel.get("adapter") or "generic").lower(), GenericAdapter)
    if cls is MeliAdapter:
        return cls(channel, country, access_token=kw.get("meli_token", ""))
    return cls(channel, country)


__all__ = ["REGISTRY", "build_adapter", "ChannelAdapter", "Listing"]
