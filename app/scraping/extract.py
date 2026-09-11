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

import functools
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
    # Liverpool「加入购物车」按钮文案被并进标题（374 条，2026-08-31 实测）：
    # 「add SAMSUNG Galaxy S25 Ultra …」。★ 必须带 \b 且只在**行首**匹配
    # （本组是前缀组）—— 裸 add 会吃掉「Adidas」「Additional」这类真词。
    r"add\b",
    # 分期促销文案（7,550 条，2026-09-03 实测）：
    #   「4 cuotas sin interés GENÉRICO PANTALLA…」
    #   「CUPÓN XIAOMI10 4 y 10 cuotas sin interés XIAOMI SMARTPHONE…」
    # ★ 只剥**行首**的分期文案。标题正文里的「12 cuotas」是价格说明，
    #   与商品名无关，但也不在行首，所以这条前缀规则碰不到它。
    #   不剥的后果：型号归一化把它当主语，产出「4 Cuotas Sin Interes」
    #   这种"型号"（实测 86 条观测挂在这个假产品上）。
    r"cup[óo]n\s+\S+",                 # 优惠券码（169）
    r"\d+\s*(?:y\s*\d+\s*)?cuotas?\s+sin\s+inter[ée]s",
    # Falabella 的促销日角标「Días R」（智利/秘鲁/哥伦比亚通用）。
    # ★ 它常和分期文案连着出现：「Días R 4 cuotas sin interés DELL NOTEBOOK…」，
    #   前缀组是循环剥的，所以两条各自命中即可，不必写成一条组合规则。
    r"d[íi]as\s+r",
    r"regalo\s+a\s+\$?\s*\d+",         # 「regalo a $1」满赠角标
    # ★ 角标被上游截断成半个词的残片（实测 15 个产品因此得名）：
    #   「sta Previa Días R …」「ncluye regalo a $1 …」「ta Previa …」「a Previa TARJETAZO …」
    #   容错只对**行首**开放，且只认这几个已知角标的后缀片段 ——
    #   不能写成通配，否则会啃掉真商品名的开头。
    #   2026-09-04 现行犯：2 字残片「a Previa」漏网，建出假产品 #9204「A Previa Tarjetazo Cupon」
    #   （库里 60d 起首形态：a previa 4 / ista previa 3 / previa 1 / ta previa 1）。
    r"(?:\w{0,3}(?:sta|ta)|a)\s+previa",     # sta/ta/vista/ista/a Previa
    r"previa",                         # 被切到只剩 Previa
    r"\w{0,3}cluye\s+regalo\s+a\s+\$?\s*\d+",   # Incluye/ncluye/cluye regalo a $1
    r"tarjetazo",                      # Falabella 信用卡日
)
_UI_BADGE_ALT = "|".join(_UI_BADGE_PREFIXES)
# 成色词在角标叠层里也是角标：「Reacondicionado 4 cuotas … VIVO Y29」。
# ★★ 只在**后面还跟着别的角标**时才剥（lookahead）。直接跟商品名的「Reacondicionado APPLE
#   iPhone 13」保留 —— 采集端拿**剥后**标题跑 detect_condition（base.py），剥掉等于把成色
#   信息丢了（实测 432 行翻新机 condition='new'）；型号归一化另有噪声表（cleaner._NOISE）
#   会把它去掉，所以保留它不会产出「Reacondicionado iPhone 13」这种型号名。
#   阴性/复数形态一起收：Falabella 写「Reacondicionada」。
_UI_BADGE_CONDITION = (r"(?:reacondicionad[oa]s?|recondicionad[oa]s?|renovad[oa]s?|reformad[oa]s?)"
                       r"(?=[\s\-–—:|]+(?:" + _UI_BADGE_ALT + r"))")

# 赠品尾巴（3,378 条）：「HONOR 600 512GB 5G Naranja + Audifonos Clip」
# ★ 只在**结尾**剥，且要求 + 号后是明确的赠品类目词 —— 裸 + 不能剥：
#   「Galaxy Tab A11+」「iPad Air 11+」的 + 是型号的一部分。
_GIFT_TAIL_RE = re.compile(
    r"\s*\+\s*(?:audi[fó]onos|aud[íi]fonos|earbuds|buds|clip|reloj|"
    r"smartwatch|parlante|bocina|funda|estuche|cargador)\b.*$",
    re.I)
# 角标之间可能夹着分隔符，也可能只有空格；用 NBSP 一起吃掉（库里 879 条标题含 NBSP）
_UI_BADGE_PREFIX_RE = re.compile(
    r"^(?:(?:" + _UI_BADGE_ALT + "|" + _UI_BADGE_CONDITION + r")[\s\-–—:|]+)+",
    re.I,
)

# ★ Ripley Chile 的商品卡把「品牌芯片 + 评分」也叠进标题头部（2026-09-04 实测 60d 2,890 条，
#   全在 Ripley）：「ACER 5.0 NOTEBOOK ACER NITRO LITE 16 …」「SONY 4.5 Audífonos WH-CH520」。
#   不剥的后果已经在库里：假型号「5.0 Vivo Y19S」60 条观测、「4.8 Honor 400」54 条。
#   守卫（每条都对着库里的反例）：
#     - 芯片词必须**全大写**（Ripley 品牌芯片的渲染形态）——「Bluetooth 5.4 con ANC」
#       「Audífonos 3.5 mm」这类真规格是首字母大写，不动；
#     - 评分限 0~5 一位小数、点号；后面不得是单位/声道词（「LG 2.1 CH SOUNDBAR」的 2.1 是声道）；
#     - 品牌词在商品名里**再出现**（92.6%）才整个芯片都剥；只出现这一次就只剥评分、保住品牌词
#       （「APPLE 4.7 AIRPODS PRO 3」→「APPLE AIRPODS PRO 3」，collector.guess_brand 靠它认品牌）。
_UI_RATING_CHIP_RE = re.compile(
    r"^(?P<brand>[A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ0-9&.\-]+)[\s\xa0]+(?P<rating>[0-5]\.\d)[\s\xa0]+"
    # 单位/声道词的守卫要大小写无关（芯片后面的商品名在 Ripley 是全大写：「LG 2.1 CH SOUNDBAR」）
    r"(?!(?i:mm|cm|pulg|pulgadas|polegadas|inch|ch|canales|channel|soundbar|barra|home\s+theater)\b)"
    r"(?![\"'”])(?=\S)")


def _strip_rating_chip(title: str) -> str:
    m = _UI_RATING_CHIP_RE.match(title)
    if not m:
        return title
    brand, rest = m.group("brand"), title[m.end():]
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(brand)}(?![A-Za-z0-9])", rest, re.I):
        return rest                              # 品牌在商品名里还会再出现 → 整个芯片都是角标
    return f"{brand} {rest}"                     # 只出现这一次 → 只剥评分，保住品牌词

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
    # 赠品尾巴（见上方 _GIFT_TAIL_RE 的说明）
    _GIFT_TAIL_RE,
)

# 剥完必须还剩**至少一个字母** —— 判据取自
# knowledge/lessons/scrape-normalize-silent-corruption.md 第 2 条：
# 「归一化剥出空型号比留着噪声危险得多」（剥空的商品会全挤进同一个产品，
#   造出「同一款降价 69%」这种根本不存在的情报）。纯数字/纯符号同样算没剥住。
_TITLE_HAS_LETTER = re.compile(r"[^\W\d_]")

