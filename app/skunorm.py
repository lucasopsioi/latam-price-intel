# -*- coding: utf-8 -*-
"""SKU 名归一化 —— 复用 nubimetrics-platform 的清洗规则。

为什么是**引用**而不是抄一份过来：
  那个项目里已经沉淀了一整套按品牌命名规律写的型号抽取规则
  （brand_patterns / exceptions / 别名表 2400 行 / 配件词 / 捆绑三道闸），
  而且带一张**联网查证过的官方名映射表** —— 它能区分"规则猜出来的"
  和"查证过的官方写法"，这正是我们缺的东西。
  它自己的注释里写着：抄出来的两份迟早会不同步。所以这里直接 import。

两边的分工（这条边界要守住）：
  · 本文件负责**渠道级预清洗** —— 情报中枢抓的是 Falabella / Ripley / Liverpool
    这些零售商站，标题前缀是 "Envío gratis app"、尾巴是 "Por <卖家>"，
    而 nubimetrics 的规则是照着 MercadoLibre 的标题写的，没有这些形态。
    不预清洗直接喂过去，会得到 "HONOR App Honor Honor 70" 这种结果；
    卖家名叫 Cablex 的手机还会因为 "cable" 被判成配件。
  · nubimetrics 负责**品牌与型号的知识** —— 型号规律、大小写词形（WH-CH720N /
    iPad / 520BT）、配件判定、捆绑拆分、官方名查证。

拿不到那个项目时会**如实降级**回本项目原有的归一化，并在日志里说清楚，
不会假装成功。
"""
from __future__ import annotations

import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("skunorm")

# 默认在同一个工作区下找；可用环境变量覆盖（换机器/换路径时不用改代码）
_DEFAULT_NUBI = Path(__file__).resolve().parents[2] / "nubimetrics-platform"
NUBI_PATH = Path(os.environ.get("NUBIMETRICS_PATH") or _DEFAULT_NUBI)


@lru_cache(maxsize=1)
def _nubi():
    """返回 nubimetrics 的 classify()，拿不到就返回 None。"""
    try:
        p = str(NUBI_PATH)
        if not (NUBI_PATH / "src" / "model" / "normalize.py").exists():
            log.warning("未找到 nubimetrics-platform（%s），SKU 归一化退回本地规则。"
                        "设环境变量 NUBIMETRICS_PATH 可指定路径", p)
            return None
        if p not in sys.path:
            sys.path.insert(0, p)
        from src.model.normalize import classify  # noqa: PLC0415
        return classify
    except Exception as e:  # noqa: BLE001
        log.warning("导入 nubimetrics 归一化失败（%s: %s），退回本地规则",
                    type(e).__name__, str(e)[:120])
        return None


def available() -> bool:
    return _nubi() is not None


# ---------------------------------------------------------------- 渠道级预清洗

# 卖家归属尾巴：Falabella 一律以 "… Por <卖家>" 收尾。
# 不切掉的话：① 同一台机器按卖家裂成十几个产品；
#            ② 卖家名会被后面的规则误读（有个卖家就叫 Cablex，
#               "cable" 一命中，整台手机被判成配件）。
_SELLER_TAIL = re.compile(
    r"\s+(?:vendido|ofrecido|comercializado|distribuido|entregado)?\s*"
    r"\bpor\b\s+.*$", re.I)

# 配送/促销前缀，可能叠好几层（"Envío gratis app APPLE …"）
_PREFIX = re.compile(
    r"^\s*(?:env[íi]o\s+gratis(?:\s+app)?|envio\s+gratis(?:\s+app)?|"
    r"frete\s+gr[áa]tis|llega\s+ma[ñn]ana|despacho\s+gratis|entrega\s+gratis|"
    r"oferta\s+especial|retiro\s+en\s+tienda|retiro\s+en\s+un\s+punto|"
    r"a\s+domicilio|hot\s+sale|black\s+friday|buen\s+fin|liquidaci[óo]n)\s*",
    re.I)

# 渠道自己的名字（Sears 把渠道名缀在标题末尾）
_CHANNEL_WORDS = re.compile(
    r"\b(?:sears|coppel|liverpool|sanborns|elektra|falabella|ripley|alkosto|"
    r"hiraoka|fast\s*shop|fastshop|shopee|mercado\s*libre)\b", re.I)


# ★ 运营商合约码：Sears/Coppel 墨西哥的标题长这样
#   "Celular Samsung A56 256Gb Color Rosa R9 (Telcel) SEARS"
#   R9 是套餐码、(Telcel) 是运营商。MercadoLibre 上没有这种写法，
#   所以 nubimetrics 的噪声表里没有 —— 不剥就会归一化出 "Samsung A56 R9 Telcel"，
#   同一台机器在不同渠道对不上。
_CARRIER = re.compile(
    r"\(\s*(?:telcel|movistar|at&t|unefon|claro|entel|bitel|tigo|personal|"
    r"vivo|tim|oi)\s*\)|\b(?:telcel|movistar|unefon)\b|(?<![a-z0-9])r\d{1,2}(?![a-z0-9])",
    re.I)

