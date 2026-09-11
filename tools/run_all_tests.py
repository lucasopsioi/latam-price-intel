# -*- coding: utf-8 -*-
"""全量跑 tests/test_*.py —— 每个测试**独立进程**，跑完汇总。

为什么需要它（2026-09-05 事故）：
  上一轮给 extract._DEVICE_WORDS 补了 `watch gt|fit|ultra`，只跑了 test_accessory_kind
  与 test_extract 两个"看起来相关"的文件，绿了就交付。真正被打破的是 test_acmestore
  （35 通过 / 1 失败）—— Acme商城的配件兜底只在 product_kind=='unknown' 时才跑，
  而那次改动把 'WATCH GT 5 Correa' 从 unknown 变成了 device，兜底永远不触发。
  ⇒ **改词表/正则类共享规则，必须跑全量，不许自选子集。** 自选子集这件事只能靠工具挡，
     靠纪律挡已经失败过一次。

这里的测试不是 pytest：每个 tests/test_*.py 是独立脚本，自己 print 结果、
失败时 sys.exit(1)。所以判据是**退出码**，摘要行只是给人看的。

★ 生产库指纹（默认开）：跑之前与跑之后各查一次 data/intel.db 的关键计数
  （price_obs 总数 / 按 audit_status / 按 product_kind、scrape_run、agent_run、rival_product）。
  对不上就当场报警并让退出码非 0 —— 「测试悄悄改了生产库」属于典型的静默故障，
  没有这层对账，它和"什么都没发生"长得一样。
  已知例外：tests/test_phonesync.py 会往 phone_export_sync 插一行再删掉
  （它不改 config.DB_PATH），那张表**不在**指纹里，因为它是同步台账不是情报数据。

跑法：
    python tools\\run_all_tests.py                 # 全量
    python tools\\run_all_tests.py --only test_accessory_kind test_acmestore
    python tools\\run_all_tests.py --list          # 只列出会跑哪些
    python tools\\run_all_tests.py --no-fingerprint
    python tools\\run_all_tests.py --timeout 1800

退出码：0 = 全绿且指纹未变；1 = 有测试失败或指纹变了。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config                                   # noqa: E402

# 摘要行的两种写法（项目里两代测试脚本各用一种），只用来给人看，判据仍是退出码
_SUMMARY_RES = [
    re.compile(r"结果[:：]\s*(\d+)\s*通过[,，]\s*(\d+)\s*失败"),
    re.compile(r"(\d+)\s*pass\s*/\s*(\d+)\s*fail"),
]
# 生产库指纹查询：只读、只 COUNT，不碰任何一行
_FINGERPRINT_SQL = {
    "price_obs": "SELECT COUNT(*) FROM price_obs",
    "price_obs.audit": "SELECT audit_status, COUNT(*) FROM price_obs GROUP BY 1 ORDER BY 1",
    "price_obs.kind": "SELECT product_kind, COUNT(*) FROM price_obs GROUP BY 1 ORDER BY 1",
    "scrape_run": "SELECT COUNT(*) FROM scrape_run",
    "agent_run": "SELECT COUNT(*) FROM agent_run",
    "rival_product": "SELECT COUNT(*) FROM rival_product",
}


def fingerprint() -> dict | None:
    """生产库关键计数。库不在或读不了 → None（不阻断，只是没这层保险）。"""
    p = Path(config.DB_PATH)
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as e:                            # noqa: BLE001
        print(f"[指纹] 打不开生产库（{e}），本轮无库对账", file=sys.stderr)
        return None
    out: dict = {}
    try:
        for name, sql in _FINGERPRINT_SQL.items():
            try:
                out[name] = [tuple(r) for r in con.execute(sql).fetchall()]
            except sqlite3.Error as e:                    # noqa: BLE001
                out[name] = f"ERR:{e}"
    finally:
        con.close()
    return out


def diff_fingerprint(before: dict | None, after: dict | None) -> list[str]:
    if not before or not after:
        return []
    return [f"  {k}: 前={before.get(k)!r}  后={after.get(k)!r}"
            for k in before if before.get(k) != after.get(k)]


def summarize(text: str) -> str:
    for rx in _SUMMARY_RES:
        m = None
        for m in rx.finditer(text):       # 取最后一次出现（收尾那行）
            pass
        if m:
            return f"{m.group(1)} pass / {m.group(2)} fail"
    return "—"


def run_one(path: Path, timeout: int, env: dict) -> dict:
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, str(path)], cwd=str(ROOT), env=env,
                           capture_output=True, timeout=timeout)
        rc: int | str = p.returncode
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        rc = "TIMEOUT"
        out = (e.stdout or b"").decode("utf-8", "replace")
        err = (e.stderr or b"").decode("utf-8", "replace")
    return {"name": path.name, "rc": rc, "sec": round(time.time() - t0, 1),
            "summary": summarize(out), "out": out, "err": err}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="逐个独立跑 tests/test_*.py 并汇总")
    ap.add_argument("--only", nargs="*", default=None, help="只跑这些（可省 .py）")
    ap.add_argument("--list", action="store_true", help="只列出会跑哪些")
    ap.add_argument("--timeout", type=int, default=1200, help="单个测试超时秒数")
    ap.add_argument("--no-fingerprint", action="store_true", help="不做生产库计数对账")
    ap.add_argument("--verbose", action="store_true", help="失败时打印完整输出而不是尾部")
    args = ap.parse_args(argv)

    tests = sorted((ROOT / "tests").glob("test_*.py"))
    if args.only:
        want = {n[:-3] if n.endswith(".py") else n for n in args.only}
        tests = [t for t in tests if t.stem in want]
        missing = want - {t.stem for t in tests}
        if missing:
            print(f"! 找不到：{sorted(missing)}", file=sys.stderr)
            return 1
    if not tests:
        print("! 没有可跑的测试", file=sys.stderr)
        return 1
    if args.list:
        for t in tests:
            print(t.name)
        return 0

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"                 # 本机 ANSI=cp936，不设它中文测试直接崩
    env["PYTHONIOENCODING"] = "utf-8"

    before = None if args.no_fingerprint else fingerprint()
    print(f"跑 {len(tests)} 个测试（解释器 {sys.executable}）")
    if before is None and not args.no_fingerprint:
        print("[指纹] 生产库不可读，本轮无库对账")

    results = []
    for t in tests:
        r = run_one(t, args.timeout, env)
        results.append(r)
        flag = "OK  " if r["rc"] == 0 else "FAIL"
        print(f"  [{flag}] {r['name']:<34} rc={r['rc']!s:<7} {r['sec']:>6.1f}s  {r['summary']}",
              flush=True)

    bad = [r for r in results if r["rc"] != 0]
    print(f"\n===== 汇总：{len(results) - len(bad)}/{len(results)} 通过 =====")
    for r in bad:
        print(f"\n----- {r['name']}  rc={r['rc']} -----")
        body = (r["out"] + ("\n--- STDERR ---\n" + r["err"] if r["err"].strip() else ""))
        lines = [l for l in body.splitlines() if l.strip()]
        print("\n".join(lines if args.verbose else lines[-30:]))

    after = None if args.no_fingerprint else fingerprint()
    drift = diff_fingerprint(before, after)
    if drift:
        print("\n!!! 生产库指纹变了 —— 有测试写了 data/intel.db：")
        print("\n".join(drift))
    elif before:
        print("\n[指纹] 生产库关键计数前后一致（price_obs / audit_status / product_kind / "
              "scrape_run / agent_run / rival_product）")

    return 1 if (bad or drift) else 0


if __name__ == "__main__":
    sys.exit(main())