# 角标实测叠到五层（「a Previa TARJETAZO CUPÓN ACER10 4 cuotas sin interés ACER 5.0 …」），
# 前缀组是一次 sub 连剥的链，评分芯片另算一轮；留余量到 6。有上限是为了任何词表改动都不会
# 变成死循环（知识页：角标叠三层要循环剥，循环必须有上限）。
_UI_BADGE_MAX_ROUNDS = 6


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
        # 品牌芯片+评分叠在分期/优惠券角标**之后**，所以放在前缀链之后、每轮都试
        new = _strip_rating_chip(new)
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


# 持卡人专享价的标签词（2026-08-28 用户拍板「公开价优先」）。
# ★ 只认**专享**语义：TARJETA FEST（Ripley 卡节）、「X% adicional tarjeta」、
#   「precio/con tarjeta CMR|Ripley|Falabella|Cencosud|Oh!」。
#   裸 "tarjeta"（=银行卡）不认：「paga con tarjeta o PIX」是支付方式说明，
#   人人可用，误标会把整个渠道剔出分析。
_CARD_PRICE_PAT = re.compile(
    r"(?i)tarjeta\s+fest"
    r"|\d+\s*%\s*adicional\s+(?:con\s+)?tarjeta"
    r"|(?:precio|con)\s+tarjeta\s+(?:cmr|ripley|falabella|cencosud|oh)"
    r"|\bcmr\s+puntos\b|\bt\.?\s*cencosud\b")


def detect_price_scope(text: str | None) -> str:
    """价格口径判定：card = 持卡人专享价，public = 公开价（默认）。"""
    return "card" if text and _CARD_PRICE_PAT.search(text) else "public"


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
# ★ 不能用 \b 起头：拉美标题常把容量紧贴在词后（"Edge 70 Fusion Plus512GB"），
#   "s" 与 "5" 都是词字符 ⇒ 没有词边界 ⇒ 整条容量漏解析（实测近 14 天
#   手机/平板里有 292 条「标题含 GB 却没解析出存储」）。
#   改用 (?<!\d) 只防止从长数字中间截断。
_ROM_ONLY_PAT = re.compile(r"(?<!\d)(\d{1,4})\s*(gb|tb)\b", re.I)
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
    cands = []
    for m2 in _ROM_ONLY_PAT.finditer(t):
        val = int(m2.group(1))
        if m2.group(2).lower() == "tb":
            val *= 1024
        cands.append(val)
        # 容量候选取最大的（标题里 8GB RAM 和 256GB 同时出现时取 256）
        if val >= 16 and (rom is None or val > rom):
            rom = val

    # ★★ 并列写法：拉美（尤其巴西）标题常把两个容量并排写，中间只有逗号或空格，
    #   **不带 RAM 字样也不带 + 号**：
    #     「Galaxy A17, 256GB, 8GB, 50MP」「Poco C85 6GB 128GB」
    #     「Redmi Pad SE 4GB 64GB Wi-Fi」
    #   上面两条规则都接不住它：_RAM_ROM_PAT 要 +或/，_RAM_ONLY_PAT 要「RAM」字样，
    #   而 _ROM_ONLY_PAT 只挑最大值当 ROM —— 于是 RAM 整条丢掉。
    #   实测这一种写法占「缺 RAM」观测的绝大多数（近 30 天 3.5 万条手机/平板
    #   缺 RAM，标题里其实都写了）。
    #   判据：恰好两个 GB 候选，一个 ≤32（内存量级）一个 ≥16（存储量级），
    #   且两者不相等 —— 相等时无从分辨（「8GB 8GB」不猜）。
    if ram is None and len(cands) == 2:
        lo, hi = min(cands), max(cands)
        if lo != hi and lo <= 32 and hi >= 16:
            ram, rom = lo, hi
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

# ★ 成色词要正则化收阴性/复数：Falabella/LikeShop 写「Reacondicionada」「Reacondicionados」，
#   裸词表只认阳性单数 ⇒ 实测 432 行翻新机 condition='new'（429 行还是 device，
#   进了价格基线）。renovado 要排除「diseño renovado」这类营销语（extra 里有页面文本）。
_REFURB_MARKERS = ["reacondicionado", "recondicionado", "refurbished", "renewed",
                   "seminuevo", "semi-novo", "usado", "used", "open box", "caja abierta"]
_REFURB_RE = re.compile(
    r"\b(?:reacondicionad[oa]s?|recondicionad[oa]s?|refurbished|renewed|reformad[oa]s?|"
    r"(?<!dise[ñn]o )(?<!design )(?<!look )renovad[oa]s?|"
    r"semi[\s\-]?nuev[oa]s?|semi[\s\-]?nov[oa]s?|usad[oa]s?|used|open\s*box|caja\s*abierta|"
    r"segunda\s*mano|recertificad[oa]s?|remanufacturad[oa]s?)\b", re.I)
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
    """成色：refurb / new。词表正则化（两头 \\b，收阴性/复数），见 _REFURB_RE。"""
    blob = f"{title or ''} {extra or ''}"
    return "refurb" if _REFURB_RE.search(blob) else "new"


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
    # 2026-09-04 残留体检：被截断的品类导航/筛选文案进了 price_obs（200,000 COP / 100 PEN）
    #   「…rtphones Celulares básicos Repuestos de celulares Marca Xiaomi Samsung…」
    #   「Apple Xiaomi Motorola … + Ver más Calificación del producto 5」
    "ver más", "ver mas", "calificación del producto", "calificacion del producto",
    "celulares básicos", "celulares basicos", "repuestos de celulares", "retira desde",
    "en un punto", "marca xiaomi samsung", "marca motorola apple",
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
        if any(w in tail for w in _BUNDLE_ACCESSORY_WORDS):
            return True
    # ★★ 整机 + 整机 的捆绑（2026-09-07 补）：上面只认「整机+配件」，
    #   「Celular Xiaomi 17T Pro 1TB 5G Negro + Tablet Redmi Pad 2」这种
    #   手机送平板的组合包全部漏检 —— 实测 84 条以 is_bundle=0 通过审计，
    #   挂在 Redmi Pad 2 的价格线上（一部手机的钱买到"一台平板"）。
    #   判据：分隔标记（+ / gratis / de regalo / incluye）**两侧各有一个设备词**，
    #   且两侧设备词不同（同一台机器重复写不算，如 "Celular ... + Celular"）。
    #   赠品配件不受影响：配件词不在 _DEVICE_WORDS 里。
    return _is_device_plus_device(low)


_BUNDLE_SPLIT_RE = re.compile(
    r"\s\+\s|\bgratis\b|\bde\s+regalo\b|\bincluye\b|\bcom\s+brinde\b|\bmais\b", re.I)


# ★ 「GPS + Celular」是 Apple Watch / Galaxy Watch 的**连接方式变体名**，不是捆绑
#   （"APPLE Watch Ultra 3 GPS + Celular 49mm"）。实测误判 76 条 —— 把一块手表
#   判成"手表+手机"套装。同族写法：GPS + Cellular / GPS+LTE。
_WATCH_CELLULAR_RE = re.compile(
    r"\bgps\s*[+/]\s*(?:celular|cellular|lte|4g|5g)\b", re.I)


