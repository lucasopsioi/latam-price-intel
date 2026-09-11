# -*- coding: utf-8 -*-
"""「价格曲线」看板（boards.js drawSingle / 产品下拉）与 /api/trend/{id}（dashboard.price_trend）的回归。

对应用户 2026-09-04 的截图：#2292「Lenovo Tab」曲线在 18 万与 8,500 CLP 之间来回跳、
下拉里写着「Lenovo Lenovo Tab」。守五条：
  1. drawSingle 必须走 Charts.trendSeries（币种/指数口径的唯一实现），blocked 时画空态，
     不再有 `showIndex = 勾选 && s0.index` 那条「缺 index 就静默退回绝对价」的分支
  2. 产品下拉用后端 label（缺失才 prodLabel），不许裸拼 `${p.brand} ${p.model_name}`
  3. 页面要写出 pending_obs 与 dropped_series ——「待审计」不能长得像「没数据」
  4. dashboard.price_trend 只取 audit_status = 'accepted'，且返回被挡掉的 pending 条数
     （临时库真跑：pending 的 8,500 不上图、pending_obs/pending_days 计数对、rejected 不算 pending）
  5. server.trend_candidates 的品牌下拉同样只认 accepted
  6. ★ price_trend 的 product_kind 口径必须与 trends.product_series 一致（= 'device'）：
     `<> 'accessory'` 会把三态里的 **unknown** 也画进去 ⇒ 两页对同一款产品给出不同的线，
     且没有任何报错（实测差集：近 90 天 unknown+accepted 6,464 条、
     339 个「产品×国家」组合完全靠 unknown 行画线，#3016 CL 的 24,602 维修屏就在其中）
  7. ★ 跨品类挂接（po.category_code ≠ rp.category_code）不进曲线 ——
     音频品类刻意没有价格地板，这类行价格审计永远抓不到，只能在消费方挡
  8. ★ 口径收紧不许静默吃行：被 product_kind 门挡掉的条数要报成 unclassified_obs

★ 每条源码级断言都先拿反例证明它会红（assertions-that-verify-nothing 那一课：
  断言匹配到自己写的注释就恒真）。JS 先剥注释再比；Python 走 ast，只看字符串常量。
★ 必须在 db 建立第一个连接**之前**改掉 config.DB_PATH，否则测试会安静地在生产库上跑。

跑法： python tests\\test_curve_board.py
"""
import ast
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config                              # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="curveboard_"))
config.DB_PATH = _TMP / "test.db"                   # ★ 必须早于任何 db.get_conn()

from app import db, dashboard                       # noqa: E402

PASS = 0
FAIL: list[str] = []


def ok(cond, msg):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(msg)


# ================================================================ JS 工具：剥注释 + 取函数体
def _skip_string(code: str, j: int) -> int:
    """code[j] 是引号；返回该字符串字面量结束后的下标。模板串里的 ${ } 递归处理（里面可再套字符串）。"""
    q, j, n = code[j], j + 1, len(code)
    while j < n:
        c = code[j]
        if c == "\\":
            j += 2
            continue
        if c == q:
            return j + 1
        if q == "`" and code.startswith("${", j):
            j = _skip_braces(code, j + 1)
            continue
        j += 1
    return n


