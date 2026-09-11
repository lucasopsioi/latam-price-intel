# -*- coding: utf-8 -*-
"""长期存档的三条不变量。

跑法：  python tests\test_archive.py

★ 为什么这些断言值钱：
  价格观测**不可再生** —— 今天某台机器标多少钱，明天变了就再也抓不回来。
  运行库只有一份，而且**维护脚本真的会删数据**
  （tools/renorm_skus.py 合并产品时会 `DELETE FROM review WHERE ...`）。
  存档要挡住的正是这件事，所以下面三条必须有测试守着：

    A. 只增不删     —— 存档代码里不许出现 DELETE / DROP
    B. 行数不许减   —— 重导时若源库行数变少，**拒绝覆盖**并留痕
    C. 清单只追加   —— manifest.jsonl 永远不重写

  外加一条工程性的：**存档不许因为一张表出错就整体失败** ——
  那样的后果是"今天一份存档都没有"，恰恰是存档要防的事。
"""
import gzip
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_arch_"))
config.DB_PATH = _TMP / "t.db"

from app import archive, db  # noqa: E402

archive.ARCHIVE_DIR = _TMP / "archive"

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

# ---------------------------------------------------------------- 造数据
with db.tx() as c:
    c.execute("INSERT OR IGNORE INTO scrape_run(id,started_at,run_date,mode,status) "
              "VALUES(1,datetime('now'),date('now'),'test','ok')")
    c.execute("INSERT INTO channel(id,code,name,country_code,kind,base_url,enabled) "
              "VALUES(900,'t','测试渠道','MX','retailer','https://t.mx/',1)")
    for i in range(30):
        d = "2026-08-10" if i < 12 else "2026-08-11"
        c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,title,
                        sale_price,currency,url,audit_status,run_id,row_hash)
                     VALUES(?,'MX',900,?,?,'MXN',?,'ok',1,?)""",
                  (d, f"Acme Astra {i} 手机", 9999.0 + i, f"https://t.mx/p/{i}", f"h{i}"))

print("== 首次存档 ==")
r1 = archive.export_facts(only="price_obs")
check("两天各一个分区", r1["written"], 2)
check("行数对得上", r1["rows"], 30)

f10 = archive.ARCHIVE_DIR / "datasets/price_obs/part=2026-08-10/price_obs.csv.gz"
check_true("分区文件已生成", f10.exists())

print("== ★ 存档是自包含的：不依赖本项目也能读 ==")
raw = gzip.open(f10, "rb").read().decode("utf-8-sig")
lines = [l for l in raw.splitlines() if l.strip()]
check("表头 + 12 行数据", len(lines), 13)
check_true("★中文没有乱码（本机 ANSI 是 cp936）", "Acme Astra 0 手机" in raw, raw[:80])
check_true("★带 UTF-8 BOM，解压后 Excel 能正确认中文",
           gzip.open(f10, "rb").read().startswith(b"\xef\xbb\xbf"))

print("== 幂等：没变化就不重写 ==")
r2 = archive.export_facts(only="price_obs")
check("全部跳过", r2["skipped"], 2)
check("没有新写", r2["written"], 0)

print("== 增长：多了行就重新导出 ==")
with db.tx() as c:
    c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,title,
                    sale_price,currency,url,audit_status,run_id,row_hash)
                 VALUES('2026-08-10','MX',900,'新增一条',1.0,'MXN','https://t.mx/p/x','ok',1,'hx')""")
r3 = archive.export_facts(only="price_obs")
check("★增长触发重导", r3["written"], 1)
raw = gzip.open(f10, "rb").read().decode("utf-8-sig")
check_true("新行确实进了存档", "新增一条" in raw)

print("== ★★ 不变量 B：源库行数变少时必须拒绝覆盖 ==")
# 模拟"维护脚本把一批观测删了"
with db.tx() as c:
    c.execute("DELETE FROM price_obs WHERE obs_date='2026-08-10' AND row_hash IN "
              "('h0','h1','h2','h3','h4')")
before_bytes = f10.stat().st_size
before_text = gzip.open(f10, "rb").read()
r4 = archive.export_facts(only="price_obs")
check("★拒绝了 1 个分区", r4["refused"], 1)
check("★没有覆盖任何文件", r4["written"], 0)
check_true("★旧存档原封不动", gzip.open(f10, "rb").read() == before_text)
check("★旧存档字节数未变", f10.stat().st_size, before_bytes)
raw = gzip.open(f10, "rb").read().decode("utf-8-sig")
check_true("★被删掉的那几行仍在存档里", "Acme Astra 0 手机" in raw)

