# -*- coding: utf-8 -*-
"""Telegram 双向交互 —— 你发消息，它查库回答。

之前只做了单向推送（sendMessage），所以你发什么都没反应。
Telegram bot 要收消息必须主动轮询 getUpdates（或架 webhook，
但那需要公网地址，本机跑不合适），这里用长轮询。

支持：
  /status          当前运行状态、今日抓了多少
  /price 型号      查该型号在各国各渠道的价格
  /run             触发一次采集
  /brief           立刻发今日简报
  其它自然语言     交给 LLM 理解意图 → 转成预定义查询

★ 安全边界（Telegram 消息是外部输入，当数据不当指令）：
  1. **只响应配置里的那个 chat_id** —— 别人找到这个 bot 也使唤不动它
  2. LLM 只用来**理解意图并抽取参数**，不生成 SQL、不执行任意代码
  3. 所有查询都是参数化的预定义模板
  4. 消息内容里的任何"指令"都不会被当成命令执行 —— 它只是查询词
"""
from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from . import db

log = logging.getLogger("tgbot")

API = "https://api.telegram.org/bot{token}/{method}"
_stop = threading.Event()
_thread: threading.Thread | None = None


def _call(token: str, method: str, **params):
    """调 Telegram API。

    ★ 不能只看 HTTP 状态码：Telegram 对参数错误（比如 HTML 标签没闭合）
      返回的响应体里 ok=false，而我们如果只判 status_code 就会以为发成功了。
      发送失败必须记下来，否则表现是"日志说发了、用户没收到"。
    """
    try:
        r = httpx.post(API.format(token=token, method=method),
                       json=params, timeout=40)
        j = r.json() if r.content else {}
        if not j.get("ok", False):
            log.warning("tg %s 返回 ok=false：%s", method,
                        str(j.get("description"))[:160])
            return None
        return j
    except Exception as e:  # noqa: BLE001
        log.debug("tg %s 失败: %s", method, str(e)[:80])
        return None


def _send(token: str, chat_id: str, text: str) -> None:
    for chunk in _split(text, 3800):
        _call(token, "sendMessage", chat_id=chat_id, text=chunk,
              parse_mode="HTML", disable_web_page_preview=True)


def _split(text: str, n: int):
    """Telegram 单条上限 4096 字符，按行切分不截断句子。"""
    out, buf = [], ""
    for line in (text or "").split("\n"):
        if len(buf) + len(line) + 1 > n:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out or [""]


# ---------------------------------------------------------------- 查询

def _q_status() -> str:
    today = db.today()
    n = db.q1("SELECT COUNT(*) c FROM price_obs WHERE obs_date=?", (today,))["c"]
    total = db.q1("SELECT COUNT(*) c FROM price_obs")["c"]
    run = db.q1("SELECT * FROM scrape_run ORDER BY id DESC LIMIT 1")
    units = db.q("""SELECT status, COUNT(*) n FROM scrape_unit
                    WHERE date(created_at)=? GROUP BY status""", (today,))
    lines = [f"<b>📊 状态 · {today}</b>", "",
             f"今日入库 <b>{n}</b> 条（累计 {total}）"]
    if run:
        lines.append(f"最近批次 #{run['id']} · {run['mode']} · {run['status']}")
    if units:
        lines.append("")
        lines.append("采集单元：" + "  ".join(
            f"{u['status']} {u['n']}" for u in units))
    ch = db.q("""SELECT c.country_code cc, COUNT(DISTINCT po.id) n
                 FROM price_obs po JOIN channel c ON c.id=po.channel_id
                 WHERE po.obs_date=? GROUP BY c.country_code ORDER BY n DESC""", (today,))
    if ch:
        lines.append("")
        lines.append("分国：" + "  ".join(f"{c['cc']} {c['n']}" for c in ch))
    return "\n".join(lines)