def _skip_braces(code: str, j: int) -> int:
    """code[j] == '{'；返回配对 '}' 之后的下标，中途跳过字符串。"""
    depth, n = 0, len(code)
    while j < n:
        c = code[j]
        if c in "\"'`":
            j = _skip_string(code, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def strip_js_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，字符串（含模板串）原样保留。"""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            j = _skip_string(src, i)
            out.append(src[i:j])
            i = j
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = (j + 2) if j != -1 else n
            continue
        out.append(c)
        i += 1
    return "".join(out)


def js_fn_body(code: str, header: str) -> str | None:
    """从（已剥注释的）源码里取 header 开头那个函数的函数体。花括号配平，不靠 `\\n}` 猜结尾。"""
    i = code.find(header)
    if i < 0:
        return None
    b = code.find("{", i)
    end = _skip_braces(code, b)
    return code[b + 1:end - 1]


# ================================================================ 判据（单一实现，真源码与反例共用）
def draw_single_violations(body: str) -> list[str]:
    v = []
    if "Charts.trendSeries(" not in body:
        v.append("drawSingle 没走 Charts.trendSeries —— 币种/指数口径必须只有 charts.js 那一处实现")
    if "s0.index" in body or "showIndex" in body:
        v.append("drawSingle 仍有 showIndex / s0.index 分支：缺 index 时会静默退回绝对价")
    if re.search(r"s\.index\s*\|\|\s*s\.pts", body):
        v.append("drawSingle 仍有 `s.index || s.pts` 兜底 —— 这就是「缺 index 画绝对价」本身")
    if ".blocked" not in body or "Charts.empty(" not in body:
        v.append("trendSeries 判 blocked 时必须画空态（Charts.empty），不能继续画")
    m = re.search(r"Charts\.ask\('change',\s*'curve-chart',\s*\{(.*?)\}\);", body, re.S)
    if not m:
        v.append("drawSingle 应通过 Charts.ask('change','curve-chart',…) 画图")
    else:
        args = m.group(1)
        for k in ("series: t.series", "ylab: t.ylab", "indexed: t.indexed",
                  "mixedCurrency: t.mixedCurrency"):
            if k not in args:
                v.append(f"Charts.ask 的参数应全部来自 trendSeries 的结果：缺 {k}")
        if re.search(r"ylab:\s*(?:showIndex|s0\.currency|`价格)", args):
            v.append("纵轴标签不许由页面自己拼（多币种时会取第一条线的币种）")
    if "pending_obs" not in body:
        v.append("drawSingle 要读 pending_obs：空图必须能说出「待审计」而不是「没数据」")
    return v


def dropdown_violations(body: str) -> list[str]:
    v = []
    if re.search(r"\$\{p\.brand\}\s*\$\{p\.model_name\}", body):
        v.append("产品下拉仍裸拼 brand + model_name ⇒「Lenovo Lenovo Tab」")
    if "p.label" not in body:
        v.append("产品下拉应优先用后端给的 label（trends.display_label）")
    return v


PAT_NEQ = re.compile(r"""(?:<>|!=)\s*['"]rejected['"]""")
PAT_ACC = re.compile(r"""audit_status\s*=\s*'accepted'""")
# product_kind 的口径：必须是 = 'device'，不许再有 <> 'accessory'（会放行三态里的 unknown）
PAT_KIND_OK = re.compile(r"""product_kind\s*=\s*'device'""")
PAT_KIND_BAD = re.compile(r"""product_kind\s*(?:<>|!=)\s*'accessory'""")


def fn_strings(src: str, name: str):
    """(FunctionDef, 该函数里的字符串常量列表，不含 docstring)。注释不在语法树里，天然排除。"""
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if fn is None:
        return None, []
    doc = ast.get_docstring(fn, clean=False)
    strs = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != doc]
    return fn, strs


def return_dict_keys(fn) -> set:
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    return keys


# ================================================================ 0. 先证明判据会红
print("== 判据自检（反例必须红、只在注释里出现的写法必须绿）==")
OLD_DRAW = """async function drawSingle() {
  const s0 = r.series[0] || {};
  const showIndex = $('#tc-index').checked && s0.index;
  const series = r.series.map(s => ({ name: s.name, pts: showIndex ? (s.index || s.pts) : s.pts }));
  if (!series.some(s => s.pts.some(v => v != null))) { return Charts.empty('curve-chart', { title: 'x' }); }
  Charts.ask('change', 'curve-chart', { xs: r.xs, series, ylab: showIndex ? 'i' : `价格（${s0.currency || '本币'}）`, area: true });
}"""
COMMENT_ONLY_DRAW = """async function drawSingle() {
  // 以前是 showIndex = checked && s0.index，退回 (s.index || s.pts)
  /* ylab: s0.currency —— 多币种时取第一条线的币种 */
  const t = Charts.trendSeries(r, $('#tc-index').checked);
  const pending = Number(r.pending_obs || 0);
  if (t.blocked) { Charts.empty('curve-chart', t); }
  else { Charts.ask('change', 'curve-chart', { xs: r.xs, series: t.series, ylab: t.ylab, fmt: t.fmt,
    indexed: t.indexed, mixedCurrency: t.mixedCurrency, currencies: t.currencies }); }
}"""
H = "async function drawSingle() {"
old_v = draw_single_violations(js_fn_body(strip_js_comments(OLD_DRAW), H))
ok(len(old_v) >= 4, f"旧版 drawSingle（showIndex/s0.index/裸拼 ylab）必须被抓到 ≥4 条，实得 {old_v}")
ok(any("showIndex" in x for x in old_v), "旧版必须命中 showIndex/s0.index 那条")
ok(any("s.index || s.pts" in x for x in old_v), "旧版必须命中 `s.index || s.pts` 兜底那条")
ok(any("trendSeries" in x for x in old_v), "旧版必须命中「没走 trendSeries」那条")
ok(draw_single_violations(js_fn_body(strip_js_comments(COMMENT_ONLY_DRAW), H)) == [],
   "违规写法只出现在注释里 ⇒ 剥注释后必须全绿（否则断言在读注释）")
