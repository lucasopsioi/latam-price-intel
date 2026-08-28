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


def _job_weekly_report() -> None:
    """每周自动出一期周报（2026-08-27 用户：报告改成每周一次）。

    锚点 = 今天减 7 天 → 吸附到**上一个完整周**；周一早上跑，
    报告覆盖刚结束的那一周，数据是全的。
    生成后直接导出 PDF+PPT 落到 exports/ —— phone_sync 会自动送手机。
    """
    import json as _json

    from . import phone_sync, report_export
    from .agents import LLMClient, WeeklyReportAgent

    try:
        from datetime import date, timedelta
        anchor = (date.today() - timedelta(days=7)).isoformat()
        acfg = config.load_runtime().get("agents") or {}
        out = WeeklyReportAgent(LLMClient(acfg), acfg).run(week_start=anchor)
        log.info("周报自动生成：#%s %s", out.get("report_id"), out.get("title"))
        r = db.q1("SELECT * FROM weekly_report WHERE id=?", (out["report_id"],))
        met = _json.loads(r["metrics"] or "{}") if r else {}
        sub = f"{r['week_start']} ~ {r['week_end']}" if r else ""
        for fmt in ("pdf", "pptx"):
            data, name = report_export.export(
                fmt, r["title"] or "竞品周报", sub,
                r["content_md"] or "", met.get("charts") or [])
            (config.EXPORT_DIR / name).write_bytes(data)
        phone_sync.kick_async()
        log.info("周报已导出 PDF+PPT 并触发手机同步")
    except Exception:  # noqa: BLE001
        log.exception("周报自动生成失败（下周一会再试；也可在界面手动生成）")


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

        # 周报每周一自动出一期（2026-08-27 用户定）。
        # misfire 宽限 12 小时：周一早上电脑没开，开机后补跑本期，
        # 而不是静默跳过一整周。
        wr = cfg.get("weekly_report") or {}
        if wr.get("auto", 1):
            wh, wm = _parse_hm(wr.get("time", "08:00"))
            sched.add_job(_job_weekly_report,
                          CronTrigger(day_of_week="mon", hour=wh, minute=wm),
                          id="weekly_report", misfire_grace_time=43200,
                          coalesce=True, max_instances=1)
            log.info("周报定时已启动：每周一 %02d:%02d 自动生成并送手机", wh, wm)

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