def _q_price(keyword: str) -> str:
    kw = f"%{(keyword or '').strip()}%"
    rows = db.q("""
        SELECT po.title, po.sale_price, po.list_price, po.discount_pct,
               po.currency, po.country_code cc, c.name ch,
               po.seller_kind, po.ram_gb, po.rom_gb, po.obs_date
        FROM price_obs po JOIN channel c ON c.id=po.channel_id
        WHERE (po.title LIKE ? OR po.model_guess LIKE ? OR po.sku_code LIKE ?)
          AND po.sale_price IS NOT NULL AND po.product_kind <> 'accessory'
        ORDER BY po.obs_date DESC, po.sale_price
        LIMIT 25
    """, (kw, kw, kw))
    if not rows:
        return f"没找到「{keyword}」。试试更短的关键词，比如「S26」或「iPad Air」。"

    kind_zh = {"self_operated": "自营", "brand_official": "品牌店",
               "third_party": "第三方", "unknown": "未知"}
    out = [f"<b>💰 「{keyword}」价格</b>（{len(rows)} 条）", ""]
    by_cc: dict[str, list] = {}
    for r in rows:
        by_cc.setdefault(r["cc"], []).append(r)
    for cc, items in by_cc.items():
        out.append(f"<b>{cc}</b>")
        for r in items[:8]:
            spec = ""
            if r["ram_gb"] or r["rom_gb"]:
                spec = f" {r['ram_gb'] or '?'}+{r['rom_gb'] or '?'}G"
            off = f" (-{r['discount_pct']:.0f}%)" if r["discount_pct"] else ""
            out.append(f"  {r['sale_price']:,.0f} {r['currency']}{off} · "
                       f"{r['ch']} · {kind_zh.get(r['seller_kind'], '')}")
            out.append(f"    <i>{r['title'][:52]}{spec}</i>")
        out.append("")
    return "\n".join(out)


def _q_channels() -> str:
    rows = db.q("""SELECT c.country_code cc, c.name, ch.verdict,
                          COUNT(po.id) n
                   FROM channel c
                   LEFT JOIN channel_health ch ON ch.channel_id=c.id
                   LEFT JOIN price_obs po ON po.channel_id=c.id
                        AND po.obs_date=date('now')
                   WHERE c.enabled=1 GROUP BY c.id
                   ORDER BY n DESC, c.country_code""")
    zh = {"healthy": "✅", "degraded": "⚠️", "selector_broken": "🔧",
          "rate_limited": "🚧", "need_login": "🔑", "need_captcha": "👤",
          "empty": "❓", "throttled": "🚧",
          "dead": "❌", None: "·"}
    out = ["<b>📡 渠道状态</b>", ""]
    for r in rows:
        out.append(f"{zh.get(r['verdict'], '·')} {r['cc']} {r['name'][:22]}"
                   f"{('  ' + str(r['n']) + ' 条') if r['n'] else ''}")
    return "\n".join(out)


HELP = """<b>🤖 拉美竞品情报中枢</b>

/status  运行状态与今日条数
/price 型号   查价格，例：<code>/price S26 Ultra</code>
/channels 各渠道状态
/brief   立刻发今日简报
/run     触发一次采集
/help    这条

也可以直接问，比如「墨西哥 iPad Air 多少钱」。"""


_QUESTION_WORDS = re.compile(
    r"(多少钱|什么价|价格是?|价钱|卖多少|多少|怎么样|咋样|如何|"
    r"查一下|查查|帮我查|看一下|看看|的价格|现在|目前|在?墨西哥|在?巴西|"
    r"在?智利|在?哥伦比亚|在?秘鲁|在?阿根廷|呢|吗|啊|了|的|\?|？|,|，|。)",
    re.I)


def _strip_question_words(t: str) -> str:
    """把问句词剥掉，剩下的当型号。"「S26 Ultra 多少钱」→「S26 Ultra」"""
    s = _QUESTION_WORDS.sub(" ", t or "")
    s = re.sub(r"\s{2,}", " ", s).strip()
    # 太短或纯中文（没有型号常见的字母数字）就别当型号查
    if len(s) < 2 or not re.search(r"[A-Za-z0-9]", s):
        return ""
    return s[:40]


