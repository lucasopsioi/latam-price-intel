# -*- coding: utf-8 -*-
"""给 rival_product 打「桶」标记（is_bucket / bucket_reason）—— 默认干跑。

背景（2026-09-04 用户截图）：#2292「Lenovo Tab」是 158 种标题的桶（Tab P11 / Tab Pro /
M10 / 钢化膜全在一起），价格曲线在 18 万与 8,500 CLP 之间来回跳。桶不是一款产品，
不能进单品消费方（趋势入口、竞品候选池、变动检测）。

判据只有一份实现：app/skunorm.py `bucket_verdict`（cleaner 入库时也用它）：
  ① 权威表系列级 SKU 且不是**系列基础款**（skunorm.is_bucket_sku）      → sku_rules:generic
  ② nubimetrics 兜底名（前 4 词）且不含数字/变体词                  → nubimetrics:fallback
  ③ 白牌桶                                                            → whitelabel
  ④ **数据判据只出报告不落标**（--data-report）：某币种 n>=8、剔掉 <20% 中位价的点后
     P90/P10 > 2.5，且标题重归一化出 >=2 个占比 >=5% 的 SKU。

用法：
    python tools/mark_buckets.py                  # 干跑：会打标的产品清单
    python tools/mark_buckets.py --data-report    # 附带数据判据报告（慢一点，逐标题重归一化）
    python tools/mark_buckets.py --apply          # 真的写（先落回滚清单）
    python tools/mark_buckets.py --rollback <回滚文件>
    选项：--force（明知采集在跑也要写，不建议）

★ 干跑不写库、不跑迁移：列不存在时照样算清单，只在 --apply 时要求列已存在
  （先跑一次 `python -c "from app import db; db.init_db()"` 应用迁移）。
★★ --apply / --rollback 两条写库路径都过采集闸（`_gate()`：/api/health task_running +
  scrape_run status='running' 双保险，与 tools/audit_backlog.py 同一份实现，
  任一为真则 exit 2）。以前这条只写在文档里、代码里没有。

★★ 两条**可持久的人工纠正通道** —— 判据把真单品判成桶时，改库是没用的：
  1. 判据错在**某个权威 SKU**（"Redmi Pad" 其实是一款在售机型）：
     在 config/sku_rules.yaml 该规则上加一行 `generic: false`，或加进
     skunorm._BASE_MODEL_SKUS。两条都被 bucket_verdict 认。
  2. 判据错在**某一个产品行**：把 bucket_reason 写成 `manual:` 开头，本工具就不再动它 ——
       UPDATE rival_product SET is_bucket=0, bucket_reason='manual:真单品，2026-09-07 人工核过'
             WHERE id=<id>;
     人工打标同理（is_bucket=1 + `manual:` 理由），本工具也不会把它摘掉。
     没有这条通道时：人工把 is_bucket 改回 0 → 下次 --apply 按判据又打回 1，
     **误标不可纠正**；而误标一个真单品会把它永久赶出趋势入口/竞品候选池/曲线下拉，
     没有任何人会发现（漏标一个桶反而还有 --data-report 兜着）。
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # cp936 控制台保命
except Exception:  # noqa: BLE001
    pass

from app import db, skunorm                     # noqa: E402

APPLY = "--apply" in sys.argv
DATA_REPORT = "--data-report" in sys.argv
FORCE = "--force" in sys.argv
ROLLBACK_DIR = ROOT / "data" / "backfill"

# ★ 人工裁定的标记前缀。bucket_reason 以它开头 = 有人看过这一行并表了态，
#   判据不许再翻它（两个方向都不许：不许把人工摘的标打回 1，也不许把人工打的标摘掉）。
MANUAL_PREFIX = "manual:"

# 数据判据参数（与上游诊断 scratchpad/bucket_classify.py 同口径）
MIN_N = 8            # 某币种至少 8 个点
TRIM_BELOW = 0.20    # 剔掉 < 20% 中位价的点（配件低尾）
RATIO_GATE = 2.5     # 剔完后 P90/P10 仍 > 2.5
SHARE_GATE = 0.05    # 重归一化出的 SKU 占比 >= 5% 才算"一款"


def _arg(name: str, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _has_cols() -> bool:
    cols = {r["name"] for r in db.q("PRAGMA table_info(rival_product)")}
    return "is_bucket" in cols and "bucket_reason" in cols


def is_manual(reason: str | None) -> bool:
    """这一行的桶标记是不是人工裁定的（判据不许再翻）。"""
    return str(reason or "").startswith(MANUAL_PREFIX)


# ---------------------------------------------------------------- 采集闸

def _collecting_now() -> tuple[bool, str]:
    """采集是否在进行。单一实现在 tools/audit_backlog.collecting_now
    （/api/health task_running + scrape_run status='running' 双保险），
    这里只转发。测试替换这个函数来演一次"采集中"。"""
    from tools.audit_backlog import collecting_now  # noqa: PLC0415
    return collecting_now()


def _gate(action: str) -> None:
    """写库前的采集闸。查不出状态时按「不确定即拒跑」处理。"""
    if FORCE:
        print(f"[--force] 跳过采集闸，{action} 继续")
        return
    try:
        busy, why = _collecting_now()
    except Exception as e:  # noqa: BLE001
        print(f"✗ 采集闸检查失败（{type(e).__name__}: {str(e)[:100]}）。"
              f"不确定就不写库；确认采集空闲后加 --force。")
        sys.exit(2)
    if busy:
        print(f"✗ 采集正在进行（{why}），{action} 会与它抢库锁，退出。等采集结束再跑，或 --force。")
        sys.exit(2)
    if why:
        print(f"  （采集闸：{why}）")


def label(brand: str | None, model: str | None) -> str:
    """展示用：型号名已含品牌前缀时不重复（"Lenovo Lenovo Tab" → "Lenovo Tab"）。"""
    b, m = (brand or "").strip(), (model or "").strip()
    return m if not b or m.lower().startswith(b.lower()) else f"{b} {m}"


def scan() -> tuple[list[dict], list[dict]]:
    """返回 (会打标的产品, 全部产品)。"""
    ready = _has_cols()
    extra = ", rp.is_bucket, rp.bucket_reason" if ready else \
            ", 0 AS is_bucket, NULL AS bucket_reason"
    rows = db.q(f"""
        SELECT rp.id, rp.brand_id, b.name AS brand, rp.model_name, rp.model_key,
               rp.category_code, rp.name_source, rp.name_verified{extra},
               (SELECT COUNT(*) FROM price_obs po WHERE po.rival_product_id = rp.id) AS obs,
               (SELECT COUNT(DISTINCT po.title) FROM price_obs po
                 WHERE po.rival_product_id = rp.id) AS titles
        FROM rival_product rp JOIN brand b ON b.id = rp.brand_id
        ORDER BY rp.id
    """)
    hits = []
    for r in rows:
        is_b, why = skunorm.bucket_verdict(r["model_name"], r["name_source"], brand=r["brand"])
        if is_b:
            d = dict(r)
            d["_reason"] = why
            hits.append(d)
    return hits, rows


def _changes_and_stale(hits: list[dict], rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(要打标的, 要摘标的, 人工裁定过因而不动的)。干跑与 --apply 走同一份，
    否则"干跑说会改 N 行、真跑改了 M 行"这种差异没人会发现。"""
    manual = [r for r in rows if is_manual(r["bucket_reason"])]
    manual_ids = {r["id"] for r in manual}
    changes = [h for h in hits if h["id"] not in manual_ids
               and not (h["is_bucket"] and h["bucket_reason"] == h["_reason"])]
    stale = [r for r in rows if r["is_bucket"] and r["id"] not in manual_ids
             and not skunorm.bucket_verdict(r["model_name"], r["name_source"], brand=r["brand"])[0]]
    return changes, stale, manual


