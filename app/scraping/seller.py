# -*- coding: utf-8 -*-
"""卖家身份识别 —— 分清【渠道自营】/【品牌官方店】/【第三方卖家】。

用户要求（2026-08-10）：
  "美克多的网站里也会有官方店和非官方的，包括亚马逊也有官方的和非官方的"
  "利物浦里面有些商家是利物浦自营的，有些不是，要搞清楚哪些是自营的"

★ 为什么要单独一个模块：这件事比看起来难，且错了完全看不出来。

第一版把 "liverpool"/"coppel"/"sears" 这些词直接当官方标识、在整页文本里搜，
结果 Liverpool 站点的页头页脚 logo alt 里到处是 "Liverpool" ——
**该站所有商品（含第三方）全被判成自营**，正好把要区分的两类混成一类。

真实判据来自实抓 DOM（2026-08-10）：

| 渠道 | 自营/官方怎么认 | 第三方怎么认 |
|---|---|---|
| Liverpool | **没有** `Vendido por` 标签 | 有 `Vendido por <商家>` |
| Amazon | `Vendido por: Amazon` | `Vendido por: <其它>` |
| MercadoLibre | `official_store_id` 非空 / `Tienda oficial` 徽章 | 其余全是第三方 |
| Falabella/Ripley | `Vendido por Falabella` | `Vendido por <商家>` |
| 品牌官网 | 天然自营 | 不存在 |

三类而不是两类，因为它们的价格含义不同：
  - self_operated  渠道自营 —— 渠道自己定价，最能代表该渠道的官方零售价
  - brand_official 品牌官方店 —— 品牌在平台开的店，约等于品牌指导价
  - third_party    第三方卖家 —— 常有溢价/水货/翻新，做价格分析时要能单独剔除
"""
from __future__ import annotations

import re

# 卖家身份细分
SELF_OPERATED = "self_operated"      # 渠道自营
BRAND_OFFICIAL = "brand_official"    # 品牌在平台开的官方店
THIRD_PARTY = "third_party"          # 第三方卖家
UNKNOWN = "unknown"

# 粗分（与既有 price_obs.seller_type 兼容：official / third_party / unknown）
_COARSE = {SELF_OPERATED: "official", BRAND_OFFICIAL: "official",
           THIRD_PARTY: "third_party", UNKNOWN: "unknown"}

# ★ 反向映射：配置里的 default_seller_type 用的是**粗分**取值，
#   而本模块内部用细分取值。两套取值域混用过一次 ——
#   拿粗分值去查细分表永远查不中，渠道默认值被静默丢弃、全退化成 unknown。
_KIND_FROM_COARSE = {"official": SELF_OPERATED, "third_party": THIRD_PARTY,
                     "unknown": UNKNOWN}

# ★ "卖家" 标签的文案。必须成对匹配「标签 + 紧随其后的名字」，
#   不能只搜关键词 —— 见模块头部说明。
_SOLD_BY = re.compile(
    r"(?:vendido\s+(?:y\s+enviado\s+)?por|vendido\s+e\s+entregue\s+por|"
    r"vendido\s+por|vendedor|sold\s+by|sold\s+and\s+shipped\s+by|"
    r"loja\s+do\s+vendedor|vendido\s+e\s+entregue)"
    r"\s*[:：]?\s*([A-Za-zÀ-ÿ0-9][^\n\r|·•]{1,60})", re.I)

# 配送方 ≠ 卖家。Amazon 上「第三方卖家 + 亚马逊配送」很常见，
# 把配送方当卖家会把大量第三方误判成官方。
_SHIPPED_BY = re.compile(
    r"(?:enviado\s+por|entregue\s+por|ships\s+from|fulfilled\s+by)"
    r"\s*[:：]?\s*([A-Za-zÀ-ÿ0-9][^\n\r|·•]{1,60})", re.I)