# nubimetrics 的颜色表（照 ML 数据整理的）里缺的几个西语颜色词。
# ★ 只补词，不另起一张表 —— 另起一张迟早和上游那张不同步。
#   实测缺 "lima"（青柠绿），导致 "Moto G67 Lima 4+256GB" 归一化成
#   "Motorola G67 Lima 4+" 而不是 "Motorola G67"。
_EXTRA_COLORS = re.compile(
    r"\b(?:lima|menta|coral|lavanda|marfil|arena|grafito|carbon|"
    r"medianoche|crema|perla|zafiro|esmeralda|turquesa)\b", re.I)

# ★ 卖家名缀在末尾且**不带 "Por"**：Sanborns 写成 "… Negro TEKKNOSHOP-"。
#   `_SELLER_TAIL` 靠 "Por" 定位，抓不到这种。
#   判据用**末尾带连字符的整词**，不是"末尾全大写词" ——
#   后者会误伤型号码（"… ANV15-52-96J3" / "… WH-CH720N"）。
_SELLER_SUFFIX = re.compile(r"\s+[A-Za-z][A-Za-z&.\s]{3,20}-\s*$")

# 内存规格 "4+256GB" / "8+128" —— 加号会被当成捆绑标记，
# 而 nubimetrics 只处理带单位的写法（"8gb+128gb"）
_MEM_SPEC = re.compile(r"(?<![a-z0-9])\d{1,2}\s*\+\s*\d{2,4}\s*(?:gb|tb)?(?![a-z0-9])",
                       re.I)

# 斜杠写法 "128/6gb"（容量/内存）：单位只跟在后一个数上，
# 前一个数没有单位，噪声表按 "\d+gb" 匹配不到它 —— 剥完只剩个光秃秃的 128
# 挂在型号后面（"Galaxy A26 128"），同一台机器又按容量裂开。
_MEM_SLASH = re.compile(r"(?<![a-z0-9])\d{2,4}\s*/\s*\d{1,3}\s*(?:gb|tb)(?![a-z0-9])",
                        re.I)


# ★ 西语/葡语的**规格与品类词**，会跟在型号后面被当成型号的一部分：
#   "A6 Pro Procesador Mediatek" / "Moto G06 Gpu 8" / "Movil Oppo Reno"
#   "Plegable Honor Magic" / "Almacenamiento G100 Pro"
#   实测手机缺规格的 104 个里，大半是这么来的 —— 型号对不上外部规格源，
#   等于有数据也用不上。
_SPEC_WORDS = re.compile(
    r"\b(?:procesador|processador|gpu|cpu|almacenamiento|armazenamento|"
    r"memoria|mem[óo]ria|c[áa]mara|camera|bateria|bater[íi]a|pantalla|tela|"
    r"m[óo]vil|movel|celulari?|smartphone|tel[ée]fono|plegable|dobr[áa]vel|"
    r"gama|serie|s[ée]rie|nuevo|novo|equipo)\b", re.I)

# Falabella 的标题用**孤立的 I** 当分隔符（"CelularI X8d I 4G I 512 GB I 8GB RAM"）。
# 它既不是型号也不是单词，留着会变成 "Celulari X8d I"。
_LONE_SEP = re.compile(r"(?<![A-Za-z0-9])[Ii](?![A-Za-z0-9])")


def pre_clean(title: str) -> str:
    """把零售商站特有的噪声剥掉，再交给 nubimetrics 的规则。"""
    t = " " + str(title or "") + " "

    cut = _SELLER_TAIL.sub(" ", t)
    # 留个保险：万一某个渠道把卖家写在前面，整段切光会把型号也吃掉
    if re.search(r"\d", cut) or len(cut.split()) >= 3:
        t = cut

    t = t.strip()
    for _ in range(3):                    # 前缀可能叠加
        t2 = _PREFIX.sub("", t)
        if t2 == t:
            break
        t = t2

    t = _CHANNEL_WORDS.sub(" ", t)
    t = _SELLER_SUFFIX.sub(" ", t)
    t = _CARRIER.sub(" ", t)
    t = _MEM_SLASH.sub(" ", t)
    t = _MEM_SPEC.sub(" ", t)
    t = _EXTRA_COLORS.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- 对外接口