def _is_device_plus_device(low: str) -> bool:
    """分隔标记两侧各有一个**不同的**设备词 → 整机+整机捆绑。"""
    if _WATCH_CELLULAR_RE.search(low):
        return False
    parts = _BUNDLE_SPLIT_RE.split(low, maxsplit=1)
    if len(parts) < 2:
        return False
    head, tail = parts[0], parts[1]
    if not head.strip() or not tail.strip():
        return False
    hw = {m.group(0).strip() for m in _DEVICE_WORD_RE.finditer(head)}
    tw = {m.group(0).strip() for m in _DEVICE_WORD_RE.finditer(tail)}
    # ★ 只要求**后半段**有前半段没有的设备词：前半段常常只有型号名没有品类词
    #   （"Xiaomi 17T Pro 1TB 5G + Redmi Pad 2" 的 "17T Pro" 不是设备词），
    #   要求两侧都有会漏掉一半。前半段要够实（≥2 词），挡住 "+ Tablet ..." 开头的残句。
    #   "… 256GB Gratis Envio" 不受影响：envio 不是设备词。
    return bool(tw - hw) and len(head.split()) >= 2


# ---------------------------------------------------------------- 整机 / 配件

# ★ 单一实现、多处消费：skumap.is_accessory（权威表前置闸，吃 accessory_para_form）、
#   detect_product_kind（通用启发式）、tools/backfill_accessory_kind.py（历史回填，走
#   base._enrich_from_title 同一条路径）、price_audit（地板样本剔配件，吃 _ACCESSORY_WORDS）。
#   分开写必然出现「分类器修好了、回填是另一套」，而两套规则的差集永远没人发现
#   （2026-09-04 实测：回填工具自带 judge() 干跑 218 行 vs 现行分类器 263 行，差 45 行无人知）。
#
# ★ 规则强弱（knowledge/lessons/host-vs-accessory-classification.md）：
#   ① 依附标记 para / compatible (con) / p/ / for —— 配件的**句法定义**；
#   ② 结论性词（物理上不可能是别的东西：lámina de vidrio / vidrio templado / power bank）无视位置，
#      只有「整机 … + / con / de regalo … 结论性词」的捆绑/赠品形态例外；
#   ③ 词序位置：配件词早于设备词 → 配件；设备词打头 → 整机（含捆绑装）；
#   ④ 词表兜底；⑤ 三态：设备名打头紧接保护类名词且无捆绑标记、或设备依附于同类设备 → unknown（待定）。
#
# ★ 2026-09-04 事故复盘（#2292「Lenovo Tab」桶 158 种标题、CL 下沿 8,500 CLP 是一张钢化膜）：
#   60 条人工归类的真配件里现行管线只认出 2 条（召回 5%）。漏判原因按量排：
#     - 平板品类的控制流走不到 detect_product_kind：skumap 命中品牌级兜底 SKU 就短路判整机，
#       能拦下配件的只有 yaml 词表 + accessory_para_form ⇒ 本节把结论性词/主语位置强名词/
#       句首依附标记都放进 accessory_para_form，让权威表那条路同样吃到（15/38 漏判在这条路上）；
#     - 词表缺词：lamina(684 条 device 观测) / hidrogel(511) / pantalla 头部(348) / vidrio(248) /
#       almohadillas(237) / manilla(39) / destornillador(58)…；依附标记只认「compatible con」而
#       Falabella 常写裸「Compatible Apple iPad」（296 条无 con）；
#     - 位置规则对 Amazon 形态失效：「Compatible con Xiaomi Pad Mini … funda」设备名在前。
#   ★★ 放宽触发前先加固守卫（知识页原话，同一课第四次复发）：产品线清单只留 _DEVICE_WORDS
#   这一份，主语守卫从它派生；补 yoga tab / legion tab / xiaoxin / acme band / galaxy fit /
#   watch gt|fit|ultra，正则通道接住「Tab M11 / Tab P12 / Band 9」这类词+数字形态。

# 词表匹配的**判定形态**：入参已 lower（重音去不去都认），词界用「前后都不是字母数字」——
# 不是 \b（\b 在 "é" 这类字符上行为依赖 Unicode 分类，且 "cable" 会在 "Cablex" 里词内命中：
# 知识页原话「有个卖家就叫 Cablex，cable 子串一命中整台手机被判成配件」）。
_WB_L = r"(?<![a-z0-9à-ÿ])"
_WB_R = r"(?![a-z0-9à-ÿ])"

# 整机信号。**这是产品线清单的唯一一份** —— 配件句法规则的主语守卫（_lead_has_device）
# 从这里派生，不再另抄一份。每条是正则片段（不含词界，统一由 _DEVICE_WORD_RE 加）。
# ★ 漏收一个厂商叫法 = 那条线的整机只要标题里出现配件词就被判成配件：
#   Xiaomi Pad / Honor Pad / Lenovo Tab（08-27）→ Yoga Tab / Legion Tab / Acme Band（09-04）。
# ★ 词后允许紧跟数字（galaxy s25 / redmi13 / tab a9），不允许紧跟字母（tablet 不许命中
#   「60 tabletas」—— 那是药片，实测躺在 phone 桶里）。
_DEVICE_WORDS = [
    # 手机
    r"smartphones?", r"celular(?:es)?", r"tel[eé]fonos?", r"telefones?", r"telem[oó]vel", r"iphones?",
    r"galaxy\s*[saz]\d", r"galaxy\s*note", r"galaxy\s*z\s*(?:flip|fold)", r"redmi", r"poco\s*[a-z]?\d",
    r"poco\s*pad", r"moto\s*[ge]\d*", r"moto\s*edge", r"razr",
    # Acme产品线（品牌商城标题没有 celular/smartphone 这类通用词）：nimbus 15 / nimbus Flip / Vega 80 / Astra X7 / Astra XT
    r"nimbus\s*(?:\d|flip)", r"vega\s*(?:\d|x)", r"astra\s*(?:\d{2}(?!\d)|x[st]?\d*)",   # Astra 60/70/X7/XT，不是 yerba astra 500g
    # 平板（厂商怎么叫就收什么；通用词覆盖不了品牌词）
    r"tablets?", r"tableta", r"ipads?", r"galaxy\s*tab", r"slate", r"xiaomi\s*pad", r"redmi\s*pad",
    r"mi\s*pad", r"honor\s*pad", r"lenovo\s*tab", r"idea\s*tab", r"ideatab", r"yoga\s*tab", r"legion\s*tab",
    r"xiaoxin(?:\s*pad)?", r"surface\s*pro", r"kindle", r"fire\s*hd", r"tab\s*[a-z]?\d+", r"magicpad",
    # 「Pad 8 Pro Pantalla 11.2 …」这类不带厂商名的产品线：pad + 一两位数字；排除 mouse pad 的尺寸写法
    r"pad\s*\d{1,2}(?!\d)(?!\s*(?:cm|mm|x\s*\d))",
    # PC
    r"laptops?", r"notebooks?", r"macbook", r"thinkpad", r"ideapad", r"chromebook", r"port[aá]til",
    # 穿戴
    r"smartwatch(?:es)?", r"reloj(?:es)?\s*inteligentes?", r"rel[oó]gio\s*inteligente", r"watch\s*series",
    r"galaxy\s*watch", r"apple\s*watch", r"watch\s*(?:gt|fit|ultra|se)", r"acme\s*watch", r"redmi\s*watch",
    r"amazfit", r"smart\s*band", r"smartband", r"(?:xiao)?mi\s*band", r"acme\s*band", r"galaxy\s*fit",
    r"band\s*\d+", r"pulseras?\s*inteligentes?", r"pulseiras?\s*inteligentes?", r"bandas?\s*inteligentes?",
    # 音频（在音频品类是主机；在平板/手机品类作为「赠品」出现时靠位置规则：设备词更早）
    r"aud[ií]fonos?", r"auriculares?", r"earbuds?", r"airpods", r"\w*buds", r"fones?\s*de\s*ouvido",
    r"headphones?", r"parlantes?", r"bocinas?", r"caixa\s*de\s*som",
]
_DEVICE_WORD_RE = re.compile(_WB_L + r"(?:" + "|".join(_DEVICE_WORDS) + r")(?![a-zà-ÿ])")

