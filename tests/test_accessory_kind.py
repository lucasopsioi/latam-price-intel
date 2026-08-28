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
      dk("Acme Slate 11.5 con lápiz M-Pencil de regalo"), "device")

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

print(f"\n{PASS} pass / {FAIL} fail")
sys.exit(1 if FAIL else 0)
