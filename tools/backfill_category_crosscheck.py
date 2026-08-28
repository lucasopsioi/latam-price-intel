# -*- coding: utf-8 -*-
"""历史回填：品类来自「采集上下文」而非商品本身 → 用标题证据交叉校验后改判。

背景（2026-08-27）：price_obs.category_code 存的是**当时在抓哪个品类页**
（collector._persist 直接把采集单元的 category 写进去），不是商品本身的品类。
搜索串味、品类页混排、渠道把耳机塞进平板页 —— 真设备就落错桶。
实测平板桶里躺着 339 MXN 的「XIAOMI Audífonos Buds 6 Play」、
68,000 COP 的「Reloj Inteligente Smartwatch T900」。
★ 注意分类器判 product_kind='device' 是**对的**（它确实是台设备），错的只有品类 ——
  所以 backfill_accessory_kind.py 那条线永远碰不到它们，是两个独立的缺陷。

★ 判定规则不在本文件里 —— 用的是 `extract.crosscheck_category`，
  与采集端将来要接的同一份实现。分开写就会出现"规则改好了、回填用的是另一套"，
  而两套规则的差集永远不会有人发现。

处置（口径由用户 2026-08-27 定）：
    fix     → category_code 改成标题证据指向的品类（**含 is_bundle=1**：
              捆绑装的品类按打头商品算；价格分析本来就用 is_bundle=0 挡着，
              改它是为了让按品类统计的产品数/覆盖度变准）
    pending → category_code 置 NULL。所有按品类的查询都是 `category_code = ?`，
              NULL 自动落选 ⇒ 零改动就排除出该品类的价格分析。
              为什么不加一列 category_status：那要在 8 处价格查询各加一个条件
              （boards/dashboard/trends/matcher/weekly/strategy/pricemove/price_audit），
              为 27 行付这个改动面不划算。改前值全在回滚清单里。

用法：
    python tools/backfill_category_crosscheck.py              # 干跑：统计 + 抽样，不改库
    python tools/backfill_category_crosscheck.py --sample 40
    python tools/backfill_category_crosscheck.py --category tablet   # 只看某个桶
    python tools/backfill_category_crosscheck.py --apply      # 真的改（先写回滚清单）
    python tools/backfill_category_crosscheck.py --rollback <回滚文件>

★ 采集正在跑时也能安全执行：独立进程 + WAL 库，只做一次短事务的 UPDATE，
  不需要停服务（tools/restart.ps1 有闸，别去动它）。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, skunorm                    # noqa: E402
from app.scraping import extract               # noqa: E402

APPLY = "--apply" in sys.argv
ROLLBACK_DIR = ROOT / "data" / "backfill"


def _arg(name: str, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def scan(only_cat: str | None = None) -> tuple[list[dict], int]:
    rows = db.q("""
        SELECT po.id, po.obs_date, po.country_code, po.category_code, po.title,
               po.sale_price, po.currency, po.is_bundle, po.rival_product_id,
               b.name AS brand, c.name AS channel, rp.model_name
        FROM price_obs po
        LEFT JOIN brand b         ON b.id  = po.brand_id
        LEFT JOIN channel c       ON c.id  = po.channel_id
        LEFT JOIN rival_product rp ON rp.id = po.rival_product_id
        WHERE po.product_kind = 'device' AND po.category_code IS NOT NULL
    """)
    hits = []
    for r in rows:
        if only_cat and r["category_code"] != only_cat:
            continue
        verdict, target, why = extract.crosscheck_category(r["title"], r["category_code"])
        if verdict == "ok":
            continue
        d = dict(r)
        d["_verdict"], d["_target"], d["_why"] = verdict, target, why
        hits.append(d)
    return hits, len(rows)


def report(hits: list[dict], total: int, n_sample: int) -> None:
    fixes = [h for h in hits if h["_verdict"] == "fix"]
    pend = [h for h in hits if h["_verdict"] == "pending"]
    print(f"\n受影响行数：{len(hits)}  "
          f"（占 product_kind='device' 且有品类的 {len(hits) / max(total, 1) * 100:.2f}%，"
          f"分母 {total}）")
    print(f"  改判 fix    ：{len(fixes)}"
          f"（其中 is_bundle=1 {sum(1 for h in fixes if h['is_bundle'])}）")
    print(f"  待定 pending：{len(pend)}  → category_code 置 NULL")

    print("\n—— 改判方向（原品类 = 采集上下文 → 标题证据指向的品类）——")
    for (a, b), n in Counter((h["category_code"], h["_target"])
                             for h in fixes).most_common():
        print(f"   {a:9s} → {b:9s} {n:6d}")

    for label, key in (("国家", "country_code"), ("渠道", "channel"), ("品牌", "brand")):
        c = Counter(str(h[key] or "—") for h in hits)
        if c:
            print(f"  按{label}： " + "  ".join(f"{k}:{v}" for k, v in c.most_common(8)))
    dates = sorted(h["obs_date"] for h in hits if h["obs_date"])
    if dates:
        print(f"  日期跨度： {dates[0]} → {dates[-1]}")

    # ★★ 独立信号交叉验证 —— 本报告里最该看的一段。
    #   skunorm.guess_category 读的是**归一化后的型号名**，与本规则读的原始标题
    #   是两条独立的路，它的赞成/反对票才是规则可信度的体温计。
    #   ★ 刻意**不用** rival_product.category_code 做这件事：它是从
    #     price_obs.category_code 抄过去的（cleaner.py:467），拿它对账是自己验自己。
    print("\n—— ★ 独立信号复核：skunorm.guess_category(型号名) 对每条改判的裁决 ——")
    agree = Counter()
    dissent = []
    for h in fixes:
        g = skunorm.guess_category(h["model_name"] or "")
        if not g:
            agree["型号名判不出（多数：型号名本身就是有损输出）"] += 1
        elif g == h["_target"]:
            agree["★ 支持改判"] += 1
        else:
            agree["✗ 反对改判"] += 1
            dissent.append((h, g))
    for k, v in agree.most_common():
        print(f"   {k:34s} {v:6d}")
    if dissent:
        print(f"\n   —— 反对票 {len(dissent)} 条，按型号名聚类（**逐簇人查**，"
              f"分歧是成簇的，不是随机的）——")
        for (name, g), n in Counter((d[0]["model_name"], d[1])
                                    for d in dissent).most_common(12):
            ex = next(d[0] for d in dissent if d[0]["model_name"] == name)
            print(f"    {n:4d}  型号名={name!r} 判 {g}，标题判 {ex['_target']}")
            print(f"          {ex['title'][:96]}")

    # ★ 抽样必须逐条打印原标题 + 判定依据 + 价格：
    #   规则的正确性无法从聚合数字验证，只能从个案证伪。
    # ★★ 必须**按方向 + 按币种分组各取价格两端**：
    #   跨币种排序会退化成比币种（COP 的数字天然比 BRL 大三个量级）；
    #   而误判在数据里的形态就是**价格离群** —— 一台被判成耳机的手机会在
    #   音频桶里贵得离谱。低价端只能证明"脏东西抓到了"，高价端才是体检位。
    #   （这条是 backfill_accessory_kind.py 用血换来的，照搬。）
    print(f"\n—— 抽样：各改判方向、各币种的价格两端 ——")
    by_dir: dict[tuple, list] = defaultdict(list)
    for h in fixes:
        by_dir[(h["category_code"], h["_target"])].append(h)
    per_end = max(n_sample // 20, 2)
    for d, v in sorted(by_dir.items(), key=lambda x: -len(x[1])):
        cur = Counter(x["currency"] for x in v).most_common(1)[0][0]
        vs = sorted([x for x in v if x["currency"] == cur and x["sale_price"]],
                    key=lambda x: x["sale_price"])
        if not vs:
            continue
        print(f"\n  【{d[0]} → {d[1]}】{len(v)} 条；下面是 {cur} 计价的 {len(vs)} 条的两端")
        for tag, sel in (("低", vs[:per_end]), ("高", vs[-per_end:])):
            for x in sel:
                print(f"    {tag} {x['sale_price']:>12,.0f} [{x['id']}] "
                      f"{x['country_code']}/{x['channel'] or '—'} "
                      f"bundle={x['is_bundle']}")
                print(f"        {x['title'][:100]}")
                print(f"        → {x['_why']}")

    if pend:
        print(f"\n—— 待定 {len(pend)} 条（**全部列出**，量小，人查后可手工定夺）——")
        for h in pend:
            price = f"{h['sale_price']:,.0f} {h['currency']}" if h["sale_price"] else "—"
            print(f"    [{h['id']}] {h['category_code']} {price} {h['country_code']}")
            print(f"        {h['title'][:100]}")
            print(f"        → {h['_why']}")

    price_impact(fixes, pend)


def price_impact(fixes: list[dict], pend: list[dict]) -> None:
    """本次回填对各品类价格下沿的影响 —— 这才是验收的标尺。

    ★ 只按「同品类同币种」比，绝不跨币种合并（同 dashboard 的口径）。
    ★ 报 P5 而不只报最小值：最小值是单点，容易被一条脏数据带跑；
      P5 才反映"下沿"这件事本身。
    """
    drop = defaultdict(set)
    for h in fixes + pend:
        drop[h["category_code"]].add(h["id"])
    print(f"\n—— ★ 验收标尺：各品类价格下沿的变化（device/非捆绑/新品/未拒绝）——")
    print(f"   {'品类':9s} {'币种':5s} {'剔除/总数':>12s} "
          f"{'最低 前→后':>26s} {'P5 前→后':>26s} {'中位 前→后':>26s}")
    for cat in ("phone", "tablet", "audio", "wearable", "pc"):
        for cur in ("MXN", "COP", "CLP", "BRL", "PEN", "ARS"):
            base = db.q("""SELECT id, sale_price FROM price_obs
                           WHERE category_code=? AND product_kind='device'
                             AND is_bundle=0 AND condition='new'
                             AND audit_status<>'rejected' AND currency=?
                             AND sale_price IS NOT NULL""", (cat, cur))
            if len(base) < 20:
                continue
            a = sorted(x["sale_price"] for x in base)
            b = sorted(x["sale_price"] for x in base if x["id"] not in drop[cat])
            if not b or len(a) == len(b):
                continue

            def q(z, p):
                return z[min(int(len(z) * p), len(z) - 1)]

            print(f"   {cat:9s} {cur:5s} {len(a)-len(b):5d}/{len(a):<6d} "
                  f"{a[0]:>11,.0f} →{b[0]:>11,.0f}  "
                  f"{q(a,.05):>11,.0f} →{q(b,.05):>11,.0f}  "
                  f"{q(a,.5):>11,.0f} →{q(b,.5):>11,.0f}")


def apply_(hits: list[dict]) -> None:
    ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = ROLLBACK_DIR / f"category_crosscheck_{stamp}.json"
    # ★ 同秒内跑第二次 --apply 会撞上同一个文件名，直接覆盖 ——
    #   而被覆盖的那份正是**第一次改动**的唯一还原依据，覆盖掉就再也回不去了。
    #   （测试里连跑三次 apply 时抓到的。）
    n = 1
    while path.exists():
        path = ROLLBACK_DIR / f"category_crosscheck_{stamp}-{n}.json"
        n += 1
    # ★ 回滚清单先落盘再改库：反过来的话进程中途挂掉就再也回不去了。
    #   存的是**改之前的值**，不是"改成什么"。
    path.write_text(json.dumps(
        {"created": stamp, "note": "price_obs.category_code 采集上下文 vs 标题证据 交叉校验",
         "rows": [{"id": h["id"], "old": h["category_code"],
                   "new": h["_target"], "verdict": h["_verdict"],
                   "why": h["_why"], "title": h["title"]} for h in hits]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n回滚清单已写：{path}")

    # 按 (旧值, 新值) 分组批量改，一个短事务
    groups: dict[tuple, list[int]] = defaultdict(list)
    for h in hits:
        groups[(h["category_code"], h["_target"])].append(h["id"])
    n = 0
    with db.tx() as conn:
        for (old, new), ids in groups.items():
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                # ★ WHERE 带上 category_code=? ：采集可能正在并发写，
                #   只改仍处于改前状态的行，避免覆盖别人刚写的值。
                conn.execute(
                    f"UPDATE price_obs SET category_code=? "
                    f"WHERE id IN ({','.join('?' * len(chunk))}) AND category_code=?",
                    [new, *chunk, old])
                n += len(chunk)
    left = 0
    for h in hits:
        row = db.q1("SELECT category_code c FROM price_obs WHERE id=?", (h["id"],))
        if row and row["c"] == h["category_code"] and h["category_code"] is not None:
            left += 1
    print(f"✓ 已更新 {n} 行；复查仍是改前品类的：{left}（应为 0）")
    print(f"  回滚： python tools/backfill_category_crosscheck.py --rollback {path}")


def rollback(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    with db.tx() as conn:
        for r in rows:
            conn.execute("UPDATE price_obs SET category_code=? WHERE id=?",
                         (r["old"], r["id"]))
    print(f"✓ 已回滚 {len(rows)} 行到改前的品类")
    return 0


def main() -> int:
    rb = _arg("--rollback", None)
    if rb:
        return rollback(rb)

    only = _arg("--category", None)
    hits, total = scan(only)
    report(hits, total, int(_arg("--sample", 40)))

    if not hits:
        print("\n没有需要回填的行。")
        return 0
    if not APPLY:
        print("\n[干跑] 未改动任何数据。确认抽样与反对票无误后执行：")
        print("       python tools/backfill_category_crosscheck.py --apply")
        return 0
    apply_(hits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
