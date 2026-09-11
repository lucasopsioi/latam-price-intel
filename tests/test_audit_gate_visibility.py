# -*- coding: utf-8 -*-
"""审计门的中间态可见性：被 `audit_status = 'accepted'` 挡掉的 pending 必须报出来。

在**临时库**上跑，绝不碰 intel.db。跑法： python tests\\test_audit_gate_visibility.py

由来（2026-09-04→09-05）：dashboard / boards / trends / notify 六处一起从
`<> 'rejected'` 切到 `= 'accepted'`，前提是"积压已清再重启服务"。前提**没成立** ——
近 30 天整机观测 MX 100% / PE 95% / CO 90% 仍是 pending。于是：
  · 首屏「每国每品类」里墨西哥整组行**从 GROUP BY 里消失**；
  · 矩阵、价格带、折扣热力、哑铃图整块留白。
而"这个国家没在打价格战"与"这个国家的数据还没审"在界面上**长得一模一样**，
后者是错的（silent-failures-detection：没审 ≠ 没有）。

守的性质：
  1. 每个聚合结果都带 pending_obs / 每行每格带 pending。
  2. **只有 pending 的组要留一行/一格**（obs=0 + audit_blocked），不许整个消失。
  3. ★ pending 的口径必须与图**逐字同源**：配件行、窗口外的行、别的国家的行
     都不许算进去 —— 对不上的数比不报更能骗人。
  4. 反向自检：把 pending 行删光后 audit_blocked 必须消失（不会红的断言等于没写）。
"""
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_gate_"))
config.DB_PATH = _TMP / "t.db"          # ★ 必须早于任何 db.get_conn()

from app import boards, dashboard, db  # noqa: E402

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
TODAY = date.today().isoformat()
YEST = (date.today() - timedelta(days=1)).isoformat()
OLD = (date.today() - timedelta(days=90)).isoformat()      # 窗口外

with db.tx() as c:
    c.execute("INSERT OR IGNORE INTO scrape_run(id,started_at,run_date,mode,status) "
              "VALUES(1,datetime('now'),date('now'),'test','ok')")
    for cid, cc, kind, sk in ((900, "MX", "retailer", "self_operated"),
                              (901, "MX", "marketplace", "third_party"),
                              (902, "CL", "retailer", "self_operated")):
        c.execute("INSERT INTO channel(id,code,name,country_code,kind,base_url,enabled,"
                  "default_seller_type) VALUES(?,?,?,?,?,?,1,?)",
                  (cid, f"t{cid}", f"店{cid}", cc, kind, f"https://{cid}.x/", sk))
    c.execute("INSERT INTO brand(id,name,is_ours,enabled) VALUES(900,'TestBrand',0,1)")

CUR = {"MX": "MXN", "CL": "CLP"}
_seq = [0]


def add(day, cc, cat, price, *, status="pending", kind="device", ch=None, disc=None,
        seller="self_operated", ram=None, rom=None, url=None, n=1):
    ids = []
    with db.tx() as c:
        for i in range(n):
            _seq[0] += 1
            cur = c.execute(
                """INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                      category_code,title,model_guess,sale_price,currency,url,audit_status,
                      run_id,row_hash,product_kind,condition,is_bundle,is_in_stock,
                      discount_pct,seller_kind,rival_product_id,ram_gb,rom_gb)
                   VALUES(?,?,?,900,?,?,?,?,?,?,?,1,?,?,'new',0,1,?,?,NULL,?,?)""",
                (day, cc, ch or (900 if cc == "MX" else 902), cat, f"机型{_seq[0]}",
                 f"机型{_seq[0]}", price, CUR[cc], url or f"https://x/{_seq[0]}", status,
                 f"h{_seq[0]}", kind, disc, seller, ram, rom))
            ids.append(cur.lastrowid)
    return ids


# CL 手机：有 accepted（图上看得见），也有 pending
add(TODAY, "CL", "phone", 300_000, status="accepted", disc=15, n=6)
add(YEST, "CL", "phone", 305_000, status="accepted", disc=12, n=6)
add(TODAY, "CL", "phone", 299_000, status="pending", disc=10, n=4)
# ★ MX 手机：**只有 pending**（真库里墨西哥就是这个状态）
add(TODAY, "MX", "phone", 9_000, status="pending", disc=20, n=9)
add(YEST, "MX", "phone", 9_100, status="pending", disc=18, n=5)
# 干扰项：都**不该**被算进 pending 计数
add(TODAY, "MX", "phone", 500, status="pending", kind="accessory", disc=30, n=7)  # 配件
add(OLD, "MX", "phone", 8_000, status="pending", disc=25, n=11)                   # 窗口外
add(TODAY, "MX", "phone", 9_500, status="rejected", disc=22, n=13)                # 已剔除

