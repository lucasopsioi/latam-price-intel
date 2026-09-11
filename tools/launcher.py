# -*- coding: utf-8 -*-
"""桌面图标的启动器 —— 用 pythonw.exe 跑，**全程没有任何窗口**。

═══ 为什么不是 .cmd ═══

.cmd 一定会创建控制台窗口。就算快捷方式设成"最小化"，它仍然会在任务栏
闪一下、抢一次焦点，出错时还会把 traceback 糊在黑窗口里 —— 用户看到的
就是那种窗口。pythonw.exe 的子系统是 GUI，**根本不分配控制台**，
所以这里改用 Python 写。

═══ 出错怎么办 ═══

没有控制台就没法 print。所以失败时弹一个**原生消息框**（ctypes 调
user32.MessageBoxW，不依赖 tkinter），把最近的日志贴进去。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
HEALTH = f"http://127.0.0.1:{PORT}/api/health"
DASH = f"http://127.0.0.1:{PORT}/"

MB_OK, MB_ICONERROR, MB_ICONWARNING = 0x0, 0x10, 0x30
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def box(msg: str, title: str = "拉美竞品情报中枢", icon: int = MB_ICONERROR) -> None:
    ctypes.windll.user32.MessageBoxW(None, msg, title, MB_OK | icon)


def healthy(timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return r.status == 200
    except Exception:                            # noqa: BLE001
        return False


def tail(p: Path, n: int = 14) -> str:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if lines else "（空）"
    except OSError:
        return "（读不到）"


def pythonw() -> str:
    """守护进程要用**装了依赖的那个**解释器起。

    ★ 这正是用户看到那个 traceback 的原因：旧脚本用裸 `python`，
      PATH 上第一个 python 是另一个虚拟环境，没有 apscheduler，
      一 import 就炸。这里显式解析，绝不依赖 PATH。
    """
    cands = [
        Path(sys.executable).with_name("pythonw.exe"),      # 就是我自己（首选）
        Path(r"C:\Python314\pythonw.exe"),
    ]
    for c in cands:
        if c.exists():
            return str(c)
    return sys.executable


def main() -> int:
    # 已经在跑：直接开看板，什么都不做
    if healthy():
        webbrowser.open(DASH)
        return 0

    # 没跑起来：拉守护进程。守护自己是单例，重复点图标不会起重。
    try:
        subprocess.Popen(
            [pythonw(), str(ROOT / "tools" / "supervisor.py")],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        )
    except Exception as e:                       # noqa: BLE001
        box(f"启动守护进程失败：\n\n{type(e).__name__}: {e}")
        return 1

    # 等它起来。冷启动要建连接池、迁 schema，给足 120 秒。
    for _ in range(40):
        time.sleep(3)
        if healthy():
            webbrowser.open(DASH)
            return 0

    box(
        "情报中枢没能启动起来（等了 120 秒仍无响应）。\n\n"
        "常见原因：\n"
        "  1. 依赖没装全 —— 先跑一次 1-install.bat\n"
        "  2. 端口 8765 被别的程序占了\n"
        "  3. 数据库被别的进程锁住了\n\n"
        "───────── 服务日志（最近几行）─────────\n"
        f"{tail(ROOT / 'logs' / 'server.log')}\n\n"
        "───────── 守护日志 ─────────\n"
        f"{tail(ROOT / 'logs' / 'supervisor.log', 8)}\n\n"
        f"完整日志：{ROOT / 'logs'}"
    )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                       # noqa: BLE001
        # 没有控制台，未捕获异常会静默消失 —— 必须自己兜住并显示
        import traceback
        box(f"启动器自身出错：\n\n{traceback.format_exc()[-1500:]}")
        sys.exit(1)