def classify(brand_name: str, title: str, category: str | None = None) -> dict:
    """归一化一条挂牌。

    返回 {sku, kind, source, verified, gift}
      kind     设备 | 配件 | 捆绑 | 白牌
      verified True = 名字在**联网查证过**的官方名表里；False = 规则猜的
      source   pattern/exception/fallback/accessory/whitelabel（可事后审计可信度）
    """
    fn = _nubi()
    cleaned = pre_clean(title)
    if fn is None:
        return {"sku": "", "kind": "未知", "source": "unavailable",
                "verified": False, "gift": None, "cleaned": cleaned}
    try:
        r = fn((brand_name or "").upper(), None, cleaned)
    except Exception as e:  # noqa: BLE001
        log.debug("nubimetrics 归一化异常 %s: %s", type(e).__name__, str(e)[:100])
        return {"sku": "", "kind": "未知", "source": "error",
                "verified": False, "gift": None, "cleaned": cleaned}
    r["cleaned"] = cleaned
    sku = _restore_line(r.get("sku") or "", brand_name, r.get("verified"))
    r["sku_full"] = sku                      # 带品牌的完整写法（导出/推送用）
    r["sku"] = _strip_brand(sku, brand_name)
    return r


# ★ 产品线名本身就说明了品类，比"当时在抓哪个品类页"可靠。
#   现状：品类来自**采集单元**（在哪个品类页/用哪个搜索词抓的），
#   搜索串味或品类页混排时就会错 —— 实测 47 个产品判错，
#   iPad / Galaxy Tab 被记成 phone、iPhone 被记成 audio。
#   看板的"每个国家每个品类"维度直接被这些行污染。
#   只对**毫无歧义**的产品线做判定，含糊的一律不动（宁可不改，不能改错）。
_CATEGORY_BY_LINE = [
    ("tablet", re.compile(
        r"(?i)^(?:ipad|galaxy\s*tab|slate|redmi\s*pad|xiaomi\s*pad|honor\s*pad|"
        r"lenovo\s*tab|idea\s*tab|tab\s*[a-z]?\d)")),
    ("phone", re.compile(
        r"(?i)^(?:iphone|galaxy\s*[asmfz]\d|galaxy\s*z\s*(?:flip|fold)|"
        r"galaxy\s*note|redmi\s*note\s*\d|redmi\s*\d|moto\s*[ge]\d|edge\s*\d|"
        r"razr|magic\s*\d|nimbus\s*\d|reno\s*\d{1,2}|poco\s*[a-z]?\d)")),
    ("wearable", re.compile(
        r"(?i)^(?:apple\s*watch|galaxy\s*watch|galaxy\s*fit|watch\s*(?:gt|fit|ultra|se|d)|"
        r"mi\s*band|redmi\s*watch|amazfit|forerunner|fenix|instinct|venu|vivoactive|"
        r"versa|charge\s*\d|band\s*\d)")),
    ("audio", re.compile(
        r"(?i)^(?:airpods|galaxy\s*buds|sonicbuds|sonicclip|redmi\s*buds|"
        r"wh-|wf-|mdr-|tune\s*\d|live\s*\d{3}|liberty\s*\d|q\d{2}\b|"
        r"quietcomfort|momentum|accentum|partybox|flip\s*\d|charge\s*\d\s*$)"
        # ★ 这些词出现在**任何位置**都说明是耳机，不必在开头。
        #   实测 18 个音频产品躺在穿戴品类里（"In Ear Con"、"Con Cancelacion Ruido"、
        #   "Moto Buds C30"）—— 它们是被采集单元的品类带错的，
        #   而穿戴的规格维度（屏幕/电池）对耳机根本不适用。
        r"|\b(?:in.?ear|intraural|buds\b|earbuds|aud[íi]fonos?|auriculares?|"
        r"cancelaci[óo]n(?:\s+de)?\s+ruido|cancelamento(?:\s+de)?\s+ru[íi]do|"
        r"headphones?|earphones?|fone\s+de\s+ouvido)\b")),
    ("pc", re.compile(
        r"(?i)^(?:macbook|imac|ideapad|thinkpad|thinkbook|vivobook|zenbook|"
        r"inspiron|latitude|pavilion|envy|omen|victus|elitebook|probook|"
        r"aspire|nitro|predator|legion|loq|yoga|galaxy\s*book|acebook|omnibook|zbook)")),
]


def guess_category(model_name: str) -> str | None:
    """从型号名判品类。判不准就返回 None —— 不猜。"""
    m = (model_name or "").strip()
    if not m:
        return None
    for cat, pat in _CATEGORY_BY_LINE:
        if pat.search(m):
            return cat
    return None


def _strip_brand(sku: str, brand_name: str) -> str:
    """去掉开头的品牌名。

    ★ nubimetrics 把品牌拼进 SKU（"Samsung Galaxy A57"），因为它的下游是
      PowerBI 的单列口径，需要自描述。
      本项目的 rival_product 有独立的 brand_id 列，界面四处都是
      「品牌 + 型号」并排显示 —— 再把品牌塞进型号名会显示成
      "Samsung Samsung Galaxy A57"。
      两边约定不同，在边界上转换一次，不去改上游。
    """
    b = (brand_name or "").strip()
    if not sku or not b:
        return sku
    low, bl = sku.lower(), b.lower()
    if low.startswith(bl + " "):
        return sku[len(b):].strip()
    # 品牌显示名可能与 brand 表写法不同（vivo/realme/soundcore/TP-Link）
    first = sku.split(" ", 1)
    if len(first) == 2 and first[0].lower() == bl:
        return first[1].strip()
    return sku


