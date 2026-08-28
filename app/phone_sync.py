# -*- coding: utf-8 -*-
"""导出文件自动同步到手机「工作」文件夹（2026-08-25 用户要求）。

    「每次输出的时候都直接导入到我的手机的工作文件夹里面。
      只要手机连着电脑，它就要自动把这个文件转到手机的存储里面。」

设计（机制沿用 Salesboard/scripts/send_to_phone.py 的已验证做法）：
- 台账驱动：exports/ 里的报告文件（pdf/pptx/docx）对照 phone_export_sync
  台账找出未同步的；手机不在时**不轮询等待**，下个调度周期再试 ——
  这正是"只要连着就自动转"的实现方式：连上后最多几分钟内自动补传。
- MTP 只能走 Shell COM（手机没有盘符）；PowerShell 用 EncodedCommand
  (UTF-16LE) 绕开 cp936，结果经 UTF-8 文件回传（stdout 会被控制台编码咬）。
- ★ 服务是 pythonw 无窗进程，子进程必须 CREATE_NO_WINDOW ——
  否则每次同步弹一个黑框（用户明令：别让我看到对话框）。
- ★ 送达判定 = 从手机拉回来重算 MD5 逐字对账。MTP 报的大小不可信。
- ★ 同名冲突纪律：手机上已有同名文件时，只有当**台账证明是我们传的**
  才用 MoveHere 搬走旧的再传新的（MTP 的 delete 动词会弹确认框挂死
  无人值守，MoveHere 无对话框）；来历不明的同名文件一概不碰，记 skipped。
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from . import config, db

log = logging.getLogger("phone_sync")

_lock = threading.Lock()
_last: dict = {"at": None, "result": None}      # 最近一次尝试（给状态页）

REPORT_EXTS = (".pdf", ".pptx", ".docx")


def _cfg() -> dict:
    try:
        return config.load_runtime().get("phone_sync") or {}
    except Exception:                             # noqa: BLE001
        return {}


def _md5(p: str) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest().upper()


def pending(export_dir=None) -> list[dict]:
    """exports/ 里还没送达手机的报告文件。

    只认报告导出物（pdf/pptx/docx，非 _ 前缀临时文件）——
    exports/ 里还有录入模板、图表 PNG、导入暂存，都不该进手机。
    刚写了不到 10 秒的文件跳过：可能还在写，半个文件传过去就是坏文件。
    """
    d = export_dir or config.EXPORT_DIR
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    for fn in names:
        if fn.startswith("_") or not fn.lower().endswith(REPORT_EXTS):
            continue
        p = os.path.join(str(d), fn)
        if not os.path.isfile(p):
            continue
        if time.time() - os.path.getmtime(p) < 10:
            continue
        m = _md5(p)
        row = db.q1("SELECT status FROM phone_export_sync WHERE file_name=? AND md5=?",
                    (fn, m))
        if row and row["status"] in ("synced", "skipped"):
            continue
        out.append({"name": fn, "path": p, "md5": m,
                    "size": os.path.getsize(p)})
    return out


def _we_synced_name(fn: str) -> bool:
    """这个文件名以前是不是我们传上去的（任何版本）——同名替换的授权边界。"""
    return db.q1("SELECT 1 FROM phone_export_sync WHERE file_name=? AND status='synced'",
                 (fn,)) is not None


def _run_ps(script: str, workdir: str, timeout: int = 900) -> str:
    out = os.path.join(workdir, "ps-out.txt")
    try:
        os.remove(out)
    except OSError:
        pass
    full = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8\n"
            "$ErrorActionPreference='Continue'\n"
            "$OUT='" + out.replace("'", "''") + "'\n"
            "$log=New-Object System.Collections.ArrayList\n"
            "function L($m){[void]$log.Add($m)}\n"
            + script +
            "\n$log | Out-File $OUT -Encoding utf8\n")
    enc = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # 无窗铁律
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-EncodedCommand", enc],
                   capture_output=True, timeout=timeout, creationflags=flags)
    try:
        return io.open(out, encoding="utf-8-sig").read().rstrip("\n")
    except OSError:
        return ""


def _ps_esc(s: str) -> str:
    return s.replace("'", "''")


def _build_script(files: list[dict], workdir: str) -> str:
    """一次 PowerShell 会话干完所有文件：设备发现只做一次。

    每个文件三步：同名处理（授权内 MoveHere 搬走 / 来历不明跳过）→
    CopyHere + 轮询大小 → 拉回 pull/<i>/ 供 Python 对账 MD5。
    """
    cfg = _cfg()
    device = _ps_esc(cfg.get("device") or "Astra 70 Air")
    folder = _ps_esc(cfg.get("folder") or "工作")
    trash = os.path.join(workdir, "trash")
    os.makedirs(trash, exist_ok=True)

    head = f"""
