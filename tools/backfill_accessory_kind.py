# -*- coding: utf-8 -*-
"""历史回填：price_obs.product_kind 误标的整机/配件 → 按**采集端同一条路径**重判。

背景：
  2026-08-27 Fast Shop 巴西的葡语壳/膜被标成 device（三轮回填 1,200 行）。
  2026-09-04 用户截图 #2292「Lenovo Tab」曲线在 18 万与 8,500 CLP 间来回跳 —— 8,500 是一张钢化膜
  「GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11」，被权威表的品牌级兜底规则短路判整机；
  60 条人工归类的真配件里旧管线只认出 2 条（召回 5%）。

★ 判定规则不在本文件里。judge() 走的是 base.ChannelAdapter._enrich_from_title —— 采集端
  **同一个函数**（fix_mojibake → strip_ui_chrome → skumap.classify(品类) → detect_product_kind），
  不是抄一份词表。上一版自带的 judge() 只复用了 accessory_para_form：干跑 218 行 vs 现行分类器
  263 行，差的 45 行没人知道（知识页：规则单一实现多处消费，否则差集永远没人发现）。
  唯一附加的一道是 extract.looks_like_device_bundle 捆绑守卫：skumap 的两条 yaml 规则
  （「keyboard 无 gb/ram」「contains apple pencil」）会把 749,990 CLP 的
  「Slate 12X with keyboard inbox」判成配件 —— 回填不能跟着误杀。这道守卫本该长在 skumap.py
  （A4），那不在本工具的改动范围，先在这里挡住，并把被挡下的行打印出来给人看。

四个桶（干跑全部打印；写库范围由开关决定）：
  flip          device → accessory  采集路径判 accessory 且不是捆绑整机（默认写）
  pending       device → unknown    采集路径判 unknown：设备名打头紧接壳膜、同类兼容品/仿品
                                    —— 加 --include-pending 才写（三态：待定不是定案）
  sku_conflict  报告               skumap 命中 SKU 短路判整机、detect_product_kind 判配件
                                    （cleaner「冲突即待定」同口径）—— 加 --include-sku-conflict 置 unknown
  bundle_guard  不动               采集路径判 accessory，但 looks_like_device_bundle 说是整机捆绑装

用法：
    python tools/backfill_accessory_kind.py                        # 干跑（默认；--dry 等价）
    python tools/backfill_accessory_kind.py --sample 40 --residual 10
    python tools/backfill_accessory_kind.py --since 2026-07-01 --limit 5000
    python tools/backfill_accessory_kind.py --apply [--include-pending] [--include-sku-conflict]
    python tools/backfill_accessory_kind.py --rollback data/backfill/accessory_kind_<stamp>.json

★ 回滚清单先落盘再改库，存**改前的值** + 标题 + 依据；文件名带毫秒+进程号+序号，同秒连跑不覆盖
  （tests/test_categorybackfill 在兄弟工具上抓过「按秒命名同秒覆盖唯一后悔药」）。
★ 采集闸（与 tools/audit_backlog.py 同一个 collecting_now，单一实现）：
  --apply / --rollback 在采集进行中一律拒跑（/api/health task_running 或 scrape_run status='running'
  任一为真 → 打印原因 exit 2），--force 才放行；干跑不受闸门限制。
  ★ 闸必须在**回滚清单落盘之前** —— 清单一旦造出来就是一份没有对应改动的脏文件，
    下次找"最新那份清单"回滚会拿到它（而它是空动作），真正的后悔药反而被挤到第二位。
  ★ 为什么采集在跑就不许写：product_kind 是采集端**正在写入**的同一列，
    并发改判会让当轮入库的行拿到半新半旧的口径，而这件事不报错。
★ 回填后 competitor_match / price_move 必须重算（product_kind 是它们的输入；同 categorizer._apply
  的作废纪律）—— 本工具不自动跑，收尾会打印命令。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                                  # noqa: E402
from app.scraping import extract                                    # noqa: E402
from app.scraping.channels.base import ChannelAdapter, Listing      # noqa: E402
from tools.audit_backlog import collecting_now                      # noqa: E402  单一实现，复用

APPLY = "--apply" in sys.argv
FORCE = "--force" in sys.argv
INCLUDE_PENDING = "--include-pending" in sys.argv
INCLUDE_SKU_CONFLICT = "--include-sku-conflict" in sys.argv
ROLLBACK_DIR = ROOT / "data" / "backfill"

B_FLIP, B_PENDING, B_SKU_CONFLICT, B_BUNDLE = "flip", "pending", "sku_conflict", "bundle_guard"
NEW_KIND = {B_FLIP: "accessory", B_PENDING: "unknown", B_SKU_CONFLICT: "unknown"}
BUCKET_LABEL = {
    B_FLIP: "device → accessory（采集路径判配件）",
    B_PENDING: "device → unknown（采集路径判待定：设备名打头紧接壳膜 / 同类兼容品仿品）",
    B_SKU_CONFLICT: "SKU 短路冲突（skumap 判整机、detect_product_kind 判配件）—— 默认只报告",
    B_BUNDLE: "捆绑守卫保住（采集路径判配件，但形态是整机 + 配件捆绑装）—— 不动",
}


def _blocked(action: str, busy: bool, why: str) -> bool:
    """采集闸的**播报**部分。判定由调用方直接调 collecting_now() 拿到（见下面三处）。

    ★ 故意不把 collecting_now() 收进本函数：闸门的存在与位置要能被 ast 断言逐个函数验，
      藏进 helper 之后「main 里到底有没有闸」就只能靠读代码了
      （tests/test_backfill_accessory_kind.py 的 ast 断言 + 反向自检守这一条）。
    """
    if not busy or FORCE:
        return False
    print(f"! 采集进行中（{why}），拒绝 {action}。确认无碍可加 --force", file=sys.stderr)
    return True


def _arg(name: str, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


# ---------------------------------------------------------------- 判定（采集端同一条路径）

def capture_path(title: str, category: str | None) -> Listing:
    """采集端看到的文本形态 + 采集端的判定 —— 就是 base._enrich_from_title 本尊，不是抄的。"""
    lst = Listing(title=title or "")
    ChannelAdapter._enrich_from_title(SimpleNamespace(current_category=category), lst)
    return lst


def judge(title: str, category: str | None) -> tuple[str | None, str | None, str]:
    """这条观测按今天的采集端规则该落哪个桶？返回 (桶, 新 product_kind 或 None, 依据)。"""
    if not (title or "").strip():
        return None, None, ""
    lst = capture_path(title, category)
    low = lst.title.lower()
    reason = lst.specs.get("_sku_reason") or lst.specs.get("_kind_reason") or ""
    # 页面框架文案（筛选面板/品类导航被截断进了标题）不是商品：采集端 base.py 会在建 Listing 前过滤，
    # 历史行漏进来的按「待定」处理，自动排除出趋势/看板（它们只认 product_kind='device'）。
    if extract.looks_like_page_chrome(lst.title):
        return B_PENDING, NEW_KIND[B_PENDING], "页面框架文案（筛选面板/导航截断），不是商品 → 待定"
    if lst.product_kind == "accessory":
        bundle = extract.looks_like_device_bundle(low)
        if bundle:
            return B_BUNDLE, None, bundle
        return B_FLIP, NEW_KIND[B_FLIP], reason
    if lst.product_kind == "unknown":
        # ★ 只有**具体的**三态判定（设备名打头紧接壳膜 / 同类兼容品仿品，理由带「待定」）才进待定桶。
        #   通用兜底「标题无明确整机/配件信号」不是反证：库里这些行是别的口径（LLM 品类层、
        #   品牌商城 adapter、历史回填）判成 device 的 —— 「Oppo Reno14 5G 512GB Telcel」「Honor 600E」
        #   「LaptopHP 14-FM0013DX」都是真机，只是词表不认识那条产品线。没证据就不动。
        why = reason or extract.detect_product_kind(lst.title)[1]
        if "待定" in why:
            return B_PENDING, NEW_KIND[B_PENDING], why
        return None, None, ""
    if lst.sku_code:
        # 采集端对命中 SKU 的行无条件写死 device（base.py 控制流，C1 待改）；这里与 cleaner 一样
        # 再过一遍位置规则：判配件的归「冲突」桶，判待定（设备名打头紧接壳膜/笔）的归「待定」桶 ——
        # 三态原则下都不直接定案配件（「XIAOMI Pad Mini Cover」「Lenovo Tab Pen Plus lápiz」）。
        kind, why = extract.detect_product_kind(lst.title)
        if kind == "accessory" and not extract.looks_like_device_bundle(low):
            return (B_SKU_CONFLICT, NEW_KIND[B_SKU_CONFLICT],
                    f"sku_rules 命中 {lst.sku_code!r} 但 detect_product_kind 判配件：{why}")
        if kind == "unknown" and "待定" in why:
            return B_PENDING, NEW_KIND[B_PENDING], f"sku_rules 命中 {lst.sku_code!r} 但 {why}"
    return None, None, ""


# ---------------------------------------------------------------- 扫描

def scan(since: str | None = None, limit: int | None = None) -> tuple[dict[str, list[dict]], int]:
    """扫 product_kind='device' 的观测，按桶分组。返回 ({桶: 行}, 扫描总行数)。"""
    sql = """
        SELECT po.id, po.obs_date, po.captured_at, po.country_code, po.category_code, po.title,
               po.sale_price, po.currency, po.product_kind, po.condition, po.sku_code,
               po.rival_product_id, po.audit_status,
               b.name AS brand, c.name AS channel, c.kind AS channel_kind
        FROM price_obs po
        LEFT JOIN brand b   ON b.id = po.brand_id
        LEFT JOIN channel c ON c.id = po.channel_id
        WHERE po.product_kind = 'device'"""
    params: list = []
    if since:
        sql += " AND po.obs_date >= ?"
        params.append(since)
    sql += " ORDER BY po.id"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = db.q(sql, params)

    buckets: dict[str, list[dict]] = {B_FLIP: [], B_PENDING: [], B_SKU_CONFLICT: [], B_BUNDLE: []}
    cache: dict[tuple[str, str | None], tuple] = {}
    for i, r in enumerate(rows, 1):
        key = (r["title"] or "", r["category_code"])
        if key not in cache:
            cache[key] = judge(*key)
        bucket, new_kind, why = cache[key]
        # ★ 品牌自营商城（Acme商城/Apple Store/Samsung Shop）的「待定」不算：那些站的 adapter
        #   （sites.py）在 base 判定**之后**还有一道「品牌商城不卖别人的东西，非配件即整机」，
        #   「Astra X7 16GB+512GB」「WATCH ULTIMATE DESIGN」在采集端就是 device。首版干跑
        #   1,442 行待定里 744 行是它们 —— 本工具只复现了 base 那一步，这里补上那道口径。
        if bucket == B_PENDING and r["channel_kind"] == "brand_store" \
                and not extract.looks_like_page_chrome(r["title"] or ""):
            bucket = None
        if bucket:
            d = dict(r)
            d["_why"], d["_new"], d["_bucket"] = why, new_kind, bucket
            buckets[bucket].append(d)
        if i % 20000 == 0:
            print(f"  … 已扫 {i}/{len(rows)}", file=sys.stderr)
    return buckets, len(rows)


# ---------------------------------------------------------------- 报告

def _show(h: dict) -> None:
    price = f"{h['sale_price']:,.0f} {h['currency']}" if h.get("sale_price") else "—"
    print(f"  [{h['id']}] {h['obs_date']} {h['country_code']}/{h.get('channel') or '—'} "
          f"{h.get('category_code') or '—'} {price}  audit={h.get('audit_status')}")
    print(f"        {(h['title'] or '')[:110]}")
    print(f"        → {h['_why']}")


def _dist(hits: list[dict]) -> None:
    for label, key in (("国家", "country_code"), ("渠道", "channel"),
                       ("品类", "category_code"), ("品牌", "brand")):
        c = Counter(str(h.get(key) or "—") for h in hits)
        if c:
            print(f"  按{label}： " + "  ".join(f"{k}:{v}" for k, v in c.most_common(8)))
    dates = sorted(h["obs_date"] for h in hits if h.get("obs_date"))
    if dates:
        print(f"  日期跨度： {dates[0]} → {dates[-1]}")
    print(f"  不同标题： {len({h['title'] for h in hits})}   涉及产品： "
          f"{len({h['rival_product_id'] for h in hits if h.get('rival_product_id')})}   "
          f"audit pending： {sum(1 for h in hits if h.get('audit_status') == 'pending')}")


def _two_ended_sample(hits: list[dict], n_sample: int) -> None:
    """★ 抽样两头、按币种分组（知识页）：低价端只证明配件抓到了，**误杀整机只出现在高价端**；
    跨币种排序比的是币种不是价格，所以高价端按币种各取最贵几条。"""
    half = max(n_sample // 2, 1)
    print(f"\n—— 抽样 A：最便宜 {half} 条（确认配件确实被抓到；按币种轮转）——")
    by_cur: dict[str, list] = defaultdict(list)
    for h in hits:
        by_cur[h["currency"] or "—"].append(h)
    ordered = {cur: sorted(v, key=lambda x: (x["sale_price"] or 0)) for cur, v in by_cur.items()}
    shown, k = 0, 0
    while shown < half and any(len(v) > k for v in ordered.values()):
        for cur in sorted(ordered):
            if k < len(ordered[cur]) and shown < half:
                _show(ordered[cur][k])
                shown += 1
        k += 1
    print(f"\n—— ★ 抽样 B：各币种最贵的几条（**误杀整机就藏在这里**）——")
    per_cur = max(half // max(len(by_cur), 1), 2)
    for cur in sorted(by_cur):
        print(f"  【{cur}】")
        for h in sorted(by_cur[cur], key=lambda x: -(x["sale_price"] or 0))[:per_cur]:
            _show(h)


def _residual(hits_all: dict[str, list[dict]], n: int, since: str | None) -> None:
    """残留体检：每 (品类, 币种) 最便宜 n 条**仍为 device** 且全新的行（去重标题）逐条看。
    知识页：残留体检是分类器修复的验收手段，比测试全绿有力（三轮 658→240→302 都是这么发现的）。"""
    touched = {h["id"] for b in (B_FLIP, B_PENDING, B_SKU_CONFLICT) for h in hits_all[b]}
    sql = """SELECT po.id, po.category_code, po.currency, po.sale_price, po.title
             FROM price_obs po
             WHERE po.product_kind='device' AND po.condition='new' AND po.is_bundle=0
               AND po.sale_price IS NOT NULL AND po.category_code IS NOT NULL"""
    params: list = []
    if since:
        sql += " AND po.obs_date >= ?"
        params.append(since)
    sql += " ORDER BY po.category_code, po.currency, po.sale_price"
    groups: dict[tuple, list] = defaultdict(list)
    seen: dict[tuple, set] = defaultdict(set)
    for r in db.q(sql, params):
        k = (r["category_code"], r["currency"])
        if r["id"] in touched or len(groups[k]) >= n or r["title"] in seen[k]:
            continue
        seen[k].add(r["title"])
        groups[k].append(r)
    print(f"\n—— ★ 残留体检：每 (品类, 币种) 最便宜 {n} 条仍为 device 的全新行（去重标题）——")
    for k in sorted(groups):
        print(f"  [{k[0]}/{k[1]}]")
        for r in groups[k]:
            print(f"    {r['sale_price']:>14,.0f}  #{r['id']}  {(r['title'] or '')[:95]}")


def report(buckets: dict[str, list[dict]], total: int, n_sample: int) -> None:
    print(f"\n扫描 product_kind='device' 观测 {total} 行")
    for b in (B_FLIP, B_PENDING, B_SKU_CONFLICT, B_BUNDLE):
        hits = buckets[b]
        pct = len(hits) / max(total, 1) * 100
        print(f"\n===== {BUCKET_LABEL[b]}：{len(hits)} 行（{pct:.2f}%）=====")
        if not hits:
            continue
        _dist(hits)
        if b == B_FLIP:
            _two_ended_sample(hits, n_sample)
        else:
            # 这三桶都是「人要看一眼」的：待定/冲突要人裁，守卫保住的要确认真是整机
            by_cur: dict[str, list] = defaultdict(list)
            for h in hits:
                by_cur[h["currency"] or "—"].append(h)
            per_cur = max(min(n_sample, 12) // max(len(by_cur), 1), 2)
            for cur in sorted(by_cur):
                print(f"  【{cur}】（按价格从高到低）")
                for h in sorted(by_cur[cur], key=lambda x: -(x["sale_price"] or 0))[:per_cur]:
                    _show(h)


# ---------------------------------------------------------------- 写库 / 回滚

def _manifest_path() -> Path:
    """回滚清单路径：秒 + 毫秒 + 进程号 + 序号，同秒（甚至同毫秒）连跑也不覆盖。"""
    ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}-{os.getpid()}"
    for seq in range(1000):
        p = ROLLBACK_DIR / f"accessory_kind_{stamp}-{seq:02d}.json"
        try:
            p.touch(exist_ok=False)          # 当场占位：同毫秒再要一次也拿不到同一个名字
        except FileExistsError:
            continue
        return p
    raise RuntimeError("回滚清单序号用尽")


def selected_for_apply(buckets: dict[str, list[dict]], *, include_pending: bool = False,
                       include_sku_conflict: bool = False) -> list[dict]:
    out = list(buckets[B_FLIP])
    if include_pending:
        out += buckets[B_PENDING]
    if include_sku_conflict:
        out += buckets[B_SKU_CONFLICT]
    return out


def apply_(hits: list[dict]) -> Path | None:
    # ★ 采集闸放在**最前面**：必须早于 _manifest_path()（它会 touch 出一个占位文件）
    #   和 db.tx()。顺序反了会留下一份「有清单、无改动」的脏后悔药。
    busy, why = collecting_now()
    if _blocked("--apply（写库）", busy, why):
        return None
    if not hits:
        print("没有需要回填的行。")
        return None
    path = _manifest_path()
    # ★ 回滚清单先落盘再改库：反过来的话进程中途挂掉就再也回不去了。存的是**改之前的值**。
    path.write_text(json.dumps(
        {"created": datetime.now().isoformat(timespec="seconds"),
         "note": "product_kind 回填（device → accessory/unknown），rows[].old 是改前的值",
         "rows": [{"id": h["id"], "old": h["product_kind"], "new": h["_new"], "bucket": h["_bucket"],
                   "title": h["title"], "reason": h["_why"]} for h in hits]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n回滚清单已写：{path}")

    by_new: dict[str, list[int]] = defaultdict(list)
    for h in hits:
        by_new[h["_new"]].append(h["id"])
    changed = 0
    with db.tx() as conn:
        for new_kind, ids in by_new.items():
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                cur = conn.execute(
                    f"UPDATE price_obs SET product_kind=? "
                    f"WHERE id IN ({','.join('?' * len(chunk))}) AND product_kind='device'",
                    [new_kind, *chunk])
                changed += cur.rowcount
    ids_all = [h["id"] for h in hits]
    left = 0
    for i in range(0, len(ids_all), 900):
        chunk = ids_all[i:i + 900]
        left += db.q1("SELECT COUNT(*) n FROM price_obs WHERE product_kind='device' "
                      f"AND id IN ({','.join('?' * len(chunk))})", chunk)["n"]
    print(f"✓ 已更新 {changed} 行（计划 {len(ids_all)}）；复查仍为 device 的：{left}（应为 0）")
    print("★ 下一步：product_kind 是竞品匹配/价格变动的输入，必须重算 ——\n"
          "   python -c \"from app.matching.matcher import CompetitorMatcher; CompetitorMatcher().rebuild_all()\"\n"
          "   然后再干跑一次本工具，第二遍集合应为 0。")
    return path


def rollback(path: str) -> int:
    # ★ 回滚同样是写库：采集在跑就别动（口径与 --apply 一致，exit 2）。闸在读清单之前。
    busy, why = collecting_now()
    if _blocked("--rollback（写库）", busy, why):
        return 2
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    n = 0
    with db.tx() as conn:
        for r in rows:
            # 只回滚仍带着本次写入值的行：之后被别的流程改过的不碰
            cur = conn.execute("UPDATE price_obs SET product_kind=? WHERE id=? AND product_kind=?",
                               (r["old"], r["id"], r.get("new") or "accessory"))
            n += cur.rowcount
    print(f"✓ 已回滚 {n}/{len(rows)} 行到改前的值")
    return 0


# ---------------------------------------------------------------- 入口

def main() -> int:
    rb = _arg("--rollback", None)
    if rb:
        return rollback(rb)

    # ★ --apply 的闸在这里先探一次（早于扫全表的几十秒，也早于任何落盘）；
    #   apply_() 里还有一道，两处都直接调 collecting_now()，谁被单独调用都拦得住。
    if APPLY:
        busy, why = collecting_now()
        if _blocked("--apply（写库）", busy, why):
            return 2

    since = _arg("--since", None)
    limit = _arg("--limit", None)
    buckets, total = scan(since, int(limit) if limit else None)
    report(buckets, total, int(_arg("--sample", 40)))
    residual = _arg("--residual", None)
    if residual:
        _residual(buckets, int(residual), since)

    sel = selected_for_apply(buckets, include_pending=INCLUDE_PENDING,
                             include_sku_conflict=INCLUDE_SKU_CONFLICT)
    print(f"\n本次写库范围：{len(sel)} 行"
          f"（flip {len(buckets[B_FLIP])}"
          f"{' + pending ' + str(len(buckets[B_PENDING])) if INCLUDE_PENDING else ''}"
          f"{' + sku_conflict ' + str(len(buckets[B_SKU_CONFLICT])) if INCLUDE_SKU_CONFLICT else ''}）")
    if not APPLY:
        print("\n[干跑] 未改动任何数据。确认两头抽样与残留体检无误后执行：")
        print("       python tools/backfill_accessory_kind.py --apply"
              + (" --include-pending" if INCLUDE_PENDING else "")
              + (" --include-sku-conflict" if INCLUDE_SKU_CONFLICT else ""))
        return 0
    apply_(sel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
