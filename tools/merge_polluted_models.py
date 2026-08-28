# -*- coding: utf-8 -*-
"""把被卖家名/渠道名污染的友商型号重新归一化，并合并因此裂开的重复产品。

★ 为什么需要这个脚本：
  归一化修好了，只影响**以后**抓的数据。库里已经躺着 579 个
  "iPhone 14 Por Kiss" / "Galaxy S26 Por FALABELLA" 这样的产品行 ——
  同一台机器按卖家裂成了好几个"产品"。不合并的话：
    · 同 SKU 比价永远对不上（价格变动检测对这些机型直接失效）
    · 看板上的"覆盖机型数"虚高
    · 竞品匹配拿着一个残缺型号去比规格

  修 bug 不等于收拾干净它留下的痕迹 —— 脏数据会继续喂给 Agent 出错误结论。

用法：
    python tools/merge_polluted_models.py          # 只看要做什么（默认）
    python tools/merge_polluted_models.py --apply  # 真的执行
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.agents.cleaner import CleanerAgent  # noqa: E402

# 所有引用 rival_product_id 的表（合并时要把外键一起搬过去）
REF_TABLES = ["price_obs", "competitor_match", "launch_event", "product_page_cache",
              "review", "review_profile", "voc_insight", "price_move",
              "strategy_signal"]

APPLY = "--apply" in sys.argv


def _pick_keep(members: list[dict]) -> int:
    """选保留哪一行。

    ★ 优先选**已经叫这个名字**的那行，而不是无脑取最小 id：
      组里若已有一行的 model_key 正好等于目标 key，却因为 id 较大没被选中，
      那么把另一行改成同一个 key 时会直接撞 UNIQUE(brand,key,category)
      —— 整个事务回滚，一行都改不动。
      顺带这样也保住了那行已经攒下的规格与历史。
    """
    exact = [m["id"] for m in members if m["model_key"] == m["new_key"]]
    return min(exact) if exact else min(m["id"] for m in members)


def main() -> int:
    import json

    brands = {b["id"]: b for b in db.q("SELECT id, name, aliases FROM brand")}
    rows = db.q("""SELECT id, brand_id, category_code, model_name, model_key
                   FROM rival_product ORDER BY id""")

    renamed, groups = [], defaultdict(list)
    for r in rows:
        b = brands.get(r["brand_id"]) or {}
        try:
            aliases = json.loads(b.get("aliases") or "[]")
        except Exception:  # noqa: BLE001
            aliases = []
        new_name = CleanerAgent.normalize_model(
            r["model_name"] or "", aliases, b.get("name") or "")
        # 归一化不出东西就别动它 —— 宁可留着脏名字，也不能把产品变成无名氏
        if not new_name:
            new_name = r["model_name"]
        new_key = (new_name or "").lower().strip()
        if new_name != r["model_name"]:
            renamed.append((r["id"], r["model_name"], new_name))
        groups[(r["brand_id"], r["category_code"], new_key)].append(
            {**r, "new_name": new_name, "new_key": new_key})

    merges = {k: v for k, v in groups.items() if len(v) > 1}

    print(f"友商产品 {len(rows)} 个")
    print(f"  型号名需要重写: {len(renamed)}")
    print(f"  会合并的组: {len(merges)} 组，涉及 "
          f"{sum(len(v) for v in merges.values())} 行 → 合并后 {len(merges)} 行")
    print(f"  合并后总产品数: {len(groups)}（减少 {len(rows) - len(groups)}）")

    print("\n改名样例：")
    for _id, old, new in renamed[:12]:
        print(f"  #{_id:5} {old!r} → {new!r}")

    print("\n合并样例（保留“已经叫这个名字”的那行，否则保留最小 id）：")
    for k, v in list(merges.items())[:8]:
        keep = _pick_keep(v)
        print(f"  → {v[0]['new_name']!r}  保留 #{keep}，"
              f"并入 {[x['id'] for x in v if x['id'] != keep]}")
        for x in v:
            print(f"       #{x['id']:5} {x['model_name']!r}")

    if not APPLY:
        print("\n[试运行] 没有改动任何数据。确认无误后加 --apply 执行。")
        return 0

    moved = defaultdict(int)
    with db.tx() as conn:
        for k, v in groups.items():
            keep = _pick_keep(v)
            dead = [x["id"] for x in v if x["id"] != keep]

            # ★ 顺序：先搬外键、先删重复行，**最后**才给 keep 改名。
            #   反过来做的话，keep 改名的瞬间组里那行同名的还没删，
            #   直接撞 UNIQUE 把整个事务回滚。
            if dead:
                qs = ",".join("?" * len(dead))
                for t in REF_TABLES:
                    cur = conn.execute(
                        f"UPDATE OR IGNORE {t} SET rival_product_id=? "
                        f"WHERE rival_product_id IN ({qs})", (keep, *dead))
                    moved[t] += cur.rowcount
                # UPDATE OR IGNORE 会因唯一约束丢下一些行（同一产品同期两条洞察），
                # 这些行合并后本就是重复的，直接删掉，不能留成孤儿
                for t in REF_TABLES:
                    conn.execute(f"DELETE FROM {t} WHERE rival_product_id IN ({qs})",
                                 tuple(dead))
                conn.execute(f"DELETE FROM rival_product WHERE id IN ({qs})", tuple(dead))

            conn.execute("UPDATE rival_product SET model_name=?, model_key=? WHERE id=?",
                         (v[0]["new_name"], v[0]["new_key"], keep))

    print("\n[已执行] 外键搬迁：")
    for t, n in moved.items():
        if n:
            print(f"  {t:22} {n} 行")
    print(f"  rival_product 现有 {db.q1('SELECT COUNT(*) c FROM rival_product')['c']} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