$sh = New-Object -ComObject Shell.Application
$dev = $null
foreach ($i in $sh.NameSpace(17).Items()) {{ if ($i.Name -match '{device}') {{ $dev = $i }} }}
if (-not $dev) {{ L 'ERR:NODEVICE'; $log | Out-File $OUT -Encoding utf8; exit 1 }}
$stor = $null
foreach ($s in $dev.GetFolder.Items()) {{ if ($s.Name -eq '内部存储') {{ $stor = $s }} }}
if (-not $stor) {{ foreach ($s in $dev.GetFolder.Items()) {{ if ($s.IsFolder) {{ $stor = $s; break }} }} }}
if (-not $stor) {{ L 'ERR:NOSTORAGE'; $log | Out-File $OUT -Encoding utf8; exit 1 }}
$work = $null
foreach ($t in $stor.GetFolder.Items()) {{ if ($t.Name -eq '{folder}') {{ $work = $t }} }}
if (-not $work) {{ L 'ERR:NOWORK'; $log | Out-File $OUT -Encoding utf8; exit 1 }}
$wf = $work.GetFolder
$trashNs = $sh.NameSpace('{_ps_esc(trash)}')
function ItemSize($f) {{
  try {{ return [int64]$f.ExtendedProperty('System.Size') }} catch {{ return -1 }}
}}
# ★ 同名判定只认**全名**：Windows 隐藏已知扩展名时 "报告.docx" 显示成
#   "报告"，恰好等于 "报告.pdf" 的基名 —— 用基名判碰撞会让同一份报告的
#   三种格式互相误判（实测 pdf/pptx 被刚传上去的 docx 挡下，双双 skip）。
function FindExact($full) {{
  foreach ($f in $wf.Items()) {{ if ($f.Name -eq $full) {{ return $f }} }}
  return $null
}}
# 落地校验可以用基名兜底，但必须**尺寸对得上**才算是我们刚传的那一个 ——
# 尺寸是同基名兄弟之间唯一可靠的区分（pdf/docx/pptx 体积各不相同）。
function FindLanded($full, $base, $expect) {{
  $e = FindExact $full
  if ($e) {{ return $e }}
  foreach ($f in $wf.Items()) {{
    if ($f.Name -eq $base -and (ItemSize $f) -eq $expect) {{ return $f }}
  }}
  return $null
}}
L ('DEV:' + $dev.Name)
"""
    parts = [head]
    for i, f in enumerate(files):
        full = _ps_esc(f["name"])
        base = _ps_esc(os.path.splitext(f["name"])[0])
        srcdir = _ps_esc(os.path.dirname(f["path"]))
        pull = os.path.join(workdir, "pull", str(i))
        os.makedirs(pull, exist_ok=True)
        replace_ok = "1" if _we_synced_name(f["name"]) else "0"
        parts.append(f"""
