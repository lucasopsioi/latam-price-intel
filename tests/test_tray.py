# -*- coding: utf-8 -*-
"""托盘应用的回归测试（2026-08-31 用户：打包 EXE + 状态栏常驻）。"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


src = (ROOT / "tools/tray.py").read_text(encoding="utf-8")

ok("schtasks" in src and "/run" in src,
   "★ 托盘拉服务只许触发计划任务（schtasks /run）—— 自己 spawn 会让服务"
   "变成托盘子进程，托盘一退服务就死（nohup 掉线 11 小时的老坑）")
_code = chr(10).join(l for l in src.splitlines()
                     if not l.strip().startswith(("#", '"""', "★")))
ok("Popen" not in _code and "main.py" not in _code,
   "★ 不许直接起服务进程（注释里提老坑不算）")
ok("0x08000000" in src or "_NOWIN" in src,
   "子进程必须 CREATE_NO_WINDOW —— 无窗铁律")
ok("returncode == 2" in src and "采集" in src,
   "★ restart.ps1 的采集保护闸（退出码 2）要如实转告用户，不能当成功报")
ok("8767" in src and "bind" in src,
   "单实例：绑独立端口判重（8764 是 supervisor 的，不许撞）")
ok("webbrowser.open" in src, "双击第二次要有反馈（打开看板）而不是静默退出")
ok("300" in src and "_last_kick" in src,
   "看门狗拉起要限频（5 分钟一次），不许每 30 秒锤计划任务")
ok("199, 0, 11" in src, "图标用Acme身份色")
ok("退出托盘（服务照常运行）" in src,
   "★ 退出项必须写明只退托盘不动服务 —— 用户点它不该造成服务停机")

ok((ROOT / "dist/LatamIntelTray.exe").exists(),
   "★ EXE 要真实存在（PyInstaller --noconsole --onefile）")

print(f"tray: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
