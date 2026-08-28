# -*- coding: utf-8 -*-
"""标题界面/促销角标剥离的回归测试（extract.strip_ui_chrome）。

跑法：  python tests\test_title_chrome.py

★ 这个文件一半的用例是**反向**的 —— 断言某些标题**不许被改**。
  正向漏剥只是名字难看；反向误剥会把真型号剥掉，让一堆不同商品
  归一化成同一个产品，然后报出「同一款降价 69%」这种根本不存在的情报
  （knowledge/lessons/scrape-normalize-silent-corruption.md 第 2 条）。
  所以放宽词表/正则时，先看反向组还全不全绿。

用例里的样例全部**取自 price_obs 真实行**（2026-08-27 抽样），不是编的。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scraping.extract import strip_ui_chrome  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}:\n         got={got!r}\n        want={want!r}")


def unchanged(name, title):
    """反向用例：这条标题必须原样返回"""
    check(name, strip_ui_chrome(title), title)


print("== 前缀：Paris.cl / Ripley「Vista Previa」快速预览按钮 ==")
check("vista previa 基础",
      strip_ui_chrome("Vista Previa Samsung Smartphone Galaxy A57 5G 128GB"),
      "Samsung Smartphone Galaxy A57 5G 128GB")
check("大小写不敏感",
      strip_ui_chrome("VISTA PREVIA Apple iPad Air 11"),
      "Apple iPad Air 11")

print("== 前缀：Falabella「Envío gratis (app)」 ==")
check("envío gratis app",
      strip_ui_chrome("Envío gratis app SAMSUNG Galaxy S25 Ultra 512GB"),
      "SAMSUNG Galaxy S25 Ultra 512GB")
check("envío gratis 不带 app",
      strip_ui_chrome("Envío gratis APPLE Celular Iphone 15 128GB"),
      "APPLE Celular Iphone 15 128GB")
check("无重音变体 envio gratis",
      strip_ui_chrome("Envio gratis app HONOR Audífonos"),
      "HONOR Audífonos")
# ★ 定点防回归：re.I 下的 "app" 会命中 "APPLE" 的前三个字母。
#   skunorm._PREFIX 就是这么坏的（少一个 \b），把「Envío gratis APPLE Celular」
#   归一化成「LE Celular」，实测 249 条。这条断言钉住本函数不踩同一个坑。
check("app 不得吃掉 APPLE 的前三个字母",
      strip_ui_chrome("Envío gratis APPLE Celular Iphone 11 64 Gb Rojo"),
      "APPLE Celular Iphone 11 64 Gb Rojo")
check("app 后接真字母也不吃",
      strip_ui_chrome("Envío gratis Appliance Soporte"),
      "Appliance Soporte")

print("== 前缀：角标会叠加，必须循环剥 ==")
check("三层叠加",
      strip_ui_chrome("Vista Previa HOT PRICE Envío gratis app APPLE iPhone 17 Pro 256GB"),
      "APPLE iPhone 17 Pro 256GB")
check("vista previa + recíbelo hoy",
      strip_ui_chrome("Vista Previa Recíbelo hoy SAMSUNG Galaxy Tab S10"),
      "SAMSUNG Galaxy Tab S10")
check("vista previa + envío rápido",
      strip_ui_chrome("Vista Previa Envío rápido XIAOMI Redmi Note 14"),
      "XIAOMI Redmi Note 14")
check("vista previa + vendedor destacado",
      strip_ui_chrome("Vista Previa Vendedor destacado GENÉRICO Funda"),
      "GENÉRICO Funda")
check("vista previa + tarjeta fest",
      strip_ui_chrome("Vista Previa TARJETA FEST Envío gratis app LENOVO Tablet"),
      "LENOVO Tablet")
check("Liverpool 广告位角标",
      strip_ui_chrome("Patrocinado SONY Audífonos In-Ear LinkBuds Clip inalámbricos"),
      "SONY Audífonos In-Ear LinkBuds Clip inalámbricos")

print("== 后缀：评分 + 评价数 ==")
check("0 (0)",
      strip_ui_chrome('Vista Previa Tecnopalace Funda Teclado 2.0 Para Xiaomi Pad 8 - 11" Pulg 0 (0)'),
      'Tecnopalace Funda Teclado 2.0 Para Xiaomi Pad 8 - 11" Pulg')
check("4.7 (394)",
      strip_ui_chrome("Apple iPhone 15 128GB Negro 4.7 (394)"),
      "Apple iPhone 15 128GB Negro")
check("逗号小数评分 4,8 (20)",
      strip_ui_chrome("Samsung Galaxy A57 5G 128GB 4,8 (20)"),
      "Samsung Galaxy A57 5G 128GB")

print("== 后缀：Paris 折扣角标（同一个数字渲染两遍） ==")
check("评分 + 双百分比",
      strip_ui_chrome("Vista Previa Apple iPhone 15 128GB Negro 4.7 (394) 35% 35%"),
      "Apple iPhone 15 128GB Negro")
check("只有双百分比",
      strip_ui_chrome("Apple iPhone 17 Pro Max 256GB Azul Liberado 11% 11%"),
      "Apple iPhone 17 Pro Max 256GB Azul Liberado")

print("== 后缀：Hiraoka 促销尾巴 ==")
check("precio especial（保留店内货号 Código）",
      strip_ui_chrome('APPLE iPhone 16 6.1" 128GB Negro Código 134368 Precio especial'),
      'APPLE iPhone 16 6.1" 128GB Negro Código 134368')

print("== NBSP：库里 879 条标题夹着不换行空格 ==")
check("NBSP 分隔的角标照样剥",
      strip_ui_chrome("Vista Previa Apple iPhone 15 128GB\xa0Negro 4.7 (326) 35% 35%"),
      "Apple iPhone 15 128GB\xa0Negro")

print("== ★ 反向：这些必须一个字都不动 ==")
# —— 尾部 (数字) 是**型号年份**，不是评价数（实测尾部形态命中 21 条）——
unchanged("屏幕尺寸 + 年份 12.2 (2024)",
          "ROCK SPACE LAMINA HIDROGEL PARA TABLET ACME SLATE PRO 12.2 (2024)")
unchanged("型号 + 年份 8 (2017)",
          "GUIONBAJO CHILE BATERÍA PARA IPHONE 8 (2017)")
unchanged("评分位恰好 ≤5 的年份 5 (2017)",
          "Apple iPad 5 (2017)")
unchanged("评分位 >5 的 10,2 (2023)",
          "Tablet 10,2 (2023)")
unchanged("括号前不是数字，不动",
          "APPLE Watch Ultra 2 (2023) Titanium 49mm - Reacondicionado Por Wireless Source (6)")
# —— 单个尾部 % 是真商品信息（实测全库 4 条，全在 Falabella Chile）——
unchanged("单个尾部百分比",
          "Malla Sombra de Raschel Reforzada para Exteriores 3x3 Metros -Sombreado 90% y Bloqueo UV 95%")
# —— 刻意没收的裸词 ——
unchanged("裸 hot 是真商品名", "Hot Blossom Difusor de Aroma Electrico")
unchanged("裸 tarjeta 是真商品名", "Tarjeta Samsung Micro SD 128GB Clase 10")
unchanged("裸 app 不成词条", "App Store Gift Card 50 USD")
unchanged("Nuevo 有歧义，不动", "APPLE Nuevo iPad Pro 11 M5 256Gb Wi-Fi")
unchanged("角标词出现在中间不剥", "Cargador para Vista Previa Digital")

print("== ★ 剥空自纠：整条都是角标时退回原文，绝不返回空串 ==")
check("整条就是角标", strip_ui_chrome("Vista Previa"), "Vista Previa")
check("剥完只剩数字", strip_ui_chrome("Vista Previa 0 (0)"), "Vista Previa 0 (0)")
check("剥完只剩符号", strip_ui_chrome("Envío gratis app - 0 (0) 35% 35%"),
      "Envío gratis app - 0 (0) 35% 35%")
check("空串", strip_ui_chrome(""), "")
check("None", strip_ui_chrome(None), "")
check("只有空白", strip_ui_chrome("   "), "")

print("== 幂等：剥两遍必须等于剥一遍 ==")
_IDEMPOTENT_SAMPLES = [
    "Vista Previa HOT PRICE Envío gratis app APPLE iPhone 17 Pro 256GB 0 (0) 11% 11%",
    "Envío gratis app SAMSUNG Galaxy S25 Ultra 512GB",
    "Vista Previa",
    "Hot Blossom Difusor de Aroma Electrico",
    'APPLE iPhone 16 6.1" 128GB Negro Código 134368 Precio especial',
]
for _s in _IDEMPOTENT_SAMPLES:
    _once = strip_ui_chrome(_s)
    check(f"幂等 {_s[:38]!r}…", strip_ui_chrome(_once), _once)

print("== ★ 接线检查：采集端真的调了这个函数吗 ==")
# 知识页 11b：「同一成因第二次出现 = 修复位置选错了」，而更早的一课是
# 「注释写了不等于做了」—— 光测函数正确没用，坏的往往是没人调它。
import inspect  # noqa: E402
from app.scraping.channels.base import ChannelAdapter  # noqa: E402

_src = inspect.getsource(ChannelAdapter._enrich_from_title)
check("_enrich_from_title 调用了 strip_ui_chrome",
      "strip_ui_chrome" in _src, True)
check("剥离排在 fix_mojibake 之后",
      _src.index("fix_mojibake") < _src.index("strip_ui_chrome"), True)
check("剥离排在型号/配件判定之前",
      _src.index("strip_ui_chrome") < _src.index("detect_product_kind"), True)

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