# 主语守卫的补充词：不是设备名本身，但出现在配件名词**之前**就说明主语是台设备
# （"Galaxy … Funda de regalo"、"Pad 6 Funda…"、"Reloj … correa…"）。子串语义、两端补空格。
_LEAD_VETO_EXTRA = (
    "galaxy", "surface", "imac", "e-reader", "ereader", " pad ", "pad se", "pad pro", "pad air",
    "tab a", "tab m", "tab s", "tab p", "nimbus ", "astra ", "vega ",
    "reloj", "watch", "fone", "audifono", "audífono", "auricular", "earbud", "airpod",
)


def _device_pos(t: str) -> tuple[int, str]:
    """最早出现的设备词 (位置, 词)；没有返回 (-1, '')。词表 + 正则通道同一份。"""
    m = _DEVICE_WORD_RE.search(t or "")
    return (m.start(), m.group(0)) if m else (-1, "")


def _lead_has_device(lead: str) -> bool:
    """配件名词前面的主语位置里有没有设备词（veto）。"""
    padded = f" {lead or ''} "
    return bool(_DEVICE_WORD_RE.search(padded)) or any(w in padded for w in _LEAD_VETO_EXTRA)


# ---------------------------------------------------------------- 句法规则（accessory_para_form）

# ★ 名词分三档，档位决定它需要什么标记才算配件 —— 「位置/标记限制必须是词的属性，
#   不能靠放在哪张表里隐式携带」（知识页）：
#   STRONG —— 出现在主语位置（前 4 个词内、前面没有设备词）就是配件，不需要依附标记。
#            只收「作为标题主语时物理上不可能是整机」的词：funda/capa/lámina/mica/película/
#            cargador/teclado/cable/soporte/adaptador/lápiz/stylus/pantalla(维修件)…
#            ★ 不收 cases?/covers?（Case Logic 这类品牌名）、不收佩戴词（见 WEAK）。
#   NOUN   —— 需要任一依附标记（para / p/ / de / do / da / for / compatible (con) / compatível (com)）。
#   WEAK   —— 语义弱或有歧义，只认 para 类标记（配 de 会误伤「base de datos」「banda de rodaje」
#            「fone de ouvido de alta qualidade」「micrófono de condensador」）：
#            correa/pulseira/banda/band/malla/base/manilla/micrófono/webcam/mochila/candado/
#            tornillos/batería/pilha/fone de ouvido com fio。佩戴词后接 inteligente/smart 不算
#            （「Pulsera/Pulseira Inteligente」是真手环）；band 后接数字不算（Band 9 是设备）。
#   ★ 佩戴词绝不放进 STRONG/NOUN：模拟证实会误杀「Pulseira Inteligente Xiaomi Band 9」。
#   ★ 不收裸 flex（Beats Flex 是耳机）、不收裸 band、不收 tabletas（药片）。
_ACC_FORM_STRONG = (
    r"capas?|capinhas?|pel[ií]culas?|fundas?|micas?|estuches?|carcasas?|forros?|"
    r"protector(?:es|a|as)?|protetor(?:es|a|as)?|l[aá]minas?|hidrogel|hydrogel|"
    r"vidrios? templados?|vidros? temperados?|cristal(?:es)? templados?|screen protectors?|tempered glass|"
    r"almohadillas?|destornillador(?:es)?|desarmador(?:es)?|kit de limpieza|"
    r"cargador(?:es)?|carregador(?:es)?|teclados?|cabos?|cables?|soportes?|suportes?|"
    r"adaptador(?:es)?|estaci[oó]n(?:es)? de carga|base de carga|flex de carga|"
    r"l[aá]pi(?:z|ces)|stylus|canetas?|pantallas?|cubre teclados?|smart folios?|smart covers?|"
    # —— 2026-09-04 残留体检补入（每 (品类,币种) 最便宜的 device 行顶出来的形态）——
    r"porta ?celular(?:es)?|porta ?smartphones?|tarjeteros?|pencils?|"
    r"garant[ií]as? extendidas?|garantia estendida|cambio de (?:t[aá]ctil|pantalla|bater[ií]a)|"
    r"estabilizador(?:es)?|gimbals?|tr[ií]podes?|teleprompters?|"
    r"flex (?:antena|de carga|centro de carga|conector|bot[oó]n|power)|"
    r"c[aá]maras? (?:frontal|trasera|posterior)|ic de touch|lector(?:es)? (?:de )?(?:tarjetas?|microsd|micro sd)"
)
_ACC_FORM_NOUN = (
    _ACC_FORM_STRONG + r"|"
    r"cases?|covers?|adapters?|vidrios?|cristal(?:es)?|cubiertas?|earpods|"
    r"micas? (?:cristal|vidrio)|film(?:es)?|"
    r"mouse|mice|rat[oó]n(?:es)?|sujetador(?:es)?|colgantes?|cordones?|lanyards?|bolsos?|bolsas?|"
    r"cer[aá]micas?|skins?|vinilos?|nano ?glass|glass|marcos?|charging"
)
_ACC_FORM_WEAK = (
    r"correas?|pulseiras?|bandas?|mallas?|straps?|bases?|manillas?|bands?(?!\s*\d)|"
    r"micr[oó]fonos?|webcams?|mochilas?|candados?|tornillos?|bater[ií]as?|pilhas?|"
    r"fones? de ouvido com fio"
)
# 依附标记。★ 这是「para 是句法定义」的同族，不是词表：compatible 后面的 con 可省
#   （Falabella 实测 296 条裸「Compatible Apple iPad」），p/ 是西/葡缩写。
_ACC_FORM_MARK_FULL = (r"para\b|p/|de\b|do\b|da\b|for\b|"
                       r"compatible\b(?:\s+con\b)?|compat[ií]vel\b(?:\s+com\b)?|apt[oa]s?\b(?:\s+para\b)?")
_ACC_FORM_MARK_PARA = (r"para\b|p/|for\b|compatible\b(?:\s+con\b)?|compat[ií]vel\b(?:\s+com\b)?|"
                       r"apt[oa]s?\b(?:\s+para\b)?")
