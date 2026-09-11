# -*- coding: utf-8 -*-
"""葡语/西语配件漏判回归 —— 2026-08-27 Fast Shop 巴西事故的定点测试。

事故：「Capa para Tablet Acme Slate Tab…」「Película para ACME Slate 11.5」
「Capa … com Teclado Bluetooth」全被标成 product_kind='device'，混进价格分析，
把Acme巴西平板 ASP 拉到 20 美元。

根因：权威表 skumap 的配件词移植自用户 PowerQuery（西语+英语），葡语词
一个都拦不住；配件检查没拦住的标题继续往下命中 Slate Tab 的 SKU 规则，
短路判成整机 —— 通用 detect_product_kind 的 para 守卫根本没轮到上场。

修复：extract.accessory_para_form（「主语位置配件词 + para/de」句法规则，
单一实现），skumap.is_accessory 与 detect_product_kind 共用；
tools/backfill_accessory_kind.py 用同一份实现回填历史行。

跑法： python tests\test_accessory_kind.py
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import skumap                       # noqa: E402
from app.scraping import extract             # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def sk(title):
    """权威表路径（平板品类）—— 事故发生的入口"""
    return skumap.classify(title, "tablet")["kind"]


def dk(title):
    """通用启发式路径"""
    return extract.detect_product_kind(title)[0]


print("== 事故原样标题（skumap 权威表路径，必须判 accessory）==")
check("Capa para（保护壳）",
      sk("Capa para Tablet Acme Slate 11.5 Polegadas Antichoque"), "accessory")
check("Película para（贴膜）",
      sk("Película para ACME Slate 11.5"), "accessory")
check("Capa com Teclado（键盘壳）",
      sk("Capa com Teclado Bluetooth para Acme Slate 11.5"), "accessory")

print("== 权威表既有行为不得回归 ==")
check("Slate Tab 整机仍归 SKU",
      sk("Acme Slate 11.5 128GB Wifi Cinza"), "sku")
check("keyboard+gb 捆绑仍算整机（M 语义 1:1）",
      sk("Samsung Galaxy Tab S9 128gb + Book Cover Keyboard"), "sku")
check("西语 funda 前缀仍走 yaml 词表",
      sk("Funda para iPad 10.9 Transparente"), "accessory")
check("葡语 capa de … para",
      sk("Capa de Silicone para Samsung Galaxy Tab A9"), "accessory")
check("主语是平板：com capa de brinde 不得误杀",
      sk("Tablet Samsung Galaxy Tab A9 128gb com capa de brinde") != "accessory",
      True)

print("== 通用启发式：西/葡配件词 + para/de ==")
check("correa para", dk("Correa para Apple Watch 44mm"), "accessory")
check("estuche para", dk("Estuche para iPad 10.9"), "accessory")
check("teclado para", dk("Teclado para Galaxy Tab S9"), "accessory")
check("caneta … para", dk("Caneta Stylus para Tablet"), "accessory")
check("cargador para", dk("Cargador para Xiaomi Pad 6"), "accessory")
check("pulseira para", dk("Pulseira para Xiaomi Smart Band 10"), "accessory")
check("película de vidro", dk("Película de vidro temperado Galaxy Tab A9"),
      "accessory")
check("capinha 免标记（葡语壳专称）",
      dk("Capinha Samsung Galaxy A15 Transparente"), "accessory")
check("mica + funda 位置规则（docstring 既有案例）",
      dk("Mica + Funda Samsung Galaxy A71"), "accessory")

print("== 残留体检补入的形态（都是库里真实标题，逐条人查过）==")
# 这些不是编的样本：首轮回填 658 行后，对"平板品类里最便宜的 device 行"
# 做残留体检顶出来的四类漏网，逐条确认是配件后才加进规则。
check("Cabo … para（葡语线缆，介词在第 7 个词）",
      dk("Cabo Lightning USB-C 1 com 1,5 Metros para iPod, iPhone e iPad - Geonav"),
      "accessory")
check("Suporte … Para（葡语支架）",
      dk("Suporte Metálico 360 - Discovery - Para Tablet / iPad - Prata"), "accessory")
check("Vidrio Templado para（多词名词）",
      dk("Vidrio Templado para iPad Air 11 Chip M4 2024"), "accessory")
check("Lámina De Vidrio Templado Para",
      dk("Lámina De Vidrio Templado Para Honor Pad X9 2024"), "accessory")
check("Caneta Stylus … para（介词离名词 6 个词）",
      dk("Caneta Stylus Bettdow Touch Screens POM Nib para Android"), "accessory")
check("Smart Folio … de（无 para，靠 de）",
      dk('Smart Folio APPLE iPad Air de 13" Chip M2 Verde Salvia'), "accessory")
check("Base para laptop（散热支架，被记在平板品类）",
      dk("STEREN Base para Laptop Ajustable"), "accessory")
check("Case for（英语 for 标记）",
      dk("Case for Xiaomi Pad 6s Pro Funda 12.4 Pulgadas Acrílico"), "accessory")
check("Forro Estuche … Para（多配件词串联）",
      dk("FORRO ESTUCHE TECLADO TRACK PAD PARA IPAD A16 2025 + MOUSE"), "accessory")
check("Cubre Teclado de Silicona para MacBook",
      dk("Cubre Teclado de Silicona para MacBook Air 15.3 M2 / M3"), "accessory")

print("== 营销/UI 前缀把配件词挤出前两个词（主语窗口放到 4 的理由）==")
# Falabella 的 "Envío gratis app"、Paris/Ripley 的 "Vista Previa" 都是
# 页面文案漏进标题，前缀长度 2~4 个词，配件词被挤到第 3~5 位。
check("Envío gratis app + 卖家名 + Funda",
      dk("Envío gratis app TECNOPALACE Funda Con Teclado Para Honor Pad X9"),
      "accessory")
check("Envío gratis GENERICO FORRO ESTUCHE",
      dk("Envío gratis GENERICO FORRO ESTUCHE TECLADO TRACKPAD + VIDRIO PARA IPAD"),
      "accessory")
check("Vista Previa + 卖家名 + Funda",
      dk("Vista Previa Tecnopalace Funda Teclado 2.0 Para Xiaomi Pad 8 - 11\" Pulg"),
      "accessory")
check("Vista Previa + LAMINA MICA … PARA TABLET",
      dk("Vista Previa GENÉRICO LAMINA MICA VIDRIO TEMPLADO PARA TABLET XIAOMI PAD 2"),
      "accessory")
check("Kepuch 2 Paquetes Vidrio Templado … para",
      dk("Kepuch 2 Paquetes Vidrio Templado Protector de Pantalla para Xiaomi Pad 6S"),
      "accessory")

print("== ★★ 主语窗口放宽后，veto 表是唯一防线：产品线名必须收全 ==")
# 窗口 4 意味着配件词可以出现在第 5 个词，下面每条的设备词都落在它前面，
# veto 漏收任何一个厂商叫法，这台整机就会进配件桶。
check("Xiaomi Pad 6 送壳", dk("Xiaomi Pad 6 Funda de regalo 128gb"), "device")
check("Redmi Pad SE 送壳", dk("Redmi Pad SE 8.7 Funda de regalo"), "device")
check("Honor Pad X9 送壳", dk("Honor Pad X9 11.5 Funda con teclado de regalo"), "device")
check("Lenovo Tab M11 送壳", dk("Lenovo Tab M11 128gb Funda de regalo"), "device")
check("Slate Tab 送膜", dk("Acme Slate 11.5 S Película de regalo"), "device")
check("Galaxy Tab S10 送键盘",
      dk("Samsung Galaxy Tab S10 FE Teclado de regalo 256gb"), "device")

print("== ★ 不能误杀：真设备必须还是 device ==")
check("西语真手环 Pulsera Inteligente",
      dk("Pulsera Inteligente Xiaomi Smart Band 10"), "device")
check("葡语真手环 Pulseira Inteligente",
      dk("Pulseira Inteligente M7 Monitor Cardiaco"), "device")
check("捆绑算整机（dev 词在前）",
      dk("Galaxy Tab S11 + Keyboard Cover 256gb"), "device")
check("laptop 提到 teclado 不误杀",
      dk("Laptop HP 15 Teclado en Español 8GB RAM"), "device")
check("普通手机", dk("Smartphone Samsung Galaxy A55 128GB"), "device")
check("普通平板", dk("Tablet Acme Slate 11.5 128gb"), "device")
# ★ 窗口放宽到 8 个词后，挡误杀的全靠"名词必须在主语位置且前面没有设备词"。
#   下面每条都是「设备词打头 + 后面提到配件」，窗口再宽也不许命中。
check("平板送壳（设备词打头）",
      dk("Tablet Samsung Galaxy Tab A9 128gb con funda de regalo"), "device")
check("笔记本提到键盘规格",
      dk("Notebook Lenovo IdeaPad 3 teclado retroiluminado de 15 pulgadas"), "device")
check("手机含充电器规格",
      dk("Smartphone Motorola Edge 50 com carregador de 68W incluso"), "device")
check("iPad 整机送 Smart Folio",
      dk("Apple iPad Air 13 M4 256gb con Smart Folio de regalo"), "device")
check("Slate Tab 整机含手写笔",
      dk("Acme Slate 11.5 con lápiz Stylus Pen de regalo"), "device")

print("== accessory_para_form 单元边界 ==")
check("手环营销词 de…para 不触发",
      extract.accessory_para_form(
          "pulseira inteligente de monitoramento para corrida"), None)
check("配件词不在主语位置不触发",
      extract.accessory_para_form(
          "tablet samsung galaxy tab a9 com capa de brinde"), None)
check("主语位置 capa para 触发",
      extract.accessory_para_form(
          "capa para tablet acme slate 11.5") is not None, True)
check("品牌打头 + 配件词仍触发",
      extract.accessory_para_form("elago funda para ipad 10.9") is not None, True)

# =====================================================================================
# 2026-09-04 #2292「Lenovo Tab」曲线跳变事故：60 条人工归类的真配件里旧管线只认出 2 条（召回 5%）。
# 下面每一段对应 knowledge/lessons/host-vs-accessory-classification.md 的一条陷阱，
# 样例全部取自 price_obs 真实标题或用户截图。
# =====================================================================================
import ast                                            # noqa: E402
from types import SimpleNamespace                     # noqa: E402
from app.scraping.channels.base import ChannelAdapter, Listing   # noqa: E402

print("== ★ 词表缺词（lamina 684 / hidrogel 511 / almohadillas 237 / manilla 39 条 device 观测）==")
check("ROCK SPACE LAMINA HIDROGEL PARA TABLET（用户样本 13,990 CLP）",
      dk("ROCK SPACE LAMINA HIDROGEL PARA TABLET LENOVO PAD PLUS"), "accessory")
check("Lamina De Pantalla Completa Para IPhone 16",
      dk("Lamina De Pantalla Completa Para IPhone 16"), "accessory")
check("Almohadillas … para AirPods",
      dk("Almohadillas cubierta ultrafina para AirPods de Apple Negro"), "accessory")
check("Manilla Compatible Con（无 con 变体也要认）",
      dk("Manilla Compatible Con Xiaomi Redmi Watch 5 Active Y Lite"), "accessory")
check("Lamina Hidrogel Para Galaxy Tab A11（id 84887，曾被规则 accepted 8,790 CLP）",
      dk("Lamina Hidrogel Para Samsung Galaxy Tab A11 87 X130"), "accessory")

print("== ★ 位置优先遇「结论性词」必须让路 ==")
check("Teknet Mica Cristal Templado Samsung Galaxy Tab S9",
      dk("Teknet Mica Cristal Templado Samsung Galaxy Tab S9 X710"), "accessory")
check("Samsung Pantalla Notas Galaxy Tab S9（贴膜，pantalla 在主语位置）",
      dk("Samsung Pantalla Notas Galaxy Tab S9 Ultra NotePaper Pantalla"), "accessory")
check("设备名打头 + 结论性词（膜以设备命名）→ 三态待定，不许判整机",
      dk("Samsung Galaxy Tab S9 Mica Cristal Templado") in ("accessory", "unknown"), True)
check("★ 干跑高价端抓出的：Celular … Redmi Note 15 Pro … Power Bank 165W 是手机（设备词在前，结论性词让路）",
      dk("Celular I Redmi Note 15 Pro I 5G I 256GB I 8GB RAM I Power Bank 165W"), "device")
check("反例：HP NOTEBOOK 15.6 PANTALLA TACTIL 是笔记本（veto notebook）",
      dk("HP NOTEBOOK 15.6 PANTALLA TACTIL Core i5 8GB"), "device")
check("反例：整机 + 膜赠品（+ 捆绑标记）仍是整机",
      dk("Tablet Lenovo Tab M11 + Vidrio Templado de regalo"), "device")
check("反例：整机 con lámina（con 捆绑标记）仍是整机",
      dk("Tablet Lenovo Tab M11 128GB con Lámina de vidrio"), "device")
check("Cargador … Power Bank 是充电宝（结论性词）", dk("Cargador Xiaomi Power Bank 20000mah"), "accessory")

print("== ★ para 是句法定义：变体 compatible(无 con) / compatível / 句首形态 ==")
check("GENERICO Lapiz Pencil Optico Compatible Apple iPad（Falabella 裸 compatible）",
      dk("GENERICO Lapiz Pencil Optico Compatible Apple iPad Magnetico Por Asmtechnology"), "accessory")
check("Fones De Ouvido Com Fio … Compativel iPhone 15（葡语，无 com）",
      dk("Fones De Ouvido Com Fio Tipo C Estéreo Compativel iPhone 15"), "accessory")
check("句首依附标记（Amazon 形态，设备名在前）",
      dk("Compatible con Xiaomi Pad Mini 8.8 pulgadas 2025 funda delgada con soporte"), "accessory")
check("反例：Fone de ouvido com fio de alta qualidade（de 不是依附标记）",
      dk("Fone de ouvido com fio de alta qualidade JBL") != "accessory", True)

print("== ★ 设备词表漏收厂商叫法 = 整机被判成配件（同一课第四次复发：Yoga/Legion Tab、Band）==")
check("Lenovo Yoga Tab +Pen +Teclado", dk("Lenovo Yoga Tab de 11.1 Pulgadas 12gb Ram 256gb +Pen Pro +Teclado"), "device")
check("Lenovo Legion Tab Incluye Teclado", dk('Lenovo Legion Tab | 8,8" 2,5K | 12GB RAM 256GB | Incluye: Teclado'), "device")
check("Tab M11 … Folio Case + Lapiz（正则通道 tab m11）",
      dk("Tab M11 MediaTek Helio G88 4GB RAM 128GB Folio Case + Lapiz Tab Pen"), "device")
check("Pulseira Inteligente Xiaomi Band 9（band 不进名词表）",
      dk("Pulseira Inteligente Xiaomi Band 9 Active Tela 1,47 Monitoramento de Saúde"), "device")
check("Smartwatch, ACME Band 11, Tela AMOLED", dk("Smartwatch, ACME Band 11, Tela AMOLED"), "device")
check("Xiaoxin Pad 送壳", dk("Lenovo Xiaoxin Pad 2024 Funda de regalo 128gb"), "device")

print("== ★ 两头词界 / 假朋友 ==")
check("Beats Flex 是耳机（不收裸 flex）", dk("Beats Flex - Audífonos in-Ear inalámbricos"), "device")
check("GALAXY TAB ACTIVE 5 … Memoria … compatible（memoria 在设备词之后是规格）",
      dk("SAMSUNG GALAXY TAB ACTIVE 5 Enterprise 6GB RAM/128GB Memoria SM-X306 compatible"), "device")
check("60 tabletas 是药片，不是平板设备", dk("SALUD NATURAL Caltrón 600+D de 60 tabletas") != "device", True)
check("capa 不许命中 capacidad（词内命中）", dk("Xiaomi 14T 5G 512GB capacidad ampliada") != "accessory", True)
check("cable 不许命中卖家名 Cablex", dk("Celular Samsung Galaxy A16 128GB Por Cablex"), "device")
check("Oppo … Pantalla 6.7 是规格（pantalla headOnly 20 字符）",
      dk("Oppo Find X9 Pro 5g Titanio 512gb + 16gb, Pantalla 6.7") != "accessory", True)
check("Realme … Batería 6000mah 是规格", dk("Realme 14t 5g 256gb Batería 6000mah") != "accessory", True)
check("Batería Para Realme（主语位置 + para）", dk("Batería Para Realme Note 70t"), "accessory")

print("== ★ 语言覆盖（葡语裸词形态）==")
check("Película Samsung Galaxy Tab S10 FE（结论性/强名词，无 para，权威表路径）",
      sk("Película Samsung Galaxy Tab S10 FE Fosca HidroArmor Gshield"), "accessory")
check("Cabo Turbo Militar … iPhone / iPad", dk("Cabo Turbo Militar 1,5M Lightning USB-A iPhone / iPad"), "accessory")

print("== ★ 控制流：放宽前先加固 —— 权威表路径也要吃到新规则 ==")
r = skumap.classify("GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11 J606F- M.T", "tablet")
check("钢化膜（#2292 CL 8,500 那条）权威表判配件", r["kind"], "accessory")
check("且 sku_code 不得为 'Lenovo Tab'", r["sku"] != "Lenovo Tab", True)
check("Xiaomi Pad 6 Funda de regalo 仍是 SKU", sk("Xiaomi Pad 6 Funda de regalo 128gb"), "sku")
check("Tablet + capa de brinde 仍是 SKU", sk("Tablet Samsung Galaxy Tab A9 128gb com capa de brinde"), "sku")
check("keyboard + gb 捆绑仍是 SKU", sk("Samsung Galaxy Tab S9 128gb + Book Cover Keyboard"), "sku")

print("== ★ 三态：冲突/证据不足 → unknown，不定案 ==")
check("Xiaomi Pad Mini funda protectora（设备名打头紧接壳、无捆绑标记）",
      dk("Xiaomi Pad Mini funda protectora con correa de mano"), "unknown")
check("Audífonos Compatible Para Galaxy Buds 3 (Genéricos)：同类设备依附 → 仿品待定",
      dk("Audífonos Bluetooth Compatible Para Samsung Galaxy Buds 3 (Genéricos)"), "unknown")
check("反例：Smartwatch compatible con iPhone y Android 是真手表（跨类依附不算）",
      dk("Smartwatch compatible con iPhone y Android pantalla AMOLED"), "device")
check("反例：Tablet para niños con funda（con 捆绑）是整机", dk("Tablet para niños con funda Android 13"), "device")
check("Micrófono para celular 是配件", dk("Micrófono para celular inalámbrico"), "accessory")
check("Micrófono de condensador USB para streaming 不是配件（para 后无设备名）",
      dk("Micrófono de condensador USB para streaming") != "accessory", True)

print("== ★ 佩戴类尾随名词（correa/pulseira/banda/malla/strap）三态 —— 2026-09-05 回归 ==")
# 事故：上一轮给 _DEVICE_WORDS 补了 `watch gt|fit|ultra`，「WATCH GT 5 Correa」从 unknown
# 变成 device；而 sites.py 的Acme商城兜底（_ACME_ACCESSORY）**只在 product_kind=='unknown'
# 时才跑**，于是那道兜底永远不触发，test_acmestore 35/1。
# 修法：把佩戴类名词加进「设备名打头、紧接配件名词、无捆绑标记 → 待定」的三态组，
# 另加一道主语窗口（名词前面 ≤4 个词），见 extract._is_trailing_wear_noun。
# ★ 下面三条是这条规则的**全部**边界，一条都不许松（松哪条就复发哪个事故）：
check("★ WATCH GT 5 Correa → 待定（Acme商城兜底靠 unknown 才跑得到）",
      dk("WATCH GT 5 Correa"), "unknown")
check("★ ACME WATCH GT 5 46mm Correa Fluoroelastomero → 整机（表带材质是规格，不在 ≤4 词窗口）",
      dk("ACME WATCH GT 5 46mm Correa Fluoroelastomero"), "device")
check("★ Correa para WATCH GT 5 → 配件（依附标记，最强信号）",
      dk("Correa para WATCH GT 5"), "accessory")
# ★ 为什么第一条必须是 unknown 而不是别的：sites.py 的Acme商城兜底（_ACME_ACCESSORY 词表）
#   **只在 product_kind == 'unknown' 的分支里跑**。判成 device 就等于把那道兜底废掉。
#   这条耦合用 ast 断（字符串搜全文会匹配到上面这段注释里的 'unknown' —— 知识页
#   assertions-that-verify-nothing：越重要的性质旁边注释写得越认真，越容易中招）。
#   端到端行为本身由 tests/test_acmestore.py 守（那边有真 adapter + 临时库）。


def _acme_gate_on_unknown(src: str) -> bool:
    """源码里 _ACME_ACCESSORY.search(...) 是否落在 `product_kind == "unknown"` 的 if 体内。"""
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
            continue
        left = node.test.left
        if not (isinstance(left, ast.Attribute) and left.attr == "product_kind"):
            continue
        if not any(isinstance(c, ast.Constant) and c.value == "unknown" for c in node.test.comparators):
            continue
        for body_stmt in node.body:
            for x in ast.walk(body_stmt):
                if isinstance(x, ast.Attribute) and x.attr == "search" \
                        and getattr(x.value, "id", "") == "_ACME_ACCESSORY":
                    return True
    return False


check("★ Acme商城配件兜底确实挂在 product_kind=='unknown' 分支下（ast，不是搜字符串）",
      _acme_gate_on_unknown((ROOT / "app/scraping/channels/sites.py").read_text(encoding="utf-8")), True)
# 反向自检：把闸门换成别的值，检测器必须变红 —— 不会红的断言等于没写
check("反向自检：闸门写成 'accessory' 时检测器报 False",
      _acme_gate_on_unknown(
          'if lst.product_kind == "accessory":\n    _ACME_ACCESSORY.search(lst.title)\n'), False)
check("反向自检：兜底挪出 if 体外时检测器报 False",
      _acme_gate_on_unknown(
          'if lst.product_kind == "unknown":\n    pass\n_ACME_ACCESSORY.search(lst.title)\n'), False)
# 不许误伤：真手环 / 表带在设备词之后较远处 / 裸表带
check("真手环 Pulseira Inteligente 仍是整机（同位置命中不算尾随）",
      dk("Pulseira Inteligente M7 Monitor Cardiaco"), "device")
check("Pulsera Inteligente Xiaomi Smart Band 10 仍是整机",
      dk("Pulsera Inteligente Xiaomi Smart Band 10"), "device")
check("Apple Watch Series 11 44mm Correa Deportiva（lead 5 词）仍是整机",
      dk("Apple Watch Series 11 44mm Correa Deportiva Negra"), "device")
check("裸表带 Correa cruzada – Rosa pálido 仍是配件（没有设备词）",
      dk("Correa cruzada – Rosa pálido"), "accessory")
check("Galaxy Watch 7 Banda deportiva → 待定（同规则）",
      dk("Galaxy Watch 7 Banda deportiva"), "unknown")
check("Malla para Apple Watch → 配件", dk("Malla para Apple Watch 44mm"), "accessory")
# ★ 现状钉死：西语裸 pulsera **不在** _ACCESSORY_WORDS（收它会波及 price_audit 地板取样）。
#   _WEAR_NOUN_RE 里写了 pulseras?，所以哪天词表收了它，三态自动兜住 —— 这条测试是那天的哨兵。
check("现状：pulseras? 已在佩戴名词表里（词表哪天收了裸 pulsera，三态自动生效）",
      extract._WEAR_NOUN_RE.match("pulsera deportiva") is not None, True)
check("现状：裸 pulsera 不在 _ACCESSORY_WORDS（改了这条要重跑全量）",
      any(w == "pulsera" for w in extract._ACCESSORY_WORDS), False)

print("== ★ 残留体检顶出来的形态（2026-09-04 干跑：每 (品类,币种) 最便宜的 device 行）==")
check("Lector MicroSD … para iPhone（36 行，词表只有 micro sd）",
      dk("Piodata iXflash Q mini Lector MicroSD USB-C para iPhone, iPad y Android con Backup"), "accessory")
check("Pad 8 Pro Pantalla 11.2 是平板（pad+数字正则通道）",
      dk("Pad 8 Pro Pantalla 11.2 16GB 512GB Snapdragon 8 Elite"), "device")
check("Sennheiser Fone de ouvido para jogos 不是仿品（标记后是复述不是产品线名）",
      dk("Sennheiser Fone de ouvido para jogos Game Zero, Fone de ouvido, Game Zero Black, One-size"), "device")
check("Charging para WATCH FIT（Acme商城充电器）", dk("Charging para WATCH FIT"), "accessory")
check("Garantía Extendida para Laptop（保修服务）", dk("Garantía Extendida para Laptop 12 Meses"), "accessory")
check("PORTA SMARTPHONE METALICO PARA MOTO", dk("PORTA SMARTPHONE METALICO PARA MOTO"), "accessory")
check("Tarjetero para celular", dk("GROUND ELECTRONICS Tarjetero para celular unisex"), "accessory")
check("Mouse … Para Apple MacBook", dk("Mouse Slim Silencioso Recarregável Bluetooth Para Apple MacBook Neo (A18) e iPad"), "accessory")
check("CAMBIO DE TACTIL IPAD（维修服务）", dk("CAMBIO DE TACTIL IPAD 3 Y 4"), "accessory")
check("Cerámica Flexible … Para acme slate（陶瓷膜）", dk("Cerámica Flexible matte antihuella Para acme slate 11"), "accessory")
check("Flex Antena Wifi Compatible con iPhone 6（维修件）", dk("Flex Antena Wifi Compatible con iPhone 6"), "accessory")
check("Camara Frontal + Sensor Proximidad Compatible iPhone 6 Plus（维修件）",
      dk("Camara Frontal + Sensor Proximidad Compatible iPhone 6 Plus"), "accessory")
check("Estabilizador para Smartphone", dk("AOC~HUAN Estabilizador para Smartphone, Estabilizador 3 Eixos"), "accessory")
check("Fones De Ouvido Com Fio … Compativel iPhone 15（com fio 不是捆绑连接词）",
      dk("Fones De Ouvido Com Fio Tipo C Intra Auricular Microfone Integrado Compativel iPhone 15 15 Pro"), "accessory")
check("… 且捆绑守卫不许把它保住",
      extract.looks_like_device_bundle("fones de ouvido com fio tipo c intra auricular microfone integrado compativel iphone 15"), None)
check("反例：Gorilla Glass 是规格（glass headOnly）", dk("Xiaomi 14T 5G 512GB Gorilla Glass Victus") != "accessory", True)
check("反例：Samsung Galaxy Tab S9 FE 128GB S Pen Gris 是整机（S Pen 随箱）",
      dk("Samsung Galaxy Tab S9 FE 128GB S Pen Gris"), "device")
check("反例：Mouse Pad 90x40cm 不是设备（pad 正则排除尺寸）", dk("Mouse Pad Gamer 90x40cm") != "device", True)
check("反例：儿童平板仍是整机（用户裁定：判据是玩具不是给小孩用）",
      dk("Tablet Infantil Multi Kid Pad 64GB Wi-Fi – Rosa Bivolt"), "device")
for t in ("martphones Celulares básicos Repuestos de celulares Marca Xiaomi Samsung Apple Motorola Honor Inf",
          "Apple Xiaomi Motorola Honor Zte Redmi Oppo Infinix Acme + Ver más Calificación del producto 5"):
    check(f"页面导航截断文案 looks_like_page_chrome: {t[:36]}", extract.looks_like_page_chrome(t), True)
check("反例：真商品不是页面文案", extract.looks_like_page_chrome("Celular Samsung Galaxy A16 128GB Negro"), False)

print("== ★ 捆绑守卫（skumap yaml「keyboard 无 gb」「apple pencil」会误杀的三条高价整机）==")
for t in ("Acme Slate 12X 2025 Green with keyboard inbox+3rd pencil",
          "Galaxy Tab S10 FE Gris + Smart Book Cover + S Pen",
          "APPLE IPAD A16 2025 128GB WIFI - PINK + APPLE PENCIL USB-C ORIGINAL"):
    check(f"looks_like_device_bundle: {t[:40]}", bool(extract.looks_like_device_bundle(t.lower())), True)
    check(f"detect_product_kind 整机: {t[:40]}", dk(t), "device")
for t in ("funda con teclado para ipad 10.9", "zagg teclado con funda pro keys connect para ipad air 11'",
          "xiaomi pad mini funda protectora con correa de mano"):
    check(f"不是捆绑: {t[:40]}", extract.looks_like_device_bundle(t), None)

print("== ★ 角标截断主语窗口 + base 路径成色 ==")
check("4 cuotas sin interés GENÉRICO FUNDA … 剥后判配件",
      dk(extract.strip_ui_chrome("4 cuotas sin interés GENÉRICO FUNDA BOLSO SLIM ELEGANTE PARA TABLET ACME SLATETAB 11")),
      "accessory")


def enrich(title, cat="phone"):
    lst = Listing(title=title)
    ChannelAdapter._enrich_from_title(SimpleNamespace(current_category=cat), lst)
    return lst


check("base 路径：Reacondicionado APPLE iPhone 13 → refurb（行首成色词直接跟商品名时不剥）",
      enrich("Reacondicionado APPLE iPhone 13 128GB").condition, "refurb")
check("base 路径：MacBook … Reacondicionada LikeShop → refurb（阴性）",
      enrich("MacBook Pro 15 (A1990) Reacondicionada LikeShop", "pc").condition, "refurb")
check("base 路径：钢化膜在平板品类判配件且无 sku_code",
      (enrich("GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11", "tablet").product_kind,
       enrich("GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11", "tablet").sku_code),
      ("accessory", None))

print("== ★ 单一实现多处消费（ast，注释不进语法树）==")
_tool = ast.parse((ROOT / "tools/backfill_accessory_kind.py").read_text(encoding="utf-8"))
_attr_calls = {n.func.attr for n in ast.walk(_tool)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
check("回填工具调用 base._enrich_from_title（不自带词表）", "_enrich_from_title" in _attr_calls, True)
_ext = ast.parse((ROOT / "app/scraping/extract.py").read_text(encoding="utf-8"))
_names = {n.id for n in ast.walk(_ext) if isinstance(n, ast.Name)}
check("extract 里主语守卫从 _DEVICE_WORDS 派生（_lead_has_device 用 _DEVICE_WORD_RE）",
      {"_DEVICE_WORD_RE", "_lead_has_device"} <= _names, True)
_pa = (ROOT / "app/agents/price_audit.py").read_text(encoding="utf-8")
check("price_audit 仍消费 extract._ACCESSORY_WORDS（改词表会直接影响地板样本）",
      "extract._ACCESSORY_WORDS" in _pa, True)

# -------------------------------------------------------------------------------------
# ★ 已知缺口（改动范围外：skumap.py / base.py），只打印不计失败 —— 修好后把它们搬进上面。
# -------------------------------------------------------------------------------------
print("== 已知缺口（KNOWN-GAP，不计入 fail；需改 skumap.py / base.py）==")
_gaps = [
    ("A4 skumap.is_accessory 捆绑守卫：Slate 12X with keyboard 应为 sku",
     sk("Acme Slate 12X 2025 Green with keyboard inbox+3rd pencil"), "sku"),
    ("C1 base 控制流：SKU 命中不再直接定 device（Xiaomi Pad Mini funda protectora → accessory/unknown）",
     enrich("Xiaomi Pad Mini funda protectora con correa de mano", "tablet").product_kind in ("accessory", "unknown"), True),
    ("C4 base.py L297 喂原文：叠层角标里的 Reacondicionado 成色不丢",
     enrich("Reacondicionado 4 cuotas sin interés VIVO Y29").condition, "refurb"),
    # ★ 佩戴类三态的**已量化代价**（2026-09-05 对全库 141,613 条 device 观测做只读体检）：
    #   10 种标题 / 38 条观测从 device 变 unknown，全部是真手表 —— 三态的安全方向，
    #   但它们会静默退出价格分析（知识页：误杀整机只出现在高价端）。列在这里是为了有人盯着。
    #   ★ 试过再加一道「设备词与佩戴词间隔 ≤1 词」：能救回下面这两条（gap=3），
    #     代价是「Apple Watch Hermès – Correa En Mer … para caja de 49 mm」（9,999 MXN×5）
    #     从 unknown 退回 device —— 把 550 美元的表带挂到 Apple Watch 的价格线上，
    #     那是更坏的一类错（配件混进整机是挂在具名机型上的），所以没收紧。
    ("W1 真手表被佩戴规则推进待定：Garmin Vivoactive 6（CL 399,990 CLP，1 条观测）",
     dk("Smartwatch Vivoactive 6 Pizarra Correa Negra"), "device"),
    ("W2 真手表被佩戴规则推进待定：Acme Banda 11（CO 219,900 COP，3 条观测）",
     dk("Smart band | Banda 11 ALUMINIO"), "device"),
    ("W3 真手表被佩戴规则推进待定：BLOOSOM Smartwatch（MX 1,039 MXN，27 条观测）",
     dk("Smartwatch, Correa Plateada, Multifunción, Resistente al Agua plata Belug BLOOSOM"), "device"),
]
_base_src = ast.parse((ROOT / "app/scraping/channels/base.py").read_text(encoding="utf-8"))
_sku_sets_device = False
for node in ast.walk(_base_src):
    if isinstance(node, ast.FunctionDef) and node.name == "_enrich_from_title":
        for n in ast.walk(node):
            if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "product_kind" for t in n.targets)
                    and isinstance(n.value, ast.Constant) and n.value.value == "device"):
                _sku_sets_device = True
_gaps.append(("C1 ast：base._enrich_from_title 里 skumap 命中 sku 不再直接赋 product_kind='device'",
              _sku_sets_device, False))
for name, got, want in _gaps:
    print(f"  [{'ok ' if got == want else 'GAP'}] {name}: got={got!r} want={want!r}")

print(f"\n{PASS} pass / {FAIL} fail")
sys.exit(1 if FAIL else 0)
