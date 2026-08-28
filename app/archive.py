# -*- coding: utf-8 -*-
"""数据长期存档 —— 把抓到的观测**永久留下来**，独立于运行库。

★★ 为什么必须有这个文件：

  1. **价格观测是不可再生的**。今天 Falabella 上这台机器标 899,990 CLP，
     明天它变了，就再也回不去了。爬虫能重跑，历史价格不能重抓。
     `data/intel.db` 是唯一一份，它损坏 / 被误删 / 被维护脚本清掉，
     这段历史就永久没了。

  2. **维护脚本真的会删数据**。`tools/renorm_skus.py` 在合并产品时会
     `DELETE FROM review WHERE rival_product_id IN (...)`（挂牌是特意保住的，
     评论没有）。也就是说：跑一次日常维护，就可能销毁一批原始观测。
     所以存档必须**先于维护发生**，而且存档目录里的东西**只增不删**。

  3. **格式要能脱离本系统读**。存档是给未来的人用的，
     那时可能没有这个项目、没有 Python 环境。所以落成 CSV.gz ——
     pandas / PowerBI / Excel 都能直接读，不依赖本项目任何代码。
     （没用 Parquet 是因为本机没装 pyarrow，不为存档新增依赖；
       gzip 是标准库，永远都在。）

★ 三条不变量（破了就失去存档的意义）：

  A. **只增不删**。本模块任何路径都不执行 DELETE / DROP。
     重复导出同一天是允许的（幂等覆盖该分区文件），但——

  B. **行数只许增不许减**。同一个分区重新导出时，如果行数比上次**少**了，
     说明源库那批数据被删过 —— 这时**拒绝覆盖**，保留旧文件并告警。
     这是整个存档体系里唯一能自动发现"数据被悄悄删了"的地方。

  C. **清单只追加**。manifest.jsonl 每次导出追加一行（时间/表/分区/行数/
     sha256/字节数）。任何时候都能回答"这批数据是什么时候、从多少行导出的"。

★ 快照用 `VACUUM INTO` 而不是复制文件：
  库开着 WAL，直接 cp 会拿到一个**不含未 checkpoint 事务**的残缺副本，
  而且不报错。VACUUM INTO 走 SQLite 自己的事务视图，拿到的是一致快照。
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from . import config, db

log = logging.getLogger("archive")

# ★ 默认放在**工作区一级**（与 knowledge/ 平级），不是项目里面。
#   理由：项目目录可能被重装、移动、清空；存档不该跟着一起没。
#   可用环境变量 LATAM_ARCHIVE_DIR 覆盖。
# ★ Windows 默认值原样保留 —— 存档是**不可再生的观测**，
#   悄悄换个默认目录等于让历史存档"消失"，而且不报错。
_DEFAULT = (Path(r"D:\workspace\数据存档\拉美竞品情报中枢") if sys.platform == "win32"
            else config.ROOT.parent / "数据存档" / "拉美竞品情报中枢")
ARCHIVE_DIR = Path(os.environ.get("LATAM_ARCHIVE_DIR") or _DEFAULT)

# ---- 事实表：按日期分区、追加式。这些是不可再生的观测 ----
# 表名 -> 分区列的 SQL 表达式（必须是 date 型或可转成 YYYY-MM-DD）
FACT_TABLES: dict[str, str] = {
    "price_obs":      "obs_date",                 # ★ 最核心：每日挂牌价
    "review":         "date(created_at)",         # 评论原文
    "review_profile": "date(last_fetched)",       # 评论量画像
    "review_aspect":  "date(created_at)",         # 维度标注
    "dynamics":       "date(created_at)",         # 情报流（按抓到的日子分区）
    "price_move":     "move_date",                # 价格变动
    "voc_insight":    "date(created_at)",         # 口碑洞察
    "launch_event":   "date(created_at)",         # 上市事件
    "strategy_signal": "date(signal_date)",       # 策略信号
    "scrape_unit":    "date(created_at)",         # 采集留痕（复盘用）
    "scrape_run":     "date(started_at)",
    "agent_run":      "date(started_at)",
}

# ★ 存档程序**绝不能因为一个表出错就整体失败** ——
#   那样的后果是"今天一份存档都没有"，而这恰恰是存档要防的事。
#   分区表达式写错 / 列改名 / 表还不存在，一律降级成"整表存一个分区"，
#   并在清单里记下降级原因，而不是抛异常中断后面所有表。
_FALLBACK_PART = "_all"

# ---- 维度表：每次整表快照。小，且没有它读不懂事实表 ----
DIM_TABLES = ["country", "category", "brand", "channel", "rival_product",
              "my_product", "my_sku", "my_pricing", "competitor_match"]


def _paths() -> tuple[Path, Path, Path]:
    root = ARCHIVE_DIR
    return root, root / "datasets", root / "snapshots"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_path() -> Path:
    return ARCHIVE_DIR / "manifest.jsonl"


def _last_rows(table: str, part: str) -> int | None:
    """清单里这个分区上次导出了多少行。没导过返回 None。"""
    mp = _manifest_path()
    if not mp.exists():
        return None
    best = None
    with open(mp, encoding="utf-8") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if j.get("table") == table and j.get("partition") == part \
                    and j.get("action") == "export":
                best = j.get("rows")
    return best


def _append_manifest(rec: dict) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_manifest_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_csv_gz(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    # utf-8-sig：解开 gz 后双击能被 Excel 正确识别中文（本机 ANSI 是 cp936）
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore",
                       lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    # 先写临时文件再改名：中途断电不会留下半个损坏的分区
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=6) as f:
        f.write(data)
    tmp.replace(path)


def export_facts(only: str = "", since: str = "") -> dict:
    """把事实表按日分区导出。幂等；行数变少时拒绝覆盖。"""
    _, ds_dir, _ = _paths()
    stats = {"written": 0, "skipped": 0, "refused": 0, "rows": 0, "details": []}

    for table, part_expr in FACT_TABLES.items():
        if only and table != only:
            continue
        try:
            cols = [c["name"] for c in db.q(f"PRAGMA table_info({table})")]
        except Exception:  # noqa: BLE001
            continue
        if not cols:
            continue
        where = ""
        params: list = []
        if since:
            where = f"WHERE {part_expr} >= ?"
            params = [since]
        try:
            parts = db.q(f"SELECT {part_expr} AS p, COUNT(*) n FROM {table} "
                         f"{where} GROUP BY p ORDER BY p", params)
            degraded = ""
        except Exception as e:  # noqa: BLE001
            # 分区列不存在/表达式失效 —— **降级整表存**，绝不跳过这张表。
            # 宁可存成一个大分区，也不能因为分区键坏了就一行都不存。
            degraded = f"{type(e).__name__}: {str(e)[:80]}"
            log.warning("%s 的分区表达式 %r 失效（%s），降级为整表存档",
                        table, part_expr, degraded)
            try:
                n_all = db.q1(f"SELECT COUNT(*) n FROM {table}")["n"]
            except Exception:  # noqa: BLE001
                continue
            parts = [{"p": _FALLBACK_PART, "n": n_all}]
            part_expr = ""      # 下面取数时走整表分支
        for p in parts:
            part = p["p"]
            if not part:
                part = "_unknown"        # 日期为空的照样存，不能因为没日期就丢
            n_now = p["n"]
            out = ds_dir / table / f"part={part}" / f"{table}.csv.gz"
            n_before = _last_rows(table, str(part))

            # ★ 不变量 B：行数只许增不许减
            if n_before is not None and n_now < n_before and out.exists():
                stats["refused"] += 1
                msg = (f"{table} 分区 {part}：源库现在只有 {n_now} 行，"
                       f"上次存档是 {n_before} 行 —— 少了 {n_before - n_now} 行。"
                       f"**拒绝覆盖**，旧存档保留。请先查清是谁删的。")
                log.warning(msg)
                _append_manifest({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "action": "refused_shrink", "table": table,
                    "partition": str(part), "rows_now": n_now,
                    "rows_archived": n_before, "note": msg,
                })
                stats["details"].append(("refused", table, part, n_now, n_before))
                continue

            if n_before is not None and n_now == n_before and out.exists():
                stats["skipped"] += 1
                continue

            if not part_expr:                       # 降级：整表一个分区
                rows = db.q(f"SELECT * FROM {table}")
            elif p["p"]:
                rows = db.q(f"SELECT * FROM {table} WHERE {part_expr} = ?", [p["p"]])
            else:
                rows = db.q(f"SELECT * FROM {table} WHERE {part_expr} IS NULL")
            _write_csv_gz(out, cols, rows)
            stats["written"] += 1
            stats["rows"] += len(rows)
            rec = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "action": "export", "table": table, "partition": str(part),
                "rows": len(rows), "bytes": out.stat().st_size,
                "sha256": _sha256(out), "path": str(out.relative_to(ARCHIVE_DIR)),
            }
            if degraded:
                rec["degraded"] = degraded          # 降级原因留痕，便于回头修
            _append_manifest(rec)
            stats["details"].append(("written", table, part, len(rows), n_before))
    return stats


def export_dims() -> dict:
    """维度表整表快照 —— 没有它们，事实表里的 id 是读不懂的。"""
    _, ds_dir, _ = _paths()
    today = date.today().isoformat()
    out_stats = {"written": 0, "rows": 0}
    for table in DIM_TABLES:
        try:
            cols = [c["name"] for c in db.q(f"PRAGMA table_info({table})")]
        except Exception:  # noqa: BLE001
            continue
        if not cols:
            continue
        rows = db.q(f"SELECT * FROM {table}")
        out = ds_dir / "_dim" / table / f"asof={today}" / f"{table}.csv.gz"
        _write_csv_gz(out, cols, rows)
        out_stats["written"] += 1
        out_stats["rows"] += len(rows)
        _append_manifest({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "action": "export_dim", "table": table, "partition": today,
            "rows": len(rows), "bytes": out.stat().st_size,
            "sha256": _sha256(out), "path": str(out.relative_to(ARCHIVE_DIR)),
        })
    return out_stats


def snapshot_db(keep: int = 14) -> Path | None:
    """整库快照。

    ★ 用 VACUUM INTO，不用 shutil.copy：
      库开着 WAL，直接复制会得到一个缺少未 checkpoint 事务的副本，
      **而且不报错** —— 等发现的时候已经晚了。
    """
    _, _, snap_dir = _paths()
    snap_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw = snap_dir / f"intel-{stamp}.db"
    gz = snap_dir / f"intel-{stamp}.db.gz"
    try:
        conn = sqlite3.connect(str(config.DB_PATH))
        conn.execute("VACUUM INTO ?", (str(raw),))
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("VACUUM INTO 失败：%s", e)
        return None
    with open(raw, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    raw.unlink()
    _append_manifest({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": "snapshot", "table": "_db", "partition": stamp,
        "bytes": gz.stat().st_size, "sha256": _sha256(gz),
        "path": str(gz.relative_to(ARCHIVE_DIR)),
    })
    # 快照按数量轮转（分区导出**从不**轮转 —— 那才是真正的长期档）
    # ★ 只轮转**自动生成**的快照（intel-YYYYmmdd-HHMMSS.db.gz）。
    #   收编进来的人工备份（…-bak-before-xxx.db.gz）是"改数据之前的那一刻"，
    #   独一无二、再也造不出来，**永不轮转**。
    #   第一版拿 sorted(glob("intel-*")) 一锅端，文件名排序会把人工备份
    #   和自动快照混在一起 —— 可能删掉更新的自动快照却留着旧的人工备份，
    #   更糟的是反过来把人工备份删了。
    import re as _re
    auto = _re.compile(r"^intel-\d{8}-\d{6}\.db\.gz$")
    snaps = sorted((p for p in snap_dir.glob("intel-*.db.gz") if auto.match(p.name)),
                   key=lambda p: p.stat().st_mtime)
    for old in (snaps[:-keep] if keep > 0 else []):
        old.unlink()
        log.info("轮转掉旧快照 %s", old.name)
    return gz


def verify() -> dict:
    """核对存档文件是否与清单里的 sha256 一致（防静默损坏）。"""
    mp = _manifest_path()
    res = {"checked": 0, "ok": 0, "changed": [], "missing": []}
    if not mp.exists():
        return res
    latest: dict[str, dict] = {}
    with open(mp, encoding="utf-8") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if j.get("path") and j.get("sha256"):
                latest[j["path"]] = j            # 同一路径以最后一次为准
    for rel, rec in latest.items():
        p = ARCHIVE_DIR / rel
        res["checked"] += 1
        if not p.exists():
            res["missing"].append(rel)
            continue
        if _sha256(p) != rec["sha256"]:
            res["changed"].append(rel)
        else:
            res["ok"] += 1
    return res


def summary() -> dict:
    """存档现状：占多大、覆盖哪些天、最近一次什么时候。"""
    root, ds_dir, snap_dir = _paths()
    if not root.exists():
        return {"exists": False, "dir": str(root)}
    total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    by_table: dict[str, dict] = {}
    for f in ds_dir.rglob("*.csv.gz") if ds_dir.exists() else []:
        t = f.parent.parent.name
        d = by_table.setdefault(t, {"parts": 0, "bytes": 0})
        d["parts"] += 1
        d["bytes"] += f.stat().st_size
    # 最近一份按**修改时间**判，不按文件名 —— 收编进来的人工备份
    # 文件名带后缀，按名字排会排到自动快照后面，报出一个误导的"最近"
    snaps = sorted(snap_dir.glob("intel-*.db.gz"),
                   key=lambda p: p.stat().st_mtime) if snap_dir.exists() else []
    import re as _re
    _auto = _re.compile(r"^intel-\d{8}-\d{6}\.db\.gz$")
    return {
        "exists": True, "dir": str(root),
        "total_bytes": total,
        "tables": by_table,
        "snapshots": len(snaps),
        "auto_snapshots": sum(1 for p in snaps if _auto.match(p.name)),
        "kept_backups": sum(1 for p in snaps if not _auto.match(p.name)),
        "latest_snapshot": snaps[-1].name if snaps else None,
    }


def run_all(keep_snapshots: int = 14) -> dict:
    """一次做完：事实分区 + 维度快照 + 整库快照。流水线收尾调这个。"""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    _write_readme()
    facts = export_facts()
    dims = export_dims()
    snap = snapshot_db(keep=keep_snapshots)
    return {"facts": facts, "dims": dims,
            "snapshot": snap.name if snap else None,
            "dir": str(ARCHIVE_DIR)}


_README = """# 拉美竞品情报中枢 —— 数据长期存档

