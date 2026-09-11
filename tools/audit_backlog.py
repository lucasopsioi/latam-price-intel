# -*- coding: utf-8 -*-
"""审计积压清理：把 price_obs 里"永远不会再被审"的 pending 行按观测日循环审完。

为什么需要它（2026-09-04 诊断）：
  PriceAuditAgent.run() 旧契约 = `WHERE obs_date = today() … ORDER BY country_code … LIMIT 2000`，
  三处调用点全部裸调、不传日期、不循环。日入库 4k~21k 行、每天只审 2000 行 ⇒
  全库 216,412 行里 182,838 条 pending（84.5%），其中 165,679 条落在"今天以前"——
  现行代码只看 today，这些行**永远不会再被审**；而 36 处分析消费方写的是 `<> 'rejected'`，
  pending 被当成通过喂图（8,500 CLP 的钢化膜就是这样上了 Lenovo Tab 的价格曲线）。
  字母序名额还让 MX 全库 accepted=0（BR→CL→CO→MX→PE，2000 名额永远先给前面的）。

它做什么：
  逐个观测日（旧→新，默认排除今天）调 PriceAuditAgent.run_all(d, batch)，规则层全量、
  LLM 只判灰区（--no-llm 则灰区留 pending 不回写），每轮一条 agent_run 留痕，
  每轮一行 JSONL 日志，**回滚清单在改库之前落盘**。

铁律：
  · 默认干跑（只读连接 + 封掉 db.tx，连 agent_run 都不写），--apply 才真写。
  · --apply 在采集进行中一律拒跑：/api/health task_running 为真，或 scrape_run 有 status='running'，
    任一为真且无 --force 则 exit 2。真跑每 10 轮重读 /api/health，采集开始就停。
  · 干跑与真跑走**同一套** _rule_check / _build_baselines / _build_category_floors，
    干跑不调 run()/_write_back()（它们会写 agent_run/price_obs）。

跑法：
  python tools\\audit_backlog.py                          # 干跑：打印执行清单（可在采集中跑）
  python tools\\audit_backlog.py --apply                  # 真跑：规则层 + LLM 判灰区
  python tools\\audit_backlog.py --apply --no-llm         # 真跑：只跑规则层，灰区留 pending
  python tools\\audit_backlog.py --floor-recheck [--apply]        # 对已 accepted 行重跑品类地板（只允许 accepted→rejected）
  python tools\\audit_backlog.py --recheck-no-baseline [--apply]  # 对"第三方且无基线暂留"行重审
  python tools\\audit_backlog.py --rollback logs\\audit_backlog_<ts>_rollback.jsonl
  python tools\\audit_backlog.py --verify                 # 验收查询（只读）

验收（跑完 --apply 后 --verify 要看到）：
  ① 今天以前每个观测日 pending=0（--no-llm 时残留 = 灰区数，另跑一轮带 LLM 收尾）
  ② 第二遍 --apply：
       带 LLM  → in=0（灰区已被判掉，没有 pending 可取）
       --no-llm → ★**in = 灰区数、acc=rej=0**，不是"取到 0 行"。
                  灰区在 --no-llm 下是**故意留 pending** 的，第二遍必然把它们重新取出来、
                  重新判成灰区、再留下。写成"处理 0 行"会让人把正常状态当成异常
                  （或者反过来：真的漏审了却以为是灰区），两种误读都发生过。
                  判据是"第二遍的 acc+rej = 0"（没有新定案），不是"in = 0"。
  ③ 各国 accepted/pending 比例与干跑预演一致
  ④ 残留体检：每国每品类 accepted 整机最低价 top10 逐条看，平板 CLP 下沿应 ≥ 0.10×中位
     （wearable 例外：条件地板会放行带整机证据的白牌真手表，CL 下沿到 9,990 CLP 是**对的**）
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, db                                   # noqa: E402
from app.agents.price_audit import (FLOOR_REASON_PREFIX, FLOOR_X,   # noqa: E402
                                    NO_BASELINE_REASON, PriceAuditAgent)
from tools.postprocess_date import _collecting               # noqa: E402  单一实现，复用

LOG_DIR = ROOT / "logs"
TOK_PER_GRAY = 135          # 历史实测：299,812 tokens / 2,227 灰区行
HEALTH_EVERY = 10           # 真跑每几轮重读一次 /api/health

# 上一次 apply_backlog 的累计计数。★ 存在的理由：验收条款②「第二遍 acc=rej=0」
#   只能从这里断言 —— 从 JSONL 日志文件读会撞上"文件名按秒命名、同秒连跑写进同一份"
#   （知识页记过的坑，回滚清单就是这么差点被覆盖的）。
LAST_SUMMARY: dict = {}


# ---------------------------------------------------------------- 闸门

def collecting_now() -> tuple[bool, str]:
    """采集是否在进行。双保险：/api/health task_running + scrape_run status='running'。"""
    busy = _collecting()
    if busy:
        return True, "/api/health task_running=true"
    run = db.q1("SELECT id, started_at FROM scrape_run WHERE status='running' "
                "ORDER BY id DESC LIMIT 1")
    if run:
        note = "；服务不在线，可能是上次被中断留下的状态，确认后可 --force" if busy is None else ""
        return True, f"scrape_run #{run['id']} status=running（{run['started_at']}）{note}"
    return False, ("服务不在线，按库里状态放行" if busy is None else "")


# ---------------------------------------------------------------- 只读模式

class ReadOnlyDB:
    """把 app.db 的 q/q1 换成只读连接、把 tx 封死 —— 干跑里任何写库企图都当场炸。"""

    def __init__(self, path: Path):
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self.con = sqlite3.connect(uri, uri=True, timeout=30)
        self.con.row_factory = sqlite3.Row

    def q(self, sql, params=()):
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def q1(self, sql, params=()):
        r = self.con.execute(sql, params).fetchone()
        return dict(r) if r else None

    def install(self) -> None:
        db.q, db.q1 = self.q, self.q1

        @contextmanager
        def _no_tx():
            raise RuntimeError("干跑模式：db.tx 已封，禁止写库")
            yield  # noqa: unreachable  —— 让它保持 contextmanager 形态

        db.tx = _no_tx


# ---------------------------------------------------------------- 取数

def pending_dates(include_today: bool, only: list[str] | None) -> list[tuple[str, int]]:
    today = db.today()
    rows = db.q("SELECT obs_date d, COUNT(*) n FROM price_obs "
                "WHERE audit_status='pending' GROUP BY 1 ORDER BY 1")
    out = []
    for r in rows:
        if not include_today and r["d"] >= today:
            continue
        if only and r["d"] not in only:
            continue
        out.append((r["d"], r["n"]))
    return out


def pending_before_today() -> int:
    r = db.q1("SELECT COUNT(*) n FROM price_obs WHERE audit_status='pending' "
              "AND obs_date < date('now','localtime')") or {}
    return r.get("n") or 0


def load_pending_rows(d: str) -> list[dict]:
    """与 PriceAuditAgent.run 的取行 SQL 同口径（不带 LIMIT）。"""
    return db.q("""
        SELECT po.*, c.name AS channel_name, c.kind AS channel_kind, c.default_seller_type
        FROM price_obs po JOIN channel c ON c.id = po.channel_id
        WHERE po.obs_date = ? AND po.audit_status = 'pending'
        ORDER BY po.id
    """, (d,))


# ---------------------------------------------------------------- 干跑（预演）

def pick_two_ends(items: list[tuple], k: int = 20) -> list[tuple]:
    """两头都抽、按币种分组：低价端证明"配件抓到了"，误杀整机只出现在高价端。
    items: (currency, price, cc, title, reason)"""
    by_cur: dict[str, list] = collections.defaultdict(list)
    for it in items:
        by_cur[it[0] or "?"].append(it)
    per = max(2, k // max(1, len(by_cur)))
    out: list[tuple] = []
    for cur in sorted(by_cur):
        its = sorted(by_cur[cur], key=lambda x: (x[1] is None, x[1] or 0))
        lo = its[:max(1, per // 2)]
        hi = its[len(lo):][-(per - len(lo)):] if len(its) > len(lo) else []
        out.extend(lo + hi)
    return out


def rehearse(agent: PriceAuditAgent, dates: list[tuple[str, int]], batch: int) -> dict:
    """逐日期对全部 pending 行跑真实 _rule_check（基线/地板按月缓存），不写库。

    ★ 近似：预演用的基线/地板是"当前库状态"算的；真跑时每轮重建，随着剔除推进会略变。
    """
    cache: dict[str, tuple[dict, dict, dict]] = {}
    plan = {"dates": [], "total": collections.Counter(), "samples":
            collections.defaultdict(list), "floors": {}, "by_country": collections.defaultdict(
                collections.Counter), "reasons": collections.defaultdict(collections.Counter)}
    for d, n in dates:
        month = d[:7]
        if month not in cache:
            bl = agent._build_baselines(month + "-01")
            fl = agent._build_category_floors(month + "-01")
            cache[month] = (bl, fl, dict(agent._floor_meta or {}))
            plan["floors"][month] = cache[month][2]
        bl, fl, _ = cache[month]
        rows = load_pending_rows(d)
        c = collections.Counter()
        for r in rows:
            v, why = agent._rule_check(r, bl, fl)
            cls = v
            if v == "rejected" and why.startswith(FLOOR_REASON_PREFIX):
                cls = "floor"
            c[cls] += 1
            plan["by_country"][r["country_code"]][cls] += 1
            plan["reasons"][cls][_reason_kind(why)] += 1
            plan["samples"][cls].append((r["currency"], r["sale_price"], r["country_code"],
                                         (r["title"] or "")[:70], why[:90], d))
            # 条件地板放行的行单独统计 + 留样例：守卫成批放行 = 词表/品类出了问题，
            # 必须在干跑清单里就看得见，不能等真跑完再从库里找。
            # ★ 口径与 run() 一致：只数真的因此活下来的行（被捆绑/翻新/缺货先剔的不算）
            veto = agent._floor_veto_hit(r, fl) if v != "rejected" else None
            if veto:
                c["veto"] += 1
                plan["by_country"][r["country_code"]]["veto"] += 1
                plan["reasons"]["veto"][_reason_kind(veto)] += 1
                plan["samples"]["veto"].append((r["currency"], r["sale_price"],
                                                r["country_code"], (r["title"] or "")[:70],
                                                veto[:90], d))
        rec = {"date": d, "pending": len(rows), "accepted": c["accepted"],
               "rejected": c["rejected"] + c["floor"], "floor": c["floor"],
               "veto": c["veto"],
               "gray": c["gray"], "rounds": math.ceil(len(rows) / batch) if rows else 0,
               "tokens_est": c["gray"] * TOK_PER_GRAY}
        plan["dates"].append(rec)
        plan["total"].update(c)
    return plan


def _reason_kind(why: str) -> str:
    import re
    return re.sub(r"[\d.,%]+", "#", why)[:56]


def print_plan(plan: dict, batch: int, no_llm: bool) -> None:
    print("\n== 执行清单（干跑，未改库）==")
    print(f"{'观测日':<12}{'pending':>9}{'accepted':>10}{'rejected':>10}{'其中地板':>10}"
          f"{'守卫放行':>10}{'gray':>8}{'预计轮数':>9}{'预计token':>11}")
    T = collections.Counter()
    for rec in plan["dates"]:
        print(f"{rec['date']:<12}{rec['pending']:>9,}{rec['accepted']:>10,}{rec['rejected']:>10,}"
              f"{rec['floor']:>10,}{rec['veto']:>10,}{rec['gray']:>8,}{rec['rounds']:>9}"
              f"{rec['tokens_est']:>11,}")
        for k in ("pending", "accepted", "rejected", "floor", "veto", "gray", "rounds",
                  "tokens_est"):
            T[k] += rec[k]
    print(f"{'合计':<12}{T['pending']:>9,}{T['accepted']:>10,}{T['rejected']:>10,}"
          f"{T['floor']:>10,}{T['veto']:>10,}{T['gray']:>8,}{T['rounds']:>9}{T['tokens_est']:>11,}")
    n = max(1, T["pending"])
    print(f"规则层可定案 {(T['accepted'] + T['rejected']) / n:.1%}，灰区 {T['gray'] / n:.2%}"
          + ("（--no-llm：灰区留 pending）" if no_llm else
             f"（交 LLM，按 {TOK_PER_GRAY} tok/行估 {T['tokens_est'] / 1e6:.2f}M token）"))

    print("\n== 按国家 ==")
    for cc in sorted(plan["by_country"]):
        c = plan["by_country"][cc]
        # ★ 分母只算判定类（veto 是叠加统计，不是一个判定桶），否则占比会被稀释
        s = c["accepted"] + c["rejected"] + c["floor"] + c["gray"] or 1
        print(f"  {cc}: accepted {c['accepted']:,} ({c['accepted'] / s:.0%})  rejected "
              f"{c['rejected'] + c['floor']:,}  其中地板 {c['floor']:,}  "
              f"守卫放行 {c['veto']:,}  gray {c['gray']:,}")

    print("\n== 品类地板（国家×品类 → 中位价 | 样本来源:n）==")
    for month, meta in sorted(plan["floors"].items()):
        print(f"  [{month}]")
        for (cc, cat), m in sorted(meta.items()):
            x = FLOOR_X.get(cat)
            print(f"    {cc} {cat:<9} med={m['med']:>13,.0f}  地板<{x * m['med']:>11,.0f}"
                  f"  ({m['src']}:{m['n']})")

    for cls, title in (("floor", "地板剔除"), ("veto", "地板守卫放行（低于地板但带整机证据）"),
                       ("rejected", "规则剔除（非地板）"),
                       ("gray", "灰区"), ("accepted", "通过")):
        items = plan["samples"].get(cls) or []
        print(f"\n== 样例·{title}（{len(items):,} 条，两头抽、按币种分）==")
        for kind, cnt in plan["reasons"][cls].most_common(6):
            print(f"    {cnt:>7,}  {kind}")
        for cur, p, cc, t, why, d in pick_two_ends(items, 20):
            ps = f"{p:,.0f}" if p is not None else "—"
            print(f"    {d} {cc} {cur} {ps:>12}  {t}  ← {why[:80]}")


# ---------------------------------------------------------------- 真跑

def _open_log(tag: str) -> tuple[Path, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOG_DIR / f"audit_backlog_{ts}{tag}.jsonl", LOG_DIR / f"audit_backlog_{ts}{tag}_rollback.jsonl"


def _append_jsonl(path: Path, rec: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def dump_rollback(path: Path, rows: list[dict], tag: str, written: str | None = None) -> int:
    """回滚清单 = 改之前的 (id, audit_status, audit_reason, audit_by)。**先落盘再改库**，
    并 fsync —— 反过来则进程中途挂掉就回不去了。

    written = 本次**将要写入**的 audit_status（复审两条路都固定是 'rejected'）。
      落进清单的 `w` 字段，rollback() 拿它当守卫，只回滚"仍带本次判决"的行。
      apply_backlog 传 None：那里的判决是逐行算出来的，而清单必须在算之前落盘
      （先落盘才是后悔药），所以只能记"改前值"，回滚时退回弱守卫（见 rollback）。
    """
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"id": r["id"], "s": r["audit_status"], "r": r["audit_reason"],
                                "b": r["audit_by"], "t": tag, "w": written},
                               ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return len(rows)


def apply_backlog(args) -> int:
    from app.agents import LLMClient

    cfg = config.load_runtime()
    llm = None
    gray_policy = "keep_pending"
    if not args.no_llm:
        llm = LLMClient(cfg["agents"])
        if llm.available():
            gray_policy = "llm"
        else:
            print("! 未配置 LLM Key：灰区改为留 pending（不按旧行为一律保留），等有 Key 再收尾")
    dates = pending_dates(args.include_today, args.dates)
    if not dates:
        print("没有待审日期。")
        return 0
    log_path, rb_path = _open_log("")
    print(f"日志 {log_path}\n回滚清单 {rb_path}")
    print(f"待处理 {len(dates)} 个观测日，共 {sum(n for _, n in dates):,} 行 pending；"
          f"阈值 {args.threshold}，批 {args.batch}，灰区策略 {gray_policy}")

    health = {"rounds": 0}

    def should_stop() -> bool:
        health["rounds"] += 1
        if health["rounds"] % HEALTH_EVERY:
            return False
        busy, why = collecting_now()
        if busy and not args.force:
            print(f"! 采集开始（{why}），停止")
            return True
        return False

    def on_round(r: dict) -> None:
        line = {"ts": db.now(), "date": r["obs_date"], "round": r["round"], "in": r["total"],
                "acc": r["accepted"], "rej": r["rejected"], "gray": r["gray_pending"],
                "floor": r["floor_rejected"], "tokens": r["tokens"],
                "agent_run_id": r["run_id"], "last_id": r["last_id"]}
        _append_jsonl(log_path, line)
        print(f"  {r['obs_date']} 第{r['round']:>3}轮 in={r['total']:>5} acc={r['accepted']:>5} "
              f"rej={r['rejected']:>5} floor={r['floor_rejected']:>4} gray={r['gray_pending']:>4} "
              f"tok={r['tokens']:>6} run#{r['run_id']}")

    grand = collections.Counter()
    for d, n in dates:
        left = pending_before_today()
        if left < args.threshold and not args.include_today:
            print(f"今天以前 pending={left} < 阈值 {args.threshold}，停止")
            break
        rows = db.q("SELECT id, audit_status, audit_reason, audit_by FROM price_obs "
                    "WHERE obs_date=? AND audit_status='pending'", (d,))
        dump_rollback(rb_path, rows, d)
        print(f"[{d}] pending {len(rows):,}，回滚清单已落盘 → 开始审计")
        agent = PriceAuditAgent(llm, cfg["agents"])
        agg = agent.run_all(d, batch=args.batch, gray_policy=gray_policy,
                            on_round=on_round, should_stop=should_stop)
        for k in ("total", "accepted", "rejected", "gray_pending", "floor_rejected",
                  "floor_vetoed", "judged_rejected", "tokens"):
            grand[k] += agg.get(k) or 0
        print(f"[{d}] 完成 {agg['rounds']} 轮：in {agg['total']:,} acc {agg['accepted']:,} "
              f"rej {agg['rejected']:,}（地板 {agg['floor_rejected']:,}"
              + (f"，守卫放行 {agg['floor_vetoed']:,}" if agg.get("floor_vetoed") else "")
              + f"）灰区留 {agg['gray_pending']:,} token {agg['tokens']:,} "
                f"剩余 pending {agg['pending_left']:,}"
              + (f"  ← 中止：{agg['stopped']}" if agg["stopped"] else ""))
        # ★ 审计自己的告警必须在这里露头：run_all 之前把它算完就丢了，
        #   于是这一行 r.get("warning") 是死代码（同 orchestrator._audit_day / postprocess_date）
        if agg.get("warning"):
            grand["warned_dates"] += 1
            print(f"  ! 审计告警：{agg['warning']}")
        _append_jsonl(log_path, {"ts": db.now(), "date": d, "summary": agg})
        if agg["stopped"] == "should_stop":
            return 3
    print(f"\n总计：in {grand['total']:,} acc {grand['accepted']:,} rej {grand['rejected']:,} "
          f"（地板 {grand['floor_rejected']:,}，守卫放行 {grand['floor_vetoed']:,}）"
          f"灰区留 {grand['gray_pending']:,} token {grand['tokens']:,}"
          + (f"  ★{grand['warned_dates']} 个观测日触发判定型剔除率告警"
             if grand["warned_dates"] else ""))
    LAST_SUMMARY.clear()
    LAST_SUMMARY.update(grand)
    verify()
    return 0


# ---------------------------------------------------------------- 复审

def _months_of(where: str, params: tuple) -> list[str]:
    return [r["m"] for r in db.q(f"SELECT DISTINCT substr(obs_date,1,7) m FROM price_obs "
                                 f"WHERE {where} ORDER BY 1", params)]


def floor_recheck(args, apply: bool) -> int:
    """对已 accepted 的整机行重跑品类地板，只允许 accepted→rejected。"""
    agent = PriceAuditAgent(None, {})
    where = "audit_status='accepted' AND product_kind='device' AND sale_price IS NOT NULL"
    params: tuple = ()
    if args.dates:
        where += f" AND obs_date IN ({','.join('?' * len(args.dates))})"
        params = tuple(args.dates)
    hits: list[tuple[dict, str]] = []
    scanned = 0
    for month in _months_of(where, params):
        floors = agent._build_category_floors(month + "-01")
        rows = db.q(f"SELECT id, obs_date, country_code, category_code, sale_price, currency, "
                    f"title, audit_status, audit_reason, audit_by FROM price_obs "
                    f"WHERE substr(obs_date,1,7)=? AND {where}", (month, *params))
        scanned += len(rows)
        for r in rows:
            why = agent._floor_check(r, floors)
            if why:
                hits.append((r, why))
    return _report_and_apply(hits, scanned, "地板复审", apply, args)


def recheck_no_baseline(args, apply: bool) -> int:
    """对"第三方且无基线，暂留"的行，用现在的基线/地板重跑 _rule_check；只写 rejected。"""
    agent = PriceAuditAgent(None, {})
    where = "audit_status='accepted' AND audit_reason LIKE ?"
    params: tuple = (NO_BASELINE_REASON + "%",)
    hits: list[tuple[dict, str]] = []
    scanned = gray = 0
    for month in _months_of(where, params):
        bl = agent._build_baselines(month + "-01")
        fl = agent._build_category_floors(month + "-01")
        rows = db.q("""SELECT po.*, c.name AS channel_name, c.kind AS channel_kind,
                              c.default_seller_type
                       FROM price_obs po JOIN channel c ON c.id = po.channel_id
                       WHERE substr(po.obs_date,1,7) = ? AND po.audit_status = 'accepted'
                         AND po.audit_reason LIKE ?""", (month, *params))
        scanned += len(rows)
        for r in rows:
            v, why = agent._rule_check(r, bl, fl)
            if v == "rejected":
                hits.append((r, why))
            elif v == "gray":
                gray += 1
    print(f"（其中 {gray:,} 条现在会进灰区 —— 保持 accepted 不动，需要 LLM 的另走正常审计）")
    return _report_and_apply(hits, scanned, "无基线复审", apply, args)


def _report_and_apply(hits: list[tuple[dict, str]], scanned: int, tag: str,
                      apply: bool, args) -> int:
    print(f"\n== {tag}：扫描 accepted {scanned:,} 行，命中 {len(hits):,} 条（accepted→rejected）==")
    by = collections.Counter((r["country_code"], r.get("category_code")) for r, _ in hits)
    for (cc, cat), n in sorted(by.items()):
        print(f"  {cc} {cat}: {n:,}")
    items = [(r["currency"], r["sale_price"], r["country_code"], (r["title"] or "")[:70],
              why[:90], r["obs_date"]) for r, why in hits]
    print("  样例（两头抽、按币种分）：")
    for cur, p, cc, t, why, d in pick_two_ends(items, 24):
        print(f"    {d} {cc} {cur} {p:>12,.0f}  {t}  ← {why[:80]}")
    if not apply:
        print("  （干跑，未改库；--apply 才写）")
        return 0
    if not hits:
        return 0
    _, rb_path = _open_log("_recheck")
    # 本次写入值确定是 'rejected'（下面的 UPDATE 写死），落进清单当回滚守卫
    dump_rollback(rb_path, [r for r, _ in hits], tag, written="rejected")
    print(f"  回滚清单已落盘 {rb_path}")
    agent = PriceAuditAgent(None, {})
    agent.start(f"{tag}：{len(hits)} 条 accepted→rejected")
    with db.tx() as conn:
        for r, why in hits:
            conn.execute("""UPDATE price_obs SET audit_status='rejected', audit_reason=?,
                            audit_by='rule:price_audit' WHERE id=? AND audit_status='accepted'""",
                         (why[:300], r["id"]))
    agent.log_step(tag, parsed={"扫描": scanned, "改判": len(hits), "按国品类": {
        f"{cc}/{cat}": n for (cc, cat), n in by.items()}}, decision="ok",
        reason=f"回滚清单 {rb_path.name}")
    agent.finish("ok", f"{tag}：{len(hits)}/{scanned} 条 accepted→rejected", scanned, len(hits))
    print(f"  已改 {len(hits):,} 条，agent_run #{agent.run_id}")
    return 0


# ---------------------------------------------------------------- 回滚 / 验收

def rollback(path: Path) -> int:
    """按清单恢复 audit_status/reason/by。**只回滚仍带本次判决的行**。

    ★ 守卫的理由：回滚清单是"这一次跑之前的样子"，不是"这一行永远该是的样子"。
      裸 UPDATE ... WHERE id=? 会把**本次之后**别人写的判决一起抹掉 ——
      服务每天按观测日重审、另一条复审线也在改同一批行，两者都会被这条回滚静默覆盖，
      而且看不出来（回滚"成功"了，日志里没有任何异常）。
      · 清单带 w（复审两条路：本次写入值确定是 'rejected'）→ `AND audit_status=w`，精确。
      · 清单不带 w（apply_backlog：判决在落盘之后才算出来）→ 退回 `AND audit_status<>s`，
        即"这一行确实已经离开过改前状态才回滚"。它挡不住"别人把它改成了另一个判决"，
        挡得住"已经回滚过一次"和"这一行压根没被本次动过"。
      两种情况都把跳过的行数报出来 —— 跳过多少本身就是情报。
    """
    recs = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    print(f"回滚清单 {len(recs):,} 行 ← {path}")
    done = collections.Counter()
    skipped = 0
    with db.tx() as conn:
        for r in recs:
            w = r.get("w")
            if w:
                cur = conn.execute(
                    "UPDATE price_obs SET audit_status=?, audit_reason=?, audit_by=? "
                    "WHERE id=? AND audit_status=?", (r["s"], r["r"], r["b"], r["id"], w))
            else:
                cur = conn.execute(
                    "UPDATE price_obs SET audit_status=?, audit_reason=?, audit_by=? "
                    "WHERE id=? AND audit_status<>?", (r["s"], r["r"], r["b"], r["id"], r["s"]))
            if cur.rowcount:
                done[r["s"]] += 1
            else:
                skipped += 1
    print(f"完成：回滚 {sum(done.values()):,} 行 {dict(done)}"
          + (f"；跳过 {skipped:,} 行（已不带本次判决 —— 已回滚过、或之后被别的审计改过，"
             f"不覆盖）" if skipped else ""))
    return 0


def verify() -> None:
    print("\n== 验收 ==")
    bad = db.q("""SELECT obs_date, SUM(audit_status='pending') p, COUNT(*) n FROM price_obs
                  WHERE obs_date < date('now','localtime') GROUP BY 1 HAVING p > 0 ORDER BY 1""")
    tot = db.q1("""SELECT COUNT(*) n, SUM(audit_status='pending' AND obs_date < date('now','localtime')) p
                   FROM price_obs""") or {}
    pct = (tot.get("p") or 0) / max(1, tot.get("n") or 0)
    print(f"① 今天以前仍有 pending 的观测日：{len(bad)} 个"
          + ("" if not bad else "  " + "，".join(f"{r['obs_date']}:{r['p']}" for r in bad[:12])))
    print(f"   pending_pct_before_today = {pct:.2%}（>1% 应告警）")
    print("③ 各国 accepted / pending / rejected / 总：")
    for r in db.q("""SELECT country_code cc, SUM(audit_status='accepted') a, SUM(audit_status='pending') p,
                            SUM(audit_status='rejected') r, COUNT(*) n
                     FROM price_obs GROUP BY 1 ORDER BY 1"""):
        print(f"   {r['cc']}: {r['a']:,} / {r['p']:,} / {r['r']:,} / {r['n']:,}")
    print("④ 残留体检：每国每品类 accepted 整机最低价 top10（本币，逐条看）")
    for g in db.q("""SELECT country_code cc, category_code cat FROM price_obs
                     WHERE audit_status='accepted' AND product_kind='device' AND category_code IS NOT NULL
                     GROUP BY 1,2 ORDER BY 1,2"""):
        rows = db.q("""SELECT sale_price p, currency, substr(title,1,60) t FROM price_obs
                       WHERE audit_status='accepted' AND product_kind='device' AND condition='new'
                         AND is_bundle=0 AND sale_price IS NOT NULL AND country_code=? AND category_code=?
                       ORDER BY sale_price LIMIT 10""", (g["cc"], g["cat"]))
        print(f"   [{g['cc']} {g['cat']}]")
        for r in rows:
            print(f"      {r['p']:>12,.0f} {r['currency']}  {r['t']}")


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    """★ 单独一个函数，是为了让**默认值本身**可测（tests/test_audit_backlog.py）：
    --threshold 的默认值改错过一次（500 = 小批次日永远不审），
    而默认值埋在 main() 里时，测试只能测"传了参数会怎样"，测不到"不传会怎样"。"""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry", action="store_true", help="干跑（默认）")
    g.add_argument("--apply", action="store_true", help="真写库")
    # ★ 默认 0（2026-09-05 从 500 改）：阈值的语义是"剩这么少就别跑了"，
    #   但它是在**每个观测日开始前**用全库 pending 判的 —— 500 会让所有
    #   ≤499 条 pending 的旧观测日**永远没人审**（一天几十条的小批次日、
    #   补跑过一半的日子），而这些行在 = 'accepted' 口径下等同于不存在。
    #   "少量积压不值得跑"是错的：积压清不干净，看板上的那一国就是空的。
    ap.add_argument("--threshold", type=int, default=0,
                    help="今天以前的 pending 少于此数即停（默认 0 = 审到一条不剩）")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--no-llm", action="store_true", help="灰区留 pending 不回写，只跑规则层")
    ap.add_argument("--dates", nargs="*", help="只处理这些观测日（缺省=所有 pending 日期，旧→新）")
    ap.add_argument("--include-today", action="store_true", help="也处理今天（采集中通常别开）")
    ap.add_argument("--floor-recheck", action="store_true", help="对已 accepted 行重跑品类地板")
    ap.add_argument("--recheck-no-baseline", action="store_true",
                    help="对'第三方且无基线暂留'的行重审（只写 rejected）")
    ap.add_argument("--rollback", metavar="FILE", help="按回滚清单恢复 audit_status/reason/by")
    ap.add_argument("--verify", action="store_true", help="只跑验收查询")
    ap.add_argument("--force", action="store_true", help="采集进行中也跑（不建议）")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    apply = bool(a.apply)

    if apply or a.rollback:
        busy, why = collecting_now()
        if busy and not a.force:
            print(f"! 采集进行中（{why}），拒跑。等空闲再跑，或 --force。")
            return 2
        if why:
            print(f"  ({why})")
    else:
        ReadOnlyDB(config.DB_PATH).install()
        print("干跑模式：只读连接，db.tx 已封。")

    if a.rollback:
        return rollback(Path(a.rollback))
    if a.verify:
        verify()
        return 0
    if a.floor_recheck:
        return floor_recheck(a, apply)
    if a.recheck_no_baseline:
        return recheck_no_baseline(a, apply)
    if apply:
        return apply_backlog(a)

    dates = pending_dates(a.include_today, a.dates)
    print(f"待审日期 {len(dates)} 个，pending 共 {sum(n for _, n in dates):,} 行"
          f"（今天以前 pending={pending_before_today():,}，阈值 {a.threshold}）")
    t0 = time.time()
    plan = rehearse(PriceAuditAgent(None, {}), dates, a.batch)
    print_plan(plan, a.batch, a.no_llm)
    print(f"\n预演耗时 {time.time() - t0:.0f}s。真跑：python tools\\audit_backlog.py --apply"
          + (" --no-llm" if a.no_llm else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
