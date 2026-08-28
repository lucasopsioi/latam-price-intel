# -*- coding: utf-8 -*-
"""图表语法层的回归测试。

用户原话：「图表之前没有逻辑」。审计属实 —— 同一个问题在不同看板用了不同图形，
单看每张都合理，连起来读没有叙事，因为**图形不是按问题选的**。

光在文档里写规矩没用，下次照样散。所以规矩必须是**唯一的调用入口**：
看板声明「我要回答哪个问题」，由 charts.js 决定用什么图。
这个测试守的就是这条入口纪律。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHARTS = (ROOT / "app/web/charts.js").read_text(encoding="utf-8")
BOARDS = (ROOT / "app/web/boards.js").read_text(encoding="utf-8")

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


def strip_js_comments(src: str) -> str:
    """先剥注释再断言 —— 本项目已因"断言匹配到自己写的注释"栽过四次。"""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            q, i = c, i + 1
            out.append(q)
            while i < n and src[i] != q:
                if src[i] == "\\":
                    out.append(src[i]); i += 1
                if i < n:
                    out.append(src[i]); i += 1
            if i < n:
                out.append(src[i]); i += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = (j + 2) if j != -1 else n
            continue
        out.append(c); i += 1
    return "".join(out)


CHARTS_CODE = strip_js_comments(CHARTS)
BOARDS_CODE = strip_js_comments(BOARDS)

PRIMITIVES = ["dumbbell", "rangeBar", "diverge", "heatmap",
              "stack100", "scatter", "slope", "hist", "line"]

# ─────────────── 1. 语法表存在且覆盖全部图元 ───────────────
m = re.search(r"var GRAMMAR = \{(.*?)\n  \};", CHARTS_CODE, re.S)
ok(m is not None, "charts.js 应有 GRAMMAR 语法表")
questions, mapped = set(), set()
if m:
    for q, fn in re.findall(r"(\w+):\s*\{\s*fn:\s*(\w+)", m.group(1)):
        questions.add(q); mapped.add(fn)
    ok(len(questions) >= 8, f"语法表至少该覆盖 8 类问题，实得 {len(questions)}：{sorted(questions)}")
    unused = sorted(set(PRIMITIVES) - mapped)
    ok(not unused,
       f"这些图元没被任何问题映射到，说明语法表不完整：{unused} —— "
       f"没进表的图元只能被绕过语义层直接调，语法就是从这里开始散的")

ok("function ask(" in CHARTS_CODE, "charts.js 应导出 ask(question, elId, o)")
ok("ask: ask" in CHARTS_CODE, "ask 必须挂到 window.Charts 上")


# ─────────────── 2. 看板不许绕过语义层直接调图元 ───────────────
direct = re.findall(r"Charts\.(" + "|".join(PRIMITIVES) + r")\(", BOARDS_CODE)
ok(not direct,
   f"★ boards.js 不许直接调图元（发现 {sorted(set(direct))}）—— "
   f"必须走 Charts.ask('<问题>', ...)。直接调图元 = 同一个问题在不同页面"
   f"可以用不同的图，这正是「图表没有逻辑」的成因")

# 用到的问题类型必须都在语法表里（写错名字只会画出一张报错图）
used = set(re.findall(r"Charts\.ask\('(\w+)'", BOARDS_CODE))
ok(used, "boards.js 应该已经在用 Charts.ask")
if questions:
    bad = sorted(used - questions)
    ok(not bad, f"用到了语法表里没有的问题类型：{bad}")

# 每类问题在全站只对应一种图 —— 这就是「一个问题 = 一种图」本身
ok(len(used) >= 5,
   f"迁移覆盖面太窄（只用了 {sorted(used)}），至少该覆盖 5 类问题")


# ─────────────── 3. 硬规则必须写在语义层里，不能靠各页自觉 ───────────────
ask_src = re.search(r"function ask\(question, elId, o\) \{(.*?)\n  \}",
                    CHARTS_CODE, re.S)
ok(ask_src is not None, "找不到 ask 的函数体")
if ask_src:
    b = ask_src.group(1)
    ok("mixedCurrency" in b,
       "★ 跨币种不能画绝对价位，这条必须由语义层强制 —— "
       "六国六币种差三个数量级，且跨币种的中位数比的是币种不是价格")
    ok("connectNulls" in b,
       "★ 缺口不连线必须由语义层强制：没采到 ≠ 价格为零，"
       "连起来会画出一条根本不存在的价格轨迹")
    ok("empty(" in b, "未知问题类型要给出可读的空态，而不是静默什么都不画")


# ─────────────── 4. 语义正确性：占比问题不许用发散图 ───────────────
# 实测：voc-cov（评论覆盖率，0~100% 的占比）原来用 diverge。
# 发散图围绕 0 分正负，而覆盖率没有负值 —— 会让人以为存在"负覆盖"。
cov = re.search(r"Charts\.ask\('(\w+)', 'voc-cov'", BOARDS_CODE)
ok(cov is not None, "voc-cov 应已迁到语义层")
if cov:
    ok(cov.group(1) == "share",
       f"★ 覆盖率是占比问题，应声明为 share，实得 {cov.group(1)} —— "
       f"原来用 deviation（发散条）是把「有方向的偏离」这个语义浪费掉了")

# 时间序列一律 change
curve = re.search(r"Charts\.ask\('(\w+)', 'curve-chart'", BOARDS_CODE)
if curve:
    ok(curve.group(1) == "change", f"价格曲线应声明 change，实得 {curve.group(1)}")

# 二维强度一律 intensity
heat = re.search(r"Charts\.ask\('(\w+)', 'bp-heat'", BOARDS_CODE)
if heat:
    ok(heat.group(1) == "intensity", f"折扣热力应声明 intensity，实得 {heat.group(1)}")


print(f"chartgrammar: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
