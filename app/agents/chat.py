# -*- coding: utf-8 -*-
"""对话 Agent —— 用自然语言问任何事，它自己决定查什么、怎么查、怎么答。

用户要求（2026-08-11）：
  "我就想让你依赖 LLM，根据 LLM 的理解，组织软件里的总 Agent 查询数据库，
   然后再输出，而不是只是完全的范式回答，一定要灵活，
   甚至你可以内置 Hermes 来帮助执行一些动作，让软件自己动起来"

架构（ReAct 式多轮工具调用）：

    你的问题
       ↓
    LLM 看到【工具清单 + 数据现状摘要】，决定调哪个工具、传什么参数
       ↓
    执行工具（参数化查询 / 触发动作）
       ↓
    结果回给 LLM ——它可以决定再查一次（最多 MAX_STEPS 轮）
       ↓
    LLM 用自然语言组织回答

★ 为什么给工具而不给 SQL：
  让模型直接写 SQL 看着更灵活，但它会写出全表扫描、错误 JOIN，甚至删数据；
  而 Telegram 消息是**外部输入**，模型一旦被诱导就直接打到库上。
  这里的做法是：**SQL 由我写死并参数化，模型只决定调哪个工具、传什么筛选条件**。
  灵活性来自参数组合与多轮调用，不是来自放开 SQL。

★ 动作类工具（让软件自己动起来）单独标记 `is_action`，
  它们会改变系统状态（触发采集等），因此：
    · 只有授权 chat_id 能触发（调用方保证）
    · 每次执行都写日志
    · 不提供任何"执行任意命令/改配置/删数据"的工具
"""
from __future__ import annotations

import json
import logging

from .. import db

log = logging.getLogger("chat_agent")

MAX_STEPS = 4          # 最多几轮工具调用，防止模型自己转圈
MAX_ROWS = 40          # 单次查询返回给模型的最大行数（控制 token）


# ---------------------------------------------------------------- 工具实现

def _t_search_prices(keyword: str = "", country: str = "", brand: str = "",
                     channel: str = "", seller_kind: str = "",
                     category: str = "", min_price: float = None,
                     max_price: float = None, only_discounted: bool = False,
                     order: str = "price_asc", limit: int = 20) -> dict:
    """按任意组合筛价格。SQL 写死，模型只填筛选条件。"""
    where, params = ["po.sale_price IS NOT NULL"], []
    if keyword:
        where.append("(po.title LIKE ? OR po.model_guess LIKE ? OR po.sku_code LIKE ?)")
        params += [f"%{keyword}%"] * 3
    if country:
        where.append("po.country_code = ?")
        params.append(country.upper()[:2])
    if brand:
        where.append("b.name LIKE ?")
        params.append(f"%{brand}%")
    if channel:
        where.append("c.name LIKE ?")
        params.append(f"%{channel}%")
    if seller_kind:
        where.append("po.seller_kind = ?")
        params.append(seller_kind)
    if category:
        where.append("po.category_code = ?")
        params.append(category)
    if min_price is not None:
        where.append("po.sale_price >= ?")
        params.append(float(min_price))
    if max_price is not None:
        where.append("po.sale_price <= ?")
        params.append(float(max_price))
    if only_discounted:
        where.append("po.discount_pct > 0")
    where.append("po.product_kind <> 'accessory'")

    order_sql = {"price_asc": "po.sale_price ASC", "price_desc": "po.sale_price DESC",
                 "discount_desc": "po.discount_pct DESC",
                 "newest": "po.obs_date DESC"}.get(order, "po.sale_price ASC")
    rows = db.q(f"""
        SELECT po.obs_date, po.country_code, c.name AS channel, b.name AS brand,
               po.title, po.sku_code, po.model_guess, po.ram_gb, po.rom_gb,
               po.sale_price, po.list_price, po.discount_pct, po.currency,
               po.seller_kind, po.seller_name, po.condition, po.url
        FROM price_obs po
        JOIN channel c ON c.id = po.channel_id
        LEFT JOIN brand b ON b.id = po.brand_id
        WHERE {' AND '.join(where)}
        ORDER BY {order_sql} LIMIT ?
    """, (*params, min(int(limit or 20), MAX_ROWS)))
    return {"count": len(rows), "rows": rows}