# ── [{i}] {full} ──
$full = '{full}'; $base = '{base}'; $expect = {f["size"]}
$old = FindExact $full
if ($old -and '{replace_ok}' -eq '0') {{
  L ('RESULT:{i}:skip-unknown-samename')
}} else {{
  if ($old) {{
    $trashNs.MoveHere($old)
    $gone = $false
    for ($k = 0; $k -lt 60; $k++) {{
      Start-Sleep -Seconds 2
      if (-not (FindExact $full)) {{ $gone = $true; break }}
    }}
    if (-not $gone) {{ L ('RESULT:{i}:replace-timeout'); $skipthis = $true }} else {{ $skipthis = $false }}
  }} else {{ $skipthis = $false }}
  if (-not $skipthis) {{
    $src = $sh.NameSpace('{srcdir}').ParseName($full)
    if (-not $src) {{ L ('RESULT:{i}:nosrc') }}
    else {{
      $wf.CopyHere($src, 16)
      $last = -1; $stable = 0; $done = $false
      for ($k = 0; $k -lt 90; $k++) {{
        Start-Sleep -Seconds 2
        # 传输中体积在长大，还对不上 $expect，所以这里先按全名找；
        # 隐藏扩展名的机器上全名找不到，就用「基名 + 目标尺寸」认领
        $cur = FindExact $full
        if (-not $cur) {{ $cur = FindLanded $full $base $expect }}
        if (-not $cur) {{ continue }}
        $sz = ItemSize $cur
        if ($sz -eq $last -and $sz -gt 0) {{ $stable++ }} else {{ $stable = 0 }}
        $last = $sz
        if (($sz -eq $expect) -or ($stable -ge 3 -and $sz -gt 0)) {{ $done = $true; break }}
      }}
      if (-not $done) {{ L ('RESULT:{i}:copy-timeout:' + $last) }}
      else {{
        $item = FindLanded $full $base $expect
        $sh.NameSpace('{_ps_esc(pull)}').CopyHere($item, 16)
        for ($k = 0; $k -lt 90; $k++) {{
          Start-Sleep -Seconds 2
          $got = Get-ChildItem -LiteralPath '{_ps_esc(pull)}' -File -ErrorAction SilentlyContinue
          if ($got -and $got[0].Length -eq $expect) {{ break }}
        }}
        L ('RESULT:{i}:copied')
      }}
    }}
  }}
}}
""")
    return "\n".join(parts)


def _record(fn: str, m: str, size: int, status: str, detail: str = "") -> None:
    with db.tx() as conn:
        conn.execute("""INSERT INTO phone_export_sync(file_name, md5, size, status, detail)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(file_name, md5) DO UPDATE SET
                          status=excluded.status, detail=excluded.detail,
                          synced_at=datetime('now','localtime')""",
                     (fn, m, size, status, detail))


def sync_now() -> dict:
    """同步一轮。无待传文件时**零开销直接返回**（不起子进程）；
    手机不在时安静返回，等下个周期 —— 不在线是常态，不是故障。"""
    if not _lock.acquire(blocking=False):
        return {"ok": True, "summary": "已有同步在跑"}
    try:
        files = pending()
        if not files:
            return {"ok": True, "summary": "无待传文件", "synced": 0}
        workdir = tempfile.mkdtemp(prefix="latam-phone-")
        try:
            out = _run_ps(_build_script(files, workdir), workdir)
            if "ERR:NODEVICE" in out or "ERR:NOSTORAGE" in out:
                r = {"ok": True, "summary": f"手机未连接，{len(files)} 个文件待传",
                     "synced": 0, "pending": len(files)}
                _last.update(at=db.now(), result=r)
                return r
            if "ERR:NOWORK" in out:
                r = {"ok": False, "summary": "手机上找不到「工作」文件夹", "synced": 0}
                _last.update(at=db.now(), result=r)
                return r
            results = {}
            for line in out.splitlines():
                if line.startswith("RESULT:"):
                    _, idx, *rest = line.split(":")
                    results[int(idx)] = ":".join(rest)
            synced = 0
            details = []
            for i, f in enumerate(files):
                st = results.get(i, "no-result")
                if st == "copied":
                    # 送达判定：拉回来的字节 MD5 必须与本地一致
                    pull = os.path.join(workdir, "pull", str(i))
                    got = [os.path.join(pull, x) for x in os.listdir(pull)] \
                        if os.path.isdir(pull) else []
                    if got and _md5(got[0]) == f["md5"]:
                        _record(f["name"], f["md5"], f["size"], "synced")
                        synced += 1
                        details.append(f"{f['name']} ✓")
                        continue
                    st = "md5-mismatch"
                if st == "skip-unknown-samename":
                    # 来历不明的同名文件不碰；记 skipped 免得每轮都撞一次
                    _record(f["name"], f["md5"], f["size"], "skipped",
                            "手机上已有同名文件且非本系统所传，未覆盖")
                else:
                    _record(f["name"], f["md5"], f["size"], "failed", st)
                details.append(f"{f['name']} ✗{st}")
            r = {"ok": True, "synced": synced, "total": len(files),
                 "summary": f"{synced}/{len(files)} 送达（MD5 对账一致）：" + "；".join(details)}
            _last.update(at=db.now(), result=r)
            return r
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    finally:
        _lock.release()


def kick_async() -> None:
    """导出后立即踢一脚（手机在线则秒传），不阻塞请求线程。"""
    threading.Thread(target=sync_now, daemon=True, name="phone-sync-kick").start()


def status() -> dict:
    rows = db.q("""SELECT file_name, md5, size, status, detail, synced_at
                   FROM phone_export_sync ORDER BY synced_at DESC LIMIT 20""")
    return {"pending": len(pending()), "last_attempt": _last,
            "history": [dict(r) for r in rows]}
