# -*- coding: utf-8 -*-
"""清洗 Agent —— 把抓回来的原始网页数据变成结构化记录。

两个职责：
  1. LLM 兜底抽取：前两级（JSON-LD / 选择器）都失败的页面，
     文本落在 raw_page 表，这里交给模型抽 "产品-价格-促销价"。
  2. 型号归一化：把五花八门的电商标题收敛成同一个型号
     （"Samsung Galaxy S25 Ultra 512GB Negro Desbloqueado" → "Galaxy S25 Ultra"），
     并把 price_obs 挂到 rival_product 上。

★ 型号归一化是竞品匹配的前提。不做归一化，同一台机器在 6 个国家
  会被当成 6 个不同产品，价格对比、上市看板全都算不出来。
"""
from __future__ import annotations

import json
import logging
import re

from .. import db, skunorm
from ..scraping import extract
from .base import BaseAgent
from .llm import as_dicts

log = logging.getLogger("cleaner")

# ★ 型号归一化的词表。改这里等于改整个系统的数据聚合口径 ——
#   漏掉一个颜色词，同一台机器就会按颜色裂成好几个"产品"，
#   价格中位数、竞品对比、上市时序全部算不出来，而且不报错。

# 一般营销噪声
_NOISE = re.compile(
    r"\b(desbloqueado|liberado|libre|dual\s*sim|4g|5g|lte|nuevo|novo|original|"
    r"garantia|garantía|env[ií]o\s*gratis|frete\s*gr[áa]tis|entrega|oficial|"
    r"vers[ãa]o|version|global|homologado|anatel|nacional|importado|lacrado|"
    r"celular|smartphone|tel[ée]fono|m[óo]vil|tablet|notebook|laptop|"
    r"reloj|smartwatch|aud[íi]fonos|auriculares|fone|headphone|earbuds|"
    # 渠道/运营商后缀与套餐码：Sears 的标题长这样
    # "Celular Samsung A07 64Gb 4G Color Negro R9 (Telcel) SEARS"
    # 不剥就会归一化成 "A07 Color R9 SEARS"，同一台机器在不同渠道对不上
    # ★ 促销/配送文案：Falabella 的标题以「Envío gratis」打头，
    #   Fast Shop 有「Frete grátis」，不剥就会被当成型号名 ——
    #   于是几十个不同商品归一化成同一个"产品"，比价直接错乱。
    # \bapp\b 单独剥：Falabella 的「Envío gratis APP」里 App 与 gratis 之间
    # 常插着别的词，靠 "gratis app" 连写匹配不到，结果 App 留在型号名开头
    r"env[íi]o\s*gratis|envio\s*gratis|frete\s*gr[áa]tis|gratis\s*app|app\b|"
    # ★ 界面文案泄漏：Falabella 的卡片把「Vista Previa」（预览按钮）也抓进了标题。
    #   实测 2993 个产品里 **208 个**的型号名以它开头 —— 所有泄漏词里最大的一个。
    #   同类：「Recíbelo hoy」「Envío rápido」「TARJETA FEST」（发卡促销）。
    r"vista\s*previa|rec[íi]belo\s*hoy|env[íi]o\s*r[áa]pido|tarjeta\s*fest|"
    # ★ 品类/特性通用词：描述的是「这是什么东西」而非「哪一款」。
    #   不剥就会成为型号名的一部分，同一款机器按描述差异裂成多个产品。
    #   实测首词统计：bluetooth 71、ouvido 66（葡语"耳"）、inalámbricos 63、
    #   monitor 31、computador 31、in-ear 30、inteligente 29、gamer 29。
    r"plegable|inal[áa]mbric[oa]s?|bluetooth|in.?ear|true\s*wireless|tws|"
    r"inteligente|gamer|monitor|computador(?:a)?|port[áa]til|"
    r"fones?\s*(?:de\s*)?ouvido|ouvido|"
    # 配送政策文案：Falabella 的卡片里混着这些，抓到的有时是它们而不是商品名
    r"a\s*domicilio|llega\s*ma[ñn]ana|retira?\s*ma[ñn]ana|"
    r"retiro\s*en\s*un\s*punto|retiro\s*en\s*tienda|env[íi]o\s*r[áa]pido|"
    r"despacho\s*gratis|entrega\s*gratis|oferta\s*especial|hot\s*sale|"
    r"black\s*friday|cyber\s*(?:monday|day)|buen\s*fin|liquidaci[óo]n|"
    # 渠道自己的名字也会出现在标题里（"… Por FALABELLA"、"SEARS" 后缀）。
    # 不剥的话同一台机器在不同渠道归一化出不同型号名，跨渠道比价直接对不上。
    # 卖家归属的动词形式（"Vendido por X"）无歧义，出现在哪都不是型号的一部分。
    # 尾巴形态已被 _SELLER_TAIL 整段切掉，这里兜住卖家写在**前面**的写法。
    r"vendido|vendedor|ofrecido|comercializado|distribuido|por|"
    r"color|cor|add|sears|coppel|liverpool|sanborns|elektra|falabella|ripley|"
    r"alkosto|hiraoka|fast\s*shop|fastshop|shopee|mercado\s*libre|"
    r"telcel|movistar|"
    r"at&t|unefon|claro|entel|bitel|vivo\s+operadora|tim\s+operadora|"
    r"r\d{1,2}|reacondicionado|renovado|refurbished|seminuevo)\b", re.I)