def _t_compare_channels(model_keyword: str, country: str = "") -> dict:
    """同一型号在各渠道/各卖家身份下的价格对比 —— 这是本系统的核心产出。"""
    where, params = ["po.sale_price IS NOT NULL",
                     "(po.title LIKE ? OR po.model_guess LIKE ? OR po.sku_code LIKE ?)"], []
    params += [f"%{model_keyword}%"] * 3
    if country:
        where.append("po.country_code = ?")
        params.append(country.upper()[:2])
    where.append("po.product_kind <> 'accessory'")
    rows = db.q(f"""
        SELECT po.country_code, c.name AS channel, po.seller_kind, po.seller_name,
               po.sale_price, po.list_price, po.discount_pct, po.currency,
               po.title, po.condition, po.rom_gb
        FROM price_obs po JOIN channel c ON c.id = po.channel_id
        WHERE {' AND '.join(where)}
        ORDER BY po.country_code, po.sale_price
    """, params)
    by_cc = {}
    for r in rows:
        by_cc.setdefault(r["country_code"], []).append(r)
    spread = {}
    for cc, items in by_cc.items():
        ps = [i["sale_price"] for i in items if i["sale_price"]]
        if len(ps) > 1:
            spread[cc] = {"min": min(ps), "max": max(ps),
                          "spread_pct": round((max(ps) - min(ps)) / min(ps) * 100, 1),
                          "n": len(ps)}
    return {"count": len(rows), "rows": rows[:MAX_ROWS], "spread_by_country": spread}


def _t_run_status() -> dict:
    today = db.today()
    return {
        "today": today,
        "rows_today": db.q1("SELECT COUNT(*) c FROM price_obs WHERE obs_date=?",
                            (today,))["c"],
        "rows_total": db.q1("SELECT COUNT(*) c FROM price_obs")["c"],
        "last_run": db.q1("SELECT id, mode, status, started_at, finished_at "
                          "FROM scrape_run ORDER BY id DESC LIMIT 1"),
        "units_today": db.q("""SELECT status, COUNT(*) n FROM scrape_unit
                               WHERE date(created_at)=? GROUP BY status""", (today,)),
        "by_country": db.q("""SELECT country_code, COUNT(*) n FROM price_obs
                              WHERE obs_date=? GROUP BY country_code
                              ORDER BY n DESC""", (today,)),
    }


def _t_channel_health() -> dict:
    return {"channels": db.q("""
        SELECT c.country_code, c.name, c.adapter, c.enabled, ch.verdict, ch.action,
               (SELECT COUNT(*) FROM price_obs po WHERE po.channel_id=c.id
                AND po.obs_date=date('now')) AS rows_today
        FROM channel c LEFT JOIN channel_health ch ON ch.channel_id=c.id
        ORDER BY c.country_code, c.priority""")}


def _t_my_products(keyword: str = "", category: str = "", limit: int = 30) -> dict:
    where, params = ["status='active'"], []
    if keyword:
        where.append("(marketing_name LIKE ? OR internal_code LIKE ?)")
        params += [f"%{keyword}%"] * 2
    if category:
        where.append("category_code = ?")
        params.append(category)
    return {"rows": db.q(
        f"""SELECT marketing_name, internal_code, category_code, series, chipset, screen
            FROM my_product WHERE {' AND '.join(where)}
            ORDER BY category_code, marketing_name LIMIT ?""",
        (*params, min(int(limit or 30), MAX_ROWS)))}


def _t_competitors(my_product: str = "", country: str = "", limit: int = 20) -> dict:
    where, params = ["1=1"], []
    if my_product:
        where.append("mp.marketing_name LIKE ?")
        params.append(f"%{my_product}%")
    if country:
        where.append("cm.country_code = ?")
        params.append(country.upper()[:2])
    return {"rows": db.q(
        f"""SELECT mp.marketing_name AS ours, rp.model_name AS rival,
                   b.name AS rival_brand, cm.country_code, cm.total_score,
                   cm.price_gap_pct, cm.spec_score, cm.reasons
            FROM competitor_match cm
            JOIN my_product mp ON mp.id = cm.my_product_id
            JOIN rival_product rp ON rp.id = cm.rival_product_id
            JOIN brand b ON b.id = rp.brand_id
            WHERE {' AND '.join(where)} AND cm.is_excluded = 0
            ORDER BY cm.total_score DESC LIMIT ?""",
        (*params, min(int(limit or 20), MAX_ROWS)))}


def _t_new_launches(days: int = 30, country: str = "", limit: int = 20) -> dict:
    where, params = ["po.obs_date >= date('now', ?)"], [f"-{int(days or 30)} day"]
    if country:
        where.append("po.country_code = ?")
        params.append(country.upper()[:2])
    return {"rows": db.q(
        f"""SELECT MIN(po.obs_date) AS first_seen, po.country_code,
                   rp.model_name, b.name AS brand, MIN(po.sale_price) AS price,
                   po.currency, COUNT(DISTINCT po.channel_id) AS channels
            FROM price_obs po
            JOIN rival_product rp ON rp.id = po.rival_product_id
            JOIN brand b ON b.id = rp.brand_id
            WHERE {' AND '.join(where)}
            GROUP BY rp.id, po.country_code
            ORDER BY first_seen DESC LIMIT ?""",
        (*params, min(int(limit or 20), MAX_ROWS)))}


