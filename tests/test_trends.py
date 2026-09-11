# -*- coding: utf-8 -*-
"""价格曲线与预警的回归测试。

这里守的不是"函数能跑"，是几条**踩过坑才立起来的性质**：
  1. 链式配对指数对货盘进出免疫（固定篮子单独做不到这件事）
  2. 链条断了要能重新接上，不许卡在某一天
  3. 跨币种绝对价位必须在后端就没有
  4. 单个关注对象每轮预警有上限
每条都对应一次真实的误导性输出，注释里写了当时错成什么样。

★★ 源码级断言一律走 **ast**（assertions-that-verify-nothing 那一课，本项目已犯四次）：
   `"X" in inspect.getsource(fn)` 会匹配到解释这条性质的注释 —— 而**越重要的性质，
   旁边的注释写得越认真，断言越容易恒真**。本文件 §3/§5/§6/§7 原来正是字符串搜索，
   实测「函数体只有注释」的假函数照样满足 `'"y_label"' in src`。
   每个 ast 判据下面都跟一条**反向自证**：拿只有注释的假函数喂进去必须判违规，
   同时确认字符串搜索会被它骗过 —— 不会红的断言等于没写。
"""
import ast
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


# ══════════════════════ ast 判据（注释与 docstring 天然不进语法树）══════════════════════
def _tree(src: str) -> ast.AST:
    """把函数源码（可能带缩进）解析成语法树。"""
    import textwrap
    return ast.parse(textwrap.dedent(src))


def _fn_node(src: str, name: str = None):
    t = _tree(src)
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (name is None or n.name == name):
            return n
    return None


def str_consts(fn) -> list[str]:
    """函数里真正进了字符串常量的内容（不含 docstring）。"""
    doc = ast.get_docstring(fn, clean=False)
    return [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != doc]


def dict_key_consts(fn) -> set:
    """函数里所有字典字面量的字符串键 —— 用来验"返回值里有没有这个字段"。"""
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            keys |= {k.value for k in n.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return keys


def name_loads(fn) -> set:
    """函数里被**读到**的名字（真的用了，而不是只在注释里提了一句）。"""
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def called_names(fn) -> set:
    return {n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def assigned_from(fn, src_name: str) -> set:
    """被 `x = <src_name>` 直接赋值出去的变量名。

    ★ 比"这个名字出现过"严得多：MAX_PER_WATCH 在 note 的 f-string 里也会被读到，
      光看"读到没有"分不清它是**真的当上限用了**还是只在提示语里提了一句
      （反向变异实测：把 `room = MAX_PER_WATCH` 改成 `room = 5` 之后，
        "读到没有"这条断言依然通过 —— 那就又是一条恒真断言）。
    """
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Name) and n.value.id == src_name:
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    return out


def has_blank_on_flag(fn, flag: str, target: str) -> bool:
    """有没有一段 `if <flag>: <target> = [None] * len(...)`（跨币种置空的结构形态）。"""
    for node in ast.walk(fn):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == flag):
            continue
        for st in ast.walk(node):
            if not isinstance(st, ast.Assign):
                continue
            tgt = st.targets[0]
            if not (isinstance(tgt, ast.Name) and tgt.id == target):
                continue
            v = st.value
            if (isinstance(v, ast.BinOp) and isinstance(v.op, ast.Mult)
                    and isinstance(v.left, ast.List) and len(v.left.elts) == 1
                    and isinstance(v.left.elts[0], ast.Constant)
                    and v.left.elts[0].value is None):
                return True
    return False


# ── 反向自证：一个**函数体只有注释与 docstring** 的假函数，字符串搜索会被它骗过 ──
DECOY = '''
def decoy():
    """跨币种时 level = [None] * len(xs)，并返回 "y_label" / "index" / "filled"。"""
    # 这里 if mixed: level = [None] * len(xs)
    # 返回 {"y_label": ..., "index": ..., "filled": ...}
    # 还会用到 MAX_PER_WATCH、_carry_forward、_chain_index，被上限折叠的条数记在 capped
    return None
'''
_decoy = _fn_node(DECOY, "decoy")
ok(_decoy is not None, "反向自证的假函数要解析得出来")
ok('"y_label"' in DECOY and "MAX_PER_WATCH" in DECOY and "_carry_forward" in DECOY,
   "反向自证前提：字符串搜索**确实会**被只有注释的假函数骗过（断言恒真的成因）")
ok(not any("y_label" in s for s in str_consts(_decoy)),
   "★ ast 判据必须识破它：注释里的 y_label 不是字符串常量")
ok(not dict_key_consts(_decoy), "★ ast 判据必须识破它：注释里的字典键不是字典键")
ok("MAX_PER_WATCH" not in name_loads(_decoy), "★ ast 判据必须识破它：注释里的名字没被读到")
ok(not assigned_from(_decoy, "MAX_PER_WATCH"), "★ 注释里的赋值不是赋值")
ok(not called_names(_decoy) and not has_blank_on_flag(_decoy, "mixed", "level"),
   "★ ast 判据必须识破它：注释里的调用与置空分支都不存在")


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

