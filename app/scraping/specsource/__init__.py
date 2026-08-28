# -*- coding: utf-8 -*-
"""规格数据源 —— 从公开规格站取**真实规格**，替代让模型猜。

为什么要这一层：
  规格补全 Agent 用大模型填规格，两种结果都不好用：
    · 认得出型号的，它给的规格**没有出处**，对不对只能靠信；
    · 认不出的（残缺型号名），它**正确地拒绝作答** —— 于是大片空白。
  实测手机 309 个里，靠模型只填出 2 个；把型号名洗干净后也才 261 个，
  而且那 261 个的可信度是"模型说的"。
  竞品匹配的规则①（规格相近）建立在这些数字上，来源不明是不行的。

已核对的站点条款（2026-08-11）：
  · GSMArena —— robots.txt 对所有 UA **禁止 /res.php3 搜索端点**，
    但品牌列表页与机型详情页未被禁止；其 RSL 许可（/license.xml）
    permits: ai-summarize / search-index，prohibits: ai-inference / ai-train，
    并要求 CC BY 4.0 署名。
    ⇒ 本模块**不使用搜索端点**，改为走品牌列表页定位机型；
      **只取我们自己在跟踪的机型**，不做全库复制；
      每条记录落库时写上来源 URL 与署名要求。
  · versus.com —— robots.txt 明确对 `anthropic-ai` / `ClaudeBot`
    `Disallow: /`。**本项目不抓该站**，音频规格另寻来源。
"""
from .gsmarena import (BRAND_SLUG, GsmArenaSource, name_variants,
                       normalize_model_key, parse_device_page,
                       trimmed_variants)

__all__ = ["GsmArenaSource", "BRAND_SLUG", "normalize_model_key",
           "name_variants", "parse_device_page", "trimmed_variants"]