# 品牌官方店徽章。
# ★ 只收"品牌自己开的店铺"这一种含义，不要把「授权经销商」算进来：
#   `distribuidor autorizado` 是**零售商的进货资质**（Coppel 从三星进货再自己卖），
#   定价权在渠道；`tienda oficial` 是**品牌自营店铺**，定价权在品牌。
#   混为一谈会把 Coppel/Sears 的自营商品全判成"品牌官方店"，
#   而这两类的价格含义正是价格分析要区分的东西。
_OFFICIAL_BADGE = re.compile(
    r"(tienda\s+oficial|loja\s+oficial|official\s+store|"
    r"tienda\s+of\.|store\s+oficial)", re.I)

# 各渠道的自营主体名（卖家名**等于**这些才算自营，不做子串泛匹配）
CHANNEL_SELF_NAMES = {
    "liverpool": ["liverpool", "liverpool.com.mx", "tienda liverpool"],
    "amazon": ["amazon", "amazon.com.mx", "amazon.com.br", "amazon méxico",
               "amazon mexico", "amazon brasil", "vendido por amazon"],
    "falabella": ["falabella", "falabella.com", "falabella retail"],
    "ripley": ["ripley", "ripley.cl", "ripley perú", "ripley peru"],
    "coppel": ["coppel", "coppel.com"],
    "sears": ["sears", "sears méxico", "sears mexico"],
    "sanborns": ["sanborns", "sanborns.com.mx"],
    "alkosto": ["alkosto", "alkosto.com"],
    "hiraoka": ["hiraoka"],
    "paris": ["paris", "paris.cl"],
    "entel": ["entel"],
    "claro": ["claro", "claro colombia"],
    "fastshop": ["fast shop", "fastshop"],
    "shopee": [],          # 纯 marketplace，无自营
    "meli": [],            # 纯 marketplace，无自营
    "samsung": ["samsung"],
    "apple": ["apple", "apple store"],
}

# 品牌官网类渠道：天然全是官方
BRAND_STORE_ADAPTERS = {"samsung", "apple", "brand_store"}

# 纯 marketplace：没有自营，缺省不能当官方
MARKETPLACE_ADAPTERS = {"meli", "shopee"}


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def extract_seller_name(page_text: str, html: str = "") -> tuple[str | None, str | None]:
    """从页面里抠出（卖家名, 配送方名）。抠不到返回 (None, None)。

    只在**标签紧邻**的位置取名字，不做全页关键词搜索。
    """
    blob = page_text or ""
    seller = shipper = None
    m = _SOLD_BY.search(blob)
    if m:
        seller = re.sub(r"\s+", " ", m.group(1)).strip(" .,:|-")[:60] or None
    m2 = _SHIPPED_BY.search(blob)
    if m2:
        shipper = re.sub(r"\s+", " ", m2.group(1)).strip(" .,:|-")[:60] or None
    return seller, shipper


