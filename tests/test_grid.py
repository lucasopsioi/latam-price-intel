# -*- coding: utf-8 -*-
"""在线表格（app/web/grid.js + products.grid_*）的回归测试。

2026-09-07 用户要「Excel 那种感觉」，于是产品页从「下载模板→填→导入」
换成了可直接编辑的表格。这里守两类东西：

  一、后端：**可写列白名单**。前端传什么都不能越界 —— 这三张表是竞品匹配的
      输入，写坏一列，下游全错且不报错。还要守：改品类必须作废旧匹配
      （与 import_product_list / categorizer 同一条纪律）。
  二、前端：那些容易写错又不会报错的交互约定，用 ast/字符串在 grid.js 上钉住
      （Shift+Right 扩选 vs Shift+Tab 反向移动第一版就写混了）。

跑法： python tests\test_grid.py
"""
import ast
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app import config  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="grid_"))
config.DB_PATH = _TMP / "t.db"

from app import db, products  # noqa: E402

PASS, FAIL = 0, []


def ok(cond, msg):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(msg)


db.init_db()
with db.tx() as c:
    c.execute("""INSERT INTO my_product(id,marketing_name,category_code,status)
                 VALUES(1,'ACME Vega 80 Pro','phone','active')""")
    c.execute("""INSERT INTO my_product(id,marketing_name,category_code,status)
                 VALUES(2,'ACME Slate 12X','tablet','active')""")
    c.execute("INSERT INTO my_sku(id,product_id,color,ram_gb,rom_gb) VALUES(1,1,'黑',12,512)")

print("== 取数 ==")
for kind, n in (("product", 2), ("sku", 1), ("pricing", 0)):
    r = products.grid_rows(kind)
    ok(len(r["rows"]) == n, f"{kind} 应有 {n} 行，实得 {len(r['rows'])}")
    ok(bool(r["editable"]), f"{kind} 要返回可编辑列清单")
ok("product_name" in (products.grid_rows("sku")["rows"][0] or {}),
   "★ SKU/定价要把产品名带出来 —— 一屏全是 id 没法录")
try:
    products.grid_rows("nope")
    ok(False, "未知表名要报错")
except ValueError:
    PASS += 1

print("== 白名单：越界的列必须写不进去 ==")
before = db.q1("SELECT * FROM my_product WHERE id=1")
products.grid_save("product", [{"key": 1, "fields": {
    "marketing_name": "改了名",
    "id": 999,                       # 主键不在白名单
    "created_at": "1999-01-01",      # 不在白名单
    "notes": "备注可写",
}}])
after = db.q1("SELECT * FROM my_product WHERE id=1")
ok(after["marketing_name"] == "改了名", "白名单内的列要写进去")
ok(after["notes"] == "备注可写", "白名单内的 notes 要写进去")
ok(after["id"] == 1, "★ 主键不在白名单，不许被改")
ok(after["created_at"] == before["created_at"], "★ created_at 不在白名单，不许被改")

# 反向自检：把 id 加进白名单，上面那条断言必须失效 —— 证明它守的是真性质
_saved = products.GRID_TABLES["product"]["cols"]
products.GRID_TABLES["product"]["cols"] = _saved + ["status"]
products.grid_save("product", [{"key": 2, "fields": {"status": "eol"}}])
ok(db.q1("SELECT status FROM my_product WHERE id=2")["status"] == "eol",
   "（自检）白名单里的列确实能写 —— 说明上面的拒绝不是因为写入通路坏了")
products.GRID_TABLES["product"]["cols"] = _saved

print("== 数字列：非法值跳过该列，不写脏数据 ==")
products.grid_save("sku", [{"key": 1, "fields": {"ram_gb": "abc", "color": "白"}}])
row = db.q1("SELECT * FROM my_sku WHERE id=1")
ok(row["ram_gb"] == 12, f"★ 非法数字不许落库，实得 {row['ram_gb']}")
ok(row["color"] == "白", "同一行里合法的列照常写入（不因一列非法整行丢弃）")

print("== 新增行 ==")
r = products.grid_save("pricing", [{"key": None, "fields": {
    "product_id": 1, "country_code": "MX", "rrp_local": 24999,
    "currency": "MXN", "on_sale": 1}}])