def _handle(text: str, llm=None) -> str:
    """把一条消息变成回复。★ 消息内容只当查询词，绝不当命令执行。"""
    t = (text or "").strip()
    low = t.lower()

    if low.startswith("/start") or low.startswith("/help"):
        return HELP
    if low.startswith("/status"):
        return _q_status()
    if low.startswith("/channels"):
        return _q_channels()
    if low.startswith("/price"):
        return _q_price(t[6:].strip())
    if low.startswith("/brief"):
        from .notify import build_daily_brief
        return build_daily_brief()
    if low.startswith("/run"):
        try:
            import httpx as _h
            _h.post("http://127.0.0.1:8765/api/run/collect",
                    json={"mode": "manual"}, timeout=10)
            return "已触发采集。用 /status 看进度。"
        except Exception as e:  # noqa: BLE001
            return f"触发失败：{type(e).__name__}。界面没开着？"

    # ★★ 其余全部交给对话 Agent —— 由 LLM 自己决定查什么、怎么查、怎么答。
    #
    #   用户明确要求："根据 LLM 的理解，组织软件里的总 Agent 查询数据库，
    #   再输出，而不是完全的范式回答，一定要灵活"。
    #
    #   Agent 手里有一组参数化查询工具 + 知识库 RAG，可以多轮调用：
    #   先查价格发现异常 → 再查知识库找原因 → 组织成一段话。
    #   固定分支做不到这个。
    if llm and llm.available():
        try:
            from .agents.chat import ask
            r = ask(t, llm)
            txt = (r.get("text") or "").strip()
            if txt:
                used = r.get("tools_used") or []
                foot = (f"\n\n<i>（查了 {'、'.join(used)}）</i>" if used else "")
                return txt + foot
        except Exception as e:  # noqa: BLE001
            log.exception("对话 Agent 失败，退回规则回答")

    # ---- 以下是 LLM 不可用时的兜底：纯规则，保证基本可用 ----
    if any(w in low for w in ("咋样", "怎么样", "进度", "跑完", "状态", "多少条")):
        return _q_status()
    if any(w in low for w in ("渠道", "网站", "站点", "哪些通", "健康")):
        return _q_channels()
    if any(w in low for w in ("简报", "日报", "报告", "brief")):
        from .notify import build_daily_brief
        return build_daily_brief()
    stripped = _strip_question_words(t)
    if stripped and len(stripped) >= 2:
        r = _q_price(stripped)
        if "没找到" not in r:
            return r
    return ("（模型不可用，只能走规则）试试：\n"
            "  · 「咋样了」— 运行状态\n"
            "  · 「渠道」— 各站情况\n"
            "  · 「S26 Ultra 多少钱」— 查价格\n\n" + HELP)


# ---------------------------------------------------------------- 轮询

def _loop(token: str, chat_id: str, llm) -> None:
    offset = 0
    log.info("Telegram 收信已启动（只响应 chat_id=%s）", chat_id)
    while not _stop.is_set():
        try:
            r = _call(token, "getUpdates", offset=offset, timeout=30)
            for up in (r or {}).get("result", []):
                offset = max(offset, up.get("update_id", 0) + 1)
                msg = up.get("message") or up.get("edited_message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = msg.get("text") or ""
                if not text:
                    continue
                # ★ 只认配置里的那个 chat_id。别人找到这个 bot 也使唤不动。
                if chat_id and chat != str(chat_id):
                    log.warning("忽略来自未授权 chat_id=%s 的消息", chat)
                    continue
                log.info("收到指令: %s", text[:60])
                try:
                    reply = _handle(text, llm)
                except Exception as e:  # noqa: BLE001
                    log.exception("处理消息失败")
                    reply = f"查询出错：{type(e).__name__}: {str(e)[:120]}"
                _send(token, chat, reply)
        except Exception:  # noqa: BLE001
            log.debug("轮询异常，稍后重试", exc_info=True)
            time.sleep(5)


def start(llm=None) -> bool:
    """启动收信线程。没配 token/chat_id 就不启动。"""
    global _thread
    token = db.get_setting("telegram_bot_token", "")
    chat_id = db.get_setting("telegram_chat_id", "")
    if not token or not chat_id:
        log.info("Telegram 未配置完整（需要 token + chat_id），收信未启动")
        return False
    if _thread and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(token, chat_id, llm),
                              daemon=True, name="tgbot")
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
