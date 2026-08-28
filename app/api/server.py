# -*- coding: utf-8 -*-
"""本地 Web 服务（FastAPI）。只监听 127.0.0.1，不对外暴露。

密钥纪律：
  设置接口只回传掩码值（sk-A********4567），任何情况下不回传明文。
  前端要改 Key 就重新填一遍，不做"回显后编辑"——回显就等于把明文送出去了。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config, db, products
from ..agents import AGENT_ROSTER

log = logging.getLogger("api")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="拉美竞品情报中枢", docs_url=None, redoc_url=None)

# 后台任务状态（采集/体检是长任务，不能阻塞 HTTP）
_TASK = {"running": False, "name": "", "progress": "", "result": None, "error": ""}
_TASK_LOCK = threading.Lock()

# 密钥类设置项：这些走加密存储，且只回传掩码
SECRET_KEYS = {
    "minimax_api_key", "meli_access_token", "x_bearer_token",
    "telegram_bot_token", "openai_api_key", "proxy_password",
}
PLAIN_KEYS = {
    "llm_base_url", "llm_model", "proxy", "telegram_chat_id",
}


# ---------------------------------------------------------------- 首页

_ASSET_RE = re.compile(r'(src|href)="(/static/[^"?]+\.(?:js|css))"')


def _stamp_assets(html: str) -> str:
    """给 /static 的 js/css 挂上按内容变化的版本号。

    ★★ 不挂的后果实测过一次，而且极难自查：改完 boards.js、重启服务、
      刷新页面 —— 服务端返回的**已经是新文件**，浏览器却仍在跑旧的那份，
      于是"改了没生效"。因为响应只有 etag/last-modified 没有 Cache-Control，
      浏览器按启发式缓存直接复用，连问都不问。
      症状是新功能静默失效、且**没有任何报错**，很容易误判成代码写错了。
    ⇒ 用文件内容的哈希做版本号：内容不变则 URL 不变（缓存照旧命中），
      内容一变 URL 就变（必然重新拉取）。比用 mtime 稳，改回原样不会白刷。
    """
    def sub(m):
        attr, path = m.group(1), m.group(2)
        f = WEB_DIR / path[len("/static/"):]
        try:
            h = hashlib.md5(f.read_bytes()).hexdigest()[:10]
        except OSError:
            return m.group(0)
        return f'{attr}="{path}?v={h}"'
    return _ASSET_RE.sub(sub, html)


@app.get("/", response_class=HTMLResponse)
def index():
    f = WEB_DIR / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>界面文件缺失</h1>", status_code=500)
    html = _stamp_assets(f.read_text(encoding="utf-8"))
    # index.html 本身也不能被缓存，否则里面的版本号永远更新不了
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache, must-revalidate",
    })


# ---------------------------------------------------------------- 健康检查

_STARTED_AT = time.time()


@app.get("/api/health")
def health():
    """守护进程用的存活探针。

    ★ 不能只看"端口通不通"。uvicorn 端口早在依赖就绪前就已经监听，
      数据库锁死 / 磁盘满 / schema 没迁移的时候端口照样答应，
      探针会一路绿灯而界面全白。所以这里**真的去查一次库**。
    """
    ok, detail = True, {}
    try:
        r = db.q1("SELECT COUNT(*) n FROM price_obs") or {}
        detail["price_obs"] = r.get("n", 0)
        last = db.q1("SELECT MAX(obs_date) d FROM price_obs") or {}
        detail["last_obs_date"] = last.get("d")
    except Exception as e:                       # noqa: BLE001
        ok = False
        detail["db_error"] = str(e)[:200]

    with _TASK_LOCK:
        detail["task_running"] = _TASK["running"]
        detail["task_name"] = _TASK["name"]

    body = {
        "ok": ok,
        "uptime_sec": int(time.time() - _STARTED_AT),
        "pid": os.getpid(),
        "detail": detail,
    }
    return JSONResponse(body, status_code=200 if ok else 503)


# ---------------------------------------------------------------- 概览

@app.get("/api/overview")
def overview():
    today = db.today()
    week_ago = (date.today() - timedelta(days=7)).isoformat()

    countries = db.q("""
        SELECT co.code, co.name_zh, co.currency,
               (SELECT COUNT(*) FROM channel WHERE country_code=co.code AND enabled=1)
                 AS channels,
               (SELECT COUNT(*) FROM price_obs WHERE country_code=co.code
                  AND obs_date>=?) AS obs_7d,
               (SELECT COUNT(DISTINCT rival_product_id) FROM price_obs
                  WHERE country_code=co.code AND obs_date>=?
                    AND rival_product_id IS NOT NULL) AS products_7d
        FROM country co WHERE co.enabled=1 ORDER BY co.sort_order
    """, (week_ago, week_ago))

    last_run = db.q1("SELECT * FROM scrape_run ORDER BY id DESC LIMIT 1")
    audit = db.q1("""
        SELECT SUM(audit_status='accepted') ok, SUM(audit_status='rejected') rej,
               SUM(audit_status='pending') pend
        FROM price_obs WHERE obs_date>=?
    """, (week_ago,)) or {}

    return {
        "date": today,
        "countries": countries,
        "totals": {
            "my_products": db.q1("SELECT COUNT(*) c FROM my_product")["c"],
            "rival_products": db.q1("SELECT COUNT(*) c FROM rival_product")["c"],
            "price_obs": db.q1("SELECT COUNT(*) c FROM price_obs")["c"],
            "price_obs_7d": db.q1("SELECT COUNT(*) c FROM price_obs WHERE obs_date>=?",
                                  (week_ago,))["c"],
            "matches": db.q1("""SELECT COUNT(*) c FROM competitor_match
                                WHERE is_excluded=0""")["c"],
            "dynamics_7d": db.q1("""SELECT COUNT(*) c FROM dynamics
                                    WHERE date(created_at)>=?""", (week_ago,))["c"],
            # ★ 侧栏「消费者口碑」的角标原本没有数据源，一直显示 0 ——
            #   而库里有 1576 条评论。角标上的 0 会被读成"没有评论数据"，
            #   和"真的没抓到"长得一模一样。
            "reviews": db.q1("SELECT COUNT(*) c FROM review")["c"],
        },
        "audit": {"accepted": audit.get("ok") or 0, "rejected": audit.get("rej") or 0,
                  "pending": audit.get("pend") or 0},
        "last_run": last_run,
        "task": dict(_TASK),
        "configured": {
            "minimax": bool(db.get_setting("minimax_api_key")),
            "meli": bool(db.get_setting("meli_access_token")),
            "telegram": bool(db.get_setting("telegram_bot_token")),
            "proxy": bool(db.get_setting("proxy")),
        },
    }


# ---------------------------------------------------------------- 设置

@app.get("/api/settings")
def get_settings():
    stored = {s["key"]: s for s in db.list_settings_masked()}
    items = []
    for key in sorted(SECRET_KEYS | PLAIN_KEYS):
        s = stored.get(key)
        items.append({
            "key": key, "is_secret": key in SECRET_KEYS,
            "value": (s or {}).get("value", ""),
            "is_set": (s or {}).get("is_set", False),
            "updated_at": (s or {}).get("updated_at"),
        })
    return {"settings": items, "runtime": config.load_runtime()}


@app.post("/api/settings/test")
def test_settings():
    """逐个测试已配置的 Key 是否真的可用。

    填完 Key 最想知道的就是"到底通没通"。不测就要等到跑完一整轮采集、
    看到报告里一片空白才发现 Key 是错的。
    ★ 任何情况下都不回显 Key 本身，只报通不通。
    """
    results = []

    # ① MiniMax —— 七个 Agent 的命脉
    key = db.get_setting("minimax_api_key")
    if not key:
        results.append({"name": "MiniMax", "ok": False,
                        "message": "未配置（Agent 将全部退回规则模式）"})
    else:
        try:
            from ..agents import LLMClient
            cfg = config.load_runtime()["agents"]
            llm = LLMClient(cfg)
            reply, tokens = llm.chat("回复两个字：正常")
            ok = bool(reply and reply.strip())
            results.append({"name": "MiniMax", "ok": ok,
                            "message": (f"连通，模型返回「{reply.strip()[:12]}」"
                                        f"（{tokens} tokens）") if ok
                                       else "调用无返回 —— 检查模型名、接口地址与账号余额"})
        except Exception as e:  # noqa: BLE001
            results.append({"name": "MiniMax", "ok": False,
                            "message": f"{type(e).__name__}: {str(e)[:90]}"})

    # ② MercadoLibre —— 不通则阿根廷完全没数据
    token = db.get_setting("meli_access_token")
    if not token:
        results.append({"name": "MercadoLibre", "ok": False,
                        "message": "未配置（阿根廷将无任何数据，其余五国走网页兜底）"})
    else:
        try:
            import httpx
            r = httpx.get("https://api.mercadolibre.com/sites/MLM/search",
                          params={"q": "samsung", "limit": 1},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
            n = len((r.json() or {}).get("results", [])) if r.status_code == 200 else 0
            results.append({"name": "MercadoLibre", "ok": r.status_code == 200,
                            "message": f"官方API可用（试搜返回 {n} 条）" if r.status_code == 200
                                       else f"HTTP {r.status_code}，token 可能已过期"})
        except Exception as e:  # noqa: BLE001
            results.append({"name": "MercadoLibre", "ok": False,
                            "message": f"{type(e).__name__}: {str(e)[:90]}"})

    # ③ Telegram
    tg = db.get_setting("telegram_bot_token")
    chat = db.get_setting("telegram_chat_id")
    if not tg:
        results.append({"name": "Telegram", "ok": False, "message": "未配置（不影响采集）"})
    else:
        try:
            import httpx
            r = httpx.get(f"https://api.telegram.org/bot{tg}/getMe", timeout=25)
            j = r.json() if r.status_code == 200 else {}
            name = (j.get("result") or {}).get("username", "")
            ok = bool(j.get("ok"))
            msg = f"机器人 @{name} 可用" if ok else "token 无效"
            if ok and not chat:
                msg += "，但还缺 Chat ID（填了才能收到消息）"
            results.append({"name": "Telegram", "ok": ok and bool(chat), "message": msg})
        except Exception as e:  # noqa: BLE001
            results.append({"name": "Telegram", "ok": False,
                            "message": f"{type(e).__name__}: {str(e)[:90]}"})

    # ④ 代理（配了就测，很多海外站在国内要靠它）
    proxy = db.get_setting("proxy")
    if proxy:
        try:
            import httpx
            r = httpx.get("https://news.google.com/rss?hl=es-419",
                          proxy=proxy, timeout=25)
            results.append({"name": "代理", "ok": r.status_code == 200,
                            "message": f"可访问 Google News（HTTP {r.status_code}）"})
        except Exception as e:  # noqa: BLE001
            results.append({"name": "代理", "ok": False,
                            "message": f"{type(e).__name__}: {str(e)[:80]}"})

    return {"results": results}


@app.post("/api/settings")
async def save_settings(payload: dict):
    saved = []
    for key, value in (payload.get("settings") or {}).items():
        if key not in SECRET_KEYS and key not in PLAIN_KEYS:
            continue
        if value is None:
            continue
        v = str(value).strip()
        # 前端回显的是掩码，用户没改动时会把掩码原样传回来 —— 必须识别并跳过，
        # 否则会把 "sk-A********4567" 当成真 Key 存进去
        if "*" * 6 in v:
            continue
        db.set_setting(key, v, is_secret=(key in SECRET_KEYS))
        saved.append(key)

    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        cur = config.load_runtime()
        config.save_runtime(config._deep_merge(cur, runtime))
        saved.append("runtime")
    return {"ok": True, "saved": saved}


# ---------------------------------------------------------------- 渠道

@app.get("/api/channels")
def get_channels():
    rows = db.q("""
        SELECT c.*, co.name_zh AS country_name, co.sort_order,
               h.verdict, h.suggested_action, h.avg_items, h.check_date
        FROM channel c
        JOIN country co ON co.code=c.country_code
        LEFT JOIN channel_health h
               ON h.channel_id=c.id
              AND h.check_date=(SELECT MAX(check_date) FROM channel_health
                                WHERE channel_id=c.id)
        ORDER BY co.sort_order, c.priority
    """)
    return {"channels": rows}


@app.post("/api/channels/{channel_id}/toggle")
def toggle_channel(channel_id: int, payload: dict):
    with db.tx() as conn:
        conn.execute("UPDATE channel SET enabled=? WHERE id=?",
                     (1 if payload.get("enabled") else 0, channel_id))
    return {"ok": True}


# ---------------------------------------------------------------- 产品

@app.get("/api/products")
def get_products():
    return {"products": products.list_products()}


@app.get("/api/products/template")
def download_template():
    out = config.EXPORT_DIR / "产品录入模板.xlsx"
    products.write_template(out)
    return FileResponse(out, filename="产品录入模板.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument."
                                   "spreadsheetml.sheet")


@app.post("/api/products/import")
async def import_products(file: UploadFile):
    tmp = config.EXPORT_DIR / f"_import_{file.filename}"
    tmp.write_bytes(await file.read())
    try:
        report = products.import_workbook(tmp)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"导入失败：{e}")
    finally:
        tmp.unlink(missing_ok=True)
    return report


@app.post("/api/products/import-list")
def import_product_list():
    """导入 config/my_products.csv（用户提供的 117 款产品清单）"""
    return products.import_product_list()


@app.get("/api/products/{pid}")
def get_product(pid: int):
    p = products.product_detail(pid)
    if not p:
        raise HTTPException(404, "产品不存在")
    return p


# ---------------------------------------------------------------- 价格

@app.get("/api/prices")
def get_prices(q: str = "", country: str = "", category: str = "", brand: str = "",
               seller_kind: str = "", only_device: bool = False,
               sort: str = "date", days: int = 7, status: str = "all",
               limit: int = 300):
    """★ 默认 status='all' 而不是 'accepted'。

    价格审计是**后置**阶段：采集刚跑完时所有行都是 pending。
    如果默认只显示 accepted，采集完成后打开页面会是**全白** ——
    用户会认为程序没跑，而实际上 5000 条数据好好地在库里。
    审计状态应该作为**标注**呈现，不是作为看得见看不见的门槛。
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    where = ["po.obs_date>=?"]
    params: list = [since]
    if q:
        # 型号搜索：标题 / 归一化型号 / 权威 SKU 三个字段都找
        where.append("(po.title LIKE ? OR po.model_guess LIKE ? OR po.sku_code LIKE ?)")
        params += [f"%{q}%"] * 3
    if country:
        where.append("po.country_code=?"); params.append(country.upper())
    if category:
        where.append("po.category_code=?"); params.append(category)
    if brand:
        where.append("b.name=?"); params.append(brand)
    if seller_kind:
        where.append("po.seller_kind=?"); params.append(seller_kind)
    if only_device:
        where.append("po.product_kind<>'accessory'")
    if status and status != "all":
        where.append("po.audit_status=?"); params.append(status)

    order = {"price_asc": "po.sale_price ASC",
             "price_desc": "po.sale_price DESC",
             "discount": "po.discount_pct DESC",
             }.get(sort, "po.obs_date DESC, po.id DESC")
    params.append(limit)

    rows = db.q(f"""
        SELECT po.id, po.obs_date, po.country_code, po.title, po.model_guess,
               po.sku_code, po.ram_gb, po.rom_gb, po.color, po.sale_price,
               po.list_price, po.discount_pct, po.currency, po.installments,
               po.seller_name, po.seller_type, po.seller_kind, po.product_kind,
               po.is_in_stock, po.condition, po.audit_status, po.audit_reason,
               po.audit_by, po.url, b.name AS brand, c.name AS channel,
               c.kind AS channel_kind, rp.model_name
        FROM price_obs po
        LEFT JOIN brand b ON b.id=po.brand_id
        LEFT JOIN channel c ON c.id=po.channel_id
        LEFT JOIN rival_product rp ON rp.id=po.rival_product_id
        WHERE {' AND '.join(where)}
        ORDER BY {order} LIMIT ?
    """, params)
    total = db.q1(f"""SELECT COUNT(*) c FROM price_obs po
                      LEFT JOIN brand b ON b.id=po.brand_id
                      WHERE {' AND '.join(where)}""", params[:-1])["c"]
    return {"prices": rows, "count": len(rows), "total": total}


