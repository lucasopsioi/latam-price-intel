# -*- coding: utf-8 -*-
"""回填 Fast Shop 渠道货号型号名 —— 从原始标题重新解析。

★ 名字变了就可能撞上已有产品（Galaxy S25 可能已存在），所以不是简单 UPDATE：
  撞上就**合并**（把引用改指到已有产品，删掉货号产品），没撞才改名。
"""
import json, re, sys
from app import db, skunorm
from app.agents.cleaner import CleanerAgent

DRY = "--apply" not in sys.argv
REFS = ["price_obs", "competitor_match", "launch_event", "product_page_cache",
        "review", "review_profile", "voc_insight", "price_move",
        "strategy_signal", "watchlist", "price_alert"]

def junk(m):
    m = (m or "").strip()
    return bool(re.match(r"^\d{4,}[_-]", m) or m.endswith("_PRD"))

rows = db.q("""SELECT rp.id, rp.model_name m, rp.category_code cat, rp.brand_id,
                      COALESCE(b.name,'') brand, b.aliases,
                      (SELECT po.title FROM price_obs po WHERE po.rival_product_id=rp.id
                        ORDER BY LENGTH(po.title) DESC LIMIT 1) title
               FROM rival_product rp LEFT JOIN brand b ON b.id=rp.brand_id""")
tgt = [r for r in rows if junk(r["m"])]
ag = CleanerAgent()
renamed = merged = skipped = 0
for r in tgt:
    if not r["title"]:
        skipped += 1; continue
    try: aliases = json.loads(r["aliases"] or "[]")
    except Exception: aliases = []
    res = skunorm.classify(r["brand"], r["title"], r["cat"])
    new = res["sku"] if (res.get("sku") and res.get("source") not in ("unavailable", "error")) else ""
    if not new:
        new = ag.normalize_model(r["title"], aliases, r["brand"])
    if not new or len(new) < 3 or junk(new):
        skipped += 1; continue
    key = new.lower().replace(" ", "")
    dup = db.q1("""SELECT id FROM rival_product
                   WHERE brand_id=? AND model_key=? AND category_code=? AND id<>?""",
                (r["brand_id"], key, r["cat"], r["id"]))
    if dup:
        merged += 1
        if not DRY:
            with db.tx() as c:
                for t in REFS:
                    # OR IGNORE：目标产品已有等价行时跳过（唯一约束冲突）
                    c.execute(f"UPDATE OR IGNORE {t} SET rival_product_id=? "
                              f"WHERE rival_product_id=?", (dup["id"], r["id"]))
                    # 跳过的那些是重复行，随货号产品一起清掉，不留悬挂引用
                    c.execute(f"DELETE FROM {t} WHERE rival_product_id=?", (r["id"],))
                c.execute("DELETE FROM rival_product WHERE id=?", (r["id"],))
    else:
        renamed += 1
        if not DRY:
            with db.tx() as c:
                c.execute("""UPDATE rival_product SET model_name=?, model_key=?,
                             name_source='title-backfill-20260828' WHERE id=?""",
                          (new, key, r["id"]))
print(("[预演] " if DRY else "[已执行] ") +
      f"目标 {len(tgt)} | 改名 {renamed} | 合并到已有产品 {merged} | 无法解析 {skipped}")