MX_PENDING_TODAY, MX_PENDING_YEST = 9, 5
MX_PENDING = MX_PENDING_TODAY + MX_PENDING_YEST
CL_PENDING = 4

print("== 首屏「每国每品类」：只有 pending 的组不许消失 ==")
rows = dashboard.country_category_summary(days=7)
by = {(r["cc"], r["cat"]): r for r in rows}
check_true("CL 手机在（有 accepted）", ("CL", "phone") in by)
check("CL 行报出被挡掉的 pending", by[("CL", "phone")]["pending"], CL_PENDING)
check("CL 行不是空态", by[("CL", "phone")]["audit_blocked"], False)
check_true("★MX 手机行还在（只有 pending 也要留一行）", ("MX", "phone") in by)
mx = by[("MX", "phone")]
check("MX 行 obs=0", mx["obs"], 0)
check("★MX 行报出 pending 条数", mx["pending"], MX_PENDING)
check("MX 行标了 audit_blocked", mx["audit_blocked"], True)
check_true("MX 行说得出原因", "待价格审计" in (mx.get("note") or ""), mx.get("note"))
check("★配件 / 窗口外 / 已剔除的行不算进 pending（口径与图同源）",
      mx["pending"], MX_PENDING)
check("MX 排在 CL 前面（country.sort_order 不变）",
      [r["cc"] for r in rows].index("MX") < [r["cc"] for r in rows].index("CL"), True)

print("== 时间矩阵：格子带 pending，只有 pending 的格子留空态格 ==")
m = dashboard.matrix(grain="day", days=7)
cells = {(c["period"], c["cc"], c["cat"]): c for c in m["cells"]}
check("矩阵报出总的 pending", m["pending_obs"], MX_PENDING + CL_PENDING)
check("矩阵报出空态格数（MX 两天）", m["blocked_cells"], 2)
check_true("矩阵 note 说得出原因", "待价格审计" in m["note"], m["note"])
check("audit_filter 写明口径", m["audit_filter"], "accepted")
check_true("★MX 今天的格子在", (TODAY, "MX", "phone") in cells)
check("MX 格子 obs=0 / pending=9", (cells[(TODAY, "MX", "phone")]["obs"],
                                    cells[(TODAY, "MX", "phone")]["pending"]),
      (0, MX_PENDING_TODAY))
check("MX 格子标了 audit_blocked", cells[(TODAY, "MX", "phone")]["audit_blocked"], True)
check("CL 格子照常有数且带 pending", (cells[(TODAY, "CL", "phone")]["obs"],
                                     cells[(TODAY, "CL", "phone")]["pending"]), (6, CL_PENDING))
check("期次仍按倒序", m["cells"][0]["period"], TODAY)
check("periods 含两天", sorted(m["periods"]), sorted({TODAY, YEST}))
m_cl = dashboard.matrix(grain="day", days=7, country="CL")
check("★按国家筛选时 pending 也跟着筛（不许报别国的数）",
      m_cl["pending_obs"], CL_PENDING)
check("筛 CL 时没有空态格", m_cl["blocked_cells"], 0)

print("== boards 四张图：都要报被挡掉的 pending ==")
band = boards.price_band(country="MX", category="phone", days=7, min_n=1)
check("价格带：MX 没有 accepted → 空图", band["items"], [])
check("★价格带报出 pending 条数", band["pending_obs"], MX_PENDING)
check_true("价格带 note 说得出是待审计", "待价格审计" in band["note"], band["note"])
band_cl = boards.price_band(country="CL", category="phone", days=7, min_n=1)
check("价格带（CL）有数据", len(band_cl["items"]), 1)
check("价格带（CL）也报 pending", band_cl["pending_obs"], CL_PENDING)

heat = boards.discount_heat(days=7, min_n=1)
hcells = {(c["x"], c["cat"]): c for c in heat["cells"]}
check("★热力图报出 pending 总数", heat["pending_obs"], MX_PENDING + CL_PENDING)
check("MX 手机格是空的但标了 audit_blocked", (hcells[("MX", "phone")]["v"],
                                              hcells[("MX", "phone")]["audit_blocked"]),
      (None, True))
