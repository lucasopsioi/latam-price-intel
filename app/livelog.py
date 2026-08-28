# -*- coding: utf-8 -*-
"""实时事件流 —— 让界面能看到"现在正在抓什么"。

用户要求："让我们看到你在搜索的过程"。

实现：一个内存环形缓冲 + logging Handler。
后台任务往里写，前端用 Server-Sent Events 订阅，实时滚动显示。

为什么用内存环形缓冲而不是读日志文件：
  日志文件要处理编码、轮转、并发读写位置；而这里只需要"最近 N 条"，
  历史查询走数据库（scrape_unit / agent_step 都有完整留痕）。
  环形缓冲天然限长，不会因为跑 20 小时把内存吃满。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque

MAX_EVENTS = 800

_events: deque = deque(maxlen=MAX_EVENTS)
_subscribers: list[queue.Queue] = []
_lock = threading.Lock()
_seq = 0


# ★ 任务进度回传。
#   第三次栽在同一件事上：采集阶段界面上的进度条**一动不动**，
#   因为采集器走的是 livelog（实时过程页），而任务进度只认 Agent 的 log_step。
#   于是「正在抓第 7 个渠道」和「卡死了」在任务状态里长得完全一样 ——
#   用户为此质问过「你根本没开始跑」。
#   采集是最长的阶段（几小时），恰恰最需要进度。
_progress_sink = None


def set_progress_sink(fn) -> None:
    """fn(message: str) -> None；传 None 解除。由 API 任务层设置。"""
    global _progress_sink
    _progress_sink = fn


def emit(kind: str, message: str, **extra) -> None:
    """推一条事件。kind: search / page / found / block / agent / stage / error"""
    global _seq
    with _lock:
        _seq += 1
        ev = {"seq": _seq, "ts": time.strftime("%H:%M:%S"),
              "kind": kind, "message": message[:400], **extra}
        _events.append(ev)
        # 只有"有进展含义"的事件才上报，page/found 那种每秒好几条的不刷
        if _progress_sink is not None and kind in ("search", "stage", "block", "error"):
            try:
                _progress_sink(f"采集｜{message[:70]}")
            except Exception:  # noqa: BLE001
                pass        # 进度是附带品，绝不能因为它把采集干挂了
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(ev)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def recent(after_seq: int = 0, limit: int = 200) -> list[dict]:
    with _lock:
        return [e for e in _events if e["seq"] > after_seq][-limit:]


# 能当作"进度"展示的事件类型（按有用程度排序）
_PROGRESS_KINDS = ("found", "search", "stage", "block", "warn")


def current_activity() -> str:
    """最近一条有意义的活动，给界面当进度文字用。

    ★ 为什么需要这个：任务的 progress 字段原来只在启动时设一次
      （"主 Agent 研判中…"），之后再没更新过。事件流明明在滚，
      顶部进度却一动不动 —— 用户据此判断"根本没在跑"。
      **一个静止的进度指示比没有进度指示更糟**，它主动传递了错误信息。
    """
    with _lock:
        for e in reversed(_events):
            if e.get("kind") in _PROGRESS_KINDS:
                return f"{e['ts']} {e['message'][:120]}"
    return ""


def stats() -> dict:
    """粗略的运行统计，给界面显示"已经干了多少活"。"""
    with _lock:
        by = {}
        for e in _events:
            by[e.get("kind", "?")] = by.get(e.get("kind", "?"), 0) + 1
        return {"total": _seq, "buffered": len(_events), "by_kind": by}


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=200)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def clear() -> None:
    with _lock:
        _events.clear()


class LiveLogHandler(logging.Handler):
    """把关键日志转成事件流。只转有信息量的，不把 DEBUG 洪水灌进界面。"""

    KIND_BY_LOGGER = {
        "browser": "page", "selenium": "page", "engine": "page",
        "collector": "search", "chief": "agent", "cleaner": "agent",
        "price_audit": "agent", "intel": "agent", "orchestrator": "stage",
        "voc": "voc", "health": "page",
    }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.INFO:
                return
            kind = self.KIND_BY_LOGGER.get(record.name, "log")
            if record.levelno >= logging.WARNING:
                kind = "block" if "拦截" in record.getMessage() or "验证" in record.getMessage() \
                    else "error"
            emit(kind, record.getMessage(), logger=record.name,
                 level=record.levelname)
        except Exception:  # noqa: BLE001
            pass


def install() -> None:
    """挂到根 logger 上。serve 启动时调一次。"""
    root = logging.getLogger()
    if any(isinstance(h, LiveLogHandler) for h in root.handlers):
        return
    h = LiveLogHandler()
    h.setLevel(logging.INFO)
    root.addHandler(h)
