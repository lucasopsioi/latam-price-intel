# -*- coding: utf-8 -*-
"""产品主数据测试 —— 重点是【重复导入的幂等性】。

★ 这个文件的由来（两个 critical 缺陷）：
  SQL 标准里 NULL != NULL，所以：
    UNIQUE(product_id, sku_id, country_code)  在 sku_id IS NULL 时永不冲突
    ON CONFLICT DO NOTHING                     在没有唯一约束时形同虚设
  表现是：每次重新导入都新建一份重复记录；改了价格重导，
  旧价那行还在，竞品匹配的 LIMIT 1 可能取到旧价去算价差 —— 静默算错。
  修法是建表达式唯一索引把 NULL 折叠成具体值。

跑法： python tests\test_products.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 本测试断言真实产品清单的目录不变量；公开仓不带 config/my_products.csv，
# 没有清单时直接跳过（复制 my_products.example.csv 为 my_products.csv 可启用）。
if not (ROOT / "config" / "my_products.csv").exists():
    print("跳过 test_products：config/my_products.csv 不存在"
          "（可复制 config/my_products.example.csv 启用）")
    sys.exit(0)

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_prod_"))
config.DB_PATH = _TMP / "t.db"
config.EXPORT_DIR = _TMP

from app import db, products  # noqa: E402

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

print("== ★ 表达式唯一索引已建立（NULL 折叠）==")
idx = {r["name"] for r in db.q(
    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ux_my%'")}
check_true("my_product 唯一索引", "ux_my_product" in idx, str(sorted(idx)))
check_true("my_sku 唯一索引", "ux_my_sku" in idx)
check_true("my_pricing 唯一索引", "ux_my_pricing" in idx)

print("== 内置清单导入 ==")
r1 = products.import_product_list()
check_true("首次导入成功", r1.get("created", 0) > 100, str(r1)[:120])
n1 = db.q1("SELECT COUNT(*) c FROM my_product")["c"]

print("== ★ 重复导入必须幂等（曾经每次都新建一份）==")
r2 = products.import_product_list()
n2 = db.q1("SELECT COUNT(*) c FROM my_product")["c"]
check("第二次导入不新建", r2.get("created", 0), 0)
check("产品总数不变", n2, n1)
check_true("第二次全部走更新分支", r2.get("updated", 0) > 100, str(r2)[:120])

r3 = products.import_product_list()
check("第三次仍不新建", db.q1("SELECT COUNT(*) c FROM my_product")["c"], n1)

print("== ★ 同名不同代号必须是两条记录 ==")
gt6 = db.q("""SELECT marketing_name, internal_code FROM my_product
              WHERE marketing_name='WATCH GT 6' ORDER BY internal_code""")
check_true("WATCH GT 6 有两个硬件平台", len(gt6) == 2,
           str([g["internal_code"] for g in gt6]))
codes = {g["internal_code"] for g in gt6}
check_true("代号是 Aris 与 Kelo", codes == {"Aris智能表", "Kelo智能表"}, str(codes))

dup = db.q("""SELECT marketing_name, COUNT(*) c FROM my_product
              GROUP BY marketing_name, COALESCE(internal_code,'')
              HAVING c > 1""")
check_true("★没有任何 (营销名,代号) 重复", not dup, str(dup[:3]))

print("== ★ my_pricing 的 UPSERT 在 sku_id IS NULL 时必须生效 ==")
pid = db.q1("SELECT id FROM my_product LIMIT 1")["id"]


def upsert_price(price):
    with db.tx() as conn:
        conn.execute("""
            INSERT INTO my_pricing(product_id,sku_id,country_code,rrp_local,currency,on_sale)
            VALUES(?,NULL,'MX',?,'MXN',1)
            ON CONFLICT(product_id, COALESCE(sku_id,0), country_code) DO UPDATE SET
              rrp_local=excluded.rrp_local, updated_at=datetime('now')
        """, (pid, price))


upsert_price(20000)
upsert_price(25999)
upsert_price(23499)
rows = db.q("SELECT rrp_local FROM my_pricing WHERE product_id=? AND country_code='MX'",
            (pid,))
check("★三次写入只留一行（NULL 折叠生效）", len(rows), 1)
check("★保留的是最后一次的价格", rows[0]["rrp_local"] if rows else None, 23499)

print("== ★ my_sku 的 UPSERT 在颜色/内存为 NULL 时必须生效 ==")


def upsert_sku(ean):
    with db.tx() as conn:
        conn.execute("""
            INSERT INTO my_sku(product_id,sku_name,ean_code)
            VALUES(?,'默认',?)
            ON CONFLICT(product_id, COALESCE(color,''), COALESCE(ram_gb,-1),
                        COALESCE(rom_gb,-1)) DO UPDATE SET ean_code=excluded.ean_code
        """, (pid, ean))


upsert_sku("111")
upsert_sku("222")
skus = db.q("SELECT ean_code FROM my_sku WHERE product_id=?", (pid,))
check("★两次写入只留一行", len(skus), 1)
check("★保留最后一次的 EAN", skus[0]["ean_code"] if skus else None, "222")

print("== 分类正确性 ==")
by_cat = {r["category_code"]: r["c"] for r in db.q(
    "SELECT category_code, COUNT(*) c FROM my_product GROUP BY category_code")}
check_true("五个产业都有产品", set(by_cat) == {"phone", "wearable", "audio", "tablet", "pc"},
           str(by_cat))
check_true("穿戴最多", by_cat.get("wearable", 0) > by_cat.get("pc", 0), str(by_cat))

print("== Excel 模板可生成且可回读 ==")
tpl = products.write_template(_TMP / "t.xlsx")
check_true("模板已生成", tpl.exists())
rep = products.import_workbook(tpl)
check_true("模板自身导入不报错", isinstance(rep, dict), str(rep)[:100])
check("★模板里的示例行不被当成真数据", rep.get("products", 0), 0)

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
