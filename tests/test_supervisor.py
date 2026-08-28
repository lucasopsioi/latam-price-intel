# -*- coding: utf-8 -*-
"""守护进程与开机自启的回归测试。

守的是几条**踩过才知道**的性质：
  1. 服务不能是某个终端会话的子进程（2026-08-15 掉线的真正原因）
  2. 探针必须真查数据库，不能只看端口通不通
  3. 同一时刻只能有一个守护进程
  4. 报警只在状态翻转时发，且失败要退避
  5. 启动脚本的编码纪律（.cmd 纯 ASCII / 中文 .ps1 带 BOM）
"""
import ast
import inspect
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("PYTHONUTF8", "1")

import supervisor as sup                                    # noqa: E402

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ─────────────────── 1. 服务必须是守护的子进程，且能独立存活 ───────────────────
src = inspect.getsource(sup.spawn)
ok("CREATE_NEW_PROCESS_GROUP" in src,
   "子进程要独立成组 —— 否则父进程所在的控制台一关就连坐（这正是掉线原因）")
ok("stdin=subprocess.DEVNULL" in src,
   "stdin 要接空设备：无窗口运行时继承的 stdin 会导致某些库阻塞")
ok("stdout=f" in src and "SRV_LOG" in inspect.getsource(sup),
   "无窗口运行没有控制台，日志必须落文件，否则出事什么都看不到")

# 绝不能按进程名杀 —— 会误伤用户自己的 Chrome / Python
kill_src = inspect.getsource(sup.kill)
ok("/PID" in kill_src and "im" not in kill_src.split("taskkill")[1][:40].lower(),
   "只能按 PID 杀树，不能按映像名（/IM）—— 会误伤用户其它进程")
ok("/T" in kill_src, "要杀整棵树，否则 Chrome 子进程会变孤儿堆积")


# ─────────────────────── 2. 探针必须真查库 ───────────────────────
server_py = (ROOT / "app" / "api" / "server.py").read_text(encoding="utf-8")
tree = ast.parse(server_py)
health_fn = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "health":
        health_fn = node
        break