def _restore_line(sku: str, brand_name: str, verified: bool) -> str:
    """补回品牌自己的产品线前缀：Samsung S26 → Samsung Galaxy S26。

    ★ 只对**未查证**的名字做。查证过的名字来自官方名映射表，是权威写法，
      再叠一层规则只会把对的改错。
    ★ 这不是编造：三星手机的 A/S/M/F/Z/Note 线全部叫 Galaxy、
      摩托的 G/E 线叫 Moto，是厂商自己的命名规律，不是我们猜的。
      别名表里有的（Samsung A56 → Galaxy A56）走查证，没有的走规律。
    """
    if not sku or verified:
        return sku
    try:
        from .agents.cleaner import _restore_line_prefix  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return sku
    b = (brand_name or "").strip()
    # nubimetrics 的输出形如 "Samsung S26"（品牌 + 型号），
    # 而 _restore_line_prefix 只认型号部分 —— 先摘掉品牌再补，最后拼回去
    body, prefix = sku, ""
    if b and sku.lower().startswith(b.lower()):
        prefix, body = sku[:len(b)], sku[len(b):].lstrip()
    fixed = _restore_line_prefix(body, b)
    return f"{prefix} {fixed}".strip() if prefix else fixed


# ---------------------------------------------------------------- 系列级 SKU（桶）

# ★★ 背景（2026-09-04，用户截图）：rival_product #2292「Lenovo Tab」的价格曲线在
#   18 万 CLP 与 8,500 CLP 之间来回跳。查明它是 158 种不同标题的桶：Tab P11 /
#   Tab Pro 12.7 / M10 / 钢化膜 全挤在一起。根因不在 nubimetrics 也不在 cleaner ——
#   权威表 config/sku_rules.yaml（用户 PowerQuery）末尾有**系列级兜底规则**
#   `Lenovo Tab ← [lenovo tab] / [tablet lenovo]`，任何没命中具体型号规则的联想
#   平板都落到这里，cleaner 再把 sku_code 原样当型号名。全库 26 个无数字 SKU
#   挂了 28 个产品 / 3,906 条观测，都是同一种桶。
#
#   ★ 不改 yaml 的规则语义（那是用户的口径，两边要对得上）；这里做两件事：
#     1. **识别**哪些权威 SKU 是"系列级"（is_generic_sku）
#     2. 命中系列级 SKU 时，回到原始标题**把产品线后面的型号 token 找回来**
#        （refine_series_sku：'LENOVO Tablet M10 5G …' → 'Lenovo Tab M10'）
#   找不回来的仍叫系列名，但 rival_product.is_bucket=1，下游按桶处理。

# 变体词：型号身份的一部分（与 matching/modelkey.VARIANT 同一口径），
# 名字里有它就不是"光秃秃的系列名"。
_VARIANT_WORDS = frozenset({
    "pro", "plus", "ultra", "max", "lite", "se", "fe", "mini", "fold", "flip",
    "kids", "play", "one", "active", "gen",
})


@lru_cache(maxsize=1)
def generic_skus() -> tuple[str, ...]:
    """权威表里的**系列级** SKU：无数字，且表里存在以它为前缀、扩展部分含字母的
    更细 SKU（"Lenovo Tab" ⊂ "Lenovo Tab M11"）。

    ★★ 这是**细分入口**（refine_series_sku 认它），不是桶判定。两件事必须分开：
      "Lenovo Idea Tab" 是系列级（表里有 Idea Tab Plus/Pro，所以标题里的 Plus
      必须细分出去），但它**自己也是一款真机**（11" 8GB/128GB）。
      桶判定走 is_bucket_sku（系列级 − 系列基础款 ± yaml 显式 generic）。
      合成一个判据就必然二选一错一头：要么 Ideatab Plus 永远留在基础款里
      （把干净单品改脏），要么基础款被永久标成桶（真单品被赶出所有单品消费方）。
    ★ 为什么不是"无数字即系列"：那会把 Lenovo Tab One / Xiaomi Pad Mini /
      Lenovo Idea Tab Pro 这类**真单品**也当成系列。
    ★ 为什么扩展部分要含字母："Redmi Pad SE" 只有 "Redmi Pad SE 8.7" 一个扩展，
      8.7 是屏幕尺寸不是型号 —— SE 本身是一款产品，不算系列。
      "Apple iPad Pro" 的扩展 "13 M5" 带芯片代号 ⇒ 算系列。
    """
    try:
        from . import skumap  # noqa: PLC0415
        skus = list(skumap.all_skus())
    except Exception:  # noqa: BLE001
        return ()
    low = [s.lower() for s in skus]
    out = []
    for s, l in zip(skus, low):
        if re.search(r"\d", s):
            continue
        for o in low:
            if o != l and o.startswith(l + " ") and re.search(r"[a-z]", o[len(l):]):
                out.append(s)
                break
    return tuple(out)