ok(r["created"] == 1, f"key 为空要走新增，实得 {r}")
p = db.q1("SELECT * FROM my_pricing WHERE product_id=1")
ok(p and p["rrp_local"] == 24999 and p["country_code"] == "MX", "新增的定价要落库")

print("== ★ 改品类必须作废旧的自动匹配 ==")
with db.tx() as c:
    c.execute("""INSERT INTO rival_product(id,brand_id,category_code,model_name,model_key)
                 VALUES(9001,1,'phone','X','x')""")
    c.execute("""INSERT INTO competitor_match(my_product_id,rival_product_id,country_code,
                 total_score,source,is_confirmed,is_excluded)
                 VALUES(1,9001,'MX',0.9,'auto',0,0)""")
    c.execute("""INSERT INTO competitor_match(my_product_id,rival_product_id,country_code,
                 total_score,source,is_confirmed,is_excluded)
                 VALUES(1,9001,'CL',0.9,'auto',1,0)""")   # 人工确认的
r = products.grid_save("product", [{"key": 1, "fields": {"category_code": "tablet"}}])
left = db.q("SELECT * FROM competitor_match WHERE my_product_id=1")
ok(r["revoked_matches"] == 1,
   f"★ 品类改了要作废自动匹配（否则留下跨品类脏行），实得 {r['revoked_matches']}")
ok(len(left) == 1 and left[0]["is_confirmed"] == 1,
   "★ 人工确认的匹配不许删 —— 人的判断优先于算法")
r2 = products.grid_save("product", [{"key": 1, "fields": {"category_code": "tablet"}}])
ok(r2["revoked_matches"] == 0, "品类没变就不该作废任何东西")

print("== 前端约定（grid.js）==")
JS = (ROOT / "app/web/grid.js").read_text(encoding="utf-8")
ok("_key(e)" in JS and "beginEdit(k)" in JS,
   "★ 选中状态直接敲可打印字符要进入编辑并覆盖原值（Excel 行为）")
ok(re.search(r"k === 'ArrowRight'\s*\)\s*\{[^}]*move\(0,\s*1,\s*e\.shiftKey\)", JS),
   "★ Shift+Right 必须**扩选**（第一版与 Shift+Tab 写混，变成了单纯右移）")
ok(re.search(r"k === 'Tab'\s*\)\s*\{[^}]*move\(0,\s*e\.shiftKey \? -1 : 1,\s*false\)", JS),
   "★ Shift+Tab 是反向**移动**，不是扩选")
ok("const GUT = 46" in JS and "let left = GUT" in JS,
   "★ 冻结列的 left 偏移要从行号栏之后开始算，否则第一冻结列盖住行号")
ok("xl-rowhead" in JS and "xl-corner" in JS, "要有行号栏与全选角")
ok("clipboardData" in JS and "\\t" in JS, "剪贴板走 TSV，与 Excel 对拷")
ok("freeze-here" in JS and "unfreeze" in JS, "要有冻结到此与取消冻结")
ok("_resizeStart" in JS and "col-resize" in (ROOT / "app/web/style.css").read_text(encoding="utf-8"),
   "要能拖表头改列宽")
ok("if (col.type === 'number' && v !== '' && !isNum(v)) return false" in JS,
   "★ 前端也要挡非法数字 —— 不能只靠后端，否则界面上看着写进去了")

CSS = (ROOT / "app/web/style.css").read_text(encoding="utf-8")
ok(".xl-status" in CSS and ".xl-st" in CSS, "状态栏要有样式")
ok("xl-frozen-col" in CSS and "position: sticky" in CSS, "冻结靠 sticky 实现")
ok("prefers-color-scheme" in CSS or "--bg-elev" in CSS, "要跟随全站主题变量（含深色）")

HTML = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
ok("/static/grid.js" in HTML, "index.html 要引入 grid.js")
ok(HTML.index("/static/grid.js") < HTML.index("/static/app.js"),
   "grid.js 要在 app.js 之前加载")

import shutil  # noqa: E402
try:
    db.get_conn().close()
except Exception:  # noqa: BLE001
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {len(FAIL)} 失败")
for m in FAIL:
    print("  [FAIL]", m)
sys.exit(1 if FAIL else 0)
