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

import re
import sys
from collections import defaultdict
from pathlib import Path

# ★ 档位词：出现在型号尾部时是**机型区分**不是噪声。
#   Redmi Note 15 Pro 与 Redmi Note 15 Pro Plus 是两款机器、两个价位段。
_TIER_RE = re.compile(
    r"\b(plus|pro|max|ultra|lite|mini|fe|neo|turbo|power|prime|air)\b", re.I)

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


def _settle(conn, x) -> int:
    """给一行落最终名；撞 UNIQUE 就回落原 key。返回是否真的改名了（1/0）。

    ★ 两阶段改名的第二阶段。撞车时**必须**把原 key 还回去 —— 阶段1 已经把它
      挪成临时值了，不还就等于留下一个假 key。
    """
    cur = conn.execute("UPDATE OR IGNORE rival_product "
                       "SET model_name=?, model_key=? WHERE id=?",
                       (x["new_name"], x["new_key"], x["id"]))
    if cur.rowcount:
        return 1
    conn.execute("UPDATE OR IGNORE rival_product SET model_key=? WHERE id=?",
                 (x["model_key"], x["id"]))
    return 0


def main() -> int:
    import json

    brands = {b["id"]: b for b in db.q("SELECT id, name, aliases FROM brand")}
    rows = db.q("""SELECT id, brand_id, category_code, model_name, model_key
                   FROM rival_product ORDER BY id""")

    renamed, groups = [], defaultdict(list)
    multi_name = []   # 逗号分隔的多机型垃圾名，单列报告不合并
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
        # ★ 逗号名不参与合并：真机型名不含逗号。这些是情报 Agent 从新闻标题
        #   建出来的多机型垃圾产品（"AirPods Pro 3, AirTag 2, iPhone 17 Pro"），
        #   全部 0 观测。留在组里会用它们的档位词把**本该合并**的真产品一起拦掉
        #   （实测多拦了 iPhone 17 Pro / AirPods / iPhone Air 三组）。
        if "," in (r["model_name"] or ""):
            multi_name.append(r)
            continue
        groups[(r["brand_id"], r["category_code"], new_key)].append(
            {**r, "new_name": new_name, "new_key": new_key})

    # ★★ 档位守卫（2026-09-07）：归一化会把 "Redmi Note 15 Pro Plus" 洗成
    #   "Redmi Note 15 Pro"，于是它和真正的 Pro 落进同一组被合并 —— 那是**两款
    #   不同的机器、两个价位段**，合并等于制造假价差（知识页原话：S26 与 S26+
    #   合并实测出现 88% 的假价差，同一病根第二次出现，只是这次载体是 "Plus" 词）。
    #   规则：同组成员的**原始**型号名里出现的档位词集合必须一致，否则整组拒绝合并
    #   （改名照做，只是不并行）。失败方向是"少并一组"，不是"把两款机器并掉"。
    #   实测：469 组里 13 组档位不一致，其中 4 组是真损失
    #   （Redmi Note 15/14/13/17 Pro Plus 共 128 条观测），其余 9 组是
    #   逗号分隔的多机型垃圾产品（"MacBook Air, MacBook Pro, AirPods Max"，0 观测）。
    def _tiers(name: str) -> tuple:
        return tuple(sorted(w.lower() for w in _TIER_RE.findall(name or "")))

    blocked = []
    merges = {}
    for k, v in groups.items():
        if len(v) <= 1:
            continue
        if len({_tiers(x["model_name"]) for x in v}) > 1:
            blocked.append((k, v))
            continue
        merges[k] = v

    print(f"友商产品 {len(rows)} 个")
    print(f"  型号名需要重写: {len(renamed)}")
    print(f"  会合并的组: {len(merges)} 组，涉及 "
          f"{sum(len(v) for v in merges.values())} 行 → 合并后 {len(merges)} 行")
    print(f"  合并后总产品数: {len(groups) + sum(len(v) - 1 for _, v in blocked)}"
          f"（减少 {sum(len(v) - 1 for v in merges.values())}）")
    if multi_name:
        n = 0
        for r in multi_name:
            n += db.q1("SELECT COUNT(*) c FROM price_obs WHERE rival_product_id=?",
                       (r["id"],))["c"]
        print(f"\n★ 逗号名（多机型垃圾产品，情报 Agent 从新闻标题建的）："
              f"{len(multi_name)} 个，共 {n} 条观测 —— 不参与合并，需单独清理")
        for r in multi_name[:6]:
            print(f"       #{r['id']:5} {r['model_name']!r}")
    if blocked:
        n_obs = 0
        print(f"\n★ 档位守卫拦下 {len(blocked)} 组（档位词不一致 = 不同机型，只改名不合并）：")
        for k, v in sorted(blocked, key=lambda x: -len(x[1]))[:12]:
            print(f"  ✋ {v[0]['new_name']!r}")
            for x in v:
                c = db.q1("SELECT COUNT(*) c FROM price_obs WHERE rival_product_id=?",
                          (x["id"],))["c"]
                n_obs += c
                print(f"       #{x['id']:5} obs={c:<5} {x['model_name']!r}")
        print(f"  以上涉及观测 {n_obs} 条 —— 合并它们会把两个价位段混算。")

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
        # ★★ 两阶段改名（2026-09-07 加）：直接改 model_key 会在**中间态**撞
        #   UNIQUE(brand_id, model_key, category_code) —— 合并组的保留行想改成 key X 时，
        #   另一个还没轮到处理的行正占着 X，整个事务当场回滚、一行都写不进去
        #   （实测就是这么炸的：POCO X8 Pro 那组）。
        #   先把**所有要改名的行**的 key 挪到不可能撞车的临时值，腾空目标 key，
        #   再做合并与最终改名。临时值带 id 保证唯一。
        need_rename = [x for v in groups.values() for x in v
                       if x["new_key"] != (x["model_key"] or "")]
        for x in need_rename:
            conn.execute("UPDATE rival_product SET model_key=? WHERE id=?",
                         (f"~tmp~{x['id']}", x["id"]))
        print(f"[阶段1] 腾空 {len(need_rename)} 个 model_key（临时值，下一阶段落最终名）")
        # ★★ 必须遍历 merges 而不是 groups —— groups 里含被档位守卫拦下的组，
        #   照 groups 走等于闸门只在打印时生效、真跑时被绕过（"闸门写了没接线"）。
        #   被拦下的组仍要**改名**（改名是安全的），只是不合并：所以下面对
        #   blocked 里的每一行单独改自己的名字，不动外键、不删行。
        for k, v in merges.items():
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

            keeper = next(x for x in v if x["id"] == keep)
            _settle(conn, keeper)

        # 只有一个成员的组：照常改名（这是本工具的主体，3,619 条改名的大头）
        for k, v in groups.items():
            if len(v) != 1:
                continue
            _settle(conn, v[0])

        # 被档位守卫拦下的组：**逐行**改自己的名字，不合并、不搬外键。
        # 撞 UNIQUE 时回落原名（宁可留个脏名字，也不能把两款机器并掉）。
        # ★★ 但改名本身也不许抹掉档位词：归一化会把 "Redmi Note 17 Pro Max"
        #   洗成 "Redmi Note 17 Pro"，于是库里出现两行同名（一行真是 Max），
        #   界面上分不出来 —— 守卫挡住了合并，却被改名从后门绕过去了。
        #   这一组里凡是改名会改变档位词集合的，一律保持原名。
        n_blocked_renamed = 0
        for k, v in blocked:
            for x in v:
                if _tiers(x["new_name"]) != _tiers(x["model_name"]):
                    conn.execute("UPDATE OR IGNORE rival_product SET model_key=? WHERE id=?",
                                 (x["model_key"], x["id"]))
                    continue
                n_blocked_renamed += _settle(conn, x)

        # ★ 收尾兜底：阶段1 腾空过 key 的行，凡是没能落上最终名的（撞 UNIQUE 被 IGNORE），
        #   必须把原 key 还回去 —— 否则它会永远带着 "~tmp~123" 这个假 key，
        #   而假 key 不报错、不变色，只会让这台机器从此再也匹配不上任何东西。
        left = conn.execute(
            "SELECT id FROM rival_product WHERE model_key LIKE '~tmp~%'").fetchall()
        for (rid,) in left:
            orig = next((x for v in groups.values() for x in v if x["id"] == rid), None)
            if orig:
                conn.execute("UPDATE OR IGNORE rival_product SET model_key=? WHERE id=?",
                             (orig["model_key"], rid))
        still = conn.execute(
            "SELECT COUNT(*) FROM rival_product WHERE model_key LIKE '~tmp~%'").fetchone()[0]
        print(f"[阶段3] 未能落最终名而回退原名：{len(left)} 行；仍带临时 key：{still} 行（必须为 0）")
        if still:
            raise RuntimeError(f"{still} 行卡在临时 key，事务回滚 —— 不能让假 key 落库")

    print(f"\n[已执行] 档位守卫拦下的 {len(blocked)} 组：只改名 {n_blocked_renamed} 行，"
          f"未合并、未搬外键")
    print("[已执行] 外键搬迁：")
    for t, n in moved.items():
        if n:
            print(f"  {t:22} {n} 行")
    print(f"  rival_product 现有 {db.q1('SELECT COUNT(*) c FROM rival_product')['c']} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
