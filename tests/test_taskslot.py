# -*- coding: utf-8 -*-
"""单任务槽的僵死接管（2026-09-07）。

事故：2026-09-04 12:30 那轮采集挂住没结束，而任务槽是**进程内的内存标志**，
只有服务重启才会清 —— 于是 09-05、09-06 两天的定时采集全被
「已有任务在运行，本次跳过」挡掉，**连续三天没有新数据**，
而界面上 `task_running: true` 看起来一切正常（"卡死"与"正常在跑"长得一样）。
再叠加 09-07 服务死在 07:30 之前没人拉起，一共丢了四天。

守三条：
  ① 有上限：超过 TASK_SLOT_MAX_HOURS 的占用视为僵死，可被接管；
  ② 上限要高于正常采集时长（实测 17~19 小时），否则会打断正在正常干活的采集；
  ③ 接管必须**留痕告警**并把 scrape_run 的 running 收成 interrupted ——
     静默接管等于把"采集卡死"这件事一并抹掉，下次照犯且照样没人知道。

跑法： python tests\test_taskslot.py
"""
import ast
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


SRC = (ROOT / "app/api/server.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

print("== 上限存在且合理 ==")
consts = {}
for n in TREE.body:
    if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) \
            and isinstance(n.value, ast.Constant):
        consts[n.targets[0].id] = n.value.value
cap = consts.get("TASK_SLOT_MAX_HOURS")
ok(cap is not None, "★ 必须有 TASK_SLOT_MAX_HOURS —— 没有上限的槽会被僵死任务永久占住")
ok(isinstance(cap, (int, float)) and cap >= 24,
   f"★ 上限要高于正常采集时长（实测 17~19 小时），否则会打断正常采集。实得 {cap}")
ok(isinstance(cap, (int, float)) and cap <= 48,
   f"★ 上限也不能太大 —— 48 小时以上等于两天没数据才发现。实得 {cap}")

print("== 接管逻辑在 acquire_task_slot 里，且比较的是 started_at ==")
fn = next((n for n in ast.walk(TREE)
           if isinstance(n, ast.FunctionDef) and n.name == "acquire_task_slot"), None)
ok(fn is not None, "找得到 acquire_task_slot")
body = ast.dump(fn) if fn else ""
ok("TASK_SLOT_MAX_HOURS" in body,
   "★ 上限必须在 acquire_task_slot 里真被消费 —— 定义了不用就是个装饰")
ok("started_at" in body,
   "★ 判据必须是槽的 started_at，不是别的时间源")

print("== 接管要留痕 + 收尾 scrape_run ==")
seg = SRC[SRC.index("def acquire_task_slot"):]
seg = seg[:seg.index("def release_task_slot")]
ok("log.error" in seg,
   "★ 接管必须 log.error 告警 —— 静默接管会把「采集卡死」这件事一并抹掉")
ok("interrupted" in seg and "scrape_run" in seg,
   "★ 接管时要把 scrape_run 里 status='running' 的收成 interrupted，"
   "否则库里永远悬着一批假的『正在跑』")
ok("db.tx()" in seg,
   "★ 收尾必须走 db.tx() —— db.q1 只 execute 不 commit，UPDATE 会静默丢失")

print("== 未超时的槽仍然拒绝（不能把闸门放开）==")
# 反向自检：把上限比较去掉，断言必须变红
_mut = SRC.replace("if held < TASK_SLOT_MAX_HOURS * 3600:", "if False:")
ok(_mut != SRC, "（自检）源码里确实有那处比较")


def _has_cap_compare(src: str) -> bool:
    """acquire_task_slot 里是否真的**拿上限做了比较**。

    ★ 不能只查名字在不在：去掉比较之后，TASK_SLOT_MAX_HOURS 仍然出现在
      告警文案的格式化参数里 —— 那正是「断言被自己的日志文案咬住」，
      与 assertions-that-verify-nothing 同族，第一版就是这么写错的。
    """
    f = next((n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "acquire_task_slot"), None)
    if f is None:
        return False
    for n in ast.walk(f):
        if isinstance(n, ast.Compare) and "TASK_SLOT_MAX_HOURS" in ast.dump(n):
            return True
    return False


ok(_has_cap_compare(SRC), "★ 上限必须真被拿去做比较，不能只出现在日志文案里")
ok(not _has_cap_compare(_mut),
   "★ 反向自检：去掉那处比较后断言会红（说明它守的是真性质，不是恒真）")

print("== 计划任务的自愈触发器 ==")
ps1 = ROOT / "tools/add_watchdog_trigger.ps1"
ok(ps1.exists(), "★ 要有加自愈触发器的脚本 —— 服务与守护一起死时只靠登录触发器永远起不来")
if ps1.exists():
    p = ps1.read_text(encoding="utf-8", errors="replace")
    ok("RepetitionInterval" in p, "触发器要带重复间隔")
    ok("IgnoreNew" in p,
       "★ 注释里要写明依赖 MultipleInstances=IgnoreNew —— 否则重复触发会起出第二个守护")
    ok(all(ord(c) < 128 for c in p),
       "★ .ps1 必须纯 ASCII（本机 ANSI=cp936，含中文的 .ps1 过 shell 管道极易损坏）")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