# 主语窗口 4 个词（营销前缀/厂商名+数量词会把配件词挤到第 3~5 位，实测窗口不能收回 2）；
# 介词窗口 8 个词（介词最远在名词后第 9 个词；真正挡误杀的是主语守卫不是窗口宽度）。
_ACC_FORM_RE = re.compile(
    rf"^(?P<lead>(?:\S+\s+){{0,4}}?)"
    rf"(?:(?P<noun>{_ACC_FORM_NOUN})\b(?:\s+\S+){{0,8}}?\s+(?:{_ACC_FORM_MARK_FULL})"
    rf"|(?P<weak>{_ACC_FORM_WEAK})\b(?!\s+(?:inteligente|smart))"
    rf"(?:\s+\S+){{0,8}}?\s+(?:{_ACC_FORM_MARK_PARA})"
    rf"|(?P<bare>{_ACC_FORM_STRONG})\b)"
)
# 保留旧名，历史注释/知识页里都叫它 veto 表；现在从 _DEVICE_WORDS 派生，见 _lead_has_device。
_ACC_FORM_LEAD_VETO = _LEAD_VETO_EXTRA
# WEAK 里「单卖也是设备/杂货」的词：依附标记之后 50 字符内必须出现设备名才算配件
_ACC_FORM_NEEDS_TARGET_RE = re.compile(r"micr[oó]fonos?|webcams?|mochilas?|candados?|tornillos?")

# ★ 结论性词：一旦出现，这东西就是配件，前面写什么都不改变（知识页：
#   「Cargador … Power Bank」按位置判成充电器，单站 4,560 台错）。只给物理上不可能是别的东西的词。
#   唯一例外是捆绑/赠品形态：「整机 … + / con / com / incluye … 结论性词」或「… 结论性词 de regalo」
#   —— 那是带膜的整机，仍按整机走位置规则。
_ACC_CONCLUSIVE_RE = re.compile(
    _WB_L + r"(?:"
    r"l[aá]minas?\s+(?:de\s+)?(?:hidrogel|hydrogel|vidrio|mica|cristal|pantalla|protector[ae]?s?|templad[oa]s?)|"
    r"micas?\s+(?:de\s+)?(?:cristal|vidrio)(?:\s+templad[oa]s?)?|micas?\s+templad[oa]s?|"
    r"vidrios?\s+templados?|cristal(?:es)?\s+templados?|vidros?\s+temperados?|tempered\s+glass|"
    r"pel[ií]culas?\s+(?:de\s+)?(?:vidro|hidrogel|privacidade|fosca|protetora|cer[aâ]mica|nano|3d)|"
    r"hidrogel|hydrogel|screen\s+protectors?|protector(?:es)?\s+de\s+pantalla|protetor(?:es)?\s+de\s+tela|"
    r"power\s*banks?|bater[ií]as?\s+externas?|cargador(?:es)?\s+port[aá]til(?:es)?|"
    r"carregador(?:es)?\s+port[aá]til(?:eis)?|kit\s+de\s+limpieza"
    r")" + _WB_R)
# 捆绑连接词（出现在整机词与配件词**之间**）与赠品说明（紧跟在配件词**之后** ≤2 个词）。
# 两者分开是因为同一个 con 在两个位置意思相反：「Tablet con funda」=带壳整机，
# 「funda protectora con correa」=壳带手带（仍是配件）。
_BUNDLE_BEFORE_RE = re.compile(
    r"\+|\bwith\b|\bcon\b|\bcom\b|\bincluye\b|\binclui\b|\bincluid[oa]s?\b|\bkit\b|\bpack\b|"
    r"\bbundle\b|\bcombo\b|\bgratis\b|\bde\s+regalo\b|\bde\s+brinde\b|\bmais\b|\bm[aá]s\b")
_GIFT_AFTER_RE = re.compile(
    r"^(?:\s+\S+){0,2}?\s+(?:de\s+regalo|de\s+brinde|gr[aá]tis|incluid[oa]s?|inclu[ií]d[oa]s?|inclusos?|"
    r"de\s+cortes[ií]a|de\s+presente|de\s+obsequio|obsequio|inbox|en\s+caja)\b")
# 句首依附标记（Amazon 形态）：「Compatible con Xiaomi Pad Mini 8.8 pulgadas … funda delgada」——
# 设备名在前、配件名在后，位置规则必错；改看**标记之后** 60 字符内有没有配件名词。
_LEAD_MARK_RE = re.compile(
    rf"^(?:compatible(?:\s+con)?|compat[ií]vel(?:\s+com)?|para|for|p/)\s+(?P<mid>.{{0,60}}?)"
    rf"{_WB_L}(?P<noun>{_ACC_FORM_NOUN}|{_ACC_FORM_WEAK}){_WB_R}")


def _conclusive_accessory(t: str) -> str | None:
    """结论性词命中 → 返回命中词。

    「无视位置」只是相对其他**配件词**（cargador 在前、power bank 在后仍是充电宝）；
    前面已经出现**设备词**的不算 —— 「Celular … Redmi Note 15 Pro … Power Bank 165W」是手机送充电宝
    （Falabella 用 I 分隔没有 + / con），干跑高价端抓出来的，1,699,900 COP 差点被判成配件。
    设备词在前的交给位置规则/三态（「Galaxy Tab S9 Mica Cristal Templado」→ 待定）。
    """
    dev_pos, _ = _device_pos(t)
    for m in _ACC_CONCLUSIVE_RE.finditer(t):
        if 0 <= dev_pos < m.start():
            continue
        return m.group(0)
    return None


def accessory_para_form(title_low: str) -> str | None:
    """配件的**句法**判定（单一实现，skumap / detect_product_kind / 回填工具共用）。

    入参须已 lower（重音去不去都认）。命中返回人话依据，未命中返回 None。
    三条规则，都是高置信、只在这些形态上开火：
      ① 主语位置配件名词 + 依附标记（para/p//de/for/compatible…），或主语位置**强**配件名词
         （lámina/mica/funda/capa/película/cargador/teclado…）—— 前面 4 个词内不得有设备词；
      ② 结论性词（lámina de vidrio / vidrio templado / hidrogel / power bank…）无视位置，
         「整机 + / con … 词」「词 de regalo」的捆绑赠品形态除外；
      ③ 句首依附标记（Compatible con / Para / For 打头）→ 看标记之后 60 字符内有没有配件名词。
    """
    t = title_low or ""
    if not t:
        return None
    m = _ACC_FORM_RE.match(t)
    if m and not _lead_has_device(m.group("lead")):
        if m.group("bare"):
            return f"主语位置强配件名词「{m.group('bare')}」→ 配件"
        word = m.group("noun") or m.group("weak")
        # 麦克风/摄像头/背包/锁/螺丝单卖也是设备或杂货：只有依附标记后面跟着**设备名**才算配件
        # （「Micrófono para celular」是配件；「Micrófono de condensador USB para streaming」不是）。
        if not (m.group("weak") and _ACC_FORM_NEEDS_TARGET_RE.fullmatch(word)
                and _device_pos(t[m.end():m.end() + 50])[0] < 0):
            return f"主语位置配件词「{word}」+ para/de 依附标记 → 配件"
    hit = _conclusive_accessory(t)
    if hit:
        return f"结论性词「{hit}」→ 配件（无视位置）"
    m = _LEAD_MARK_RE.match(t)
    if m:
        return f"句首依附标记 + 「{m.group('noun')}」→ 配件（Amazon 形态：设备名在前）"
    return None


# ---------------------------------------------------------------- 词表（位置规则用）