ok(health_fn is not None, "server.py 必须有 health 端点")
if health_fn:
    body = ast.get_source_segment(server_py, health_fn) or ""
    # ★ 用 AST 取实际代码，不是搜全文 —— 以前两次断言误匹配到自己的注释
    sqls = [n.value for n in ast.walk(health_fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "SELECT" in n.value.upper()]
    ok(sqls, "health 必须真的执行一条查询 —— 端口在依赖就绪前就已监听，"
             "只看端口会在库锁死/磁盘满时一路绿灯而界面全白")
    ok(any("price_obs" in s for s in sqls),
       f"应查业务表 price_obs，实得 {sqls}")
    ok("503" in body, "不健康时要返回 503，200 会让守护误判为正常")


# ─────────────────────── 3. 单例：只能有一个守护 ───────────────────────
lock_src = inspect.getsource(sup.claim_singleton)
ok("bind" in lock_src, "单例要靠占端口实现")

# ★★ 用 AST 查**真正执行的代码**，不能搜全文。
#   这个断言第一版写成 `"SO_REUSEADDR" not in lock_src`，结果匹配到了
#   函数里那句解释"不设 SO_REUSEADDR"的注释，直接误报。
#   同一个错误在本项目已经犯到第三次（archive.py 的 DELETE、brand 的
#   is_ours=0），所以这里固定用 AST：注释不进语法树。
lock_ast = ast.parse(inspect.getsource(sup.claim_singleton).lstrip())
setsockopts = [n for n in ast.walk(lock_ast)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == "setsockopt"]
ok(not setsockopts,
   "★ 绝不能调 setsockopt(SO_REUSEADDR) —— 那样两个守护都能 bind 成功，"
   "单例形同虚设")
ok("lock is None" in inspect.getsource(sup.main),
   "main 必须在抢不到锁时退出，否则计划任务和桌面图标会各起一个守护")

# 实测抢锁语义。★ 不能假设 8764 是空的 —— 真正的守护进程此刻多半正占着它，
#   那种情况下"首次应抢到"必然失败，是测试的错不是代码的错。
#   改成借一个确定空闲的端口来验证语义。
import socket as _sk

_probe = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
_probe.bind(("127.0.0.1", 0))
_free = _probe.getsockname()[1]
_probe.close()

_orig_port = sup.SINGLETON_PORT
try:
    sup.SINGLETON_PORT = _free
    first = sup.claim_singleton()
    ok(first is not None, f"空闲端口 {_free} 上首次应抢到单例锁")
    second = sup.claim_singleton()
    ok(second is None,
       "★ 第二个守护必须抢不到 —— 否则两个守护抢 8765，"
       "输的那个反复重启失败会把 Telegram 刷屏")
    if first:
        first.close()
    third = sup.claim_singleton()
    ok(third is not None, "锁释放后应能重新抢到（内核自动回收，不留陈旧锁）")
    if third:
        third.close()
finally:
    sup.SINGLETON_PORT = _orig_port

# 真实运行中的守护应当正占着锁：抢不到才说明单例真的在生效
live = sup.claim_singleton()
if live is None:
    PASS[0] += 1        # 守护在跑，锁被占 —— 符合预期
else:
    live.close()        # 守护没跑（比如测试环境），不算失败


# ─────────────────────── 4. 报警纪律 ───────────────────────
main_src = inspect.getsource(sup.main)
ok("degraded" in main_src,
   "要有故障态标志：报警只在状态翻转时发，不是每轮都发")
ok("if degraded" in main_src and "已恢复" in main_src,
   "恢复时也要报一次 —— 只报坏不报好，用户不知道啥时候能用了")
ok(len(sup.BACKOFF) >= 3 and sup.BACKOFF == sorted(sup.BACKOFF),
   f"重启退避必须递增，实得 {sup.BACKOFF}")
ok(sup.BACKOFF[-1] >= 300,
   f"退避上限要够大：断网 8 小时不该收到几百条报警，实得 {sup.BACKOFF[-1]}s")

alert_src = inspect.getsource(sup.alert)
ok("except" in alert_src,
   "★ Telegram 发失败不能让守护进程崩 —— 报警通道挂了比服务挂了更隐蔽")
ok("ALERT" in alert_src or "写一个显眼的文件" in alert_src,
   "Telegram 没配时要有能看见的兜底")

# 外网故障不该触发重启（重启治不好断网）
net_src = inspect.getsource(sup.probe_internet)
ok(len(sup.NET_TARGETS) >= 3,
   "外网探针要多个互不相干的目标，只探一个会因那家抽风而误报断网")
ok("不重启" in net_src or "restart" not in net_src.lower(),
   "外网失败只报警不重启")

# 探针间隔要按时间算，不能按轮数 —— wait() 让每轮不再等长
ok("last_net_check" in main_src,
   "★ 外网检查要按时间判：改用 wait() 后一轮不再固定 60 秒，"
   "按轮数算会在进程反复崩时把探针打成高频请求")
ok("proc.wait(timeout=" in main_src,
   "★ 要用 wait 而非 sleep：进程崩溃要秒级发现（实测 60s+ → 8s）")


# ─────────────────── 5. 启动脚本的 cp936 编码纪律 ───────────────────
def raw(p):
    return (ROOT / p).read_bytes()


for f in ["tools/open-dashboard.cmd", "2-start.bat", "看板.cmd"]:
    b = raw(f)
    ok(all(c < 128 for c in b),
       f"{f} 必须纯 ASCII —— cp936 下 .cmd 含非 ASCII 会被 cmd.exe "
       f"从中间劈开且静默失效")

for f in ["tools/install-service.ps1", "tools/alert-popup.ps1"]:
    b = raw(f)
    has_zh = any(c > 127 for c in b)
    if has_zh:
        ok(b[:3] == b"\xef\xbb\xbf",
           f"{f} 含中文，必须带 UTF-8 BOM，否则本机 cp936 会读成乱码")

ps = (ROOT / "tools/install-service.ps1").read_text(encoding="utf-8-sig")
code = [l for l in ps.splitlines() if not l.strip().startswith("#")]
ok(not any("&&" in l for l in code),
   "install-service.ps1 不能用 && —— Windows PowerShell 5.1 会解析报错")
ok(not any("??" in l for l in code), "不能用 ?? —— PS 5.1 不支持")

# 开机触发必须是登录时而非开机时（Selenium 需要用户会话）
ok("AtLogOn" in ps, "必须用 AtLogOn")
ok("AtStartup" not in ps,
   "★ 不能用 AtStartup：那会跑在 SYSTEM/session 0，没有用户桌面和 "
   "Chrome 配置，Selenium 直接废掉；而且注册它需要管理员权限")
ok("ExecutionTimeLimit" in ps and "Zero" in ps,
   "必须去掉执行时长上限，否则计划任务默认 3 天后会杀掉守护进程")
ok("IgnoreNew" in ps, "MultipleInstances 要设 IgnoreNew，防止重复拉起")


# ─────────────── 6. 零窗口 + 解释器解析（用户看到黑窗口那次）───────────────
# 用户截图里的黑窗口 + traceback 有两个独立成因：
#   a) 启动路径经过 .cmd —— .cmd 必然创建控制台窗口（PE subsystem=3）
#   b) 脚本用裸 `python` —— PATH 上第一个 python 是 hermes venv，
#      没有 apscheduler，一 import 就 ModuleNotFoundError
# 两个都要守住。

import struct


def pe_subsystem(path):
    """2=GUI（不分配控制台）  3=CONSOLE（一定弹黑窗）"""
    d = pathlib.Path(path).read_bytes()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    return struct.unpack_from("<H", d, pe + 24 + 68)[0]


ps = (ROOT / "tools/install-service.ps1").read_text(encoding="utf-8-sig")
ok("$sc.TargetPath       = $PyW" in ps,
   "★ 桌面图标必须直接指向 pythonw.exe。指向 .cmd 的话，即使快捷方式设成"
   "最小化，仍会在任务栏闪一下并抢焦点，出错还会把 traceback 糊在黑窗口里")
ok("launcher.py" in ps, "图标要带上无窗口启动器 launcher.py")

_pyw = pathlib.Path(
    r"C:\Python314\pythonw.exe")
if _pyw.exists():
    ok(pe_subsystem(_pyw) == 2,
       "pythonw.exe 必须是 GUI 子系统（subsystem=2），否则照样弹窗")

# 启动器不能靠 print 报错 —— 没有控制台，print 出去的字没人看得见
lau = (ROOT / "tools/launcher.py").read_text(encoding="utf-8")
ok("MessageBoxW" in lau,
   "★ 无控制台环境下必须用消息框报错，print 的内容会直接消失")
ok("except" in lau and "traceback" in lau,
   "★ 未捕获异常在 pythonw 下是静默消失的，必须自己兜住并弹框")

# 所有 .bat/.cmd 都不许用裸 python
import re

bare = re.compile(r'(?<![\\"\w%])python(?:w)?(?:\.exe)?\s+(?:-|main\.py|tools)', re.I)
for f in ROOT.glob("*.bat"):
    body = f.read_text(encoding="ascii", errors="replace")
    code = [l for l in body.splitlines()
            if not l.strip().upper().startswith("REM") and not l.strip().startswith("echo")]
    hits = [l.strip() for l in code if bare.search(l)]
    ok(not hits,
       f"★ {f.name} 不许用裸 python（PATH 上第一个是 hermes venv，没有 "
       f"apscheduler，跑起来就是 ModuleNotFoundError）：{hits}")

for f in [ROOT / "看板.cmd", ROOT / "tools/open-dashboard.cmd"]:
    body = f.read_text(encoding="ascii", errors="replace")
    ok("HUBPY" in body,
       f"{f.name} 要通过 pyenv.cmd 解析出的 %HUBPY% 调用解释器")

# pyenv 必须**验证依赖**而不是只看文件在不在
pyenv = (ROOT / "tools/pyenv.cmd").read_text(encoding="ascii")
ok("import apscheduler" in pyenv,
   "★ pyenv.cmd 要真的 import 一个依赖来验证解释器，"
   "只判 exist 会选中一个装着但缺依赖的 python")
ok(all(c < 128 for c in (ROOT / "tools/pyenv.cmd").read_bytes()),
   "pyenv.cmd 必须纯 ASCII")

# 前台启动脚本不该再存在（会和守护进程抢 8765）
for f in [ROOT / "2-start.bat", ROOT / "看板.cmd"]:
    body = f.read_text(encoding="ascii", errors="replace")
    ok("main.py serve" not in body,
       f"★ {f.name} 不许再前台起服务 —— 会和守护进程抢 8765，"
       f"输的那个反复重启会把 Telegram 刷屏")


print(f"supervisor: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