def is_generic_sku(sku: str | None) -> bool:
    """是不是**系列级** SKU（= 该不该拿标题去细分）。桶判定请用 is_bucket_sku。"""
    return bool(sku) and sku in generic_skus()


# ★★ 系列基础款：结构判据（无数字 + 表里有更长的带字母兄弟）会把它们判成系列，
#   但它们**自己就是一款在售机型**。为什么必须有这条通道：is_bucket 一旦落到
#   这几个产品身上，trackable_products / matcher._candidates / suggest_watch 会
#   永久排除它们，而人工把 is_bucket 改回 0 又会被 tools/mark_buckets.py --apply
#   打回 1 —— 误标在旧实现里**不可纠正**。误标一个真单品没人会发现，
#   漏标一个桶还有数据判据报告兜着（mark_buckets --data-report）。
#   逐条证据（2026-09-07 查库，全部是"拆掉能细分出去的变体之后剩下什么"）：
#     Lenovo Idea Tab  #2291  738 条观测 / 103 种标题。带 Plus 的那批
#       （"Ideatab Plus … 12.1 pulgadas 256 GB"）由 _lenovo_idea_tab 细分出去，
#       剩下的 640 条全是同一款 11" 2.5K 90Hz 8GB/128GB。
#     Lenovo Yoga Tab  #2934  71 条，全是 11.1" 3.2K 144Hz SD8Gen3 12GB RAM
#       （12.7"/16GB 那款是 Yoga Tab Plus —— 见 _YOGA_PLUS 的尺寸证据）。
#     Redmi Pad        #3719  Redmi Pad（2022，10.61"）本身在售；SE / SE 8.7 /
#       Pro / 2 / 2 Pro / 7 在 yaml 里各有规则且**排在它前面**，落到它头上的
#       只剩基础款自己。
#   ★ 刻意不收：Lenovo Legion Tab（兄弟 Legion Tab Gen 3 是真的世代差异）、
#     Apple iPad Air / iPad Mini / iPad Pro / iPad（多代同名，库里就是混的）。
#     没有"剩下的是同一款"的证据就不许进这张表 —— 这张表是收窄判定的闸，
#     不是放宽的检出器。
_BASE_MODEL_SKUS = frozenset({
    "Lenovo Idea Tab",
    "Lenovo Yoga Tab",
    "Redmi Pad",
})


@lru_cache(maxsize=1)
def sku_generic_flags() -> dict[str, bool]:
    """权威表里**显式写出**的桶标记：给 config/sku_rules.yaml 的某条规则加一行
    `generic: true` 或 `generic: false`，这里就认它，优先于本文件的任何判据。

    ★ 这是给用户的**可持久纠正通道**：判据把某个真单品判成桶时，改一行 yaml 就能
      永久否掉（在 skunorm 里加 _BASE_MODEL_SKUS 是另一条，两条等价）。
      改 rival_product.is_bucket 不行 —— mark_buckets --apply 会按判据再打回来。
    ★ 读的是 skumap 已缓存的那份规则，不另开一次 yaml 解析（两份解析迟早不同步）。
      与 generic_skus() 同样是进程级缓存：改完 yaml 要重启进程（或
      skumap.reload_rules() + 本函数 .cache_clear()）。
    """
    try:
        from . import skumap  # noqa: PLC0415
        rules = skumap._load().get("rules") or []
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, bool] = {}
    for r in rules:
        if isinstance(r, dict) and r.get("sku") and r.get("generic") is not None:
            out[str(r["sku"])] = bool(r["generic"])
    return out


def is_bucket_sku(sku: str | None) -> bool:
    """这个权威 SKU 是不是**桶**（一个名字装着多款产品）。

    优先级：yaml 显式 generic → 系列基础款白名单 → 结构判据 is_generic_sku。
    """
    if not sku:
        return False
    flag = sku_generic_flags().get(sku)
    if flag is not None:
        return flag
    if sku in _BASE_MODEL_SKUS:
        return False
    return is_generic_sku(sku)


def _norm(title: str) -> str:
    """与权威表同一条归一化（lower / 去重音 / '-'→空格），细分正则都跑在这上面。"""
    try:
        from . import skumap  # noqa: PLC0415
        return skumap.normalize(title)
    except Exception:  # noqa: BLE001
        return re.sub(r"\s+", " ", (title or "").lower()).strip()


# 型号数字后面：不能紧跟小数点+数字（'p 12.7' 是 12.7 英寸屏，不是 Tab P12），
# 也不能紧跟字母（'tab s5e' 是 S5e 不是 S5 —— 实测一条 S5e 电池挂牌差点新建出
# 「Samsung Galaxy Tab S5」）。
_NOT_DECIMAL = r"(?![.,]?\d)(?![a-z])"

