# -*- coding: utf-8 -*-
"""重跑情报抽取（2026-09-04 用户截图：「小米在 IFA 2026 发布 Galaxy Z Fold 8 竞品」）。

为什么要重跑：
  ① 原来喂给模型的正文只有前 120 字，「The Xiaomi 18 Fold was showcased…」在第 128 字，
     模型看不到机型名，只能照搬三星站的标题框架；
  ② 模型抽出的 brand 原来回写时直接丢弃，重要度≥3 的动态 91% 没挂品牌。
  两个修复都在 app/agents/intel.py 的 _analyze 里，重跑一遍即可让历史条目吃到。

范围：近 --days 天、importance ≥ --min-importance 的全部条目（品牌挂接对所有条目
都有效，不只是正文被截断的那些）。--ids 1,2,3 只跑指定条；--dry 只统计不调模型。

跑法： python tools\reanalyze_dynamics.py [--days 30] [--min-importance 3] [--ids ...] [--dry]
                                         [--retry-dropped]   # 主轮后把漏答/拒收的再送一轮
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")
from app import config, db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-importance", type=int, default=3)
    ap.add_argument("--ids", default="")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--retry-dropped", action="store_true",
                    help="主轮跑完后，把摘要没变化的（模型漏答/核对码拒收）再送一轮")
    a = ap.parse_args()

    if a.ids:
        ids = [int(x) for x in a.ids.split(",") if x.strip()]
        rows = db.q(f"SELECT * FROM dynamics WHERE id IN ({','.join('?' * len(ids))})", ids)
    else:
        rows = db.q("""SELECT * FROM dynamics
                       WHERE created_at >= datetime('now', ?) AND importance >= ?
                       ORDER BY importance DESC, id DESC""",
                    (f"-{a.days} day", a.min_importance))
    nobrand = sum(1 for r in rows if r["brand_id"] is None)
    print(f"待重跑 {len(rows)} 条（其中无品牌 {nobrand} 条）")
    if not rows:
        return 0

    # url_hash 必须与 _analyze 回写时算出的一致，否则回写落空。逐条核对，不一致的挑出来。
    items, skipped = [], []
    for r in rows:
        h = db.row_hash(r["url"] or r["title"] or "")
        if h != r["url_hash"]:
            skipped.append(r["id"])
            continue
        items.append({"title": r["title"], "summary": r["raw_text"] or "",
                      "source": r["source_name"], "url": r["url"],
                      "brand_id": r["brand_id"], "country_code": r["country_code"],
                      "category_code": r["category_code"],
                      "published_raw": r["published_at"]})
    if skipped:
        print(f"  ! {len(skipped)} 条 url_hash 与当前算法不一致，回写会落空，跳过：{skipped[:8]}")
    if a.dry:
        return 0

    from app.agents import LLMClient
    from app.agents.intel import IntelAgent
    cfg = config.load_runtime()["agents"]
    ag = IntelAgent(LLMClient(cfg), cfg)
    if not (ag.llm and ag.llm.available()):
        print("! 模型不可用（未配 Key），重跑没有意义 —— 退出，不动库")
        return 2
    ag.start(f"重跑情报抽取 {len(items)} 条（正文喂满 + 品牌回写）")

    before = {r["id"]: r for r in rows}
    done = 0
    for i in range(0, len(items), 150):          # _analyze 内部上限 150
        res = ag._analyze(items[i:i + 150])
        done += len(res)
        print(f"  {min(i + 150, len(items))}/{len(items)} 已回写 {len(res)} 条")

    ids_all = [r["id"] for r in rows]

    def _snapshot():
        return {r["id"]: r for r in db.q(
            f"SELECT id, summary_zh, brand_id, importance FROM dynamics "
            f"WHERE id IN ({','.join('?' * len(ids_all))})", ids_all)}

    # ★ 模型每批漏答约 13%（实测 22 批漏 42 条）。漏答的条目保留原值是对的，
    #   但原值可能还是修复前的（正文截断、无品牌），所以再补一轮：只送摘要
    #   没变化的那些。补一轮仍漏的就留着 —— 不能为了覆盖率去猜。
    if a.retry_dropped:
        mid = _snapshot()
        # rows 与 items 不一定等长（url_hash 不一致的被跳过），按 url 对齐
        by_url = {it["url"]: it for it in items}
        redo = [by_url[r["url"]] for r in rows
                if r["url"] in by_url
                and (mid[r["id"]]["summary_zh"] or "") == (r["summary_zh"] or "")]
        print(f"\n补漏一轮：{len(redo)} 条摘要没变化（模型漏答或核对码拒收）")
        for i in range(0, len(redo), 150):
            res = ag._analyze(redo[i:i + 150])
            print(f"  {min(i + 150, len(redo))}/{len(redo)} 已回写 {len(res)} 条")

    after = _snapshot()
    ch_sum = [k for k in ids_all if (after[k]["summary_zh"] or "") != (before[k]["summary_zh"] or "")]
    new_brand = [k for k in ids_all if before[k]["brand_id"] is None and after[k]["brand_id"] is not None]
    rebound = [k for k in ids_all if before[k]["brand_id"] is not None
               and after[k]["brand_id"] not in (None, before[k]["brand_id"])]
    still = sum(1 for k in ids_all if after[k]["brand_id"] is None)
    ag.finish("ok", f"摘要变更 {len(ch_sum)}，新挂品牌 {len(new_brand)}，改绑 {len(rebound)}")

    bname = {b["id"]: b["name"] for b in db.q("SELECT id,name FROM brand")}
    print(f"\n摘要变更 {len(ch_sum)} 条；新挂品牌 {len(new_brand)} 条；改绑 {len(rebound)} 条；"
          f"仍无品牌 {still} 条（原文里确实没提到库内品牌）")
    print("\n摘要变更抽样（前 → 后）：")
    for k in ch_sum[:10]:
        print(f"  #{k} [{bname.get(after[k]['brand_id'], '-')}]")
        print(f"     前: {(before[k]['summary_zh'] or '')[:60]}")
        print(f"     后: {(after[k]['summary_zh'] or '')[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
