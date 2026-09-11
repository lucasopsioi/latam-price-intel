# -*- coding: utf-8 -*-
"""SKU 名归一化测试（复用 nubimetrics-platform 的清洗规则）。

★ 这个文件盯两件事：
  1. **渠道级预清洗**（本项目的责任）—— nubimetrics 的规则是照 MercadoLibre
     的标题写的，不认识 Falabella 的 "Envío gratis app" 前缀、"Por <卖家>" 尾巴、
     Sears 的运营商套餐码 "R9 (Telcel)"。不预清洗直接喂过去会得到
     "HONOR App Honor Honor 70"，卖家叫 Cablex 的手机还会被判成配件。
  2. **边界约定的转换** —— nubimetrics 把品牌拼进 SKU（PowerBI 单列口径），
     本项目 brand 是独立列且界面四处「品牌 + 型号」并排显示，
     不转换会显示成 "Samsung Samsung Galaxy A57"。

拿不到 nubimetrics 时**跳过**而不是失败：那是可选依赖，
但要显式报告跳过了，不能静默假装通过。

跑法： python tests\test_skunorm.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import skunorm  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def check_true(name, cond, hint=""):
    check(f"{name}{(' — ' + hint) if hint else ''}", bool(cond), True)


print("== 渠道级预清洗（不依赖 nubimetrics，纯本地逻辑）==")
pc = skunorm.pre_clean
# 卖家尾巴：不切会把一台机器按卖家裂成十几个产品
check("切掉 Por 卖家尾巴",
      pc("APPLE IPhone 14 128GB Reacondicionado Por Kiss Elec"),
      "APPLE IPhone 14 128GB Reacondicionado")
check("切掉 Por 卖家+套餐尾巴",
      pc("APPLE IPhone 17 Por FALABELLA Equipo + Plan"), "APPLE IPhone 17")
# 配送/促销前缀，可能叠好几层
check("剥 Envío gratis app 前缀",
      pc("Envío gratis app SAMSUNG Galaxy A57 256GB"), "SAMSUNG Galaxy A57 256GB")
check("剥 Frete grátis 前缀",
      pc("Frete grátis Smartphone Galaxy A17"), "Smartphone Galaxy A17")
# 运营商套餐码（Sears 墨西哥）—— MercadoLibre 上没有这种写法
check("剥运营商套餐码",
      pc("Celular Samsung A56 256Gb Color Rosa R9 (Telcel) SEARS"),
      "Celular Samsung A56 256Gb Color Rosa")
# 内存规格里的加号会被当成捆绑标记
check("剥内存规格 4+256GB", pc("MOTOROLA G67 4+256GB"), "MOTOROLA G67")
# 反向：正常标题的**型号部分**不能被削掉（颜色词该剥就剥）
check("正常标题只剥颜色", pc("Samsung Galaxy Z Flip8 Crema 512GB"),
      "Samsung Galaxy Z Flip8 512GB")
check_true("型号主体完整保留",
           "Galaxy Z Flip8" in pc("Samsung Galaxy Z Flip8 Crema 512GB"))
# 卖家写在前面时，整段切光会把型号也吃掉 —— 要有保险
check_true("卖家写在前面不会被切光",
           len(pc("Vendido por FALABELLA APPLE IPhone 15 128GB")) > 0)

if not skunorm.available():
    print("\n⚠ 未找到 nubimetrics-platform，跳过规则相关用例。")
    print("  设 NUBIMETRICS_PATH 指向该项目根目录可启用。")
    print(f"\n结果: {PASS} 通过, {FAIL} 失败（部分用例已跳过）")
    sys.exit(1 if FAIL else 0)

print("== 型号抽取（走 nubimetrics 规则）==")


def sku(brand, title):
    return skunorm.classify(brand, title)["sku"]


check("Falabella 前缀+尾巴全剥",
      sku("Samsung", "Envío gratis app SAMSUNG Galaxy A57 256GB 8GB RAM Por FALABELLA"),
      "Galaxy A57")
check("翻新机尾巴", sku("Apple", "APPLE IPhone 14 Plus 128GB - Reacondicionado Por TEKTRADE"),
      "iPhone 14 Plus")
# ★ 型号码里的连字符要接回来：官方写 WH-CH720N，丢了对不上包装盒
check("连字符型号码", sku("Sony", "SONY Audífonos Bluetooth WH-CH720N Noise Cancelling"),
      "WH-CH720N")
# ★ 产品线前缀按厂商命名规律补回（Sears 标题省掉 Galaxy）
check("补回 Galaxy 线", sku("Samsung", "Celular Samsung S26 256Gb 5G Violeta R9 (Telcel) SEARS"),
      "Galaxy S26")
check("补回 Moto 线", sku("Motorola", "Envío gratis app MOTOROLA G67 Lima 4+256GB Por FALABELLA"),
      "Moto G67")
# ★ 卖家名叫 Cablex，"cable" 子串一命中整台手机会被判成配件
check("卖家名 Cablex 不该触发配件判定",
      sku("Xiaomi", "Celular Xiaomi Redmi 14C 256GB 4GB Negro Nuevo Por Cablex"),
      "Redmi 14C")

print("== 配件与设备要分开 ==")
check("触控笔是配件",
      skunorm.classify("Apple", "Lápiz óptico Stylus Pluma Para iPad, Carga Rápida")["kind"],
      "配件")
check("保护壳是配件",
      skunorm.classify("Samsung", "Funda Para Samsung Galaxy Tab A9 8.7 Incluye Mica")["kind"],
      "配件")
check("手机是设备",
      skunorm.classify("Samsung", "Samsung Galaxy S26 Ultra 512GB")["kind"], "设备")

print("== ★ 品牌前缀的边界转换 ==")
# nubimetrics 输出带品牌（PowerBI 单列口径），本项目 brand 是独立列
r = skunorm.classify("Samsung", "SAMSUNG Galaxy A57 256GB")
check("存库用不带品牌", r["sku"], "Galaxy A57")
check("完整写法仍保留", r["sku_full"], "Samsung Galaxy A57")
# 品牌显示名与 brand 表写法不同的（vivo / soundcore 全小写）也要能剥
r2 = skunorm.classify("Soundcore", "Envío gratis app SOUNDCORE BY ANKER Audífonos Liberty 4 NC")
check_true("soundcore 前缀也被剥掉",
           not r2["sku"].lower().startswith("soundcore"), f"得到 {r2['sku']!r}")

print("== ★ 查证过的名字要能与猜的区分开 ==")
# 这是引入 nubimetrics 最主要的收益：它带一张联网查证过的官方名表。
# 猜的名字拿去和用户既有报表对账会对不上，而且没有任何提示 —— 必须能分辨。
r3 = skunorm.classify("Sony", "SONY Audífonos Bluetooth WH-CH720N")
check_true("官方名表命中标记为已查证", r3["verified"] is True)
r4 = skunorm.classify("Garmin", "GARMIN Smartwatch Forerunner 970 Negro")
check_true("表里没有的如实标为未查证", r4["verified"] is False)
check_true("未查证的仍给出可用名字", len(r4["sku"]) > 2)

print("== 降级路径：拿不到上游时不许假装成功 ==")
res = skunorm.classify("Samsung", "")
check_true("空标题不炸", isinstance(res, dict))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
