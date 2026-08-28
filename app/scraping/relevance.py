# -*- coding: utf-8 -*-
"""搜索结果相关性校验 —— 挡住"搜索没生效但看起来成功"的污染。

★ 为什么必须有这一层（实测案例）：
  Coppel 的搜索 URL 试了 5 种变体，全部返回同样的 11 条商品：
  锐步运动鞋、Flexi 皮鞋、Spring Air 床垫。页面正常返回 200，
  解析器正常解析出 11 条带价格的商品 —— 体检报告显示「OK 11条」，
  比被风控拦截的 Liverpool 看起来还健康。

  但这 11 条是首页推荐位，不是「celular samsung」的搜索结果。
  如果直接入库，运动鞋的 899 MXN 会进入手机价格基线，
  把中位价拉到地板上，之后所有竞品比价与异常剔除全部失真 —— 而且不报错。

  **抓不到数据你知道没数据；抓错了你以为有数据。后者危险得多。**

判定策略（批级，不是逐条）：
  逐条过滤只能删掉杂质，识别不出"搜索根本没生效"这个根因。
  批级判定看整批的相关率：相关率过低 → 整批作废并标记 selector_broken，
  这样主 Agent 下一轮就知道该修这个渠道，而不是继续攒垃圾。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("relevance")

# 整批相关率低于此值 → 判定搜索未生效
MIN_BATCH_RELEVANCE = 0.34

# ★ 批量小于这个数就不做「整批作废」的判定 —— 样本不足，统计上没意义。
#
#   实测教训（Alkosto CO）：页面实际有 80 个商品链接、88 处手机词，
#   但解析器只认出 4 条，其中 3 条是推荐位 ⇒ 相关率 25% < 34% ⇒ 整批作废，
#   报告写「搜索没生效，必须修 search_url」。**而 search_url 是对的**，
#   真正的问题是解析器认不出这个站的商品卡片。
#
#   一个防污染机制自己产生了误判，还把人引向错误的修复方向 ——
#   所以样本量下限要给足，并且小样本时**如实说明是样本不足**，
#   不能伪装成"搜索没生效"这种确定性结论。
MIN_BATCH_SIZE = 8

# 解析条数远少于页面商品数时的提示阈值：这是"解析器坏了"的独立信号，
# 与"搜索没生效"是两回事，必须分开报
LOW_YIELD_HINT = 6

# 明显不属于消费电子的品类词，命中即判为无关。
# 拉美综合零售商（Coppel/Liverpool/Sears）什么都卖，这类词很有区分度。
_OFF_TOPIC = re.compile(
    r"\b(tenis|zapato|zapatilla|bota|calzado|sandalia|sapato|t[êe]nis|"
    r"colch[óo]n|colch[ãa]o|cama|sof[áa]|mueble|m[óo]vel|sill[óo]n|mesa|"
    r"camisa|pantal[óo]n|blusa|vestido|chamarra|jean|ropa|roupa|playera|"
    r"perfume|maquillaje|cosm[ée]tic|shampoo|crema|pa[ñn]al|"
    r"licuadora|refrigerador|lavadora|estufa|microondas|batidora|"
    r"bicicleta|juguete|brinquedo|mochila|maleta|reloj de pared|"
    r"comedor|lampara|l[âa]mpada|cortina|toalla|s[áa]bana)\b", re.I)

# 消费电子正面信号
#
# ★ 结尾**不能**用 \b —— 西语/葡语的复数词尾会让匹配失效：
#     \b(celular)\b   匹配 "celular" 但**不匹配 "celulares"**
#     \b(audifon)\b   不匹配 "audifonos"
#   实测因此把 Hiraoka 的 8 条耳机全判成"与耳机无关"。
#   改成允许词尾（\w*），前缀边界 \b 保留（避免 "gb" 命中 "bigbang"）。
_ON_TOPIC = re.compile(
    r"\b(celular|smartphone|tel[ée]fono|m[óo]vil|iphone|galaxy|redmi|poco|"
    r"tablet|tableta|ipad|notebook|laptop|macbook|smartwatch|smart\s*watch|"
    r"aud[íi]fon|auricular|earbud|airpods|fone de ouvido|headphone|"
    r"pulgada|polegada|gb|ram|amoled|snapdragon|dimensity|mah)\w*", re.I)

# ★ 按品类分组的信号词。判「搜索是否生效」要看**品类**对不对，
#   而不是品牌对不对：
#     搜「celular samsung」返回床垫  → 搜索坏了，要修 URL
#     搜「audifonos Apple」返回 Philips 耳机 → 品类对，只是这家没 Apple 的货
#   两者处置完全不同，混为一谈会把「该渠道不卖这个牌子」误报成故障，
#   还会把整批可用数据扔掉（实测 Hiraoka 因此作废 11 批）。
#   同样：结尾用 \w* 而不是 \b，否则复数形式全部漏掉。
_CATEGORY_SIGNALS = {
    "phone": re.compile(r"\b(celular|smartphone|tel[ée]fono|m[óo]vil|iphone|"
                        r"galaxy [asz]|redmi|poco|moto |edge |razr)\w*", re.I),
    "tablet": re.compile(r"\b(tablet|tableta|ipad|galaxy tab|slate|"
                         r"mediapad|pad \d|xiaomi pad|redmi pad)\w*", re.I),
    "wearable": re.compile(r"\b(smartwatch|smart\s*watch|reloj|rel[óo]gio|"
                           r"banda|pulsera|fitband|watch)\w*", re.I),
    "audio": re.compile(r"\b(aud[íi]fon|auricular|earbud|airpods|buds|"
                        r"fone de ouvido|headphone|bocina|parlante|speaker)\w*", re.I),
    "pc": re.compile(r"\b(laptop|notebook|macbook|port[áa]til|"
                     r"computador|desktop|all in one)\w*", re.I),
}


def category_of_query(query: str) -> str | None:
    """从搜索词里认出品类（"audifonos Apple" → audio）。"""
    for cat, pat in _CATEGORY_SIGNALS.items():
        if pat.search(query or ""):
            return cat
    return None


def category_match_rate(titles: list[str], cat: str) -> float:
    """这批标题里有多少条属于该品类。"""
    pat = _CATEGORY_SIGNALS.get(cat)
    if not pat or not titles:
        return 0.0
    return sum(1 for t in titles if pat.search(t or "")) / len(titles)


def check_yield(html: str, parsed_count: int) -> dict | None:
    """低产率检测：页面上明明有很多商品，却只解析出寥寥几条。

    这是与「搜索没生效」**完全不同**的一种故障，必须分开报：
      搜索没生效 → 修 search_url
      解析低产   → 写专用适配器

    实测 Alkosto：页面 80 个商品链接，解析器只认出 4 条，
    而那 4 条恰好是边角的推荐位 ⇒ 相关率 25% ⇒ 被判成"搜索没生效"，
    报告让人去修一个本来就正确的 URL。
    """
    if not html or parsed_count >= LOW_YIELD_HINT:
        return None
    links = len(re.findall(
        r'href="[^"]*(?:/p/|/producto|/product|/pdp|/dp/|-mp-|/ip/)', html, re.I))
    if links >= parsed_count * 4 and links >= 20:
        return {
            "links_on_page": links, "parsed": parsed_count,
            "reason": (f"★ 页面上有约 {links} 个商品链接，解析器只认出 {parsed_count} 条"
                       f"（{parsed_count / links:.0%}）。这不是「没货」也不是"
                       f"「搜索没生效」—— 是**该站的商品卡片结构与通用解析器不匹配**，"
                       f"需要写专用适配器。"),
        }
    return None


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", (s or "").lower()) if len(t) >= 3}


def score_item(title: str, query: str, brand_aliases: list[str] | None = None) -> float:
    """单条相关性 0~1。"""
    if not title:
        return 0.0
    low = title.lower()

    if _OFF_TOPIC.search(low):
        return 0.0

    score = 0.0
    # 品牌命中：最强信号
    for a in (brand_aliases or []):
        if a and re.search(rf"(?<![a-z0-9]){re.escape(a.lower())}(?![a-z0-9])", low):
            score += 0.6
            break
    # 搜索词命中
    qt = _tokens(query)
    if qt:
        hit = len(qt & _tokens(title)) / len(qt)
        score += 0.3 * hit
    # 品类信号
    if _ON_TOPIC.search(low):
        score += 0.3
    return min(score, 1.0)


def check_batch(titles: list[str], query: str,
                brand_aliases: list[str] | None = None,
                query_purpose: str = "discover") -> dict:
    """批级校验。返回判定详情。

    verdict:
      ok               相关率正常，可入库
      low_relevance    相关率偏低，逐条过滤后入库
      search_ineffective  搜索未生效（整批无关），整批作废
      too_few          样本太少，不做批级判定
    """
    if not titles:
        return {"verdict": "too_few", "relevance": 0.0, "kept": [],
                "reason": "无结果"}

    scores = [score_item(t, query, brand_aliases) for t in titles]
    relevant = [i for i, s in enumerate(scores) if s >= 0.4]
    rate = len(relevant) / len(titles)

    # ★ 用【具体型号名】搜索时，站点返回同品类的其它型号是**正常行为**，
    #   不是"搜索失效"。
    #   实测：Hiraoka 搜「Xiaomi Pad Mini」返回 Redmi Pad 2、Slate 12X 等，
    #   相关率 25% ⇒ 被判整批作废。但那批数据完全可用（都是平板），
    #   而且里面往往就有我们要的那款 —— 整批扔掉是纯损失。
    #   所以型号词搜索只做逐条过滤，不做"整批作废"。
    if query_purpose in ("sku", "track"):
        if not relevant:
            return {"verdict": "too_few", "relevance": rate, "kept": [],
                    "reason": f"型号词「{query}」无匹配结果（该型号可能未在此渠道上架）"}
        return {"verdict": "low_relevance", "relevance": rate, "kept": relevant,
                "reason": (f"型号词「{query}」相关率 {rate:.0%} —— "
                           f"用具体型号搜时站点会返回同品类其它型号，属正常，"
                           f"逐条过滤后保留 {len(relevant)}/{len(titles)} 条")}

    if len(titles) < MIN_BATCH_SIZE:
        return {"verdict": "too_few", "relevance": rate, "kept": relevant,
                "reason": f"仅 {len(titles)} 条，样本不足以做批级判定，逐条过滤"}

    # ★ 品类对但品牌不对 = **该渠道不卖这个牌子**，不是搜索失效。
    #   实测 Hiraoka 搜「audifonos Apple」返回 QCY/Philips/Miray 的耳机 ——
    #   品类 100% 正确，只是这家没有 Apple 耳机。
    #   这是事实，不是故障：不该报警、不该整批作废、也不该记进渠道健康档案。
    qcat = category_of_query(query)
    if qcat and rate < MIN_BATCH_RELEVANCE:
        cat_rate = category_match_rate(titles, qcat)
        if cat_rate >= 0.6:
            return {
                "verdict": "brand_absent", "relevance": rate, "kept": relevant,
                "reason": (f"搜索正常（返回的 {cat_rate:.0%} 都是{qcat}类商品），"
                           f"但该渠道似乎没有这个品牌的货 —— "
                           f"只有 {len(relevant)}/{len(titles)} 条匹配品牌。"
                           f"这是渠道选品的事实，不是故障。"),
            }

    if rate < MIN_BATCH_RELEVANCE:
        samples = [t[:45] for t in titles[:3]]
        return {
            "verdict": "search_ineffective", "relevance": rate, "kept": [],
            "reason": (f"★ 搜索「{query}」返回 {len(titles)} 条，"
                       f"只有 {len(relevant)} 条相关（{rate:.0%}）。"
                       f"典型样例：{ ' / '.join(samples) }。"
                       f"整批作废，避免污染价格基线。"
                       # ★ 措辞不能武断。同样的表象有两个完全不同的根因，
                       #   指错方向会让人去修一个本来就对的 URL（实测 Alkosto 即是）。
                       f"两种可能：①搜索 URL 没生效，抓到的是首页推荐位；"
                       f"②搜索是对的，但解析器认不出该站的商品卡片，"
                       f"只捞到了页面边角的推荐位。"
                       f"先看页面上到底有没有目标商品，再决定修哪个。"),
        }

    if rate < 0.7:
        return {"verdict": "low_relevance", "relevance": rate, "kept": relevant,
                "reason": (f"搜索「{query}」相关率 {rate:.0%}，"
                           f"已逐条过滤，保留 {len(relevant)}/{len(titles)} 条")}

    return {"verdict": "ok", "relevance": rate, "kept": relevant,
            "reason": f"相关率 {rate:.0%}，正常"}