# 三星平板变体尾巴：写法 → 表里的写法
_SAMSUNG_VARIANT = {"+": "+", "plus": "+", "lite": " Lite", "ultra": " Ultra",
                    "fe": " FE", "fe+": " FE+", "fe plus": " FE+"}

# ★★ Yoga Tab Plus 的**规格证据**：12.7" 屏 或 16GB RAM
#   （基础款 Lenovo Yoga Tab 是 11.1" 3.2K + 12GB RAM，两代规格不重叠）。
#   只看 'plus' 这一个词是不够的：#2292 里 12 条
#   "Lenovo Tablet Yoga Snapdragon 8 G3 16GB RAM 256GB 12.7''"（629,990~799,990 CLP）
#   标题里根本没有 plus，全被归进 11.1"/12GB 的基础款 #2934 —— 而**同一个函数上面
#   已经用 12.7 去否决 Tab P12 了**，尺寸证据只用了一半。
#   ★ 16GB 必须**贴着 RAM** 才算：#2934 里躺着一条
#     "TABLET YOGA TAB 3 8 ANDROID 5.1 16GB 2GB"（2015 年的 Yoga Tab 3，
#     16GB 是**存储**、RAM 只有 2GB），裸 `16gb` 会把它判成 2025 年的旗舰 Plus。
#     关键词判定先排假朋友，见 knowledge/lessons/scrape-normalize-silent-corruption。
#   ★ normalize 已把 ',' 换成 '.'，所以 "12,7" 这种写法也走这条。
_YOGA_PLUS = re.compile(
    r"\byoga\s*(?:tab\s*)?plus\b"
    r"|\b12\.7\b"
    r"|\b16\s*gb\s*(?:de\s+)?ram\b|\bram\s*(?:de\s+)?16\s*gb\b")


def _lenovo_tab(t: str) -> str | None:
    # 字母+两位数：M10 / M11 / P11 / P12 / K10 / K11（允许 'tab m 10' 这种带空格写法）
    m = re.search(r"\b(?:tab\s*)?([mpk])\s?(\d{1,2})" + _NOT_DECIMAL + r"\b", t)
    if m:
        base = f"Lenovo Tab {m.group(1).upper()}{m.group(2)}"
        v = re.match(r"\s*(plus|pro|kids)\b", t[m.end():])
        return _prefer_table(base, v.group(1).capitalize() if v else "")
    if re.search(r"\byoga\b", t):
        return _prefer_table("Lenovo Yoga Tab", "Plus" if _YOGA_PLUS.search(t) else "")
    if re.search(r"\blegion\b", t):
        return "Lenovo Legion Tab"
    if re.search(r"\bxiaoxin\b", t):
        return "Lenovo Xiaoxin Pad"
    m = re.search(r"\bidea\s*tab\b(?:\s*(pro|plus))?", t)
    if m:
        return _prefer_table("Lenovo Idea Tab", (m.group(1) or "").capitalize())
    # 只有变体词：'Lenovo Tablet Pro Mediatek' / 'Tab Play Kids' / 'Tab One'
    m = re.search(r"\btab(?:let)?\s+(pro|plus|one|play)\b", t)
    if m:
        return f"Lenovo Tab {m.group(1).capitalize()}"
    return None


def _lenovo_idea_tab(t: str) -> str | None:
    m = re.search(r"\bidea\s*tab\s*(pro|plus)\b", t)
    return f"Lenovo Idea Tab {m.group(1).capitalize()}" if m else None


def _lenovo_legion_tab(t: str) -> str | None:
    if re.search(r"\bgen\s*3\b|\b3\s*(?:ra|a|rd)?\s*gen\b|\bgen3\b", t):
        return "Lenovo Legion Tab Gen 3"
    return None


def _lenovo_yoga_tab(t: str) -> str | None:
    # ★ 与 _lenovo_tab 的 Yoga 分支同一份证据（_YOGA_PLUS）：同一条标题走
    #   yaml 的 "yoga tab" 规则还是 "lenovo tab" 兜底规则，只是命中顺序不同，
    #   两边给出不同的名字就等于同一款机器按命中路径裂成两个产品。
    return "Lenovo Yoga Tab Plus" if _YOGA_PLUS.search(t) else None