ok(draw_single_violations(js_fn_body(COMMENT_ONLY_DRAW, H)) != [],
   "不剥注释直接比会误报 —— 这证明剥注释这一步不是多余的")

OLD_DROP = "async function loadTrendBoard() {\n  fill('#tc-product', d.products.map(p => ({ v: p.id, t: `${p.brand} ${p.model_name}（x）` })));\n}"
NEW_DROP = "async function loadTrendBoard() {\n  // 以前 `${p.brand} ${p.model_name}` 会拼出 Lenovo Lenovo Tab\n  fill('#tc-product', d.products.map(p => ({ v: p.id, t: `${p.label || prodLabel(p.brand, p.model_name)}（x）` })));\n}"
HL = "async function loadTrendBoard() {"
ok(dropdown_violations(js_fn_body(strip_js_comments(OLD_DROP), HL)) != [], "裸拼 brand+model_name 的下拉必须被抓到")
ok(dropdown_violations(js_fn_body(strip_js_comments(NEW_DROP), HL)) == [], "用 p.label 的下拉（注释里提到旧写法）必须绿")

FAKE_OLD_PY = '''
def price_trend():
    """docstring 故意写 <> 'rejected'"""
    # 注释也故意写 audit_status <> 'rejected'，并写 product_kind = 'device'
    where = ["po.audit_status <> 'rejected'", "po.product_kind <> 'accessory'"]
    return {"series": []}
'''
FAKE_NEW_PY = '''
def price_trend():
    """<> 'rejected'"""
    # audit_status <> 'rejected'，以前是 product_kind <> 'accessory'
    where = ["po.audit_status = 'accepted'", "po.product_kind = 'device'"]
    return {"series": [], "pending_obs": 0, "unclassified_obs": 0}
'''
fn_o, s_o = fn_strings(FAKE_OLD_PY, "price_trend")
fn_n, s_n = fn_strings(FAKE_NEW_PY, "price_trend")
ok(sum(bool(PAT_NEQ.search(s)) for s in s_o) == 1,
   "反例：字符串里的 <> 'rejected' 抓到恰好 1 处（docstring 与注释不算）")
ok(not any(PAT_ACC.search(s) for s in s_o), "反例：没有 = 'accepted'")
ok("pending_obs" not in return_dict_keys(fn_o), "反例：返回字典没有 pending_obs")
ok(not any(PAT_NEQ.search(s) for s in s_n), "docstring/注释里的 <> 'rejected' 不算违规（走 ast 的意义）")
ok(any(PAT_ACC.search(s) for s in s_n) and "pending_obs" in return_dict_keys(fn_n), "正例：= 'accepted' + pending_obs 都能认出")
# product_kind 判据同样先证明会红：反例里 `<> 'accessory'` 在字符串常量里、
# `= 'device'` 只在注释里 ⇒ 走 ast 时必须判违规。
ok(any(PAT_KIND_BAD.search(s) for s in s_o) and not any(PAT_KIND_OK.search(s) for s in s_o),
   "反例：product_kind <> 'accessory' 抓得到，而注释里的 = 'device' 不算数")
