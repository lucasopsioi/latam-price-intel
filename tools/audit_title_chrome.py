# -*- coding: utf-8 -*-
"""干跑体检：`extract.strip_ui_chrome` 在**历史标题**上会剥掉什么。

背景（2026-08-27）：部分渠道把商品卡的界面文案抓进了 price_obs.title 开头 ——
Paris.cl / Ripley 的「Vista Previa」、Falabella 的「Envío gratis app」，
尾部还挂着「4.7 (394) 35% 35%」这类评分/折扣角标。规则本身在
`app/scraping/extract.py::strip_ui_chrome`，**本文件不重复实现**，只负责
把它拿到真实数据上量一遍（知识页原话：先统计再改规则，别照着几个样例改）。

★ 本脚本**只读不写**。它回答的核心问题是：
    「剥掉之后标题为空的有几条」
  strip_ui_chrome 内部有自纠（剥完没有字母就整段退回原文），所以线上永远
  不会出现空标题；但**自纠触发了多少次**必须能报出来 ——
  触发得多就说明词表收得太宽，正在啃真标题
  （knowledge/lessons/scrape-normalize-silent-corruption.md 第 2 条：
    归一化剥出空型号比留着噪声危险得多）。

用法：
    python tools/audit_title_chrome.py                # 全量体检
    python tools/audit_title_chrome.py --sample 30    # 多看几条 diff 样例
    python tools/audit_title_chrome.py --channel Paris.cl

★ 采集正在跑时可以安全执行：独立进程 + 只读查询，不需要停服务。
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                               # noqa: E402
from app.scraping import extract                 # noqa: E402


def _arg(name: str, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _has_letter(s: str) -> bool:
    return bool(re.search(r"[^\W\d_]", s or ""))


def scan(channel_filter: str = "") -> dict:
    """把每条历史标题过一遍剥离，分四类计数。"""
    rows = db.q("""
        SELECT po.title, c.name AS channel
        FROM price_obs po
        LEFT JOIN channel c ON c.id = po.channel_id
        WHERE po.title IS NOT NULL AND po.title <> ''
    """)
    if channel_filter:
        rows = [r for r in rows if (r["channel"] or "") == channel_filter]

    out = {
        "total": len(rows),
        "changed": [],        # 剥掉了东西，且结果有效
        "emptied": [],        # ★ 剥完没有字母 → 自纠退回原文（本脚本要回答的那个数）
        "unchanged": 0,
        "by_channel": Counter(),
        "removed_head": Counter(),
        "removed_tail": Counter(),
    }
    for r in rows:
        # 采集端的顺序：fix_mojibake → strip_ui_chrome，体检必须照抄这个顺序，
        # 否则乱码的 EnvÃ­o 认不出来，会把真实剥离率报低。
        fixed = extract.fix_mojibake(r["title"])
        # ★ 用**不带自纠**的那个：strip_ui_chrome 在「没东西可剥」和
        #   「剥到只剩空」两种情况下返回值一模一样（都是原文），
        #   拿它数永远数不出自纠触发了多少次。规则本身还是同一份实现。
        stripped = extract.strip_ui_chrome_raw(fixed)

        if stripped == fixed:
            out["unchanged"] += 1
            continue

        # 剥离产物落在原文里的位置 → 拆出被剥掉的头和尾
        idx = fixed.find(stripped)
        head = fixed[:idx] if idx > 0 else ""
        tail = fixed[idx + len(stripped):] if idx >= 0 else ""
        rec = {"channel": r["channel"], "raw": fixed, "new": stripped,
               "head": head.strip(), "tail": tail.strip()}

        # ★ 这就是采集端会触发自纠的那一批：线上会退回原文，
        #   不会产生空标题；但条数必须能报出来。
        if not _has_letter(stripped):
            out["emptied"].append(rec)
            continue

        out["changed"].append(rec)
        out["by_channel"][r["channel"] or "(未知渠道)"] += 1
        if rec["head"]:
            out["removed_head"][rec["head"].lower()[:40]] += 1
        if rec["tail"]:
            out["removed_tail"][rec["tail"].lower()[:40]] += 1
    return out


def report(res: dict, n_sample: int) -> None:
    total = max(res["total"], 1)
    n_changed = len(res["changed"])
    n_empty = len(res["emptied"])

    print(f"\n标题总数：{res['total']}")
    print(f"  剥掉角标后变短：{n_changed}  ({n_changed / total * 100:.2f}%)")
    print(f"  原样不动      ：{res['unchanged']}  "
          f"({res['unchanged'] / total * 100:.2f}%)")
    print(f"\n★ 剥完标题为空（无字母）→ 自纠退回原文：{n_empty} 条  "
          f"({n_empty / total * 100:.3f}%)")
    if n_empty:
        print("  —— 这些行线上保持原样，不会产生空标题；但条数偏高就说明词表收宽了 ——")
        for r in res["emptied"][:n_sample]:
            print(f"    [{r['channel']}] {r['raw']!r}")
    else:
        print("  （一条都没有：词表没有啃到任何一条真标题）")

    print(f"\n受影响渠道（top 10）：")
    for ch, n in res["by_channel"].most_common(10):
        print(f"  {n:7d}  {ch}")

    print(f"\n剥掉的**前缀**形态（top 12）：")
    for k, n in res["removed_head"].most_common(12):
        print(f"  {n:7d}  {k!r}")
    print(f"\n剥掉的**后缀**形态（top 12）：")
    for k, n in res["removed_tail"].most_common(12):
        print(f"  {n:7d}  {k!r}")

    print(f"\n抽样 diff（{n_sample} 条）：")
    step = max(len(res["changed"]) // max(n_sample, 1), 1)
    for r in res["changed"][::step][:n_sample]:
        print(f"  [{r['channel']}]")
        print(f"    旧  {r['raw']!r}")
        print(f"    新  {r['new']!r}")


def main() -> None:
    n_sample = int(_arg("--sample", 12))
    channel = _arg("--channel", "")
    res = scan(channel)
    report(res, n_sample)
    print("\n（本脚本只读，未修改任何数据）")


if __name__ == "__main__":
    main()
