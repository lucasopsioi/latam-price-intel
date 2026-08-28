# -*- coding: utf-8 -*-
"""Acme自营商城适配器：我方官方定价的解析口径。

跑法：  python tests\test_acmestore.py

★ 为什么这个渠道值得单独一个测试文件：
  它是**我方官方价的唯一来源**（用户口径：「商城的价格就是官方定价」），
  这里错一个数字，「我方 vs 友商」的每一张图都跟着错，
  而且错得很像真的 —— 价格数量级对、币种对、只是数字不对。

★ 这个适配器修掉的两个真实错误：
  1. **通用解析器抓的是分期月供**：智利 Astra X7 记成 183,333 CLP，
     而 183,333 × 12 = 2,199,990 才是真实价。秘鲁、墨西哥同样中招
     （Slate Tab 记成 583~866 MXN，约 30 美元）。
     修法不是"调选择器"，而是**改用页面内嵌 JSON** ——
     那里 installmentAmount 与 totalAmount 是分开的字段，取不错。
  2. **接口原始数字过了本地化显示解析器**：CO 的 streetPrice 是字符串
     "9999900.00"，而 COP/CLP 的显示格式用 "." 当千分位 ⇒ 被读成
     999,990,000，大了 100 倍。现价恰好是 JSON 数字所以没中招，
     于是现价对、原价错，图上会显示"打了 99% 折"。
     （与 Entel 智利那次同一个坑：接口口径 ≠ 显示口径。）
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_hw_"))
config.DB_PATH = _TMP / "t.db"

from app import db  # noqa: E402
from app.scraping.channels import REGISTRY, build_adapter  # noqa: E402

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


db.init_db()

print("== 适配器已注册 ==")
check_true("acme_store 在注册表里", "acme_store" in REGISTRY)
check("类名", REGISTRY["acme_store"].__name__, "AcmeStoreAdapter")


def _adapter(cc: str, currency: str):
    ch = {"code": "acme_store", "adapter": "acme_store", "kind": "brand_store",
          "name": f"Acme商城 {cc}", "country_code": cc,
          "base_url": f"https://consumer.acme.com/{cc.lower()}"}
    return build_adapter(ch, {"code": cc, "currency": currency})


# ---------------------------------------------------------------- 夹具
# 结构照抄真实页面（2026-08-14 实抓），只保留判定用得到的字段。
def _page(items: list[dict], key: str = "productList") -> str:
    return ('<html><body><script>window.__DATA__={"moduleData":{"' + key + '":'
            + json.dumps(items, ensure_ascii=False) + '}}</script></body></html>')


FIXTURE_CL = _page([{
    "skuCode": "80560110010016802",
    "productTitle": "Astra X7, 16+512G, Negro",
    "linkUrl": "https://consumer.acme.com/cl/product/buy/?skucode=80560110010016802",
    # ★ 这三个数字是同一件商品的三种口径 —— 通用解析器抓的是最后一个
    "salePrice": 2199990, "streetPrice": "2199990.00",
    "installmentAmount": "183333.00", "installmentNum": 12,
    "disabled": False,
}])

FIXTURE_CO_DISCOUNT = _page([{
    "skuCode": "CO-1", "productTitle": "nimbus 15 Max",
    "linkUrl": "/co/product/nova15/",
    "salePrice": 1799900,          # JSON 数字
    "streetPrice": "2399900.00",   # ★ 字符串，小数点是**小数点**不是千分位
    "savePrice": "600000.00",
    "disabled": False,
}])

FIXTURE_NO_DISCOUNT = _page([{
    "skuCode": "X-1", "productTitle": "SonicClip 2 S",
    "salePrice": 219990, "streetPrice": "219990.00", "savePrice": "0.00",
}])

print("== ★ 取总价，不取分期月供 ==")
items = _adapter("CL", "CLP").parse_listings(FIXTURE_CL, "")
check("解析出 1 个商品", len(items), 1)
if items:
    it = items[0]
    check("★现价是总价 2,199,990，不是月供 183,333", it.sale_price, 2199990.0)
    check_true("★月供只留在 installments 里，绝不当价格",
               it.installments and "183333" in it.installments, str(it.installments))
    check("不打折时不填划线原价", it.list_price, None)
    check("SKU 已带上", it.sku_code, "80560110010016802")
    check("卖家恒为品牌官方", it.seller_kind, "brand_official")
    check("来源标成内嵌 JSON", it.source, "embedded_json")

print("== ★ 接口原始数字直读，不走本地化显示解析 ==")
items = _adapter("CO", "COP").parse_listings(FIXTURE_CO_DISCOUNT, "")
check("解析出 1 个商品", len(items), 1)
if items:
    it = items[0]
    check("现价", it.sale_price, 1799900.0)
    # ★ 这一条是本文件最关键的断言：
    #   "2399900.00" 若按 COP 显示口径（. 是千分位）会被读成 239,990,000
    check("★原价 2,399,900（不是被放大 100 倍的 239,990,000）",
          it.list_price, 2399900.0)
    check_true("★原价必须大于现价", it.list_price > it.sale_price)
    ratio = it.list_price / it.sale_price
    check_true("★原价/现价在合理倍数内（<3）", ratio < 3, f"倍数 {ratio:.1f}")

print("== 不打折时不能造出 0% 折扣 ==")
items = _adapter("CL", "CLP").parse_listings(FIXTURE_NO_DISCOUNT, "")
if items:
    check("★原价==现价时 list_price 留空", items[0].list_price, None)

print("== 边界与脏数据 ==")
check("空页面不炸", len(_adapter("MX", "MXN").parse_listings("<html></html>", "")), 0)
check("空字符串不炸", len(_adapter("MX", "MXN").parse_listings("", "")), 0)
bad = _page([{"skuCode": "n1", "productTitle": "无价商品"},
             {"skuCode": "n2", "productTitle": "", "salePrice": 999},
             {"skuCode": "n3", "productTitle": "零价", "salePrice": 0}])
items = _adapter("MX", "MXN").parse_listings(bad, "")
check("★没有标题的丢弃", all(i.title for i in items), True)
check_true("★价格为 0 的不当有效价", all(i.sale_price is None or i.sale_price > 0
                                        for i in items))

print("== 同一商品出现在多个容器里只算一次 ==")
dup = ('<html><body><script>{"productList":'
       + json.dumps([{"skuCode": "D1", "productTitle": "Astra X7", "salePrice": 100}],
                    ensure_ascii=False)
       + ',"flashSaleProductList":'
       + json.dumps([{"skuCode": "D1", "productTitle": "Astra X7", "salePrice": 100}],
                    ensure_ascii=False)
       + '}</script></body></html>')
check("★跨容器去重", len(_adapter("MX", "MXN").parse_listings(dup, "")), 1)

print("== 嵌套数组不能把括号配平搞乱 ==")
nested = _page([{
    "skuCode": "N1", "productTitle": "WATCH GT 6",
    "salePrice": 1299,
    # 真实页面里这些嵌套数组就在商品对象内部
    "hotSelling": [{"icon": "", "name": "长续航"}, {"icon": "", "name": "血氧"}],
    "cornerGroup": [{"cornerCopy": "NUEVO", "cornerType": {"value": "red"}}],
    "installmentInfos": [{"num": 12, "amount": "108"}],
}])
items = _adapter("PE", "PEN").parse_listings(nested, "")
check("★带嵌套数组的商品照样解析出来", len(items), 1)
if items:
    check("价格正确", items[0].sale_price, 1299.0)

print("== 相对链接要补成绝对 ==")
items = _adapter("CO", "COP").parse_listings(FIXTURE_CO_DISCOUNT, "")
if items:
    check_true("★相对 linkUrl 已补全域名",
               items[0].url.startswith("https://consumer.acme.com/co"),
               items[0].url)

print("== 不再需要进详情页 ==")
if items:
    d = _adapter("CO", "COP").parse_detail("", "", items[0])
    check("★列表页 JSON 已给全，标记为无需详情", d.detail_fetched, True)

print("== ★ 品牌自营商城：非配件即整机 ==")
# 由来：先用 skunorm.guess_category 认型号，81 个整机仍判不出来 ——
# Vega 90s Pro Max / Astra X7 / WATCH Apex / Band 11 它都不认识，
# 而 product_kind='device' 正是"我方官方价"的闸门，等于把自家旗舰全挡在外。
# 改成用渠道性质判：品牌自营商城不卖别人的东西，非配件即整机。
# 反过来做（认出来才算整机）会漏掉每一款新机，而新机恰恰最该看。
NEW_MODELS = _page([
    {"skuCode": "m1", "productTitle": "Vega 90s Pro Max", "salePrice": 22999},
    {"skuCode": "m2", "productTitle": "Astra X7", "salePrice": 34999},
    {"skuCode": "m3", "productTitle": "WATCH ULTIMATE DESIGN", "salePrice": 69999},
    {"skuCode": "m4", "productTitle": "ACME Band 11", "salePrice": 1499},
    {"skuCode": "a1", "productTitle": "WiFi 7 Mesh Router X1 Pro", "salePrice": 4499},
    {"skuCode": "a2", "productTitle": "Cargador Super Max 100W", "salePrice": 699},
    {"skuCode": "a3", "productTitle": "WATCH GT 5 Correa", "salePrice": 299},
])
items = {i.title: i for i in _adapter("MX", "MXN").parse_listings(NEW_MODELS, "")}
check("★没有任何 unknown",
      sum(1 for i in items.values() if i.product_kind == "unknown"), 0)
for name in ("Vega 90s Pro Max", "Astra X7", "WATCH ULTIMATE DESIGN", "ACME Band 11"):
    check(f"★{name} 判为整机", items[name].product_kind, "device")
for name in ("WiFi 7 Mesh Router X1 Pro", "Cargador Super Max 100W", "WATCH GT 5 Correa"):
    check(f"{name} 判为配件", items[name].product_kind, "accessory")

print("== ★ 套装要标出来，否则污染价格带 ==")
# 由来：秘鲁的 SonicBuds Pro 5 显示成 $506，而单卖只要 $186 ——
# 抓到的是 "nimbus 15 Max + SonicBuds Pro 5" 套装价。
BUNDLES = _page([
    {"skuCode": "b1", "productTitle": "nimbus 15 Max + SonicBuds Pro 5", "salePrice": 1899},
    {"skuCode": "b2", "productTitle": "SonicBuds Pro 5", "salePrice": 699},
    {"skuCode": "b3", "productTitle": "Astra X7, 16+512G, Negro", "salePrice": 7999},
    {"skuCode": "b4", "productTitle": "Slate 11.5 8+256G", "salePrice": 2399},
])
items = {i.title: i for i in _adapter("PE", "PEN").parse_listings(BUNDLES, "")}
check("★套装被标出来", items["nimbus 15 Max + SonicBuds Pro 5"].is_bundle, True)
check("单品不误标", items["SonicBuds Pro 5"].is_bundle, False)
# ★ 反向保险：容量写法里的 + 不能被当成套装
check("★「16+512G」不是套装", items["Astra X7, 16+512G, Negro"].is_bundle, False)
check("★「8+256G」不是套装", items["Slate 11.5 8+256G"].is_bundle, False)

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
