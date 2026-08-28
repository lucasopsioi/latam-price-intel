# -*- coding: utf-8 -*-
"""页面数据抽取：价格解析、JSON-LD、规格识别。

★ 价格解析是本文件最关键的部分。拉美六国数字格式互不兼容：
    BRL/ARS  1.234,56   点=千分位, 逗号=小数点
    MXN/PEN  1,234.56   逗号=千分位, 点=小数点
    CLP/COP  1.234.567  全是千分位，本币无小数位
  同一串 "1.234" 在墨西哥是 1.234，在智利是 1234 —— 差一千倍。
  所以必须按货币走分支，绝不能用一套通用正则糊过去。
"""
from __future__ import annotations

import json
import re
import unicodedata

from bs4 import BeautifulSoup

# 无小数位的货币（智利比索、哥伦比亚比索）
NO_DECIMAL_CURRENCIES = {"CLP", "COP"}
# 逗号做小数点的货币
COMMA_DECIMAL_CURRENCIES = {"BRL", "ARS"}

CURRENCY_SYMBOLS = {
    "MXN": ["$", "MXN", "MN", "M.N."],
    "BRL": ["R$", "BRL"],
    "CLP": ["$", "CLP"],
    "COP": ["$", "COP"],
    "PEN": ["S/", "S/.", "PEN"],
    "ARS": ["$", "ARS"],
}

COUNTRY_CURRENCY = {"MX": "MXN", "BR": "BRL", "CL": "CLP",
                    "CO": "COP", "PE": "PEN", "AR": "ARS"}

# 合理价格区间（本币），用于挡住把"分期金额/评论数/容量"当成价格
# 下界防止把 "12 meses" 读成 12 元；上界防止多打一位（真实样例里出现过）
PRICE_SANITY = {
    "MXN": (300, 200_000),
    "BRL": (80, 60_000),
    "CLP": (8_000, 6_000_000),
    "COP": (60_000, 30_000_000),
    "PEN": (50, 40_000),
    "ARS": (8_000, 30_000_000),
}


# ★ 双重编码（mojibake）的特征串。
#   í(U+00ED) → UTF-8 \xc3\xad → 被误当 latin-1 解码成 "Ã­" → 再编码一次。
#   实测 Falabella 的标题全是这个毛病：「Envío gratis」存成「EnvÃ­o gratis」。
#   后果是连锁的：乱码的促销词没被噪声表认出来 → 当成型号名 →
#   几十个不同商品归一化成同一个"产品" → 比价张冠李戴，
#   出现「139,990 → 139,990 却报降价 39%」这种荒谬结果。
_MOJIBAKE_HINT = re.compile(r"Ã[\x80-\xbf]|Â[\x80-\xbf]|â€|ï¼|Ã­|Ã©|Ã±|Ãº|Ã³")


def fix_mojibake(s: str | None) -> str:
    """把双重编码的文本还原。

    只在检测到 mojibake 特征时才动手 —— 对正常文本做 latin-1 往返
    会把合法的重音字母毁掉。
    """
    if not s or not _MOJIBAKE_HINT.search(s):
        return s or ""
    out = s
    # 可能编码了不止两次，最多还原 3 轮，每轮都要求结果"更干净"
    for _ in range(3):
        try:
            cand = out.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if not _MOJIBAKE_HINT.search(cand):
            return cand
        if cand == out:
            break
        out = cand
    return out


# ---------------------------------------------------------------- 标题界面文案剥离

# ★ 部分渠道把**商品卡上的界面文案**一起抓进了标题，且**叠在商品名前面**。
#   实测（2026-08-27，price_obs 全库 101,946 条标题，已 fix_mojibake）：
#     「Vista Previa …」          13,099 条（12.8%）  Paris.cl 6,692 / Ripley Chile 6,407
#     「Envío gratis …」           7,431 条（ 7.3%）  Falabella CL 4,384 / CO 3,046 / PE 1
#   两者合计占全库 20%。角标还会**叠加**，最长实测三层：
#     「Vista Previa HOT PRICE Envío gratis app APPLE iPhone 17 …」
#   危害有三，且全部静默：
#     ① 型号归一化把角标词当成产品名的一部分 —— 知识页记录过首词统计里
#        vista 208 / bluetooth 71 是坏名字的最大泄漏源
#        （knowledge/lessons/sku-normalization-reuse.md 2026-08-17 那条）；
#     ② 配件判定靠「配件词在不在主语位置」，角标把真词挤到第 3~5 位 ——
#        下面 _ACC_FORM_RE 的主语窗口从 2 放宽到 4 就是为了兜这个，
#        那是补偿不是修复，本函数才是修复；
#     ③ 同一商品在不同渠道的标题对不上，跨渠道比价直接失效。
#
# ★ 词表是**逐条从库里统计出来的**，不是照样例拍的（知识页原话：先统计再改规则）。
#   每条后面的数字 = 该形态在全库标题里作为**开头**出现的条数。
#   收词的判据只有一个：这段文字在讲**页面功能或促销活动**，不在讲商品。
#   刻意没收的几个（宁漏不误杀，见文件末 ★ 待验证）：
#     - 裸 "hot"      —— 库里有 "Hot Blossom"(7) / "Hot Tub"(3) / "Hot 100"(1) 是真商品名
#     - 裸 "tarjeta"  —— 有 "Tarjeta Samsung"(60) / "Tarjeta Gráfica" / "Tarjeta Madre"
#     - 裸 "app"      —— 只作为 "Envío gratis app" 的第二段出现（单独打头 0 条），
#                        所以写成 envío gratis 的可选后缀，不做独立词条
#     - "Nuevo"       —— 开头 113 条里两种意思都有：Acme商城的「新品」角标，
#                        和 Falabella「APPLE Nuevo iPad Pro」的真商品名，分不开就不动
_UI_BADGE_PREFIXES = (
    # Paris.cl / Ripley Chile：商品卡的「快速预览」按钮文案（13,099）
    r"vista previa",
    # Falabella CL/CO/PE：配送权益角标；app = 「仅 App 免运费」（7,431，其中带 app 4,385）
    # ★ app 后面的 \b 不能省：没它会把「Envío gratis APPLE」吃成「LE」
    #   （re.I 下 app 命中 APPLE 的前三个字母）。同文件外的
    #   skunorm._PREFIX 正是这么坏的，实测 249 条型号名以 LE 开头。
    r"env[íi]o gratis(?:\s+app\b)?",
    # 第二层角标（剥掉上面一层后统计得到）
    r"hot price",                      # Paris 大促标（1,260）
    r"rec[íi]belo hoy",                # 当日达角标（950；库里只有 hoy 一种搭配）
    r"env[íi]o r[áa]pido",             # 快速配送角标（1,493）
    r"vendedor destacado",             # 优选卖家角标（375）
    r"tarjeta fest",                   # Falabella 信用卡活动（229）
    # Liverpool：广告位角标（205，全部来自 Liverpool）
    r"patrocinado",
)
# 角标之间可能夹着分隔符，也可能只有空格；用 NBSP 一起吃掉（库里 879 条标题含 NBSP）
_UI_BADGE_PREFIX_RE = re.compile(
    r"^(?:(?:" + "|".join(_UI_BADGE_PREFIXES) + r")[\s\-–—:|]+)+",
    re.I,
)

