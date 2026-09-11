# -*- coding: utf-8 -*-
"""把 LLM 重分类结果落成底数据（2026-08-31 用户：从底层梳理，要绝对准确）。

落库口径：
  - rival_product.category_code  ← 产品的真实品类（报告的产品维度按它走）
  - price_obs.category_code      ← 同步改（报告的价格分析按它走，两处必须一致）
  - product_kind='accessory'     ← 判为配件的，整条排除出设备价格分析
  - category_code=NULL           ← 判为 other（电视/路由等）的，不属于五个分析品类

★ 只改**判定与现状不同**的，且逐条留痕到 reclass_audit 表 —— 3501 条改动
  没有痕迹的话，事后发现某条判错了根本查不回来是谁改的、依据是什么。
★ dry-run 默认：不加 --apply 只打印统计与抽样，不动数据库。
"""
from __future__ import annotations

import io
import json
import sys
from collections import Counter

sys.path.insert(0, ".")
from app import db  # noqa: E402

DRY = "--apply" not in sys.argv
SRC = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else "data/reclass/result.json"

ANALYSIS_CATS = {"phone", "tablet", "wearable", "audio", "pc"}


def ensure_audit_table():
    with db.tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS reclass_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rival_product_id INTEGER NOT NULL,
            old_category TEXT, new_category TEXT,
            old_kind TEXT, new_kind TEXT,
            confidence TEXT, source TEXT, reason TEXT,
            obs_affected INTEGER,
            applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")


