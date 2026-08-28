# -*- coding: utf-8 -*-
"""权威 SKU 映射表测试 —— 移植自用户的 PowerQuery M 代码。

★ 这份表是用户的既有资产（272 规则 / 131 SKU），是**权威口径**。
  移植必须 1:1，任何语义偏差都会让整个平板品类的归类结果改变。

移植时踩到的坑（这组测试就是为了防它复发）：
  用正则从 M 代码抓 `Text.Contains(T, "...")` 时，**把否定条件也抓成了配件词** ——
  `book cover ... and not Text.Contains(T, "gb")` 里的 "gb" 进了配件词表，
  于是**任何写了容量的平板都被判成配件**，整个品类数据全废，且不报错。

跑法： python tests\test_skumap.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import skumap  # noqa: E402

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


def sku(title):
    return skumap.classify(title)["sku"]


def kind(title):
    return skumap.classify(title)["kind"]


print("== 规则表加载 ==")
check("唯一 SKU 数", len(skumap.all_skus()), 131)
check_true("规则表非空", len(skumap._load()["rules"]) == 272,
           str(len(skumap._load()["rules"])))

print("== ★ 配件词表不得含「正常商品必然出现的词」==")
# 这是移植 bug 的直接检出手段：否定条件被当成肯定条件
DANGER = {"gb", "ram", "memoria", "tablet", "tableta", "slate", "ipad", "galaxy"}
contains = set(skumap._load().get("accessory_contains") or [])
check_true("★配件词表无危险词", not (contains & DANGER), str(contains & DANGER))

print("== 文本归一化与 M 代码一致 ==")
check("小写", skumap.normalize("IPAD AIR"), "ipad air")
check("去重音", skumap.normalize("Tablet Genérica Ñandú"), "tablet generica nandu")
check("逗号转点（西语小数点）", skumap.normalize("12,9 pulgadas"), "12.9 pulgadas")
check("连字符转空格", skumap.normalize("Wi-Fi"), "wi fi")
check("下划线转空格", skumap.normalize("tab_s11"), "tab s11")
check("压缩空白", skumap.normalize("  iPad   Air  "), "ipad air")
check("NBSP 当空格", skumap.normalize("iPad Air"), "ipad air")
check("空值", skumap.normalize(None), "")

print("== SKU 命中（各品牌）==")
check("iPad Air 13 M4", sku("Apple iPad Air 13 pulgadas M4 256GB Wi-Fi"),
      "Apple iPad Air 13 M4")
check("Galaxy Tab S11 Ultra", sku("Tablet Samsung Galaxy Tab S11 Ultra 512GB"),
      "Samsung Galaxy Tab S11 Ultra")
check("Slate Tab PaperMatte", sku("ACME Slate 11.5 PaperMatte 8GB 256GB"),
      "Acme Slate 11.5 PaperMatte")
check("Lenovo Tab M11", sku("Lenovo Tab M11 4GB 128GB Gris"), "Lenovo Tab M11")
check("Redmi Pad SE 8.7", sku("Xiaomi Redmi Pad SE 8.7 4GB 128GB"),
      "Redmi Pad SE 8.7")
check("Honor Pad X9a", sku("Honor Pad X9a 8GB 128GB"), "Honor Pad X9a")
check("POCO Pad", sku("POCO Pad 8GB 256GB"), "POCO Pad")

print("== ★ 顺序敏感：具体规则必须先于泛化规则命中 ==")
check("具体优先 13 M4", sku("iPad Air 13 M4"), "Apple iPad Air 13 M4")
check("次具体 13", sku("iPad Air 13 pulgadas"), "Apple iPad Air 13")
check("最泛化", sku("iPad Air Wi-Fi"), "Apple iPad Air")
check("Tab S10 FE+ 不被 S10 FE 抢走", sku("Galaxy Tab S10 FE+ 128GB"),
      "Samsung Galaxy Tab S10 FE+")
check("Tab S9 Ultra 不被 Tab S9 抢走", sku("Galaxy Tab S9 Ultra 512GB"),
      "Samsung Galaxy Tab S9 Ultra")

print("== ★ key 的尾部空格有意义（防止前缀误匹配）==")
check("iPad 8 → 8th Gen", sku("iPad 8 generacion 32GB"), "Apple iPad 8th Gen")
check("★iPad 80 不匹配 8th Gen", sku("iPad 80 pulgadas raro"), "Apple iPad")
check("iPad 7 → 7th Gen", sku("iPad 7 32GB"), "Apple iPad 7th Gen")

print("== ★ 配件判定（优先于 SKU 匹配）==")
check("保护套", kind("Funda para iPad Air 11 M2"), "accessory")
check("贴膜", kind("Mica de cristal templado para Galaxy Tab"), "accessory")
check("充电器", kind("Cargador USB-C 45W para tablet"), "accessory")
check("Apple Pencil", kind("Apple Pencil Pro para iPad"), "accessory")
check("充电宝", kind("Power Bank Xiaomi 20000mAh"), "accessory")
check("Smart Folio", kind("Apple Smart Folio para iPad Pro"), "accessory")

print("== ★ 捆绑装算整机（book cover + 有容量）==")
check("★纯配件：无容量", kind("Galaxy Tab S9+ Book Cover Keyboard"), "accessory")
check("★捆绑装：有容量就是整机",
      sku("Galaxy Tab S9+ 256GB con Book Cover Keyboard 12GB RAM"),
      "Samsung Galaxy Tab S9+")
check("含 memoria 也算整机",
      kind("Galaxy Tab S9 Book Cover memoria 128"), "sku")

print("== ★ M Pencil 的语境判定 ==")
check("笔单卖 → 配件", kind("ACME M Pencil 3 generacion"), "accessory")
check("★平板带笔 → 整机", sku("ACME Slate 11 con M Pencil incluido"),
      "Acme Slate 11")
check("M Pen 单卖 → 配件", kind("Acme M Pen 2 stylus"), "accessory")

print("== 兜底分类 ==")
check("认不出但是平板", sku("Tablet generica china 10 pulgadas"), "Other Tablet")
check("认不出也不是平板", sku("Audifonos Samsung Buds3"), "Other")
check("空标题", kind(""), "empty")
check("None", kind(None), "empty")

print("== 搜索词生成（保证覆盖）==")
terms = skumap.search_terms()
check("搜索词数 = SKU 数", len(terms), 131)
check_true("含 Galaxy Tab S11 Ultra", "Samsung Galaxy Tab S11 Ultra" in terms)
apple = skumap.search_terms("Apple")
check_true("按品牌过滤有效", all(t.startswith("Apple") for t in apple) and apple)

print("== 品牌归属 ==")
check("Redmi 归 Xiaomi", skumap.brand_of("Redmi Pad SE"), "Xiaomi")
check("POCO 归 Xiaomi", skumap.brand_of("POCO Pad X1"), "Xiaomi")
check("Apple", skumap.brand_of("Apple iPad Air"), "Apple")
check("Samsung", skumap.brand_of("Samsung Galaxy Tab S11"), "Samsung")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
