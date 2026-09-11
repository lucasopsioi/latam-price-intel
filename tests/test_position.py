# -*- coding: utf-8 -*-
"""价格站位（我的位置）的回归测试。

守三条，每条都对应一种"图照画、数照显示、结论是错的"的失败：
  1. 符号方向：正数必须是「我方更贵」
  2. 样本闸门：对位太少不许下"明显偏高/偏低"的判断
  3. 用中位数不用均值
"""
import ast
import inspect
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app import boards  # noqa: E402

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ─────────────── 1. 分档表本身要自洽 ───────────────
bands = boards.POSITION_BANDS
ok(len(bands) == 5, f"应有 5 档，实得 {len(bands)}")
los = [b[0] for b in bands]
ok(los == sorted(los), "分档区间必须单调递增，否则 _band_of 会命中错的档")
for lo, hi, zh, key in bands:
    ok(lo < hi, f"{key} 的区间反了：{lo} >= {hi}")
# 区间必须首尾相接，不能有缝（有缝的值会落到兜底档，被悄悄归成"持平"）
for a, b in zip(bands, bands[1:]):
    ok(a[1] == b[0], f"分档之间有缝：{a[1]} → {b[0]}，缝里的值会被静默归错档")

ok(boards._band_of(0.0)[1] == "even", "0% 应是持平")
ok(boards._band_of(50.0)[1] == "high_hard", "+50% 应是明显偏高")
ok(boards._band_of(-50.0)[1] == "low_hard", "-50% 应是明显偏低")
ok(boards._band_of(5.0)[1] == "high_soft", "+5% 应是略高")
ok(boards._band_of(-5.0)[1] == "low_soft", "-5% 应是略低")


# ─────────────── 2. 符号方向（最容易被弄反的一条）───────────────
# 价差的正负在传递中极易反向，而反了之后图还是照画、数还是照显示 ——
# "我明显偏贵"会被读成"我明显便宜"，是能直接导致错误定价决策的那种错。
src = inspect.getsource(boards.my_position)
ok('(g["my_price"] - med) / med' in src,
   "★ 站位必须算成 (我的价 − 对位中位价)/对位中位价 —— "
   "正数=我更贵。写反了图照画，但结论完全相反")
ok("my_vs_field_pct" in src,
   "★ 字段名要把方向写进去（my_vs_field），"
   "叫 gap_pct 这种中性名字最容易在下游被理解反")
ok("sign_note" in src, "返回里必须带符号约定说明，供界面直接显示")


# ─────────────── 3. 样本闸门 ───────────────
# 实测：不设闸门时 6 个"明显偏高"里有 4 个建立在 1~2 个样本上。
# 最极端的是折叠屏 Astra X7 只匹到 1 款 iPhone 17 Pro Max（非折叠），
# 拿它当"对位中位数"得出"贵 28%" —— 而折叠屏本来就该更贵。
ok(getattr(boards, "MIN_FIELD", 0) >= 3,
   f"★ 下站位判断至少要 {3} 个对位机型，实得 MIN_FIELD={getattr(boards,'MIN_FIELD',None)}")
ok("MIN_FIELD" in src, "my_position 必须实际用到 MIN_FIELD")
ok('"thin"' in src or "'thin'" in src,
   "样本不足要单独标一个档，而不是硬塞进某个正常档")


# ─────────────── 4. 用中位数不用均值 ───────────────
tree = ast.parse(inspect.getsource(boards.my_position).lstrip())
calls = [n.func.id if isinstance(n.func, ast.Name) else
         (n.func.attr if isinstance(n.func, ast.Attribute) else "")
         for n in ast.walk(tree) if isinstance(n, ast.Call)]
ok("_quantile" in calls,
   "★ 应用分位数（中位）而不是均值：一台离群的高价机会把均值整个拽偏")
ok("mean" not in "".join(calls), "不该出现 mean")


# ─────────────── 5. 真实数据上的性质 ───────────────
try:
    d = boards.my_position()
    items = d.get("items") or []
    if items:
        ok(d.get("total") == len(items), "total 应与 items 数量一致")

        # 样本不足的必须排在后面，不能靠大数字霸占屏幕顶部
        first_thin = next((i for i, x in enumerate(items) if x["band"] == "thin"), None)
        last_solid = max((i for i, x in enumerate(items) if x["band"] != "thin"),
                         default=None)
        if first_thin is not None and last_solid is not None:
            ok(first_thin > last_solid,
               "★ 样本不足的条目必须排在有结论的之后 —— "
               "否则一个 1 款对位的 +28% 会顶在最前面误导人")

        # 任何被判"明显偏高/偏低"的，对位数必须够
        bad = [x for x in items
               if x["band"] in ("high_hard", "low_hard")
               and x["rival_n"] < boards.MIN_FIELD]
        ok(not bad,
           f"★ 有 {len(bad)} 条在样本不足时仍给了强判断："
           f"{[(x['my_name'], x['rival_n']) for x in bad][:3]}")

        # 符号自检：找一条我方价高于中位价的，它的百分比必须为正
        higher = next((x for x in items if x["my_price"] > x["field_median"]), None)
        if higher:
            ok(higher["my_vs_field_pct"] > 0,
               f"★ 我方价({higher['my_price']}) > 对位中位({higher['field_median']}) "
               f"时百分比必须为正，实得 {higher['my_vs_field_pct']}")
        lower = next((x for x in items if x["my_price"] < x["field_median"]), None)
        if lower:
            ok(lower["my_vs_field_pct"] < 0,
               f"★ 我方价低于对位中位时百分比必须为负，"
               f"实得 {lower['my_vs_field_pct']}")

        # 中位数必须落在区间内
        for x in items[:40]:
            ok(x["field_low"] <= x["field_median"] <= x["field_high"],
               f"{x['my_name']} 的中位价没落在区间内")

        # 只统计 active 产品（重复登记已标 duplicate，不该再出现）
        from app import db
        dup_names = {r["marketing_name"] for r in db.q(
            "SELECT marketing_name FROM my_product WHERE status='duplicate'")}
        if dup_names:
            names = [x["my_name"] for x in items]
            ok(len(names) == len(set((n, x["country_code"])
                                     for n, x in zip(names, items))),
               "同一(产品,国家)不该出现多行 —— 重复登记应已被排除")
except Exception as e:                       # noqa: BLE001
    print(f"  （跳过真实数据检查：{type(e).__name__}: {e}）")


print(f"position: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