ok(any(PAT_KIND_OK.search(s) for s in s_n) and not any(PAT_KIND_BAD.search(s) for s in s_n),
   "正例：product_kind = 'device' 认得出，注释里的旧写法不误报")
ok("unclassified_obs" not in return_dict_keys(fn_o)
   and "unclassified_obs" in return_dict_keys(fn_n),
   "返回字典里有没有 unclassified_obs 要能分辨（口径收紧不许静默吃行）")

# 模板串里套字符串、套 ${} 的花括号配平（真源码 renderCurveBasket 就是这种形态）
NEST = "function f() {\n  x.innerHTML = a ? `<a href=\"${esc(u)}\">${b ? `q{}` : 'z}'}</a>` : '';\n  return 1;\n}\nfunction g() { return 2; }"
ok((js_fn_body(strip_js_comments(NEST), "function f() {") or "").strip().endswith("return 1;"),
   "函数体提取要能跨过模板串里的嵌套引号与花括号，否则取到的是半截函数")

# ================================================================ 1. boards.js 真源码
print("== boards.js：drawSingle 走 trendSeries；下拉用 label；待审计/未画线写到页面 ==")
BOARDS = (ROOT / "app/web/boards.js").read_text(encoding="utf-8")
CODE = strip_js_comments(BOARDS)
draw = js_fn_body(CODE, H)
ok(draw is not None, "boards.js 应有 async function drawSingle()")
if draw is not None:
    vio = draw_single_violations(draw)
    ok(vio == [], "drawSingle 违规：" + "；".join(vio))
    ok("renderCurveMeta(" in draw, "drawSingle 每个分支之后都要刷说明区（renderCurveMeta）")
    ok("renderCurveBasket(" in draw, "drawSingle 每个分支之后都要刷篮子区（不然上一个对象的篮子留在页面上）")
    # 空图分支要能说出待审计
    ok("待价格审计" in draw, "画不出线时的空态文案要点明「待价格审计」")
    # blocked 分支必须先于 ask —— 守卫在前
    ok(draw.index(".blocked") < draw.index("Charts.ask('change'"),
       "blocked 判定必须在 Charts.ask 之前")

meta = js_fn_body(CODE, "function renderCurveMeta(")
ok(meta is not None, "boards.js 应有 renderCurveMeta")
if meta is not None:
    ok("dropped_series" in meta, "说明区要列出 dropped_series（哪些渠道线因观测不足未画）")
    ok("pending" in meta and "待价格审计" in meta, "说明区要写出待审计条数")
    ok("audit_status=accepted" in meta, "说明区要点明口径是 audit_status=accepted")
    ok(re.search(r"min_points\s*\|\|\s*2", meta) is not None, "未画线的门槛天数取接口的 min_points（缺省 2）")

lt = js_fn_body(CODE, HL)
ok(lt is not None, "boards.js 应有 loadTrendBoard")
if lt is not None:
    dv = dropdown_violations(lt)
    ok(dv == [], "产品下拉违规：" + "；".join(dv))
    ok("prodLabel(" in lt, "后端没给 label 时退到 prodLabel（app.js 的同一逻辑），不是退回裸拼")

# 全文（剥注释后）不许再出现旧分支
ok("showIndex" not in CODE, "boards.js 剥注释后不许再出现 showIndex")
ok("s0.index" not in CODE, "boards.js 剥注释后不许再出现 s0.index")