# ★ 尾部角标。每条都**单独加了守卫**，因为裸形态实测会剥掉真标题内容：
_UI_BADGE_SUFFIX_RES = (
    # ① 折扣角标，Paris.cl 把同一个数字渲染两遍：「… 0 (0) 35% 35%」（5,587 条）
    #    ★ 只认**重复**形态。裸的单个尾部 % 不能剥 —— 库里 5,591 条尾部带 %，
    #      减去重复形态的 5,587 之后剩的 4 条是真商品名：
    #      「Malla Sombra … Sombreado 90% y Bloqueo UV 95%」。
    re.compile(r"[\s\xa0]+\d{1,3}\s*%[\s\xa0]+\d{1,3}\s*%\s*$"),
    # ② 评分 + 评价数角标：「… 4.7 (394)」「… 0 (0)」（1,124 条）
    #    ★ 两道守卫缺一不可，实测各挡下一类误杀：
    #      - 评分位限 0~5 一位小数：挡「SLATE PRO 12.2 (2024)」「IPHONE 8 (2017)」
    #        「10,2 (2023)」这类**屏幕尺寸/型号 + 年份**（尾部形态命中 21 条）；
    #      - 括号内不许是 19xx/20xx 年份：挡「iPad 5 (2017)」这种评分位恰好 ≤5 的，
    #        代价是评价数正好落在 1900~2099 的商品留着角标不剥（保守方向，可接受）。
    re.compile(r"[\s\xa0]+[0-5](?:[.,]\d)?\s*\((?!(?:19|20)\d{2}\))\d{1,6}\)\s*$"),
    # ③ Hiraoka（秘鲁）促销尾巴「… Código 134368 Precio especial」（3,203 条）
    #    只剥促销词，Código 那截是店内货号，属商品信息，不动。
    re.compile(r"[\s\xa0]+[-–—]?\s*precio especial\s*$", re.I),
)

# 剥完必须还剩**至少一个字母** —— 判据取自
# knowledge/lessons/scrape-normalize-silent-corruption.md 第 2 条：
# 「归一化剥出空型号比留着噪声危险得多」（剥空的商品会全挤进同一个产品，
#   造出「同一款降价 69%」这种根本不存在的情报）。纯数字/纯符号同样算没剥住。
_TITLE_HAS_LETTER = re.compile(r"[^\W\d_]")

# 角标实测最多叠三层，留一格余量；有上限是为了任何词表改动都不会变成死循环
_UI_BADGE_MAX_ROUNDS = 4


def strip_ui_chrome(title: str | None) -> str:
    """剥掉标题首尾的渠道界面/促销角标，返回商品名本身。

    渠道无关：不看 channel，只看文本形态 —— 同一句角标换个站也一样剥，
    新接渠道不用再改一遍（这也是它放在 extract 而不是各 adapter 里的原因）。

    ★ 保守优先：剥不动就原样返回；**剥完没有字母就整段退回原文**。
      留着噪声只是名字难看，剥空会让一堆不同商品并成同一个产品。

    幂等：strip_ui_chrome(strip_ui_chrome(s)) == strip_ui_chrome(s)。

    >>> strip_ui_chrome("Vista Previa Apple iPhone 17 256GB Azul 0 (0) 11% 11%")
    'Apple iPhone 17 256GB Azul'
    >>> strip_ui_chrome("Vista Previa")        # 整条都是角标 → 退回原文，不返回空串
    'Vista Previa'
    """
    raw = (title or "").strip()
    stripped = strip_ui_chrome_raw(raw)
    # ★ 自纠：剥完没字母了（空串 / 纯数字 / 纯符号）就整段退回原文
    return stripped if _TITLE_HAS_LETTER.search(stripped) else raw


def strip_ui_chrome_raw(title: str | None) -> str:
    """不带自纠的剥离 —— **只给体检/回填脚本用**，采集端请用 strip_ui_chrome。

    ★ 存在的理由：带自纠的那个函数，在「没东西可剥」和「剥到只剩空」
      两种情况下**返回值完全一样**（都是原文）。不把两者分开，就永远
      回答不了「剥完之后标题为空的有几条」—— 而那个数字正是
      词表有没有收得太宽的唯一体温计。知识页反复警告过这个形态：
      两种完全不同的状态不能长得一模一样，否则唯一的处理办法是干等。

    反过来说：采集端绝不能直接用它 —— 它会真的吐出空串。
    """
    out = (title or "").strip()
    if not out:
        return ""
    for _ in range(_UI_BADGE_MAX_ROUNDS):
        new = _UI_BADGE_PREFIX_RE.sub("", out, count=1)
        for rx in _UI_BADGE_SUFFIX_RES:
            new = rx.sub("", new, count=1)
        if new == out:
            break
        # ★ 分隔符清理只在**真剥掉了角标**之后做 —— 它收的是角标留下的接缝。
        #   无条件跑会去动根本没角标的标题（体检里撞到过：
        #   「Funda de Regalo TEKKNOSHOP-」被削成「TEKKNOSHOP」，685 条），
        #   那是本函数职责之外的改动，注释也没声明过。
        new = new.strip(" \t\xa0-–—:|")
        out = new
    return out