# 配件词（西/葡/英）。price_audit 也吃这一份（地板样本剔配件）。
# ★ 佩戴类裸词（correa/pulseira/banda）保留 —— 「Correa cruzada – Rosa pálido」没有 para/de，
#   删裸词会放跑它。「Pulsera/Pulseira Inteligente」真手环靠 _DEVICE_WORDS 同位置平局判整机。
# ★ 词表匹配带词界 + 西/葡复数/阴性后缀（-s/-es/-a/-as），见 _accessory_word_re：
#   "capa" 不再命中 "capacidad"，"cable" 不再命中卖家名 "Cablex"。
_ACCESSORY_WORDS = [
    # 保护类
    "funda", "carcasa", "case", "capa", "capinha", "protector", "protetor",
    "mica", "cristal templado", "vidrio templado", "vidro temperado", "película", "pelicula",
    "lámina", "lamina", "hidrogel", "hydrogel", "screen protector", "tempered glass",
    "smart folio", "keyboard cover", "flip cover", "book cover", "estuche", "forro",
    "almohadilla", "manilla", "cerámica", "ceramica",
    # 佩戴类
    "correa", "pulseira", "banda", "strap", "malla para",
    # 供电类
    "cargador", "carregador", "charger", "cable", "cabo", "adaptador", "adapter",
    "power bank", "powerbank", "bateria externa", "batería externa", "power adapter",
    "adaptador de corriente", "cargador portatil", "cargador portátil",
    "estación de carga", "estacion de carga", "base de carga", "flex de carga",
    # 维修 / 工具 / 服务
    "destornillador", "desarmador", "kit de limpieza", "garantía extendida", "garantia extendida",
    "cambio de tactil", "cambio de táctil", "cambio de pantalla",
    # 支撑 / 携带
    "soporte", "suporte", "holder", "stand", "porta celular", "portacelular", "porta smartphone",
    "tarjetero", "sujetador", "estabilizador", "gimbal", "trípode", "tripode", "teleprompter", "gancho",
    # 其它
    "lápiz", "lapiz", "stylus", "caneta", "teclado", "pencil",
    "s pen", "apple pencil", "memoria", "micro sd", "microsd", "tarjeta sd", "earpods",
    "lector microsd", "lector micro sd", "lector de tarjetas", "card reader",
    # 歧义裸词：只在标题前 _HEAD_WINDOW 字符内算配件（见 _ACC_HEAD_ONLY）
    "pantalla", "vidrio", "cristal", "cubierta", "bateria", "batería", "display", "tela",
    "glass", "charging", "mouse", "marco", "camara frontal", "cámara frontal",
    "camara trasera", "cámara trasera",
]
# ★ 歧义裸词 headOnly（知识页实测 45→20 字符）：手机标题前 45 字塞得下规格 ——
#   「Oppo Find X9 … Pantalla 6.7」「Realme 14t … Batería 6000mah」「… 128GB Memoria」
#   「… Gorilla Glass」「… Fast charging 67W」「… + mouse」都是整机。约束和词绑死。
_ACC_HEAD_ONLY = frozenset({
    "pantalla", "vidrio", "cristal", "cubierta", "memoria", "cable", "cabo",
    "bateria", "batería", "display", "tela", "stand", "banda", "microsd", "micro sd",
    "glass", "charging", "mouse", "marco", "camara frontal", "cámara frontal",
    "camara trasera", "cámara trasera",
})
_HEAD_WINDOW = 20


def _accessory_word_re(words) -> "re.Pattern[str]":
    alts = sorted((w.strip() for w in words), key=len, reverse=True)
    return re.compile(_WB_L + r"(?:" + "|".join(re.escape(w) for w in alts) + r")(?:as|a|es|s)?" + _WB_R)


_ACCESSORY_WORD_RE = _accessory_word_re(_ACCESSORY_WORDS)
_ACC_HEAD_ONLY_RE = _accessory_word_re(_ACC_HEAD_ONLY)


def _accessory_pos(t: str) -> tuple[int, str]:
    """最早的配件词 (位置, 词)；歧义裸词只在前 _HEAD_WINDOW 字符内算。没有 → (-1, '')。"""
    for m in _ACCESSORY_WORD_RE.finditer(t or ""):
        if m.start() >= _HEAD_WINDOW and _ACC_HEAD_ONLY_RE.fullmatch(m.group(0)):
            continue
        return m.start(), m.group(0)
    return -1, ""


# "配件 para 设备" 的守卫词：命中后只看**前半段**是不是配件（变体与 _ACC_FORM_MARK 同族）
_FOR_GUARD = re.compile(r"\b(?:para|for|compatible(?:\s+con)?|compat[ií]vel(?:\s+com)?)\b|\bp/", re.I)
# 三态用的保护类名词：设备名打头、≤4 词内紧接它、且无捆绑/赠品标记 → 待定
#（「Xiaomi Pad Mini funda protectora」进待定，「Xiaomi Pad 6 Funda de regalo」仍是整机）。
_PROTECT_NOUN_RE = re.compile(
    _WB_L + r"(?:fundas?|cases?|capas?|capinhas?|micas?|l[aá]minas?|protector(?:es|a|as)?|"
    r"protetor(?:es|a|as)?|carcasas?|estuches?|forros?|pel[ií]culas?|hidrogel|vidrios?|cristal(?:es)?|"
    r"covers?|l[aá]pi(?:z|ces)|stylus|canetas?)" + _WB_R)
# 三态用的**佩戴类**尾随名词，与保护类同规则（设备名打头 + 无捆绑/赠品标记 → 待定）。
# ★ 为什么单列一张表而不是并进 _PROTECT_NOUN_RE：窗口口径不同。
#   保护类按「设备词与配件词之间 ≤4 个词」算（funda 出现得再靠后也仍是壳）；
#   佩戴类必须再加一道**主语窗口**（名词前面至多 _WEAR_LEAD_WORDS 个词，与 _ACC_FORM_RE 的
#   lead 同口径）—— 因为表带材质是整机的**规格写法**，位置比壳更靠后：
#     「WATCH GT 5 Correa」            correa 是第 4 个词（lead 3）→ 待定（可能是单卖表带）
#     「ACME WATCH GT 5 46mm Correa Fluoroelastomero」 correa 是第 6 个词（lead 5）→ 整机
#   只按「间隔 ≤4 词」判的话后者间隔只有 3 个词（gt / 5 / 46mm），会把整机拖进待定。
# ★ 名词必须落在设备词**之后**（acc_pos >= 设备词结尾）：「Pulseira/Pulsera Inteligente」
#   里两个词同位置命中，不加这道会把真手环判成待定（知识页原话：真手环靠同位置平局判整机）。
# ★ pulseras?（西语）现在**不在** _ACCESSORY_WORDS 里，所以这条对它是休眠的 ——
#   收裸 pulsera 会波及 price_audit 的地板取样，不在本次范围；先把词写在这里，
#   哪天 _ACCESSORY_WORDS 收了它，三态自动兜住（tests/test_accessory_kind.py 钉了这个现状）。
# ★ 已量化代价（2026-09-05 对全库 141,613 条 device 观测只读体检）：本规则把
#   **10 种标题 / 38 条观测**从 device 推进待定，全是真手表（Garmin Vivoactive 6 399,990 CLP、
#   Acme「Banda 11」219,900 COP、BLOOSOM 1,039 MXN×27…）。三态是安全方向，但它们会
#   静默退出价格分析，所以这笔账记在 tests/test_accessory_kind.py 的 KNOWN-GAP 里有人盯。
#   ★ 试过再加一道「设备词与佩戴词间隔 ≤1 词」把它们救回来：代价是
#     「Apple Watch Hermès – Correa En Mer … para caja de 49 mm」（9,999 MXN×5）从 unknown
#     退回 device —— 550 美元的表带挂进 Apple Watch 的价格线，那是更坏的一类错
#     （知识页：配件混进整机是挂在具名机型上的）。故不收紧。
_WEAR_NOUN_RE = re.compile(
    _WB_L + r"(?:correas?|pulseiras?|pulseras?|bandas?|mallas?|straps?)" + _WB_R
    + r"(?!\s+(?:inteligente|smart))")