# 源码级（ast）：确认这是在 _compose 内部掐的，不是某个上层顺手补的。
# 旧写法是 `"mixed" in src and "[None] * len(xs)" in src` —— 上面的 DECOY 证明了
# 那种写法连"函数体只有注释"都拦不住。这里改判**结构**：真的存在 `if mixed: level = [None] * …`。
import inspect
_compose_fn = _fn_node(inspect.getsource(trends._compose), "_compose")
ok(_compose_fn is not None, "取得 _compose 的语法树")
ok(has_blank_on_flag(_compose_fn, "mixed", "level"),
   "跨币种置空必须写在 _compose 内部（结构：if mixed: level = [None] * …）")


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
# 旧写法搜的是 "MAX_PER_WATCH" in src / "折叠" in src —— 而 scan_alerts 的注释里
# 恰好把这两个词都写了（越重要的性质注释写得越认真），断言恒真。改判结构。
_scan = _fn_node(inspect.getsource(trends.scan_alerts), "scan_alerts")
ok(_scan is not None, "取得 scan_alerts 的语法树")
ok(bool(assigned_from(_scan, "MAX_PER_WATCH")),
   "scan_alerts 必须把 MAX_PER_WATCH **当成上限用**（`<余额> = MAX_PER_WATCH`）——"
   "只在 note 的 f-string 里提一句不算（反向变异证明过：那样断言恒真）")
ok("capped" in dict_key_consts(_scan),
   "被上限折叠的条数要进返回值（'capped' 是真的字典键），静默截断=谎报太平")
ok("capped" in name_loads(_scan), "capped 要真的被累加与读出")
# ★ 这里只能做到结构级：scan_alerts 会**写 price_alert 表**，
#   本文件跑在真库上（没有临时库），不许真调它。运行级的截断验证属于带临时库的测试。

for p in ("P0", "P1", "P2"):
    ok(th[p]["drop"] > 0 and th[p]["rise"] > 0, f"{p} 阈值必须为正")

# 预警标签要带渠道。旧写法 `"channel" in src` 会匹配到注释；改判 _alert_label
# 真的从 m 里读了 channel_name（结构：下标常量 'channel_name'）。
_lbl = _fn_node(inspect.getsource(trends._alert_label), "_alert_label")
ok(any("channel_name" == s for s in str_consts(_lbl)),
   "预警标签要带渠道 —— 只有型号名分不清是哪个店在降价（须真的取 m['channel_name']）")


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

# product_series 出口必须带 filled 数组且与 pts 等长（ast：调用节点 + 真的字典键）
import inspect as _insp
_ps = _fn_node(_insp.getsource(trends.product_series), "product_series")
ok(_ps is not None, "取得 product_series 的语法树")
ok("_carry_forward" in called_names(_ps), "product_series 必须**调用** _carry_forward")
ok("filled" in dict_key_consts(_ps), "序列要携带 filled 标记供前端区分（真的字典键）")


# ───────── 7. 展示名去重（用户截图：下拉里出现「Lenovo Lenovo Tab」）─────────
# 权威 SKU 的 model_name 自带品牌前缀，入库层不许剥（test_modelkey 钉住），
# 所以去重在展示层：trends.display_label 与 weekly._label 必须**逐字一致**，
# 否则周报叫一个名、曲线页叫另一个名。
from app.agents import weekly as _weekly  # noqa: E402

_LABEL_CASES = [
    (("Lenovo", "Lenovo Tab"), "Lenovo Tab"),
    (("Lenovo", "Lenovo Idea Tab"), "Lenovo Idea Tab"),
    (("Honor", "Honor Honor 70 Honor"), "Honor 70"),
    (("Samsung", "Galaxy S25"), "Samsung Galaxy S25"),
    (("Apple", "Apple iPad Air 11 M3"), "Apple iPad Air 11 M3"),
    (("", "Redmi Pad 2"), "Redmi Pad 2"),
    (("Xiaomi", ""), "Xiaomi"),
    ((None, None), ""),
    (("Lenovo", "lenovo tab"), "lenovo tab"),
]
for (b, m), want in _LABEL_CASES:
    got = trends.display_label(b, m)
    ok(got == want, f"display_label({b!r},{m!r}) 应为 {want!r}，实得 {got!r}")
    ok(got == _weekly._label(b, m),
       f"display_label 与 weekly._label 分叉：{b!r},{m!r} → {got!r} vs {_weekly._label(b, m)!r}")

# 曲线返回要带展示名与纵轴标签（前端不该自己拼、也不该拿第一条线的币种当标签）。
# 旧写法 `'"y_label"' in src` 被 DECOY 证明连"只有注释的函数"都拦不住 —— 改判真的字典键。
_ps_keys = dict_key_consts(_ps)
ok("y_label" in _ps_keys, "product_series 要返回 y_label（单币种=币种，多币种=指数）")
ok("index" in _ps_keys, "product_series 每条线要带 index（多币种时前端只能画它）")
ok("pending_obs" in _ps_keys and "audit_filter" in _ps_keys,
   "product_series 要如实报出被审计门挡掉的条数与口径（空图不能长得像没数据）")
# 合成指数走逐日配对链式（与 _basket_series 同一份实现），不许各线指数取中位数
ok("_chain_index" in called_names(_ps),
   "product_series 的合成指数必须调 _chain_index（构成效应：晚上架的线会把中位数拽下来）")
ok("_chain_index" in called_names(_compose_fn),
   "_compose 也必须调同一个 _chain_index —— 指数算法只许有一份实现")

print(f"trends: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)