@app.get("/api/dashboard/matrix")
def dash_matrix(grain: str = "day", days: int = 30, country: str = "",
                category: str = ""):
    """竞品看板：时间 × 国家 × 品类 的竞争动态矩阵。"""
    from .. import dashboard
    return dashboard.matrix(grain, days, country, category)


@app.get("/api/dashboard/summary")
def dash_summary(days: int = 7):
    from .. import dashboard
    return {"rows": dashboard.country_category_summary(days)}


@app.get("/api/dashboard/movers")
def dash_movers(days: int = 7, country: str = "", category: str = "",
                official_only: bool = False, limit: int = 40):
    from .. import dashboard
    return {"rows": dashboard.movers(days, country, category, official_only, limit)}


@app.get("/api/dashboard/signals")
def dash_signals(days: int = 14, country: str = "", signal_type: str = "",
                 limit: int = 50):
    from .. import dashboard
    return {"rows": dashboard.signals(days, country, signal_type, limit)}


@app.get("/api/trend/products")
def trend_products(country: str = "", category: str = "", days: int = 90,
                   limit: int = 200):
    from .. import dashboard
    return {"rows": dashboard.trackable_products(country, category, days, limit)}


# ---------------------------------------------------------------- 价格曲线

@app.get("/api/trend/series")
def trend_series(kind: str = "category", key: str = "", country: str = "",
                 days: int = 90, by_channel: bool = True):
    """单个对象的价格曲线。kind = product | category | brand。"""
    from .. import trends
    if kind == "product":
        if not str(key).isdigit():
            raise HTTPException(400, "产品曲线的 key 要传 rival_product_id")
        return trends.product_series(int(key), days=days, country=country,
                                     by_channel=by_channel)
    if kind == "category":
        return trends.category_series(key, country=country, days=days)
    if kind == "brand":
        return trends.brand_series(key, country=country, days=days)
    raise HTTPException(400, "kind 只能是 product / category / brand")


