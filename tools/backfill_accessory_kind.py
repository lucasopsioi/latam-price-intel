# -*- coding: utf-8 -*-
"""历史回填：被误标成 device 的葡语/西语配件 → accessory。

背景（2026-08-27）：采集端分类器的配件词表移植自用户 PowerQuery，
天生只有西语+英语，葡语一个词都拦不住 —— Fast Shop 巴西的
「Capa para Tablet Acme Slate Tab」（壳）、「Película para ACME Slate 11.5」（膜）、
「Capa … com Teclado Bluetooth」（键盘壳）全被标成 product_kind='device'，
混进价格分析，把Acme巴西平板 ASP 拉到 20 美元。

★ 判定规则不在本文件里 —— 用的是 `extract.accessory_para_form`，
  与修好后的采集端分类器（detect_product_kind / skumap.is_accessory）**同一份实现**。
  分开写就会出现"分类器修好了、回填用的是另一套规则"的分叉，
  而两套规则的差集永远不会有人发现。

★ 判定要跑在**分类器实际看到的那个文本形态**上，两条都跑取并集：
    - fix_mojibake(title).lower()      ← detect_product_kind 走这条
    - skumap.normalize(title)          ← 权威表走这条（去重音）
  历史行里两种形态都存在（乱码修复是后来才加的），只查一条会漏。

用法：
    python tools/backfill_accessory_kind.py            # 干跑：统计 + 抽样，不改库
    python tools/backfill_accessory_kind.py --sample 50
    python tools/backfill_accessory_kind.py --apply    # 真的改（先写回滚清单）
    python tools/backfill_accessory_kind.py --rollback <回滚文件>

★ 采集正在跑时也能安全执行：本脚本是独立进程 + WAL 库，
  只做一次短事务的 UPDATE，不需要停服务（tools/restart.ps1 有闸，别去动它）。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, skumap                      # noqa: E402
from app.scraping import extract                # noqa: E402

APPLY = "--apply" in sys.argv
ROLLBACK_DIR = ROOT / "data" / "backfill"


def _arg(name: str, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def judge(title: str) -> str | None:
    """这条标题按修好后的规则是不是配件？返回依据，不是则 None。"""
    if not title:
        return None
    # ① 通用启发式看到的形态
    why = extract.accessory_para_form(extract.fix_mojibake(title).lower().strip())
    if why:
        return why
    # ② 权威表看到的形态（去重音、压空白）
    return extract.accessory_para_form(skumap.normalize(title))


def scan() -> list[dict]:
    rows = db.q("""
        SELECT po.id, po.obs_date, po.country_code, po.category_code, po.title,
               po.sale_price, po.currency, po.product_kind,
               b.name AS brand, c.name AS channel
        FROM price_obs po
        LEFT JOIN brand b   ON b.id = po.brand_id
        LEFT JOIN channel c ON c.id = po.channel_id
        WHERE po.product_kind = 'device'
    """)
    hits = []
    for r in rows:
        why = judge(r["title"])
        if why:
            d = dict(r)
            d["_why"] = why
            hits.append(d)
    return hits


def report(hits: list[dict], total_device: int, n_sample: int) -> None:
    print(f"\n受影响行数：{len(hits)}  "
          f"（占 product_kind='device' 的 {len(hits) / max(total_device, 1) * 100:.2f}%，"
          f"device 总数 {total_device}）")

    for label, key in (("国家", "country_code"), ("渠道", "channel"),
                       ("品类", "category_code"), ("品牌", "brand")):
        c = Counter(str(h[key] or "—") for h in hits)
        if c:
            top = "  ".join(f"{k}:{v}" for k, v in c.most_common(8))
            print(f"  按{label}： {top}")

    dates = sorted(h["obs_date"] for h in hits if h["obs_date"])
    if dates:
        print(f"  日期跨度： {dates[0]} → {dates[-1]}")

    # ★ 抽样必须逐条打印原标题 + 判定依据 + 价格：
    #   分类器的正确性无法从聚合数字验证，只能从个案证伪。
    # ★★ 必须**按币种取最贵的那一头**，不能只看便宜的。
    #   便宜的那头只能证明"配件抓到了"，而这次回填真正的风险是**误杀整机** ——
    #   一台被判成配件的平板会从价格分析里消失，且不报错。
    #   误杀在数据里的形态就是"配件价高得离谱"，所以高价端才是体检位。
    #   跨币种排序会退化成比币种不是比价格（CLP 的数字天然比 BRL 大三个量级），
    #   故按币种分组各取最贵几条。
    def _show(h):
        price = f"{h['sale_price']:,.0f} {h['currency']}" if h["sale_price"] else "—"
        print(f"  [{h['id']}] {h['obs_date']} {h['country_code']}/"
              f"{h['channel'] or '—'} {price}")
        print(f"        {h['title'][:110]}")
        print(f"        → {h['_why']}")

    half = max(n_sample // 2, 1)
    print(f"\n—— 抽样 A：最便宜 {half} 条（确认配件确实被抓到）——")
    for h in sorted(hits, key=lambda x: (x["sale_price"] or 0))[:half]:
        _show(h)

    print(f"\n—— ★ 抽样 B：各币种最贵的几条（**误杀整机就藏在这里**）——")
    by_cur: dict[str, list] = {}
    for h in hits:
        by_cur.setdefault(h["currency"] or "—", []).append(h)
    per_cur = max(half // max(len(by_cur), 1), 2)
    for cur in sorted(by_cur):
        print(f"  【{cur}】")
        for h in sorted(by_cur[cur], key=lambda x: -(x["sale_price"] or 0))[:per_cur]:
            _show(h)


def apply(hits: list[dict]) -> None:
    ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = ROLLBACK_DIR / f"accessory_kind_{stamp}.json"
    # ★ 回滚清单先落盘再改库：反过来的话进程中途挂掉就再也回不去了。
    #   存的是**改之前的值**，不是"改成什么"。
    path.write_text(json.dumps(
        {"created": stamp, "note": "product_kind device→accessory 回填",
         "rows": [{"id": h["id"], "old": h["product_kind"], "title": h["title"]}
                  for h in hits]}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n回滚清单已写：{path}")

    ids = [h["id"] for h in hits]
    with db.tx() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            conn.execute(
                f"UPDATE price_obs SET product_kind='accessory' "
                f"WHERE id IN ({','.join('?' * len(chunk))}) AND product_kind='device'",
                chunk)
    left = db.q1("SELECT COUNT(*) n FROM price_obs WHERE product_kind='device' "
                 "AND id IN (%s)" % ",".join("?" * len(ids)), ids) if ids else {"n": 0}
    print(f"✓ 已更新 {len(ids)} 行；复查仍为 device 的：{left['n']}（应为 0）")


def rollback(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    with db.tx() as conn:
        for r in rows:
            conn.execute("UPDATE price_obs SET product_kind=? WHERE id=?",
                         (r["old"], r["id"]))
    print(f"✓ 已回滚 {len(rows)} 行到改前的值")
    return 0


def main() -> int:
    rb = _arg("--rollback", None)
    if rb:
        return rollback(rb)

    total_device = db.q1(
        "SELECT COUNT(*) n FROM price_obs WHERE product_kind='device'")["n"]
    hits = scan()
    report(hits, total_device, int(_arg("--sample", 20)))

    if not hits:
        print("\n没有需要回填的行。")
        return 0
    if not APPLY:
        print(f"\n[干跑] 未改动任何数据。确认抽样无误后执行：")
        print(f"       python tools/backfill_accessory_kind.py --apply")
        return 0
    apply(hits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