def classify(seller_name: str | None, adapter: str, page_text: str = "",
             brand: str = "", channel_default: str = UNKNOWN,
             official_store_id=None, shipper: str | None = None) -> dict:
    """判定卖家身份。返回 {kind, coarse, seller_name, reason, is_official}。

    判定顺序是有讲究的，不能调换：
      1. 官方 API 的 official_store_id —— 最硬的证据，有就直接采信
      2. 品牌官网渠道 —— 天然自营
      3. 抓到了卖家名 —— 拿名字比对该渠道的自营主体
      4. 有官方店徽章
      5. 没有卖家名 —— 按渠道形态给缺省（这一步最容易错，见下）
    """
    adapter = (adapter or "").lower()
    name_n = _norm(seller_name)

    # ① 官方 API 给的官方店 ID —— 最硬的证据
    if official_store_id not in (None, "", 0, "0"):
        return _mk(BRAND_OFFICIAL, seller_name,
                   f"官方API标记为官方店（official_store_id={official_store_id}）")

    # ② 品牌官网：天然自营
    if adapter in BRAND_STORE_ADAPTERS:
        return _mk(SELF_OPERATED, seller_name or adapter,
                   f"品牌官方商城（{adapter}）直营，不存在第三方卖家")

    self_names = CHANNEL_SELF_NAMES.get(adapter, [])

    # ③ 抓到了卖家名 —— 用名字判，最可靠
    if name_n:
        if any(name_n == s or name_n.startswith(s + " ") or name_n == s.replace(" ", "")
               for s in self_names):
            return _mk(SELF_OPERATED, seller_name,
                       f"卖家名「{seller_name}」= 该渠道自营主体")
        # 品牌自己在平台开的店
        if brand and _norm(brand) and _norm(brand) in name_n:
            if _OFFICIAL_BADGE.search(page_text or ""):
                return _mk(BRAND_OFFICIAL, seller_name,
                           f"卖家名含品牌「{brand}」且页面有官方店徽章")
            return _mk(BRAND_OFFICIAL, seller_name,
                       f"卖家名「{seller_name}」含品牌名，判为品牌官方店")
        return _mk(THIRD_PARTY, seller_name,
                   f"卖家名「{seller_name}」不是渠道自营也不是品牌店 → 第三方")

    # ④ 没抓到卖家名时，★单独一个徽章文案不足以判定。
    #
    #   判定用的是整页文本，而 "tienda oficial" 可能出现在页面任何地方 ——
    #   推荐位、导航、其它商品的卡片。这与 Liverpool 那个原始 bug 同构
    #   （全页搜关键词导致全站商品被判成官方）。
    #   实测 Coppel：8 条商品因页面某处有徽章被判成 brand_official，
    #   而它们其实是 Coppel 自营。
    #
    #   所以徽章只在**能同时定位到品牌名**时才采信，否则退回按渠道形态判。
    badge = _OFFICIAL_BADGE.search(page_text or "")
    if badge and brand:
        near = (page_text or "")[max(0, badge.start() - 60): badge.end() + 60].lower()
        if _norm(brand) and _norm(brand) in near:
            return _mk(BRAND_OFFICIAL, None,
                       f"徽章「{badge.group(1)}」附近出现品牌名「{brand}」"
                       f"（未抠到店铺名，判为品牌官方店）")

    # ⑤ ★ 没有任何卖家标识时的缺省 —— 这一步的语义完全取决于渠道形态
    if adapter in MARKETPLACE_ADAPTERS:
        # 纯 marketplace 上"没标官方"就是没标官方，绝不能当官方。
        # MELI/Shopee 绝大多数挂牌都是第三方卖家。
        return _mk(UNKNOWN, None,
                   "纯marketplace且无官方店标识 —— 不能默认当官方，"
                   "需详情页确认（价格分析里按未知处理）")
    if self_names:
        # ★ 自营型渠道（Liverpool/Coppel/Sears…）：**没有** Vendido por 标签
        #   恰恰是自营的标志 —— marketplace 商品才会显式标出商家。
        #   这是实抓 Liverpool 得到的结论：56 个商品卡片里只有 1 处该标签。
        return _mk(SELF_OPERATED, None,
                   f"页面无第三方卖家标签 → 判为 {adapter} 自营"
                   f"（该类站点只有marketplace商品才标卖家）")
    # channel_default 可能是粗分（配置里的写法）也可能是细分，两种都接
    fallback = (channel_default if channel_default in _COARSE
                else _KIND_FROM_COARSE.get(channel_default, UNKNOWN))
    return _mk(fallback, None,
               f"页面无卖家信息，取渠道默认值（{channel_default}）")


def _mk(kind: str, name: str | None, reason: str) -> dict:
    return {"kind": kind, "coarse": _COARSE.get(kind, "unknown"),
            "seller_name": name, "reason": reason,
            "is_official": kind in (SELF_OPERATED, BRAND_OFFICIAL)}


def detect(page_text: str, html: str = "", adapter: str = "", brand: str = "",
           channel_default: str = UNKNOWN, seller_name: str | None = None,
           official_store_id=None) -> dict:
    """一步到位：从页面抠卖家名并判定身份。"""
    shipper = None
    if not seller_name:
        seller_name, shipper = extract_seller_name(page_text, html)
    return classify(seller_name, adapter, page_text, brand,
                    channel_default, official_store_id, shipper)
