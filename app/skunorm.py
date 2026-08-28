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
