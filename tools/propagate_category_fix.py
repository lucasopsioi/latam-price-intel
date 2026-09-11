# -*- coding: utf-8 -*-
"""把 price_obs 的品类修正**传导**到 price_move 与 rival_product。

背景（2026-08-28）：backfill_category_crosscheck 修了 8859 行 price_obs，
但周报的 Top 变动表直接读 price_move.category_code、产品口径读
rival_product.category_code —— 不传导的话「OPPO A5 出现在穿戴」照旧。

口径：
  · 产品真实品类 = 其 price_obs 中非 NULL 品类的**多数票**（修正后）。
  · rival_product 改品类可能撞 UNIQUE(brand_id, model_key, category_code)
    —— 撞上就合并（引用改指 + 删重复行），与型号名回填同一套做法。
  · 全部观测品类都为 NULL 的产品（防丢器等）不动：所有按品类的查询
    都命中不到它，等效隐身。

用法：  python tools/propagate_category_fix.py            # 干跑
        python tools/propagate_category_fix.py --apply
"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import db                                  # noqa: E402

APPLY = "--apply" in sys.argv
REFS = ["price_obs", "launch_event", "product_page_cache",
        "review", "review_profile", "voc_insight", "price_move",
        "strategy_signal", "watchlist", "price_alert"]
# competitor_match 不改指、只删除 —— 唯一约束是 (brand_id, model_key,
# category_code)，同一型号可在多个品类各存一行；合并时把匹配从「同品类那一行」
# 改指到「另一品类那一行」，合法匹配当场变成跨品类脏行，而且不留任何痕迹
# （审计记的是被删的源产品 id）。匹配是可重算的派生数据，作废后统一重算。
DERIVED_DROP = ["competitor_match"]

# ── ① 产品多数票品类 ──
votes = db.q("""SELECT rival_product_id pid, category_code cat, COUNT(*) n
                FROM price_obs
                WHERE rival_product_id IS NOT NULL AND category_code IS NOT NULL
                GROUP BY pid, cat""")
best: dict[int, tuple[str, int]] = {}
for v in votes:
    cur = best.get(v["pid"])
    if cur is None or v["n"] > cur[1]:
        best[v["pid"]] = (v["cat"], v["n"])

dropped = 0
prods = db.q("SELECT id, brand_id, model_key, category_code FROM rival_product")
recat = merged = 0
for p in prods:
    got = best.get(p["id"])
    if not got or got[0] == p["category_code"]:
        continue
    newcat = got[0]
    dup = db.q1("""SELECT id FROM rival_product WHERE brand_id=? AND model_key=?
                   AND category_code=? AND id<>?""",
                (p["brand_id"], p["model_key"], newcat, p["id"]))
    if dup:
        merged += 1
        if APPLY:
            with db.tx() as c:
                for t in REFS:
                    c.execute(f"UPDATE OR IGNORE {t} SET rival_product_id=? "
                              f"WHERE rival_product_id=?", (dup["id"], p["id"]))
                    c.execute(f"DELETE FROM {t} WHERE rival_product_id=?", (p["id"],))
                for t in DERIVED_DROP:
                    dropped += c.execute(
                        f"DELETE FROM {t} WHERE rival_product_id IN (?,?)",
                        (p["id"], dup["id"])).rowcount or 0
                c.execute("DELETE FROM rival_product WHERE id=?", (p["id"],))
    else:
        recat += 1
        if APPLY:
            with db.tx() as c:
                # updated_at 跟着动，否则"品类在匹配之后被改过"在时间戳上隐形
                c.execute("UPDATE rival_product SET category_code=?, "
                          "updated_at=datetime('now') WHERE id=?",
                          (newcat, p["id"]))
                for t in DERIVED_DROP:
                    dropped += c.execute(
                        f"DELETE FROM {t} WHERE rival_product_id=? AND source='auto'"
                        f" AND is_confirmed=0 AND is_excluded=0",
                        (p["id"],)).rowcount or 0

# ── ② price_move 跟随产品品类 ──
moves = db.q("""SELECT pm.id, pm.category_code cat, pm.rival_product_id pid
                FROM price_move pm WHERE pm.rival_product_id IS NOT NULL""")
mv_fix = 0
for m in moves:
    got = best.get(m["pid"])
    want = got[0] if got else None
    if want != m["cat"]:
        mv_fix += 1
        if APPLY:
            with db.tx() as c:
                c.execute("UPDATE price_move SET category_code=? WHERE id=?",
                          (want, m["id"]))

tag = "[已执行]" if APPLY else "[干跑]"
print(f"{tag} rival_product 改品类 {recat} · 合并 {merged} | price_move 改品类 {mv_fix} | 作废竞品匹配 {dropped}")

# 品类改了必须重算竞品匹配 —— 否则库里留着一批按旧品类算出来的匹配，
# 它们不报错、不变色、computed_at 也不动，只会等用户截图。
if APPLY and (recat or merged):
    try:
        from app.matching.matcher import CompetitorMatcher
        res = CompetitorMatcher().rebuild_all()
        print(f"已重算竞品匹配：{res.get('matches')} 条")
    except Exception as e:                           # noqa: BLE001
        print(f"! 竞品匹配重算失败：{str(e)[:120]}"
              f"  库里现在**缺**匹配（已作废、未重建），"
              f"请手工跑 CompetitorMatcher().rebuild_all()")