# ★ 卖家归属尾巴：Falabella 的卡片标题一律以「… Por <卖家>」收尾
#   （"IPhone 14 128GB Reacondicionado Por Kiss Elec"）。
#   Marketplace 的卖家名千变万化，不剥掉的话同一台 iPhone 14 会**按卖家裂开**：
#   iPhone 14 Por Kiss / iPhone 14 Por FALABELLA / iPhone 14 Por REUSE …
#   裂开之后同 SKU 比价永远对不上（价格变动检测直接失效），
#   看板上的"覆盖机型数"也是虚高的 —— 实测 2227 个友商产品里 579 个是这么来的。
#   注意它必须**整段切到行尾**：卖家名后面往往还跟着 "Equipo + Plan" 之类的尾巴。
_SELLER_TAIL = re.compile(
    r"\s+(?:vendido|ofrecido|comercializado|distribuido|entregado)?\s*"
    r"\bpor\b\s+.*$", re.I)

# 屏幕技术短语：必须整体剥，不能逐词剥
# （"Super AMOLED plus" 逐词剥会留下孤零零的 plus，被当成型号的一部分）
_SCREEN_TECH = re.compile(
    r"\b(dynamic\s+amoled(\s*2x)?|super\s+amoled(\s+plus)?|fluid\s+amoled|"
    r"p-?oled|amoled|oled|lcd|ips|tft|retina|liquid\s+retina|ltpo|"
    r"pantalla|tela|display|pulgadas?|polegadas?|inch(es)?)\b", re.I)

# 颜色词：拉美电商标题几乎必带颜色，且各厂商爱用营销色名
_COLOR = re.compile(
    r"\b(negro|preto|black|blanco|branco|white|azul|blue|verde|green|"
    r"gris|cinza|gray|grey|grafito|graphite|dorado|dourado|gold|"
    r"plata|prata|silver|titanio|titanium|morado|roxo|purple|violeta|"
    r"rosa|pink|rojo|vermelho|red|amarillo|amarelo|yellow|naranja|orange|"
    r"crema|cream|beige|caf[ée]|marron|marrom|brown|lila|lavanda|menta|mint|"
    r"medianoche|midnight|starlight|natural|desert|ultramarine|teal|"
    r"celeste|turquesa|coral|arena|sand|obsidian|onyx|jade|zafiro|sapphire)\b",
    re.I)

