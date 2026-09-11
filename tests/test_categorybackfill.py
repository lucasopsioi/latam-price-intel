# -*- coding: utf-8 -*-
"""品类回填工具的 apply / rollback 往返 —— 在**临时库**上跑，绝不碰 intel.db。

★ 为什么要单独测这个：回滚清单是这次改动唯一的后悔药，而没被执行过的
  回滚路径等于没有。规则本身的测试在 tests/test_categorycrosscheck.py。

★ 关键姿势：必须在 db 建立第一个连接**之前**改掉 config.DB_PATH。
  db.get_conn() 把连接缓存在线程本地，一旦连上真库就再也切不走了 ——
  那样这个测试会安静地在生产库上跑。

跑法： python tests\test_categorybackfill.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config                          # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="catbackfill_"))
config.DB_PATH = _TMP / "test.db"                # ★ 必须早于任何 db.get_conn()

from app import db                               # noqa: E402
import importlib                                 # noqa: E402

backfill = importlib.import_module("tools.backfill_category_crosscheck")
backfill.ROLLBACK_DIR = _TMP / "rollback"

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


# ---------------------------------------------------------------- 建库 + 造数
# init_db 会带上 schema.sql 里的种子数据，所以维度行一律 OR IGNORE
db.init_db()
with db.tx() as c:
    c.execute("INSERT OR IGNORE INTO country(code,name_zh,currency,lang,locale,timezone) "
              "VALUES('MX','墨西哥','MXN','es','es-MX','America/Mexico_City')")
    for code in ("phone", "tablet", "audio", "wearable", "pc"):
        c.execute("INSERT OR IGNORE INTO category(code,name_zh) VALUES(?,?)", (code, code))
    c.execute("INSERT OR IGNORE INTO channel(id,code,country_code,name,kind) "
              "VALUES(1,'lp','MX','Liverpool','retailer')")
    c.execute("INSERT OR IGNORE INTO brand(id,name) VALUES(1,'Xiaomi')")

ROWS = [
    # (id, 采集品类, 标题, 期望结论)
    (1, "tablet", "XIAOMI Audífonos In-Ear Buds 6 Play inalámbricos", "audio"),
    (2, "tablet", "Reloj Inteligente Smartwatch T900 Pro Max L", "wearable"),
    (3, "tablet", "Notebook Gamer LOQ Intel Core i7 16GB RAM", "pc"),
    (4, "tablet", 'Tablet Samsung Galaxy Tab A9 64GB', None),          # 本类有证据，不动
    (5, "phone", "Honor 600E 512GB 5G Gratis Honor Play10+audifonos", None),  # 赠品闸
    (6, "tablet", "Juguete para bebé didáctico Winfun: Tablet I-Fun pad", "PENDING"),
    (7, "audio", "Celular Samsung Galaxy A16 128GB Negro", "phone"),
]
with db.tx() as c:
    for i, cat, title, _ in ROWS:
        c.execute("""INSERT INTO price_obs(id,obs_date,country_code,channel_id,brand_id,
                       category_code,title,sale_price,currency,product_kind,row_hash)
                     VALUES(?,'2026-08-27','MX',1,1,?,?,999,'MXN','device',?)""",
                  (i, cat, title, f"h{i}"))

BEFORE = {r["id"]: r["category_code"]
          for r in db.q("SELECT id,category_code FROM price_obs")}

# ---------------------------------------------------------------- 干跑
hits, total = backfill.scan()
check("扫描总分母 = device 且有品类的行数", total, 7)
by_id = {h["id"]: h for h in hits}
check("命中行数（4/5 不该命中）", sorted(by_id), [1, 2, 3, 6, 7])
check("耳机 → audio", by_id[1]["_target"], "audio")
check("智能表 → wearable", by_id[2]["_target"], "wearable")
check("笔记本 → pc", by_id[3]["_target"], "pc")
check("玩具 → pending", by_id[6]["_verdict"], "pending")
check("玩具的目标是 NULL（不是猜一个品类）", by_id[6]["_target"], None)
check("手机 → phone", by_id[7]["_target"], "phone")
check("干跑不改库", {r["id"]: r["category_code"]
                     for r in db.q("SELECT id,category_code FROM price_obs")}, BEFORE)

# ---------------------------------------------------------------- apply
backfill.apply_(hits)
after = {r["id"]: r["category_code"] for r in db.q("SELECT id,category_code FROM price_obs")}
check("① 改成 audio", after[1], "audio")
check("② 改成 wearable", after[2], "wearable")
check("③ 改成 pc", after[3], "pc")
check("④ 未命中，原样", after[4], "tablet")
check("⑤ 赠品闸拦住，原样", after[5], "phone")
check("⑥ pending → NULL（自动落选所有按品类查询）", after[6], None)
check("⑦ 改成 phone", after[7], "phone")

# 回滚清单必须落盘、且存的是**改前**的值
manifests = sorted((_TMP / "rollback").glob("category_crosscheck_*.json"))
check("回滚清单已落盘", len(manifests), 1)
import json                                       # noqa: E402
man = json.loads(manifests[0].read_text(encoding="utf-8"))
check("清单行数", len(man["rows"]), 5)
check("清单存的是改前的值", {r["id"]: r["old"] for r in man["rows"]},
      {1: "tablet", 2: "tablet", 3: "tablet", 6: "tablet", 7: "audio"})

# ---------------------------------------------------------------- rollback
backfill.rollback(str(manifests[0]))
check("★ 回滚后与改前逐行一致",
      {r["id"]: r["category_code"] for r in db.q("SELECT id,category_code FROM price_obs")},
      BEFORE)

# ---------------------------------------------------------------- 幂等
backfill.apply_(backfill.scan()[0])
snap = {r["id"]: r["category_code"] for r in db.q("SELECT id,category_code FROM price_obs")}
# ★ 改判后的行本品类已有证据 ⇒ 判 ok；pending 的行 category_code 已是 NULL，
#   而 scan() 的 WHERE 就带着 category_code IS NOT NULL ⇒ 也不会被重扫。
#   两条合起来：重复 --apply 是**空操作**，不会来回改同一批行。
check("apply 幂等：第二次扫描无任何命中", backfill.scan()[0], [])
backfill.apply_(backfill.scan()[0])
check("再 apply 一次也不动数据",
      {r["id"]: r["category_code"] for r in db.q("SELECT id,category_code FROM price_obs")},
      snap)
check("pending 行保持 NULL，不会被重新猜一个品类", snap[6], None)

# 每次 apply 都必须留下**独立**的回滚清单 —— 同秒连跑不能互相覆盖，
# 那是唯一的后悔药。
check("三次 apply 留下三份清单",
      len(sorted((_TMP / "rollback").glob("category_crosscheck_*.json"))), 3)

print(f"\n{PASS} pass / {FAIL} fail   （临时库：{_TMP}）")
sys.exit(1 if FAIL else 0)