def parse_price(raw: str | float | int | None, currency: str) -> float | None:
    """把页面上的价格字符串解析成数字。按货币选小数点规则。

    >>> parse_price("R$ 1.234,56", "BRL")   -> 1234.56
    >>> parse_price("$1.234.567", "CLP")    -> 1234567.0
    >>> parse_price("$21,999.00", "MXN")    -> 21999.0
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    s = unicodedata.normalize("NFKC", str(raw))
    # 去掉货币符号、空格（含不换行空格）、字母
    s = re.sub(r"[^\d.,\-]", "", s.replace("\xa0", " "))
    if not s or not re.search(r"\d", s):
        return None

    cur = (currency or "").upper()
    has_dot, has_comma = "." in s, "," in s

    try:
        if has_dot and has_comma:
            # 两种都有：最后出现的那个是小数点
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")     # 1.234,56 → 1234.56
            else:
                s = s.replace(",", "")                        # 1,234.56 → 1234.56
        elif has_comma:
            if cur in COMMA_DECIMAL_CURRENCIES:
                # 巴西/阿根廷：逗号是小数点，但 1,234 这种 3 位的其实是千分位
                s = s.replace(",", ".") if len(s.split(",")[-1]) <= 2 else s.replace(",", "")
            else:
                s = s.replace(",", "")
        elif has_dot:
            tail = s.split(".")[-1]
            if cur in NO_DECIMAL_CURRENCIES:
                s = s.replace(".", "")                        # CLP/COP：点全是千分位
            elif cur in COMMA_DECIMAL_CURRENCIES:
                s = s.replace(".", "")                        # BRL/ARS：点是千分位
            elif len(tail) == 3 and len(s.replace(".", "")) > 4:
                s = s.replace(".", "")                        # 1.234.567 明显是千分位
            # 其余情况点就是小数点，原样
        val = float(s)
    except ValueError:
        return None

    if cur in NO_DECIMAL_CURRENCIES:
        val = round(val)
    return val if val > 0 else None


def price_is_sane(value: float | None, currency: str) -> bool:
    """价格是否落在合理区间。不合理的不是丢弃，而是标记出来交价格审计 Agent。"""
    if value is None:
        return False
    lo, hi = PRICE_SANITY.get((currency or "").upper(), (0.01, 1e12))
    return lo <= value <= hi


# ---------------------------------------------------------------- JSON-LD

def _walk_jsonld(node, found: list) -> None:
    if isinstance(node, list):
        for x in node:
            _walk_jsonld(x, found)
        return
    if not isinstance(node, dict):
        return
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    if "Product" in types:
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            item = {
                "title": str(node.get("name") or "")[:250],
                "sale_price_raw": offers.get("price") or offers.get("lowPrice"),
                "list_price_raw": (offers.get("highPrice")
                                   or (node.get("priceSpecification") or {}).get("price")
                                   if isinstance(node.get("priceSpecification"), dict) else None),
                "currency": offers.get("priceCurrency") or "",
                "url": offers.get("url") or node.get("url") or "",
                "sku": node.get("sku") or node.get("mpn") or "",
                "brand": _brand_name(node.get("brand")),
                "availability": str(offers.get("availability") or ""),
                "seller_name": _seller_name(offers.get("seller")),
                "condition": str(offers.get("itemCondition") or ""),
            }
            if item["title"] and item["sale_price_raw"] is not None:
                found.append(item)
    for key in ("@graph", "itemListElement", "mainEntity", "item", "hasVariant"):
        if key in node:
            _walk_jsonld(node[key], found)


def _brand_name(b) -> str:
    if isinstance(b, dict):
        return str(b.get("name") or "")[:60]
    return str(b or "")[:60]


def _seller_name(s) -> str:
    if isinstance(s, dict):
        return str(s.get("name") or "")[:80]
    return str(s or "")[:80]


def extract_jsonld_products(html: str) -> list[dict]:
    """解析页面里的 schema.org Product。大量拉美零售站都带，是最可靠的一路。"""
    if not html:
        return []
    found: list[dict] = []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except Exception:  # noqa: BLE001
            continue
        _walk_jsonld(data, found)

    seen, out = set(), []
    for p in found:
        k = (p["title"].lower(), str(p.get("sale_price_raw")))
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


# ---------------------------------------------------------------- 规格识别

# 单位在拉美标题里常常只写一次（"8+256GB"），所以第一个单位必须可选。
# 分隔符只认 + 和 /：认 '-' 会把 "Galaxy S24-256" 这类型号切错。
_RAM_ROM_PAT = re.compile(
    r"(\d{1,4})\s*(gb|tb|g|t)?\s*(?:ram)?\s*[+/]\s*(\d{1,4})\s*(gb|tb|g|t)\b", re.I)
# 允许 1 位数字，"1TB" 才不会漏；下面用 >=16GB 的阈值挡住把 RAM 当容量
_ROM_ONLY_PAT = re.compile(r"\b(\d{1,4})\s*(gb|tb)\b", re.I)
_RAM_ONLY_PAT = re.compile(r"\b(\d{1,2})\s*gb\s*(?:de\s*)?ram\b", re.I)
_SCREEN_PAT = re.compile(r"\b(\d{1,2}[.,]\d{1,2}|\d{1,2})\s*(?:\"|''|pulgadas|polegadas|inch)", re.I)


def parse_ram_rom(title: str) -> tuple[int | None, int | None]:
    """从商品标题解析 RAM/ROM。拉美电商标题格式很杂：
    "Galaxy S24 256GB 8GB RAM" / "8+256" / "12GB/512GB" / "1TB"
    """
    if not title:
        return None, None
    t = title.replace("＋", "+")

    m = _RAM_ROM_PAT.search(t)
    if m:
        v1, u1, v2, u2 = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        if u1 and u1.lower().startswith("t"):
            v1 *= 1024
        if u2 and u2.lower().startswith("t"):
            v2 *= 1024
        # 防倒置："8+256" 正常，"256GB+8GB" 是反着写的，小的那个才是 RAM
        ram, rom = (v1, v2) if v1 <= v2 else (v2, v1)
        return (ram if ram <= 32 else None), rom

    ram = None
    mr = _RAM_ONLY_PAT.search(t)
    if mr:
        ram = int(mr.group(1))

    rom = None
    for m2 in _ROM_ONLY_PAT.finditer(t):
        val = int(m2.group(1))
        if m2.group(2).lower() == "tb":
            val *= 1024
        # 容量候选取最大的（标题里 8GB RAM 和 256GB 同时出现时取 256）
        if val >= 16 and (rom is None or val > rom):
            rom = val
    return ram, rom


def parse_screen_size(title: str) -> float | None:
    if not title:
        return None
    m = _SCREEN_PAT.search(title)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return v if 3.0 <= v <= 20.0 else None


_COLOR_WORDS = {
    "negro": "黑", "preto": "黑", "black": "黑", "midnight": "黑",
    "blanco": "白", "branco": "白", "white": "白", "starlight": "白",
    "azul": "蓝", "blue": "蓝", "verde": "绿", "green": "绿",
    "gris": "灰", "cinza": "灰", "gray": "灰", "grey": "灰", "grafito": "灰",
    "dorado": "金", "dourado": "金", "gold": "金",
    "plata": "银", "prata": "银", "silver": "银", "titanio": "钛",
    "morado": "紫", "roxo": "紫", "purple": "紫", "violeta": "紫",
    "rosa": "粉", "pink": "粉", "rojo": "红", "vermelho": "红", "red": "红",
}


def parse_color(title: str) -> str | None:
    if not title:
        return None
    low = title.lower()
    for word in _COLOR_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", low):
            return word
    return None


# ---------------------------------------------------------------- 成色/捆绑

_REFURB_MARKERS = ["reacondicionado", "recondicionado", "refurbished", "renewed",
                   "seminuevo", "semi-novo", "usado", "used", "open box", "caja abierta"]
_BUNDLE_MARKERS = ["combo", "kit", "pack", "bundle", "2 unidades", "duo", "x2",
                   "leve 2", "lleve 2"]
# ★ 运营商合约机：Falabella 智利大量挂「Equipo + Plan」，标的价是**签约价**，
#   不是裸机零售价。它和零售价不是一个计价口径 —— 混进来就会出现
#   "iPhone 17 在 Falabella 只卖零售价的三分之一" 这种假价差，
#   顺带把该型号的价格基线整体拽低。这里当捆绑处理，从价格分析里排除。
#   只认「plan 跟设备/线路一起出现」的写法，避免误伤正常用词。
_CARRIER_PLAN_MARKERS = [
    "equipo + plan", "equipo+plan", "equipo mas plan", "con plan", "+ plan",
    "plan movil", "plan móvil", "portabilidad", "con linea", "con línea",
    "com plano", "plano + aparelho", "aparelho + plano", "com fidelidade",
]
# "主商品 + 配件" 是捆绑最常见的形态，配件词穷举不完，所以用「加号后面跟配件类词」判
_BUNDLE_ACCESSORY_WORDS = [
    "funda", "case", "capa", "cover", "teclado", "keyboard", "pen", "lapiz", "lápiz",
    "caneta", "mica", "pelicula", "película", "protector", "cargador", "carregador",
    "audifonos", "audífonos", "fone", "correa", "pulseira", "soporte", "adaptador",
    "memoria", "microsd", "smartwatch", "reloj", "bocina", "parlante",
]


def detect_condition(title: str, extra: str = "") -> str:
    blob = f"{title} {extra}".lower()
    return "refurb" if any(m in blob for m in _REFURB_MARKERS) else "new"


# ★ 页面框架文案（筛选面板 / 导航 / 页头），不是商品。
#   实测 Falabella 的**筛选侧栏**被通用卡片启发式当成了商品卡：
#     标题 "Tipo de Entrega Envío a domicilio Gratis Llega mañana Retiro en un punto…"
#     价格 100,000 / 300,000 / 500,000 COP —— 那是**价格筛选滑块的档位值**，
#     还有 52 / 100 这种滑块最小值。
#   这类行不会报错，只会静默进价格基线：一台"手机"52 比索。
#   判据用"筛选器词汇同时出现多个"，单个词（如 "categoría"）在正常标题里也会有。
_CHROME_WORDS = [
    "tipo de entrega", "llega mañana", "llega manana", "retiro en un punto",
    "retira mañana", "retira manana", "mejores marcas", "ordenar por",
    "inicia sesión", "inicia sesion", "mi cuenta", "vende en", "ingresa tu ubicación",
    "ingresa tu ubicacion", "categoría tecnología", "categoria tecnologia",
    "filtrar por", "ver todos los", "envío a domicilio gratis",
]


def looks_like_page_chrome(title: str) -> bool:
    """标题是不是页面框架文案（筛选面板/导航），而不是商品。"""
    low = (title or "").lower()
    if not low:
        return False
    return sum(1 for w in _CHROME_WORDS if w in low) >= 2


def detect_bundle(title: str) -> bool:
    low = (title or "").lower()
    if any(m in low for m in _BUNDLE_MARKERS):
        return True
    if any(m in low for m in _CARRIER_PLAN_MARKERS):
        return True
    # 加号之后出现配件词 → 捆绑（"Galaxy Tab S9 + Keyboard Cover"）
    if "+" in low:
        tail = low.split("+", 1)[1]
        return any(w in tail for w in _BUNDLE_ACCESSORY_WORDS)
    return False


# ---------------------------------------------------------------- 整机 / 配件

# ★ 葡语/西语「配件词 + para/de」句法规则 —— **单一实现，三处消费**：
#   skumap.is_accessory（权威表的前置闸）、detect_product_kind、
#   tools/backfill_accessory_kind.py（历史回填）都必须用这一份。
#   分开写就会出现"分类器修好了、回填用的是另一套规则"的分叉。
#
#   背景（2026-08-27）：Fast Shop 巴西的「Capa para Tablet Acme Slate Tab」
#   「Película para ACME Slate 11.5」全被标成 device —— 权威表的配件词
#   移植自用户 PowerQuery，天生只有西语+英语，葡语词一个都拦不住，
#   往下走命中 Slate Tab 的 SKU 规则直接判整机，把Acme巴西平板 ASP 拉到 20 美元。
#
#   形态刻意收窄，宁漏不误杀（窗口的当前值以代码为准，见下面两条 ★ 的调整记录）：
#     - 配件名词必须在**主语位置**（标题前 4 个词以内）——西/葡标题主体在前，
#       「Tablet Samsung com capa de brinde」是带壳平板，capa 不在主语位置，不杀；
#     - 名词后 8 个词以内出现依附标记 para / p/ / de / do / da / for / compatible con；
#     - 佩戴与支撑类（correa/pulseira/banda/malla/base）只认 para 类标记，
#       且佩戴词后接词不得是 inteligente/smart ——「Pulsera/Pulseira Inteligente」
#       是真手环，误杀一次等于把整条穿戴产品线从价格分析里清零；
#     - capinha（葡语手机壳专称，无歧义）在主语位置可免标记。
#   ★ 名词表与窗口宽度都是**实测调出来的**，不是拍的：
#     首版只收用户点名的那批词、介词窗口 3 个词，回填掉 658 行后残留体检
#     （平板品类里 <400 BRL 的"整机"）又顶出四类同形态漏网：
#       `Cabo Lightning … para iPad`（葡语线缆词没收）、`Suporte Metálico 360 Para Tablet`、
#       `Vidrio Templado para iPad Air`（多词名词）、
#       `Caneta Stylus Bettdow Touch Screens POM Nib para Android`（介词在第 6 个词）。
#     窗口放宽到 8 个词（介词最远可在名词后第 9 个词）是安全的，因为真正挡误杀的
#     是 _ACC_FORM_LEAD_VETO（名词必须在主语位置、前面不能有设备词），不是窗口宽度；
#     放宽后的**增量命中已逐条人查**，见 tests/test_accessory_kind.py 的定点用例。
#   ★ 主语窗口同理从 2 放到 4：营销前缀会把配件词挤出前两个词
#     （`Envío gratis GENERICO FUNDA ESTUCHE…`、`Kepuch 2 Paquetes Vidrio Templado…`），
#     而 Falabella 的 "Envío gratis app" 前缀是本库记录在案的已知噪声。
#     ★ 放宽主语窗口前**先加固 veto 表**（补齐平板/手机产品线名）——
#       顺序反了就会放 `Xiaomi Pad 6 Funda de regalo` 这类整机进配件桶。
_ACC_FORM_NOUN = (
    r"capas?|capinhas?|pel[ií]culas?|fundas?|micas?|estuches?|carcasas?|"
    r"protector(?:es|a|as)?|protetor(?:es|a|as)?|cargador(?:es)?|"
    r"carregador(?:es)?|teclados?|canetas?|l[aá]pi(?:z|ces)|stylus|"
    r"cases?|covers?|forros?|"
    # —— 残留体检补入 ——
    r"cabos?|cables?|soportes?|suportes?|adaptador(?:es)?|adapters?|"
    r"vidrios? templados?|vidros? temperados?|cristales? templados?|"
    r"smart covers?|smart folios?|cubre teclados?"
)
# 佩戴 / 支撑类：语义比上面弱，只认 para 类标记 —— 配 de/do/da 会误伤
#   （"banda de rodaje"、"base de datos" 这类；佩戴词还要防 Pulsera Inteligente）
_ACC_FORM_WEAR = r"correas?|pulseiras?|bandas?|mallas?|straps?|bases?"
_ACC_FORM_MARK_FULL = (r"para\b|p/|de\b|do\b|da\b|for\b|"
                       r"compatible con\b|compat[ií]vel com\b")
_ACC_FORM_MARK_PARA = (r"para\b|p/|for\b|compatible con\b|compat[ií]vel com\b")
_ACC_FORM_RE = re.compile(
    rf"^(?P<lead>(?:\S+\s+){{0,4}}?)"
    rf"(?:(?P<noun>{_ACC_FORM_NOUN})\b(?:\s+\S+){{0,8}}?\s+(?:{_ACC_FORM_MARK_FULL})"
    rf"|(?P<wear>{_ACC_FORM_WEAR})\b(?!\s+(?:inteligente|smart))"
    rf"(?:\s+\S+){{0,8}}?\s+(?:{_ACC_FORM_MARK_PARA})"
    rf"|(?P<bare>capinhas?)\b)"
)
# 主语位置的前导词里不得有设备词：「Tablet Acme ... Capa」打头的是平板。
# ★★ 这张表是**唯一**挡误杀的东西（窗口宽度不是），所以它漏收一个厂商叫法
#   就等于放一台整机进配件桶。知识页原话：设备名词表漏收厂商叫法 = 整机被判成配件。
#   产品线名必须收全，通用词覆盖不了：`Xiaomi Pad 6 Funda de regalo` 里
#   没有 tablet/ipad/galaxy 任何一个词，只有产品线名 `pad`。
_ACC_FORM_LEAD_VETO = (
    # 通用品类词
    "tablet", "tableta", "celular", "smartphone", "telefono", "teléfono",
    "notebook", "laptop", "chromebook", "e-reader", "ereader",
    # 平板/电脑产品线名（厂商怎么叫就收什么）
    "ipad", "slate", "galaxy", "redmi pad", "xiaomi pad", "poco pad",
    "mi pad", "honor pad", "lenovo tab", "ideapad", "thinkpad", "macbook",
    "imac", "surface", "kindle", " pad ", "pad se", "pad pro", "pad air",
    "tab a", "tab m", "tab s", "tab p",
    # 手机产品线名
    "iphone", "redmi note", "moto g", "moto e", "nimbus ", "astra ", "vega ",
    # 穿戴/音频
    "reloj", "watch", "smartwatch", "smartband", "smart band",
    "fone", "audifono", "audífono", "auricular", "earbud", "airpod", "buds",
)


def accessory_para_form(title_low: str) -> str | None:
    """「主语位置配件词 + para/de」句法判定。入参须已 lower（重音去不去都认）。

    命中返回人话依据，未命中返回 None。高置信规则：只在这个形态上开火。
    """
    m = _ACC_FORM_RE.match(title_low or "")
    if not m:
        return None
    # 两端补空格：veto 里带空格的条目（" pad "）在词首/词尾也要能命中，
    # 否则「Pad 6 Funda de …」这种以产品线名开头的标题守卫会失效。
    lead = f" {m.group('lead') or ''} "
    if any(w in lead for w in _ACC_FORM_LEAD_VETO):
        return None
    word = m.group("noun") or m.group("wear") or m.group("bare")
    return f"主语位置配件词「{word}」+ para/de 依附标记 → 配件"


# 配件词（西/葡/英）。手机类目下混着一堆保护壳、贴膜、表带、充电器。
# ★ 佩戴类裸词（correa/pulseira/banda）保留 —— 「Correa cruzada – Rosa pálido」
#   这类表带标题没有 para/de，删裸词会放跑它（定点回归钉着）。
#   「Pulsera/Pulseira Inteligente」真手环的保护不靠删词，靠 _DEVICE_WORDS
#   收录 pulsera/pulseira inteligente 后在同位置平局时判整机（位置裁决）。
_ACCESSORY_WORDS = [
    # 保护类
    "funda", "carcasa", "case", "capa", "capinha", "protector", "protetor",
    "mica", "cristal templado", "vidrio templado", "película", "pelicula",
    "screen protector", "smart folio", "keyboard cover", "flip cover",
    "estuche",
    # 佩戴类
    "correa", "pulseira", "banda", "strap", "malla para",
    # 供电类
    "cargador", "carregador", "charger", "cable", "adaptador", "adapter",
    "power bank", "bateria externa", "batería externa",
    # 其它
    "soporte", "suporte", "holder", "stand", "lápiz", "lapiz", "stylus",
    "caneta", "teclado",
    "s pen", "apple pencil", "memoria", "micro sd", "tarjeta sd",
]
# 整机信号：出现这些说明是主设备，优先于配件词
_DEVICE_WORDS = [
    "smartphone", "celular", "teléfono", "telefono", "iphone", "galaxy s",
    "galaxy a", "galaxy z", "galaxy note", "redmi", "poco ", "moto g",
    "moto e", "moto edge", "razr", "tablet", "ipad", "galaxy tab", "slate",
    # ★ 平板产品线名必须收全 —— 漏一个，这条线的整机只要标题里出现配件词
    #   （送壳/送膜/带键盘）就会被判成配件。实测漏的就是这几个：
    #   `Xiaomi Pad 6 Funda de regalo` 里没有 tablet/ipad/galaxy 任何一个词，
    #   dev_pos=-1、acc_pos=13 → 整机进配件桶，且不报错。
    #   （通用词覆盖不了品牌词，知识页 host-vs-accessory-classification 原话。）
    "xiaomi pad", "redmi pad", "poco pad", "mi pad", "honor pad", "lenovo tab",
    "idea tab", "ideatab", "surface pro", "kindle",
    "laptop", "notebook", "macbook", "thinkpad", "ideapad",
    "smartwatch", "reloj inteligente", "watch series", "galaxy watch",
    # ★ 穿戴设备的厂商叫法/西葡叫法必须正面收录，否则会被配件词抢走
    #   （知识页：设备名词表漏收厂商叫法 = 整机被判成配件）。
    #   pulsera/pulseira inteligente 与配件裸词 pulseira 同位置命中时，
    #   位置平局判整机 —— 这就是「Pulseira Inteligente」真手环的防误杀线。
    "smart band", "smartband", "mi band", "pulsera inteligente",
    "pulseira inteligente", "banda inteligente",
    "audífonos", "audifonos", "auriculares", "earbuds", "airpods", "buds",
    "fone de ouvido", "headphone",
]
# "配件 para 设备" 的守卫词：命中后只看**前半段**是不是配件
_FOR_GUARD = re.compile(r"\b(para|for|compatible con|compatível com)\b", re.I)


def detect_product_kind(title: str) -> tuple[str, str]:
    """判定标题是【整机】还是【配件】。返回 (kind, 依据)。

    ★ 管线顺序本身就是规则，不能随便调换（移植自上一代项目的 83 项单测结论）：

      0. 「主语位置配件词 + para/de」句法规则（accessory_para_form）——
         最强信号最先走，且与 skumap、历史回填共用同一份实现。
      1. `para/for/compatible` 守卫 —— 必须在整机判定**之前**。
         配件标题里必然出现设备名（"Funda para iPhone 17"），
         不守卫就会被设备名拐走判成整机。
      2. 整机信号 —— 必须在配件词**之前**。
         这样"捆绑算整机"自动成立："Galaxy Tab S11 + Keyboard Cover"
         先命中 Tab，根本走不到配件那步，不需要特判。
      3. 配件词
      4. 兜底 unknown（**不猜**，交给价格审计用基线判）
    """
    t = (title or "").lower().strip()
    if not t:
        return "unknown", "标题为空"

    # ⓪ 句法规则：主语位置配件词 + 依附标记（葡/西），高置信直接定案
    why0 = accessory_para_form(t)
    if why0:
        return "accessory", why0

    # ① 守卫：只看 para/for 前半段
    m = _FOR_GUARD.search(t)
    if m:
        head = t[:m.start()]
        for w in _ACCESSORY_WORDS:
            if w in head:
                return "accessory", f"「{w}」+ 「{m.group(1)}」结构 → 配件（前半段是配件名）"

    # ② ★ 比位置，不是"整机一律优先"。
    #    简单的"整机信号优先于配件词"太粗：
    #      "Mica + Funda Samsung Galaxy A71" 命中 galaxy a → 判成整机（错，是贴膜+壳）
    #    但改成"配件优先"又会破坏「捆绑算整机」：
    #      "Galaxy Tab S11 + Keyboard Cover" 应判整机
    #    两者的差别就是**谁先出现** —— 标题以什么打头，它就是什么。
    dev_pos = min((t.find(w) for w in _DEVICE_WORDS if w in t), default=-1)
    acc_pos = min((t.find(w) for w in _ACCESSORY_WORDS if w in t), default=-1)

    if dev_pos >= 0 and acc_pos >= 0:
        if acc_pos < dev_pos:
            return "accessory", f"配件词出现在整机词之前（{acc_pos} < {dev_pos}）→ 配件"
        return "device", f"整机词出现在配件词之前（{dev_pos} < {acc_pos}）→ 整机（含捆绑装）"
    if dev_pos >= 0:
        return "device", "命中整机信号"
    if acc_pos >= 0:
        return "accessory", "命中配件词"

    return "unknown", "标题无明确整机/配件信号，留给价格审计按基线判"


# ------------------------------------------------------- 品类交叉校验（采集上下文 vs 标题）

# ★ 病根：price_obs.category_code 存的是「**当时在抓哪个品类页**」
#   （collector._persist 直接把采集单元的 category 写进去），不是商品本身的品类。
#   搜索串味、品类页混排、渠道把耳机塞进平板页 —— 都会让一条真设备落错桶。
#   实测平板桶里躺着 339 比索的「XIAOMI Audífonos Buds 6 Play」，把平板价格下沿拽穿。
#   分类器判 device 是**对的**（它确实是台设备），错的只是品类。
#   知识页 sku-normalization-reuse.md 第 7 条：产品线名比采集上下文可靠。
#
# ★ 与 skunorm.guess_category 的分工（**不是重复实现**）：
#     skunorm.guess_category —— 输入是**归一化后的型号名**，^ 锚定产品线。
#     本函数              —— 输入是**原始标题**，通用词 + 产品线名，任意位置。
#   两条路的输入不同：型号名可能已经是有损输出（"5. 3 Honor"、AirPods 被归一化成
#   "iPad Air"），而标题是事实来源。实测两者能同时表态的 1412 条里 1403 条一致，
#   9 条分歧全部是 guess_category 把 Lenovo **Legion Tab / Yoga Tab**（游戏平板）
#   按 legion/yoga 判成 pc —— 即分歧处是那边错。故这里不复用它，也不改它。

_CAT_EVIDENCE = {
    # 通用词覆盖不了品牌词，两类都要收（同 _DEVICE_WORDS 那条教训）。
    # ★ tableta 只认单数：西语「60 tabletas」是**药片**，
    #   实测 "Conecta Gold 600mg 60 Tabletas" / "Caltrón 600+D de 60 tabletas"
    #   就躺在 phone 桶里，认复数会把保健品搬进平板品类。
    # ★★ 每一条都必须**两头都有 \b**。少了前面那个 \b 是词内命中，
    #   而词内命中不会报错、只会静默给出错误品类。实测抓到两条：
    #     reno\s*\d  命中「Qualcomm **Adreno 12**」—— Adreno 是笔记本的 GPU 名，
    #                25 条 ASUS/LENOVO 笔记本因此带上了"手机证据"；
    #     nimbus/magic/poco/tab 同形，只是暂时没撞上。
    #   例外只有 buds（见下），它**故意**允许词内命中。
    "tablet": (r"\btablets?\b|\btableta(?!s)\b|\bipad\b|\bgalaxy\s*tab\b|"
               r"\btab\s*[as]\d|\bslate\b|"
               r"\b(?:redmi|xiaomi|mi|honor|poco|oppo|nokia)\s*pad\b|"
               r"\blenovo\s*tab\b|\bidea\s*tab\b|\bideatab\b|\bfire\s*hd\b|"
               r"\bsurface\s*pro\b"),
    "phone": (r"\bcelular(?:es)?\b|\bsmartphones?\b|\btelefono\b|\btelefone\b|"
              r"\btelemovel\b|\biphone\b|\bgalaxy\s*[asmfz]\d|\bgalaxy\s*note\b|"
              r"\bgalaxy\s*z\s*(?:flip|fold)|\bredmi\s*note\b|\bmoto\s*[ge]\d|"
              r"\bmoto\s*edge\b|\brazr\b|\bnimbus\s*\d|\breno\s*\d|\bmagic\s*\d|"
              r"\bpoco\s*[xmfc]\d"),
    # ★ buds 是唯一**故意**不加前置 \b 的：厂商把它粘在词里 ——
    #   Ear/Free/Galaxy/Redmi Buds。写成 \w*buds\b 才能一条通吃，
    #   加了前置 \b 会漏掉 SonicBuds（实测Acme音频的主力命名）。
    "audio": (r"\w*buds\b|\bearbud\b|\baudifonos?\b|\bauriculares?\b|\bairpods\b|"
              r"\bheadphones?\b|\bearphones?\b|\bfones?\s*de\s*ouvido\b|"
              r"\bin\s*ear\b|\bintraural\b|\bparlante\b|\bbocina\b|\baltavoz\b|"
              r"\bcaixa\s*de\s*som\b|\bsoundbar\b|\bbarra\s*de\s*sonido\b"),
    # ★ mi band 同理留半个口子：「Xiaomi Band 10」里 mi band 是词内命中，
    #   加死 \b 就漏。用 (?:xiao)? 显式收这一种，而不是整条放开。
    "wearable": (r"\bsmartwatch(?:es)?\b|\breloj(?:es)?\s*inteligente\b|"
                 r"\brelogio\s*inteligente\b|\bapple\s*watch\b|\bgalaxy\s*watch\b|"
                 r"\bgalaxy\s*fit\b|\bwatch\s*(?:gt|fit|ultra|se)\b|"
                 r"\b(?:xiao)?mi\s*band\b|\bsmart\s*band\b|\bsmartband\b|"
                 r"\bamazfit\b|\bpulsera\s*inteligente\b|"
                 r"\bpulseira\s*inteligente\b|\bbanda\s*inteligente\b"),
    "pc": (r"\blaptops?\b|\bnotebooks?\b|\bportatil\b|\bcomputador(?:a|es)?\b|"
           r"\bmacbook\b|\bimac\b|\bthinkpad\b|\bideapad\b|\bchromebook\b|"
           r"\ball\s*in\s*one\b|\bultrabook\b"),
}
_CAT_EVIDENCE_RE = {k: re.compile(v) for k, v in _CAT_EVIDENCE.items()}

# ★★ 这条闸是整个规则的命根子。**证据只认标题头部** —— 出现在下列标记之后的
#   设备名不是本商品，而是「送的」「配的」「兼容的」「卖家的名字」。
#   不加这条闸，实测 360 条会被改错，且全是成簇的系统性错误：
#     赠品   "Honor 600E 512GB 5G **Gratis** Honor Play10+audifonos" → 手机被判成音频
#     捆绑   "Vivo Y11D 256Gb 4G **Gratis** Buds+Speaker"            → 手机被判成音频
#            "ACME Vega 70 **Bundle** Sonicbuds Pro 3"              → 手机被判成音频
#     兼容   "Magic Keyboard **para** iPad Pro 13"                   → 配件被判成平板
#            "Trípode **para** Celular, Selfie Stick"                → 配件被判成手机
#            "Garantía Extendida **para** Laptop 12 Meses"           → 保修被判成 PC
#   ★ por 也在闸里：卖家名会注入假证据 —— 「…**Por** FALABELLA」。
#     知识页原话「有个卖家就叫 Cablex，cable 子串一命中整台手机被判成配件」，
#     同一个坑换个方向再踩一次。用同一条闸解决，不另抄一份卖家尾巴正则。
#   ★ 闸只会**移除**证据、不会重排，所以它永远朝保守方向失败：
#     切多了 ⇒ 这条不改（安全）；切少了才会改错。
#   ★ 连词单列就够，不写全短语：「compatible con」里的 con 更靠前，
#     先命中就先切 —— 更短，也更保守。
_CAT_CUT = re.compile(
    r"\b(?:para|for|compatible|compativel|con|com|e|y|mas|por|"
    r"gratis|gratuito|regalo|obsequio|incluye|inclui|incluido|"
    r"brinde|bundle|combo|kit)\b|\bp/|\+")

# 玩具：确实叫 tablet、也确实是真商品，但不是消费电子，不该进平板价格基线。
# 实测「Juguete para bebé didáctico Winfun: Tablet I-Fun pad」49,900 COP 就是 COP 的下沿。
#
# ★ 只收 juguete/brinquedo（"玩具"本身），**刻意不收 infantil / kids / niños / educativo**。
#   一度收了，实测多拦下 318 条 —— 但它们是**真安卓平板**
#   （"Tablet Infantil Multi Kid Pad 64GB Wi-Fi Android 13"，BR 157 条 323~1,299 BRL，
#   品类中位 1,700）。那是低端子段，不是非消费电子；一律排除等于让情报看不见
#   竞品在儿童机市场的动作。用户 2026-08-27 定的口径：当低端段保留。
#   ⇒ 判据是"这是不是玩具"，不是"这是不是给小孩用的"。全库真玩具仅 1 条。
_CAT_TOY = re.compile(r"\bjuguete\b|\bbrinquedo\b")

_CAT_ACCENTS = str.maketrans("áàäâãéèëêíìïîóòöôõúùüûñç", "aaaaaeeeeiiiiooooouuuunc")


def _cat_prep(title: str) -> str:
    """品类证据的判定形态：剥界面角标 → 修乱码 → 小写 → 去重音 → 标点归一。

    ★★ 必须先过 strip_ui_chrome，而且**恰恰是因为 _CAT_CUT 的存在**：
      Falabella 的配送角标写作「Envío gratis app …」，里面的 gratis 与赠品标记
      「… Gratis Audifonos」**是同一个词**。不剥角标，_CAT_CUT 会在第 6 个字符
      就切掉，头部只剩 "envio" ⇒ 658 条真耳机
      （"Envío gratis app HONOR Audífonos In Ear…"）全部判成"无证据"而漏掉。
      ★ 我一度以为「界面噪声只是让位置后移、不影响相对顺序，所以可以不剥」——
        错的：噪声里含闸门词时，它不是把证据后移，是把头部整个截断。
      本文件上面那套角标剥离正是为这类形态写的（连 Vista Previa / Recíbelo hoy
      也一起剥掉，那两个 skunorm.pre_clean 不认），所以就地引用，不跨模块抄。
    """
    t = strip_ui_chrome(fix_mojibake(title or "")).lower().translate(_CAT_ACCENTS)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s+/]", " ", t)).strip()


def category_evidence(title: str, head_only: bool = True) -> list[tuple[int, str, str]]:
    """标题里的品类证据 [(位置, 品类, 命中词)]，按位置升序，每类只取最早一次。

    head_only=True 时只看 _CAT_CUT 之前的头部（默认，见上面 ★★）。
    """
    t = _cat_prep(title)
    if head_only:
        m = _CAT_CUT.search(t)
        if m:
            t = t[:m.start()]
    return sorted((m.start(), cat, m.group(0))
                  for cat, rx in _CAT_EVIDENCE_RE.items() if (m := rx.search(t)))


def crosscheck_category(title: str, ctx_category: str | None) -> tuple[str, str | None, str]:
    """采集上下文品类 vs 标题证据。返回 (结论, 目标品类, 依据)。

    结论 ok      —— 标题证据支持采集品类，或标题说不出话（**不猜**）
         fix     —— 标题只指向另一个品类，可以改判
         pending —— 标题指向多个品类，人工定夺；该行不该进任何品类的价格分析

    ★ 只在「本品类**毫无**证据 + 他类有明确证据」时才动 —— 两侧都设闸门。
      单侧闸门的教训见知识页：「取最短标题/取最长标题都是错的启发式」，
      只改「当前可判为坏」且「新值可判为好」的。
    """
    if not ctx_category:
        return "ok", None, "无采集品类，不判"
    # ★ 玩具闸必须在最前：玩具平板的标题是「Juguete **para** bebé … Tablet I-Fun pad」，
    #   para 会把头部切成 "juguete" ⇒ 走到"无证据 → 不动"就漏了。
    #   它确实叫 tablet、确实是台真商品，但不是消费电子，不该进平板价格基线。
    if _CAT_TOY.search(_cat_prep(title)):
        return "pending", None, "玩具/教具，非消费电子 → 待定"
    ev = category_evidence(title)
    if not ev:
        return "ok", None, "标题无任何品类证据 → 不动（不猜）"
    cats = {c for _, c, _ in ev}
    if ctx_category in cats:
        return "ok", None, f"标题含本品类证据「{ev[0][2]}」"
    if len(cats) > 1:
        detail = "、".join(f"{c}「{w}」" for _, c, w in ev)
        return "pending", None, f"头部同时指向多个品类（{detail}）→ 待定"
    pos, target, word = ev[0]
    return "fix", target, f"「{word}」@{pos} 是 {target} 的明确设备名，且无 {ctx_category} 证据"



# ---------------------------------------------------------------- 卖家类型


def detect_seller_type(seller_name: str | None, page_text: str = "",
                       channel_default: str = "unknown", brand: str = "",
                       adapter: str = "", official_store_id=None) -> tuple[str, str]:
    """判定卖家类型。返回 (粗分类型, 依据)。

    ★ 真正的判定逻辑在 seller.py。这里只是保持旧调用点的签名兼容。

    旧实现是在整页文本里搜 "liverpool"/"coppel"/"sears" 这些关键词，
    而这些词在对应站点的页头页脚 logo alt 里到处都是 ——
    结果该站所有商品（含第三方卖家）全被判成官方自营，
    正好把用户要区分的两类混成了一类，且完全看不出错。
    """
    from .seller import detect as _detect
    r = _detect(page_text=page_text or "", adapter=adapter, brand=brand,
                channel_default=channel_default, seller_name=seller_name,
                official_store_id=official_store_id)
    return r["coarse"], r["reason"]


def detect_seller_full(page_text: str, html: str = "", adapter: str = "",
                       brand: str = "", channel_default: str = "unknown",
                       seller_name: str | None = None,
                       official_store_id=None) -> dict:
    """完整判定（含自营/品牌官方店/第三方三分）。见 seller.py。"""
    from .seller import detect as _detect
    return _detect(page_text=page_text, html=html, adapter=adapter, brand=brand,
                   channel_default=channel_default, seller_name=seller_name,
                   official_store_id=official_store_id)


# ---------------------------------------------------------------- 库存

_OOS_MARKERS = ["agotado", "sin stock", "no disponible", "esgotado",
                "indisponível", "out of stock", "sold out", "producto no disponible"]


# ★ 假朋友：这些短语里含缺货词，但说的**不是商品没货**，而是
#   「某家线下门店不能自提」「你所在区域不能配送」。
#   实测 Falabella 智利的商品页写：
#       「Entrega en Cerrillos  Sin stock en tienda Cerrillos, Metropolitana」
#   —— 意思是 Cerrillos 那家门店不能自提，商品本身在线正常发货。
#   裸的 "sin stock" 子串一命中，整条挂牌就被判缺货、被价格审计剔掉：
#   实测 **525 条全新非捆绑商品**（含 Apple Watch Series 11、Galaxy Watch
#   这种当红在售款）因此被踢出价格分析，Falabella 自营缺货率被算成 37%。
#   零售商自营的当季主推款不可能有三成缺货 —— 这个数本身就是警报。
_OOS_FALSE_FRIENDS = re.compile(
    r"(?:sin\s+stock|agotado|no\s+disponible|indispon[íi]vel|sem\s+estoque)"
    r"\s+(?:en|em|para|na|no)\s+"
    r"(?:tienda|la\s+tienda|sucursal|loja|retiro|retirada|despacho|domicilio|"
    r"env[íi]o|entrega|tu\s+(?:zona|regi[óo]n|comuna|ubicaci[óo]n|direcci[óo]n))"
    r"|(?:retiro|retirada)\s+(?:en\s+tienda\s+)?no\s+disponible"
    r"|(?:despacho|env[íi]o|entrega)\s+(?:a\s+domicilio\s+)?no\s+disponible",
    re.I)


# ★★ 方向 14：把「门店库存」这段文案**反过来用**。
#   它原本只作为 _OOS_FALSE_FRIENDS 被整段抹掉（不抹会把整条挂牌误判成缺货，
#   实测坑过 525 条）。抹是对的，但抹完不存等于扔掉一个免费的铺货指标。
#
#   实测（1108 个留有页面文本的采集页）：
#     门店有货  49 （4%）   "Stock en tienda Falabella Plaza Oeste 23 unidades disponibles"
#     门店无货 229 （21%）  "Sin stock en tienda Cerrillos, Metropolitana"
#     没有门店模块 830 （75%）
#
#   ★ 边界必须写清楚：页面只显示**一个默认门店**（智利 Cerrillos / 哥伦比亚
#     Usaquén / 秘鲁 Cercado de Lima），所以这是「该国默认门店有没有货」，
#     **不是城市级铺货地图**。把它当成后者会得出错误的铺货结论。
#   ★ 正负两侧的"门店名"含义不同：无货时给的是**你所在的区**，
#     有货时给的是**具体门店**（Falabella Plaza Oeste / Electrohogar）。
_STORE_POS = re.compile(
    r"stock\s+en\s+tienda\s+([A-Za-zÁÉÍÓÚÑáéíóúñ0-9 ._'-]{3,40}?)\s*"
    r"(?:(\d{1,4})\s*unidades\s*disponibles"
    r"|quedan\s+solo\s+(\d{1,4})\s*unidades)", re.I)
_STORE_NEG = re.compile(
    r"sin\s+stock\s+en\s+tienda\s+([A-Za-zÁÉÍÓÚÑáéíóúñ0-9 ._'-]{3,40})", re.I)


def detect_store_stock(page_text: str) -> dict:
    """解析门店库存信号。返回 {store_stock, store_units, store_name}。

    ★ 拿不到就返回全 None，**不猜**：把"页面没有这个模块"记成"无货"，
      会让铺货率凭空变差 75 个百分点。
    """
    t = page_text or ""
    none = {"store_stock": None, "store_units": None, "store_name": None}
    pos, neg = _STORE_POS.findall(t), _STORE_NEG.findall(t)

    # ★★ 一页里出现多个门店模块 = 这是**多商品页**，无法判断这段库存属于哪一个。
    #   实测目前每页恰好 1 处（搜索链接会重定向到单商品页），但这条约束必须写进
    #   代码而不是靠"目前如此"—— 哪天站方改成真·列表页，
    #   取第一处匹配就会把 A 商品的库存记到 B 商品头上，而且**不报错**。
    if len(pos) + len(neg) != 1:
        return none

    m = _STORE_POS.search(t)
    if m:
        return {"store_stock": 1,
                "store_units": int(m.group(2) or m.group(3)),
                "store_name": re.sub(r"\s+", " ", m.group(1)).strip()[:60]}
    m = _STORE_NEG.search(t)
    if m:
        return {"store_stock": 0, "store_units": None,
                "store_name": re.sub(r"\s+", " ", m.group(1)).strip()[:60]}
    return none


def detect_in_stock(page_text: str, availability: str = "") -> bool:
    # 结构化字段（JSON-LD availability）最可信，有就用它，不猜文案
    if availability:
        a = availability.lower()
        if "outofstock" in a or "soldout" in a:
            return False
        if "instock" in a:
            return True
    low = (page_text or "")[:3000].lower()
    # 先把"门店自提/配送不可用"这类假朋友抹掉，再找真正的缺货词
    low = _OOS_FALSE_FRIENDS.sub(" ", low)
    return not any(m in low for m in _OOS_MARKERS)


# ---------------------------------------------------------------- 分期

_INSTALLMENT_PAT = re.compile(
    r"(\d{1,2})\s*(?:x|cuotas?|meses|parcelas?|vezes)\s*(?:de\s*)?"
    r"([$R\s/.,\d]+)?(sin inter[eé]s|sem juros|sin intereses)?", re.I)


def parse_installments(text: str) -> str | None:
    """分期是拉美电商的核心卖点，友商常用「12期免息」打价格战，必须抓。"""
    if not text:
        return None
    m = _INSTALLMENT_PAT.search(text[:2000])
    if not m:
        return None
    n = m.group(1)
    amount = (m.group(2) or "").strip()
    free = "免息" if m.group(3) else ""
    return f"{n}期{('×' + amount) if amount else ''}{free}"[:60]