@app.post("/api/trend/compare")
def trend_compare(payload: dict):
    """多对象同屏对比。跨币种自动指数化（基期=100）。

    payload: {"entities":[{"kind":..,"key":..,"country":..}], "days":90,
              "index_base": null|true|false}
    """
    from .. import trends
    ents = payload.get("entities") or []
    if not ents:
        raise HTTPException(400, "至少选一个对象")
    return trends.compare(ents, days=int(payload.get("days") or 90),
                          index_base=payload.get("index_base"))


@app.get("/api/trend/candidates")
def trend_candidates(limit: int = 60):
    """可画曲线的对象清单（给选择器用）。"""
    from .. import trends
    prods = trends.suggest_watch(limit)
    cats = db.q("""SELECT c.code, c.name_zh FROM category c
                   WHERE c.enabled=1 ORDER BY c.sort_order""")
    brands = db.q("""SELECT b.name, b.is_ours,
                            COUNT(DISTINCT po.obs_date) AS obs_days
                     FROM brand b JOIN price_obs po ON po.brand_id=b.id
                     WHERE po.audit_status<>'rejected'
                     GROUP BY b.id HAVING obs_days>=2 ORDER BY obs_days DESC""")
    return {"products": prods, "categories": cats, "brands": brands}


# ---------------------------------------------------------------- 关注清单与预警