# ================================================================ 2. dashboard.price_trend 源码
print("== dashboard.price_trend / server.trend_candidates：= 'accepted' 且返回 pending 条数 ==")
DASH = (ROOT / "app/dashboard.py").read_text(encoding="utf-8")
fn, strs = fn_strings(DASH, "price_trend")
ok(fn is not None, "dashboard.py 应有 price_trend")
if fn is not None:
    ok(any(PAT_ACC.search(s) for s in strs), "price_trend 的 SQL 必须用 audit_status = 'accepted'")
    ok(not any(PAT_NEQ.search(s) for s in strs), "price_trend 不许再有 <> 'rejected'（pending 当通过）")
    ok(any("audit_status = 'pending'" in s for s in strs), "price_trend 要另数一遍 pending 的条数")
    # ★ 与 trends.product_series 的口径必须一字不差（两页画同一款产品）
    ok(any(PAT_KIND_OK.search(s) for s in strs),
       "price_trend 的 product_kind 必须是 = 'device'（与 trends.product_series 同口径）")
    ok(not any(PAT_KIND_BAD.search(s) for s in strs),
       "price_trend 不许再用 product_kind <> 'accessory' —— 那会放行三态里的 unknown")
    ok(any("rp.category_code" in s for s in strs),
       "price_trend 要挡跨品类挂接（po.category_code 与 rp.category_code 对不上的行）")
    keys = return_dict_keys(fn)
    ok({"series", "pending_obs", "pending_days", "audit_filter", "note",
        "unclassified_obs", "unclassified_days"} <= keys,
       f"price_trend 返回要带 pending_obs/pending_days/audit_filter/unclassified_obs，"
       f"实得 {sorted(keys)}")

# 两处口径必须一致：trends.product_series 也走 = 'device' + 跨品类守卫
TRENDS = (ROOT / "app/trends.py").read_text(encoding="utf-8")
fn_ps, strs_ps = fn_strings(TRENDS, "product_series")
ok(fn_ps is not None, "trends.py 应有 product_series")
if fn_ps is not None:
    ok(any(PAT_KIND_OK.search(s) for s in strs_ps) and not any(PAT_KIND_BAD.search(s) for s in strs_ps),
       "trends.product_series 的 product_kind 口径应为 = 'device'")
    ok(any("rp.category_code" in s for s in strs_ps),
       "trends.product_series 同样要挡跨品类挂接")

SERVER = (ROOT / "app/api/server.py").read_text(encoding="utf-8")
fn_s, strs_s = fn_strings(SERVER, "trend_candidates")
ok(fn_s is not None, "server.py 应有 trend_candidates")
if fn_s is not None:
    ok(any(re.search(r"audit_status\s*=\s*'accepted'", s) for s in strs_s),
       "品牌下拉的 obs_days 只数 accepted")
    ok(not any(PAT_NEQ.search(s) for s in strs_s), "品牌下拉不许再用 <> 'rejected'")

# ================================================================ 3. 临时库真跑 price_trend
print(f"== price_trend 临时库：pending 不上图、计数对（{config.DB_PATH}）==")
db.init_db()
with db.tx() as c:
    c.execute("INSERT OR IGNORE INTO country(code,name_zh,currency,lang,locale,timezone) "
              "VALUES('CL','智利','CLP','es','es-CL','America/Santiago')")
    c.execute("INSERT OR IGNORE INTO country(code,name_zh,currency,lang,locale,timezone) "
              "VALUES('MX','墨西哥','MXN','es','es-MX','America/Mexico_City')")
    c.execute("INSERT OR IGNORE INTO category(code,name_zh) VALUES('tablet','平板')")
    c.execute("INSERT OR IGNORE INTO brand(id,name,is_ours) VALUES(9101,'LenovoTest',0)")
    c.execute("INSERT OR IGNORE INTO channel(id,code,country_code,name,kind) "
              "VALUES(9101,'t_ripley','CL','Ripley·测','retailer')")
    c.execute("INSERT OR IGNORE INTO channel(id,code,country_code,name,kind) "
              "VALUES(9102,'t_liverpool','MX','Liverpool·测','retailer')")
    for pid, model in ((9101, "LenovoTest Tab P11"), (9102, "LenovoTest Tab M10")):
        c.execute("INSERT INTO rival_product(id,brand_id,category_code,model_name,model_key) "
                  "VALUES(?,9101,'tablet',?,?)", (pid, model, model.lower().replace(" ", "")))

# price_trend 的窗口按 date.today() 算，日期贴着今天造
D = [(date.today() - timedelta(days=k)).isoformat() for k in (3, 2, 1)]
OBS = []   # (pid, day, cc, channel, currency, price, audit)
for d, p in zip(D, (180_000, 170_000, 160_000)):        # CL Ripley 整机，审计通过
    OBS.append((9101, d, "CL", 9101, "CLP", p, "accepted"))