# 括号内容、加号之后、容量/参数尾巴
_TRAILING = re.compile(
    # ★ 加号只在【两边有空格】时当分隔符剥掉。
    #   紧贴前一个词的 + 是型号的一部分，必须保留：
    #     "Galaxy S26+ Dynamic AMOLED"  → 保留 + ⇒ Galaxy S26+
    #     "Galaxy Tab S11 + Keyboard"   → 剥掉   ⇒ Galaxy Tab S11
    #   第一版写成 `[+/|,].*$` 把两种都剥了，于是 S26 与 S26+ 合并成同一个
    #   产品 —— 两个价位段混算，实测出现 88% 的假价差。
    r"[\(\[].*?[\)\]]|\s\+\s.*$|[/|,].*$|"
    # ★ 拉美挂牌极常见的「内存+存储」简写：12+512GB / 8+256G / 12+12GB。
    #   只剥 "512GB" 会留下孤零零的 "12+" 粘在型号名上（实测「600 Pro 12+」），
    #   同一款机器按配置裂成多个产品。必须把整个 N+N 结构一起剥。
    r"\b\d{1,3}\s*\+\s*\d{1,4}\s*(gb|tb|g|t)?\b|"
    r"\b\d{1,4}\s*(gb|tb|mah|mp|hz|w|nits)\b|"
    r"\b\d{1,2}[.,]\d{1,2}\s*(?=$|\s)", re.I)

# ★ 产品线前缀补全：电商标题常省掉产品线名，不补就跨渠道对不上。
#
#   实测：同一台 Galaxy A07
#     Liverpool  "SAMSUNG Galaxy A07 …"        → Galaxy A07
#     Sears      "Celular Samsung A07 64Gb …"  → A07        ← 对不上！
#   价格没法比，上市时序也算不出来。
#
#   规则：型号以「产品线字母 + 数字」开头且缺前缀时补上。
#   只在有把握的品牌上做 —— 补错比不补更糟（会把两个不同产品并成一个）。
_LINE_PREFIX_RULES = {
    "samsung": (
        # A07 / S26 / M54 / F55 / Z Flip8 / Note20 → Galaxy xxx
        re.compile(r"^(?:[asmfz]\d{1,3}\b|z\s*(?:flip|fold)|note\s*\d)", re.I),
        "Galaxy",
        ("galaxy", "tab", "watch", "buds", "book"),
    ),
    "motorola": (
        re.compile(r"^[ge]\d{1,3}\b", re.I),          # G06 / E22 → Moto G06
        "Moto",
        ("moto", "edge", "razr", "thinkphone"),
    ),
    "xiaomi": (
        re.compile(r"^note\s*\d", re.I),              # Note 15 → Redmi Note 15
        "Redmi",
        ("redmi", "poco", "mi ", "xiaomi", "mix", "pad"),
    ),
}


def _tidy_case(model: str) -> str:
    """统一型号名的大小写。

    去重键是小写的，所以 "GALAXY a16" 与 "Galaxy A16" 本来就会合并；
    但**显示名**取的是先入库那条的原样，界面上会同时出现两种写法，
    看起来像两个产品。这里统一成「首字母大写 + 型号码全大写」。
    """
    out = []
    for w in (model or "").split():
        canon = _CAMEL_WORDS.get(w.lower())
        if canon:                                            # iphone → iPhone
            out.append(canon)
        elif re.fullmatch(r"[a-z]\d{1,3}\+?", w, re.I):      # a16 / s26+ → A16 / S26+
            out.append(w.upper())
        elif re.fullmatch(r"[A-Z]{2,}\d*\+?", w):            # 已是全大写缩写
            out.append(w.capitalize() if w.lower() in _LOWER_WORDS else w)
        elif re.fullmatch(r"\d+[a-z]{0,2}", w, re.I):        # 纯数字/带单位
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower() if w.isalpha() else w.capitalize())
    return " ".join(out)