print("== 拒绝这件事本身要留痕（否则等于没发生）==")
mp = archive.ARCHIVE_DIR / "manifest.jsonl"
recs = [json.loads(l) for l in mp.read_text(encoding="utf-8").splitlines() if l.strip()]
refused = [r for r in recs if r.get("action") == "refused_shrink"]
check_true("★清单里记了 refused_shrink", len(refused) >= 1)
if refused:
    # 记的是**这个分区**的行数（2026-08-10：13 行 → 删 5 行 → 8 行），
    # 不是整表行数。分区才是覆盖的单位，也才是该报警的单位。
    check("★记下了现在多少行", refused[-1]["rows_now"], 8)
    check("★记下了存档时多少行", refused[-1]["rows_archived"], 13)
    check("★记下了是哪个分区", refused[-1]["partition"], "2026-08-10")

print("== ★ 不变量 C：清单只追加，从不重写 ==")
n_before = len(recs)
archive.export_facts(only="price_obs")
recs2 = [json.loads(l) for l in mp.read_text(encoding="utf-8").splitlines() if l.strip()]
check_true("★清单只增不减", len(recs2) >= n_before)
check_true("★历史记录没有被改写",
           [json.dumps(r, sort_keys=True) for r in recs2[:n_before]]
           == [json.dumps(r, sort_keys=True) for r in recs])

print("== ★ 不变量 A：存档从不执行 DELETE / DROP ==")
# ★ 判据要精确到"**执行**了什么 SQL"，不能拿全文搜关键字：
#   archive.py 的文档字符串里就写着 `DELETE FROM review WHERE ...`
#   （在解释"维护脚本会删数据，所以才要存档"）——
#   按全文搜会把这句注释当成违规，测试变成噪声然后被无视。
#   用 ast 找所有 execute(...) 调用的 SQL 字面量，只看真正要跑的语句。
import ast  # noqa: E402

tree = ast.parse((ROOT / "app" / "archive.py").read_text(encoding="utf-8"))
executed_sql: list[str] = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name in ("execute", "executescript", "executemany", "q", "q1"):
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    executed_sql.append(a.value.upper())
                elif isinstance(a, ast.JoinedStr):   # f-string 拼的 SQL
                    executed_sql.append("".join(
                        v.value.upper() for v in a.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)))
check_true("★确实扫到了 SQL 语句（否则这条断言是空转）", len(executed_sql) >= 5,
           f"扫到 {len(executed_sql)} 条")
for kw in ("DELETE", "DROP", "TRUNCATE", "UPDATE"):
    hits = [s for s in executed_sql if kw in s]
    check_true(f"★存档执行的 SQL 里没有 {kw}", not hits, str(hits)[:90])

print("== ★ 一张表坏掉不能连累其它表（不许 fail-closed）==")
saved = dict(archive.FACT_TABLES)
try:
    archive.FACT_TABLES["price_obs"] = "date(这个列不存在)"
    r5 = archive.export_facts(only="price_obs")
    check_true("★分区键失效时降级整表存，而不是抛异常",
               r5["written"] + r5["skipped"] >= 1, str(r5))
    deg = archive.ARCHIVE_DIR / "datasets/price_obs/part=_all/price_obs.csv.gz"
    check_true("★降级分区文件已生成", deg.exists())
finally:
    archive.FACT_TABLES.clear()
    archive.FACT_TABLES.update(saved)

print("== 整库快照必须能真的打开（不是复制个文件了事）==")
snap = archive.snapshot_db(keep=5)
check_true("快照已生成", snap is not None and snap.exists())
if snap:
    import os
    import sqlite3
    tmpdb = _TMP / "restore.db"
    with gzip.open(snap, "rb") as fi, open(tmpdb, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    con = sqlite3.connect(str(tmpdb))
    n = con.execute("SELECT COUNT(*) FROM price_obs").fetchone()[0]
    con.close()
    os.unlink(tmpdb)
    check("★快照里的数据能读出来", n, 26)

print("== 校验能发现静默损坏 ==")
v = archive.verify()
check_true("校验通过", len(v["changed"]) == 0 and len(v["missing"]) == 0, str(v))
# 人为改坏一个文件
with gzip.open(f10, "wb") as f:
    f.write("坏掉了".encode("utf-8"))
v2 = archive.verify()
check_true("★改动过的文件被查出来", len(v2["changed"]) >= 1, str(v2["changed"])[:120])

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
