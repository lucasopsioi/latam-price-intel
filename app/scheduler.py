# -*- coding: utf-8 -*-
"""定时调度：每天定点跑采集流水线，之后推 Telegram 简报。

跟着 serve 一起起来（后台线程），设置页可开关与改时间。
misfire_grace_time 给 2 小时：电脑睡眠错过触发点时，醒来后补跑，
而不是静默跳过一整天 —— 静默跳过会让人以为"今天友商没动静"。
"""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, db

log = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def _job_collect() -> None:
    """★ 必须走与界面共用的单任务闸。

    APScheduler 的 max_instances=1 只防定时任务自己重入，防不住
    「界面手动点了采集，同时定时任务到点」这一路 ——
    两套浏览器同时跑会争抢 SQLite 写锁、翻倍触发风控、
    并且两个 Orchestrator 各开一个引擎，机器直接被拖垮。
    """
    from .agents import Orchestrator
    from .api.server import acquire_task_slot, release_task_slot

    if not acquire_task_slot("daily_collect"):
        log.warning("已有任务在运行，本次定时采集跳过（下次到点再跑）")
        return
    log.info("定时任务：开始每日采集")
    try:
        result = Orchestrator(mode="daily").run_daily()
        log.info("定时采集完成：批次 %s，写入 %s 条",
                 result.get("run_id"), result.get("collect", {}).get("rows"))
        release_task_slot(result=result)
    except Exception as e:  # noqa: BLE001
        log.exception("定时采集失败")
        release_task_slot(error=str(e)[:400])


def _job_brief() -> None:
    from .notify import send_daily_brief
    if not db.get_setting("telegram_bot_token"):
        return
    log.info("定时任务：推送 Telegram 简报")
    try:
        r = send_daily_brief()
        log.info("简报推送：%s %s", "成功" if r["ok"] else "失败", r["message"])
    except Exception:  # noqa: BLE001
        log.exception("简报推送失败")


def generate_weekly(anchor: str | None = None, scope: str = "all",
                    category: str = "") -> dict:
    """生成周报 → 导出 PDF/Word/PPT → 推手机。**软件自己全程完成**。

    ★ 2026-08-27 用户确认要点：报告必须由软件调用 MiniMax API 自动产出，
      不是靠人在命令行里跑。所以这条链路被抽成一个函数，
      定时任务（周一 08:00）和界面按钮**走同一条**，不存在两套行为。
    ★ 正文与结论由 MiniMax 生成（LLMClient 从库里取密钥）；模型不可用时
      Agent 内部各段自动退化为纯事实文案，报告照出不误——
      但会在返回值里如实标出 llm=False，不假装是模型写的。
    """
    import json as _json

    from . import phone_sync, report_export
    from .agents import WeeklyReportAgent, report_llm

    acfg = config.load_runtime().get("agents") or {}
    # 报告走写作型供应商（默认 DeepSeek，Key 未配则诚实回落 MiniMax）；
    # 采集/研判类 Agent 不经这里，仍各自用 MiniMax
    llm = report_llm(acfg)
    out = WeeklyReportAgent(llm, acfg).run(
        week_start=anchor, scope=scope, category=category)
    rid = out["report_id"]
    r = db.q1("SELECT * FROM weekly_report WHERE id=?", (rid,))
    met = _json.loads(r["metrics"] or "{}") if r else {}
    sub = f"{r['week_start']} ~ {r['week_end']}" if r else ""
    files = []
    for fmt in ("pdf", "docx", "pptx"):
        try:
            data, name = report_export.export(
                fmt, r["title"] or "竞品周报", sub,
                r["content_md"] or "", met.get("charts") or [])
            (config.EXPORT_DIR / name).write_bytes(data)
            files.append(name)
        except Exception:                    # noqa: BLE001
            log.exception("周报导出失败：%s（其余格式不受影响）", fmt)
    phone_sync.kick_async()
    return {"report_id": rid, "title": out.get("title"),
            "chars": out.get("chars"), "tokens": out.get("tokens"),
            "llm": llm.available(), "model": llm.model,
            "provider": llm.provider,
            "files": files, "charts": len(met.get("charts") or [])}


def _job_weekly_report() -> None:
    """每天凌晨自动刷新周报（2026-08-28 用户：每天凌晨出报告，报告 Agent 用
    DeepSeek）。锚点 = **昨天**：周一凌晨昨天是周日 → 出上一个完整周；
    其余日子 → 出本周到昨天为止的滚动版。周期吸附保证整周只有一行记录，
    每天覆盖更新，不会攒出七份互相矛盾的"本周报告"。
    """
    from datetime import date, timedelta
    try:
        anchor = (date.today() - timedelta(days=1)).isoformat()
        r = generate_weekly(anchor=anchor)
        log.info("周报自动生成：#%s %s · %s 字 · %s tokens · 导出 %s",
                 r["report_id"], r["title"], r["chars"], r["tokens"],
                 "/".join(r["files"]) or "无")
    except Exception:  # noqa: BLE001
        log.exception("周报自动生成失败（下周一会再试；也可在界面手动生成）")