def main():
    items = json.load(io.open(SRC, encoding="utf-8"))
    cur = {r["id"]: r for r in db.q(
        """SELECT rp.id, rp.category_code cat, rp.model_name m,
                  COALESCE(b.name,'') brand,
                  (SELECT COUNT(*) FROM price_obs po WHERE po.rival_product_id=rp.id) obs
           FROM rival_product rp LEFT JOIN brand b ON b.id=rp.brand_id""")}

    changes, stats, conf = [], Counter(), Counter()
    for it in items:
        pid = it["id"]
        row = cur.get(pid)
        if not row:
            stats["产品已不存在(跳过)"] += 1
            continue
        new_cat = it["category"]
        conf[it.get("confidence", "?")] += 1
        # ★ 目标值：other / accessory 作为**禁用品类**入库（category 表里
        #   enabled=0），而不是置 NULL —— rival_product.category_code 有
        #   NOT NULL 约束，且置 NULL 会丢掉"它到底是什么"这条信息。
        #   报告只遍历 enabled=1 的品类，所以它们自动被排除出分析。
        tgt_cat = new_cat
        tgt_kind = "accessory" if new_cat == "accessory" else None
        if tgt_cat == row["cat"] and (tgt_kind is None or new_cat != "accessory"):
            stats["与现状一致"] += 1
            continue
        changes.append({
            "id": pid, "brand": row["brand"], "model": row["m"],
            "old": row["cat"], "new": new_cat, "tgt_cat": tgt_cat,
            "tgt_kind": tgt_kind, "obs": row["obs"],
            "confidence": it.get("confidence"), "source": it.get("source"),
            "reason": it.get("reason", ""),
        })
        stats[f"{row['cat']} → {new_cat}"] += 1

    print(("[预演] " if DRY else "[已执行] ")
          + f"判定 {len(items)} 条 | 需改动 {len(changes)} 个产品 "
            f"（涉及 {sum(c['obs'] for c in changes):,} 条观测）")
    print("置信度分布：" + "  ".join(f"{k}={v}" for k, v in conf.most_common()))
    print()
    print("改动明细（按观测量）：")
    for k, v in stats.most_common(18):
        if k in ("与现状一致", "产品已不存在(跳过)"):
            continue
        print(f"  {k:<26} {v:>4} 个产品")
    print()
    print("抽样 12 条：")
    for c in sorted(changes, key=lambda x: -x["obs"])[:12]:
        print(f"  {c['obs']:>5} obs | {c['old'] or 'NULL':<9} → {c['new']:<9} | "
              f"{c['brand']} {str(c['model'])[:26]:<26} | {c['reason'][:30]}")

    if DRY:
        print("\n加 --apply 才会真正写库。")
        return

    ensure_audit_table()
    # ★ 改品类会撞唯一约束 (brand_id, model_key, category_code)：目标品类下
    #   已有同名产品时不能直接改，要**合并**过去 —— 否则整条改动失败。
    #   实测 Apple「Watch Ultra 2」同时存在于 phone 与 wearable 两个品类下。
    REFS = ["price_obs", "launch_event", "product_page_cache",
            "review", "review_profile", "voc_insight", "price_move",
            "strategy_signal", "watchlist", "price_alert"]
    # ★ competitor_match **不改指、只删除**，所以不在 REFS 里。
    #   这是「SonicBuds 5 的竞品是小米手表」的根因：唯一约束是
    #   (brand_id, model_key, category_code)，同一型号可以在多个品类各存一行；
    #   重分类撞键时走合并分支，把 competitor_match.rival_product_id 从
    #   「同品类那一行」改指到「另一品类那一行」，一条合法匹配当场变成跨品类
    #   脏行。而审计表记的是**被删的源产品 id**，查目标产品什么都查不到 ——
    #   查不出来正是因为改指没留痕。
    #   匹配是**可重算的派生数据**，改指没有任何价值：正确做法是让它失效，
    #   下面收尾统一 rebuild_all 重算。
    DERIVED_DROP = ["competitor_match"]
    n_p = n_o = n_m = n_err = n_d = 0
    keys = {r["id"]: (r["brand_id"], r["model_key"])
            for r in db.q("SELECT id, brand_id, model_key FROM rival_product")}
    for c in changes:
        bk = keys.get(c["id"])
        dup = None
        if bk:
            dup = db.q1("""SELECT id FROM rival_product
                           WHERE brand_id IS ? AND model_key=? AND category_code=?
                             AND id<>?""", (bk[0], bk[1], c["tgt_cat"], c["id"]))
        try:
            with db.tx() as conn:
                conn.execute("""INSERT INTO reclass_audit(rival_product_id, old_category,
                                new_category, old_kind, new_kind, confidence, source,
                                reason, obs_affected) VALUES(?,?,?,?,?,?,?,?,?)""",
                             (c["id"], c["old"], c["new"], None, c["tgt_kind"],
                              c["confidence"], c["source"],
                              (c["reason"] or "")[:300], c["obs"]))
                if dup:
                    # 合并：引用改指到目标产品，重复行清掉，源产品删除
                    for t in REFS:
                        conn.execute(f"UPDATE OR IGNORE {t} SET rival_product_id=? "
                                     f"WHERE rival_product_id=?", (dup["id"], c["id"]))
                        conn.execute(f"DELETE FROM {t} WHERE rival_product_id=?", (c["id"],))
                    # 派生表：源产品的直接删，目标产品的也一并作废 ——
                    # 目标产品刚吃进一批新观测，旧匹配的价格快照已经不作数了
                    for t in DERIVED_DROP:
                        n_d += conn.execute(
                            f"DELETE FROM {t} WHERE rival_product_id IN (?,?)",
                            (c["id"], dup["id"])).rowcount or 0
                    conn.execute("DELETE FROM rival_product WHERE id=?", (c["id"],))
                    conn.execute("UPDATE price_obs SET category_code=? "
                                 "WHERE rival_product_id=?", (c["tgt_cat"], dup["id"]))
                    if c["tgt_kind"] == "accessory":
                        conn.execute("UPDATE price_obs SET product_kind='accessory' "
                                     "WHERE rival_product_id=?", (dup["id"],))
                    n_m += 1
                else:
                    # updated_at 必须跟着动，否则"品类在匹配之后被改过"
                    # 这件事在时间戳上完全隐形
                    conn.execute("UPDATE rival_product SET category_code=?, "
                                 "updated_at=datetime('now') WHERE id=?",
                                 (c["tgt_cat"], c["id"]))
                    r = conn.execute("UPDATE price_obs SET category_code=? "
                                     "WHERE rival_product_id=?", (c["tgt_cat"], c["id"]))
                    n_o += r.rowcount or 0
                    # 品类是竞品匹配的输入，改了就作废（人工确认/排除的不动）
                    for t in DERIVED_DROP:
                        n_d += conn.execute(
                            f"DELETE FROM {t} WHERE rival_product_id=? AND source='auto'"
                            f" AND is_confirmed=0 AND is_excluded=0",
                            (c["id"],)).rowcount or 0
                    if c["tgt_kind"] == "accessory":
                        conn.execute("UPDATE price_obs SET product_kind='accessory' "
                                     "WHERE rival_product_id=?", (c["id"],))
                    n_p += 1
        except Exception as e:                       # noqa: BLE001
            n_err += 1
            print(f"  ! id={c['id']} 改判失败（跳过，不影响其余）：{str(e)[:90]}")
    print(f"\n已更新：改品类 {n_p} 个，合并到已有产品 {n_m} 个，"
          f"观测 {n_o:,} 条；作废竞品匹配 {n_d} 条；失败 {n_err} 个；"
          f"全部改动已记入 reclass_audit。")

    # ★ 收尾必须重算。品类改了不重算 = 库里留着一批按旧品类算出来的匹配，
    #   它们不报错、不变色、computed_at 也不动，只会等用户截图。
    try:
        from app.matching.matcher import CompetitorMatcher
        res = CompetitorMatcher().rebuild_all()
        print(f"已重算竞品匹配：{res.get('matches')} 条"
              f"（产品 {res.get('products')} × 国家 {res.get('countries')}）")
    except Exception as e:                           # noqa: BLE001
        print(f"! 竞品匹配重算失败：{str(e)[:120]}\n"
              f"  库里现在**缺**匹配（已作废、未重建），"
              f"请手工跑 CompetitorMatcher().rebuild_all()")


if __name__ == "__main__":
    main()