_WEAR_LEAD_WORDS = 4
# 「同类设备依附于同类设备」的类别（仿品/第三方兼容品形态）。只在音频与穿戴上开：
#   「Audífonos Bluetooth Compatible Para Samsung Galaxy Buds 3 (Genéricos)」是耳机仿品，
#   「Smartwatch compatible con iPhone y Android」是真手表（穿戴依附手机，跨类不算）。
#   平板/手机不开：「Tablet para niños Tablet Infantil」这类复述会误伤。
#   ★ 标记之后必须是**产品线名**（Galaxy Buds / AirPods / Apple Watch / Mi Band…），不能是同一个通用词：
#     「Sennheiser Fone de ouvido para jogos …, Fone de ouvido, Game Zero」是把自己复述了一遍，不是仿品。
_SAME_CLASS_TAIL_RE = {
    "audio": re.compile(r"\w*buds\b|\bairpods\b|\bearpods\b|\bsoniclace\b|\bwf-\w+|\bwh-\w+"),
    "wearable": re.compile(r"\bapple\s*watch\b|\bgalaxy\s*watch\b|\bgalaxy\s*fit\b|\bwatch\s*(?:gt|fit|ultra|se)\b|"
                           r"\b(?:xiao)?mi\s*band\b|\bacme\s*band\b|\bredmi\s*watch\b|\bamazfit\b|"
                           r"\bsmart\s*band\b|\bacme\s*watch\b"),
}


def _same_class_after(head: str, tail: str) -> str | None:
    hn, tn = head.translate(_CAT_ACCENTS), tail.translate(_CAT_ACCENTS)
    for cat, tail_rx in _SAME_CLASS_TAIL_RE.items():
        mh, mt = _CAT_EVIDENCE_RE[cat].search(hn), tail_rx.search(tn)
        if mh and mt:
            return f"{mh.group(0)} → {mt.group(0)}"
    return None


def _is_trailing_wear_noun(t: str, dev_pos: int, dev_w: str, acc_pos: int) -> bool:
    """配件词是不是「设备名之后紧跟的佩戴类名词」（三态候选）。

    三道闸同时成立才算（少一道就会误伤，每道都对应一条实测）：
      ① 词本身在 _WEAR_NOUN_RE 里，且后面不是 inteligente/smart（那是真手环）；
      ② 位置在设备词**结尾之后** —— 挡住「Pulseira Inteligente …」两词同位置命中；
      ③ 名词前面至多 _WEAR_LEAD_WORDS 个词 —— 挡住
         「ACME WATCH GT 5 46mm Correa Fluoroelastomero」这种把表带材质当规格写的整机。
    """
    if not _WEAR_NOUN_RE.match(t, acc_pos):
        return False
    if acc_pos < dev_pos + len(dev_w):
        return False
    return len(t[:acc_pos].split()) <= _WEAR_LEAD_WORDS


def _device_then_accessory(t: str, dev_pos: int, dev_w: str,
                           acc_pos: int, acc_w: str) -> tuple[str, str]:
    """设备词在配件词之前。整机（含捆绑装）还是待定，看配件词是不是紧接的保护/佩戴类名词。"""
    if not (_PROTECT_NOUN_RE.match(t, acc_pos) or _is_trailing_wear_noun(t, dev_pos, dev_w, acc_pos)):
        return "device", f"整机词「{dev_w}」在配件词「{acc_w}」之前 → 整机（含捆绑装）"
    gap = t[dev_pos + len(dev_w):acc_pos]
    if len(gap.split()) > 4 or _BUNDLE_BEFORE_RE.search(gap) \
            or _GIFT_AFTER_RE.match(t[acc_pos + len(acc_w):]):
        return "device", f"整机词「{dev_w}」在前，「{acc_w}」是赠品/捆绑说明 → 整机（含捆绑装）"
    return "unknown", (f"设备名「{dev_w}」打头、紧接保护/佩戴类名词「{acc_w}」且无捆绑标记 → "
                       f"配件还是带壳整机说不清，待定")


def looks_like_device_bundle(title_low: str) -> str | None:
    """「整机 … (+|with|con|com|incluye) … 配件」= 整机捆绑装，不是配件。返回依据或 None。

    ★ 给权威表 skumap.is_accessory 的两条 yaml 规则当守卫：「keyboard 无 gb/ram」
      「contains apple pencil」会把 749,990 CLP 的「Slate 12X with keyboard inbox」、
      「iPad A16 + Apple Pencil」这类**捆绑整机**判成配件。回填工具在改判前先过这一道。
    """
    # 「com fio / con cable / sem fio」是有线/无线的说法，不是捆绑连接词 —— 先抹掉
    t = _WIRED_PHRASE_RE.sub(lambda m: " " * len(m.group(0)), title_low or "")
    dev_pos, dev_w = _device_pos(t)
    if dev_pos < 0:
        return None
    acc_pos, _ = _accessory_pos(t)
    if 0 <= acc_pos < dev_pos:                 # 配件词打头：「Funda con teclado para iPad」是配件
        return None
    dev_end = dev_pos + len(dev_w)
    for m in _BUNDLE_BEFORE_RE.finditer(t, dev_end):
        # 连接词必须连接的是「整机 ↔ 配件」：整机词与连接词之间已经有配件词的，
        # 连接的是「配件 ↔ 配件」（「Xiaomi Pad Mini funda protectora **con** correa」），不算捆绑
        if _accessory_pos(t[dev_end:m.start()])[0] >= 0:
            return None
        mt = _BUNDLE_TAIL_RE.search(t, m.end())
        if mt:
            return f"整机「{dev_w}」+ 捆绑标记「{m.group(0)}」+ 配件「{mt.group(0)}」→ 整机捆绑装"
    return None


_BUNDLE_TAIL_RE = _accessory_word_re(
    _BUNDLE_ACCESSORY_WORDS + ["pencil", "book cover", "s pen", "keyboard", "smart cover", "m-pen",
                               "m pencil", "stylus", "caneta"])
_WIRED_PHRASE_RE = re.compile(r"\b(?:com|sem|con|sin)\s+(?:fio|cable|cabo)\b")


