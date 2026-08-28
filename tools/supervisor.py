# -*- coding: utf-8 -*-
"""守护进程：保证界面 24 小时在线，掉了自动拉起，拉不起来就报警。

═══ 为什么需要它 ═══

原来的启动方式（2-start.bat / 看板.cmd）是**前台进程**：开一个黑窗口，
服务是那个窗口的子进程。窗口一关、会话一断，整棵进程树跟着死。
2026-08-15 那次掉线就是这么没的 —— 日志停在 10:56:07，没有 traceback、
没有优雅关闭记录，同一秒所有 Chrome 会话一起断连，是被整树杀掉的特征。
**服务从来就没有作为独立进程活过。**

═══ 三层保障 ═══

  第一层  计划任务在登录时拉起本脚本（本脚本自己是常驻的）
  第二层  本脚本盯着子进程，死了就重启（指数退避，防止疯狂重启刷屏）
  第三层  本脚本每 60 秒探一次 /api/health，探不通也重启

═══ 为什么探针要查库而不是只看端口 ═══

uvicorn 的端口在依赖就绪之前就已经在监听了。数据库锁死、磁盘满、
schema 没迁移的时候端口照样答应，只看端口会一路绿灯而界面全白。
/api/health 会真的查一次 price_obs，查不动返回 503。

═══ 报警纪律 ═══

只在**状态翻转**时报警（掉线时报一次、恢复时报一次），不是每次探测都报。
连续故障用指数退避 —— 断网 8 小时不该收到 480 条 Telegram。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("LATAM_PORT", "8765"))
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"

PROBE_EVERY = 60            # 探针间隔（秒）
PROBE_TIMEOUT = 15          # 单次探针超时；库大的时候 health 会慢，别设太短
START_GRACE = 90            # 刚拉起后的宽限期，这段时间不判死（要建连接池、迁 schema）
FAIL_BEFORE_RESTART = 3     # 连续几次探针失败才重启（单次失败可能只是刚好在跑重活）
NET_CHECK_EVERY = 600       # 外网连通性检查间隔（秒）

BACKOFF = [5, 15, 60, 180, 600]     # 重启退避阶梯（秒），到顶就一直用最后一个

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
SUP_LOG = LOG_DIR / "supervisor.log"
SRV_LOG = LOG_DIR / "server.log"

# Windows: 让子进程独立成组，这样父进程退出不会连坐；
# 也便于我们只杀自己这一棵树，不误伤用户其它 Chrome/Python。
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


SINGLETON_PORT = PORT - 1   # 8764：只用来占位，不收数据


def claim_singleton() -> "socket.socket | None":
    """保证同一时刻只有一个守护进程。

    ★ 为什么需要：计划任务的 MultipleInstances=IgnoreNew 只管住计划任务自己。
      桌面图标走的是另一条路（open-dashboard.cmd 直接起 pythonw），
      服务恰好没起来时点图标，就会**再起一个守护**。两个守护抢 8765，
      输的那个反复重启失败，Telegram 会被刷屏 —— 报警刷屏等于没有报警。

    ★ 为什么用端口而不是锁文件：端口在进程死亡时由内核**自动释放**，
      不会留下需要人工清理的陈旧锁。锁文件遇到强杀就会变成"幽灵锁"，
      下次永远起不来，那种故障比重复启动更难查。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 不设 SO_REUSEADDR —— 这里要的就是"被占用时抢不到"
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with SUP_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def alert(text: str) -> None:
    """报警。Telegram 发不出去也不能让守护进程崩 —— 报警失败本身要记下来。"""
    log(f"[报警] {text}")
    try:
        from app import notify
        ok, detail = notify.send_telegram(f"🖥 情报中枢守护\n{text}")
        log(f"[报警] Telegram {'已发送' if ok else '发送失败: ' + str(detail)}")
    except Exception as e:                       # noqa: BLE001
        log(f"[报警] Telegram 异常（不影响守护）: {e}")
    # Telegram 没配也要有个能看见的兜底：写一个显眼的文件
    try:
        (LOG_DIR / "ALERT-最近一次故障.txt").write_text(
            f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n", encoding="utf-8")
    except OSError:
        pass


def probe() -> tuple[bool, str]:
    """探活。返回 (是否健康, 说明)。"""
    try:
        import httpx
        r = httpx.get(HEALTH_URL, timeout=PROBE_TIMEOUT)
        if r.status_code == 200 and r.json().get("ok"):
            return True, "ok"
        return False, f"HTTP {r.status_code} body={r.text[:160]}"
    except Exception as e:                       # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:160]}"


# 外网探针的目标：挑三个**互不相干**的家，任意一个通就算有网。
# 只探一个的话，那一家自己抽风就会误报"断网"。
NET_TARGETS = ["https://www.cloudflare.com/cdn-cgi/trace",
               "https://www.gstatic.com/generate_204",
               "https://api.telegram.org"]


def probe_internet() -> tuple[bool, str]:
    """外网连通性。

    ★ 这个失败**不重启服务** —— 重启治不好断网，只会把日志刷满。
      但必须报警：没网的时候采集是静默空转，界面照常绿灯，
      你会以为在采数据其实一条都没进来。
    """
    try:
        import httpx
    except Exception as e:                       # noqa: BLE001
        return True, f"httpx 不可用，跳过外网检查: {e}"
    last = ""
    for url in NET_TARGETS:
        try:
            r = httpx.get(url, timeout=10)
            if r.status_code < 500:
                return True, url
        except Exception as e:                   # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:100]}"
    return False, last or "全部目标不可达"


def spawn() -> subprocess.Popen:
    """拉起服务子进程。日志重定向到文件 —— 无窗口运行时 stdout 无处可去。"""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    f = SRV_LOG.open("a", encoding="utf-8", errors="replace")
    f.write(f"\n{'=' * 70}\n{datetime.now():%Y-%m-%d %H:%M:%S} 启动服务\n{'=' * 70}\n")
    f.flush()
    p = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "main.py"), "serve", "--port", str(PORT)],
        cwd=str(ROOT), env=env, stdout=f, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NEW_PROCESS_GROUP,
    )
    log(f"已拉起服务进程 pid={p.pid}，端口 {PORT}")
    return p


