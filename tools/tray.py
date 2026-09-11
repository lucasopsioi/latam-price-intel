# -*- coding: utf-8 -*-
"""系统托盘常驻应用（2026-08-31 用户：状态栏里要显示这个应用，
不要每次都去网页打开）。

职责边界：
  - 托盘只是**门面 + 第二道看门狗**：真正的 24 小时运转仍由
    计划任务 LatamIntelHub → supervisor → serve 负责（那套已验证过
    断电重启、进程崩溃等场景）。托盘挂了服务照跑；服务挂了托盘变灰
    并自动拉起计划任务。
  - ★ 拉服务只许 `schtasks /run`（触发计划任务），绝不自己 spawn 子进程 ——
    否则服务变成托盘的子进程，托盘一退服务就死（老坑：nohup 掉线 11 小时）。

打包：tools/build_tray.ps1 → PyInstaller --noconsole 单文件 exe。
单实例：绑 127.0.0.1:8766 判重，双击第二次只会把看板打开一遍就退出。
"""
from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.request
import webbrowser

import pystray
from PIL import Image, ImageDraw

BASE = "http://127.0.0.1:8765"
SINGLETON_PORT = 8767          # 8764 是 supervisor 的单例口，别撞
TASK_NAME = "LatamIntelHub"
ACME_RED = (199, 0, 11)
GRAY = (142, 142, 147)

# CREATE_NO_WINDOW：托盘本身无窗，子进程也不许弹黑框
_NOWIN = 0x08000000


def _icon_img(color) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=color)
    d.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 230))
    return img


def _get(path: str, timeout: float = 4):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str, timeout: float = 8):
    req = urllib.request.Request(BASE + path, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _start_service():
    subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                   capture_output=True, creationflags=_NOWIN, timeout=30)


class Tray:
    def __init__(self):
        self.healthy = False
        self.tip = "拉美竞品情报中枢 · 启动中…"
        self._last_kick = 0.0
        self.icon = pystray.Icon(
            "latam-intel", _icon_img(GRAY), self.tip, menu=pystray.Menu(
                pystray.MenuItem("打开看板", self.open_board, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("立即生成报告（DeepSeek/MiniMax）", self.gen_report),
                pystray.MenuItem("同步手机", self.sync_phone),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("重启服务", self.restart_service),
                pystray.MenuItem("退出托盘（服务照常运行）", self.quit),
            ))

    # ---- 菜单动作 ----
    def open_board(self, *_):
        webbrowser.open(BASE)

    def gen_report(self, *_):
        def w():
            try:
                _post("/api/run/weekly")
                self.icon.notify("已开始生成，完成后自动导出三格式并送手机", "报告生成中")
            except Exception:                    # noqa: BLE001
                self.icon.notify("服务未响应，先点「重启服务」", "无法触发")
        threading.Thread(target=w, daemon=True).start()

    def sync_phone(self, *_):
        def w():
            try:
                r = _post("/api/phone-sync/run", timeout=600)
                self.icon.notify(str(r.get("summary", ""))[:120], "手机同步")
            except Exception:                    # noqa: BLE001
                self.icon.notify("同步失败或服务未响应", "手机同步")
        threading.Thread(target=w, daemon=True).start()

    def restart_service(self, *_):
        def w():
            try:
                import pathlib
                ps1 = pathlib.Path(__file__).resolve().parent / "restart.ps1"
                # 打包成 exe 后 __file__ 在临时目录，退回固定安装路径
                if not ps1.exists():
                    ps1 = pathlib.Path(r"D:\workspace\拉美竞品情报中枢\tools\restart.ps1")
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(ps1)],
                    capture_output=True, creationflags=_NOWIN, timeout=180)
                # restart.ps1 的采集保护闸：采集中会拒绝（退出码 2），如实转告
                if r.returncode == 2:
                    self.icon.notify("正在采集，为保护数据本次不重启；稍后再试", "已拒绝")
                else:
                    self.icon.notify("已重启" if r.returncode == 0 else "重启失败，看日志", "服务")
            except Exception:                    # noqa: BLE001
                self.icon.notify("重启脚本执行失败", "服务")
        threading.Thread(target=w, daemon=True).start()

    def quit(self, *_):
        self.icon.stop()

    # ---- 健康轮询（30 秒）：变色 + 看门狗 ----
    def _poll(self):
        while True:
            try:
                h = _get("/api/health")
                ok = bool(h.get("ok"))
                d = h.get("detail") or {}
                task = f" · 采集中" if d.get("task_running") else ""
                tip = (f"拉美竞品情报中枢 · 运行中{task}\n"
                       f"观测 {d.get('price_obs', 0):,} 条 · 最近 {d.get('last_obs_date', '?')}")
            except Exception:                    # noqa: BLE001
                ok, tip = False, "拉美竞品情报中枢 · 服务未响应（正在自动拉起）"
                # 看门狗：5 分钟最多拉一次，只触发计划任务（见文件头纪律）
                now = time.time()
                if now - self._last_kick > 300:
                    self._last_kick = now
                    try:
                        _start_service()
                    except Exception:            # noqa: BLE001
                        pass
            if ok != self.healthy or tip != self.tip:
                self.healthy, self.tip = ok, tip
                self.icon.icon = _icon_img(ACME_RED if ok else GRAY)
                self.icon.title = tip
            time.sleep(30)

    def run(self):
        threading.Thread(target=self._poll, daemon=True).start()
        self.icon.run()


def main():
    # 单实例：绑不上端口说明托盘已在跑，把看板打开一下就退（用户双击有反馈）
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
    except OSError:
        webbrowser.open(BASE)
        return
    try:
        Tray().run()
    finally:
        s.close()


if __name__ == "__main__":
    main()
