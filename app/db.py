# -*- coding: utf-8 -*-
"""数据库层：连接、建表、种子数据、以及各表的读写助手。

密钥存储按平台分两条路：

  - **Windows**：DPAPI 加密后落库（CryptProtectData，绑定当前 Windows 用户）——
    把 intel.db 拷到别的机器/别的账号也解不开，且不引入任何第三方依赖。
  - **Linux/macOS**：没有 DPAPI 等价物。密钥**优先从环境变量读**
    （`LATAM_SECRET_<KEY 大写>`，由 systemd 从 chmod 600 的 EnvironmentFile 注入），
    读不到才回退库里的 `plain:` base64 —— 后者靠文件权限保护（intel.db chmod 600）。

★ 环境变量优先于数据库是刻意的：它让 intel.db 里可以一个密钥都不存，
  备份/传输数据库时不再有泄密风险。代价是设置页改不动这类 Key，
  所以 `list_settings_masked()` 会标出每个值的来源，
  避免界面显示"未设置"而实际正在用环境变量跑。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from . import config

_local = threading.local()


# ---------------------------------------------------------------- DPAPI

# ★ 平台判断必须包住 **import 和类定义两处**，只在函数里 try/except 是不够的：
#     - `import ctypes.wintypes` 在非 Windows 上直接抛异常（模块级，救不回来）
#     - `_DataBlob._fields_` 在**类定义时**就要求值 wt.DWORD
#   db.py 是所有模块的地基，这里炸了整个程序连 import 都过不去。
_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import ctypes.wintypes as wt

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> _DataBlob:
        buf = ctypes.create_string_buffer(data, len(data))
        return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _blob_bytes(blob: _DataBlob) -> bytes:
        return ctypes.string_at(blob.pbData, blob.cbData)


def env_secret(key: str) -> str:
    """环境变量里的密钥覆盖：`minimax_api_key` → `LATAM_SECRET_MINIMAX_API_KEY`。

    只认非空值 —— 空字符串等同于没设，否则一个手滑的空 EnvironmentFile 条目
    会静默盖掉库里配好的 Key，症状是"设置页明明填了却说没配"。
    """
    return (os.environ.get(f"LATAM_SECRET_{key.upper()}") or "").strip()


def encrypt_secret(plain: str) -> str:
    """DPAPI 加密 → base64。非 Windows 或调用失败时退回明文并加前缀标记。"""
    if not plain:
        return ""
    if not _IS_WIN:
        return "plain:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")
    try:
        out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(_blob(plain.encode("utf-8"))), None, None, None, None, 0,
            ctypes.byref(out))
        if not ok:
            raise OSError("CryptProtectData failed")
        data = _blob_bytes(out)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return "dpapi:" + base64.b64encode(data).decode("ascii")
    except Exception:  # noqa: BLE001
        return "plain:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    try:
        if stored.startswith("plain:"):
            return base64.b64decode(stored[6:]).decode("utf-8")
        if not stored.startswith("dpapi:"):
            return stored  # 兼容历史明文
        if not _IS_WIN:
            # 在 Linux 上遇到 dpapi: 只有一种可能：这个库是从 Windows 拷过来的。
            # 解不开是 DPAPI 的设计使然（绑定 Windows 账号），不是故障。
            # 必须 warning 而不是静默返回空 —— 否则表现为"Key 填了但一直说没配"，
            # 是那种查半天才反应过来的问题。
            logging.getLogger("db").warning(
                "库里有 DPAPI 加密的密钥，但当前不是 Windows —— 解不开（正常现象，"
                "该库来自 Windows）。请用环境变量 LATAM_SECRET_* 提供，"
                "或在设置页重新填一次（会以 plain: 重新落库）。")
            return ""
        raw = base64.b64decode(stored[6:])
        out = _DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(_blob(raw)), None, None, None, None, 0, ctypes.byref(out))
        if not ok:
            return ""
        data = _blob_bytes(out)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return data.decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 连接

def get_conn() -> sqlite3.Connection:
    """每线程一个连接（SQLite 连接不能跨线程共享）"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# 增量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