def kill(p: subprocess.Popen) -> None:
    """只杀我们自己拉起的那棵树。

    ★ 绝不按进程名杀 —— 用户自己开的 Chrome / 别的 Python 不能误伤。
      taskkill /T 按 PID 杀整棵树，范围就限定在我们的子进程里。
    """
    if p.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    except Exception as e:                       # noqa: BLE001
        log(f"taskkill 失败（继续）: {e}")
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        log("子进程未在 15 秒内退出，继续往下走")


def main() -> int:
    lock = claim_singleton()
    if lock is None:
        log(f"已有守护进程在跑（{SINGLETON_PORT} 被占），本进程退出，不重复拉起")
        return 0

    log("=" * 60)
    log(f"守护进程启动 pid={os.getpid()}  目标端口 {PORT}")
    log(f"探针 {HEALTH_URL} 每 {PROBE_EVERY}s，连续 {FAIL_BEFORE_RESTART} 次失败才重启")

    proc = spawn()
    started = time.time()
    fails = 0
    restarts = 0
    degraded = False            # 当前是否处于"已报警的故障态"
    offline = False             # 外网是否处于"已报警的断网态"
    tick = 0
    last_net_check = time.time()

    while True:
        # ★ 用 proc.wait(timeout=) 代替 sleep()：
        #   进程**当场死掉**时 wait 立刻返回，不用干等到下一个探针周期。
        #   实测差别很大：纯 sleep 轮询时"杀掉→恢复"最坏要 60s+5s；
        #   换成 wait 之后进程崩溃是秒级发现，只有"进程还活着但没响应"
        #   那种假死才需要等探针。两种故障都覆盖到了。
        try:
            proc.wait(timeout=PROBE_EVERY)
        except subprocess.TimeoutExpired:
            pass                                 # 还活着，走下面的健康探针
        tick += 1

        # 外网每 10 分钟查一次。★ 按**时间**判而不是按轮数 ——
        # 上面改成 wait() 之后一轮不再固定是 60 秒，进程反复崩的时候
        # 轮数会跑得飞快，按轮数算会把外网探针打成高频请求。
        if time.time() - last_net_check >= NET_CHECK_EVERY:
            last_net_check = time.time()
            net_ok, net_why = probe_internet()
            if not net_ok and not offline:
                alert(f"🌐 外网不通，采集会空转（服务本身没事）。\n最后错误：{net_why}")
                offline = True
            elif net_ok and offline:
                alert(f"🌐 外网已恢复（{net_why}）。")
                offline = False

        # 子进程直接没了 —— 不用等探针
        if proc.poll() is not None:
            code = proc.returncode
            log(f"服务进程已退出（exit={code}）")
            alert(f"服务进程意外退出（exit={code}），正在重启。\n"
                  f"日志：{SRV_LOG}")
            degraded = True
            wait = BACKOFF[min(restarts, len(BACKOFF) - 1)]
            log(f"退避 {wait}s 后重启（第 {restarts + 1} 次）")
            time.sleep(wait)
            proc = spawn()
            started = time.time()
            restarts += 1
            fails = 0
            continue

        # 刚启动的宽限期内不判死
        if time.time() - started < START_GRACE:
            continue

        healthy, why = probe()
        if healthy:
            if degraded:
                alert(f"✅ 已恢复。累计重启 {restarts} 次。")
                degraded = False
            fails = 0
            restarts = 0        # 稳定运行后把退避阶梯清零
            continue

        fails += 1
        log(f"探针失败 {fails}/{FAIL_BEFORE_RESTART}: {why}")
        if fails < FAIL_BEFORE_RESTART:
            continue

        if not degraded:
            alert(f"❌ 界面探针连续 {fails} 次失败，正在重启服务。\n原因：{why}")
            degraded = True

        kill(proc)
        wait = BACKOFF[min(restarts, len(BACKOFF) - 1)]
        log(f"退避 {wait}s 后重启（第 {restarts + 1} 次）")
        time.sleep(wait)
        proc = spawn()
        started = time.time()
        restarts += 1
        fails = 0

        # 连续重启很多次说明不是偶发，升级报警
        if restarts in (5, 20, 50):
            alert(f"⚠️ 已连续重启 {restarts} 次仍未稳定，"
                  f"大概率不是偶发故障，请看日志：{SRV_LOG}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("守护进程收到中断，退出")
        sys.exit(0)
