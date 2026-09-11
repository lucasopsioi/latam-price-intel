# -*- coding: utf-8 -*-
"""展示名与曲线口径：前端 JS 与后端 Python 必须逐字一致（用 node 真跑 JS，不是读代码）。

守的性质（用户截图：曲线页下拉/标题出现「Lenovo Lenovo Tab」）：
  1. app.js prodLabel(brand, model) 与 trends.display_label / weekly._label 对同一批输入
     输出逐字相同 —— 三处分叉的后果是周报叫一个名、曲线页叫另一个名。
  2. app.js modelPart：品牌单独显示时型号格去掉打头的品牌词，剥空则退回完整名
     （「剥出空型号比留噪声危险」）。
  3. charts.js Charts.trendSeries（趋势接口返回 → 画图参数的唯一实现）：
     多币种必走指数；要走指数而缺 index 则 blocked，**绝不退回绝对价**；
     单币种纵轴标签用后端 y_label（该币种），多币种是「指数（基期=100）」。
  4. ★ **真渲染**：把 brand='Lenovo' / model_name='Lenovo Tab' 喂给页面的渲染函数，
     断言渲染出来的文字里不含「Lenovo Lenovo」。
     ——「静态正则扫源码」这条路已经被证伪：旧版的 RAW_CONCAT 只认
     `${x.brand}…${x.model_name}`（同一变量、且字段就叫 model_name）这一种形态，
     于是 boards.js:601 的 `${esc(r.brand)} ${esc(r.model)}`（字段叫 model）**看不见**，
     测试却印着「boards.js 0 处」—— 一句**假的干净**比不检查更坏，
     它让人以为这条性质有人守。静态扫描保留为**次要**网（已放宽到 model/model_name），
     判定以真渲染为准。
  5. 已知未修：boards.js 的两处（loadWatchBoard 候选表、loadPosition 对位机型列）
     本轮不许改 boards.js ⇒ 钉成**基线清单**：多一处会红（新增违规），
     少一处也会红（修好了就把它从清单里删掉）。绝不写成"只打印不断言"。

没有 node 时 1~4 打印 SKIP 并以退出码 2 结束 —— 跳过不是通过。
跑法： python tests\\test_label_parity.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app import trends                      # noqa: E402
from app.agents import weekly               # noqa: E402

PASS = 0
FAIL: list[str] = []


def ok(cond, msg):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(msg)


APP_JS = (ROOT / "app/web/app.js").read_text(encoding="utf-8")
BOARDS_JS = (ROOT / "app/web/boards.js").read_text(encoding="utf-8")
CHARTS_JS = (ROOT / "app/web/charts.js").read_text(encoding="utf-8")


def js_function(src: str, name: str) -> str:
    """按花括号配对切出 `function name(...) {...}` 的完整源码（JS 没有 ast，靠配对不靠正则）。

    ★ `async` 前缀要带上：切掉它得到的是一个**同步**函数，函数体里的 await 直接语法错误。
    """
    i = src.find(f"function {name}(")
    if i < 0:
        raise KeyError(name)
    if src[max(0, i - 6):i] == "async ":
        i -= 6
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        ch = src[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise ValueError(f"unbalanced braces in {name}")


# ---------------------------------------------------------------- 4. 裸拼扫描（纯 Python）
def strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


# `${esc(x.brand)}</b> ${esc(x.model_name)}` / `${p.brand} ${p.model_name}` 一类；
# 中间夹 modelPart(/prodLabel( 的不算（那正是修法）。
# ★ 字段名放宽到 model / model_name：旧版只认 model_name，于是 boards.js:601 的
#   `${esc(r.brand)} ${esc(r.model)}` 逃掉了，测试还印着「0 处」。
#   即便如此，这条静态网仍然**只是次要网** —— 判定以下面的真渲染为准。
RAW_CONCAT = re.compile(r"(\w+)\.brand\)?\}(?:\s|</?b>|·)*\$\{(?:esc\()?\1\.model(?:_name)?\b")
ok(RAW_CONCAT.search("t: `${p.brand} ${p.model_name}（x）`") is not None
   and RAW_CONCAT.search("<b>${esc(r.brand)}</b> ${esc(r.model_name)}") is not None,
   "正例自检：裸拼正则必须能命中两种已知形态（否则下面的 0 命中是恒真）")
ok(RAW_CONCAT.search("`${esc(r.brand)} ${esc(r.model)}`") is not None,
   "★ 正例自检：boards.js:601 那种 `.model`（不是 model_name）也必须命中 ——"
   "旧版正则漏掉它，于是打印出一句假的「boards.js 0 处」")
ok(RAW_CONCAT.search("<b>${esc(r.brand)}</b> ${esc(modelPart(r.brand, r.model_name))}") is None,
   "反例自检：经 modelPart 的拼法不该被判成裸拼")
app_hits = RAW_CONCAT.findall(strip_js_comments(APP_JS))
ok(not app_hits, f"app.js 里不许再裸拼 brand + model_name，实得 {len(app_hits)} 处")
boards_hits = len(RAW_CONCAT.findall(strip_js_comments(BOARDS_JS)))
ok(boards_hits == 1,
   f"boards.js 的静态裸拼数应为 1（loadPosition 的对位机型列，本轮不许改它）——"
   f"多一处是新增违规、少一处说明修好了要把基线改掉，实得 {boards_hits}")

NODE = shutil.which("node")
if not NODE:
    print(f"label_parity: {PASS} 通过, {len(FAIL)} 失败；SKIP: 未找到 node，JS 侧 1~3 未验证")
    for f in FAIL:
        print("  ✗", f)
    sys.exit(2 if not FAIL else 1)

# ---------------------------------------------------------------- 1~3. 在 node 里真跑 JS
CASES = [
    ("Lenovo", "Lenovo Tab"), ("Lenovo", "Lenovo Idea Tab"), ("Lenovo", "lenovo tab"),
    ("Honor", "Honor Honor 70 Honor"), ("Samsung", "Galaxy S25"),
    ("Apple", "Apple iPad Air 11 M3"), ("Apple", "iPad Air 11 M3"),
    ("Xiaomi", "Xiaomi Redmi Pad 2"), ("HONOR", "honor pad x9"),
    ("Lenovo", "  Lenovo   Tab  "), ("Motorola", "Moto G06"),
    (None, "Galaxy S25"), ("Samsung", None), (None, None), ("", "Redmi Pad 2"), ("Xiaomi", ""),
    ("Xiaomi", "Xiaomi"), ("Apple", "Apple Apple"), ("Tab", "Lenovo Tab"),
    ("Acme", "Slate Pro 12.2 (2024)"), ("Samsung", "Galaxy Tab A11 Samsung"),
]
MODELPART = [("Lenovo", "Lenovo Tab"), ("Samsung", "Galaxy S25"), ("Xiaomi", "Xiaomi"),
             ("", "Redmi Pad 2"), ("Honor", "Honor Honor 70 Honor")]
TREND_FIXTURES = {
    "mixed_missing_index": {"r": {"mixed_currency": True, "currencies": ["CLP", "BRL"],
                                  "series": [{"name": "a", "pts": [1, 2], "index": [100, 200]},
                                             {"name": "b", "pts": [3, 4], "index": None}]},
                            "want": False},
    "mixed_ok": {"r": {"mixed_currency": True, "currencies": ["CLP", "BRL"],
                       "y_label": "指数（基期=100）",
                       "series": [{"name": "a", "pts": [None, None], "index": [100, 110],
                                   "filled": [False, True]},
                                  {"name": "b", "pts": [None, None], "index": [100, 90]}]},
                 "want": False},
    "single_abs": {"r": {"mixed_currency": False, "currencies": ["CLP"], "y_label": "价格（CLP）",
                         "series": [{"name": "a", "pts": [100000, 110000], "index": [100, 110]}]},
                   "want": False},
    "single_idx": {"r": {"mixed_currency": False, "currencies": ["CLP"], "y_label": "价格（CLP）",
                         "series": [{"name": "a", "pts": [100000, 110000], "index": [100, 110]}]},
                   "want": True},
    "required_flag": {"r": {"mixed_currency": False, "indexed_required": True, "currencies": [],
                            "series": [{"name": "a", "pts": [1, 2], "index": [100, 200]}]},
                      "want": False},
}

# ---------------------------------------------------------------- 4. 真渲染夹具
# ★ 只喂一个品牌词「Lenovo」，所以渲染结果里出现「Lenovo Lenovo」必然是裸拼造成的，
#   不会是夹具自己带进去的。model_name / model 两个字段名都给（两处页面用的名字不同）。
RENDER_FIXTURES = {
    "/api/trend/products": {"rows": [
        {"id": 1, "brand": "Lenovo", "model_name": "Lenovo Tab", "cat": "tablet",
         "obs_days": 5, "countries": 1, "channels": 2}]},
    "/api/trend/": {
        "product": {"brand": "Lenovo", "model_name": "Lenovo Tab"},
        "series": [{"country_code": "CL", "channel": "Ripley", "currency": "CLP",
                    "change_pct": -1.0, "days": 2, "first_price": 100000,
                    "last_price": 99000, "min_price": 99000, "max_price": 100000,
                    "points": [{"date": "2026-09-01", "price": 100000, "listings": 1},
                               {"date": "2026-09-02", "price": 99000, "listings": 1}]}],
        "obs_days": 2, "trendable": True, "note": "口径说明", "pending_obs": 0,
        "pending_days": 0, "unclassified_obs": 0, "unclassified_days": 0},
    "/api/watchlist": {
        "items": [{"id": 1, "priority": "P1", "priority_zh": "常规",
                   "label": "Lenovo Tab · CL", "eff_drop": 5, "eff_rise": 5,
                   "unread": 0, "note": ""}],
        "candidates": [{"id": 1, "brand": "Lenovo", "model_name": "Lenovo Tab",
                        "cat_zh": "平板", "obs_days": 5, "channels": 2,
                        "moves": 1, "watched": 0}]},
    "/api/alerts": {"items": []},
    "/api/position": {
        "total": 1, "thin_n": 0, "min_field": 3, "sign_note": "站位口径", "note": "说明",
        "dist": {}, "items": [
            {"my_product_id": 1, "my_name": "ACME Slate Tab", "country_code": "CL",
             "currency": "CLP", "my_price": 100000, "field_median": 90000,
             "field_low": 80000, "field_high": 120000, "rival_n": 4,
             "my_vs_field_pct": 11.1, "band": "high_soft", "band_zh": "略高",
             "top_rivals": [{"brand": "Lenovo", "model": "Lenovo Tab",
                             "price": 90000, "score": 0.9, "confirmed": True}]}]},
}
# ★ 空图夹具：审计积压未清时的真实形态 —— 有观测、但一条都还没审完（pending-only）。
#   showTrend 以前在 series 为空时写死「这个产品在当前筛选下没有价格」并 return，
#   接口给的 pending_obs / note 一个字都到不了页面。近 30 天 3,826 个有观测的产品里
#   2,409 个（63%）是 pending-only ⇒ 那句话此刻正对着 63% 的产品说谎。
RENDER_EMPTY = dict(RENDER_FIXTURES)
RENDER_EMPTY["/api/trend/"] = {
    "product": {"brand": "Lenovo", "model_name": "Lenovo Tab"},
    "series": [], "obs_days": 0, "trendable": False,
    "note": "只画审计通过的；另有 7 条观测（3 个采集日）仍待价格审计，未上图。",
    "pending_obs": 7, "pending_days": 3,
    "unclassified_obs": 2, "unclassified_days": 1}
# 真的没有任何观测（不是待审）：这时才该说"没有价格"
RENDER_NODATA = dict(RENDER_FIXTURES)
RENDER_NODATA["/api/trend/"] = {
    "product": {"brand": "Lenovo", "model_name": "Lenovo Tab"},
    "series": [], "obs_days": 0, "trendable": False, "note": "",
    "pending_obs": 0, "pending_days": 0,
    "unclassified_obs": 0, "unclassified_days": 0}

ROUTE_SETS = {"main": RENDER_FIXTURES, "empty": RENDER_EMPTY, "nodata": RENDER_NODATA}
TARGETS = [
    {"name": "app.loadTrend", "module": "app", "fn": "loadTrend", "routes": "main"},
    {"name": "app.showTrend", "module": "app", "fn": "showTrend", "routes": "main", "arg": 1},
    {"name": "app.showTrend.pending", "module": "app", "fn": "showTrend",
     "routes": "empty", "arg": 1},
    {"name": "app.showTrend.nodata", "module": "app", "fn": "showTrend",
     "routes": "nodata", "arg": 1},
    {"name": "boards.loadWatchBoard", "module": "boards", "fn": "loadWatchBoard",
     "routes": "main"},
    {"name": "boards.loadPosition", "module": "boards", "fn": "loadPosition",
     "routes": "main"},
]
# 已知未修的渲染函数（本轮不许改 boards.js）。基线两头都守：多一个红、少一个也红。
KNOWN_DUP = {"boards.loadWatchBoard", "boards.loadPosition"}

RENDER_HARNESS = r"""
/* 用最小 DOM 桩把渲染函数真的跑一遍，收集所有写进 innerHTML 的 HTML。 */
function makeEnv(routes) {
  const html = [];
  const mkEl = () => {
    const e = { value: '', checked: false, textContent: '', style: {}, dataset: {},
                classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
                appendChild() {}, addEventListener() {}, removeAttribute() {},
                setAttribute() {}, focus() {}, scrollIntoView() {},
                querySelector: () => mkEl(), querySelectorAll: () => [],
                options: [], _html: '' };
    Object.defineProperty(e, 'innerHTML', {
      get() { return e._html; },
      set(v) { e._html = String(v); html.push(String(v)); },
    });
    return e;
  };
  const keys = Object.keys(routes).sort((a, b) => b.length - a.length);
  const env = {
    html,
    document: { getElementById: () => mkEl(), querySelector: () => mkEl(),
                querySelectorAll: () => [], createElement: () => mkEl(),
                addEventListener() {} },
    window: {},
    $: () => mkEl(),
    $$: () => [],
    esc: (s) => String(s ?? '').replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    num: (v) => (v == null ? '—' : String(Math.round(Number(v)))),
    api: async (path) => {
      for (const k of keys) if (String(path).startsWith(k)) return routes[k];
      return {};
    },
    toast() {}, setSel() {}, applyCtx() {}, run() {}, fill() {},
    setNote() {}, syncKindPicker() {}, renderPicks() {},
    CAT_ZH: { phone: '手机', tablet: '平板' },
    Charts: { empty() {}, ask() {}, trendSeries: (r) => ({ blocked: true }),
              resize() {}, dispose() {} },
    fetch: async () => ({ json: async () => ({}) }),
  };
  return env;
}