def _t_trigger_collect(categories: str = "", countries: str = "") -> dict:
    """★ 动作类：触发一次采集，让软件自己动起来。"""
    import httpx
    payload = {"mode": "manual"}
    if categories:
        payload["categories"] = [c.strip() for c in categories.split(",") if c.strip()]
    if countries:
        payload["countries"] = [c.strip().upper() for c in countries.split(",") if c.strip()]
    try:
        r = httpx.post("http://127.0.0.1:8765/api/run/collect", json=payload, timeout=15)
        return {"ok": r.status_code == 200, "detail": r.text[:200], "payload": payload}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def _t_trigger_doctor(country: str = "", limit: int = 6) -> dict:
    """★ 动作类：体检渠道，看哪些还通。"""
    import httpx
    try:
        r = httpx.post("http://127.0.0.1:8765/api/run/doctor",
                       json={"country": country.upper() if country else None,
                             "limit": int(limit or 6)}, timeout=15)
        return {"ok": r.status_code == 200, "detail": r.text[:200]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


# ---------------------------------------------------------------- 工具注册表

def _t_search_knowledge(query: str, k: int = 5) -> dict:
    """★ RAG：检索 Obsidian 知识库。

    数据库里只有「数字」，知识库里才有「为什么」——
    渠道 URL 怎么破解的、卖家身份怎么判、踩过哪些坑、某个结论的依据。
    问「Coppel 为什么串味」「Sears 参数名是什么」这类问题走这里。
    """
    from .. import rag
    from ..config import load_runtime
    from .llm import LLMClient
    hits = rag.search(query, k=min(int(k or 5), 8),
                      llm=LLMClient(load_runtime()["agents"]))
    return {"count": len(hits),
            "hits": [{"来源": f"{h['title']} / {h['heading']}",
                      "内容": h["text"]} for h in hits]}


TOOLS = {
    "search_knowledge": {
        "fn": _t_search_knowledge, "is_action": False,
        "desc": "★检索知识库(Obsidian)。回答「为什么/怎么做/依据是什么」类问题："
                "渠道破解记录、卖家身份判据、踩过的坑、架构决策。"
                "参数：query(问题) k(返回条数)。"
                "数据库里只有数字，原因和依据在这里。",
    },
    "search_prices": {
        "fn": _t_search_prices, "is_action": False,
        "desc": "按任意条件搜价格。可组合：keyword(型号关键词) country(MX/BR/CO/CL/PE/AR) "
                "brand channel seller_kind(self_operated自营/brand_official品牌店/third_party第三方) "
                "category(phone/tablet/wearable/audio/pc) min_price max_price "
                "only_discounted(只看打折的) order(price_asc/price_desc/discount_desc/newest) limit",
    },
    "compare_channels": {
        "fn": _t_compare_channels, "is_action": False,
        "desc": "同一型号在各渠道/各卖家身份下的价格对比，并给出每国的价差百分比。"
                "参数：model_keyword(必填) country(可选)。问「某型号哪里便宜」用这个。",
    },
    "run_status": {
        "fn": _t_run_status, "is_action": False,
        "desc": "当前运行状态：今日入库条数、最近批次、各国分布、采集单元状态。无参数。",
    },
    "channel_health": {
        "fn": _t_channel_health, "is_action": False,
        "desc": "所有渠道的健康状态与今日抓取条数。无参数。问「哪些站通/不通」用这个。",
    },
    "my_products": {
        "fn": _t_my_products, "is_action": False,
        "desc": "查我方(Acme)产品清单。参数：keyword category limit。",
    },
    "competitors": {
        "fn": _t_competitors, "is_action": False,
        "desc": "查某个我方产品的竞品匹配结果。参数：my_product(我方产品名) country limit。",
    },
    "new_launches": {
        "fn": _t_new_launches, "is_action": False,
        "desc": "最近出现的新品(首次被抓到)。参数：days country limit。",
    },
    "trigger_collect": {
        "fn": _t_trigger_collect, "is_action": True,
        "desc": "★动作：立刻触发一次采集。参数：categories(逗号分隔，如 tablet,phone) "
                "countries(逗号分隔，如 MX,BR)。用户明确说「跑一次/抓一下」才调。",
    },
    "trigger_doctor": {
        "fn": _t_trigger_doctor, "is_action": True,
        "desc": "★动作：体检渠道可用性。参数：country limit。用户问「测一下哪些站还能用」才调。",
    },
}


def _tools_spec() -> str:
    return "\n".join(f"- {n}: {t['desc']}" for n, t in TOOLS.items())


SYSTEM = """你是「拉美竞品情报中枢」的对话助手，帮助Acme拉美 销售团队 分析竞品价格。

你可以调用工具查数据库。**每次回复必须是一个 JSON 对象**，两种形式之一：

调用工具：
{"action":"call","tool":"工具名","args":{...},"why":"一句话说明为什么调这个"}

给出最终答案：
{"action":"answer","text":"给用户看的回答"}

可用工具：
%TOOLS%

规则：
1. 先想清楚要什么数据，再调工具。可以连续调多次（最多 4 次）来交叉验证或补充。
2. 拿到数据后用**中文**组织回答，要具体：带上数字、渠道名、卖家身份、币种。
3. 价格必须标明币种和国家 —— 跨国比较绝对价格是无意义的（币种不同）。
4. 卖家身份很重要：self_operated=渠道自营、brand_official=品牌官方店、
   third_party=第三方(常有溢价/翻新，要提醒用户)。
5. 如果数据是空的，直说没有数据，并说明可能原因（该渠道没上架/还没抓到），
   **不要编造数字**。
6. 回答要简短好读，适合在手机上看。可以用 <b>粗标</b> 和换行，不要用 Markdown 表格。
7. 动作类工具(★)只在用户明确要求执行时才调。

现在的数据概况：%CONTEXT%"""


def _context() -> str:
    try:
        n = db.q1("SELECT COUNT(*) c FROM price_obs")["c"]
        today = db.q1("SELECT COUNT(*) c FROM price_obs WHERE obs_date=date('now')")["c"]
        cc = db.q("SELECT country_code, COUNT(*) n FROM price_obs GROUP BY country_code")
        return (f"库里共 {n} 条价格观测（今日 {today} 条），"
                f"分国：{', '.join(f'{c[chr(39)+chr(39)] if False else c['country_code']}{c['n']}' for c in cc) or '暂无'}。")
    except Exception:  # noqa: BLE001
        return "（数据概况读取失败）"


def ask(question: str, llm, max_steps: int = MAX_STEPS) -> dict:
    """跑一轮对话。返回 {text, steps, tools_used}。"""
    if not (llm and llm.available()):
        return {"text": "未配置 MiniMax API Key，对话功能不可用。",
                "steps": 0, "tools_used": []}

    system = SYSTEM.replace("%TOOLS%", _tools_spec()).replace("%CONTEXT%", _context())
    transcript = [f"用户问：{question}"]
    used = []

    for step in range(max_steps):
        parsed, raw, _ = llm.chat_json("\n\n".join(transcript), system=system,
                                       default=None)
        if not isinstance(parsed, dict):
            log.warning("模型返回非 JSON（第 %d 轮）：%s", step + 1, str(raw)[:200])
            return {"text": (raw or "").strip()[:1500] or "模型没有返回有效内容。",
                    "steps": step + 1, "tools_used": used}

        act = parsed.get("action")
        if act == "answer":
            return {"text": str(parsed.get("text") or "").strip() or "（空回答）",
                    "steps": step + 1, "tools_used": used}

        if act != "call":
            return {"text": json.dumps(parsed, ensure_ascii=False)[:1200],
                    "steps": step + 1, "tools_used": used}

        name = str(parsed.get("tool") or "")
        spec = TOOLS.get(name)
        if not spec:
            transcript.append(f"（工具 {name} 不存在。可用：{', '.join(TOOLS)}）")
            continue

        args = parsed.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        # ★ 只传工具函数签名里有的参数，模型编出来的多余参数直接丢掉
        import inspect
        allowed = set(inspect.signature(spec["fn"]).parameters)
        args = {k: v for k, v in args.items() if k in allowed}

        log.info("对话Agent 调用 %s(%s) —— %s", name, args, parsed.get("why", ""))
        used.append(name)
        try:
            result = spec["fn"](**args)
        except Exception as e:  # noqa: BLE001
            log.exception("工具 %s 执行失败", name)
            result = {"error": f"{type(e).__name__}: {str(e)[:160]}"}

        blob = json.dumps(result, ensure_ascii=False, default=str)
        if len(blob) > 6000:
            blob = blob[:6000] + " …(已截断)"
        transcript.append(f"你调用了 {name}({json.dumps(args, ensure_ascii=False)})，"
                          f"结果：{blob}")

    return {"text": "查了几轮还是没能给出结论，换个问法试试？",
            "steps": max_steps, "tools_used": used}
