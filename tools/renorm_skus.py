# -*- coding: utf-8 -*-
"""用 nubimetrics 的清洗规则重算全部友商型号名，并合并因此重合的产品。

为什么要从 **price_obs.title 原始标题** 重算，而不是拿现有 model_name 再洗一遍：
  现有名字已经是上一版规则的**有损输出**（"Cobertura Satelital Este Equipo"、
  "17t 12ram"、"Galaxy S26 Por FALABELLA"）。信息已经丢了，再洗只是把垃圾洗干净，
  洗不回真名。原始标题才是唯一的事实来源。

用法：
    python tools/renorm_skus.py           # 只看会发生什么（默认）
    python tools/renorm_skus.py --apply   # 真的执行
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, skunorm  # noqa: E402

REF_TABLES = ["price_obs", "competitor_match", "launch_event", "product_page_cache",
              "review", "review_profile", "voc_insight", "price_move",
              "strategy_signal"]

APPLY = "--apply" in sys.argv


def pick_name(cands: list[tuple[str, bool, str]]) -> tuple[str, bool, str]:
    """一个产品可能有多条挂牌（不同渠道/不同标题），选一个代表名。

    ★ 查证过的优先，其次取出现次数最多的 —— 不是取第一条。
      第一条纯看抓取顺序，而"多数渠道都这么叫"更接近真实叫法。
    """
    ver = [c for c in cands if c[1]]
    pool = ver or cands
    top = Counter(c[0] for c in pool).most_common(1)[0][0]
    for c in pool:
        if c[0] == top:
            return c
    return pool[0]


def main() -> int:
    if not skunorm.available():
        print("✗ 找不到 nubimetrics-platform，无法重算。")
        print("  设环境变量 NUBIMETRICS_PATH 指向该项目根目录后重试。")
        return 2

    brands = {b["id"]: b["name"] for b in db.q("SELECT id, name FROM brand")}

    # 每个产品收集它名下所有挂牌标题，逐条重算
    rows = db.q("""
        SELECT rp.id, rp.brand_id, rp.category_code, rp.model_name AS old_name,
               po.title
        FROM rival_product rp
        JOIN price_obs po ON po.rival_product_id = rp.id
        WHERE po.title IS NOT NULL AND po.title <> ''
    """)
    per_product: dict[int, list[tuple[str, bool, str]]] = defaultdict(list)
    meta: dict[int, dict] = {}
    accessory_only: set[int] = set()
    acc_hits: dict[int, int] = Counter()
    total_titles: dict[int, int] = Counter()

    for r in rows:
        meta[r["id"]] = {"brand_id": r["brand_id"], "cat": r["category_code"],
                         "old": r["old_name"]}
        total_titles[r["id"]] += 1
        res = skunorm.classify(brands.get(r["brand_id"], ""), r["title"],
                               r["category_code"])
        if res["kind"] == "配件":
            acc_hits[r["id"]] += 1
            continue
        if res["sku"] and res["source"] not in ("unavailable", "error"):
            per_product[r["id"]].append((res["sku"], bool(res["verified"]),
                                         res["source"]))

    # 名下**所有**标题都被判成配件的产品，才算配件（单条命中不足以定性）
    for pid, n in acc_hits.items():
        if n == total_titles[pid] and pid not in per_product:
            accessory_only.add(pid)

    # 算新名字 + 新分组
    groups: dict[tuple, list[int]] = defaultdict(list)
    new_of: dict[int, tuple[str, bool, str]] = {}
    for pid, cands in per_product.items():
        name, ver, src = pick_name(cands)
        new_of[pid] = (name, ver, src)
        m = meta[pid]
        # ★ 分组键必须和数据库的唯一键**逐字一致**：
        #   UNIQUE(brand_id, model_key, category_code)，而 model_key 是去空格小写。
        #   用 name.lower() 分组会让 "Redmi Note 15" 与 "Redmi Note15" 分成两组，
        #   但它们的 model_key 都是 "redminote15" —— 改名时第二组直接撞 UNIQUE，
        #   整个事务回滚一行都改不动（第二次踩这个坑了）。
        groups[(m["brand_id"], m["cat"], name.lower().replace(" ", ""))].append(pid)

    changed = [p for p, (n, _, _) in new_of.items()
               if n.strip().lower() != (meta[p]["old"] or "").strip().lower()]
    merges = {k: v for k, v in groups.items() if len(v) > 1}
    verified_n = sum(1 for v in new_of.values() if v[1])

    print(f"友商产品（有挂牌可重算的）  {len(new_of)}")
    print(f"  型号名会变               {len(changed)}")
    print(f"  其中**联网查证过**       {verified_n}  ({verified_n / max(len(new_of),1)*100:.1f}%)")
    print(f"  会合并的组               {len(merges)} 组 / 涉及 "
          f"{sum(len(v) for v in merges.values())} 行 → {len(merges)} 行")
    print(f"  判定为配件（全部标题都是）{len(accessory_only)}")
    print(f"  合并+去配件后剩余         "
          f"{len(groups)} 个产品（原 {len(new_of) + len(accessory_only)}）")

    print("\n改名样例：")
    for pid in changed[:20]:
        n, ver, src = new_of[pid]
        print(f"  {'✔' if ver else ' '} {str(meta[pid]['old'])[:30]:32} → {n[:30]:32} {src}")

    print("\n合并样例：")
    for k, v in list(merges.items())[:6]:
        print(f"  → {new_of[v[0]][0]!r}")
        for pid in v:
            print(f"       #{pid:5} {meta[pid]['old']!r}")

    if not APPLY:
        print("\n[试运行] 没有改动任何数据。确认后加 --apply 执行。")
        return 0

    moved = Counter()
    with db.tx() as conn:
        for k, pids in groups.items():
            name, ver, src = new_of[pids[0]]
            key = k[2]
            # 保留"已经叫这个名字"的那行，否则改名时会撞 UNIQUE
            # （brand_id, model_key, category_code）——教训见 merge_polluted_models.py
            exact = [p for p in pids
                     if (meta[p]["old"] or "").lower().replace(" ", "") == key]
            keep = min(exact) if exact else min(pids)
            dead = [p for p in pids if p != keep]
            # ★ 还要防**组外**撞车：库里可能有一个没有任何挂牌的产品
            #   （因此不在 per_product 里）正好已经占着这个 model_key。
            #   它不会被改名也不会被合并，但会让 keep 改名时撞 UNIQUE。
            outsider = db.q1("""SELECT id FROM rival_product
                                WHERE brand_id=? AND category_code=? AND model_key=?
                                  AND id<>?""", (k[0], k[1], key, keep))
            if outsider:
                dead.append(keep)          # 让位给已占键的那行
                keep = outsider["id"]
                dead = [p for p in dead if p != keep]
            if dead:
                qs = ",".join("?" * len(dead))
                for t in REF_TABLES:
                    cur = conn.execute(
                        f"UPDATE OR IGNORE {t} SET rival_product_id=? "
                        f"WHERE rival_product_id IN ({qs})", (keep, *dead))
                    moved[t] += cur.rowcount
                for t in REF_TABLES:
                    conn.execute(
                        f"DELETE FROM {t} WHERE rival_product_id IN ({qs})", dead)
                conn.execute(f"DELETE FROM rival_product WHERE id IN ({qs})", dead)
            conn.execute("""UPDATE rival_product
                            SET model_name=?, model_key=?, name_verified=?,
                                name_source=?, updated_at=datetime('now')
                            WHERE id=?""",
                         (name, key, 1 if ver else 0,
                          f"nubimetrics/{src}", keep))

        # 全部标题都是配件的产品：挂牌保留（价格仍有用），但不再算成"竞品机型"
        if accessory_only:
            qs = ",".join("?" * len(accessory_only))
            ids = list(accessory_only)
            conn.execute(
                f"UPDATE price_obs SET rival_product_id=NULL, product_kind='accessory' "
                f"WHERE rival_product_id IN ({qs})", ids)
            for t in REF_TABLES:
                if t == "price_obs":
                    continue
                conn.execute(f"DELETE FROM {t} WHERE rival_product_id IN ({qs})", ids)
            conn.execute(f"DELETE FROM rival_product WHERE id IN ({qs})", ids)

    print("\n[已执行] 外键搬迁：")
    for t, n in moved.items():
        if n:
            print(f"  {t:22} {n} 行")
    print(f"  友商产品现有 {db.q1('SELECT COUNT(*) c FROM rival_product')['c']} 个")
    print(f"  其中查证过的 "
          f"{db.q1('SELECT COUNT(*) c FROM rival_product WHERE name_verified=1')['c']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