function collect(env, factorySrc, params, pick) {
  const fn = new Function(...params, factorySrc);
  return fn(...params.map(p => env[p]))[pick];
}

const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const PARAMS = ['document', 'window', '$', '$$', 'esc', 'num', 'api', 'toast', 'setSel',
                'applyCtx', 'run', 'fill', 'setNote', 'syncKindPicker', 'renderPicks',
                'CAT_ZH', 'Charts', 'fetch', 'prodLabel', 'modelPart'];

async function renderAll() {
  const out = {};
  const APP_SRC = input.appFns + '\nlet TREND_SEL = null;\n'
    + '\nreturn { loadTrend, showTrend };';
  const BOARDS_SRC = input.boardsSrc + '\nreturn { loadWatchBoard, loadPosition };';
  const SRC = { app: APP_SRC, boards: BOARDS_SRC };
  for (const t of input.targets) {
    const env = makeEnv(input.routeSets[t.routes]);
    env.prodLabel = prodLabel;
    env.modelPart = modelPart;
    try {
      const f = collect(env, SRC[t.module], PARAMS, t.fn);
      await (t.arg === undefined ? f() : f(t.arg));
      out[t.name] = { ok: true, html: env.html.join('\n') };
    } catch (e) {
      out[t.name] = { ok: false, err: String(e && e.message || e), html: env.html.join('\n') };
    }
  }
  return out;
}
"""

JS = "\n".join([
    js_function(APP_JS, "prodLabel"),
    js_function(APP_JS, "modelPart"),
    js_function(CHARTS_JS, "trendSeries"),
    RENDER_HARNESS,
    """
