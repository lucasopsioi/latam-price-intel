# -*- coding: utf-8 -*-
"""按观测日期补跑采集后处理：清洗(挂接) → 品类判定 → 价格审计 → 价格变动检测。

为什么需要它（2026-09-04）：
  09-03 那轮流水线跑在 09-01 启动的旧进程里，清洗阶段撞 IntegrityError 被整个带走，
  当天 19,030 条观测只挂上 24%（秘鲁 0%），审计与变动检测全在脏数据上跑。
  流水线的后处理是按 run_id 取 obs_dates 的，事后没有入口对某一天单独补跑 —— 这就是入口。

与 orchestrator 的阶段顺序、构造参数逐字一致（品类判定必须在审计之前；变动检测必须在审计之后）。
★ 不要在采集进行中跑：与采集进程抢库锁，采集侧写失败会丢观测。脚本会先查 /api/health。

跑法： python tools\postprocess_date.py 2026-09-03 [2026-09-04 ...] [--force] [--skip-audit]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

sys.path.insert(0, ".")
from app import config, db  # noqa: E402


def _collecting() -> bool | None:
    try:
        h = json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=5))
        return bool(h.get("detail", {}).get("task_running"))
    except Exception:  # noqa: BLE001
        return None


def _rate(d: str) -> tuple[int, int, int]:
    r = db.q1("""SELECT COUNT(*) n, SUM(rival_product_id IS NOT NULL) l,
                        SUM(audit_status='pending') p
                 FROM price_obs po JOIN brand b ON b.id=po.brand_id
                 WHERE b.is_ours=0 AND po.product_kind='device' AND po.obs_date=?""", (d,))
    return r["n"] or 0, r["l"] or 0, r["p"] or 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dates", nargs="+")
    ap.add_argument("--force", action="store_true", help="采集进行中也跑（不建议）")
    ap.add_argument("--skip-audit", action="store_true")
    a = ap.parse_args()

    busy = _collecting()
    if busy and not a.force:
        print("! 采集正在进行（/api/health task_running=true），补跑会与它抢库锁，退出。"
              "等空闲再跑，或 --force。")
        return 2
    if busy is None:
        print("  (服务不在线，直接对库跑)")

    from app.agents import LLMClient
    from app.agents.categorizer import CategoryAgent
    from app.agents.cleaner import CleanerAgent
    from app.agents.price_audit import PriceAuditAgent
    from app.agents.pricemove import PriceMoveAgent

    cfg = config.load_runtime()
    llm = LLMClient(cfg["agents"])
    ag_cfg = cfg["agents"]

    before = {d: _rate(d) for d in a.dates}
    for d in a.dates:
        n, l, p = before[d]
        print(f"[{d}] 补跑前：整机观测 {n} 条，挂接 {l} ({l / n:.0%})，审计待定 {p}" if n
              else f"[{d}] 没有观测，跳过")

    # 顺序与 orchestrator 一致
    for d in a.dates:
        if not before[d][0]:
            continue
        r = CleanerAgent(llm, ag_cfg).run(obs_date=d) or {}
        print(f"[{d}] 清洗：{ {k: v for k, v in r.items() if isinstance(v, (int, float))} }")
    r = CategoryAgent(llm, ag_cfg).run(rematch=False) or {}
    print(f"品类判定：{ {k: v for k, v in r.items() if isinstance(v, (int, float))} }")
    if not a.skip_audit:
        # ★ 按日审到取尽（run_all），不是 .run() 的"今天前 2000 行"
        for d in a.dates:
            if not before[d][0]:
                continue
            r = PriceAuditAgent(llm, ag_cfg).run_all(obs_date=d) or {}
            print(f"[{d}] 价格审计：{ {k: v for k, v in r.items() if isinstance(v, (int, float))} }")
            if r.get("warning"):
                print("  ! 审计告警：", r["warning"])
            if r.get("pending_left"):
                print(f"  ! 未取尽，仍有 {r['pending_left']} 条 pending")
    for d in a.dates:
        if not before[d][0]:
            continue
        r = PriceMoveAgent(llm, ag_cfg).run(obs_date=d) or {}
        print(f"[{d}] 价格变动检测：{ {k: v for k, v in r.items() if isinstance(v, (int, float))} }")

    print()
    for d in a.dates:
        n, l, p = _rate(d)
        n0, l0, p0 = before[d]
        if n:
            print(f"[{d}] 补跑后：挂接 {l0}→{l} ({l / n:.0%})，审计待定 {p0}→{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
