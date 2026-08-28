# -*- coding: utf-8 -*-
"""手机同步功能的回归测试（2026-08-25 用户要求）。

  「每次输出的时候都直接导入到我的手机的工作文件夹里面。
    只要手机连着电脑，它就要自动把这个文件转到手机的存储里面。」

纪律来自 销售团队 send_to_phone 的血泪（MTP 三条铁律）+ 本服务的无窗铁律。
"""
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


src = (ROOT / "app/phone_sync.py").read_text(encoding="utf-8")

# ─────────── 1. MTP 三条铁律（销售团队 实测沉淀，一条都不能丢） ───────────
ok("CREATE_NO_WINDOW" in src,
   "★ 服务是 pythonw 无窗进程，PowerShell 子进程必须 CREATE_NO_WINDOW —— "
   "否则每 3 分钟弹一个黑框（用户明令：别让我看到对话框）")
ok("MoveHere" in src and "InvokeVerb" not in src,
   "★ 清同名旧文件只许 MoveHere 搬走 —— MTP 的 delete 动词会弹确认框，"
   "无人值守直接挂死")
ok("_md5(got[0]) == f[\"md5\"]" in src,
   "★ 送达判定必须是拉回来重算 MD5 —— MTP 报的大小不可信")
ok("utf-16-le" in src and "utf-8-sig" in src,
   "PowerShell 走 EncodedCommand(UTF-16LE)、结果走 UTF-8 文件回传 —— "
   "cp936 控制台两头都会咬中文")

# ─────────── 2. 授权边界：来历不明的同名文件不碰 ───────────
ok("skip-unknown-samename" in src and "_we_synced_name" in src,
   "★ 手机上已有同名文件时，只有台账证明是我们传的才替换；"
   "来历不明的一概不碰（授权边界与 销售团队 清理同一条纪律）")
ok("FindExact" in src and "FindLanded" in src,
   "★ 同名碰撞只认全名、落地校验才用基名 —— Windows 隐藏扩展名时 "
   "「报告.docx」显示成「报告」，用基名判碰撞会让同一份报告的 pdf/pptx "
   "被刚传上去的 docx 挡下（实测双双 skip）")
_fe = src[src.index("function FindExact"):src.index("function FindLanded")]
ok("-eq $base" not in _fe,
   "★ FindExact 里绝不能有基名回退，否则碰撞误判会复发")
ok("-eq $expect" in src[src.index("function FindLanded"):src.index("L ('DEV:'")],
   "★ 基名兜底必须同时比对尺寸 —— 尺寸是同基名兄弟间唯一可靠的区分")
ok("$old = FindExact $full" in src,
   "替换判定走全名精确匹配")
ok("status='synced'" in src.replace('"', "'"),
   "同名替换的判据是台账里的 synced 记录")

# ─────────── 3. 行为契约 ───────────
_sn = src[src.index("def sync_now"):]
ok("if not files:" in _sn and _sn.index("if not files:") < _sn.index("_run_ps("),
   "★ 无待传文件时零开销返回 —— 不起子进程、不摸设备")
ok("ERR:NODEVICE" in src and "手机未连接" in src,
   "手机不在时安静返回等下个周期 —— 不在线是常态，不是故障，不许报警刷屏")
ok('".pdf", ".pptx", ".docx"' in src,
   "只同步报告导出物 —— exports/ 里的模板/PNG/导入暂存不进手机")
ok('fn.startswith("_")' in src, "_ 前缀临时文件不进手机")
ok("getmtime" in src and "< 10" in src,
   "刚写完 10 秒内的文件跳过 —— 半个文件传过去就是坏文件")
ok("blocking=False" in src, "并发闸：定时任务与导出踢腿不许同时跑两轮")

# ─────────── 4. 功能测试：pending 扫描 ───────────
from app import db  # noqa: E402

db.init_db()
from app import phone_sync  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    for fn in ("报告A.pdf", "_temp.pdf", "模板.xlsx", "图.png", "报告B.pptx"):
        pathlib.Path(td, fn).write_bytes(b"x" * 100)
    old = time.time() - 60
    for fn in os.listdir(td):
        os.utime(os.path.join(td, fn), (old, old))
    got = {f["name"] for f in phone_sync.pending(td)}
    ok(got == {"报告A.pdf", "报告B.pptx"},
       f"pending 只认报告文件，实得 {got}")
    # 台账里 synced 的不再待传
    m = phone_sync._md5(os.path.join(td, "报告A.pdf"))
    with db.tx() as conn:
        conn.execute("INSERT OR REPLACE INTO phone_export_sync"
                     "(file_name, md5, size, status) VALUES(?,?,?,?)",
                     ("报告A.pdf", m, 100, "synced"))
    got2 = {f["name"] for f in phone_sync.pending(td)}
    ok(got2 == {"报告B.pptx"}, f"已送达的不再待传，实得 {got2}")
    with db.tx() as conn:
        conn.execute("DELETE FROM phone_export_sync WHERE file_name='报告A.pdf'")

# ─────────── 5. 接线：调度器 + 导出端点 + 表 ───────────
sched = (ROOT / "app/scheduler.py").read_text(encoding="utf-8")
ok("_job_phone_sync" in sched and 'id="phone_sync"' in sched,
   "★ 调度器要有 phone_sync 周期任务 —— 「只要连着就自动转」靠它")
ok(sched.index('id="phone_sync"') < sched.index('cfg["schedule"].get("enabled")'),
   "★ 手机同步不受 schedule.enabled 管 —— 那个开关管每日采集，"
   "关了采集不等于不要手机同步")

api = (ROOT / "app/api/server.py").read_text(encoding="utf-8")
ok("kick_async" in api and "EXPORT_DIR / name" in api,
   "★ 导出端点必须落盘 exports/ 并踢一次同步 —— 否则「每次输出」只到浏览器")
ok("/api/phone-sync" in api, "要有状态端点可查同步台账")

schema = (ROOT / "app/schema.sql").read_text(encoding="utf-8")
ok("phone_export_sync" in schema and "UNIQUE(file_name, md5)" in schema,
   "台账表要在 schema.sql（IF NOT EXISTS 全量执行，存量库自动建）")

cfgy = (ROOT / "config/runtime.yaml").read_text(encoding="utf-8")
ok("phone_sync:" in cfgy and "Astra 70 Air" in cfgy,
   "设备名/文件夹/周期要可配（runtime.yaml phone_sync 块）")

print(f"phonesync: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