def _samsung_tab(t: str) -> str | None:
    m = re.search(r"\btab\s*active\s*(\d)\b(?:\s*(pro))?", t)
    if m:
        return _prefer_table(f"Samsung Galaxy Tab Active{m.group(1)}",
                             "Pro" if m.group(2) else "")
    m = re.search(r"\btab\s*([as])\s?(\d{1,2})" + _NOT_DECIMAL, t)
    if not m:
        return None
    base = f"Samsung Galaxy Tab {m.group(1).upper()}{m.group(2)}"
    rest = t[m.end():]
    # ★ 结尾不能用 \b：'fe+' 后面是空格，+ 与空格都是非词字符，\b 在那里不成立，
    #   于是 'fe+' 永远只匹到 'fe'（S10 FE+ 被并进 S10 FE，两个价位段混算）。
    v = re.match(r"\s*(fe\s*\+|fe\s+plus|fe|\+|plus|lite|ultra)(?![a-z0-9])", rest)
    tail = ""
    if v:
        key = re.sub(r"\s+", " ", v.group(1)).strip()
        key = {"fe +": "fe+"}.get(key, key)
        tail = _SAMSUNG_VARIANT.get(key, "")
    name = base + tail
    return name


def _redmi_pad(t: str) -> str | None:
    m = re.search(r"\bredmi\s*pad\s*(\d)" + _NOT_DECIMAL + r"\b(?:\s*(pro))?", t)
    if m:
        return _prefer_table(f"Redmi Pad {m.group(1)}", "Pro" if m.group(2) else "")
    m = re.search(r"\bredmi\s*pad\s*se\b(?:\s*(8\.7))?", t)
    if m:
        return _prefer_table("Redmi Pad SE", m.group(1) or "")
    if re.search(r"\bredmi\s*pad\s*pro\b", t):
        return "Redmi Pad Pro"
    return None


def _xiaomi_pad(t: str) -> str | None:
    m = re.search(r"\bpad\s*(\d)(s)?" + _NOT_DECIMAL + r"\b(?:\s*(pro))?", t)
    if m and "redmi" not in t and "poco" not in t:
        base = f"Xiaomi Pad {m.group(1)}{(m.group(2) or '').upper()}"
        return _prefer_table(base, "Pro" if m.group(3) else "")
    if re.search(r"\bpad\s*mini\b", t):
        return "Xiaomi Pad Mini"
    if re.search(r"\bxiaomi\s*pad\s*pro\b", t):
        return "Xiaomi Pad Pro"
    return None


def _poco_pad(t: str) -> str | None:
    m = re.search(r"\bpoco\s*pad\s*([xcm]\d)\b", t)
    return f"POCO Pad {m.group(1).upper()}" if m else None


def _prefer_table(base: str, variant: str) -> str:
    """base+变体 若在权威表里就用它；不在而 base 在表里 → 按表的口径归到 base
    （yaml 的 `tab m10` 本来就把 M10 Plus 收进 M10）；两者都不在 → 保留变体
    （新名字，Pro/Plus 是型号身份的一部分，见 modelkey 的教训）。"""
    full = f"{base} {variant}".strip()
    if not variant:
        return base
    try:
        from . import skumap  # noqa: PLC0415
        table = set(skumap.all_skus())
    except Exception:  # noqa: BLE001
        table = set()
    if full in table:
        return full
    if base in table:
        return base
    return full


# 系列级 SKU → 细分器。没登记的系列（Apple iPad / Acme Slate Tab：世代与芯片
# 写法太多，正则猜错的代价高）不细分，留在桶里由 is_bucket 标出来。
_REFINERS = {
    "Lenovo Tab": _lenovo_tab,
    "Lenovo Idea Tab": _lenovo_idea_tab,
    "Lenovo Legion Tab": _lenovo_legion_tab,
    "Lenovo Yoga Tab": _lenovo_yoga_tab,
    "Samsung Galaxy Tab": _samsung_tab,
    "Redmi Pad": _redmi_pad,
    "Xiaomi Pad": _xiaomi_pad,
    "POCO Pad": _poco_pad,
}


def refine_series_sku(sku: str | None, title: str | None) -> str | None:
    """系列级 SKU + 原始标题 → 更细的 SKU 名；找不到型号 token 返回 None。

    ★ 只对 is_generic_sku 为真的 SKU 起作用：具体型号（"Lenovo Tab M10"）是权威
      表说了算的，不再二次加工 —— 否则又是"两条路各洗一遍"。
    ★ 返回值保留品牌前缀，与表里的写法一致（"Lenovo Tab M10"），这样细分出来的
      名字与 yaml 直接命中的产品**同键合并**，不会各建一个。品牌去重在展示层做。
    """
    if not sku or not title or not is_generic_sku(sku):
        return None
    fn = _REFINERS.get(sku)
    if fn is None:
        return None
    t = _norm(title)
    if not t:
        return None
    try:
        out = fn(t)
    except Exception:  # noqa: BLE001
        return None
    if not out or out == sku:
        return None
    return out