def report(hits: list[dict], rows: list[dict]) -> None:
    gen = skunorm.generic_skus()
    buckets = tuple(s for s in gen if skunorm.is_bucket_sku(s))
    base = tuple(s for s in gen if not skunorm.is_bucket_sku(s))
    print(f"权威表系列级 SKU（结构判据，{len(gen)} 个），其中判为桶的 {len(buckets)} 个：")
    print("  " + " | ".join(buckets))
    if base:
        # 系列基础款：结构上像系列，但自己就是一款在售机型 —— 细分照跑、桶标不打。
        print(f"  系列基础款（不打桶标，skunorm._BASE_MODEL_SKUS / yaml generic:false）："
              + " | ".join(base))
    print(f"\n产品总数 {len(rows)}，会打标 {len(hits)} 个，"
          f"涉及观测 {sum(h['obs'] for h in hits):,} 条")
    by_reason = Counter(h["_reason"] for h in hits)
    for k, v in by_reason.most_common():
        obs = sum(h["obs"] for h in hits if h["_reason"] == k)
        print(f"  {k:<22} {v:>4} 个产品 / {obs:>7,} 条观测")
    already = [h for h in hits if h["is_bucket"]]
    changes, stale, manual = _changes_and_stale(hits, rows)
    print(f"  已标记（不变）{len(already)} 个；库里已标但判据不再认的 {len(stale)} 个"
          f"{'（--apply 会摘标）' if stale else ''}")
    print(f"  --apply 实际会改 {len(changes)} 行（打标）+ {len(stale)} 行（摘标）；"
          f"人工裁定（bucket_reason 以 {MANUAL_PREFIX!r} 开头）{len(manual)} 行，一律不动")

    print("\n会打标的产品（按观测量）：")
    print(f"  {'id':>5} | {'obs':>6} | {'标题数':>4} | {'判据':<20} | {'来源':<26} | 品牌 型号")
    for h in sorted(hits, key=lambda x: -x["obs"])[:int(_arg("--limit", 80))]:
        print(f"  {h['id']:>5} | {h['obs']:>6} | {h['titles']:>4} | {h['_reason']:<20} | "
              f"{str(h['name_source'] or 'NULL'):<26} | {label(h['brand'], h['model_name'])}")
    if stale:
        print("\n库里已标 is_bucket=1 但当前判据不认（--apply 会改回 0）：")
        for r in stale[:20]:
            print(f"  #{r['id']} {label(r['brand'], r['model_name'])} ({r['bucket_reason']})")
    if manual:
        print("\n人工裁定过，判据不许再翻（--apply 跳过）：")
        for r in manual[:20]:
            print(f"  #{r['id']} {label(r['brand'], r['model_name'])} "
                  f"is_bucket={r['is_bucket']} ({r['bucket_reason']})")