check("MX 手机格报出 pending 条数", hcells[("MX", "phone")]["pending"], MX_PENDING)
check("CL 手机格有值 → 不算 audit_blocked", hcells[("CL", "phone")]["audit_blocked"], False)
check("★样本不足的空格与没审的空格分得开",
      hcells[("BR", "phone")]["audit_blocked"], False)
check_true("热力图 note 说得出原因", "还没审" in heat["note"], heat["note"])

# 哑铃图要求"同一产品 + 同配置 + 两类卖家"，所以它的 pending 计数也只能数满足这些条件的行
# —— 这正是"口径同源"要守的东西：上面那 14 条 MX pending 一条都不该出现在这里。
with db.tx() as c:
    c.execute("INSERT INTO rival_product(id,brand_id,category_code,model_name,model_key) "
              "VALUES(900,900,'phone','Test Phone X','testphonex')")
add(TODAY, "MX", "phone", 9_000, status="accepted", ram=8, rom=128, n=2)
add(TODAY, "MX", "phone", 11_000, status="accepted", ch=901, seller="third_party",
    ram=8, rom=128, n=2)
add(TODAY, "MX", "phone", 12_000, status="pending", ch=901, seller="third_party",
    ram=8, rom=128, n=3)
with db.tx() as c:
    c.execute("UPDATE price_obs SET rival_product_id=900 "
              "WHERE ram_gb=8 AND rom_gb=128 AND country_code='MX'")
sp = boards.seller_spread(country="MX", category="phone", days=7)
check("哑铃图：自营 vs 三方各有一条 accepted → 出一条线", len(sp["items"]), 1)
check("★哑铃图报出 pending 条数（只数它自己口径内的 3 条）", sp["pending_obs"], 3)
check_true("哑铃图 note 说得出是待审计", "待价格审计" in sp["note"], sp["note"])
check("audit_filter 写明口径", sp["audit_filter"], "accepted")

ps = boards.promo_shrink(days=7, min_basket=1)
check_true("★促销收缩（空态/不足）也带 pending 计数", "pending_obs" in ps, ",".join(sorted(ps)))
check("促销收缩的 pending 覆盖两国（含上面新加的 3 条第三方待审）",
      ps["pending_obs"], MX_PENDING + CL_PENDING + 3)
check_true("促销收缩 note 里带上了审计说明",
           "待价格审计" in (ps.get("note") or ""), ps.get("note"))
# ★ 另一条空态出口：历史不够（span<4）。它也必须带 pending 计数 ——
#   "数据太短"与"数据没审"是两个结论，在同一张空图上长得一样。
_orig_span = boards._available_span
boards._available_span = lambda: 2          # 假装库里只有 3 天数据
try:
    ps_short = boards.promo_shrink(days=7, min_basket=1)
finally:
    boards._available_span = _orig_span
check_true("★历史不够的空态也带 pending 计数", "pending_obs" in ps_short,
           ",".join(sorted(ps_short)))
check_true("历史不够的空态 note 同时说了两件事",
           "至少需要 4 天" in ps_short["note"] and "待价格审计" in ps_short["note"],
           ps_short["note"])

print("== ★ 反向自检：pending 清零后空态标记必须消失（不会红的断言等于没写）==")
with db.tx() as c:
    c.execute("UPDATE price_obs SET audit_status='accepted' WHERE audit_status='pending'")
rows2 = dashboard.country_category_summary(days=7)
check("清零后没有 audit_blocked 行", [r for r in rows2 if r["audit_blocked"]], [])
check("清零后 MX 行变成真行（obs>0）",
      next(r["obs"] for r in rows2 if r["cc"] == "MX") > 0, True)
m2 = dashboard.matrix(grain="day", days=7)
check("清零后矩阵 pending_obs = 0", m2["pending_obs"], 0)
check("清零后没有空态格", m2["blocked_cells"], 0)
check("清零后矩阵 note 为空", m2["note"], "")
check("清零后价格带 pending_obs = 0",
      boards.price_band(country="MX", category="phone", days=7, min_n=1)["pending_obs"], 0)
check("清零后热力图 note 为空", boards.discount_heat(days=7, min_n=1)["note"], "")

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
