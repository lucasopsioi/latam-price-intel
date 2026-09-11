# -*- coding: utf-8 -*-
"""导出「竞品情报包」—— 给《拉美双周报工作台》用的接口文件。

用法（在中枢目录）：
    python main.py export-pack --period 2026-W35            # 按双周期次导出
    python main.py export-pack --period 2026-W35 --out D:\\某处
    python main.py export-pack --validate exports\\竞品情报包_xxx.json   # 校验一个包
    python tools\\export_biweekly_pack.py --period 2026-W35          # 也可直接跑

★ 这个脚本**不写任何 SQL**。所有查询来自同目录的 intel-sql-bundle.json ——
  那是双周报工作台从它的 intel-sql.js 生成的。SQL 只有一份，两边数字才同源。
  包里带每条 SQL 的指纹，工作台导入时逐条核对；指纹不同的板块不会被采用。

★ 只读三防线（与工作台的直读通道同一纪律）：
  ① URI mode=ro 打开   ② PRAGMA query_only=1   ③ 自检：真的试一条 CREATE，必须被拒
  intel.db 是不可再生的观测存档，这里绝不以可写方式打开它。

★ 窗口推导与工作台 source-core.planWindow 逐字一致：
    curEnd = min(dataAsOf, period.windowTo)；curStart = curEnd−13d；
    prevEnd = curStart−1d；prevStart = prevEnd−13d
  健康采集日只影响"可比不可比"，工作台拿包里的 dayHealth 行自己判，这里不判。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUNDLE_PATH = HERE / "intel-sql-bundle.json"
SCHEMA = "latam-intel-pack"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------- 期次日历
# 与工作台 period-core 一致：数据截止 = 奇数 ISO 周的周日；窗口 = 截止日往前 14 天；
# 发布 = 截止 + publishOffset（默认 4 天，周四）。
PUBLISH_OFFSET_DAYS = 4


def iso_week_sunday(year: int, week: int) -> dt.date:
    """ISO 年/周 → 该周周日。"""
    jan4 = dt.date(year, 1, 4)
    week1_monday = jan4 - dt.timedelta(days=jan4.isoweekday() - 1)
    monday = week1_monday + dt.timedelta(weeks=week - 1)
    return monday + dt.timedelta(days=6)


def period_from_id(pid: str) -> dict:
    m = re.fullmatch(r"(\d{4})-W(\d{1,2})", pid.strip().upper())
    if not m:
        raise SystemExit(f"期次格式应为 2026-W35，收到：{pid}")
    y, w = int(m.group(1)), int(m.group(2))
    if w % 2 == 0:
        raise SystemExit(f"双周报的数据截止落在**奇数** ISO 周（W29/31/33/35…），{pid} 是偶数周")
    cutoff = iso_week_sunday(y, w)
    return {
        "id": f"{y}-W{w:02d}",
        "windowFrom": (cutoff - dt.timedelta(days=13)).isoformat(),
        "windowTo": cutoff.isoformat(),
        "cutoff": cutoff.isoformat(),
        "publish": (cutoff + dt.timedelta(days=PUBLISH_OFFSET_DAYS)).isoformat(),
    }


def current_period(today: dt.date | None = None) -> dict:
    """今天所属的期次：最近一个已过去（或就是今天）的奇数周周日为截止。"""
    today = today or dt.date.today()
    y, w, _ = today.isocalendar()
    # 从本周往前找最近的奇数周周日 ≤ today
    for back in range(0, 4):
        d = today - dt.timedelta(weeks=back)
        yy, ww, _ = d.isocalendar()
        if ww % 2 == 1 and iso_week_sunday(yy, ww) <= today:
            return period_from_id(f"{yy}-W{ww}")
    raise SystemExit("推不出当前期次")


# ---------------------------------------------------------------- 窗口 / 计划
def add_days(d: str, n: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


def nominal_window(period: dict, data_as_of: str) -> dict:
    cur_end = data_as_of if data_as_of < period["windowTo"] else period["windowTo"]
    cur_start = add_days(cur_end, -13)
    prev_end = add_days(cur_start, -1)
    prev_start = add_days(prev_end, -13)
    return {"dataAsOf": data_as_of,
            "cur": {"start": cur_start, "end": cur_end},
            "prev": {"start": prev_start, "end": prev_end}}


TOKEN_RE = re.compile(r"^\$(asOf|cur\.start|cur\.end|prev\.start|prev\.end)(?:([+-]\d+)d)?$")


def resolve_value(v, win: dict):
    if not (isinstance(v, str) and v.startswith("$")):
        return v
    m = TOKEN_RE.match(v)
    if not m:
        raise SystemExit(f"计划里有未知记号：{v}")
    key, off = m.group(1), int(m.group(2) or 0)
    base = {"asOf": win["dataAsOf"], "cur.start": win["cur"]["start"], "cur.end": win["cur"]["end"],
            "prev.start": win["prev"]["start"], "prev.end": win["prev"]["end"]}[key]
    return add_days(base, off) if off else base


def resolve_params(spec: dict, win: dict) -> dict:
    return {k: resolve_value(v, win) for k, v in (spec or {}).items()}


# ---------------------------------------------------------------- 参数绑定（与工作台 bindParams 同规则）
def bind_params(qspec: dict, params: dict) -> list:
    """按参数规格顺序产出绑定值；缺必填报错；有默认值补默认。"""
    out = []
    for p in qspec.get("params") or []:
        name = p["name"]
        if name in params and params[name] is not None:
            v = params[name]
        elif p.get("required"):
            raise SystemExit(f"查询缺少必填参数 {name}")
        else:
            v = p.get("def")
        t = p.get("type")
        if v is not None:
            if t == "date":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
                    raise SystemExit(f"参数 {name} 应为日期：{v}")
            elif t == "int":
                v = int(v)
            elif t == "num":
                v = float(v)
            else:
                v = str(v)
        out.append(v)
    return out


def sql_uses_named(sql: str) -> bool:
    return bool(re.search(r"[:@$][A-Za-z_]\w*", sql)) and "?" not in sql


# ---------------------------------------------------------------- 只读打开 + 自检
def open_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def prove_read_only(conn: sqlite3.Connection) -> dict:
    """真的试一条写入，必须被拒。只验"能读"不算证明。"""
    detail = []
    ok = True
    for stmt in ("CREATE TABLE __lbs_probe__(x)", "INSERT INTO price_obs(id) VALUES(-1)"):
        try:
            conn.execute(stmt)
            ok = False
            detail.append(f"{stmt[:20]}… 竟然成功了")
        except sqlite3.Error as e:  # noqa: PERF203
            detail.append(f"{stmt[:20]}… 被拒：{str(e)[:60]}")
    return {"writeRejected": ok, "detail": "；".join(detail)}


# ---------------------------------------------------------------- 主流程
def load_bundle(path: Path = BUNDLE_PATH) -> dict:
    if not path.exists():
        raise SystemExit(
            f"找不到 SQL 捆绑包：{path}\n"
            "  它由双周报工作台生成：在工作台目录运行  node scripts/build-sql-bundle.js\n"
            "  会同时写到本目录。没有它这个脚本无法工作——它不自带 SQL。")
    b = json.loads(path.read_text(encoding="utf-8"))
    if b.get("schema") != "latam-intel-sql-bundle":
        raise SystemExit("intel-sql-bundle.json 不是合法的捆绑包")
    return b


def run_board(conn, bundle, board_id, plan_entry, win) -> dict:
    q = plan_entry["query"]
    qspec = bundle["queries"].get(q)
    if not qspec:
        raise SystemExit(f"捆绑包里没有查询 {q}")
    params = resolve_params(plan_entry.get("params") or {}, win)
    sql = qspec["sql"]
    t0 = time.perf_counter()
    if sql_uses_named(sql):
        # 命名绑定也要按规格补默认值/校类型——少一个命名参数 sqlite 直接报错
        names = [p["name"] for p in (qspec.get("params") or [])]
        bound = dict(zip(names, bind_params(qspec, params)))
        rows = conn.execute(sql, bound).fetchall()
    else:
        rows = conn.execute(sql, bind_params(qspec, params)).fetchall()
    ms = round((time.perf_counter() - t0) * 1000)
    return {"query": q, "sqlHash": qspec["sqlHash"], "flags": qspec.get("flags"),
            "params": params, "rows": [dict(r) for r in rows], "ms": ms, "mode": "hub-python"}


def fetch_weekly_digest(conn, win: dict) -> list[dict]:
    """中枢自己的周报汇总（LLM 写的）——进包只作**素材**，工作台会让它过溯源门禁。"""
    try:
        rows = conn.execute(
            """SELECT id, week_start, week_end, scope, title, highlights, content_md, created_at
               FROM weekly_report
               WHERE week_end >= ? AND week_start <= ?
               ORDER BY created_at DESC LIMIT 12""",
            (win["cur"]["start"], win["cur"]["end"])).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["highlights"] = json.loads(d.get("highlights") or "[]")
        except Exception:  # noqa: BLE001
            d["highlights"] = []
        out.append(d)
    return out


def export_pack(db_path: Path, period: dict, out_dir: Path, bundle_path: Path = BUNDLE_PATH) -> Path:
    bundle = load_bundle(bundle_path)
    conn = open_ro(db_path)
    try:
        proof = prove_read_only(conn)
        if not proof["writeRejected"]:
            raise SystemExit("只读自检失败：数据库竟然可写。拒绝继续——intel.db 绝不能被以可写方式碰到。")
        meta = conn.execute(bundle["queries"]["dbMeta"]["sql"]).fetchone()
        meta = dict(meta) if meta else {}
        data_as_of = meta.get("data_as_of")
        if not data_as_of:
            raise SystemExit("dbMeta 没返回 data_as_of，库可能是空的")
        win = nominal_window(period, data_as_of)
        boards = {}
        for board_id, entry in bundle["plan"].items():
            boards[board_id] = run_board(conn, bundle, board_id, entry, win)
        digest = {"weeklyReports": fetch_weekly_digest(conn, win)}
    finally:
        conn.close()

    pack = {
        "schema": SCHEMA, "schemaVersion": SCHEMA_VERSION,
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "generator": {"name": "hub-python", "version": _hub_version(),
                      "bundleBuiltFor": bundle.get("studioVersion")},
        "bundleHash": bundle.get("bundleHash"),
        "period": period, "window": win,
        "hub": {"dbMeta": meta, "dbBytes": db_path.stat().st_size,
                "dbPath": str(db_path.name)},   # 只带文件名，不带本机路径
        "boards": boards, "digest": digest, "readOnlyProof": proof,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    name = (f"竞品情报包_{period['id']}_{win['cur']['start'].replace('-', '')}-"
            f"{win['cur']['end'].replace('-', '')}_导出{dt.date.today().strftime('%Y%m%d')}.json")
    out = _unique(out_dir / name)
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _unique(p: Path) -> Path:
    """重名加序号，绝不覆盖已有的包。"""
    if not p.exists():
        return p
    for i in range(2, 100):
        q = p.with_name(f"{p.stem}({i}){p.suffix}")
        if not q.exists():
            return q
    raise SystemExit("同名文件太多")


def _hub_version() -> str | None:
    try:
        sys.path.insert(0, str(ROOT))
        from app import config  # type: ignore
        return getattr(config, "VERSION", None) or getattr(config, "APP_VERSION", None)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 校验（让中枢也"认"这个格式）
def validate_pack(path: Path, bundle_path: Path = BUNDLE_PATH) -> int:
    pack = json.loads(path.read_text(encoding="utf-8"))
    issues = []
    if pack.get("schema") != SCHEMA:
        issues.append(("error", f"schema 不是 {SCHEMA}"))
    if pack.get("schemaVersion") != SCHEMA_VERSION:
        issues.append(("error", f"schemaVersion={pack.get('schemaVersion')}，只认 {SCHEMA_VERSION}"))
    win = pack.get("window") or {}
    if not (win.get("cur") and win.get("dataAsOf")):
        issues.append(("error", "缺少窗口信息"))
    if not (pack.get("readOnlyProof") or {}).get("writeRejected"):
        issues.append(("warn", "没有只读自检证明"))
    try:
        bundle = load_bundle(bundle_path)
        for bid, b in (pack.get("boards") or {}).items():
            q = b.get("query", bid)
            local = (bundle["queries"].get(q) or {}).get("sqlHash")
            if local and b.get("sqlHash") != local:
                issues.append(("error", f"板块 {bid} 的 SQL 指纹 {b.get('sqlHash')} ≠ 本地 {local}"))
        if pack.get("bundleHash") and pack["bundleHash"] != bundle.get("bundleHash"):
            issues.append(("warn", "捆绑包整体指纹不同：两边软件版本不同步"))
    except SystemExit as e:
        issues.append(("warn", f"未比对 SQL 指纹：{e}"))
    boards = pack.get("boards") or {}
    print(f"竞品情报包 {path.name}")
    print(f"  期次 {(pack.get('period') or {}).get('id')}  窗口 {win.get('cur', {}).get('start')} ~ "
          f"{win.get('cur', {}).get('end')}  数据截止 {win.get('dataAsOf')}")
    print(f"  导出于 {pack.get('generatedAt')}  生成器 {(pack.get('generator') or {}).get('name')}")
    for bid, b in boards.items():
        print(f"  板块 {bid:<12} {len(b.get('rows') or []):>6} 行  {b.get('sqlHash')}")
    reps = ((pack.get("digest") or {}).get("weeklyReports") or [])
    if reps:
        print(f"  附中枢周报汇总 {len(reps)} 份（素材）")
    errors = [t for lv, t in issues if lv == "error"]
    for lv, t in issues:
        print(f"  {'✗' if lv == 'error' else '△'} {t}")
    print("  结论：" + ("不可用" if errors else "可用"))
    return 1 if errors else 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出/校验 竞品情报包（双周报工作台接口）")
    ap.add_argument("--period", help="双周期次，如 2026-W35；缺省=今天所属期次")
    ap.add_argument("--db", help="intel.db 路径；缺省=中枢 data/intel.db")
    ap.add_argument("--out", help="输出目录；缺省=中枢 exports/")
    ap.add_argument("--bundle", help="intel-sql-bundle.json 路径；缺省=本脚本同目录")
    ap.add_argument("--validate", metavar="PACK", help="只校验一个情报包，不导出")
    ap.add_argument("--latest", action="store_true",
                    help="同时覆盖写一份固定名「竞品情报包_最新.json」（网盘同步/计划任务直接拿）")
    a = ap.parse_args(argv)
    bundle_path = Path(a.bundle) if a.bundle else BUNDLE_PATH
    if a.validate:
        return validate_pack(Path(a.validate), bundle_path)
    db_path = Path(a.db) if a.db else ROOT / "data" / "intel.db"
    out_dir = Path(a.out) if a.out else ROOT / "exports"
    period = period_from_id(a.period) if a.period else current_period()
    out = export_pack(db_path, period, out_dir, bundle_path)
    print(f"已导出：{out}")
    if a.latest:
        latest = out_dir / "竞品情报包_最新.json"
        latest.write_bytes(out.read_bytes())
        print(f"已更新：{latest}")
    print("  把这个文件带到公司电脑，在双周报工作台「竞品」页点「导入情报包」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
