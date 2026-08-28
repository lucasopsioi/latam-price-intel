# -*- coding: utf-8 -*-
"""型号归一化回归测试。

★ 为什么这个测试特别重要：
  归一化决定了"哪些挂牌算同一个产品"。漏掉一个颜色词，同一台 Galaxy Z Flip8
  就会按 Crema / Rosa / Negro 裂成三个"产品"，价格中位数、竞品对比、
  上市时序全部算不出来 —— 而且不报错，看起来像"友商机型特别多"。

  下面的用例大部分是 2026-08-10 从墨西哥 Liverpool / Amazon 真实抓回来的标题。

跑法： python tests\test_normalize.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.cleaner import CleanerAgent  # noqa: E402

norm = CleanerAgent.normalize_model
PASS, FAIL = 0, 0

SAMSUNG = (["Samsung", "Galaxy", "Galaxy S", "Galaxy A", "Galaxy Z Fold",
            "Galaxy Z Flip", "Galaxy Watch", "Galaxy Buds", "Galaxy Tab"], "Samsung")
MOTO = (["Motorola", "Moto G", "Moto E", "Moto Edge", "Motorola Razr", "moto"], "Motorola")
APPLE = (["Apple", "iPhone", "iPhone Pro", "iPhone Pro Max"], "Apple")
XIAOMI = (["Xiaomi", "Redmi", "POCO", "Mi", "Xiaomi Pad"], "Xiaomi")
HONOR = (["Honor", "HONOR", "Honor Magic", "Honor X"], "Honor")


def check(title, brand_tuple, want):
    global PASS, FAIL
    aliases, bname = brand_tuple
    got = norm(title, aliases, bname)
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {title[:58]}")
        print(f"         got={got!r}  want={want!r}")


print("== 真实抓取标题（Liverpool，2026-08-10）==")
check("SAMSUNG Galaxy S26 Ultra Dynamic AMOLED 2X 6.9 pulgadas", SAMSUNG, "Galaxy S26 Ultra")
check("SAMSUNG Galaxy A57 Super AMOLED plus 6.7 pulgadas", SAMSUNG, "Galaxy A57")
check("SAMSUNG Galaxy A56 Super AMOLED 6.7 pulgadas", SAMSUNG, "Galaxy A56")
check("MOTOROLA Moto Razr 60 POLED 6.9 pulgadas", MOTO, "Moto Razr 60")
check("MOTOROLA Moto G15 LCD 6.7 pulgadas", MOTO, "Moto G15")

print("== 真实抓取标题（Amazon MX，2026-08-10）==")
# ★ 核心用例：颜色不同必须归一到同一型号，否则价格对比会散架
check("Samsung Galaxy Z Flip8 Crema 512GB", SAMSUNG, "Galaxy Z Flip8")
check("Samsung Galaxy Z Flip8 Rosa 512GB", SAMSUNG, "Galaxy Z Flip8")
check("Samsung Galaxy Z Fold8 Crema 512GB", SAMSUNG, "Galaxy Z Fold8")
check("Samsung Galaxy Z Fold8 Ultra Grafito 512GB", SAMSUNG, "Galaxy Z Fold8 Ultra")
check("Motorola G06 Cafe (4GB RAM/256GB), Celular Desbloqueado", MOTO, "Moto G06")

print("== 同机型不同颜色/容量必须收敛到同一型号 ==")
base = norm("Samsung Galaxy Z Flip8 Crema 512GB", *SAMSUNG[::-1][::-1]) \
    if False else norm("Samsung Galaxy Z Flip8 Crema 512GB", SAMSUNG[0], SAMSUNG[1])
for variant in [
    "Samsung Galaxy Z Flip8 Rosa 256GB",
    "SAMSUNG Galaxy Z Flip8 Negro 512GB Desbloqueado",
    "Samsung Galaxy Z Flip8 Azul 512GB Dual SIM 5G",
    "Galaxy Z Flip8 Blanco",
]:
    check(variant, SAMSUNG, base)

print("== 品牌前缀不能被剥掉（剥了会跨品牌撞车）==")
check("Apple iPhone 16 Pro Max 256GB Titanio Natural", APPLE, "iPhone 16 Pro Max")
check("Xiaomi Redmi Note 14 Pro 256GB Negro", XIAOMI, "Redmi Note 14 Pro")
check("HONOR Magic7 Lite 256GB Verde", HONOR, "Magic7 Lite")

print("== 营销噪声 ==")
check("Samsung Galaxy A16 5G 128GB Azul Desbloqueado Envío Gratis", SAMSUNG, "Galaxy A16")
check("SAMSUNG Galaxy S25 FE 256GB Nuevo Original Garantía", SAMSUNG, "Galaxy S25 FE")
# Edge 是 Motorola 的独立产品线，官方命名就是 "Motorola Edge"，不叫 "Moto Edge"。
# 只有 G / E 系列才需要补 Moto 前缀。
check("Motorola Edge 60 Fusion 256GB Verde Lacrado Nacional Anatel", MOTO, "Edge 60 Fusion")

print("== 屏幕技术短语整体剥离 ==")
check("SAMSUNG Galaxy S25 Dynamic AMOLED 2X pantalla 6.2 pulgadas", SAMSUNG, "Galaxy S25")
check("MOTOROLA Moto G85 POLED 6.67 polegadas", MOTO, "Moto G85")
# ★ 整数尺寸保留、小数尺寸剥掉，是有意为之：
#   平板/PC 的尺寸就是型号的一部分（iPad Pro 11 ≠ iPad Pro 13），
#   而手机标题里的 "6.9 pulgadas" 只是屏幕描述。
#   小数点是天然的分界线 —— 没有 "iPhone 6.9"，也没有 "11.0 寸屏"的写法。
check("Apple iPad Pro Liquid Retina 11 pulgadas", APPLE, "iPad Pro 11")
check("Apple iPad Pro Liquid Retina 13 pulgadas 256GB", APPLE, "iPad Pro 13")

print("== 括号与加号尾巴 ==")
check("Samsung Galaxy Tab S10 (Wi-Fi, 128GB) Gris", SAMSUNG, "Galaxy Tab S10")
check("Motorola Moto G35 + Funda + Mica", MOTO, "Moto G35")

print("== ★ 产品分层标记（+ / Ultra / Pro）必须保留 ==")
# 第一版 `[+/|,].*$` 把「S26+」的 + 连同后面全剥了 ⇒ S26 与 S26+ 合并，
# 两个价位段混算，实测出现 88% 的假价差。
# 但「Tab S11 + Keyboard Cover」的 + 是分隔符，仍要剥。差别是两边有没有空格。
check("SAMSUNG Galaxy S26 Dynamic AMOLED 2X 6.2 pulgadas", SAMSUNG, "Galaxy S26")
check("SAMSUNG Galaxy S26+ Dynamic AMOLED 2X 6.7 pulgadas", SAMSUNG, "Galaxy S26+")
check("Celular Samsung Galaxy S26+ Liberado 512 GB Violeta", SAMSUNG, "Galaxy S26+")
check("SAMSUNG Galaxy S26 Ultra Dynamic AMOLED 2X", SAMSUNG, "Galaxy S26 Ultra")
check("Galaxy Tab S11 + Keyboard Cover + S Pen", SAMSUNG, "Galaxy Tab S11")

_tiers = {norm("Galaxy S26 Dynamic", *SAMSUNG[::-1][::-1]) if False else
          norm("Galaxy S26 Dynamic", SAMSUNG[0], SAMSUNG[1]),
          norm("Galaxy S26+ Dynamic", SAMSUNG[0], SAMSUNG[1]),
          norm("Galaxy S26 Ultra Dynamic", SAMSUNG[0], SAMSUNG[1])}
if len(_tiers) == 3:
    PASS += 1
else:
    FAIL += 1
    print(f"  [FAIL] ★三个分层应是三个不同型号: {sorted(_tiers)}")

print("== ★ 跨渠道对齐：省略的产品线前缀要补回来 ==")
# 同一台 Galaxy A07：
#   Liverpool "SAMSUNG Galaxy A07 …"       带 Galaxy
#   Sears     "Celular Samsung A07 64Gb …" 不带
# 不补前缀就是两个产品，价格没法比、上市时序也算不出来
check("SAMSUNG Galaxy A07 Super AMOLED 6.7 pulgadas", SAMSUNG, "Galaxy A07")
check("Celular Samsung A07 64Gb 4G Color Negro R9 (Telcel) SEARS", SAMSUNG, "Galaxy A07")
check("Motorola G06 Cafe (4GB RAM/256GB)", MOTO, "Moto G06")
check("MOTOROLA Moto G06 LCD 6.7 pulgadas", MOTO, "Moto G06")
check("Motorola Edge 70 Fusion POLED", MOTO, "Edge 70 Fusion")
check("Xiaomi Redmi Note 15 256 GB", XIAOMI, "Redmi Note 15")
check("Samsung Galaxy Tab S11 Ultra", SAMSUNG, "Galaxy Tab S11 Ultra")

print("== ★ 渠道/运营商噪声要剥干净 ==")
# Sears 标题："Celular Samsung A07 64Gb 4G Color Negro R9 (Telcel) SEARS"
# 不剥就归一化成 "A07 Color R9 SEARS"
check("Celular Samsung A56 256Gb Color Rosa R9 (Telcel) SEARS", SAMSUNG, "Galaxy A56")
check("Celular Samsung A36 128Gb Color Negro R9 (Telcel) SEARS", SAMSUNG, "Galaxy A36")
check("Celular Samsung S26 256Gb 5G Violeta R9 (Telcel) SEARS", SAMSUNG, "Galaxy S26")

print("== ★ 卖家归属尾巴（Falabella 智利真实标题）==")
# 实测：2227 个友商产品里 579 个的型号名带着卖家名。同一台 iPhone 14
# 按卖家裂成 iPhone 14 Por Kiss / Por FALABELLA / Por REUSE …
# 裂开之后同 SKU 比价永远匹配不上 —— 价格变动检测对这些机型完全失效，
# 而看板上的"覆盖机型数"是虚高的。这组用例守住"按卖家不许裂开"。
check("APPLE IPhone 14 Plus 128GB - Reacondicionado Por TEKTRADE", APPLE, "iPhone 14 Plus")
check("APPLE IPhone 14 128GB Azul Reacondicionado Por Kiss Elec", APPLE, "iPhone 14")
check("Envío gratis app APPLE IPhone 14 128GB Por FALABELLA Equipo + Plan",
      APPLE, "iPhone 14")
check("Envío gratis app APPLE IPhone 15 256 GB 12GB RAM eSIM Apple "
      "Por FALABELLA Equipo + Plan", APPLE, "iPhone 15")
check("MOTOROLA Razr 60 512GB Por TECHSPOT", MOTO, "Razr 60")
# 卖家写在前面：整段切到行尾会把型号本身也吃掉，必须靠动词形态兜住
check("Vendido por FALABELLA APPLE IPhone 15 128GB", APPLE, "iPhone 15")
# 反向：不带卖家尾巴的正常标题不能被误伤
check("APPLE IPhone 15 Pro Max 256GB Titanio", APPLE, "iPhone 15 Pro Max")

print("== ★ 绝不能归一化出空型号 / 纯数字型号 ==")
# Garmin 的别名里混着产品线名 [Garmin, Forerunner, Fenix, Venu, Instinct]，
# 当成品牌名一起剥 → Instinct E 和 Fenix E 双双变成空字符串。
# 空型号会让所有剥空的商品挤进同一个"产品"：两块价差 3 倍的表被并成一款，
# 于是 249,990 ↔ 799,990 被记成"同一款在降价"。
GARMIN = (["Garmin", "Forerunner", "Fenix", "Venu", "Instinct", "Vivoactive"], "Garmin")
check("Envío gratis app GARMIN Smartwatch Instinct E Por FALABELLA", GARMIN, "Instinct")
check("Envío gratis app GARMIN Smartwatch Fenix E Por FALABELLA", GARMIN, "Fenix")
# ★ 丢了产品线名才是错的：Forerunner 被当品牌别名剥掉后只剩 "970"，
#   必须回退到"只剥品牌主名"保住 Forerunner。
check("GARMIN Smartwatch Forerunner 970 Negro", GARMIN, "Forerunner 970")

# ★★ 但「纯数字」本身不是错误 —— 这两件事必须分开。
#   Honor 70 / Xiaomi 17 / Bose 700 的型号名本来就是纯数字，品牌另存一列。
#   一度把两者合成一个守卫（要求结果必须含字母），于是这些合法型号被判"不可用"、
#   一路回退到**原始噪声文本**，产出「Envío Gratis App Teléfono Móvil…」当型号名 ——
#   实测 2993 个产品里 264 个（8.8%）是这么来的，看着像"没剥干净"，
#   其实是"剥干净了但被否决"。
HONOR = (["Honor"], "Honor")
check("Envío gratis app HONOR Teléfono Móvil Honor 70 256GB", HONOR, "70")
check("HONOR Celular 600 Pro 12+512GB Por FALABELLA", HONOR, "600 Pro")
BOSE = (["Bose"], "Bose")
check("Envío gratis app BOSE Audífonos Bluetooth 700 Por NewCycle", BOSE, "700")

# ★ 界面文案泄漏：Falabella 把「Vista Previa」（预览按钮）抓进了标题，
#   实测 208 个产品的型号名以它开头 —— 所有泄漏词里最大的一个。
SAMSUNG_B = (["Samsung", "Galaxy"], "Samsung")
check("Vista Previa Envío rápido SAMSUNG Galaxy Book 6 Pro (14'')",
      SAMSUNG_B, "Galaxy Book 6 Pro")
JBL = (["JBL"], "JBL")
check("Vista Previa JBL 3.2 AUDÍFONOS JBL TUNE FLEX 2 BLUETOOTH", JBL, "TUNE FLEX 2")
# 标题里除了规格什么都没有时，规格就是唯一区分度，不能剥
XIAOMI_W = (["Xiaomi", "Redmi", "Mi", "Xiaomi Watch"], "Xiaomi")
check("XIAOMI Smartwatch 1.48 Pulgadas Por FALABELLA", XIAOMI_W, "Smartwatch 1.48 Pulgadas")
check("XIAOMI Smartwatch 2.07 Pulgadas Por FALABELLA", XIAOMI_W, "Smartwatch 2.07 Pulgadas")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
