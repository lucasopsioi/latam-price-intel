# -*- coding: utf-8 -*-
"""门店铺货信号（方向 14）的回归测试。

守三条：
  1. 「没有门店模块」不能记成「无货」—— 差 75 个百分点
  2. 一页出现多个门店模块时不许硬猜属于谁
  3. 这段文案仍然必须被 detect_in_stock 当假朋友抹掉（原来的坑不能复活）
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app.scraping import extract  # noqa: E402

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ─────────────── 1. 正例：有货，带门店名与件数 ───────────────
# 取自 Falabella 智利真实页面文本
POS = ("Especificaciones Código del producto: 17506904 Cód. tienda: 17506904 "
       "Entrega en Cerrillos Stock en tienda Falabella Plaza Oeste "
       "23 unidades disponibles Envío a domicilio")
d = extract.detect_store_stock(POS)
ok(d["store_stock"] == 1, f"应判为门店有货，实得 {d['store_stock']}")
ok(d["store_units"] == 23, f"件数应为 23，实得 {d['store_units']}")
ok("Plaza Oeste" in (d["store_name"] or ""), f"门店名应含 Plaza Oeste，实得 {d['store_name']}")

# 「Quedan solo N unidades」是另一种写法（低库存时）
POS2 = ("Entrega en Cerrillos Stock en tienda Falabella Electrohogar "
        "Quedan solo 1 unidades Mostrar Otras Tiendas")
d2 = extract.detect_store_stock(POS2)
ok(d2["store_stock"] == 1, "「Quedan solo」也应判为有货")
ok(d2["store_units"] == 1, f"件数应为 1，实得 {d2['store_units']}")


# ─────────────── 2. 负例：门店无货 ───────────────
NEG = ("Código del producto: 152598073 Entrega en Cerrillos "
       "Sin stock en tienda Cerrillos, Metropolitana De Santiago "
       "Mostrar Otras Tiendas Envío a domicilio")
d3 = extract.detect_store_stock(NEG)
ok(d3["store_stock"] == 0, f"应判为门店无货，实得 {d3['store_stock']}")
ok(d3["store_units"] is None, "无货时不该有件数")
ok("Cerrillos" in (d3["store_name"] or ""), f"应记下门店/区名，实得 {d3['store_name']}")


# ─────────────── 3. ★ 没有门店模块 ≠ 无货 ───────────────
# 实测 1108 个页面里 830 个（75%）根本没有这个模块。
# 把它们记成「无货」会让铺货率凭空差 75 个百分点。
for txt in ["", "SAMSUNG Galaxy S26 Ultra 512GB Negro Envío gratis",
            "Código del producto: 123 Envío a domicilio Llega mañana"]:
    d4 = extract.detect_store_stock(txt)
    ok(d4["store_stock"] is None,
       f"★ 没有门店模块必须返回 None（未知），不能记成无货：{txt[:30]!r} → {d4['store_stock']}")
ok(extract.detect_store_stock(None)["store_stock"] is None, "None 输入不该炸")


# ─────────────── 4. ★ 一页多个模块时不许硬猜 ───────────────
# 目前每页恰好 1 处（搜索链接会重定向到单商品页），但站方哪天改成真·列表页，
# 取第一处匹配就会把 A 商品的库存记到 B 商品头上，而且不报错。
MULTI = POS + " ..... " + NEG
d5 = extract.detect_store_stock(MULTI)
ok(d5["store_stock"] is None,
   f"★ 一页出现多个门店模块时必须返回 None —— 无法判断属于哪个商品，"
   f"硬取第一处会张冠李戴且不报错。实得 {d5['store_stock']}")

MULTI2 = POS + " ||| " + POS2
ok(extract.detect_store_stock(MULTI2)["store_stock"] is None,
   "两个正例同页也一样不许判")


# ─────────────── 5. ★ 原来的坑不能复活 ───────────────
# 「Sin stock en tienda X」里的缺货词曾让整条挂牌被判缺货，
# 实测 525 条在售商品（含当红款）因此被踢出价格分析。
# 现在既要**解析**它，又必须继续把它当假朋友**抹掉**。
ok(extract.detect_in_stock(NEG) is True,
   "★ 「Sin stock en tienda」说的是门店自提，不是商品缺货 —— "
   "整条挂牌必须仍判为在售（这个坑踢掉过 525 条在售商品）")
ok(extract.detect_in_stock(POS) is True, "门店有货时整条挂牌当然在售")
# 真正的缺货词仍要生效
ok(extract.detect_in_stock("Producto agotado temporalmente") is False,
   "真缺货仍要判缺货")


# ─────────────── 6. 落库链路 ───────────────
import inspect  # noqa: E402

from app.scraping.channels import base  # noqa: E402

src = inspect.getsource(base.ChannelAdapter._from_cards)
ok("detect_store_stock" in src, "卡片解析路径要取门店信号")
ok("detect_store_stock(blob)" in src,
   "★ 必须用**卡片自身文本**而不是整页文本 —— "
   "整页文本会把某个商品的门店库存记到同页所有商品头上")

from app import db  # noqa: E402

cols = {c[1] for c in [(0, m[1]) for m in [] ] } or None
mig = {(t, c) for t, c, _ in db.MIGRATIONS}
for c in ("store_stock", "store_units", "store_name"):
    ok(("price_obs", c) in mig, f"price_obs.{c} 应在迁移表里")

col = inspect.getsource(sys.modules.get("app.scraping.collector")
                        or __import__("app.scraping.collector",
                                      fromlist=["x"]))
ok("store_stock" in col and "store_units" in col,
   "collector 落库语句要带上门店字段，否则解析了也进不了库")


print(f"storestock: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