OBS.append((9101, D[2], "CL", 9101, "CLP", 8_500, "pending"))    # 钢化膜价，待审 —— 绝不能上图
OBS.append((9101, D[1], "CL", 9101, "CLP", 9_000, "pending"))    # 第二条待审（另一天）
OBS.append((9101, D[0], "CL", 9101, "CLP", 1_000, "rejected"))   # 已驳回：既不上图也不算 pending
for d, p in zip(D[:2], (5_000, 5_000)):                  # MX Liverpool 两天
    OBS.append((9101, d, "MX", 9102, "MXN", p, "accepted"))
for d in D:                                              # 产品 9102 全部待审
    OBS.append((9102, d, "CL", 9101, "CLP", 150_000, "pending"))
with db.tx() as c:
    for i, (pid, d, cc, ch, cur, price, audit) in enumerate(OBS):
        c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                        category_code,rival_product_id,title,sale_price,currency,
                        product_kind,condition,is_bundle,audit_status,row_hash)
                     VALUES(?,?,?,9101,'tablet',?,?,?,?,'device','new',0,?,?)""",
                  (d, cc, ch, pid, f"cb{i}", price, cur, audit, f"cbh{i}"))

    # ★ 口径用例（都是 CL / Ripley / 产品 9101，价位都远低于整机线，进了就一眼看得出）：
    #   ① unknown + accepted：分类器**拿不准**是不是整机（extract 三态兜底，
    #      "留给价格审计按基线判"），审计只管价格合不合理、不回答这个问题。
    #      旧口径 `product_kind <> 'accessory'` 会把它画上去 —— 真实个案是
    #      #3016 CL 的 24,602 维修屏对上整机线的 139,990~729,990。
    #   ② unknown + pending：既不上图也不算 pending（pending 计数只数 device）
    #   ③ 跨品类挂接（po.category_code='audio' 而 rp.category_code='tablet'）+ accepted：
    #      音频品类刻意无价格地板 ⇒ 审计永远抓不到，只能在消费方挡。
    EXTRA = [                 # (day, price, kind, category, audit, hash)
        (D[0], 6_600, "unknown", "tablet", "accepted", "cbx1"),
        (D[1], 6_400, "unknown", "tablet", "accepted", "cbx2"),
        (D[2], 6_200, "unknown", "tablet", "pending", "cbx3"),
        (D[0], 7_700, "device", "audio", "accepted", "cbx4"),
        (D[1], 7_100, "device", "audio", "pending", "cbx5"),
    ]
    for d, price, kind, cat, audit, h in EXTRA:
        c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                        category_code,rival_product_id,title,sale_price,currency,
                        product_kind,condition,is_bundle,audit_status,row_hash)
                     VALUES(?,'CL',9101,9101,?,9101,?,?,'CLP',?,'new',0,?,?)""",
                  (d, cat, f"extra {h}", price, kind, audit, h))

r = dashboard.price_trend(9101, days=30)
prices = [p["price"] for s in r["series"] for p in s["points"]]
ok(len(r["series"]) == 2, f"应有 CL Ripley + MX Liverpool 两条线，实得 {len(r['series'])}")
ok(8_500 not in prices and 9_000 not in prices and 1_000 not in prices,
   f"★ pending 的 8,500/9,000 与 rejected 的 1,000 都不能进 MIN(sale_price)，实得 {prices}")
cl = next(s for s in r["series"] if s["country_code"] == "CL")
ok([p["price"] for p in cl["points"]] == [180_000, 170_000, 160_000],
   f"CL 线应只剩三条审计通过的整机价，实得 {[p['price'] for p in cl['points']]}")
ok(r["pending_obs"] == 2 and r["pending_days"] == 2,
   f"被挡掉的 pending 应报 2 条 / 2 天（rejected 不算），实得 {r['pending_obs']}/{r['pending_days']}")
ok(r["audit_filter"] == "accepted", "返回要声明口径 audit_filter=accepted")
ok(r["trendable"] is True and "待价格审计" in r["note"] and "2 条" in r["note"],
   f"有线可画时 note 也要带上待审计条数，实得 {r['note']!r}")

