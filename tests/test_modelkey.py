# -*- coding: utf-8 -*-
"""型号键的回归测试。

守的是三条性质，每条都对应一次真实的错误输出：
  1. 中西文口径要能归一（不然电脑/平板一个对照都没有）
  2. 变体词不许撞键（不然 Vega 70 会匹到 Vega 70 Ultra，价差彻底错）
  3. 配置串不许当型号号码（不然同一个 nimbus 14 被拆成三个键）
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app.matching.modelkey import SERIES, model_keys, same_model  # noqa: E402

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ─────────────── 1. 中西文口径归一（全部取自真实数据）───────────────
# 这些是实测中"永远匹配不上"的原样，差在：「吋」是中文单位、
# D 16 与 D16 空格不同、"2024 12th Gen Core" 商城根本不写。
REAL_PAIRS = [
    ("AceBook D 16 16吋 2024 12th Gen Core",
     'ACME Laptop Acebook D16 16" FHD | Intel Core i5-13420H | 16GB RAM | 512GB SSD'),
    ("ACME Slate 11.5吋 2023",
     "ACME Tablet Acme Slate 11.5 256GB 8GB RAM Space Gray + Pen + Mouse"),
    ("ACME Slate Pro 13.2英寸 2023",
     'ACME Tablet Acme Slate Pro 13.2" 512GB 12GB RAM Gold + Teclado + Lapiz'),
    ("ACME Slate 12 X 12-inch",
     "ACME Tablet Acme Slate 12 X 256GB 12 GB RAM White"),
]
for mine, store in REAL_PAIRS:
    ok(same_model(mine, store),
       f"中西文应归一：{mine[:38]} ≠ {store[:44]}")

# 空格不敏感 —— 最初那个病
ok(same_model("AceBook D 16", "Acebook D16"),
   "★ 'D 16' 与 'D16' 必须同键：我方登记写空格、商城写连写，"
   "这正是电脑品类 0 匹配的直接原因")

# 中文单位不该影响
ok(same_model("ACME Slate 11.5吋", "ACME Slate 11.5英寸"),
   "吋 / 英寸 / 寸 都只是单位，不该改变型号身份")


# ─────────────── 2. 变体词必须区分（撞键 = 错误价差）───────────────
# 第一版把这三个全归成 vega-70，价位差一大截，算出的价差是彻底错的且不报错。
VARIANT_MUST_DIFFER = [
    ("Vega 70", "Vega 70 Pro"),
    ("Vega 70 Pro", "Vega 70 Ultra"),
    ("Vega 70", "Vega 70 Ultra"),
    ("nimbus 13", "nimbus 13 Pro"),
    ("nimbus 14", "nimbus 14 Pro"),
    ("WATCH GT 5", "WATCH GT 5 Pro"),
    ("Slate 11.5", "Slate 11.5 S"),
    ("Slate Pro 12.2", "Slate Pro 13.2"),
]
for a, b in VARIANT_MUST_DIFFER:
    ok(not same_model(a, b),
       f"★ 变体必须区分：{a} 与 {b} 撞键会把不同价位的机型判成同款，"
       f"算出的价差完全错误且不报错")


# ─────────────── 3. 配置串不许当型号号码 ───────────────
# 商城标题里 512GB / 12GB / 50MP 若被当成型号号码，
# 同一个 nimbus 14 会被拆成 nimbus-14 / nimbus-14-512gb / nimbus-14-512gb-12gb，
# 三个键互不相认 ⇒ 匹配不上。
CONFIG_NOISE = [
    ("nimbus 14", "ACME nimbus 14 512GB 12GB RAM"),
    ("nimbus 14", "ACME nimbus 14 256GB"),
    ("Vega 80", "ACME Vega 80 256GB 12GB 50MP"),
]
for a, b in CONFIG_NOISE:
    ok(same_model(a, b),
       f"★ 配置串不该改变型号身份：{a} ≠ {b} —— "
       f"512GB 被当成型号号码会把同款拆成多个键")

# 颜色/赠品/促销词同理
ok(same_model("Slate 11.5", "ACME Tablet Slate 11.5 Violet + Pen + Teclado Nuevo"),
   "颜色与赠品不该改变型号身份")


# ─────────────── 4. 系列词的匹配顺序 ───────────────
# SERIES 里长的必须排在前面，否则 'slate' 会被 'astra' 抢先匹配。
ok(SERIES.index("slate") < SERIES.index("astra"),
   "★ slate 必须排在 astra 之前，否则 Slate Tab 会被识别成 Astra 系列")
ok(SERIES.index("acebook") < SERIES.index("astra"),
   "★ acebook 必须排在 astra 之前")
for k in model_keys("ACME Slate 11.5"):
    ok(k.startswith("slate"), f"Slate Tab 应识别为 slate 系列，实得 {k}")


# ─────────────── 5. 边界安全 ───────────────
ok(model_keys(None) == set(), "None 不该炸")
ok(model_keys("") == set(), "空串不该炸")
ok(model_keys("完全不相关的东西") == set(), "认不出系列时应返回空集而不是瞎猜")
ok(not same_model("Vega 70", None), "有一边为空时不该判为同款")
ok(not same_model("随便什么", "另一个随便什么"),
   "★ 两边都认不出系列时**不能**判为同款 —— "
   "空集与空集相等会把所有无法识别的东西判成同一款")


# ─────────────── 6. 真实数据上的整体命中率不许退化 ───────────────
# 实测基线：114 个我方型号里 75 个能在商城找到（66%），
# 理论上限 82 个（72%）—— 其余 32 个是拉美商城确实没铺的代际。
try:
    from app import db
    my = [r["marketing_name"] for r in db.q(
        "SELECT marketing_name FROM my_product WHERE marketing_name IS NOT NULL")]
    titles = [r["title"] for r in db.q(
        """SELECT DISTINCT po.title FROM price_obs po JOIN brand b ON b.id=po.brand_id
           WHERE b.is_ours=1 AND po.product_kind='device'
             AND po.sale_price IS NOT NULL""")]
    if my and titles:
        store = set()
        for t in titles:
            store |= model_keys(t)
        hit = sum(1 for n in my if model_keys(n) & store)
        rate = hit / len(my)
        ok(rate >= 0.60,
           f"★ 命中率不该退化到 60% 以下（基线 66%，上限 72%），实得 {rate*100:.0f}%")
        # 平板与电脑曾经是 0，绝不能退回去
        for cat, floor in (("tablet", 0.8), ("pc", 0.2)):
            names = [r["marketing_name"] for r in db.q(
                "SELECT marketing_name FROM my_product WHERE category_code=?", (cat,))]
            if names:
                h = sum(1 for n in names if model_keys(n) & store)
                ok(h / len(names) >= floor,
                   f"★ {cat} 命中率不该低于 {floor*100:.0f}%（曾经是 0），"
                   f"实得 {h}/{len(names)}")
except Exception as e:                       # noqa: BLE001
    print(f"  （跳过真实数据检查：{type(e).__name__}）")


print(f"modelkey: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