# 所以新增字段必须在这里登记一笔，否则老库升级后会报 no such column。
# ★ 给 schema.sql 里的表加新列时，**必须同时在这里登记一条**。
#   executescript 执行的 CREATE TABLE IF NOT EXISTS 对已存在的表是空操作 ——
#   新列不会自动加上，而且**不报错**：程序照跑，只是那一列永远是空的。
#   实测踩过：加了 sku_code / discount_pct 后 init 显示成功，但字段根本不存在。
MIGRATIONS: list[tuple[str, str, str]] = [
    # (表名, 列名, 列定义)
    ("channel", "category_urls", "TEXT"),
    ("channel", "force_engine", "TEXT"),
    ("price_obs", "seller_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("price_obs", "seller_shipper", "TEXT"),
    ("price_obs", "product_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("price_obs", "sku_code", "TEXT"),
    ("price_obs", "discount_pct", "REAL"),
    # 型号名是"联网查证过的官方写法"还是"规则猜的" —— 必须能分辨。
    # 猜的名字拿去和用户既有报表对账会对不上，而且没有任何提示。
    ("rival_product", "name_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("rival_product", "name_source", "TEXT"),
    # 该国有没有Acme自营商城可取官方价。false = 按口径不做竞品对照，
    # 不是数据缺口（BR/AR Acme没有 offer 商城）。
    ("country", "own_pricing", "INTEGER NOT NULL DEFAULT 1"),
    # ★ 门店铺货信号（方向 14）。以前这段文案被当噪声整段剥掉 ——
    #   因为「Sin stock en tienda Cerrillos」里的缺货词会让整条挂牌被误判缺货
    #   （实测 525 条在售商品因此被踢出价格分析）。剥是对的，但**只剥不存**
    #   等于把一个免费的铺货指标扔了。
    #   NULL = 页面没有门店模块（占 75%），1/0 = 默认门店有货/无货。
    # 情报流国家归属口径（2026-08-19）：country_code 一直存的是**新闻源所在国**，
    # 巴西媒体报全球新闻会被记成"巴西动态"。geo_named=1 表示国家是**原文点名**的，
    # 才能当"该国发生的事"用；0/NULL 只是来源地，按国家看动态时不该算进去。
    ("dynamics", "geo_named", "INTEGER"),
    ("price_obs", "store_stock", "INTEGER"),
    ("price_obs", "store_units", "INTEGER"),
    ("price_obs", "store_name", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for table, column, decl in MIGRATIONS:
        try:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue                      # 表还不存在，schema.sql 会建
        if column not in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                applied.append(f"{table}.{column}")
            except sqlite3.Error as e:
                logging.getLogger("db").warning("迁移 %s.%s 失败: %s", table, column, e)
    return applied


def init_db() -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    conn = get_conn()
    conn.executescript(schema)
    applied = _migrate(conn)
    if applied:
        logging.getLogger("db").info("已应用迁移: %s", ", ".join(applied))
    conn.commit()
    seed()


def reconcile_dangling_runs(conn=None) -> int:
    """把上个进程留下的 running 轮次标成 interrupted。

    ★ 起因：19 个采集轮次里 18 个卡在 running、finished_at 永远是 NULL。
      轮次开始时插一行、结束时更新 —— 但进程被杀 / 服务重启 / 崩溃时，
      那行**永远不会被关掉**，而且没有任何人回头收拾。

      两个后果：运行记录页几乎全是"运行中"，看着像系统坏了；
      更糟的是"最近一次成功采集是什么时候"这类判断会被这些僵尸行带偏。

      ★ 只能在**服务启动**时调用，不能塞进 init_db()。
        init_db() 还会被 tools/site_login.py、tools/ml_login.py 调用，
        而那两个脚本的使用场景恰恰是"服务正在跑着，你本人去过一次验证" ——
        在那时候收僵尸会把**正在跑的**那轮标成中断。
        依据只在服务启动这一刻成立：新服务进程起来时，采集只可能由它自己发起，
        所以此刻还挂着 running 的必然是上一个进程的遗留。
        （残留风险：用户另开一个 `main.py run` 进程跑流水线的同时重启服务，
        会误收那一轮。实际用法是双击快捷方式用服务跑，暂不为此加 pid 表。）
    """
    conn = conn or get_conn()
    cur = conn.execute("""
        UPDATE scrape_run
           SET status='interrupted',
               finished_at=COALESCE(finished_at, datetime('now')),
               warnings=COALESCE(NULLIF(warnings,''),
                                 '进程重启时仍处于 running，判定为被中断')
         WHERE status='running'""")
    conn.commit()
    if cur.rowcount:
        logging.getLogger("db").info("回收僵尸采集轮次 %d 个（上个进程遗留）", cur.rowcount)
    return cur.rowcount


# ---------------------------------------------------------------- 种子

def seed() -> None:
    """把 YAML 配置灌进维度表。幂等：改 YAML 后重跑会更新而不是重复插入。"""
    ch_cfg = config.load_channels()
    br_cfg = config.load_brands()

    with tx() as conn:
        for code, c in (ch_cfg.get("countries") or {}).items():
            conn.execute("""
                INSERT INTO country(code,name_zh,name_local,currency,lang,locale,timezone,
                                    meli_site,meli_domain,sort_order,own_pricing)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET
                  name_zh=excluded.name_zh, name_local=excluded.name_local,
                  currency=excluded.currency, lang=excluded.lang, locale=excluded.locale,
                  timezone=excluded.timezone, meli_site=excluded.meli_site,
                  meli_domain=excluded.meli_domain, sort_order=excluded.sort_order,
                  own_pricing=excluded.own_pricing
            """, (code, c.get("name_zh"), c.get("name_local"), c.get("currency"),
                  c.get("lang"), c.get("locale"), c.get("timezone"),
                  c.get("meli_site"), c.get("meli_domain"), c.get("sort_order", 0),
                  0 if c.get("own_pricing") is False else 1))

        for code, cat in (br_cfg.get("categories") or {}).items():
            conn.execute("""
                INSERT INTO category(code,name_zh,icon,sort_order) VALUES(?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET
                  name_zh=excluded.name_zh, icon=excluded.icon, sort_order=excluded.sort_order
            """, (code, cat.get("name_zh"), cat.get("icon"), cat.get("sort_order", 0)))

        for b in (br_cfg.get("brands") or []):
            conn.execute("""
                INSERT INTO brand(name,aliases,is_ours) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                  aliases=excluded.aliases, is_ours=excluded.is_ours
            """, (b["name"], json.dumps(b.get("aliases") or [], ensure_ascii=False),
                  1 if b.get("is_ours") else 0))

        for ch in (ch_cfg.get("channels") or []):
            cat_urls = ch.get("category_urls")
            conn.execute("""
                INSERT INTO channel(code,country_code,name,kind,base_url,search_url,
                                    category_urls,force_engine,adapter,
                                    default_seller_type,priority,enabled,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(code,country_code) DO UPDATE SET
                  name=excluded.name, kind=excluded.kind, base_url=excluded.base_url,
                  search_url=excluded.search_url, category_urls=excluded.category_urls,
                  force_engine=excluded.force_engine, adapter=excluded.adapter,
                  default_seller_type=excluded.default_seller_type,
                  priority=excluded.priority, enabled=excluded.enabled,
                  notes=excluded.notes
            """, (ch["code"], ch["country"], ch["name"], ch["kind"], ch.get("base_url"),
                  ch.get("search_url"),
                  json.dumps(cat_urls, ensure_ascii=False) if cat_urls else None,
                  ch.get("force_engine"), ch.get("adapter", "generic"),
                  ch.get("default_seller_type", "unknown"), ch.get("priority", 100),
                  int(ch.get("enabled", 1)), ch.get("notes")))


# ---------------------------------------------------------------- 设置

def set_setting(key: str, value: str, is_secret: bool = False) -> None:
    stored = encrypt_secret(value) if is_secret else value
    with tx() as conn:
        conn.execute("""
            INSERT INTO setting(key,value,is_secret,updated_at)
            VALUES(?,?,?,datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
              value=excluded.value, is_secret=excluded.is_secret, updated_at=datetime('now')
        """, (key, stored, 1 if is_secret else 0))


def get_setting(key: str, default: str = "") -> str:
    """读设置。**环境变量优先于数据库**（见模块 docstring）。"""
    env = env_secret(key)
    if env:
        return env
    row = get_conn().execute("SELECT value,is_secret FROM setting WHERE key=?", (key,)).fetchone()
    if not row or row["value"] is None:
        return default
    return decrypt_secret(row["value"]) if row["is_secret"] else row["value"]


def list_settings_masked() -> list[dict]:
    """给设置页用：密钥一律掩码，绝不回传明文。

    ★ 每条额外带一个 `source`（"env" / "db"）。没有这个标记的话，
      用环境变量提供的 Key 在界面上会显示成"未设置"，而系统其实正在正常用它 ——
      界面骗人比界面没信息更糟。
    """
    rows = get_conn().execute(
        "SELECT key,value,is_secret,updated_at FROM setting ORDER BY key").fetchall()
    out, seen = [], set()
    for r in rows:
        seen.add(r["key"])
        env = env_secret(r["key"])
        val = env or (decrypt_secret(r["value"]) if r["is_secret"] else (r["value"] or ""))
        out.append({
            "key": r["key"],
            "value": config.mask(val) if r["is_secret"] else val,
            "is_secret": bool(r["is_secret"]),
            "is_set": bool(val),
            "source": "env" if env else "db",
            "updated_at": r["updated_at"],
        })
    # ★ 只存在于环境变量、库里还没有记录的 Key。不补这一段的话，
    #   "密钥完全不落库"这条路走通了，设置页却会整条漏掉它们 —— 看起来像没配。
    for name, raw in os.environ.items():
        if not name.startswith("LATAM_SECRET_") or not (raw or "").strip():
            continue
        key = name[len("LATAM_SECRET_"):].lower()
        if key in seen:
            continue
        out.append({
            "key": key,
            "value": config.mask(raw.strip()),
            "is_secret": True,
            "is_set": True,
            "source": "env",
            "updated_at": None,
        })
    out.sort(key=lambda d: d["key"])
    return out


# ---------------------------------------------------------------- 通用助手

def q(sql: str, params: tuple | list = ()) -> list[dict]:
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def q1(sql: str, params: tuple | list = ()) -> dict | None:
    r = get_conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def row_hash(*parts) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def today() -> str:
    return date.today().isoformat()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 运行记录

def start_run(mode: str, scope: dict | None = None) -> int:
    with tx() as conn:
        # ★ 新一轮开始前，把还标 running 的旧轮判中断。
        #   任务槽全局只有一个（acquire_task_slot），同时存在第二个 running
        #   必然是僵尸。启动时的 reconcile 有「3 分钟活动检查」——
        #   服务重启恰逢采集刚被杀时，最近的落库让它以为"有进程在采"而跳过回收，
        #   实测 run #41 因此卡在 running 5 个小时。这里兜底：开新轮 = 旧轮必死。
        conn.execute("""UPDATE scrape_run
                        SET status='interrupted', finished_at=datetime('now'),
                            warnings='新一轮采集启动时本轮仍标 running，判定为僵尸轮次'
                        WHERE status='running'""")
        cur = conn.execute(
            "INSERT INTO scrape_run(run_date,mode,scope) VALUES(?,?,?)",
            (today(), mode, json.dumps(scope or {}, ensure_ascii=False)))
        return cur.lastrowid


def finish_run(run_id: int, status: str, warnings: list | None = None) -> None:
    with tx() as conn:
        agg = conn.execute("""
            SELECT COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END),0) ok,
                   COALESCE(SUM(CASE WHEN status IN ('blocked','login_wall') THEN 1 ELSE 0 END),0) blocked,
                   COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) failed,
                   COALESCE(SUM(items),0) items
            FROM scrape_unit WHERE run_id=?
        """, (run_id,)).fetchone()
        conn.execute("""
            UPDATE scrape_run SET finished_at=datetime('now'), status=?,
              pages_ok=?, pages_blocked=?, pages_failed=?, rows_written=?, warnings=?
            WHERE id=?
        """, (status, agg["ok"], agg["blocked"], agg["failed"], agg["items"],
              json.dumps(warnings or [], ensure_ascii=False), run_id))


def log_unit(run_id: int, *, channel_id=None, country=None, brand_id=None, category=None,
             query=None, status="ok", engine=None, items=0, duration_ms=0, message=None) -> None:
    with tx() as conn:
        conn.execute("""
            INSERT INTO scrape_unit(run_id,channel_id,country_code,brand_id,category_code,
                                    query,status,engine,items,duration_ms,message)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (run_id, channel_id, country, brand_id, category, query, status, engine,
              items, duration_ms, message))