这里是**抓取观测的永久留存**，独立于运行库 `拉美竞品情报中枢/data/intel.db`。

## 为什么要有这份存档

价格观测是**不可再生**的：今天某台机器标多少钱，明天变了就再也抓不回来。
运行库只有一份，损坏、误删、或被维护脚本清理掉，这段历史就永久消失。
（`tools/renorm_skus.py` 在合并产品时确实会删除关联的评论行。）

## 目录

```
datasets/            事实数据，按日分区，**只增不删**
  price_obs/part=2026-08-14/price_obs.csv.gz     ← 最核心：每日挂牌价
  review/part=.../review.csv.gz                  ← 评论原文
  dynamics/ price_move/ review_profile/ ...
  _dim/brand/asof=2026-08-14/brand.csv.gz        ← 维度表整表快照
snapshots/           整库快照（VACUUM INTO + gzip），按数量轮转
manifest.jsonl       每次写入追加一行：时间/表/分区/行数/sha256/字节数
```

## 怎么读（不需要本项目的任何代码）

```python
import pandas as pd, glob
df = pd.concat([pd.read_csv(f) for f in
                glob.glob('datasets/price_obs/part=*/price_obs.csv.gz')])
```

CSV 带 UTF-8 BOM，解压后双击能被 Excel 正确识别中文。
PowerBI 直接"从文件夹"导入 `datasets/price_obs` 即可。

## 三条不变量

- **只增不删**：存档程序里没有任何 DELETE / DROP。
- **行数只许增不许减**：重新导出同一分区时若行数变少，
  说明源库那批数据被删过 —— 程序会**拒绝覆盖**并在 manifest 里记
  `refused_shrink`。这是唯一能自动发现"数据被悄悄删了"的机制。
- **清单只追加**：manifest.jsonl 从不重写。

## 维护

```
python tools/archive.py            # 增量存档（幂等，随时可跑）
python tools/archive.py --verify   # 按 sha256 核对有没有静默损坏
python tools/archive.py --summary  # 看占多大、覆盖哪些天
```

★ 存档在每轮采集结束时自动执行。手动跑一次也完全安全。
★ 这个目录**不要**放进版本库，也不要让清理脚本扫到。
"""


def _write_readme() -> None:
    p = ARCHIVE_DIR / "README.md"
    if not p.exists() or p.read_text(encoding="utf-8") != _README:
        p.write_text(_README, encoding="utf-8")