# ---------------------------------------------------------------- ④ 数据判据（只报告）

def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def data_report(hits: list[dict]) -> list[dict]:
    """价差 + 标题重归一化：谁**像**桶但没被 ①②③ 抓到。只打印，不落标。"""
    from app.agents.cleaner import identity_for  # noqa: PLC0415

    flagged_ids = {h["id"] for h in hits}
    rows = db.q("""
        SELECT po.rival_product_id AS pid, po.currency, po.sale_price
        FROM price_obs po
        WHERE po.rival_product_id IS NOT NULL AND po.sale_price IS NOT NULL
          AND po.product_kind <> 'accessory' AND po.audit_status <> 'rejected'
          AND po.is_bundle = 0 AND po.condition = 'new'
    """)
    prices: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        prices[(r["pid"], r["currency"])].append(float(r["sale_price"]))

    cands: dict[int, list[tuple]] = defaultdict(list)
    for (pid, cur), vals in prices.items():
        if len(vals) < MIN_N:
            continue
        vals.sort()
        med = statistics.median(vals)
        core = [v for v in vals if v >= med * TRIM_BELOW]
        if len(core) < MIN_N:
            continue
        p10, p90 = _pct(core, 0.10), _pct(core, 0.90)
        ratio = (p90 / p10) if p10 else float("inf")
        raw = (vals[-1] / vals[0]) if vals[0] else float("inf")
        if ratio > RATIO_GATE:
            cands[pid].append((cur, len(vals), raw, ratio, len(vals) - len(core)))

    out = []
    if not cands:
        print("\n数据判据：没有产品在剔低尾后 P90/P10 > 2.5")
        return out
    meta = {r["id"]: r for r in db.q(f"""
        SELECT rp.id, b.name AS brand, rp.model_name, rp.name_source, rp.category_code
        FROM rival_product rp JOIN brand b ON b.id = rp.brand_id
        WHERE rp.id IN ({','.join('?' * len(cands))})""", list(cands))}
    for pid, cur_stats in cands.items():
        titles = db.q("""
            SELECT po.title, po.sku_code, po.category_code, b.name AS brand_name,
                   b.aliases, COUNT(*) AS n
            FROM price_obs po JOIN brand b ON b.id = po.brand_id
            WHERE po.rival_product_id = ? AND po.product_kind <> 'accessory'
            GROUP BY po.title, po.sku_code, po.category_code""", (pid,))
        names: Counter = Counter()
        total = 0
        for t in titles:
            ident = identity_for(dict(t))
            if ident["kind"] != "device":
                continue
            names[ident["model"]] += t["n"]
            total += t["n"]
        majors = [(n, c) for n, c in names.most_common() if total and c / total >= SHARE_GATE]
        if len(majors) >= 2:
            m = meta[pid]
            out.append({"id": pid, "brand": m["brand"], "model": m["model_name"],
                        "already_flagged": pid in flagged_ids,
                        "currencies": cur_stats, "majors": majors[:6]})
    out.sort(key=lambda x: -max(c[3] for c in x["currencies"]))
    print(f"\n数据判据（只报告，不落标）：价差 >2.5 且标题重归一化出 >=2 款的产品 {len(out)} 个"
          f"（其中 {sum(1 for o in out if o['already_flagged'])} 个已被 ①②③ 抓到）")
    for o in out[:int(_arg("--limit", 80))]:
        tag = "已标" if o["already_flagged"] else "未标"
        cur = "; ".join(f"{c} n={n} raw={raw:.1f}x trim={ratio:.2f}x 低尾{lt}"
                        for c, n, raw, ratio, lt in o["currencies"])
        print(f"  [{tag}] #{o['id']} {label(o['brand'], o['model'])} | {cur}")
        print("        重归一化 → " + ", ".join(f"{n}×{c}" for n, c in o["majors"]))
    return out