def _job_export_pack() -> None:
    """每天刷新「竞品情报包」到 exports/ —— 给《拉美双周报工作台》带到公司电脑用。

    ★ 2026-09-01 用户：中枢只能在家跑，公司电脑没有 intel.db，所以包必须自动常新。
    ★ 不写任何 SQL（全部来自 tools/intel-sql-bundle.json，由工作台生成）；
      库以只读方式打开并自检写入被拒。任何异常只记日志，绝不影响其他任务。
    产出两份：带日期的（留档）+ 固定名「竞品情报包_最新.json」（网盘同步直接拿）。
    """
    try:
        import sys as _sys
        from pathlib import Path as _P
        tools = _P(__file__).resolve().parent.parent / "tools"
        if str(tools) not in _sys.path:
            _sys.path.insert(0, str(tools))
        import export_biweekly_pack as ep  # noqa: WPS433
        period = ep.current_period()
        out = ep.export_pack(config.DB_PATH, period, config.EXPORT_DIR)
        latest = config.EXPORT_DIR / "竞品情报包_最新.json"
        latest.write_bytes(out.read_bytes())
        log.info("竞品情报包已刷新：%s（期次 %s）", out.name, period["id"])
    except Exception:  # noqa: BLE001
        log.exception("竞品情报包导出失败（不影响其他任务）")


def _job_phone_sync() -> None:
    """导出物同步到手机。无待传文件时零开销返回；手机不在是常态不是故障。"""
    from . import phone_sync
    try:
        r = phone_sync.sync_now()
        if r.get("synced"):
            log.info("手机同步：%s", r.get("summary"))
    except Exception:  # noqa: BLE001
        log.exception("手机同步失败")


def start() -> BackgroundScheduler | None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return _scheduler
        cfg = config.load_runtime()

        sched = BackgroundScheduler(timezone="Asia/Shanghai")

        # ★ 手机同步不受 schedule.enabled 管：那个开关管的是每日采集；
        #   「只要手机连着就自动转」是常驻承诺，除非 phone_sync.enabled 显式关。
        ps_cfg = cfg.get("phone_sync") or {}
        if ps_cfg.get("enabled", True):
            sched.add_job(_job_phone_sync,
                          IntervalTrigger(minutes=int(ps_cfg.get("interval_min", 3))),
                          id="phone_sync", coalesce=True, max_instances=1)

        if cfg["schedule"].get("enabled"):
            h, m = _parse_hm(cfg["schedule"].get("daily_time", "07:30"))
            sched.add_job(_job_collect, CronTrigger(hour=h, minute=m),
                          id="daily_collect", misfire_grace_time=7200,
                          coalesce=True, max_instances=1)
            log.info("定时任务已启动：每日 %02d:%02d 采集", h, m)
        else:
            log.info("每日采集定时未启用（设置页可开）")

        # 每天凌晨刷新（2026-08-28 用户定，取代此前的每周一）。misfire 宽限 12 小时：
        # 凌晨没开机，开机后补跑当天这期，不静默跳过。
        wr = cfg.get("weekly_report") or {}
        if wr.get("auto", 1):
            wh, wm = _parse_hm(wr.get("time", "05:00"))
            sched.add_job(_job_weekly_report,
                          CronTrigger(hour=wh, minute=wm),
                          id="weekly_report", misfire_grace_time=43200,
                          coalesce=True, max_instances=1)
            log.info("周报定时已启动：每天 %02d:%02d 自动生成并送手机", wh, wm)

        # 双周报工作台接口：每天刷新竞品情报包（默认开；runtime 里 biweekly_pack.auto=0 可关）。
        # 排在周报之后（默认 05:20），misfire 宽限 12 小时：凌晨没开机，开机后补跑。
        bp = cfg.get("biweekly_pack") or {}
        if bp.get("auto", 1):
            ph, pm = _parse_hm(bp.get("time", "05:20"))
            sched.add_job(_job_export_pack, CronTrigger(hour=ph, minute=pm),
                          id="biweekly_pack", misfire_grace_time=43200,
                          coalesce=True, max_instances=1)
            log.info("竞品情报包定时已启动：每天 %02d:%02d 刷新到 exports/", ph, pm)

        if cfg["telegram"].get("enabled"):
            bh, bm = _parse_hm(cfg["telegram"].get("daily_time", "08:30"))
            sched.add_job(_job_brief, CronTrigger(hour=bh, minute=bm),
                          id="daily_brief", misfire_grace_time=7200,
                          coalesce=True, max_instances=1)

        sched.start()
        _scheduler = sched
        return sched


def stop() -> None:
    global _scheduler
    with _lock:
        if _scheduler:
            _scheduler.shutdown(wait=False)
            _scheduler = None


def status() -> dict:
    if not _scheduler:
        return {"running": False, "jobs": []}
    return {"running": True, "jobs": [
        {"id": j.id, "next_run": str(j.next_run_time)} for j in _scheduler.get_jobs()]}


def _parse_hm(s: str) -> tuple[int, int]:
    try:
        h, m = str(s).split(":")
        return int(h) % 24, int(m) % 60
    except Exception:  # noqa: BLE001
        return 7, 30
