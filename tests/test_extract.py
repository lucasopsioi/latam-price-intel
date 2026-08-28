# -*- coding: utf-8 -*-
"""价格解析与规格识别的回归测试。

跑法：  python tests\test_extract.py
这些断言是"改词表/改正则时的回归网"——拉美六国数字格式互不兼容，
改错一个分支会让某个国家的价格整体差 100 倍，而且不报错、静默算错。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scraping.extract import (  # noqa: E402
    parse_price, price_is_sane, parse_ram_rom, parse_screen_size,
    detect_condition, detect_bundle, detect_seller_type, detect_in_stock,
    parse_installments, extract_jsonld_products,
)

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


print("== 价格解析：六国格式 ==")
# 墨西哥：逗号千分位、点小数
check("MXN 21,999.00", parse_price("$21,999.00", "MXN"), 21999.0)
check("MXN 1,234.56", parse_price("$ 1,234.56 MN", "MXN"), 1234.56)
check("MXN 999", parse_price("$999", "MXN"), 999.0)
# 巴西：点千分位、逗号小数
check("BRL 1.234,56", parse_price("R$ 1.234,56", "BRL"), 1234.56)
check("BRL 4.999,00", parse_price("R$4.999,00", "BRL"), 4999.0)
check("BRL 899,90", parse_price("R$ 899,90", "BRL"), 899.90)
# 智利：无小数，点全是千分位
check("CLP 1.234.567", parse_price("$1.234.567", "CLP"), 1234567.0)
check("CLP 899.990", parse_price("$ 899.990", "CLP"), 899990.0)
# 哥伦比亚：同智利
check("COP 3.499.900", parse_price("$3.499.900", "COP"), 3499900.0)
# 秘鲁：逗号千分位、点小数
check("PEN 3,499.00", parse_price("S/ 3,499.00", "PEN"), 3499.0)
check("PEN 1,299.90", parse_price("S/1,299.90", "PEN"), 1299.90)
# 阿根廷：点千分位、逗号小数
check("ARS 1.299.999,00", parse_price("$1.299.999,00", "ARS"), 1299999.0)
check("ARS 895.999", parse_price("$ 895.999", "ARS"), 895999.0)

print("== 价格解析：★同一串在不同国家必须解出不同值 ==")
# 这一组是本文件存在的理由
check("'1.234' @MXN", parse_price("1.234", "MXN"), 1.234)
check("'1.234' @CLP", parse_price("1.234", "CLP"), 1234.0)
check("'1.234' @BRL", parse_price("1.234", "BRL"), 1234.0)

print("== 价格解析：脏输入 ==")
check("空", parse_price("", "MXN"), None)
check("None", parse_price(None, "MXN"), None)
check("无数字", parse_price("Consultar precio", "MXN"), None)
check("数字型", parse_price(21999, "MXN"), 21999.0)
check("带空格符号", parse_price("MXN\xa021,999.00", "MXN"), 21999.0)

print("== 价格合理性 ==")
check("MXN 正常", price_is_sane(21999, "MXN"), True)
check("MXN 太低(分期金额)", price_is_sane(12, "MXN"), False)
check("CLP 正常", price_is_sane(899990, "CLP"), True)
check("ARS 多打一位", price_is_sane(3_512_000_00, "ARS"), False)

print("== RAM/ROM 解析 ==")
check("8+256", parse_ram_rom("Galaxy S24 8+256GB"), (8, 256))
check("12GB/512GB", parse_ram_rom("Xiaomi 14 12GB/512GB"), (12, 512))
check("256GB 8GB RAM", parse_ram_rom("iPhone 15 256GB 8GB RAM"), (8, 256))
check("仅容量 1TB", parse_ram_rom("iPad Pro 1TB"), (None, 1024))
check("仅容量 128GB", parse_ram_rom("Moto G84 128GB"), (None, 128))
check("倒置写法 256GB+8GB", parse_ram_rom("Redmi Note 13 256GB+8GB RAM"), (8, 256))
check("RAM超32判无效", parse_ram_rom("Disco 512GB+256GB"), (None, 512))
check("无容量", parse_ram_rom("AirPods Pro"), (None, None))

print("== 屏幕尺寸 ==")
check("6.7 pulgadas", parse_screen_size('Galaxy S24 Ultra 6.7 pulgadas'), 6.7)
check('11"', parse_screen_size('iPad Pro 11" 256GB'), 11.0)
check("polegadas", parse_screen_size("Tablet 10,5 polegadas"), 10.5)
check("不合理值", parse_screen_size("Cargador 100 pulgadas"), None)

print("== 成色 / 捆绑 ==")
check("翻新 es", detect_condition("iPhone 13 Reacondicionado"), "refurb")
check("翻新 pt", detect_condition("iPhone 13 Recondicionado"), "refurb")
check("全新", detect_condition("iPhone 15 Pro Max nuevo"), "new")
check("捆绑", detect_bundle("Galaxy Tab S9 + Keyboard Cover"), True)
check("非捆绑", detect_bundle("Galaxy Tab S9 256GB"), False)
# ★ 运营商合约机：标的是签约价，不是裸机零售价。混进价格分析会造出
#   "iPhone 17 在 Falabella 只卖零售价三分之一" 这种假价差，并把该型号
#   的价格基线整体拽低。当捆绑处理，从比价里排除。
check("合约机 Equipo+Plan",
      detect_bundle("Envío gratis app APPLE IPhone 17 Por FALABELLA Equipo + Plan"), True)
check("合约机 con Plan", detect_bundle("Samsung Galaxy A56 con Plan Entel"), True)
check("合约机 portabilidad", detect_bundle("Motorola G15 128GB portabilidad Claro"), True)
check("巴西合约机", detect_bundle("Galaxy S26 com plano Vivo"), True)
# 反向：正常裸机不能被误判成合约机
check("裸机不误判", detect_bundle("Samsung Galaxy S26 Ultra 512GB"), False)

print("== ★ 页面框架文案不是商品 ==")
# 实测：Falabella 的**筛选侧栏**被通用卡片启发式当成商品卡入库，
# 价格取到的是**价格筛选滑块的档位值**（100,000 / 300,000 / 500,000 COP），
# 甚至滑块最小值 52 —— 一台"手机" 52 比索进了价格基线，全程不报错。
from app.scraping.extract import looks_like_page_chrome as _chrome  # noqa: E402

check("筛选面板", _chrome("Tipo de Entrega Envío a domicilio Gratis Llega mañana "
                          "Retiro en un punto Retira mañana"), True)
check("页头导航", _chrome("Inicia sesión Mi cuenta 0 Ingresa tu ubicación "
                          "Vende en falabella"), True)
check("被截断的面板文案", _chrome("ío a domicilio Gratis Llega mañana Retiro en un "
                                  "punto Retira mañana Categoría"), True)
# 反向：正常商品标题里偶尔也会带一两个筛选词，不能误杀（所以判据是"同时出现多个"）
check("正常标题不误杀", _chrome("Samsung Galaxy S26 Ultra 512GB Negro"), False)
check("带一个筛选词也不误杀",
      _chrome("Envío gratis app APPLE IPhone 15 128GB Categoría Tecnología"), False)
check("空标题", _chrome(""), False)

print("== ★ 门店自提无货 ≠ 商品无货 ==")
# 真实页面原文（Falabella 智利/秘鲁商品页，2026-08-11 从库里 raw_page 捞出）：
#   「Entrega en Cerrillos  Sin stock en tienda Cerrillos, Metropolitana」
# 说的是**那家线下门店不能自提**，商品在线正常发货。
# 裸的 "sin stock" 子串一命中就整条判缺货 → 价格审计剔除 →
# 实测 523 条全新非捆绑商品（含 Apple Watch Series 11、Galaxy Watch 这种当红在售款）
# 被踢出价格分析，Falabella 自营缺货率被算成 37%。
# 零售商当季主推款不可能三成缺货 —— 这个数本身就该当成警报读。
check("门店自提无货(智利)",
      detect_in_stock("Entrega en Cerrillos Sin stock en tienda Cerrillos, Metropolitana"), True)
check("门店自提无货(秘鲁)",
      detect_in_stock("Entrega en Cercado De Lima Sin stock en tienda Cercado De Lima, Lima"), True)
check("自提不可用", detect_in_stock("Retiro en tienda no disponible"), True)
check("配送不可用", detect_in_stock("Despacho a domicilio no disponible para tu región"), True)
check("巴西门店无货", detect_in_stock("Sem estoque na loja Morumbi"), True)
# 反向：真缺货必须还判得出来，不能为了修误判把这条闸开死
check("真缺货-agotado", detect_in_stock("Producto agotado"), False)
check("真缺货-sin stock", detect_in_stock("Sin stock"), False)
check("真缺货-no disponible", detect_in_stock("Este producto no disponible temporalmente"), False)
check("真缺货-葡语", detect_in_stock("Esgotado"), False)
check("真缺货-英语", detect_in_stock("Out of stock"), False)
check("正常在售", detect_in_stock("Agregar al carro. Envío gratis"), True)
# 结构化字段优先于文案猜测
check("JSON-LD 说有货", detect_in_stock("sin stock en tienda", "https://schema.org/InStock"), True)
check("JSON-LD 说缺货", detect_in_stock("comprar ahora", "https://schema.org/OutOfStock"), False)
check("平板不误判", detect_bundle("Xiaomi Pad 7 Plan de datos incluido 256GB"), False)

print("== ★ 整机 / 配件（管线顺序就是规则）==")
from app.scraping.extract import detect_product_kind  # noqa: E402

# 实抓 Apple 官网时混进来的真实标题
k, _ = detect_product_kind("Correa cruzada – Rosa pálido")
check("表带 → 配件", k, "accessory")
k, _ = detect_product_kind("Funda de silicón con MagSafe para el iPhone 17 Pro")
check("★保护壳含 iPhone 也是配件（para 守卫）", k, "accessory")
k, _ = detect_product_kind("iPhone 17 Pro y iPhone 17 Pro Max")
check("整机", k, "device")
k, _ = detect_product_kind("SAMSUNG Galaxy S26 Ultra Dynamic AMOLED 2X 6.9 pulgadas")
check("整机（长标题）", k, "device")
# ★ 捆绑算整机：整机信号在配件词之前，所以不需要特判
k, _ = detect_product_kind("Galaxy Tab S11 + Keyboard Cover + S Pen")
check("★捆绑装算整机（整机信号优先于配件词）", k, "device")
k, _ = detect_product_kind("Cargador rápido 45W")
check("充电器 → 配件", k, "accessory")
k, _ = detect_product_kind("Mica de cristal templado para Galaxy S26")
check("★贴膜含 Galaxy 也是配件", k, "accessory")
k, _ = detect_product_kind("Audífonos Galaxy Buds3 Pro")
check("耳机是整机不是配件", k, "device")
k, _ = detect_product_kind("")
check("空标题 → unknown", k, "unknown")
k, _ = detect_product_kind("Producto genérico XYZ-123")
check("★判不出就 unknown，不猜", k, "unknown")

print("== 卖家类型（细分逻辑见 tests/test_seller.py）==")
t, _ = detect_seller_type("Samsung Tienda Oficial", "", "unknown", "Samsung")
check("品牌官方店", t, "official")
# ★ 必须传 adapter：判"是不是该渠道自营"要拿卖家名跟**该渠道**的自营主体比。
#   不传 adapter 就没有比对基准，任何卖家名都只能判成第三方。
t, _ = detect_seller_type(None, "Vendido por Falabella", "unknown",
                          adapter="falabella")
check("零售商自营", t, "official")
t, _ = detect_seller_type("ElectroMundo SA", "", "unknown", adapter="falabella")
check("第三方", t, "third_party")
t, _ = detect_seller_type(None, "", "official")
check("回退渠道默认（粗分值也要认）", t, "official")
# 回归：Liverpool 页面到处是 "Liverpool" 字样，不能让第三方商品变官方
t, _ = detect_seller_type(None, "Liverpool Menú Liverpool © Liverpool 2026 "
                                "Vendido por TecnoMundo SA", "unknown",
                          adapter="liverpool")
check("★页面品牌词不污染判定", t, "third_party")

print("== 库存 ==")
check("有货", detect_in_stock("Comprar ahora", "InStock"), True)
check("缺货 schema", detect_in_stock("", "http://schema.org/OutOfStock"), False)
check("缺货 文本es", detect_in_stock("Producto agotado"), False)
check("缺货 文本pt", detect_in_stock("Produto esgotado"), False)

print("== 分期 ==")
check("12x 免息", parse_installments("12 meses sin intereses") is not None, True)
check("无分期", parse_installments("Envio gratis"), None)

print("== JSON-LD ==")
HTML = """<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Galaxy S24 256GB","brand":{"name":"Samsung"},
 "offers":{"@type":"Offer","price":"21999.00","priceCurrency":"MXN",
 "availability":"http://schema.org/InStock","seller":{"name":"Samsung Tienda Oficial"}}}
</script></head><body></body></html>"""
items = extract_jsonld_products(HTML)
check("JSON-LD 条数", len(items), 1)
if items:
    check("JSON-LD 标题", items[0]["title"], "Galaxy S24 256GB")
    check("JSON-LD 价格", parse_price(items[0]["sale_price_raw"], "MXN"), 21999.0)
    check("JSON-LD 品牌", items[0]["brand"], "Samsung")
    check("JSON-LD 卖家", items[0]["seller_name"], "Samsung Tienda Oficial")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
