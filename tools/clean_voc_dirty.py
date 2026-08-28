# -*- coding: utf-8 -*-
"""VOC 历史脏数据清理（2026-08-28）。

对应三个已修的采集/分析缺陷，把它们在库里留下的痕迹清掉：

  ① 界面控件被当评论入库 —— 评分分布控件、栏目标题、图标连字、按钮文案。
     直接删。理由：它们不是消费者的话，**留着会继续虚高评论量**，
     而评论量正是"主销款"判定的代理指标（见 voc.py 开头的说明）。

  ② 批量翻译错位 —— 模型丢条目后重新编号，`chunk[idx]` 把 A 的情感写到 B 头上。
     把受影响批次写过的行 content_zh/sentiment/aspects 清空。
     不删行、只清译文：原文是真的，重跑一遍就好
     （_translate_and_tag 的查询条件就是 content_zh IS NULL，自愈）。

  ③ 正文带页面抬头「评分 日期 标题 作者」 —— 原地剥掉。
     ★ content_hash 必须跟着重算：它是 md5(url|content[:200])，
       修好后的采集器算出来的是**剥完**的哈希。不同步改的话，
       下次重抓同一条评论会因为哈希对不上而**再插一行**（去重静默失效）。

用法：
    python tools/clean_voc_dirty.py            # 干跑，只报数不改库
    python tools/clean_voc_dirty.py --apply    # 真改

★ 先干跑逐条看，别直接 --apply（知识库 lessons/silent-failures-detection 第③条：
  批量修复工具本身就是高危，官方工具改坏 2894/3255 行的先例在那儿摆着）。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app.scraping.voc import (UI_NOISE_REASONS, review_reject_reason,  # noqa: E402
                              strip_page_head)

DB = ROOT / "data" / "intel.db"
norm = lambda s: re.sub(r"\s+", " ", s or "").strip()  # noqa: E731


def row_hash(*parts) -> str:
    """与 app.db.row_hash 同构（这里不引 db 模块，避免连上运行库的连接池）。"""
    import hashlib
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def classify(con) -> dict:
    """把要动的行分门别类。**只分类不改库**，好让干跑看得见每一类。

    ★ 分类靠 review_reject_reason 给出的**具体理由**，不能只看布尔。
      实测按布尔分会把 "Muy buen equipo"、"Entrega super rapida"、
      "Hermoso Muy bonito" 这些**真实短评**一起划进删除名单 ——
      它们栽的是"至少要有连续 8 个字母"那条老启发式（legacy 数据，
      入库时还没这条规则），不是界面文案。干跑时一眼看见才拦住的。
    """
    rows = con.execute("""SELECT id, content, content_zh, sentiment, product_url
                          FROM review""").fetchall()
    junk_ui, keep_short, head_fix, shell = [], [], [], []
    for r in rows:
        raw = r["content"] or ""
        why = review_reject_reason(raw)
        if why is not None:
            (junk_ui if why in UI_NOISE_REASONS else keep_short).append((r, why))
            continue
        body = strip_page_head(norm(raw))
        if body == norm(raw):
            continue
        (shell if len(body) < 15 else head_fix).append((r, body))
    return {"junk_ui": junk_ui, "keep_short": keep_short,
            "head_fix": head_fix, "shell": shell}


def suspect_translation_ids(con) -> set:
    """从 agent_step 留痕反推：哪些行的译文来自"返回条数/编号对不上"的批次。

    ★ 判据是**批次完整性**而不是逐行语义：一个批次只要返回的 idx 集合
      不是完整的 0..n-1，这一批里所有写过的行都不可信 ——
      模型一旦丢条目就会重新编号，错位从缺口处一路传到批尾。
      宁可多重译几条（token 便宜），也不能留着不可信的情感标签。
    """
    idx60 = {}
    for r in con.execute("SELECT id, content FROM review"):
        idx60.setdefault(norm(r["content"])[:60], []).append(r["id"])
    LINE = re.compile(r"(?m)^(\d+)\.\s\[")
    suspect = set()
    for s in con.execute("""SELECT prompt_digest, parsed_result FROM agent_step
                            WHERE step_name LIKE '翻译分析评论%'"""):
        body = (s["prompt_digest"] or "").split("\n\n维度只能从下面")[0]
        marks = list(LINE.finditer(body))
        if not marks:
            continue
        items = []
        for k, m in enumerate(marks):
            seg = body[m.end():marks[k + 1].start() if k + 1 < len(marks) else len(body)]
            cut = seg.find("] ")
            items.append(norm(seg[cut + 2:] if 0 <= cut <= 40 else seg))
        n = len(items)
        try:
            pr = json.loads(s["parsed_result"]) if s["parsed_result"] else []
        except Exception:  # noqa: BLE001
            pr = []
        idxs = [it.get("idx") for it in pr if isinstance(it, dict)]
        if len(idxs) == n and sorted(x for x in idxs if isinstance(x, int)) == list(range(n)):
            continue                      # 这一批对得上账，放过
        for x in idxs:
            if isinstance(x, int) and 0 <= x < n:
                suspect.update(idx60.get(items[x][:60], []))
    return suspect


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的改库（默认干跑）")
    ap.add_argument("--samples", type=int, default=8, help="每类抽样条数")
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    total = con.execute("SELECT COUNT(*) FROM review").fetchone()[0]
    g = classify(con)
    susp = suspect_translation_ids(con)
    susp_live = [r["id"] for r in con.execute(
        "SELECT id FROM review WHERE content_zh IS NOT NULL AND content_zh<>''")
        if r["id"] in susp]

    from collections import Counter
    why_n = Counter(w for _, w in g["junk_ui"])
    print(f"review 总行数 {total}\n")
    print("① 删除：界面控件 / 页面文案        %5d 行  %s"
          % (len(g["junk_ui"]), dict(why_n)))
    print("① 删除：剥完抬头只剩空壳          %5d 行" % len(g["shell"]))
    print("② 清空译文重跑：受错位批次影响    %5d 行" % len(susp_live))
    print("③ 原地剥抬头（含重算 hash）       %5d 行" % len(g["head_fix"]))
    print("   ★ 保留不动：%d 行真实短评（栽在长度/自然语言启发式上，不是界面文案）"
          % len(g["keep_short"]))

    for tag, sample in (("① 界面控件", g["junk_ui"]),
                        ("① 空壳", g["shell"]), ("③ 剥抬头", g["head_fix"]),
                        ("★ 保留的真实短评", g["keep_short"])):
        print(f"\n--- {tag} 抽样 ---")
        for r, extra in sample[:a.samples]:
            print(f"  [{r['id']}] {norm(r['content'])[:88]}")
            if isinstance(extra, str) and extra and not extra.islower():
                print(f"        -> {extra[:88]!r}")

    if not a.apply:
        print("\n（干跑，未改库。确认无误后加 --apply）")
        return 0

    del_ids = [r["id"] for r, _ in g["junk_ui"]] + [r["id"] for r, _ in g["shell"]]
    with con:
        con.execute("PRAGMA foreign_keys=ON")
        # review_aspect 有 ON DELETE CASCADE，但外键约束默认是关的，
        # 上面显式打开；再手工清一遍以防连接级设置没生效（孤儿行最后会核对）
        con.executemany("DELETE FROM review_aspect WHERE review_id=?",
                        [(i,) for i in del_ids])
        con.executemany("DELETE FROM review WHERE id=?", [(i,) for i in del_ids])
        n_del = len(del_ids)
        n_res = 0
        for rid in susp_live:
            con.execute("""UPDATE review SET content_zh=NULL, sentiment=NULL,
                           aspects=NULL WHERE id=?""", (rid,))
            con.execute("DELETE FROM review_aspect WHERE review_id=?", (rid,))
            n_res += 1
        n_head = 0
        for r, body in g["head_fix"]:
            con.execute("UPDATE review SET content=?, content_hash=? WHERE id=?",
                        (body, row_hash(r["product_url"], body[:200]), r["id"]))
            n_head += 1
    left = con.execute("SELECT COUNT(*) FROM review").fetchone()[0]
    print(f"\n✔ 删除 {n_del} 行；清空译文 {n_res} 行；剥抬头 {n_head} 行")
    print(f"  review 剩余 {left} 行（{total} -> {left}）")
    orphan = con.execute("""SELECT COUNT(*) FROM review_aspect ra
                            LEFT JOIN review r ON r.id=ra.review_id
                            WHERE r.id IS NULL""").fetchone()[0]
    print(f"  review_aspect 孤儿行 {orphan}（应为 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
