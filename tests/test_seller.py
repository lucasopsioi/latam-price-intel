# -*- coding: utf-8 -*-
"""卖家身份识别测试 —— 分清【渠道自营】/【品牌官方店】/【第三方】。

★ 由来（用户 2026-08-10 明确要求）：
  "美克多的网站里也会有官方店和非官方的，包括亚马逊也有官方的和非官方的"
  "利物浦里面有些商家是利物浦自营的，有些不是，要搞清楚哪些是自营的"

旧实现把 "liverpool"/"coppel"/"sears" 当官方标识、在整页文本里搜关键词，
而这些词在对应站点的页头页脚 logo alt 里到处都是 ——
**该站所有商品（含第三方卖家）全被判成官方自营**，
正好把要区分的两类混成一类，且完全看不出错。这组测试锁死修复。

跑法： python tests\test_seller.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scraping import seller  # noqa: E402
from app.scraping.seller import (BRAND_OFFICIAL, SELF_OPERATED,  # noqa: E402
                                 THIRD_PARTY, UNKNOWN)

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


def kind(text="", adapter="", brand="", seller_name=None, store_id=None,
         default=UNKNOWN):
    return seller.detect(page_text=text, adapter=adapter, brand=brand,
                         seller_name=seller_name, official_store_id=store_id,
                         channel_default=default)


print("== ★ 页头页脚的品牌词绝不能让商品变成「官方」 ==")
# 这是旧实现的致命假阳性：Liverpool 页面到处是 "Liverpool" 字样
LIVERPOOL_CHROME = (
    "Liverpool es parte de mi vida. Menú Liverpool Tienda Liverpool "
    "Crédito Liverpool © Liverpool 2026. Vendido por TecnoMundo SA de CV "
    "Samsung Galaxy S26 Ultra $26,399")
r = kind(LIVERPOOL_CHROME, "liverpool")
check("★第三方商品不因页面含 Liverpool 字样变官方", r["kind"], THIRD_PARTY)
check_true("卖家名抠对了", (r["seller_name"] or "").startswith("TecnoMundo"),
           str(r["seller_name"]))

print("== ★ Liverpool：没有 Vendido por 标签 = 自营 ==")
# 实抓依据：列表页 56 个卡片里只有 1 处该标签 → 自营商品不标卖家
r = kind("SAMSUNG Galaxy S26 Ultra Dynamic AMOLED 2X 6.9 pulgadas $ 23,999 . 20",
         "liverpool")
check("★无卖家标签判为自营", r["kind"], SELF_OPERATED)
check_true("是官方口径", r["is_official"])
check_true("理由说清了判据", "无第三方卖家标签" in r["reason"], r["reason"][:60])

r = kind("Galaxy A57 $8,074 Vendido por ElectroPlus", "liverpool")
check("有卖家标签判为第三方", r["kind"], THIRD_PARTY)
check_true("非官方口径", not r["is_official"])

print("== ★ Amazon：卖家 vs 配送方必须分开 ==")
# "第三方卖家 + 亚马逊配送" 在墨西哥站非常普遍，
# 把配送方当卖家会把大量第三方误判成官方
r = kind("Vendido por: CelularesMX  Enviado por: Amazon", "amazon")
check("★第三方卖家+亚马逊配送 → 第三方", r["kind"], THIRD_PARTY)
check_true("卖家名取的是卖家不是配送方",
           "celular" in (r["seller_name"] or "").lower(), str(r["seller_name"]))

r = kind("Vendido por: Amazon  Enviado por: Amazon", "amazon")
check("★亚马逊自营 → 自营", r["kind"], SELF_OPERATED)

r = kind("Vendido por: Samsung Store  Enviado por: Amazon", "amazon", brand="Samsung")
check("品牌旗舰店 → 品牌官方店", r["kind"], BRAND_OFFICIAL)
check_true("品牌店算官方口径", r["is_official"])

print("== ★ MercadoLibre：官方API的 official_store_id 是最硬证据 ==")
r = kind("", "meli", store_id=12345, seller_name="SAMSUNGOFICIAL")
check("有 official_store_id → 品牌官方店", r["kind"], BRAND_OFFICIAL)
check_true("理由点名了API字段", "official_store_id" in r["reason"], r["reason"][:60])

r = kind("", "meli", seller_name="VENDEDOR_GENERICO_99")
check("无 store_id 的卖家 → 第三方", r["kind"], THIRD_PARTY)

print("== ★ 纯 marketplace 无标识时不能默认当官方 ==")
# MELI/Shopee 自己不卖货，绝大多数挂牌是第三方。
# 这里若默认 official，会把整个平台的第三方溢价当成官方价
r = kind("Galaxy S26 Ultra $26,399", "meli")
check("★MELI 无标识 → unknown（不是 official）", r["kind"], UNKNOWN)
check_true("不算官方口径", not r["is_official"])
r = kind("Galaxy S26 $3,099", "shopee")
check("★Shopee 无标识 → unknown", r["kind"], UNKNOWN)

print("== 品牌官网天然自营 ==")
for ad in ("samsung", "apple", "brand_store"):
    r = kind("iPhone 17 Pro Desde $28,499", ad)
    check(f"{ad} → 自营", r["kind"], SELF_OPERATED)
    check_true(f"{ad} 算官方", r["is_official"])

print("== 官方店徽章 ==")
r = kind("Tienda oficial Samsung  Galaxy S26 $26,399", "meli", brand="Samsung")
check("徽章+品牌名 → 品牌官方店", r["kind"], BRAND_OFFICIAL)

print("== ★ 孤立的徽章文案不足以定性 ==")
# 与 Liverpool 那个原始 bug 同构：判定读的是整页文本，
# "tienda oficial" 可能出现在推荐位/导航/别的商品卡片里。
# 实测 Coppel 有 8 条自营商品因此被误判成 brand_official。
r = kind("Celular Samsung Galaxy S26+ Liberado 512 GB ... "
         "（页面别处）Visita nuestra tienda oficial ... ", "coppel")
check("★徽章离品牌名很远 → 不判品牌官方店", r["kind"], SELF_OPERATED)
r = kind("Loja oficial  Galaxy S26 R$4.999", "meli")
check("★无品牌参数时徽章也不足以定性", r["kind"], UNKNOWN)
r = kind("Tienda oficial Samsung Galaxy S26", "meli", brand="Samsung")
check("徽章紧邻品牌名 → 采信", r["kind"], BRAND_OFFICIAL)

print("== ★ 授权经销商 ≠ 品牌官方店 ==")
# Coppel/Sears 会标"distribuidor autorizado"（进货资质），
# 但商品是渠道自营、定价权在渠道 —— 不能判成"品牌在平台开的官方店"
r = kind("Celular Samsung Galaxy A27 5G Liberado 128 GB "
         "Distribuidor autorizado Samsung", "coppel")
check("★授权经销商标识不算品牌官方店", r["kind"], SELF_OPERATED)
check_true("仍算官方口径（价格可信）", r["is_official"])
r = kind("Galaxy S26 Tienda oficial Samsung", "meli", brand="Samsung")
check("真·官方店徽章仍然认", r["kind"], BRAND_OFFICIAL)

print("== 卖家名提取 ==")
cases = [
    ("Vendido por TecnoMundo SA", "tecnomundo"),
    ("Vendido por: CelularesMX", "celularesmx"),
    ("Vendido e entregue por Casas Bahia", "casas bahia"),
    ("Sold by TechStore", "techstore"),
    ("Vendido y enviado por Amazon", "amazon"),
]
for text, want in cases:
    name, _ = seller.extract_seller_name(text)
    check_true(f"抠出「{want}」", want in (name or "").lower(), f"got={name!r}")

name, shipper = seller.extract_seller_name("Vendido por: ABC  Enviado por: Amazon")
check_true("卖家与配送方分别抠出", (name or "").lower().startswith("abc")
           and "amazon" in (shipper or "").lower(), f"{name!r} / {shipper!r}")

print("== 粗分与细分保持一致 ==")
for k, coarse in [(SELF_OPERATED, "official"), (BRAND_OFFICIAL, "official"),
                  (THIRD_PARTY, "third_party"), (UNKNOWN, "unknown")]:
    check(f"{k} → {coarse}", seller._COARSE[k], coarse)

print("== 每条判定都要留下可读的依据 ==")
for text, ad in [("", "liverpool"), ("Vendido por X", "amazon"),
                 ("", "meli"), ("", "samsung")]:
    r = kind(text, ad)
    check_true(f"{ad} 有判定理由", len(r["reason"]) > 8, r["reason"][:40])

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