def detect_product_kind(title: str) -> tuple[str, str]:
    """判定标题是【整机】【配件】还是【待定】。返回 (kind, 依据)。

    ★ 管线顺序本身就是规则，不能随便调换（移植自上一代项目的 83 项单测结论）：

      0. 句法规则 accessory_para_form —— 依附标记 / 主语位置强名词 / 结论性词 / 句首标记。
         最强信号最先走，且与 skumap、历史回填共用同一份实现。
      1. `para/for/compatible` 守卫 —— 必须在整机判定**之前**。
         配件标题里必然出现设备名（"Funda para iPhone 17"），不守卫就会被设备名拐走判成整机。
         ★ 前半段里配件词必须**早于任何设备词**（「GALAXY TAB ACTIVE 5 … 128GB Memoria … compatible」
           里 memoria 在设备词之后，是规格），歧义裸词只在前 20 字符算。
         ★ 前半段是设备、没有配件词、标记之后又是**同类**设备名 → 第三方兼容品/仿品 → 待定。
      2. 比位置，不是"整机一律优先"：
           "Mica + Funda Samsung Galaxy A71" —— 配件词在前 → 配件
           "Galaxy Tab S11 + Keyboard Cover" —— 整机词在前 → 整机（捆绑算整机，不需要特判）
         ★ 设备名打头、≤4 词内紧接保护类名词且无捆绑/赠品标记 → 待定（三态）。
           佩戴类名词（correa/pulseira/banda/malla/strap）同规则，另加一道主语窗口
           （名词前面 ≤4 个词），见 _is_trailing_wear_noun。
      3. 只有配件词 → 配件；只有整机词 → 整机
      4. 兜底 unknown（**不猜**，交给价格审计用基线判）
    """
    t = (title or "").lower().strip()
    if not t:
        return "unknown", "标题为空"

    # ⓪ 句法规则（与 skumap.is_accessory / 回填工具同一份实现）
    why0 = accessory_para_form(t)
    if why0:
        return "accessory", why0

    # ① 守卫：只看 para/for/compatible 前半段
    m = _FOR_GUARD.search(t)
    if m:
        head = t[:m.start()]
        dev_h, _ = _device_pos(head)
        acc_h, acc_hw = _accessory_pos(head)
        if acc_h >= 0 and (dev_h < 0 or acc_h < dev_h):
            return "accessory", (f"「{acc_hw}」+「{m.group(0)}」结构 → 配件"
                                 f"（前半段是配件名，且早于任何设备词）")
        if dev_h >= 0 and acc_h < 0:
            same = _same_class_after(head, t[m.end():])
            if same:
                return "unknown", (f"设备依附于同类设备（{same}，标记「{m.group(0)}」）"
                                   f"→ 第三方兼容品/仿品，待定")

    # ② 比位置
    dev_pos, dev_w = _device_pos(t)
    acc_pos, acc_w = _accessory_pos(t)

    if dev_pos >= 0 and acc_pos >= 0:
        if acc_pos < dev_pos:
            return "accessory", f"配件词「{acc_w}」出现在整机词「{dev_w}」之前（{acc_pos} < {dev_pos}）→ 配件"
        return _device_then_accessory(t, dev_pos, dev_w, acc_pos, acc_w)
    if dev_pos >= 0:
        return "device", f"命中整机信号「{dev_w}」"
    if acc_pos >= 0:
        return "accessory", f"命中配件词「{acc_w}」"

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
# 防丢器词表。★ rastreador/localizador 只认**标题开头**（那里是"这件商品
#   是什么"）：手表标题中段大量出现「Rastreador Fitness / de Actividad /
#   Salud」当**功能词**（实测 23 处里多数是真手表），全文匹配会误伤。
#   产品名（AirTag/SmartTag/Moto Tag）无歧义，全文认；裸 "tag" 不认
#   （会误伤 Tagus 等品牌名）。
_CAT_TRACKER = re.compile(
    r"^\s*(?:rastreador|localizador)\b"
    r"|\bairtag\b|\bsmart\s?tag\d?\b|\bmoto\s+tag\b")

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


@functools.lru_cache(maxsize=1)
def _brand_cats() -> dict:
    """品牌 → 允许品类集合（config/brands.yaml 的 categories，用户维护的口径）。

    用户 2026-08-28 原话：「我之前给你说过每个品类的主力品牌都是什么，
    怎么能把 moto 和 OPPO 当成穿戴品牌」—— 这份先验一直在配置里，
    但品类判定从来没用过它。取不到配置返回空表（先验失效但不报错）。
    """
    try:
        from ..config import load_brands
        return {b["name"].lower(): set(b.get("categories") or [])
                for b in load_brands().get("brands", [])}
    except Exception:                               # noqa: BLE001
        return {}


def crosscheck_category(title: str, ctx_category: str | None,
                        brand: str = "") -> tuple[str, str | None, str]:
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
    cats = {c for _, c, _ in ev}
    if ev and ctx_category in cats:
        # ★★ 2026-09-07 修：原来只要当前品类**出现在证据集里任意位置**就判 ok，
        #   而且理由里引用的是 ev[0]（最靠前那条），它可能属于**别的品类** ——
        #   于是「Tablet Samsung Galaxy S10 Lite 10.9"」在 ctx=phone 时返回
        #   「ok，标题含本品类证据『tablet』」：**拿平板的证据确认了手机**。
        #   后果不是判错一条，是这条产品**永远进不了 LLM 兜底层**（categorizer
        #   只接手 verdict==ok 且理由是"无任何品类证据"的），错误就此固化。
        #   实测 299 个 category_source 为空的产品全卡在这里。
        #   ⇒ 按知识页的「位置优先」：只有当前品类的**最靠前证据**不晚于其它品类的
        #     最靠前证据时才算确认；被更靠前的他类证据压过就落到下面的多品类分支
        #     （pending，交人/LLM 定夺）。失败方向是"待定"，不是"确认错的"。
        first = {}
        for pos, c, w in ev:
            if c not in first:
                first[c] = (pos, w)
        mine = first[ctx_category]
        earlier = [(p, c, w) for c, (p, w) in first.items()
                   if c != ctx_category and p < mine[0]]
        if not earlier:
            return "ok", None, f"标题含本品类证据「{mine[1]}」@{mine[0]}"
        p2, c2, w2 = min(earlier)
        return "pending", None, (
            f"本品类证据「{mine[1]}」@{mine[0]}，但更靠前有 {c2} 的证据"
            f"「{w2}」@{p2} → 待定（位置优先，不拿他类证据确认本品类）")
    # ★ 蓝牙防丢器（Moto Tag / SmartTag / AirTag）不属于任何分析品类：
    #   在渠道的"穿戴"页里卖，但既不是手表也不是手环，价格量级差一个
    #   数量级 —— 用户 2026-08-28 点名「巴西 moto 的那个穿戴也不太对」。
    #   置 pending（品类清空），自动排除出所有按品类的分析。
    #   ★ 必须放在「本品类证据」之后：手表标题里的「Rastreador Fitness」
    #   是功能词，先有 smartwatch/reloj 证据的根本轮不到这条。
    if _CAT_TRACKER.search(_cat_prep(title)):
        return "pending", None, "蓝牙防丢器/物品追踪器，非穿戴设备 → 待定"
    # ★ 品牌品类先验（brands.yaml 的 categories，用户维护）：标题说不出话、
    #   而品牌**只做一个品类**时按品牌改判（JBL 只做 audio ⇒ 穿戴桶里的
    #   「JBL Charge 6」判去音频）；多品类品牌不猜。品牌先验的优先级低于
    #   标题证据 —— 上面本品类/他品类证据都没命中才轮到这里。
    if not ev and brand:
        bc = _brand_cats().get(brand.strip().lower())
        if bc and ctx_category not in bc:
            if len(bc) == 1:
                only = next(iter(bc))
                return "fix", only, (f"品牌 {brand} 只做 {only}（用户口径），"
                                     f"且标题无 {ctx_category} 证据")
            return "pending", None, (f"品牌 {brand} 不做 {ctx_category}"
                                     f"（用户口径），标题无证据 → 待定")
    if not ev:
        return "ok", None, "标题无任何品类证据 → 不动（不猜）"
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