# ★ 品牌的驼峰写法必须原样保留 —— 统一大小写时最容易把 iPhone 写成 Iphone。
_CAMEL_WORDS = {w.lower(): w for w in (
    "iPhone", "iPad", "iMac", "iPod", "AirPods", "AirTag", "MacBook", "iOS",
    "ThinkPad", "ThinkBook", "IdeaPad", "IdeaTab", "Slate Tab", "AceBook",
    "SonicBuds", "WatchFit", "GT", "XPS", "ROG", "ZenBook", "VivoBook",
    "OnePlus", "ZenFone", "ROG", "TabS", "MagSafe",
)}

# 这些词即使全大写也要转成首字母大写（它们是普通词不是型号码）
_LOWER_WORDS = {"galaxy", "moto", "redmi", "poco", "edge", "razr", "ultra",
                "pro", "plus", "max", "fold", "flip", "note", "tab", "watch",
                "buds", "air", "mini", "lite", "fusion", "power", "play", "neo"}


def _restore_line_prefix(model: str, brand_name: str) -> str:
    rule = _LINE_PREFIX_RULES.get((brand_name or "").lower())
    if not rule or not model:
        return model
    pattern, prefix, already = rule
    low = model.lower()
    if low.startswith(already):
        return model
    if pattern.match(model):
        return f"{prefix} {model}"
    return model


# 型号前缀：这些是型号名的一部分，绝不能当品牌名剥掉
# （剥掉 Galaxy 之后 "Galaxy S26 Ultra" 变成 "S26 Ultra"，
#   而 Apple 的 "S26" 也存在，跨品牌就撞车了）
_MODEL_PREFIXES = {
    "galaxy", "moto", "redmi", "poco", "iphone", "ipad", "macbook", "airpods",
    "watch", "band", "nimbus", "astra", "slate", "acebook", "sonicbuds",
    "reno", "enco", "magic", "vivobook", "zenbook", "ideapad", "thinkpad",
    "inspiron", "pavilion", "aspire", "yoga", "legion", "victus", "omnibook",
}



# 条形码不是型号码。
# ★★ 实测事故：Alkosto 的 Algolia 索引把 **EAN/UPC 条形码** 放在 code_string 里，
#   采集时落进 price_obs.sku_code，而这里把 sku_code 当权威型号直接用 ⇒
#   友商产品表里出现 434 个形如「195949776212」的"型号"，还进了 149 条竞品对照，
#   直接显示在决策屏上。
#   更隐蔽的是**第二个症状**：走 sku_code 这条路会整个跳过下面 `if not model:`
#   分支，而**配件闸门就在那个分支里** —— 于是 EarPods、充电底座、转接头
#   全被建成了"竞品设备"，还参与规格与价格比对。
#   一个错误的取值，造成两类完全不同的污染。
# ⇒ 只有"像型号码"的才认：必须含字母，或短到不可能是条形码。
#   认不出来就返回空串，让它落到基于标题的归一化（那条路带配件闸门）。
_BARCODE = re.compile(r"^\d{8,14}$")


