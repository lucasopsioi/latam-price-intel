# -*- coding: utf-8 -*-
"""修复"平板权威表越权认领其他品类商品"造成的错挂。

事故经过：
  `config/sku_rules.yaml` 是用户 PowerQuery 的 SKU_Short 表，**只覆盖平板**。
  但 `skumap.classify()` 当初没有品类闸门，被拿去判所有品类的标题。
  其中一条规则 key 是 `apple air` → `Apple iPad Air`，而归一化后的标题
      "apple airpods 4 con cancelacion activa de ruido"
  **含子串 "apple air"** ⇒ AirPods 命中 iPad 的规则。

后果链：
  price_obs.sku_code 写成 "Apple iPad Air"、product_kind 判成 device
  ⇒ cleaner 优先用 sku_code 建产品 ⇒ 建出「Apple iPad Air」(category=audio)
  ⇒ 该商品页的评论全挂到这个假产品上
  ⇒ 口碑维度图上 iPad 会显示"降噪好、佩戴舒适"。

根因已在 `skumap.classify(title, category)` 修掉（品类不在覆盖范围就不判定）。
这个脚本收拾已经落库的痕迹 —— **修 bug ≠ 清痕迹**，两件事都得做。

用法：
    python tools/fix_skumap_scope.py           # 只看会发生什么（默认）
    python tools/fix_skumap_scope.py --apply   # 真的执行
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, skumap, skunorm  # noqa: E402
from app.agents.cleaner import CleanerAgent  # noqa: E402

APPLY = "--apply" in sys.argv


def main() -> int:
    db.init_db()
    tablet_skus = set(skumap.all_skus())
    print(f"平板权威表共 {len(tablet_skus)} 个 SKU\n")

    # ---- 1. 找出被越权认领的挂牌 ----
    rows = db.q("""
        SELECT po.id, po.title, po.category_code, po.sku_code, po.brand_id,
               po.rival_product_id, po.url, b.name AS brand_name, b.aliases
        FROM price_obs po LEFT JOIN brand b ON b.id = po.brand_id
        WHERE po.sku_code IS NOT NULL AND po.sku_code <> ''
          AND po.category_code IS NOT NULL AND po.category_code <> 'tablet'
    """)
    victims = [r for r in rows if r["sku_code"] in tablet_skus]
    if not victims:
        print("没有发现越权认领的挂牌 —— 干净。")
        return 0

    by_cat = Counter(f"{r['category_code']} ← {r['sku_code']}" for r in victims)
    print(f"★ 被平板表越权认领的挂牌：{len(victims)} 条")
    for k, n in by_cat.most_common(12):
        print(f"    {n:5}  {k}")
    if len(by_cat) > 12:
        print(f"    …… 另有 {len(by_cat) - 12} 组")

    # ---- 2. 受牵连的假产品与评论 ----
    pids = {r["rival_product_id"] for r in victims if r["rival_product_id"]}
    ghosts = db.q(f"""
        SELECT rp.id, rp.model_name, rp.category_code,
               (SELECT COUNT(*) FROM review r WHERE r.rival_product_id=rp.id) nrev
        FROM rival_product rp
        WHERE rp.id IN ({','.join('?' * len(pids))}) AND rp.category_code <> 'tablet'
    """, tuple(pids)) if pids else []
    ghosts = [g for g in ghosts if g["model_name"] in tablet_skus]
    n_rev = sum(g["nrev"] for g in ghosts)
    print(f"\n★ 因此建出的假产品：{len(ghosts)} 个，其下挂着 {n_rev} 条评论")
    for g in ghosts:
        if g["nrev"]:
            print(f"    #{g['id']} 「{g['model_name']}」({g['category_code']}) "
                  f"← {g['nrev']} 条评论其实是别的产品的")

    # ---- 3. 重算这些挂牌的型号名（回到原始标题）----
    # ★ 走**和 cleaner 完全一样的优先级**：nubimetrics 官方名表优先，
    #   拿不到才退回本地通用归一化。不这么做的话，修完的名字和正常采集
    #   产出的名字会是两套写法，同一台机器又会裂成两个产品。
    #   实测差距很大：通用归一化给 "AirPods Max ISHOPPY MEXICO"（把卖家名
    #   吃进型号里）、"AirPods 4 Cancelación Activa"，nubimetrics 给
    #   "AirPods Max"、"AirPods 4"（还标了 verified）。
    print("\n★ 重算型号（从原始标题，不拿有损输出再洗）：")
    plan: list[tuple[dict, str, bool, str]] = []
    n_acc = 0
    for r in victims:
        try:
            aliases = json.loads(r["aliases"] or "[]")
        except Exception:  # noqa: BLE001
            aliases = []
        model, verified = "", False
        res = skunorm.classify(r["brand_name"] or "", r["title"] or "",
                               r["category_code"])
        if res.get("kind") == "配件":
            n_acc += 1                      # 配件不建竞品产品，与 cleaner 一致
            continue
        if res.get("sku") and res.get("source") not in ("unavailable", "error"):
            model, verified = res["sku"], bool(res.get("verified"))
        else:
            model = CleanerAgent.normalize_model(r["title"] or "", aliases,
                                                 r["brand_name"] or "")
        if len(model) < 3 or not r["brand_id"]:
            # 品牌都没识别出来的挂牌，重挂不了产品（rival_product.brand_id NOT NULL）。
            # 但**错的 sku_code 照样要清掉** —— 留着它下一轮还会把这条
            # 挂到 iPad 上去。下面的 leftover 分支会处理。
            continue
        # ★ 品类以**型号名**为准，不以采集上下文为准。
        #   price_obs.category_code 记的是"当时在抓哪个品类页"，
        #   一台 Galaxy Tab 出现在音频品类页里，它照样是平板。
        #   不覆盖的话这次修完会建出「Galaxy Tab A11」(category=audio)，
        #   等于把一个错误换成另一个错误。推不出来时才用采集上下文兜底。
        cat = skunorm.guess_category(model) or r["category_code"]
        plan.append((r, model, verified, cat))
    sample = Counter(f"{m}  [{c}]" for _, m, _, c in plan)
    for m, n in sample.most_common(10):
        print(f"    {n:5}  → {m}")
    moved_cat = sum(1 for r, _, _, c in plan if c != r["category_code"])
    print(f"    （其中 {moved_cat} 条按型号名纠正了品类，不跟采集上下文）")
    print(f"    （判为配件不建产品 {n_acc} 条；"
          f"重算不出可用型号 {len(victims) - len(plan) - n_acc} 条，只清 sku_code）")

    if not APPLY:
        print("\n（预演，未改动。加 --apply 真的执行）")
        return 0

    # ---- 4. 执行 ----
    relinked = 0
    with db.tx() as c:
        for r, model, verified, cat in plan:
            key = model.lower().replace(" ", "")
            pid, _ = CleanerAgent._upsert_rival(r["brand_id"], model, key,
                                                cat, verified)
            c.execute("""UPDATE price_obs SET sku_code=NULL, rival_product_id=?,
                                model_guess=? WHERE id=?""", (pid, model, r["id"]))
            relinked += 1
        # 重算不出型号 / 判成配件 / 没有品牌的：至少把错的 sku_code 清掉。
        # ★ 还要**把它和假产品脱钩**。只清 sku_code 的话，rival_product_id
        #   仍然指着那个假的「Apple iPad Air」，评论继续挂在上面 ——
        #   实测第一版就是这样漏下 4 条 AirPods 评论没迁走。
        #   脱钩后它们变成"没归属"，这是**诚实的空**，
        #   比"归属到一个错的产品"好：前者看得出缺数据，后者会被当成事实读。
        ghost_ids = {g["id"] for g in ghosts}
        leftover = [r for r in victims
                    if r["id"] not in {x["id"] for x, _, _, _ in plan}]
        for r in leftover:
            if r["rival_product_id"] in ghost_ids:
                c.execute("UPDATE price_obs SET sku_code=NULL, rival_product_id=NULL "
                          "WHERE id=?", (r["id"],))
            else:
                c.execute("UPDATE price_obs SET sku_code=NULL WHERE id=?", (r["id"],))
        # 评论也从假产品上摘下来
        for gid in ghost_ids:
            c.execute("UPDATE review SET rival_product_id=NULL WHERE rival_product_id=?",
                      (gid,))

    # ---- 5. 评论跟着商品页走 ----
    # 评论本来就是按 product_url 关联到挂牌的（voc.py 的兜底逻辑同款），
    # 挂牌重挂之后，评论要跟着改到新产品上。
    moved = 0
    with db.tx() as c:
        for row in db.q("""
                SELECT r.id AS rid, po.rival_product_id AS newpid
                FROM review r JOIN price_obs po ON po.url = r.product_url
                WHERE po.rival_product_id IS NOT NULL
                  AND (r.rival_product_id IS NULL
                       OR r.rival_product_id <> po.rival_product_id)
                GROUP BY r.id"""):
            c.execute("UPDATE review SET rival_product_id=? WHERE id=?",
                      (row["newpid"], row["rid"]))
            moved += 1

    # ---- 6. 删掉已经没人引用的假产品 ----
    killed = 0
    with db.tx() as c:
        for g in ghosts:
            used = c.execute(
                "SELECT (SELECT COUNT(*) FROM price_obs WHERE rival_product_id=?) + "
                "(SELECT COUNT(*) FROM review WHERE rival_product_id=?) + "
                "(SELECT COUNT(*) FROM rival_sku WHERE product_id=?)",
                (g["id"], g["id"], g["id"])).fetchone()[0]
            if not used:
                c.execute("DELETE FROM rival_product WHERE id=?", (g["id"],))
                killed += 1

    print(f"\n完成：重挂 {relinked} 条挂牌，迁移 {moved} 条评论，"
          f"删除 {killed} 个假产品（{len(ghosts) - killed} 个仍被引用，保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
