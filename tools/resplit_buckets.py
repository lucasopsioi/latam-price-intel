# -*- coding: utf-8 -*-
"""拆桶：把「系列级 SKU」产品名下的观测按当前身份规则重算，迁到细分产品。默认干跑。

背景（2026-09-04 用户截图）：#2292「Lenovo Tab」801 条观测 / 158 种标题，Tab P11、
Tab Pro 12.7、M10、钢化膜全在一个桶里，曲线在 18 万与 8,500 CLP 之间来回跳。
根因是权威表 sku_rules.yaml 的系列级兜底规则 + 采集端对命中行无条件写 device。

为什么现有两个重算入口都不能用：
  · app/reprocess.renormalize_all —— 取**最长标题** + normalize_model，
    实测会把 3255 个产品改坏 2894 个（knowledge/lessons/scrape-normalize-silent-corruption）
  · tools/renorm_skus.py —— 逐标题 skunorm 多数票，**不认 sku_code / 权威表**，
    会把 116 个权威产品改成 "Tab Full Hd" 这类前 4 词兜底名
本工具**不改任何产品的名字**：桶产品原样保留（装剩下的），每条观测各自按
`app.agents.cleaner.identity_for`（与每日入库**同一份实现**）算身份，
细分得出的名字若已有同键产品就并进去，没有才新建。

三条纪律（沿用 renorm_skus.py / apply_reclass.py 的教训）：
  1. 分组键与 UNIQUE(brand_id, model_key, category_code) **逐字一致**（lower + 去空格），
     并到已占键的行上，绝不改名撞键；
  2. 派生表 competitor_match **只删不改指**（apply_reclass 的 DERIVED_DROP），
     price_move 近 N 天对受影响 id **删了重算**（PriceMoveAgent.run 按日 upsert，幂等）；
     review / launch_event / watchlist 等挂在桶产品上的整体引用**不动**（它们不是按观测挂的）；
  3. 回滚清单**先落盘再改库**，存改前的值。

★ 低尾闸：迁出/新建只认价格 >= 该桶该币种中位价 20% 的观测。低于的几乎全是词表
  还没收的配件（"ROCK SPACE LAMINA HIDROGEL PARA TABLET LENOVO TAB K10" —— lámina/
  hidrogel 不在 extract 的配件词表里，detect_product_kind 判 device），拿它们去新建
  「Lenovo Tab K10」会造出一个由贴膜价组成的"产品"。这些行留在桶里并单列报告，
  交给 extract 词表补齐 + tools/backfill_accessory_kind.py。

用法：
    python tools/resplit_buckets.py                     # 干跑：全部系列级桶
    python tools/resplit_buckets.py --ids 2292,2291     # 只看这几个
    python tools/resplit_buckets.py --all               # 连 nubimetrics 兜底桶 / 白牌桶也算（慢）
    python tools/resplit_buckets.py --apply             # 真写（先落回滚清单，再重算派生表）
    python tools/resplit_buckets.py --rollback <回滚文件>
    选项：--sample 6  --since-days 30  --limit 40  --no-recompute（只搬不重算，随后手工重算）
         --force（明知采集在跑也要写，不建议）

★ 桶 → 桶：观测细分出来的名字若仍是系列级（"Lenovo Tab" 里的 Legion 标题 → "Lenovo Legion Tab"），
  只在目标桶**已存在**时并入（更窄的桶是真收益），绝不新建一个桶。

★ 干跑只读，不跑迁移。--apply / --rollback 两条写库路径都要求：
  ① rival_product 已有 is_bucket 列；
  ② **采集不在进行**（`_gate()`：/api/health task_running + scrape_run status='running'
     双保险，与 tools/audit_backlog.py 同一份实现，任一为真则 exit 2）。
  这条闸以前只写在文档里、代码里没有 —— 采集中改产品挂接会与采集侧抢锁，
  且会在半路改动它正在写的行。

★ 回滚只删**本次真的新建出来的**产品，且删之前逐张外键表点名检查（_blockers）：
  review / watchlist / launch_event / price_alert / voc_insight 是 ON DELETE CASCADE，
  apply 与 rollback 之间流水线只要给新产品挂上一条评论，回滚就会把它静默级联删掉
  （评论按存档铁律不可再生）；strategy_signal / product_page_cache 没有 CASCADE，
  会抛 IntegrityError 让整份回滚原地失效。任一表非空 ⇒ 保留产品并打印，不删。
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app import db, skunorm                          # noqa: E402
from app.agents.cleaner import identity_for          # noqa: E402

APPLY = "--apply" in sys.argv
ALL = "--all" in sys.argv
FORCE = "--force" in sys.argv
# --no-recompute：只搬观测、作废派生行，不在本进程里重算 price_move / competitor_match
# （测试用；或想把重算放到采集结束后的独立一步）。作废了不重算 = 库里缺变动/匹配，
# runbook 里必须紧跟一步 PriceMoveAgent / rebuild_all。
RECOMPUTE = "--no-recompute" not in sys.argv
ROLLBACK_DIR = ROOT / "data" / "backfill"
TRIM_BELOW = 0.20      # 低尾闸（与 mark_buckets / 上游诊断同口径）
RATIO_GATE = 2.5       # 残留仍算桶的价差门槛（剔低尾后 P90/P10）
MIN_N = 8


def _arg(name: str, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


SAMPLE = int(_arg("--sample", 6))
SINCE_DAYS = int(_arg("--since-days", 30))
LIMIT = int(_arg("--limit", 40))


def _has_cols() -> bool:
    cols = {r["name"] for r in db.q("PRAGMA table_info(rival_product)")}
    return "is_bucket" in cols and "bucket_reason" in cols


# ---------------------------------------------------------------- 采集闸

def _collecting_now() -> tuple[bool, str]:
    """采集是否在进行。判据的单一实现在 tools/audit_backlog.collecting_now
    （/api/health task_running + scrape_run status='running' 双保险）——
    这里只做转发，两份实现迟早不同步。测试替换这个函数来演一次"采集中"。"""
    from tools.audit_backlog import collecting_now  # noqa: PLC0415
    return collecting_now()


def _gate(action: str) -> None:
    """写库前的采集闸。查不出状态时按「不确定即拒跑」处理 —— 采集中改挂接
    会与采集侧抢库锁，还会在半路改动它正在写的行。"""
    if FORCE:
        print(f"[--force] 跳过采集闸，{action} 继续")
        return
    try:
        busy, why = _collecting_now()
    except Exception as e:  # noqa: BLE001
        print(f"✗ 采集闸检查失败（{type(e).__name__}: {str(e)[:100]}）。"
              f"不确定就不写库；确认采集空闲后加 --force。")
        sys.exit(2)
    if busy:
        print(f"✗ 采集正在进行（{why}），{action} 会与它抢库锁并改动它正在写的行，退出。"
              f"等采集结束再跑，或 --force。")
        sys.exit(2)
    if why:
        print(f"  （采集闸：{why}）")


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _spread(prices: list[float]) -> dict | None:
    """某币种的价差体检：raw = max/min；trim = 剔 <20% 中位后的 P90/P10。"""
    v = sorted(p for p in prices if p)
    if len(v) < MIN_N:
        return None
    med = statistics.median(v)
    core = [x for x in v if x >= med * TRIM_BELOW]
    if len(core) < 2:
        return None
    p10, p90 = _pct(core, 0.10), _pct(core, 0.90)
    return {"n": len(v), "raw": v[-1] / v[0] if v[0] else float("inf"),
            "trim": p90 / p10 if p10 else float("inf"), "low_tail": len(v) - len(core),
            "median": med}


# ---------------------------------------------------------------- 候选与计划

def candidates(ids: set[int] | None) -> list[dict]:
    ready = _has_cols()
    extra = ", rp.is_bucket, rp.bucket_reason" if ready else \
            ", 0 AS is_bucket, NULL AS bucket_reason"
    rows = db.q(f"""
        SELECT rp.id, rp.brand_id, b.name AS brand, rp.model_name, rp.model_key,
               rp.category_code, rp.name_source{extra}
        FROM rival_product rp JOIN brand b ON b.id = rp.brand_id
        ORDER BY rp.id""")
    out = []
    for r in rows:
        if ids is not None:
            if r["id"] in ids:
                d = dict(r)
                # 点名的产品不要求判据认它是桶（用户可以拿它体检任何产品），
                # 但报告里要说清是判据认的还是点名的。
                _, why = skunorm.bucket_verdict(r["model_name"], r["name_source"], brand=r["brand"])
                d["bucket_reason"] = r["bucket_reason"] or why or "manual（点名，非判据）"
                out.append(d)
            continue
        is_b, why = skunorm.bucket_verdict(r["model_name"], r["name_source"], brand=r["brand"])
        flagged = bool(r["is_bucket"]) or bool(is_b)
        if not flagged:
            continue
        reason = r["bucket_reason"] or why
        if not ALL and reason != "sku_rules:generic":
            continue
        d = dict(r)
        d["bucket_reason"] = reason
        out.append(d)
    return out


_CAT_MEDIAN: dict[tuple, float] = {}
_ENABLED_CATS: set[str] = set()

_HIST_ACC_WHY = "product_kind 已是 accessory（历史回填/审计判定），配件不挂产品"


def enabled_categories() -> set[str]:
    """启用的分析品类。观测的品类不在其中（NULL / accessory / other 这类禁用品类）时
    不为它新建产品：rival_product.category_code 是 NOT NULL，NULL 会让整个事务回滚；
    禁用品类下新建的产品报告永远看不到，只是多一行没人盯的桶。"""
    if not _ENABLED_CATS:
        _ENABLED_CATS.update(r["code"] for r in db.q("SELECT code FROM category WHERE enabled=1"))
    return _ENABLED_CATS


def category_medians() -> dict[tuple, float]:
    """(品类, 币种) → 整机观测中位价。低尾闸的第二把尺子：一个**整桶都是配件**的桶
    （#6938 Xiaomi Pad 的 51 条全是贴膜/壳）自己的中位价就是配件价，只拿桶内中位
    比什么都拦不住，会把 "ROCK SPACE LAMINA HIDROGEL PARA TABLET XIAOMI PAD 4" 建成
    「Xiaomi Pad 4」。"""
    if _CAT_MEDIAN:
        return _CAT_MEDIAN
    rows = db.q("""SELECT category_code AS cat, currency, sale_price
                   FROM price_obs
                   WHERE sale_price IS NOT NULL AND product_kind = 'device'
                     AND is_bundle = 0 AND condition = 'new' AND category_code IS NOT NULL""")
    acc: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        acc[(r["cat"], r["currency"])].append(float(r["sale_price"]))
    for k, v in acc.items():
        if len(v) >= MIN_N:
            _CAT_MEDIAN[k] = statistics.median(v)
    return _CAT_MEDIAN


def plan_product(p: dict, key_index: dict) -> dict:
    """算一个桶的拆分计划（纯读）。"""
    obs = db.q("""
        SELECT po.id, po.title, po.sku_code, po.category_code, po.brand_id,
               b.name AS brand_name, b.aliases, po.currency, po.sale_price,
               po.obs_date, po.channel_id, po.product_kind, po.model_guess, po.is_bundle
        FROM price_obs po JOIN brand b ON b.id = po.brand_id
        WHERE po.rival_product_id = ?""", (p["id"],))
    cat_med = category_medians()
    ident_cache: dict[tuple, dict] = {}
    med_by_cur: dict[str, float] = {}
    by_cur: dict[str, list[float]] = defaultdict(list)
    for o in obs:
        # 价差体检只看整机、非捆绑：捆绑（"17T Pro + Redmi Pad 2"）是另一个计价口径
        if o["sale_price"] and (o["product_kind"] or "") != "accessory" and not o["is_bundle"]:
            by_cur[o["currency"]].append(float(o["sale_price"]))
    for cur, vals in by_cur.items():
        med_by_cur[cur] = statistics.median(vals)

    groups: dict[str, dict] = {}       # 目标键 → {name, ids, titles, target_id, is_new,...}
    stay, detach, pending, lowtail = [], [], [], []
    stay_prices: dict[str, list[float]] = defaultdict(list)
    enabled = enabled_categories()
    for o in obs:
        price = float(o["sale_price"]) if o["sale_price"] else None
        if (o["product_kind"] or "") == "accessory":
            # ★ 已被回填/审计判成配件却还挂在产品上的历史行（全库 2,509 条）：按 cleaner 的
            #   纪律配件不挂产品，摘出即可 —— 绝不能再拿 identity_for 把它当整机重挂
            #   （实测 #6938「Xiaomi Pad」51 条全是 accessory 的贴膜，不挡会新建出
            #   「Xiaomi Pad 4」「Xiaomi Pad 3」三个由贴膜价组成的"产品"）。
            detach.append({"id": o["id"], "title": o["title"], "why": _HIST_ACC_WHY,
                           "product_kind": o["product_kind"], "model_guess": o["model_guess"],
                           "price": price, "currency": o["currency"]})
            continue
        ck = (o["title"], o["sku_code"], o["category_code"])
        ident = ident_cache.get(ck)
        if ident is None:
            ident = identity_for(dict(o))
            ident_cache[ck] = ident
        med = med_by_cur.get(o["currency"])
        cmed = cat_med.get((o["category_code"], o["currency"]))
        is_low = bool(price and ((med and price < med * TRIM_BELOW)
                                 or (cmed and price < cmed * TRIM_BELOW)))

        if ident["kind"] == "accessory":
            rec = {"id": o["id"], "title": o["title"], "why": ident["bucket_reason"],
                   "product_kind": o["product_kind"], "model_guess": o["model_guess"],
                   "price": price, "currency": o["currency"]}
            # 冲突（权威表整机 / 位置规则配件）= 待定，不是定案配件：实测 31 种标题里
            # 2 种是真平板（Legion Tab 尾巴写着 "Incluye: Folio Case + Mica"）。
            (pending if ident.get("conflict") else detach).append(rec)
            continue
        same = ident["key"] == p["model_key"] and o["category_code"] == p["category_code"]
        tgt_key = (o["brand_id"], ident["key"], o["category_code"])
        if same or (ident["is_bucket"] and tgt_key not in key_index) \
                or (o["category_code"] not in enabled and tgt_key not in key_index):
            # 桶 → 桶只在**目标桶已存在**时迁："Lenovo Tab" 里的 Yoga 标题并进既有的
            # #2934 Lenovo Yoga Tab —— 把 80 万的旗舰线从 15 万的入门桶里摘出来是真收益。
            # 但绝不为它**新建**一个桶（换个品类再建一个 "Apple iPad Air" 什么都没赢，
            # 还多一个要人盯的桶），没有现成目标就留着。
            stay.append(o["id"])
            if price and not o["is_bundle"]:
                stay_prices[o["currency"]].append(price)
            continue
        if is_low:
            lowtail.append({"id": o["id"], "title": o["title"], "new": ident["model"],
                            "price": price, "currency": o["currency"]})
            if price and not o["is_bundle"]:
                stay_prices[o["currency"]].append(price)     # 留在桶里
            continue
        gk = f"{o['brand_id']}|{ident['key']}|{o['category_code']}"
        g = groups.get(gk)
        if g is None:
            tgt = key_index.get((o["brand_id"], ident["key"], o["category_code"]))
            g = groups[gk] = {
                "name": ident["model"], "key": ident["key"], "brand_id": o["brand_id"],
                "category": o["category_code"], "source": ident["source"],
                "verified": ident["verified"], "is_bucket": ident["is_bucket"],
                "bucket_reason": ident["bucket_reason"],
                "target_id": tgt["id"] if tgt else None,
                "target_name": tgt["model_name"] if tgt else None,
                "obs": [], "titles": Counter(), "prices": defaultdict(list)}
        g["obs"].append({"id": o["id"], "model_guess": o["model_guess"]})
        g["titles"][o["title"]] += 1
        if price:
            g["prices"][o["currency"]].append(price)

    before = {cur: _spread(vals) for cur, vals in by_cur.items()}
    after = {cur: _spread(vals) for cur, vals in stay_prices.items()}
    return {"product": p, "n_obs": len(obs), "stay": stay, "detach": detach,
            "pending": pending, "lowtail": lowtail, "groups": groups,
            "before": before, "after": after,
            "n_titles": len({o["title"] for o in obs})}


def residual_verdict(plan: dict) -> str:
    """拆完之后剩下的那部分还像不像桶。"""
    after = [s for s in plan["after"].values() if s]
    if not after:
        return "样本不足"
    worst = max(s["trim"] for s in after)
    return f"残留仍像桶（剔低尾 P90/P10 最大 {worst:.2f}x）" if worst > RATIO_GATE \
        else f"残留像单品（剔低尾 P90/P10 最大 {worst:.2f}x ≤ {RATIO_GATE}，可考虑摘标）"


# ---------------------------------------------------------------- 报告

def _fmt_spread(d: dict[str, dict | None]) -> str:
    parts = []
    for cur in sorted(d):
        s = d[cur]
        if s:
            parts.append(f"{cur} n={s['n']} raw={s['raw']:.1f}x trim={s['trim']:.2f}x 低尾{s['low_tail']}")
    return "; ".join(parts) or "—"


def report(plans: list[dict], derived: dict) -> None:
    n_split = sum(1 for pl in plans if pl["groups"])
    n_moved = sum(len(g["obs"]) for pl in plans for g in pl["groups"].values())
    new_keys = {(g["brand_id"], g["key"], g["category"]) for pl in plans
                for g in pl["groups"].values() if not g["target_id"]}
    n_merge = sum(len(g["obs"]) for pl in plans for g in pl["groups"].values() if g["target_id"])
    n_detach = sum(len(pl["detach"]) for pl in plans)
    n_hist = sum(1 for pl in plans for d in pl["detach"] if d["why"] == _HIST_ACC_WHY)
    n_pend = sum(len(pl["pending"]) for pl in plans)
    n_low = sum(len(pl["lowtail"]) for pl in plans)
    total = sum(pl["n_obs"] for pl in plans)
    print(f"候选桶 {len(plans)} 个 / 观测 {total:,} 条")
    print(f"  会分裂的产品           {n_split}")
    print(f"  观测迁移               {n_moved:,}（并入已有产品 {n_merge:,}，进新建产品 {n_moved - n_merge:,}）")
    print(f"  新建产品               {len(new_keys)}")
    print(f"  配件摘出（rival_product_id 置空 + product_kind=accessory） {n_detach}"
          f"（其中 {n_hist} 条本来就已判 accessory，只是还挂着产品）")
    print(f"  冲突待定（权威表整机/位置规则配件 → 置空 + product_kind=unknown） {n_pend}")
    print(f"  低尾疑似配件（留桶、单列） {n_low}")
    print(f"  留在桶里               {sum(len(pl['stay']) for pl in plans):,}"
          f"（{sum(len(pl['stay']) for pl in plans) / max(total, 1) * 100:.0f}%）")
    print(f"  派生表影响：price_move 近 {SINCE_DAYS} 天 {derived['price_move']} 行删后重算；"
          f"competitor_match {derived['competitor_match']} 行作废后重算；"
          f"watchlist 引用 {derived['watchlist']} 条（不动）")

    shown = 0
    for pl in sorted(plans, key=lambda x: -(sum(len(g["obs"]) for g in x["groups"].values())
                                            + len(x["detach"]))):
        if shown >= LIMIT:
            break
        if not pl["groups"] and not pl["detach"] and not pl["pending"] and not pl["lowtail"]:
            continue
        shown += 1
        p = pl["product"]
        shown_name = p["model_name"] if p["model_name"].lower().startswith(p["brand"].lower()) \
            else f"{p['brand']} {p['model_name']}"
        print(f"\n#{p['id']} {shown_name}  [{p['bucket_reason']}]  "
              f"obs={pl['n_obs']} 标题={pl['n_titles']}")
        print(f"  价差 前：{_fmt_spread(pl['before'])}")
        print(f"  价差 后：{_fmt_spread(pl['after'])}  → {residual_verdict(pl)}")
        print(f"  留桶 {len(pl['stay'])} 条")
        for g in sorted(pl["groups"].values(), key=lambda g: -len(g["obs"])):
            tgt = (f"并入 #{g['target_id']} {g['target_name']}"
                   f"{'（目标仍是桶，只是更窄）' if g['is_bucket'] else ''}") if g["target_id"] else \
                  f"新建（{g['source']}{'，桶' if g['is_bucket'] else ''}）"
            print(f"  → {g['name']:<32} {len(g['obs']):>4} 条  {tgt}")
            for t, n in g["titles"].most_common(SAMPLE):
                print(f"        {n:>3}× {t[:88]}")
        for label_, key_ in (("✂ 配件摘出", "detach"), ("? 冲突待定（product_kind→unknown，逐条人看）", "pending")):
            if pl[key_]:
                print(f"  {label_} {len(pl[key_])} 条：")
                seen: Counter = Counter()
                for d in pl[key_]:
                    seen[(d["title"], d["why"])] += 1
                for (t, why), n in seen.most_common(SAMPLE):
                    print(f"        {n:>3}× {t[:70]}  ← {(why or '')[:50]}")
        if pl["lowtail"]:
            print(f"  ⚠ 低尾疑似配件 {len(pl['lowtail'])} 条（价 < 20% 中位，不迁不建，交给词表/回填）：")
            seen = Counter()
            for d in pl["lowtail"]:
                seen[(d["title"], d["new"], d["currency"])] += 1
            for (t, new, cur), n in seen.most_common(SAMPLE):
                pr = [x["price"] for x in pl["lowtail"] if x["title"] == t and x["price"]]
                print(f"        {n:>3}× {t[:60]}  → 本应 {new}  ({min(pr):,.0f} {cur})" if pr
                      else f"        {n:>3}× {t[:60]}  → 本应 {new}")


def derived_impact(plans: list[dict]) -> dict:
    ids = {pl["product"]["id"] for pl in plans}
    ids |= {g["target_id"] for pl in plans for g in pl["groups"].values() if g["target_id"]}
    if not ids:
        return {"price_move": 0, "competitor_match": 0, "watchlist": 0, "ids": []}
    qs = ",".join("?" * len(ids))
    since = (date.today() - timedelta(days=SINCE_DAYS)).isoformat()
    pm = db.q1(f"SELECT COUNT(*) c FROM price_move WHERE rival_product_id IN ({qs}) "
               f"AND move_date >= ?", (*ids, since))["c"]
    cm = db.q1(f"SELECT COUNT(*) c FROM competitor_match WHERE rival_product_id IN ({qs}) "
               f"AND source='auto' AND is_confirmed=0 AND is_excluded=0", tuple(ids))["c"]
    wl = db.q1(f"SELECT COUNT(*) c FROM watchlist WHERE rival_product_id IN ({qs})", tuple(ids))["c"]
    return {"price_move": pm, "competitor_match": cm, "watchlist": wl, "ids": sorted(ids)}


# ---------------------------------------------------------------- 写库

def apply(plans: list[dict], derived: dict) -> None:
    if not _has_cols():
        print("✗ rival_product 还没有 is_bucket 列。先应用迁移："
              '  python -c "from app import db; db.init_db()"')
        sys.exit(2)
    _gate("--apply")
    ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = ROLLBACK_DIR / f"resplit_buckets_{stamp}.json"
    since = (date.today() - timedelta(days=SINCE_DAYS)).isoformat()

    obs_rows, new_keys = [], []
    for pl in plans:
        for g in pl["groups"].values():
            for o in g["obs"]:
                obs_rows.append({"id": o["id"], "rival_product_id": pl["product"]["id"],
                                 "model_guess": o["model_guess"], "product_kind": None})
            if not g["target_id"]:
                # ★ pre_existing_id：计划新建、但**库里此刻已有同键行**（别的桶刚建的 /
                #   无挂牌产品）。回滚按键找回新建行时必须认得出它 —— 否则会把一个
                #   apply 之前就存在的产品当成"我们建的"删掉。
                pre = db.q1("""SELECT id FROM rival_product
                               WHERE brand_id=? AND model_key=? AND category_code=?""",
                            (g["brand_id"], g["key"], g["category"]))
                new_keys.append({"brand_id": g["brand_id"], "key": g["key"],
                                 "category": g["category"], "name": g["name"],
                                 "pre_existing_id": pre["id"] if pre else None})
        for d in pl["detach"] + pl["pending"]:
            obs_rows.append({"id": d["id"], "rival_product_id": pl["product"]["id"],
                             "model_guess": d["model_guess"], "product_kind": d["product_kind"]})
    # ★ 先落盘：改前的值 + 计划新建的键（回滚时按键找回新建行）
    path.write_text(json.dumps({"kind": "resplit_buckets", "at": stamp, "since": since,
                                "affected_ids": derived["ids"], "price_obs": obs_rows,
                                "new_products": new_keys, "created_ids": []},
                               ensure_ascii=False), encoding="utf-8")

    created: dict[tuple, int] = {}        # 键 → 目标产品 id（新建的 + 组外占键复用的）
    inserted: dict[tuple, int] = {}       # ★ 只装**本次真的 INSERT 出来的**
    reused: dict[tuple, int] = {}         # 组外占键查到的既有产品：绝不能进回滚删除清单
    n_move = n_detach = n_pm = n_cm = 0
    with db.tx() as conn:
        for pl in plans:
            for g in pl["groups"].values():
                tgt = g["target_id"]
                if not tgt:
                    k = (g["brand_id"], g["key"], g["category"])
                    tgt = created.get(k)
                    if tgt is None:
                        # 组外占键检查：库里可能已有同键行（别的桶刚建的 / 无挂牌产品）
                        row = conn.execute("""SELECT id FROM rival_product
                                              WHERE brand_id=? AND model_key=? AND category_code=?""",
                                           k).fetchone()
                        if row:
                            # ★ 既有产品：只是被复用当目标，**不是本次新建的**。
                            #   写进 created_ids 的话，回滚会连它一起删 ——
                            #   它名下原有的观测/评论/关注全跟着 CASCADE 掉。
                            tgt = row[0]
                            reused[k] = tgt
                        else:
                            cur = conn.execute("""
                                INSERT INTO rival_product(brand_id,category_code,model_name,model_key,
                                    name_verified,name_source,is_bucket,bucket_reason)
                                VALUES(?,?,?,?,?,?,?,?)""",
                                (g["brand_id"], g["category"], g["name"], g["key"],
                                 1 if g["verified"] else 0, g["source"],
                                 1 if g["is_bucket"] else 0,
                                 g["bucket_reason"] if g["is_bucket"] else None))
                            tgt = cur.lastrowid
                            inserted[k] = tgt
                        created[k] = tgt
                ids = [o["id"] for o in g["obs"]]
                qs = ",".join("?" * len(ids))
                n_move += conn.execute(
                    f"UPDATE price_obs SET rival_product_id=?, model_guess=? WHERE id IN ({qs})",
                    (tgt, g["name"], *ids)).rowcount
            for key_, kind_ in (("detach", "accessory"), ("pending", "unknown")):
                if pl[key_]:
                    ids = [d["id"] for d in pl[key_]]
                    qs = ",".join("?" * len(ids))
                    n_detach += conn.execute(
                        f"UPDATE price_obs SET rival_product_id=NULL, model_guess=NULL, "
                        f"product_kind=? WHERE id IN ({qs})", (kind_, *ids)).rowcount
        # 派生表：只删，收尾重算（apply_reclass 的 DERIVED_DROP 做法）
        aff = set(derived["ids"]) | set(created.values())
        if aff:
            qs = ",".join("?" * len(aff))
            n_pm = conn.execute(f"DELETE FROM price_move WHERE rival_product_id IN ({qs}) "
                                f"AND move_date >= ?", (*aff, since)).rowcount
            n_cm = conn.execute(f"DELETE FROM competitor_match WHERE rival_product_id IN ({qs}) "
                                f"AND source='auto' AND is_confirmed=0 AND is_excluded=0",
                                tuple(aff)).rowcount
    data = json.loads(path.read_text(encoding="utf-8"))
    data["created_ids"] = sorted(inserted.values())      # ★ 只有真新建的进得来
    data["reused_ids"] = sorted(reused.values())
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"[已执行] 观测迁移 {n_move} 条，配件摘出/冲突待定 {n_detach} 条，"
          f"新建产品 {len(inserted)} 个（另有 {len(reused)} 个是组外占键复用的既有产品，回滚不删）；"
          f"作废 price_move {n_pm} 行、competitor_match {n_cm} 行；回滚清单 {path}")

    # ★ 收尾必须重算，否则库里留着按旧桶算出来的变动/匹配，不报错、不变色。
    if not RECOMPUTE:
        print(f"[--no-recompute] 已作废 price_move {n_pm} 行 / competitor_match {n_cm} 行但**没有重算**，"
              f"请随后跑：PriceMoveAgent().run(obs_date=d)（{since} 起逐日）与 CompetitorMatcher().rebuild_all()")
        return
    try:
        from app.agents.pricemove import PriceMoveAgent  # noqa: PLC0415
        days = [r["d"] for r in db.q("""SELECT DISTINCT obs_date d FROM price_obs
                                        WHERE obs_date >= ? ORDER BY obs_date""", (since,))]
        for d in days:
            PriceMoveAgent().run(obs_date=d)
        print(f"已按日重算价格变动 {len(days)} 天（{since} 起）")
    except Exception as e:  # noqa: BLE001
        print(f"! 价格变动重算失败：{str(e)[:120]}\n  请手工逐日跑 PriceMoveAgent().run(obs_date=d)")
    try:
        from app.matching.matcher import CompetitorMatcher  # noqa: PLC0415
        res = CompetitorMatcher().rebuild_all()
        print(f"已重算竞品匹配：{res.get('matches')} 条")
    except Exception as e:  # noqa: BLE001
        print(f"! 竞品匹配重算失败：{str(e)[:120]}\n  请手工跑 CompetitorMatcher().rebuild_all()")


# 本工具自己作废并重算的派生表：删产品前可以连它们一起删（apply 刚作废过一轮）。
# 除这两张之外的任何引用都是**别人写的内容**，一律拦住删除。
_OWNED_DERIVED = frozenset({"price_move", "competitor_match"})


def _referencing_tables(conn) -> list[tuple[str, str, str]]:
    """库里所有指向 rival_product(id) 的外键：(表, 列, on_delete)。

    ★ 从 PRAGMA 读，不写死清单：写死的清单迟早漏掉后加的表，而漏掉的那张要么
      带 ON DELETE CASCADE 被**静默**删掉（review 按存档铁律不可再生），
      要么没有 CASCADE ⇒ 抛 IntegrityError 让整份回滚原地失效（事务回滚，
      日志与"什么都没干"长得一模一样）。
    """
    out: list[tuple[str, str, str]] = []
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        for fk in conn.execute(f'PRAGMA foreign_key_list("{t}")'):
            if (fk["table"] or "").lower() == "rival_product":
                out.append((t, fk["from"], (fk["on_delete"] or "").upper()))
    return sorted(out)


def _blockers(conn, pid: int) -> list[str]:
    """删这个产品之前，还有谁在引用它。空 = 可以删。"""
    out = []
    for tbl, col, on_del in _referencing_tables(conn):
        if tbl in _OWNED_DERIVED:
            continue
        n = conn.execute(f'SELECT COUNT(*) FROM "{tbl}" WHERE "{col}"=?', (pid,)).fetchone()[0]
        if n:
            out.append(f"{tbl}.{col}={n} 行（ON DELETE {on_del or '无'}）")
    return out


def rollback(path: str) -> None:
    _gate("--rollback")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    kept = 0
    with db.tx() as conn:
        for r in data["price_obs"]:
            if r.get("product_kind") is not None:
                conn.execute("UPDATE price_obs SET rival_product_id=?, model_guess=?, product_kind=? "
                             "WHERE id=?", (r["rival_product_id"], r["model_guess"],
                                            r["product_kind"], r["id"]))
            else:
                conn.execute("UPDATE price_obs SET rival_product_id=?, model_guess=? WHERE id=?",
                             (r["rival_product_id"], r["model_guess"], r["id"]))
        ids = list(data.get("created_ids") or [])
        reused = set(data.get("reused_ids") or [])
        # ★ created_ids 在改库之前落盘时是空的（进程死在中途也要能回滚），
        #   所以还要按计划新建的键找一遍；但**只认不是 apply 之前就存在的那些**：
        #   pre_existing_id 是 apply 当时查到的组外占键行，它不是我们建的。
        for nk in data.get("new_products") or []:
            row = conn.execute("""SELECT id FROM rival_product
                                  WHERE brand_id=? AND model_key=? AND category_code=?""",
                               (nk["brand_id"], nk["key"], nk["category"])).fetchone()
            if not row:
                continue
            rid = row[0]
            if rid == nk.get("pre_existing_id") or rid in reused:
                continue
            if rid not in ids:
                ids.append(rid)
        for pid in [i for i in ids if i not in reused]:
            # ★★ 删产品前逐张外键表点名检查（不只是 price_obs）：
            #   review / watchlist / launch_event / price_alert / voc_insight /
            #   review_profile / rival_sku 是 ON DELETE CASCADE —— apply 与 rollback
            #   之间流水线只要给新产品挂上一条评论，这一删就把评论**静默**级联删掉，
            #   而评论按存档铁律不可再生；strategy_signal / product_page_cache 没有
            #   CASCADE —— 会抛 IntegrityError 把整份回滚（连观测归位一起）全部撤销。
            #   两种后果都不出错误信号，所以宁可留下一个空产品：留着是脏，删错是丢数据。
            why = _blockers(conn, pid)
            if why:
                kept += 1
                print(f"  ! 新建产品 #{pid} 仍被引用，保留不删：{'；'.join(why)}")
                continue
            for t in sorted(_OWNED_DERIVED):
                conn.execute(f"DELETE FROM {t} WHERE rival_product_id=?", (pid,))
            conn.execute("DELETE FROM rival_product WHERE id=?", (pid,))
    n_del = len([i for i in ids if i not in reused]) - kept
    print(f"[已回滚] 观测 {len(data['price_obs'])} 条；删除本次新建产品 {n_del} 个"
          f"（保留 {kept} 个仍被引用的、{len(reused)} 个组外占键复用的）；"
          f"price_move / competitor_match 需重新跑一遍（PriceMoveAgent / rebuild_all）")


def main() -> int:
    if "--rollback" in sys.argv:
        rollback(_arg("--rollback", ""))
        return 0
    ids_arg = _arg("--ids", "")
    ids = {int(x) for x in ids_arg.split(",") if x.strip()} if ids_arg else None
    cands = candidates(ids)
    if not cands:
        print("没有候选桶。")
        return 0
    key_index = {(r["brand_id"], r["model_key"], r["category_code"]): r
                 for r in db.q("SELECT id, brand_id, model_key, category_code, model_name "
                               "FROM rival_product")}
    plans = [plan_product(p, key_index) for p in cands]
    derived = derived_impact(plans)
    report(plans, derived)
    if not APPLY:
        print("\n[试运行] 没有改动任何数据。确认后加 --apply 执行"
              "（需先应用迁移；采集在跑会被 _gate 拦下并 exit 2）。")
        return 0
    apply(plans, derived)
    return 0


if __name__ == "__main__":
    sys.exit(main())