def _authoritative_sku(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if _BARCODE.match(s):
        return ""                       # EAN/UPC/GTIN —— 不是型号
    if s.isdigit() and len(s) >= 7:
        return ""                       # 纯长数字，同样可疑
    return s


class CleanerAgent(BaseAgent):
    name = "cleaner"
    role = "cleaner"
    description = "LLM 兜底抽取 + 型号归一化 + 挂接友商产品"

    def run(self, obs_date: str | None = None) -> dict:
        obs_date = obs_date or db.today()
        self.start(f"清洗 {obs_date}")

        n_extracted = self._extract_raw_pages()
        n_linked, n_created = self._normalize_and_link(obs_date)

        summary = (f"LLM 兜底抽取 {n_extracted} 条；型号归一化挂接 {n_linked} 条，"
                   f"新建友商产品 {n_created} 个")
        self.finish("ok", summary, n_extracted + n_linked, n_linked)
        return {"extracted": n_extracted, "linked": n_linked, "created": n_created}

    # ------------------------------------------------ LLM 兜底抽取

    def _extract_raw_pages(self, limit: int = 30) -> int:
        pages = db.q("""
            SELECT rp.*, c.country_code AS cc, co.currency, c.id AS ch_id
            FROM raw_page rp
            JOIN channel c ON c.id = rp.channel_id
            JOIN country co ON co.code = c.country_code
            WHERE rp.status='pending' LIMIT ?
        """, (limit,))
        if not pages:
            return 0
        if not (self.llm and self.llm.available()):
            self.log_step("LLM兜底抽取", parsed={"待处理": len(pages)},
                          decision="skipped", status="degraded",
                          reason="未配置 API Key，页面文本保留在 raw_page 待后续处理")
            return 0

        total = 0
        for p in pages:
            prompt = (
                f"下面是拉美某电商搜索结果页的可见文本（{'葡语' if p['cc'] == 'BR' else '西语'}）。\n"
                f"请抽取其中的**在售商品**及价格，输出 JSON 数组，每条：\n"
                '{"title":"商品完整标题","price":现价数字,"list_price":划线原价数字或null,'
                '"seller":"卖家名或null"}\n'
                f"规则：①price 填页面上的生效价（有划线原价时，划线的填 list_price）；"
                f"②价格只要数字，不要货币符号和千分位；"
                f"③该国货币是 {p['currency']}，"
                f"{'注意逗号是小数点（1.234,56 = 1234.56）' if p['currency'] in ('BRL', 'ARS') else ''}"
                f"{'注意该币种无小数位（1.234.567 = 1234567）' if p['currency'] in ('CLP', 'COP') else ''}；"
                f"④只要商品条目，广告位/推荐位/分类导航不要；⑤最多 20 条；"
                f"⑥不确定的价格不要编造，宁可漏也不要错。\n\n"
                + (p["text"] or "")[:9000])

            items = as_dicts(self.ask_json(
                "抽取页面商品", prompt, input_ref=p["url"][:150],
                system="你是数据抽取工具，只输出 JSON，不加任何解释。", default=[]))

            written = self._persist_extracted(items, p, obs_date=db.today())
            total += written
            with db.tx() as conn:
                conn.execute("UPDATE raw_page SET status=? WHERE id=?",
                             ("done" if written else "failed", p["id"]))
        return total

    def _persist_extracted(self, items: list[dict], page: dict, obs_date: str) -> int:
        written = 0
        with db.tx() as conn:
            for it in items[:20]:
                title = str(it.get("title") or "").strip()[:250]
                price = extract.parse_price(it.get("price"), page["currency"])
                if not title or price is None:
                    continue
                if not extract.price_is_sane(price, page["currency"]):
                    continue
                ram, rom = extract.parse_ram_rom(title)
                h = db.row_hash(obs_date, page["ch_id"], page["cc"], title, price)
                cur = conn.execute("""
                    INSERT OR IGNORE INTO price_obs(obs_date,country_code,channel_id,
                      title,ram_gb,rom_gb,color,list_price,sale_price,currency,
                      seller_name,condition,is_bundle,url,row_hash,audit_reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (obs_date, page["cc"], page["ch_id"], title, ram, rom,
                      extract.parse_color(title),
                      extract.parse_price(it.get("list_price"), page["currency"]),
                      price, page["currency"], str(it.get("seller") or "")[:80] or None,
                      extract.detect_condition(title),
                      1 if extract.detect_bundle(title) else 0,
                      page["url"], h, "LLM 从页面文本抽取（前两级解析均失败）"))
                if cur.rowcount:
                    written += 1
        return written

    # ------------------------------------------------ 型号归一化

    @staticmethod
    def normalize_model(title: str, brand_aliases: list[str],
                        brand_name: str = "") -> str:
        """把电商标题收敛成型号名。纯规则，可复现、零成本。

        "SAMSUNG Galaxy S26 Ultra Dynamic AMOLED 2X 6.9 pulgadas"  → "Galaxy S26 Ultra"
        "Samsung Galaxy Z Flip8 Crema 512GB"                        → "Galaxy Z Flip8"
        "Motorola G06 Cafe (4GB RAM/256GB), Celular Desbloqueado"   → "Moto G06"

        ★ 剥离顺序不能换：
          括号/加号尾巴 → 屏幕技术短语 → 颜色 → 营销噪声 → 品牌名
          屏幕短语必须在颜色之前整体剥（"Super AMOLED plus" 逐词剥会剩下 plus）；
          品牌名必须最后剥（先剥会让 "Galaxy" 这种型号前缀失去锚点）。
        """
        t = " " + (title or "") + " "

        # 卖家尾巴最先切：它后面的内容整段都不是型号。
        # 但要留个保险 —— 万一哪个渠道把卖家写在**前面**（"Vendido por X - iPhone 15"），
        # 整段切光会把型号本身也吃掉。切完必须还剩得下东西才采用，
        # 宁可留点噪声，也不能归一化出一个空型号（空型号会让所有商品挤成同一个产品）。
        cut = _SELLER_TAIL.sub(" ", t)
        if re.search(r"\d", cut) or len(cut.split()) >= 3:
            t = cut

        t_raw = t          # 留一份"只切了卖家尾巴"的，兜底时还要用它找区分度
        t = _TRAILING.sub(" ", t)
        t = _SCREEN_TECH.sub(" ", t)
        t = _COLOR.sub(" ", t)
        t = _NOISE.sub(" ", t)

        # 只剥品牌主名，不剥型号前缀。
        # "Samsung Galaxy S26" → "Galaxy S26"（对）
        # 若把 aliases 里的 Galaxy 也剥掉 → "S26"，跨品牌会撞车（错）
        strip_words = [brand_name] if brand_name else []
        strip_words += [a for a in brand_aliases
                        if a.lower() not in _MODEL_PREFIXES
                        and " " not in a.strip()]

        def _strip(text: str, words: list[str]) -> str:
            for a in sorted(set(w for w in words if w), key=len, reverse=True):
                text = re.sub(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", " ",
                              text, flags=re.I)
            return text

        def _finish(text: str) -> str:
            text = re.sub(r"[^\w\s.+-]", " ", text)
            text = re.sub(r"\s{2,}", " ", text).strip(" -–—,|/.")
            toks = [x for x in text.split() if x and not _is_junk_token(x)]
            # 型号名极少超过 4 个词（Galaxy Z Fold8 Ultra 已是上限）
            return " ".join(toks[:4])[:80].strip()

        # ★ 归一化出**空型号或纯数字型号**是最危险的结果，比留着噪声糟得多：
        #   所有剥空的商品会挤进同一个"产品"。实测 Garmin 的别名是
        #   [Garmin, Forerunner, Fenix, Venu, Instinct…]，产品线名被当品牌名剥掉，
        #   于是 Instinct E 和 Fenix E 双双变成空 —— 两块不同的表并成一个，
        #   价格 249,990 ↔ 799,990 还被记成"同一款在降价"。
        #   _MODEL_PREFIXES 那张白名单不可能穷举所有品牌的产品线，改成自纠：
        #   逐级退让，只要还剩得下**带字母的**东西就用它。
        def _usable(m: str) -> bool:
            # ★★ 这里原本还要求"必须含字母"（`re.search(r"[^\W\d_]", m)`），
            #   本意是防止剥成空。但**大量品牌的型号名本来就是纯数字**：
            #   Honor 70 / Honor 600 / Xiaomi 17 / Bose 700 —— 品牌另存一列，
            #   型号叫「70」完全合法。
            #   后果很隐蔽：剥对了反被判"不可用"，于是逐级退让、最终回落到
            #   **只切过卖家尾巴的原文**，产出「Envío Gratis App Teléfono Móvil…」
            #   这种整段促销文案当型号名。实测 2993 个产品里 264 个（8.8%）
            #   是这么来的 —— 看起来像"没剥干净"，其实是"剥干净了但被否决"。
            #   真正要防的是**空**，不是**纯数字**。
            return bool(m and m.strip(" -–—.+"))

        model = _finish(_strip(t, strip_words))

        # ★★ 「剥成纯数字」与「剥成空」是两回事，必须分开处理 ——
        #   我一度把两者合成一个守卫（要求结果必须含字母），结果：
        #     · 好的一面：Garmin 的产品线名 Forerunner 被当别名剥掉后，
        #       结果 '970' 是纯数字 → 触发回退 → 保住 'Forerunner 970' ✓
        #     · 坏的一面：Honor 70 / Xiaomi 17 / Bose 700 的型号名**本来就是纯数字**，
        #       也被判不可用 → 一路回退到原始噪声文本，产出
        #       「Envío Gratis App Teléfono Móvil…」当型号名（实测 264 个）✗
        #   正确做法：纯数字本身不是错误，**丢了产品线名**才是。
        #   所以只在「全剥后是纯数字、而只剥品牌还留着字母」时才改用后者。
        brand_only = _finish(_strip(t, [brand_name])) if brand_name else ""

        def _numeric_only(x: str) -> bool:
            return bool(x) and not re.search(r"[^\W\d_]", x)

        if brand_only and _numeric_only(model) and not _numeric_only(brand_only):
            model = brand_only
        if not _usable(model) and brand_name:
            # 退让①：只剥品牌主名
            model = brand_only or model
        if not _usable(model) and brand_name:
            # 退让②：回到只切过卖家尾巴的原文。屏幕尺寸这类"规格"平时算噪声，
            # 但当标题里除了规格什么都没有时（"XIAOMI Smartwatch 1.48 Pulgadas"），
            # 它就是唯一的区分度 —— 剥掉的话 1.48 和 2.07 会并成同一块表。
            model = _finish(_strip(t_raw, [brand_name]))
        if not _usable(model):
            model = _finish(t_raw)

        return _tidy_case(_restore_line_prefix(model, brand_name))

    @staticmethod
    def restore_line_prefix(model: str, brand_name: str) -> str:
        """对外暴露，便于测试"""
        return _restore_line_prefix(model, brand_name)

    def _normalize_and_link(self, obs_date: str) -> tuple[int, int]:
        # ★ 配件不建竞品产品记录。
        #   实测回填后库里出现了 "Mica"（贴膜）、"Funda VRS Terra Guard"（保护壳）
        #   这样的"竞品"。它们会污染竞品匹配（拿保护壳去和手机比规格），
        #   也会把价格基线拽低。配件的价格仍然入库，只是不挂产品。
        rows = db.q("""
            SELECT po.*, b.name AS brand_name, b.aliases
            FROM price_obs po JOIN brand b ON b.id = po.brand_id
            WHERE po.obs_date=? AND po.rival_product_id IS NULL
              AND po.brand_id IS NOT NULL AND b.is_ours=0
              AND COALESCE(po.product_kind,'unknown') <> 'accessory'
        """, (obs_date,))
        if not rows:
            return 0, 0

        linked, created = 0, 0
        cache: dict[tuple, int] = {}
        for r in rows:
            try:
                aliases = json.loads(r["aliases"] or "[]")
            except Exception:  # noqa: BLE001
                aliases = []
            # ★ 权威 SKU 优先（用户的 PowerQuery SKU_Short 表）。
            #   它是人工维护的穷举映射，比通用归一化精确得多，
            #   且是用户既有报表的口径 —— 两边必须对得上。
            #   采集时已判定并存进 price_obs.sku_code，这里直接用。
            model = _authoritative_sku(r.get("sku_code"))
            verified = False
            if not model:
                # ★ 优先走 nubimetrics 的清洗规则（app/skunorm.py 说明了为什么是引用）。
                #   它带按品牌命名规律写的型号抽取 + **联网查证过的官方名表**，
                #   比这里的通用归一化准得多，而且能告诉我们哪些名字是查证过的。
                #   拿不到那个项目时如实降级回本地规则，不假装成功。
                res = skunorm.classify(r["brand_name"], r["title"],
                                       r["category_code"])
                if res["kind"] == "配件":
                    continue          # 配件不建竞品产品（与下面的 accessory 闸一致）
                if res["sku"] and res["source"] not in ("unavailable", "error"):
                    model, verified = res["sku"], bool(res["verified"])
                else:
                    model = self.normalize_model(r["title"], aliases, r["brand_name"])
            if len(model) < 3:
                continue
            key = (r["brand_id"], model.lower().replace(" ", ""), r["category_code"])
            pid = cache.get(key)
            if pid is None:
                pid, is_new = self._upsert_rival(r["brand_id"], model, key[1],
                                                 r["category_code"], verified)
                cache[key] = pid
                created += 1 if is_new else 0
            with db.tx() as conn:
                conn.execute("""UPDATE price_obs SET rival_product_id=?, model_guess=?
                                WHERE id=?""", (pid, model, r["id"]))
            linked += 1

        self.log_step("型号归一化", parsed={"挂接": linked, "新建产品": created,
                                            "样例": list(cache.keys())[:5]},
                      decision="ok",
                      reason=f"把 {linked} 条挂牌收敛到 {len(cache)} 个型号，"
                             f"其中 {created} 个是首次出现的新产品")
        return linked, created

    @staticmethod
    def _upsert_rival(brand_id: int, model: str, model_key: str,
                      category: str, verified: bool = False) -> tuple[int, bool]:
        existing = db.q1("""SELECT id, name_verified FROM rival_product
                            WHERE brand_id=? AND model_key=? AND category_code=?""",
                         (brand_id, model_key, category))
        if existing:
            # 已存在的行：只允许"未查证 → 已查证"单向升级。
            # 反向覆盖会让一个查证过的官方名被后来某条规则输出改掉。
            if verified and not existing["name_verified"]:
                with db.tx() as conn:
                    conn.execute("""UPDATE rival_product
                                    SET model_name=?, name_verified=1
                                    WHERE id=?""", (model, existing["id"]))
            return existing["id"], False
        with db.tx() as conn:
            cur = conn.execute("""
                INSERT INTO rival_product(brand_id,category_code,model_name,model_key,
                                          name_verified,name_source)
                VALUES(?,?,?,?,?,?)
            """, (brand_id, category, model, model_key,
                  1 if verified else 0, "nubimetrics" if verified else None))
            return cur.lastrowid, True


# 归一化后要丢掉的残渣 token
_JUNK_TOKENS = {
    "de", "con", "para", "y", "e", "o", "the", "a", "com", "sem", "tel",
    "pro5g", "5g", "4g", "lte", "wifi", "wi-fi", "nfc", "esim",
    # 容量单位词：数字部分已被 _TRAILING 剥掉，只剩下光秃秃的单位词。
    # "12GB RAM eSIM" → 归一化出 "iPhone 15 RAM"，于是同一台 iPhone 15
    # 因为标题写没写 RAM 而裂成两个产品。
    "ram", "rom", "gb", "tb", "mb", "gbram", "gbrom",
}


def _is_junk_token(tok: str) -> bool:
    low = tok.lower().strip(".-")
    if low in _JUNK_TOKENS:
        return True
    # 纯符号或单个字母（"S" 这种要留，因为 "11.5 S" 是型号；但 "-" 不要）
    return not any(c.isalnum() for c in low)
