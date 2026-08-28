# -*- coding: utf-8 -*-
"""价格曲线与预警的回归测试。

这里守的不是"函数能跑"，是几条**踩过坑才立起来的性质**：
  1. 链式配对指数对货盘进出免疫（固定篮子单独做不到这件事）
  2. 链条断了要能重新接上，不许卡在某一天
  3. 跨币种绝对价位必须在后端就没有
  4. 单个关注对象每轮预警有上限
每条都对应一次真实的误导性输出，注释里写了当时错成什么样。
"""
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("PYTHONUTF8", "1")

from app import trends

FAIL = []
PASS = [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


def close(a, b, tol=0.51):
    return a is not None and abs(a - b) <= tol


# ───────────────────────────── 1. 组成免疫 ─────────────────────────────
# 场景复刻实测事故：Samsung·CL 的 08-10 只有 1/34 件商品在架，
# 那一件恰好是 1,099,990 的贵机，固定篮子中位数于是从 1.1M "跌"到 250k，
# 图上是 −77% 的崩盘，实际一分钱没降。

def _mk(rows):
    """rows: {url: {day: price}} → 喂给 _basket_series 的行格式"""
    out = []
    for url, pts in rows.items():
        for d, p in pts.items():
            out.append({"url": url, "d": d, "p": float(p), "currency": "CLP"})
    return out


def run_series(rows, xs, **kw):
    return trends._compose(_mk(rows), xs, "category", "phone", "CL", **kw)


D = [f"2026-08-{n:02d}" for n in range(10, 16)]   # 10..15

# 五件商品价格全程不动，但 08-11 只有一件在架（其余缺采）
flat = {}
for i, base in enumerate([100_000, 300_000, 500_000, 800_000, 1_099_990]):
    pts = {d: base for d in D}
    if i != 4:
        del pts["2026-08-11"]      # 只留最贵那件
    flat[f"u{i}"] = pts

r = run_series(flat, D)
lvl = r["series"][0]["pts"]
idx = r["series"][0]["index"]

ok(lvl[D.index("2026-08-11")] is None,
   "覆盖率闸门失效：08-11 只有 1/5 件在架，绝对价位不该出点")

vals = [v for v in idx if v is not None]
ok(vals and max(vals) - min(vals) < 0.5,
   f"链式指数不该因货盘进出而波动，实得 {vals}")

# 同一批数据，若只用固定篮子中位数就会被骗 —— 把这个反例钉住，
# 说明"改用链式"不是风格偏好而是必需
naive = [trends._median([v[d] for v in flat.values() if d in v]) for d in D]
ok(naive[1] is not None and naive[0] is not None and naive[1] / naive[0] > 2,
   "反例应成立：朴素中位数在 08-11 会跳到最贵那件（说明固定篮子单独不够）")


# ───────────────────────── 2. 断点后必须重新接上 ─────────────────────────
# 第一版把"基准日不前移"当成保守做法，结果链条永久卡在稀疏那天，
# 之后每天都拿那 1 件配对，整条曲线只剩一个孤零零的 100。

gap = {}
for i in range(6):
    pts = {d: 200_000 + i * 1000 for d in D}
    if i > 0:
        del pts["2026-08-11"]           # 08-11 只剩 1 件
    gap[f"g{i}"] = pts
r2 = run_series(gap, D)
i2 = r2["series"][0]["index"]

# 稀疏日**跨过去**即可：08-12 与 08-10 六件全在，两天直接可比。
# 这种情况不该记断点 —— 记了就是虚报，用户会以为两侧不可比。
ok(r2["breaks"] == [],
   f"稀疏日能跨过去时不该记断点（虚报同样有害），实得 {r2['breaks']}")
tail = [v for d, v in zip(D, i2) if d > "2026-08-11" and v is not None]
ok(len(tail) >= 3,
   f"链条必须跨过稀疏日继续（回归：曾卡死只剩 1 个点），实得 {len(tail)} 个点")
ok(max(v for v in i2 if v is not None) - min(v for v in i2 if v is not None) < 0.5,
   "跨过稀疏日之后指数仍应是平的（这批数据没有任何一件调过价）")

# 真正的断点：某天覆盖率够，但与上一基准日**没有重叠商品**（货盘整批换血，
# 实测形态是某渠道整个商品集被替换）。这时两侧确实不可直接比，必须记断点。
swap = {}
for i in range(5):                      # 前半段商品：只在 10~12 出现
    swap[f"a{i}"] = {d: 300_000 for d in D[:3]}
for i in range(5):                      # 后半段商品：只在 13~15 出现
    swap[f"b{i}"] = {d: 900_000 for d in D[3:]}
r3 = trends._compose(_mk(swap), D, "category", "phone", "CL", min_days_ratio=0.4)
i3 = r3["series"][0]["index"]

ok(r3["breaks"], f"整批换血必须记断点，实得 {r3['breaks']}")
ok("2026-08-13" in r3["breaks"], f"断点应落在换血当天，实得 {r3['breaks']}")
# 换血前后价格差 3 倍，但那是两批不同商品 —— 指数**绝不能**因此跳 3 倍
seg2 = [v for d, v in zip(D, i3) if d >= "2026-08-13" and v is not None]
ok(all(abs(v - 100.0) < 0.5 for v in seg2),
   f"断点后应重新基准为 100，不许把换血当成涨价，实得 {seg2}")


# ─────────────────────── 3. 跨币种绝对价位不许出数 ───────────────────────
# 实测「全部国家·手机」：绝对中位数 16999 → 10148 → 9999，像崩了 41%，
# 其实是中位数那一件从 PEN 商品换到了 MXN 商品。同期指数是平的 100.0。

mixed_rows = []
for cur, base in [("PEN", 1_100), ("MXN", 17_000), ("COP", 1_680_000)]:
    for k in range(4):
        for d in D:
            mixed_rows.append({"url": f"{cur}{k}", "d": d,
                               "p": float(base + k), "currency": cur})
rm = trends._compose(mixed_rows, D, "category", "phone", "")

ok(rm["mixed_currency"] is True, "应识别为跨币种")
ok(all(v is None for v in rm["series"][0]["pts"]),
   "跨币种时绝对价位必须全为 None —— 这类脏数要在后端掐掉，"
   "不能指望每个调用方都记得判 mixed_currency")
ok(any(v is not None for v in rm["series"][0]["index"]),
   "指数是无量纲比率，跨币种仍应成立，降级到指数不该丢信息")
ok("币种" in rm["note"], "note 要说明为什么没有绝对价位")

# 源码级：确认这是在 _basket_series 里掐的，不是某个上层顺手补的
import inspect
src = inspect.getsource(trends._compose)
ok("mixed" in src and "[None] * len(xs)" in src,
   "跨币种置空必须写在 _compose 内部")


# ───────────────── 4. 多对象对比：跨币种判定与指数来源 ─────────────────
# 回归两个连着的坑（症状是两条线**全空**，没有任何报错）：
#   a) 跨币种的对象 currency 字段是空串，进不了 currencies 集合，于是
#      「全部国家 Samsung vs 全部国家 Xiaomi」被判成同币种、走绝对价位分支
#      —— 而它们的绝对价位刚在后端被置空了。
#   b) 指数分支拿**绝对价位**重新求基。绝对价位对货盘构成敏感，而且跨币种
#      时本来就是空的，求基只会得到空线。应该用链式指数。

def _fake(name, currency, pts, index, mixed):
    return {"xs": D, "mixed_currency": mixed, "note": "",
            "series": [{"name": name, "currency": currency,
                        "pts": pts, "index": index}]}


_orig_brand = trends.brand_series
try:
    # a) 每条线自己内部就跨币种（绝对价位全空，链式指数有值）
    def fake_mixed(brand, country="", days=90, trim=True):
        return _fake(brand, "", [None] * len(D),
                     [100.0, 100.0, 98.0, 98.0, 97.0, 97.0], True)

    trends.brand_series = fake_mixed
    c = trends.compare([{"kind": "brand", "key": "Samsung", "country": ""},
                        {"kind": "brand", "key": "Xiaomi", "country": ""}], days=6)
    ok(c["indexed"] is True,
       "单条线自己跨币种时也必须指数化（回归：曾被判成同币种走绝对价）")
    for s in c["series"]:
        got = [v for v in s["pts"] if v is not None]
        ok(len(got) >= 4,
           f"{s['name']} 指数化后应有点，实得 {len(got)} 个（回归：曾全空）")
    ok(c["series"][0]["pts"][-1] == 97.0,
       f"应取链式指数归一，97/100*100=97，实得 {c['series'][0]['pts'][-1]}")

    # b) 同币种时不该强行指数化，绝对价位要照常出
    def fake_same(brand, country="", days=90, trim=True):
        return _fake(brand, "CLP", [200_000] * len(D), [100.0] * len(D), False)

    trends.brand_series = fake_same
    c2 = trends.compare([{"kind": "brand", "key": "Samsung", "country": "CL"},
                         {"kind": "brand", "key": "Xiaomi", "country": "CL"}], days=6)
    ok(c2["indexed"] is False, "同币种应直接比绝对价")
    ok("CLP" in c2["unit"], f"单位应标出币种，实得 {c2['unit']}")
    ok(c2["series"][0]["pts"][0] == 200_000, "同币种时绝对价位不该被改写")

    # c) 指数来源优先链式：给一条构成敏感的绝对价位 + 平稳的链式指数，
    #    指数化结果必须是平的（若错用绝对价位求基会看到 −50% 的假崩盘）
    def fake_trap(brand, country="", days=90, trim=True):
        return _fake(brand, "", [100.0, 100.0, 50.0, 50.0, 50.0, 50.0],
                     [100.0] * len(D), True)

    trends.brand_series = fake_trap
    c3 = trends.compare([{"kind": "brand", "key": "T", "country": ""}], days=6)
    got3 = [v for v in c3["series"][0]["pts"] if v is not None]
    ok(all(abs(v - 100.0) < 0.01 for v in got3),
       f"指数化必须取链式指数而非绝对价位求基，实得 {got3}")
finally:
    trends.brand_series = _orig_brand


# ──────────────────────── 5. 预警阈值与每轮上限 ────────────────────────
th = trends.PRIORITY_THRESHOLD
ok(th["P0"]["drop"] < th["P1"]["drop"] < th["P2"]["drop"],
   "优先级越高阈值越低（P0 更敏感）")
ok(trends.MAX_PER_WATCH == 5,
   f"每个关注对象每轮上限应为 5，实得 {trends.MAX_PER_WATCH}")

# 实测教训：5 个关注对象一次扫出 53 条，等于没有预警。
# 上限必须真的截断，且被折叠了多少要能说出来。
sig = inspect.getsource(trends.scan_alerts)
ok("MAX_PER_WATCH" in sig, "scan_alerts 必须实际用到 MAX_PER_WATCH")
ok("suppress" in sig or "折叠" in sig or "suppressed" in sig,
   "被上限折叠的条数要统计出来告诉用户，静默截断=谎报太平")

for p in ("P0", "P1", "P2"):
    ok(th[p]["drop"] > 0 and th[p]["rise"] > 0, f"{p} 阈值必须为正")

lbl = inspect.getsource(trends._alert_label)
ok("channel" in lbl, "预警标签要带渠道 —— 只有型号名分不清是哪个店在降价")


# ───────────────────────────── 汇总 ─────────────────────────────
# ───────── 6. 产品曲线必须连续（LOCF 延续，用户 2026-08-19 明确要求）─────────
# 根因复盘：采集是品类轮换制（一天一个品类），平板只在轮到那天有系统性观测，
# 08-14~16 无平板轮次 → 图上断线。用户原话「我肯定是要连续的价格」。
# 挂牌价在无新观测时视为未变（LOCF）在语义上成立；但延续值必须带标记，
# 前端画空心小点 —— 连续但不撒谎。
cf_v, cf_f = trends._carry_forward([None, 100, None, None, 120, None])
ok(cf_v == [None, 100, 100, 100, 120, 120],
   f"★ 缺口按最近观测延续，首观测前保持 None，实得 {cf_v}")
ok(cf_f == [False, False, True, True, False, True],
   f"★ 延续点必须带标记（前端画空心点、悬浮标注），实得 {cf_f}")
ok(trends._carry_forward([])[0] == [], "空列表不该炸")
ok(trends._carry_forward([None, None])[0] == [None, None],
   "从未观测过的序列不许凭空造值")

# product_series 出口必须带 filled 数组且与 pts 等长
import inspect as _insp
_src = _insp.getsource(trends.product_series)
ok("_carry_forward" in _src, "product_series 必须应用延续")
ok('"filled"' in _src, "序列要携带 filled 标记供前端区分")


print(f"trends: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)


