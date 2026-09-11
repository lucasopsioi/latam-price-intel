# -*- coding: utf-8 -*-
"""夜间规格补全（2026-09-03 用户批准："排上吧"）。

为什么做成"每晚跑一次、跑不完明晚接着跑"而不是一次跑完：
  GSMArena 是 **IP 级限流**。9/03 那轮 1417 次请求（6 秒间隔）把额度打爆，
  之后连 Selenium 都拿不到页（换浏览器无用，证明不是指纹问题）。
  所以这里主动放慢到 20 秒，且遇连续限流就收工（GsmArenaSource.give_up），
  剩下的留给明晚。已取到的规格都已落库，重跑只补差集 —— 天然可续。

为什么用 Python 而不是 PowerShell：
  ★ 本机 cp936 + PS 5.1 的组合在这个脚本上连踩三坑：可插值 here-string 吃掉
    Python 代码里的 $ 与引号、Add-Content -Encoding utf8 仍写乱码、
    try 块内的 exit 不终止脚本。同样的逻辑用 Python 写没有任何一个。

三条纪律：
  1. 采集进行中就让路（与 tools/restart.ps1 同一条）
  2. 先探一次限流状态，还在限流就直接收工，不白撞
  3. 规格变了要刷新推断 —— 撤销失效的 → 重推 → 苹果回填
     （规格补全会改产品 ROM，之前按旧 ROM 推的 RAM 会悬空）
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = r"C:\Python314\python.exe"
LOG = ROOT / "logs" / "nightly_specs.log"
PROBE_URL = "https://www.gsmarena.com/samsung_galaxy_a17_5g-14041.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def say(msg: str) -> None:
    line = f"[{datetime.now():%m-%d %H:%M}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(args: list, tag: str) -> int:
    """跑子进程并把输出转进日志。用 UTF8 环境，避免 cp936 咬中文。"""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    for ln in (out + err).splitlines():
        if ln.strip():
            say(f"  {tag}| {ln[:160]}")
    return p.returncode


def collection_busy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health",
                                    timeout=8) as r:
            import json
            h = json.loads(r.read().decode("utf-8"))
        d = h.get("detail") or {}
        if d.get("task_running"):
            say(f"采集进行中（{d.get('task_name')}），本轮跳过")
            return True
    except Exception:                                   # noqa: BLE001
        say("服务未响应，仍继续（补规格只读价格库，不依赖服务）")
    return False


def rate_limited() -> bool:
    """先探一次。还在限流就别开跑 —— 反复撞 429 只会延长封禁。"""
    try:
        import httpx
        r = httpx.get(PROBE_URL, timeout=25,
                      headers={"User-Agent": UA,
                               "Accept-Language": "en-US,en;q=0.9"})
        if r.status_code == 200:
            say("限流已解除，开始补规格（20 秒/次）")
            return False
        say(f"GSMArena 仍在限流（HTTP {r.status_code}），本轮跳过，明晚再试")
        return True
    except Exception as e:                              # noqa: BLE001
        say(f"探测失败（{str(e)[:60]}），本轮跳过")
        return True


REFRESH = """
import sys
sys.path.insert(0, '.')
from app import config, db
from app.agents import LLMClient, SpecFillerAgent
cfg = config.load_runtime()['agents']
ag = SpecFillerAgent(LLMClient(cfg), cfg)
print('撤销失效推断:', ag._revoke_stale_inferences())
print('同变体重推  :', ag._infer_same_variant())
print('苹果回填    :', ag._fill_apple())
r = db.q1("SELECT COUNT(*) n, SUM(ram_gb IS NOT NULL) ram FROM price_obs "
          "WHERE category_code IN ('phone','tablet') AND product_kind='device'")
print('RAM 覆盖: %.1f%%' % (r['ram'] / r['n'] * 100))
"""


def main() -> int:
    if collection_busy():
        return 0
    if rate_limited():
        return 0
    run([PY, "tools/fetch_specs.py", "--apply", "--delay", "20"], "取规格")
    say("刷新规格推断（规格变了，旧推断的前提可能已失效）")
    run([PY, "-c", REFRESH], "推断  ")
    say("本轮完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