# ★ 兜底名里的「描述词」：说的是"这是什么东西"，不是"哪一款"。
#   nubimetrics 的 fallback 是"品牌 + 剥噪声后的前 4 词"，剥不干净时剩下的就是这些。
#   全库 169 个无数字/无变体的 fallback 名逐条看过（2026-09-04）：
#   含描述词的（Over Ear Hd / Akg Tipo C / Con Cancelacion Ruido / In Ear Con）是桶；
#   **不含**的多是真单品（iPhone Air / iPhone Xr / Studio Buds / Solo Buds /
#   Powerbeats Fit / Vivomove Trend / Fenix E / GTS）—— 只按"无数字"判会把它们
#   赶出趋势入口与竞品候选池。词表在这里是**收窄**判定的闸，不是放宽的检出器：
#   漏标一个桶还有数据判据报告兜着，误标一个真单品没人会发现。
#   刻意不收：buds（Beats Studio Buds）/ anc（Skullcandy Icon ANC）/ stereo
#   （Xiaomi OpenWear Stereo）/ sport（Momentum Sport）/ watch（Moto Watch）—— 它们
#   出现在真产品名里。Sennheiser 的 Accentum Wireless 会被 wireless 命中，认了：
#   Accentum / Accentum Plus / Accentum Wireless 本来就分不清。
_DESCRIPTOR_WORDS = frozenset("""
ear in on over open con sin com sem de para by tipo type usb
cancelacion cancelamento ruido noise cancelling true wireless inalambrico inalambricos
bluetooth tws intraurales intra ouvido fio plugue microfono microfone audio auriculares
audifonos fones headphones earphones jack hifi hi fi generico generic compatible compatibles
originales original kit libre pack equipo sonido telefones profesionales consumer multipunto
abiertos pasiva activa certificado lentes inteligente inteligentes incorporado frecuencia
conectividad portable printer camara modulo espia oem compresor aire ventilador herramientas
maquinas marca marcas mejores cnologia nologia gimbal estabilizador chain fashion app tis atis
core intel amd ryzen ia dr dre deportivos smart full hd fhd uhd hz
""".split())


def _fallback_looks_like_bucket(model_name: str, brand: str | None) -> bool:
    m = model_name.strip()
    if not m:
        return False
    if re.search(r"\d", m):
        return False
    toks = [w.strip("+").lower() for w in m.split()]
    if set(toks) & _VARIANT_WORDS:
        return False
    if brand and m.lower() == brand.strip().lower():
        return True                       # 型号名就是品牌名（"Sony"/"Bose"）：什么都没剥出来
    if toks and toks[0] in ('"', "'"):
        return True                       # 以残留引号开头（'" Xiaomi Mi'）：剥坏了
    norm = [t.translate(str.maketrans("áéíóúüñ", "aeiouun")) for t in toks]
    return bool(set(norm) & _DESCRIPTOR_WORDS)


def bucket_verdict(model_name: str | None, name_source: str | None,
                   skumap_hit: dict | None = None,
                   brand: str | None = None) -> tuple[int, str | None]:
    """一个产品是不是"桶"（系列/兜底名，装着多款不同产品）。单一实现，
    cleaner 入库、tools/mark_buckets.py 打标、tools/resplit_buckets.py 拆桶都用它。

    返回 (is_bucket, reason)：
      ① 权威表系列级 SKU（is_bucket_sku：yaml 显式 generic → 系列基础款白名单
         → 结构判据）→ (1, 'sku_rules:generic')
      ② nubimetrics 兜底名（前 4 词）：无数字、无变体词，且**含描述词**（或名字就是
         品牌名 / 以残留引号开头）→ (1, 'nubimetrics:fallback')
      ③ 白牌桶 → (1, 'whitelabel')
      其余 → (0, None)。**数据判据**（价差 + 标题重归一化出多款）只出报告不落标，
      见 tools/mark_buckets.py --data-report。
    """
    m = (model_name or "").strip()
    if skumap_hit and skumap_hit.get("kind") == "sku" and skumap_hit.get("generic") is not None:
        # 权威表**显式**表态：true 和 false 都以它为准（false 是纠正通道，
        # 只认 true 等于只能往"更像桶"的方向改，误标就永远改不回来）。
        return (1, "sku_rules:generic") if skumap_hit["generic"] else (0, None)
    # ★★ 权威表存的是**含品牌的全名**（"Lenovo Tab" / "Apple iPad"），而
    #   rival_product 把品牌拆成独立列、model_name 只留 "Tab" / "iPad" ——
    #   两种形态都要认，否则判据在"名字里有品牌"时成立、在"品牌拆出去了"时静默失效。
    #   2026-09-07 实测：合并工具把品牌前缀剥掉后，#3720（898 观测/164 标题，
    #   正是用户截图那个桶）当场从 18 个 sku_rules:generic 里消失，只剩 4 个 ——
    #   判据没报错、没变色，只是不再命中（silent-failures 的典型形态）。
    for cand in (m, f"{(brand or '').strip()} {m}".strip()):
        if cand and is_bucket_sku(cand):
            return 1, "sku_rules:generic"
    if (name_source or "") == "nubimetrics/fallback" and _fallback_looks_like_bucket(m, brand):
        return 1, "nubimetrics:fallback"
    if m.startswith("白牌"):
        return 1, "whitelabel"
    return 0, None