@app.get("/api/watchlist")
def get_watchlist(all: bool = False):
    from .. import trends
    return {"items": trends.watchlist(enabled_only=not all),
            "thresholds": trends.PRIORITY_THRESHOLD,
            "priority_zh": trends.PRIORITY_ZH,
            "candidates": trends.suggest_watch(60)}


@app.post("/api/watchlist/add")
def watchlist_add(payload: dict):
    from .. import trends
    try:
        return trends.add_watch(
            payload.get("scope", "product"), payload.get("key"),
            country=payload.get("country", "") or "",
            priority=payload.get("priority", "P1"),
            drop_pct=payload.get("drop_pct"), rise_pct=payload.get("rise_pct"),
            note=payload.get("note", "") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/watchlist/{watch_id}/remove")
def watchlist_remove(watch_id: int):
    from .. import trends
    return trends.remove_watch(watch_id)


@app.get("/api/alerts")
def get_alerts(unread: bool = False, limit: int = 100):
    from .. import trends
    return {"items": trends.alerts(unread_only=unread, limit=limit)}


@app.post("/api/alerts/scan")
def alerts_scan(days: int = 3):
    """按关注清单扫一遍价格变动，产出预警。"""
    from .. import trends
    return trends.scan_alerts(days=days)


@app.post("/api/alerts/read")
def alerts_read(payload: dict | None = None):
    from .. import trends
    return trends.mark_read((payload or {}).get("ids"))


@app.post("/api/alerts/push")
def alerts_push():
    from .. import trends
    return trends.push_pending()


# ---------------------------------------------------------------- 图形看板

@app.get("/api/board/price-band")
def board_price_band(country: str = "", category: str = "", days: int = 7):
    """价格带卡位（区间条）。锁单一国家、用本币 —— 跨国不同轴。"""
    from .. import boards
    return boards.price_band(country, category, days)


@app.get("/api/board/discount-heat")
def board_discount_heat(days: int = 7):
    """折扣力度热力（国家 × 品类），中性点 = 大盘中位折扣。"""
    from .. import boards
    return boards.discount_heat(days)


@app.get("/api/board/seller-spread")
def board_seller_spread(country: str = "", category: str = "", days: int = 7):
    """自营 vs 第三方价差（哑铃）。同配置比，不同配置不算一款。"""
    from .. import boards
    return boards.seller_spread(country, category, days)


@app.get("/api/board/own-vs-rivals")
def board_own_vs_rivals(country: str = "", category: str = "", days: int = 21):
    """我方 vs 友商价差（%，跨国可比）。"""
    from .. import boards
    return boards.own_vs_rivals(country, category, days)


@app.get("/api/board/moves")
def board_moves(direction: str = "up", days: int = 30, country: str = "",
                category: str = "", official_only: bool = False, tier: str = ""):
    """涨价/降价清单 + 可信度分档。看板的第一张图先看噪声占比。"""
    from .. import boards
    return boards.price_moves_tiered(direction, days, country, category,
                                     official_only, tier)


@app.get("/api/board/promo-shrink")
def board_promo_shrink(days: int = 14, country: str = ""):
    """促销收缩 —— 涨价先行指标。按商品固定篮子，剔除构成效应。"""
    from .. import boards
    return boards.promo_shrink(days, country)


@app.get("/api/board/voc-rank")
def board_voc_rank(country: str = "", category: str = "", brand: str = "",
                   days: int = 365, kind: str = "product"):
    """口碑维度排行（发散条）+ 情感来源分层 + 覆盖漏斗。"""
    from .. import boards
    return {
        "rank": boards.voc_dimension_rank(country, category, brand, days, kind),
        "source": boards.voc_sentiment_source(country, category, days),
        "coverage": boards.voc_coverage(),
    }


@app.get("/api/voc/radar")
def voc_radar(country: str = "", category: str = "", brand: str = "",
              product_id: int = 0, days: int = 180, kind: str = "product"):
    """口碑维度雷达。kind=product 看产品好坏，kind=experience 看渠道好坏。"""
    from .. import dashboard, voc_aspects
    data = dashboard.voc_radar(country, category, brand,
                               product_id or None, days, kind)
    return {**data, "kinds": ["product", "experience"],
            "all_aspects": [{"code": c, "name": voc_aspects.ASPECT_ZH[c],
                             "kind": voc_aspects.ASPECT_KIND[c]}
                            for c in voc_aspects.ASPECT_CODES]}


@app.get("/api/voc/decay")
def voc_decay(product_id: int = 0, bucket_days: int = 30, max_days: int = 360):
    """上市后口碑衰减曲线。excluded 里要如实说明谁没画、为什么。"""
    from .. import dashboard
    return dashboard.voc_decay(product_id or None, bucket_days, max_days)


@app.get("/api/trend/{rival_product_id}")
def trend_one(rival_product_id: int, country: str = "", days: int = 90,
              official_only: bool = False):
    from .. import dashboard
    p = db.q1("""SELECT rp.model_name, b.name AS brand, rp.category_code
                 FROM rival_product rp JOIN brand b ON b.id=rp.brand_id
                 WHERE rp.id=?""", (rival_product_id,))
    if not p:
        raise HTTPException(404, "产品不存在")
    data = dashboard.price_trend(rival_product_id, country, None, days,
                                 official_only)
    return {**data, "product": dict(p)}


@app.get("/api/weekly/list")
def weekly_list():
    return {"reports": db.q("""SELECT id, week_start, week_end, scope, title,
                                      created_at
                               FROM weekly_report
                               -- ★ 按**生成时间**排，不是按期次起点。
                               --   旧周报用周一做 week_start（如 08-17），
                               --   新口径用期次起点（08-05）—— 按 week_start 排会让
                               --   刚生成的新报告被半个月前的旧报告压在下面，
                               --   界面上"最新一期"打开的是旧格式，看起来像没生效。
                               ORDER BY created_at DESC, id DESC LIMIT 30""")}


@app.get("/api/weekly/{rid}/export/{fmt}")
def weekly_export(rid: int, fmt: str):
    """导出 Word / PPT / PDF。图在服务端用 matplotlib 重画（网页版是 ECharts）。"""
    from fastapi.responses import Response
    from .. import report_export

    r = db.q1("SELECT * FROM weekly_report WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "报告不存在")
    try:
        met = json.loads(r["metrics"] or "{}")
    except Exception:                                   # noqa: BLE001
        met = {}
    sub = f"{r['week_start']} ~ {r['week_end']} · 范围 {r['scope']}"
    try:
        data, name = report_export.export(
            fmt, r["title"] or "竞品报告", sub,
            r["content_md"] or "", met.get("charts") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    # ★ 落盘一份到 exports/ 并踢一次手机同步（2026-08-25 用户要求：
    #   每次输出都自动转进手机「工作」文件夹，手机在线则立即送达，
    #   不在线由 phone_sync 定时任务在连上后补传）。落盘失败不影响下载。
    try:
        (config.EXPORT_DIR / name).write_bytes(data)
        from .. import phone_sync
        phone_sync.kick_async()
    except Exception:                                   # noqa: BLE001
        log.warning("导出物落盘/手机同步触发失败（下载不受影响）", exc_info=True)
    # ★ 文件名含中文：必须用 RFC 5987 的 filename*，否则浏览器会存成乱码
    from urllib.parse import quote
    return Response(
        content=data, media_type=report_export.MIME.get(fmt, "application/octet-stream"),
        headers={"Content-Disposition":
                 f"attachment; filename=report.{fmt}; "
                 f"filename*=UTF-8''{quote(name)}"})


@app.get("/api/phone-sync")
def phone_sync_status():
    """手机同步状态：待传数、最近一次尝试、台账。"""
    from .. import phone_sync
    return phone_sync.status()


@app.post("/api/phone-sync/run")
def phone_sync_run():
    """手动触发一轮同步（阻塞至完成，返回结果）。"""
    from .. import phone_sync
    return phone_sync.sync_now()


@app.get("/api/weekly/{rid}")
def weekly_get(rid: int):
    r = db.q1("SELECT * FROM weekly_report WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "周报不存在")
    return r


@app.get("/api/meta/filters")
def get_filter_options():
    """筛选框的可选项。

    ★ 之前只填了国家下拉框，品类和品牌的下拉框**从来没被填充过** ——
      永远只有"全部产业"一个选项，用户看到的就是一个点不动的摆设。
      选项必须从**实际有数据的行**里取，而不是从配置里取：
      配置里列了 38 个品牌，但库里可能只有 12 个真的抓到过，
      让用户在下拉框里选一个必然 0 结果的品牌，比不给选项更糟。
    """
    return {
        "countries": db.q("""SELECT DISTINCT po.country_code AS code,
                                    co.name_zh AS name, COUNT(*) n
                             FROM price_obs po JOIN country co ON co.code=po.country_code
                             GROUP BY po.country_code ORDER BY n DESC"""),
        "categories": db.q("""SELECT category_code AS code, COUNT(*) n
                              FROM price_obs WHERE category_code IS NOT NULL
                              GROUP BY category_code ORDER BY n DESC"""),
        # ★ is_ours 必须带出来：关注清单是「友商」清单，前端要靠它把我方品牌
        #   区分开。以前没给这个字段，前端写的 `filter(x => !x.is_ours)` 就是
        #   一句空转（!undefined 恒为 true），Acme会混进友商清单里。
        "brands": db.q("""SELECT b.name AS code, b.is_ours, COUNT(*) n
                          FROM price_obs po JOIN brand b ON b.id=po.brand_id
                          GROUP BY b.id ORDER BY n DESC"""),
        "channels": db.q("""SELECT c.name AS code, c.country_code, COUNT(*) n
                            FROM price_obs po JOIN channel c ON c.id=po.channel_id
                            GROUP BY c.id ORDER BY n DESC"""),
        # ★ 上面那些是"有数据的"，下面这个是"应该有的"。两者必须都给：
        #   只给前者，用户看不到 Positivo（配了但还没抓到），
        #   会以为系统压根不跟踪它 —— 分不清"没配"和"配了没抓到"。
        "brands_by_category": _brands_by_category(),
    }


def _brands_by_category() -> dict:
    """配置里每个品类**应该**覆盖的品牌 + 各自实际抓到多少条。

    n=0 不是错误，是"还没抓到" —— 界面要照样列出来并标注，
    这样"这个牌子怎么没有"能一眼看出是覆盖问题还是采集问题。
    """
    from .. import config

    rows = db.q("""SELECT b.name, po.category_code AS cat, COUNT(*) n
                   FROM price_obs po JOIN brand b ON b.id=po.brand_id
                   WHERE po.category_code IS NOT NULL
                   GROUP BY b.id, po.category_code""")
    counts = {(r["name"], r["cat"]): r["n"] for r in rows}

    out: dict[str, list] = {}
    for b in (config.load_brands().get("brands") or []):
        for cat in (b.get("categories") or []):
            out.setdefault(cat, []).append({
                "code": b["name"],
                "n": counts.get((b["name"], cat), 0),
                "is_ours": bool(b.get("is_ours")),
            })
    for cat in out:
        out[cat].sort(key=lambda x: (-x["n"], x["code"]))
    return out


# ---------------------------------------------------------------- 竞品对照

@app.get("/api/matches")
def get_matches(product_id: int = 0, country: str = ""):
    where, params = ["cm.is_excluded=0"], []
    if product_id:
        where.append("cm.my_product_id=?"); params.append(product_id)
    if country:
        where.append("cm.country_code=?"); params.append(country.upper())

    rows = db.q(f"""
        SELECT cm.*, mp.marketing_name AS my_name, mp.category_code,
               rp.model_name AS rival_name, b.name AS rival_brand,
               co.name_zh AS country_name
        FROM competitor_match cm
        JOIN my_product mp ON mp.id=cm.my_product_id
        JOIN rival_product rp ON rp.id=cm.rival_product_id
        JOIN brand b ON b.id=rp.brand_id
        JOIN country co ON co.code=cm.country_code
        WHERE {' AND '.join(where)}
        ORDER BY mp.marketing_name, co.sort_order, cm.rank_in_country
    """, params)
    for r in rows:
        try:
            r["reasons"] = json.loads(r.get("reasons") or "{}")
        except Exception:  # noqa: BLE001
            r["reasons"] = {}
    return {"matches": rows}


@app.get("/api/position")
def get_position(country: str = "", category: str = ""):
    """我的每款产品相对对位竞品的价格站位（组合层面，一屏看完）。

    ★ 与 /api/matches 的分工：那个是「某一款的对照明细」，必须先选产品；
      这个回答的是「我该先看哪几款」—— 那是组合层面的问题，
      原来只能一款一款点，70 款要点 70 次。
    """
    from .. import boards
    return boards.my_position(country=country, category=category)


@app.post("/api/matches/rebuild")
def rebuild_matches(payload: dict | None = None):
    from ..matching import CompetitorMatcher
    pid = (payload or {}).get("product_id")
    return CompetitorMatcher().rebuild_all(pid)


@app.post("/api/matches/{match_id}/mark")
def mark_match(match_id: int, payload: dict):
    field = "is_confirmed" if payload.get("action") == "confirm" else "is_excluded"
    with db.tx() as conn:
        conn.execute(f"UPDATE competitor_match SET {field}=?, source='manual' WHERE id=?",
                     (1 if payload.get("value", True) else 0, match_id))
    return {"ok": True}


# ---------------------------------------------------------------- 上市看板

@app.get("/api/launches")
def get_launches(days: int = 180, brand: str = ""):
    since = (date.today() - timedelta(days=days)).isoformat()
    # ★★ 只放真上市。情报 Agent 以前把任何没带国家码的新闻都记成 global_launch，
    #   实测 12 条里 10 条是降价 / 评测 / 传闻 / 固件更新 —— 83% 噪声。
    #   噪声混进来会让「距全球首发 N 天」这类推算整体失真，
    #   而且看板上一条假首发和一条真首发长得一模一样。
    #   非上市的那些已改成 promo/rumor/review/software，留在库里但不进这块看板。
    LAUNCH_TYPES = ("global_launch", "country_available")
    where, params = ["le.event_date>=?",
                     f"le.event_type IN ({','.join('?' * len(LAUNCH_TYPES))})"], \
                    [since, *LAUNCH_TYPES]
    if brand:
        where.append("b.name=?"); params.append(brand)
    rows = db.q(f"""
        SELECT le.*, rp.model_name, rp.category_code, rp.global_launch_date,
               b.name AS brand, co.name_zh AS country_name
        FROM launch_event le
        LEFT JOIN rival_product rp ON rp.id=le.rival_product_id
        LEFT JOIN brand b ON b.id=le.brand_id
        LEFT JOIN country co ON co.code=le.country_code
        WHERE {' AND '.join(where)}
        ORDER BY le.event_date DESC LIMIT 500
    """, params)

    # 「全球已发布但拉美还没上市」—— 销售团队 最想要的前瞻清单
    #
    # ★★ 只放**首发日期有出处**的产品（spec_source=gsmarena，厂商公布的发布日）。
    #   此前这里不筛来源，于是把情报 Agent 的推断也当成事实展示：
    #   它拿的是**新闻发布日期**，所以整页的"全球首发"全是当天，
    #   还混进了 "Googlebook"、"Chromebook Plus Spin 714 / phone" 这种
    #   模型从新闻里抽出来的臆造名与错品类。
    #   一个看板宁可少几行，也不能把猜测当事实摆出来 ——
    #   用户照着它做上市决策，错的比空的贵得多。
    pending = db.q("""
        SELECT rp.id, rp.model_name, rp.category_code, rp.global_launch_date,
               b.name AS brand, rp.spec_source,
               (SELECT COUNT(DISTINCT country_code) FROM launch_event
                 WHERE rival_product_id=rp.id AND event_type='country_available')
                 AS latam_countries,
               (SELECT COUNT(DISTINCT country_code) FROM price_obs
                 WHERE rival_product_id=rp.id) AS countries_on_shelf,
               CAST(julianday('now') - julianday(rp.global_launch_date) AS INTEGER)
                 AS days_since_global
        FROM rival_product rp JOIN brand b ON b.id=rp.brand_id
        WHERE rp.global_launch_date IS NOT NULL
          AND rp.global_launch_date >= date('now','-270 day')
          AND rp.spec_source LIKE 'gsmarena%'
        GROUP BY rp.id
        HAVING countries_on_shelf < 6
        ORDER BY rp.global_launch_date DESC LIMIT 100
    """)
    # 被挡掉多少、为什么，如实告诉界面 —— 否则"变少了"会被当成数据丢了
    suppressed = db.q1("""
        SELECT COUNT(*) c FROM rival_product
        WHERE global_launch_date IS NOT NULL
          AND global_launch_date >= date('now','-270 day')
          AND (spec_source IS NULL OR spec_source NOT LIKE 'gsmarena%')""")["c"]
    # 被判为非上市而挡在外面的，要报数 —— 静默过滤等于谎报太平
    filtered = db.q1("""
        SELECT COUNT(*) c FROM launch_event
        WHERE event_date>=? AND event_type NOT IN ('global_launch','country_available')
    """, (since,))
    n_filtered = (filtered or {}).get("c") or 0

    return {"events": rows, "pending_latam": pending,
            "filtered_non_launch": n_filtered,
            "filtered_note": (
                f"另有 {n_filtered} 条情报被判定为**不是上市**（降价促销 / 评测 / "
                f"传闻 / 固件更新），已挡在本看板之外，可在情报流里看到。"
                f"以前这些会被一律记成「全球首发」—— 12 条里有 10 条是噪声。"
            ) if n_filtered else "",
            "suppressed": suppressed,
            "suppressed_note": (
                f"另有 {suppressed} 个产品的首发日期来自情报 Agent 的**新闻推断**"
                f"（取的是新闻发布日期，不是厂商发布日），不足以支撑上市决策，"
                f"已从清单中挡掉。跑 tools/fetch_specs.py 补到有出处的发布日后会自动出现。"
            ) if suppressed else ""}


# ---------------------------------------------------------------- VOC

@app.get("/api/voc")
def get_voc(country: str = "", hot_only: bool = False):
    where, params = ["1=1"], []
    if country:
        where.append("rpf.country_code=?"); params.append(country.upper())
    if hot_only:
        where.append("rpf.is_hot=1")

    profiles = db.q(f"""
        SELECT rpf.*, rp.model_name, b.name AS brand, c.name AS channel
        FROM review_profile rpf
        LEFT JOIN rival_product rp ON rp.id=rpf.rival_product_id
        LEFT JOIN brand b ON b.id=rp.brand_id
        LEFT JOIN channel c ON c.id=rpf.channel_id
        WHERE {' AND '.join(where)}
        ORDER BY rpf.total_reviews DESC LIMIT 120
    """, params)

    insights = db.q("""
        SELECT vi.*, rp.model_name, b.name AS brand, co.name_zh AS country_name
        FROM voc_insight vi
        JOIN rival_product rp ON rp.id=vi.rival_product_id
        JOIN brand b ON b.id=rp.brand_id
        LEFT JOIN country co ON co.code=vi.country_code
        ORDER BY vi.review_count DESC LIMIT 60
    """)
    for i in insights:
        for k in ("praise_points", "complaint_points", "watch_signals"):
            try:
                i[k] = json.loads(i.get(k) or "[]")
            except Exception:  # noqa: BLE001
                i[k] = []

    stats = db.q1("""
        SELECT COUNT(*) total,
               SUM(sentiment='positive') pos, SUM(sentiment='negative') neg,
               SUM(content_zh IS NOT NULL AND content_zh<>'') translated
        FROM review
    """) or {}
    return {"profiles": profiles, "insights": insights, "stats": stats}


# ---------------------------------------------------------------- 情报

@app.get("/api/intel")
def get_intel(days: int = 7, min_importance: int = 0, tag: str = "",
              country: str = "", category: str = ""):
    since = (date.today() - timedelta(days=days)).isoformat()
    where = ["date(d.created_at)>=?", "d.importance>=?"]
    params: list = [since, min_importance]
    if tag:
        where.append("d.tag=?"); params.append(tag)
    if country:
        # 选了国家 = 看**原文点名发生在该国**的事，不是"该国媒体报过的事"
        where.append("d.country_code=? AND d.geo_named=1")
        params.append(country.upper())
    if category:
        where.append("d.category_code=?"); params.append(category)
    rows = db.q(f"""
        SELECT d.*, b.name AS brand, co.name_zh AS country_name
        FROM dynamics d
        LEFT JOIN brand b ON b.id=d.brand_id
        LEFT JOIN country co ON co.code=d.country_code
        WHERE {' AND '.join(where)}
        ORDER BY d.importance DESC, d.published_at DESC LIMIT 300
    """, params)

    # ★ 两块看板：按国家 / 按产业。给的是**当前时间窗内**的真实条数，
    #   不是配置里应该有多少 —— 0 条要能看出来是"那个国家没情报"，
    #   而不是被隐藏。CL 只有 77 条 vs BR 910 条这种偏斜，
    #   不摆出来就会被读成"智利本周很平静"。
    base_where = ["date(d.created_at)>=?", "d.importance>=?"]
    base_params: list = [since, min_importance]
    # ★★ 按国家看板只统计 **geo_named=1（原文点名）** 的条目。
    #   country_code 在抓取时写的是"新闻源所在国"——巴西媒体报一条全球新闻
    #   会被记成"巴西动态"，之前这块看板显示的其实是"从哪国媒体抓来的"，
    #   不是"发生在哪国"。未点名的归入「未点名/全球」一档，如实分开。
    by_country = db.q(f"""
        SELECT CASE WHEN d.geo_named=1 THEN COALESCE(d.country_code,'—')
                    ELSE '—' END AS code,
               CASE WHEN d.geo_named=1 THEN COALESCE(co.name_zh, d.country_code)
                    ELSE '未点名 / 全球' END AS name,
               COUNT(*) AS n,
               SUM(CASE WHEN d.importance>=4 THEN 1 ELSE 0 END) AS important
        FROM dynamics d LEFT JOIN country co ON co.code=d.country_code
        WHERE {' AND '.join(base_where)}
        GROUP BY 1, 2 ORDER BY (code='—'), n DESC""", base_params)
    by_category = db.q(f"""
        SELECT COALESCE(d.category_code,'—') AS code, COUNT(*) AS n,
               SUM(CASE WHEN d.importance>=4 THEN 1 ELSE 0 END) AS important
        FROM dynamics d
        WHERE {' AND '.join(base_where)}
        GROUP BY d.category_code ORDER BY n DESC""", base_params)
    return {"items": rows, "by_country": by_country, "by_category": by_category}


# ---------------------------------------------------------------- Agent 留痕

@app.get("/api/agents")
def get_agents():
    runs = db.q("""
        SELECT ar.*, (SELECT COUNT(*) FROM agent_step WHERE run_id=ar.id) AS steps
        FROM agent_run ar ORDER BY ar.id DESC LIMIT 60
    """)
    plans = db.q("SELECT * FROM crawl_plan ORDER BY id DESC LIMIT 10")
    return {"roster": AGENT_ROSTER, "runs": runs, "plans": plans}


@app.get("/api/agents/{run_id}/steps")
def get_agent_steps(run_id: int):
    run = db.q1("SELECT * FROM agent_run WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "找不到该次运行")
    steps = db.q("SELECT * FROM agent_step WHERE run_id=? ORDER BY step_no", (run_id,))
    return {"run": run, "steps": steps}


# ---------------------------------------------------------------- 运行

@app.get("/api/runs")
def get_runs():
    runs = db.q("SELECT * FROM scrape_run ORDER BY id DESC LIMIT 30")
    for r in runs:
        try:
            r["warnings"] = json.loads(r.get("warnings") or "[]")
        except Exception:  # noqa: BLE001
            r["warnings"] = []
    return {"runs": runs, "task": dict(_TASK)}


@app.get("/api/runs/{run_id}/units")
def get_run_units(run_id: int):
    return {"units": db.q("""
        SELECT su.*, c.name AS channel_name, b.name AS brand_name
        FROM scrape_unit su
        LEFT JOIN channel c ON c.id=su.channel_id
        LEFT JOIN brand b ON b.id=su.brand_id
        WHERE su.run_id=? ORDER BY su.id
    """, (run_id,))}


def acquire_task_slot(name: str) -> bool:
    """占用单任务槽。定时任务与界面共用同一把闸 —— 见 scheduler._job_collect。"""
    with _TASK_LOCK:
        if _TASK["running"]:
            return False
        _TASK.update({"running": True, "name": name, "progress": "启动中…",
                      "result": None, "error": "", "started_at": time.time()})
        return True


def release_task_slot(result=None, error: str = "") -> None:
    with _TASK_LOCK:
        _TASK["running"] = False
        _TASK["progress"] = "已结束"
        if result is not None:
            _TASK["result"] = result
        if error:
            _TASK["error"] = error


@app.post("/api/run/{action}")
def trigger_run(action: str, payload: dict | None = None):
    payload = payload or {}
    if not acquire_task_slot(action):
        raise HTTPException(409, f"已有任务在跑：{_TASK['name']}")

    def worker():
        # 每个 Agent 的每一步都回传到任务进度：界面上不会再出现
        # "启动中…" 挂 20 分钟、分不清是卡死还是在干活的情况。
        from .. import livelog as _livelog
        from ..agents.base import set_progress_sink
        _sink = lambda s: _TASK.__setitem__("progress", s)  # noqa: E731
        set_progress_sink(_sink)
        # 采集阶段不走 Agent 的 log_step，进度得从 livelog 引过来 ——
        # 否则几小时的采集期间任务状态一动不动，和卡死分不出来
        _livelog.set_progress_sink(_sink)
        try:
            if action == "collect":
                from ..agents import Orchestrator
                orch = Orchestrator(
                    mode=payload.get("mode", "manual"),
                    categories=payload.get("categories"),
                    countries=payload.get("countries"),
                    dry_run=bool(payload.get("dry_run")))
                _TASK["progress"] = "主 Agent 研判中…"
                _TASK["result"] = orch.run_daily()
            elif action == "doctor":
                _TASK["progress"] = "渠道体检中…"
                _TASK["result"] = _run_doctor(payload)
            elif action == "audit":
                from ..agents import LLMClient, PriceAuditAgent
                cfg = config.load_runtime()["agents"]
                _TASK["result"] = PriceAuditAgent(LLMClient(cfg), cfg).run()
            elif action == "match":
                from ..matching import CompetitorMatcher
                _TASK["result"] = CompetitorMatcher().rebuild_all()
            elif action == "intel":
                from ..agents import IntelAgent, LLMClient
                cfg = config.load_runtime()["agents"]
                _TASK["result"] = IntelAgent(LLMClient(cfg), cfg).run()
            elif action == "voc":
                from ..agents import LLMClient, VocAgent
                cfg = config.load_runtime()["agents"]
                _TASK["result"] = VocAgent(LLMClient(cfg), cfg).run()
            elif action == "pricemove":
                from ..agents import LLMClient, PriceMoveAgent
                cfg = config.load_runtime()["agents"]
                a = PriceMoveAgent(LLMClient(cfg), cfg)
                _TASK["progress"] = "检测价格变动中…"
                out = {}
                for row in db.q("SELECT DISTINCT obs_date FROM price_obs "
                                "ORDER BY obs_date DESC LIMIT 30"):
                    r = a.run(obs_date=row["obs_date"])
                    for k, v in r.items():
                        out[k] = out.get(k, 0) + v
                _TASK["result"] = out
            elif action == "strategy":
                from ..agents import LLMClient, StrategyAgent
                cfg = config.load_runtime()["agents"]
                _TASK["progress"] = "分析价格策略中…"
                _TASK["result"] = StrategyAgent(LLMClient(cfg), cfg).run(
                    days=int((payload or {}).get("days", 14)))
            elif action == "brandintel":
                from ..agents import BrandIntelAgent, LLMClient
                cfg = config.load_runtime()["agents"]
                _TASK["progress"] = "分析品牌动态中…"
                _TASK["result"] = BrandIntelAgent(LLMClient(cfg), cfg).run(
                    days=int((payload or {}).get("days", 7)))
            elif action == "weekly":
                from ..agents import LLMClient, WeeklyReportAgent
                cfg = config.load_runtime()["agents"]
                _TASK["progress"] = "生成周报中…"
                _pl = payload or {}
                _TASK["result"] = WeeklyReportAgent(LLMClient(cfg), cfg).run(
                    week_start=_pl.get("period") or None,
                    scope=_pl.get("scope", "all"),
                    category=_pl.get("category", ""))
            elif action == "specs":
                from ..agents import LLMClient, SpecFillerAgent
                cfg = config.load_runtime()["agents"]
                _TASK["progress"] = "补全产品规格中…"
                _TASK["result"] = SpecFillerAgent(LLMClient(cfg), cfg).run(
                    scope=(payload or {}).get("scope", "both"))
            else:
                _TASK["error"] = f"未知任务：{action}"
        except Exception as e:  # noqa: BLE001
            log.exception("后台任务失败")
            _TASK["error"] = f"{type(e).__name__}: {str(e)[:460]}"
        finally:
            set_progress_sink(None)
            _livelog.set_progress_sink(None)
            release_task_slot()

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "started": action}


def _run_doctor(payload: dict) -> dict:
    from ..scraping.engine import ScrapeEngine
    from ..scraping.health import check_channel, save_health

    where, params = "WHERE c.enabled=1", []
    if payload.get("country"):
        where += " AND c.country_code=?"
        params.append(str(payload["country"]).upper())
    channels = db.q(f"""SELECT c.* FROM channel c JOIN country co ON co.code=c.country_code
                        {where} ORDER BY co.sort_order, c.priority""", params)
    countries = {c["code"]: c for c in db.q("SELECT * FROM country")}
    token = db.get_setting("meli_access_token", "")
    results = []
    with ScrapeEngine(config.load_runtime()["scrape"]) as engine:
        for i, ch in enumerate(channels, 1):
            _TASK["progress"] = f"体检 {i}/{len(channels)}：{ch['name']}"
            results.append(check_channel(engine, ch, countries[ch["country_code"]], token))
    save_health(results)
    return {"checked": len(results),
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "results": results}


@app.get("/api/task")
def get_task():
    return dict(_TASK)


# ---------------------------------------------------------------- 实时过程

@app.get("/api/live")
def live_events(after: int = 0):
    """轮询式取最近事件。前端每 1.5 秒拉一次，比 SSE 简单且断线自愈。"""
    from .. import livelog
    task = dict(_TASK)
    # ★ 进度用「最近一条活动」实时覆盖。
    #   原来 progress 只在任务启动时设一次，之后一动不动，
    #   用户看进度条不动就判断"根本没在跑" —— 静止的进度指示比没有更糟。
    #
    #   但只放"最近一条活动"还不够：单个渠道要跑好几分钟，
    #   那行字也不变。所以再拼上**一直在走的**运行时长与累计入库条数，
    #   让"它还活着"这件事一眼可见。
    if task.get("running"):
        parts = []
        started = task.get("started_at")
        if started:
            mins = (time.time() - started) / 60
            parts.append(f"已运行 {mins:.1f} 分")
        try:
            n = db.q1("SELECT COUNT(*) c FROM price_obs WHERE obs_date=?",
                      (db.today(),))["c"]
            parts.append(f"今日已抓 {n} 条")
        except Exception:  # noqa: BLE001
            pass
        act = livelog.current_activity()
        if act:
            parts.append(act)
        if parts:
            task["progress"] = " ｜ ".join(parts)
    return {"events": livelog.recent(after), "task": task,
            "stats": livelog.stats()}


@app.post("/api/live/clear")
def live_clear():
    from .. import livelog
    livelog.clear()
    return {"ok": True}


# ---------------------------------------------------------------- Telegram

@app.post("/api/telegram/test")
def telegram_test():
    from ..notify import send_telegram
    ok, msg = send_telegram("✅ 拉美竞品情报中枢 —— 测试消息，连接正常。")
    return {"ok": ok, "message": msg}


@app.post("/api/telegram/brief")
def telegram_brief():
    from ..notify import send_daily_brief
    return send_daily_brief()


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
