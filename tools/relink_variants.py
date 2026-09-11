# -*- coding: utf-8 -*-
"""把「变体机型被挂进基础款」的观测重挂到正确产品上。

2026-09-07 发现：清洗只处理 rival_product_id IS NULL 的行，**一条观测挂错就
永远不会被复核**（归一化器后来修好了也没用）。实测已挂接+整机+审计通过的
81,277 条非桶观测里，11,167 条（13.7%）的型号身份与标题不符，集中在一种形态：
带档位词的变体被并进基础款 —— 'Redmi A7 Pro' 挂进 'Redmi A7'、'X5b Plus' 挂进
'X5b'、'Magic 8 Lite' 挂进 'Magic 8'、'moto g100 pro' 挂进 'Moto G100'。
后果与「Lenovo Tab 是个 164 标题的桶」同族：两个价位段混在一条曲线上。

★ 只处理**无歧义**的那一档：标题归一化出的 key 在同品牌同品类下**已经有**
  一个产品，且那个产品不是当前挂的这个。这种重挂没有猜的成分。
  归一化出的 key 没有对应产品的（要新建产品）**不在本工具范围**——
  新建产品会引出「这是不是真机型」的判断，留给人看。

默认干跑。--apply 前会拦采集（复用 audit_backlog.collecting_now）。
回滚清单先落盘再改库。

    python tools\relink_variants.py            # 干跑
    python tools\relink_variants.py --apply
    python tools\relink_variants.py --rollback data\backfill\relink_<stamp>.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.agents.cleaner import CleanerAgent  # noqa: E402

ROLLBACK_DIR = ROOT / "data" / "backfill"

# 档位词与型号码：型号身份的两个信号（与 tools/merge_polluted_models.py 同一判据）
TIER = re.compile(r"\b(plus|pro|max|ultra|lite|mini|fe|neo|turbo|power|prime|air)\b", re.I)
CODE = re.compile(r"\b[a-z]{1,3}\d{1,4}[a-z]?\b", re.I)


def tiers(s):
    return tuple(sorted(w.lower() for w in TIER.findall(s or "")))


def codes(s):
    return tuple(sorted(w.lower() for w in CODE.findall(s or "")))


def fit(name: str, title_tiers: set) -> int:
    """产品名与**原始标题**的档位契合度：命中的减去多余的。

    ★★ 这一条是本工具的命门。第一版拿 `normalize_model(title)` 的输出当真相，
      而归一化**会剥掉档位词** —— 于是干跑当场要把 85 条本来挂对的
      「Redmi Note 15 Pro Plus」搬进「Redmi Note 15 Pro」，即帮倒忙。
      （同一个坑今晚第三次：merge_polluted_models 也是被它咬的。）
      正确的真相来源是**原始标题**：标题里写着 Plus，就不许搬到没有 Plus 的产品上。
      只有目标比当前更贴合标题才搬，否则不动 —— 失败方向是"不搬"，不是"搬错"。
    """
    t = set(tiers(name))
    return len(t & title_tiers) - len(t - title_tiers)


def _gate(force: bool) -> None:
    """采集进行中拒跑 —— rival_product_id 是采集端正在写的同一列。"""
    if force:
        return
    try:
        from tools.audit_backlog import collecting_now
    except Exception:  # noqa: BLE001
        sys.path.insert(0, str(ROOT / "tools"))
        from audit_backlog import collecting_now  # type: ignore
    busy, why = collecting_now()
    if busy:
        print(f"! 采集进行中（{why}），重挂会与它抢同一列，退出。等空闲再跑，或 --force。")
        raise SystemExit(2)


def scan() -> tuple[list, Counter]:
    brands = {b["id"]: b for b in db.q("SELECT id, name, aliases FROM brand")}
    # 同品牌同品类下 key → 产品 id（重挂的目标只能从已有产品里选）
    bykey = {}
    for p in db.q("""SELECT id, brand_id, category_code, model_key, model_name,
                            COALESCE(is_bucket,0) is_bucket FROM rival_product"""):
        bykey[(p["brand_id"], p["category_code"], p["model_key"] or "")] = p

    rows = db.q("""SELECT po.id, po.title, po.brand_id, po.category_code,
                          po.rival_product_id, po.country_code cc, po.sale_price, po.currency,
                          rp.model_key, rp.model_name, COALESCE(rp.is_bucket,0) is_bucket,
                          COALESCE(b.name,'') brand
                   FROM price_obs po
                   JOIN rival_product rp ON rp.id = po.rival_product_id
                   JOIN brand b ON b.id = po.brand_id
                   WHERE po.product_kind='device' AND b.is_ours=0""")

    alias = {}
    plan, stats = [], Counter()
    for r in rows:
        stats["扫描"] += 1
        if r["is_bucket"]:
            stats["桶（不动）"] += 1
            continue
        bid = r["brand_id"]
        if bid not in alias:
            try:
                alias[bid] = json.loads((brands.get(bid) or {}).get("aliases") or "[]")
            except Exception:  # noqa: BLE001
                alias[bid] = []
        new = CleanerAgent.normalize_model(r["title"] or "", alias[bid],
                                           (brands.get(bid) or {}).get("name") or "")
        nk = (new or "").lower().strip()
        if not nk or len(nk) < 3:
            stats["归一化无结论"] += 1
            continue
        mk = r["model_key"] or ""
        if tiers(nk) == tiers(mk) and codes(nk) == codes(mk):
            stats["身份一致"] += 1
            continue
        tgt = bykey.get((bid, r["category_code"], nk))
        if tgt is None:
            stats["★身份不符但目标产品不存在（需人看，本工具不建产品）"] += 1
            continue
        if tgt["id"] == r["rival_product_id"]:
            stats["身份一致"] += 1
            continue
        if tgt["is_bucket"]:
            stats["目标是桶（不动）"] += 1
            continue
        # ★ 档位契合度闸：以**原始标题**为真相，目标必须比当前更贴合才搬。
        tt = set(tiers(r["title"]))
        if fit(tgt["model_name"], tt) <= fit(r["model_name"], tt):
            stats["★目标不比当前更贴合标题（不动，防把 Pro Plus 搬进 Pro）"] += 1
            continue
        stats["可重挂"] += 1
        plan.append({"obs": r["id"], "from": r["rival_product_id"], "to": tgt["id"],
                     "from_name": r["model_name"], "to_name": tgt["model_name"],
                     "brand": r["brand"], "cc": r["cc"], "title": r["title"]})
    return plan, stats


def main() -> int:
    argv = sys.argv[1:]
    if "--rollback" in argv:
        p = Path(argv[argv.index("--rollback") + 1])
        items = json.loads(p.read_text(encoding="utf-8"))
        _gate("--force" in argv)
        n = 0
        with db.tx() as c:
            for it in items:
                # 守卫：只回滚**仍带本次写入值**的行，别人后写的不覆盖
                n += c.execute("UPDATE price_obs SET rival_product_id=? "
                               "WHERE id=? AND rival_product_id=?",
                               (it["from"], it["obs"], it["to"])).rowcount or 0
        print(f"已回滚 {n}/{len(items)} 条")
        return 0

    plan, stats = scan()
    print("统计：")
    for k, v in stats.most_common():
        print(f"  {k:<44} {v:>7}")

    by_pair = defaultdict(list)
    for it in plan:
        by_pair[(it["brand"], it["from_name"], it["to_name"])].append(it)
    print(f"\n可重挂 {len(plan)} 条，涉及 {len(by_pair)} 组（挂在 → 应挂）：")
    for (br, fn, tn), items in sorted(by_pair.items(), key=lambda x: -len(x[1]))[:25]:
        print(f"  x{len(items):<4} {br} 「{fn[:26]}」→「{tn[:26]}」")
        print(f"        例：{(items[0]['title'] or '')[:72]}")

    if "--apply" not in argv:
        print("\n[试运行] 没有改动任何数据。确认后加 --apply。")
        return 0

    _gate("--force" in argv)
    ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
    stamp = db.q1("SELECT strftime('%Y%m%d-%H%M%f','now') s")["s"].replace(".", "-")
    rb = ROLLBACK_DIR / f"relink_{stamp}.json"
    rb.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")  # 先落盘再改库
    print(f"\n回滚清单已写：{rb}")

    n = 0
    with db.tx() as c:
        for it in plan:
            n += c.execute("UPDATE price_obs SET rival_product_id=? "
                           "WHERE id=? AND rival_product_id=?",
                           (it["to"], it["obs"], it["from"])).rowcount or 0
    print(f"✓ 已重挂 {n} 条（计划 {len(plan)}）")
    print("★ 下一步：rival_product_id 是竞品匹配/价格变动的输入，必须重算 ——")
    print("   python -c \"from app.matching.matcher import CompetitorMatcher; "
          "print(CompetitorMatcher().rebuild_all())\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