# ---------------------------------------------------------------- 写库

def apply(hits: list[dict], rows: list[dict]) -> None:
    if not _has_cols():
        print("✗ rival_product 还没有 is_bucket 列。先应用迁移："
              '  python -c "from app import db; db.init_db()"  （采集结束后再跑）')
        sys.exit(2)
    _gate("--apply")
    # ★ 与干跑同一份计算，且 manual: 行两个方向都不动（见模块 docstring 的纠正通道）
    changes, stale, manual = _changes_and_stale(hits, rows)
    if manual:
        print(f"  跳过 {len(manual)} 行人工裁定：{', '.join('#%s' % r['id'] for r in manual[:10])}"
              + ("…" if len(manual) > 10 else ""))
    if not changes and not stale:
        print("没有需要改动的行。")
        return
    ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = ROLLBACK_DIR / f"mark_buckets_{stamp}.json"
    # ★ 回滚清单先落盘再改库，存的是**改之前的值**
    path.write_text(json.dumps({
        "kind": "mark_buckets", "at": stamp,
        "rows": [{"id": r["id"], "is_bucket": int(r["is_bucket"] or 0),
                  "bucket_reason": r["bucket_reason"]} for r in changes + stale],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    with db.tx() as conn:
        for h in changes:
            conn.execute("""UPDATE rival_product SET is_bucket=1, bucket_reason=?,
                            updated_at=datetime('now') WHERE id=?""", (h["_reason"], h["id"]))
        for r in stale:
            conn.execute("""UPDATE rival_product SET is_bucket=0, bucket_reason=NULL,
                            updated_at=datetime('now') WHERE id=?""", (r["id"],))
    print(f"[已执行] 打标 {len(changes)} 个，摘标 {len(stale)} 个；回滚清单 {path}")


def rollback(path: str) -> None:
    _gate("--rollback")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    with db.tx() as conn:
        for r in data["rows"]:
            conn.execute("UPDATE rival_product SET is_bucket=?, bucket_reason=? WHERE id=?",
                         (r["is_bucket"], r["bucket_reason"], r["id"]))
    print(f"[已回滚] {len(data['rows'])} 行")


def main() -> int:
    if "--rollback" in sys.argv:
        rollback(_arg("--rollback", ""))
        return 0
    hits, rows = scan()
    report(hits, rows)
    if DATA_REPORT:
        data_report(hits)
    if not APPLY:
        print("\n[试运行] 没有改动任何数据。确认后加 --apply 执行（需先应用迁移）。")
        return 0
    apply(hits, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