const out = { labels: input.cases.map(([b, m]) => prodLabel(b, m)),
              parts: input.parts.map(([b, m]) => modelPart(b, m)),
              trend: {} };
for (const [k, f] of Object.entries(input.trend)) {
  const t = trendSeries(f.r, f.want);
  out.trend[k] = { blocked: !!t.blocked, indexed: !!t.indexed, ylab: t.ylab || null,
                   pts: (t.series || []).map(s => s.pts), fmt0: t.fmt ? t.fmt(110.456) : null };
}
renderAll().then(r => { out.render = r; process.stdout.write(JSON.stringify(out)); })
           .catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(3); });
""",
])
_APP_FNS = "\n".join(js_function(APP_JS, n) for n in
                     ("loadTrend", "showTrend", "trendSvg"))
payload = json.dumps({"cases": CASES, "parts": MODELPART, "trend": TREND_FIXTURES,
                      "routeSets": ROUTE_SETS, "targets": TARGETS,
                      "appFns": _APP_FNS, "boardsSrc": BOARDS_JS})
proc = subprocess.run([NODE, "-e", JS], input=payload, capture_output=True, text=True,
                      encoding="utf-8", timeout=60)
ok(proc.returncode == 0, f"node 执行 JS 失败：{proc.stderr[-400:]}")
res = json.loads(proc.stdout) if proc.returncode == 0 else {"labels": [], "parts": [], "trend": {}}

# 1. prodLabel ≡ display_label ≡ weekly._label
for (b, m), got in zip(CASES, res["labels"]):
    want = trends.display_label(b, m)
    ok(got == want, f"prodLabel({b!r},{m!r}) = {got!r}，display_label = {want!r}")
    ok(want == weekly._label(b, m),
       f"display_label 与 weekly._label 分叉：{b!r},{m!r} → {want!r} vs {weekly._label(b, m)!r}")
ok(len(res["labels"]) == len(CASES), "node 返回的 label 数要与用例数相等")

# 2. modelPart
WANT_PARTS = ["Tab", "Galaxy S25", "Xiaomi", "Redmi Pad 2", "70"]
for (b, m), got, want in zip(MODELPART, res["parts"], WANT_PARTS):
    ok(got == want, f"modelPart({b!r},{m!r}) 应为 {want!r}，实得 {got!r}")

# 3. Charts.trendSeries
t = res["trend"]
ok(t.get("mixed_missing_index", {}).get("blocked") is True,
   "多币种且有线缺 index：必须 blocked（绝不退回绝对价）")
ok(t.get("mixed_missing_index", {}).get("pts") == [],
   "blocked 时不该给出任何可画的序列")
mo = t.get("mixed_ok", {})
ok(mo.get("blocked") is False and mo.get("indexed") is True,
   f"多币种且全有 index：走指数，实得 {mo}")
ok(mo.get("pts") == [[100, 110], [100, 90]], f"多币种时画的是 index 不是 pts，实得 {mo.get('pts')}")
ok(mo.get("ylab") == "指数（基期=100）", f"多币种纵轴标签应为指数，实得 {mo.get('ylab')!r}")
ok(mo.get("fmt0") == "110.5", f"指数格式化保留 1 位小数，实得 {mo.get('fmt0')!r}")
sa = t.get("single_abs", {})
ok(sa.get("indexed") is False and sa.get("pts") == [[100000, 110000]],
   f"单币种未勾选指数：画绝对价，实得 {sa}")
ok(sa.get("ylab") == "价格（CLP）", f"单币种纵轴标签用后端 y_label（该币种），实得 {sa.get('ylab')!r}")
ok(sa.get("fmt0") == "110", f"绝对价格式化取整，实得 {sa.get('fmt0')!r}")
si = t.get("single_idx", {})
ok(si.get("indexed") is True and si.get("pts") == [[100, 110]]
   and si.get("ylab") == "指数（基期=100）", f"单币种勾选指数：可选口径要能走，实得 {si}")
ok(t.get("required_flag", {}).get("indexed") is True,
   "后端 indexed_required=true 时即使未勾选也必须走指数")

# ---------------------------------------------------------------- 4. 真渲染：不许出现「Lenovo Lenovo」
def visible_text(html: str) -> str:
    """把渲染出来的 HTML 折成"读者看到的字"：标签换成空格、反转义、压空白。

    ★ 标签必须换成**空格**而不是删掉，否则 `<td>A</td><td>B</td>` 会粘成 AB；
      换成空格之后，跨单元格的品牌重复（品牌列 + 型号列都写 Lenovo）同样看得见 ——
      那正是 app.js 用 modelPart 解决、而 boards.js 还没解决的同一件事。
    """
    txt = re.sub(r"<[^>]*>", " ", html or "")
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        txt = txt.replace(a, b)
    return re.sub(r"\s+", " ", txt)


DUP = re.compile(r"\bLenovo\s+Lenovo\b")
ok(DUP.search(visible_text("<td>Lenovo</td><td><b>Lenovo Tab</b></td>")) is not None
   and DUP.search(visible_text("`<b>Lenovo</b> Lenovo Tab`")) is not None,
   "★ 判据自检：两种真实形态（跨单元格 / 同一格）都必须被认出，否则下面全是恒真")
ok(DUP.search(visible_text("<b>Lenovo</b> Tab")) is None
   and DUP.search(visible_text("<td>Lenovo</td><td>Tab</td>")) is None,
   "★ 判据自检：经 modelPart 去重之后不该误报")

render = res.get("render") or {}
ok(set(render) == {t["name"] for t in TARGETS},
   f"每个渲染用例都要跑到，实得 {sorted(render)}")
for _name in sorted(render):
    _r = render[_name]
    ok(_r.get("ok") is True, f"{_name} 渲染出错（桩不够/函数变了）：{_r.get('err')}")
    ok(bool(_r.get("html")), f"{_name} 一个字都没渲染出来 —— 断言会恒真，先修夹具")

dup_found = {n for n, r in render.items() if DUP.search(visible_text(r.get("html")))}
ok(not {n for n in dup_found if n.startswith("app.")},
   f"★ app.js 的渲染结果不许出现「Lenovo Lenovo」，实得 {sorted(dup_found)}")

# ---------------------------------------------------------------- 5. 空图必须区分"待审计"与"没数据"
_pend_html = visible_text(render.get("app.showTrend.pending", {}).get("html", ""))
_nodata_html = visible_text(render.get("app.showTrend.nodata", {}).get("html", ""))
ok("待价格审计" in _pend_html,
   f"★ pending-only 的产品：空图必须说清是「待价格审计」，实得 {_pend_html[:220]!r}")
# ★ 只禁那句**下结论**的文案本身；空态里引用它做对比（「这不是"没有价格"」）是好文案，
#   不能用裸子串一刀切 —— 那是 scrape-normalize 那一课的"假朋友"。
ok("当前筛选下没有价格" not in _pend_html,
   f"★ 有 7 条待审观测时**绝不许**写「这个产品在当前筛选下没有价格」——"
   f"那是对 63% 的产品说谎，实得 {_pend_html[:220]!r}")
ok("7 条" in _pend_html and "3 个采集日" in _pend_html,
   f"★ 待审的条数与采集日数要落到页面上（后端专门给了才有意义），实得 {_pend_html[:220]!r}")
ok("未判定" in _pend_html or "整机/配件" in _pend_html,
   f"unclassified_obs 也要说出来（被 product_kind 门挡掉的那批），实得 {_pend_html[:220]!r}")
ok("仍待价格审计，未上图" in _pend_html,
   f"★ note 在**空图分支**也要渲染（以前空图直接 return，note 一个字都到不了页面），"
   f"实得 {_pend_html[:220]!r}")
ok("当前筛选下没有价格" in _nodata_html and "待价格审计" not in _nodata_html,
   f"★ 真没有观测时才说「没有价格」—— 两种空态不能长得一样，实得 {_nodata_html[:220]!r}")
ok(_pend_html != _nodata_html,
   "★ 「待审计」与「真没数据」两种空态渲染出的字必须不同（长得一样=分不出）")
_full_html = visible_text(render.get("app.showTrend", {}).get("html", ""))
ok("口径说明" in _full_html,
   f"★ 画得出线时 note 同样要渲染（以前只在 !trendable 时渲染），实得 {_full_html[:220]!r}")
# 已知未修的两处：本轮不许改 boards.js。基线两头都守 —— 多一个是新增违规，
# 少一个说明有人修好了、要把它从清单里划掉（"只打印不断言"会让这条性质永远没人守）。
ok(dup_found == KNOWN_DUP,
   f"★ boards.js 的已知裸拼基线应恰好是 {sorted(KNOWN_DUP)}，实得 {sorted(dup_found)}")
print(f"info: boards.js 真渲染出「Lenovo Lenovo」的函数：{sorted(dup_found)}"
      f"（本轮不许改 boards.js；修法是用后端 label / modelPart）")

print(f"label_parity: {PASS} 通过, {len(FAIL)} 失败  （node {NODE}）")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