r_cl = dashboard.price_trend(9101, country="CL", days=30)
r_mx = dashboard.price_trend(9101, country="MX", days=30)
ok(r_cl["pending_obs"] == 2 and len(r_cl["series"]) == 1, "按国家筛选后 pending 计数同一组过滤（CL=2）")
ok(r_mx["pending_obs"] == 0 and len(r_mx["series"]) == 1, "MX 没有待审行 ⇒ pending_obs=0")
r_ch = dashboard.price_trend(9101, channel_id=9102, days=30)
ok(r_ch["pending_obs"] == 0 and len(r_ch["series"]) == 1, "按渠道筛选（Liverpool）pending_obs=0")
r_off = dashboard.price_trend(9101, days=30, official_only=True)
ok(r_off["series"] == [] and r_off["pending_obs"] == 0,
   "official_only 路径（JOIN channel 的 c.kind）在两条查询上都要能跑且口径一致")

r2 = dashboard.price_trend(9102, days=30)
ok(r2["series"] == [], "全 pending 的产品：series 为空，不许把待审点画上去")
ok(r2["pending_obs"] == 3 and r2["pending_days"] == 3,
   f"★ 全 pending 的产品要报出 3 条 / 3 天待审计 —— 空图才分得清「待审」与「没数据」，实得 {r2['pending_obs']}/{r2['pending_days']}")
ok(r2["trendable"] is False and "待价格审计" in r2["note"], f"note 要点明待审计，实得 {r2['note']!r}")

r3 = dashboard.price_trend(9101, days=30, country="BR")
ok(r3["series"] == [] and r3["pending_obs"] == 0 and "待价格审计" not in r3["note"],
   "真没数据的筛选（BR）不许凭空说有待审计")

# ============================================ 4. 口径：unknown 与跨品类都不进曲线
print("== product_kind='device' + 跨品类守卫：unknown / audio 行都不上图 ==")
cl2 = next(s for s in r["series"] if s["country_code"] == "CL")
cl_prices = [p["price"] for p in cl2["points"]]
ok(6_600 not in cl_prices and 6_400 not in cl_prices,
   f"★ product_kind='unknown' 的低价行（分类器拿不准）不许进 MIN(sale_price)，实得 {cl_prices}")
ok(7_700 not in cl_prices,
   f"★ 跨品类挂接（po.category='audio' vs rp.category='tablet'）不许进曲线，实得 {cl_prices}")
ok(cl_prices == [180_000, 170_000, 160_000],
   f"★ CL 线仍应只有三条审计通过的整机价，实得 {cl_prices}")
ok(r["pending_obs"] == 2,
   f"pending 计数走同一组过滤：unknown 的 6,200 与 audio 的 7,100 都不算，实得 {r['pending_obs']}")
ok(r["unclassified_obs"] == 2 and r["unclassified_days"] == 2,
   f"★ 被 product_kind 门挡掉的 accepted 行要如实报出（2 条 / 2 天）——"
   f"口径收紧不许静默吃行，实得 {r['unclassified_obs']}/{r['unclassified_days']}")
ok("未判定" in r["note"] and "2 条" in r["note"],
   f"note 要把「未判定」与「待审计」分开说，实得 {r['note']!r}")
ok(r.get("kind_filter") == "device", "返回要声明 product_kind 口径")

# 反向自证：不加这两道守卫时，那三条低价行确实会成为 MIN —— 否则上面几条断言是恒真的
_raw = db.q("""SELECT obs_date, MIN(sale_price) p FROM price_obs
               WHERE rival_product_id=9101 AND country_code='CL' AND channel_id=9101
                 AND audit_status='accepted' AND product_kind <> 'accessory'
               GROUP BY obs_date ORDER BY obs_date""")
ok([x["p"] for x in _raw] == [6_600, 6_400, 160_000],
   f"反向自证失败：旧口径（<> 'accessory' 且不判品类）本该把 6,600/6,400/7,700 顶成当天最低价，"
   f"实得 {[x['p'] for x in _raw]}")

print(f"\ncurve_board: {PASS} 通过, {len(FAIL)} 失败  （临时库 {config.DB_PATH}）")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
