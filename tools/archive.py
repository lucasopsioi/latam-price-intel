# -*- coding: utf-8 -*-
"""数据长期存档的命令行入口。

    python tools/archive.py              # 增量存档（幂等，随时可跑）
    python tools/archive.py --verify     # 按 sha256 核对有没有静默损坏
    python tools/archive.py --summary    # 看占多大、覆盖哪些天
    python tools/archive.py --adopt      # 把 data/ 里散落的 .bak 收编进存档

存档目录默认 D:\\workspace\\数据存档\\拉美竞品情报中枢，
可用环境变量 LATAM_ARCHIVE_DIR 覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import archive, config  # noqa: E402


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def do_summary() -> int:
    s = archive.summary()
    if not s.get("exists"):
        print(f"存档目录还不存在：{s['dir']}\n先跑一次 python tools/archive.py")
        return 1
    print(f"存档目录：{s['dir']}")
    print(f"总占用  ：{_mb(s['total_bytes'])}")
    print(f"整库快照：{s['snapshots']} 份"
          f"（自动 {s.get('auto_snapshots', 0)} + 人工备份 {s.get('kept_backups', 0)} 永不轮转）")
    print(f"最近一份：{s['latest_snapshot']}")
    print(f"\n{'表':22}{'分区数':>8}{'占用':>12}")
    print("-" * 44)
    for t, d in sorted(s["tables"].items(), key=lambda x: -x[1]["bytes"]):
        print(f"{t:22}{d['parts']:>8}{_mb(d['bytes']):>12}")
    return 0


def do_verify() -> int:
    print("按 manifest 里的 sha256 逐个核对……")
    r = archive.verify()
    print(f"核对 {r['checked']} 个文件：完好 {r['ok']}，"
          f"内容变了 {len(r['changed'])}，丢失 {len(r['missing'])}")
    for p in r["changed"][:10]:
        print(f"   ★ 内容与存档时不一致：{p}")
    for p in r["missing"][:10]:
        print(f"   ★ 文件不见了：{p}")
    if r["changed"] or r["missing"]:
        print("\n★ 存档文件本该是只读的历史。出现变化/丢失要当事故查。")
        return 1
    print("全部完好。")
    return 0


def do_adopt() -> int:
    """把 data/ 目录里手工留下的 .bak 收编进存档的 snapshots/。

    这些是修数据之前留的"当时状态"，是有价值的历史，
    但散在工作目录里既没人管也随时可能被当垃圾删掉。
    """
    import gzip
    import shutil
    from datetime import datetime

    snap_dir = archive.ARCHIVE_DIR / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    data_dir = Path(config.DB_PATH).parent
    found = sorted(data_dir.glob("*.bak*"))
    if not found:
        print("data/ 下没有 .bak 文件，无需收编。")
        return 0
    for f in found:
        stamp = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
        tag = f.name.replace("intel.db.", "").replace(".", "-")
        out = snap_dir / f"intel-{stamp}-{tag}.db.gz"
        if out.exists():
            print(f"  已收编过，跳过：{f.name}")
            continue
        with open(f, "rb") as fi, gzip.open(out, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)
        archive._append_manifest({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "action": "adopt_backup", "table": "_db", "partition": stamp,
            "bytes": out.stat().st_size, "sha256": archive._sha256(out),
            "path": str(out.relative_to(archive.ARCHIVE_DIR)),
            "note": f"收编自 {f.name}（修数据前的人工备份）",
        })
        print(f"  已收编 {f.name} → {out.name}（{_mb(out.stat().st_size)}）")
    print("\n★ 原文件保留在 data/ 下，确认存档无误后可自行删除。")
    return 0


def main() -> int:
    if "--summary" in sys.argv:
        return do_summary()
    if "--verify" in sys.argv:
        return do_verify()
    if "--adopt" in sys.argv:
        return do_adopt()

    print(f"存档到：{archive.ARCHIVE_DIR}\n")
    r = archive.run_all()
    f, d = r["facts"], r["dims"]
    print(f"事实分区：新写 {f['written']} 个（{f['rows']} 行），"
          f"无变化跳过 {f['skipped']} 个")
    if f["refused"]:
        print(f"\n★★ 有 {f['refused']} 个分区**拒绝覆盖** —— 源库行数比存档时少了。")
        for kind, t, part, now, before in f["details"]:
            if kind == "refused":
                print(f"    {t} 分区 {part}：现在 {now} 行，存档时 {before} 行")
        print("    旧存档已保留。请查清是谁删的（维护脚本？手工？）再决定。")
    print(f"维度快照：{d['written']} 张表（{d['rows']} 行）")
    print(f"整库快照：{r['snapshot']}")
    print()
    do_summary()
    return 2 if f["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
