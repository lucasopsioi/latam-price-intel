# -*- coding: utf-8 -*-
"""全局上下文栏与导航分组的回归测试。

守的性质都对应一次真实的静默失效 —— 这一类 bug 的共同点是
**界面照常渲染、控制台干净、只是行为不对**，只能靠断言复现。
"""
import ast
import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
APP = (ROOT / "app/web/app.js").read_text(encoding="utf-8")
BOARDS = (ROOT / "app/web/boards.js").read_text(encoding="utf-8")
SERVER = (ROOT / "app/api/server.py").read_text(encoding="utf-8")

FAIL, PASS = [], [0]


def strip_js_comments(src: str) -> str:
    """去掉 JS 注释再做顺序/存在性断言。

    ★★ 本项目第 4 次栽在同一件事上：断言去搜全文，结果匹配到**自己写的注释**。
      这一次是 go() 里的注释 `// 必须在 loader() 之前套用全局上下文`，
      让 `body.index("loader()")` 找到注释里的那一处，
      于是"applyCtx 在 loader 之前"这条断言恒假 —— 明明代码是对的。
      前三次分别是 archive 的 DELETE、brand 的 is_ours=0、单例的 SO_REUSEADDR。
      规律很稳定：**越重要的性质，旁边注释写得越认真，越容易被误匹配。**
      Python 侧用 ast 解决；JS 没有现成 ast，就先剥注释再比。
    """
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":                       # 跳过字符串，别把里面的 // 当注释
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


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ───────────────────── 1. 元素 ID 不许重复 ─────────────────────
# 这个坑在本项目吃过两次：
#   ① 新建「价格曲线」时 #trend-chart 撞上旧的「价格趋势」，
#      新页面一直往旧页面那个隐藏元素里画图，看起来像图没出来。
#   ② VOC 页整行筛选器被复制了两份，第二份的 #vr-kind/#vr-category/#vr-brand
#      全是死控件 —— getElementById 只返回第一个，点它的「查询」用的是第一张卡的值。
ids = re.findall(r'\bid="([^"]+)"', HTML)
dup = {k: v for k, v in collections.Counter(ids).items() if v > 1}
ok(not dup, f"index.html 里存在重复 ID：{dup}。"
            f"getElementById 只返回第一个，后面的全是死控件，且**不报任何错**")


# ───────────────── 2. 全局上下文栏的选择器必须真实存在 ─────────────────
# CTX_TARGETS 里写错一个选择器名字，那一页就静默不同步 —— 没有报错。
m = re.search(r'const CTX_TARGETS = \{(.*?)\n\};', APP, re.S)
ok(m is not None, "app.js 里应有 CTX_TARGETS 映射表")
if m:
    sels = re.findall(r"'#([\w-]+)'", m.group(1))
    ok(len(sels) >= 20, f"映射表至少该覆盖 20 个页面级选择器，实得 {len(sels)}")
    missing = [s for s in sels if f'id="{s}"' not in HTML]
    ok(not missing,
       f"CTX_TARGETS 指向了不存在的元素：{missing} —— "
       f"写错名字不会报错，只会让那一页静默脱离全局筛选")

    # 反向：页面上出现的国家/产业/品牌选择器，都该被全局栏接管
    # ★ 只看 <select> —— 第一版的正则把 <table id="intel-by-country"> 也算成了
    #   筛选器，报了两个不存在的问题
    page_sels = set(re.findall(
        r'<select[^>]*id="([\w-]*(?:country|category|brand|days))"', HTML))
    page_sels -= {"ctx-country", "ctx-category", "ctx-brand", "ctx-days"}
    orphan = sorted(page_sels - set(sels))
    ok(not orphan,
       f"这些筛选器没被全局上下文接管，会造成"
       f"「改了全局它不动」的不一致：{orphan}")


# ───────────────── 3. 套用顺序：必须在 loader 之前 ─────────────────
go_src = re.search(r'function go\(page\) \{(.*?)\n\}', APP, re.S)
ok(go_src is not None, "app.js 应有 go(page)")
if go_src:
    body = strip_js_comments(go_src.group(1))
    ok("applyCtx()" in body, "go() 必须套用全局上下文")
    ok(body.index("applyCtx()") < body.index("loader()"),
       "★ applyCtx 必须在 loader() **之前** —— 页面 loader 是直接读自己"
       "选择器的值去请求的，套晚了这一次还是用旧值，要点两次才对")
    ok("CUR_PAGE = page" in body, "要记录当前页，否则重新加载不知道刷哪一页")

# 重建 options 的 loader 必须在重建后补一次
ok("applyCtx" in strip_js_comments(BOARDS),
   "★ loadTrendBoard 会重建三个下拉的 options，把刚写进去的值冲掉。"
   "实测症状：全局选了 Samsung，曲线页却画 Amazfit（列表第一个）且不报错。"
   "重建 options 之后必须补一次 applyCtx")
lt = re.search(r'async function loadTrendBoard\(\) \{(.*?)\n\}', BOARDS, re.S)
if lt:
    b = strip_js_comments(lt.group(1))
    ok("applyCtx" in b and b.index("fill('#tc-brand'") < b.index("applyCtx"),
       "applyCtx 要在 fill 之后调用，否则一样会被冲掉")


# ───────────────── 4. 「本页支持哪些维度」不能看全局 DOM ─────────────────
# 所有 .page 是同时存在于 DOM 里的（只靠 class 切换显示），
# 所以用 $('#pf-country') 判断"当前页有没有国家筛选"永远为真。
apply_src = re.search(r'function applyCtx\(\) \{(.*?)\n\}', APP, re.S)
ok(apply_src is not None, "app.js 应有 applyCtx")
if apply_src:
    b = strip_js_comments(apply_src.group(1))
    ok("$('#page-' + CUR_PAGE)" in b,
       "★ 判断本页支持哪些维度必须限定在当前页容器内 —— "
       "所有页面的 DOM 同时存在，全局查找会统计到别的页面的选择器，"
       "于是在情报流页上提示「本页不支持按产业筛选」而它明明支持")
    ok("$(s, page)" in b, "统计要用当前页作为查找根")


# ───────────────── 5. 选项集合不一致时的降级 ─────────────────
set_src = re.search(r'function setSel\((.*?)\n\}', APP, re.S)
ok(set_src is not None, "app.js 应有 setSel")
if set_src:
    b = strip_js_comments(set_src.group(1))
    ok("includes(val)" in b,
       "写值前要确认该选项存在 —— 赋一个不存在的值浏览器会**静默忽略**")
    ok("Math.abs" in b,
       "★ 各页时间窗选项不一致（1/7/30/365 vs 30/90/180/365），"
       "找不到精确值要退到最接近的，否则用户以为切了其实没切")


# ───────────────── 6. 导航分组：一个页面都不能丢 ─────────────────
pages = re.findall(r'data-page="([a-z]+)"', HTML)
ok(len(pages) == len(set(pages)), f"导航项重复：{[k for k, v in collections.Counter(pages).items() if v > 1]}")
ok(len(pages) == 19, f"重排导航不该丢页面，应为 19 个，实得 {len(pages)}")

# 每个导航项都要有对应的 .page 容器和 PAGES 路由
for pg in pages:
    ok(f'id="page-{pg}"' in HTML, f"导航项 {pg} 没有对应的页面容器")
routes = set(re.findall(r'^\s{2}(\w+):\s*\[', APP, re.M))
missing_route = sorted(set(pages) - routes)
ok(not missing_route, f"这些导航项没有路由，点了没反应：{missing_route}")

groups = re.findall(r'class="nav-title">([^<]+)<', HTML)
ok(len(groups) == 5, f"导航应分 5 组（今天/市场/我的位置/对外/系统），实得 {len(groups)}：{groups}")


# ───────────────── 7. 静态资源必须带版本号 ─────────────────
# 实测：改完 boards.js、重启服务、刷新页面，服务端返回的已经是新文件，
# 浏览器仍跑旧的那份 —— 响应只有 etag/last-modified 没有 Cache-Control，
# 浏览器按启发式缓存直接复用。新功能静默失效且无报错。
tree = ast.parse(SERVER)
fn = next((n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "_stamp_assets"), None)
ok(fn is not None, "server.py 应有 _stamp_assets 给静态资源挂版本号")
if fn:
    src = ast.get_source_segment(SERVER, fn) or ""
    ok("md5" in src or "sha" in src,
       "版本号应基于**文件内容**哈希：内容没变则 URL 不变（缓存照常命中），"
       "内容一变 URL 必变（强制重新拉取）")
idx_fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "index"), None)
if idx_fn:
    src = ast.get_source_segment(SERVER, idx_fn) or ""
    ok("_stamp_assets" in src, "首页必须走版本号注入")
    ok("no-cache" in src,
       "index.html 自身不能被缓存，否则里面的版本号永远更新不了")




# ─────────── 8. 渠道禁用必须写在配置里，不能只改库 ───────────
# ★★ 实测事故：我在库里把 6 个 MercadoLibre 渠道 enabled=0，
#   后来因为别的事调了一次 db.init_db()，它们**全部变回 enabled=1** 并被真的采集了
#   （8 个采集单元，全 failed）。根因是 db.seed() 的
#   `ON CONFLICT(code,country_code) DO UPDATE SET ... enabled=excluded.enabled`
#   —— 每次 init 都用配置文件覆盖库里的值。
#   这条同样适用于**界面上的「禁用渠道」按钮**：它改的是库，下次 init 就复活。
import yaml as _yaml  # noqa: E402

_ch = _yaml.safe_load((ROOT / "config/channels.yaml").read_text(encoding="utf-8"))
_meli = [c for c in (_ch or {}).get("channels", []) if c.get("code") == "meli"]
ok(len(_meli) >= 6, f"配置里应有 6 个 MercadoLibre 渠道，实得 {len(_meli)}")
ok(all(int(c.get("enabled", 1)) == 0 for c in _meli),
   "★ MercadoLibre 必须在**配置文件**里 enabled: 0 —— "
   "robots 点名封禁 + 用户明示不做；只在库里关会被下次 init_db() 冲掉")

_db_src = (ROOT / "app/db.py").read_text(encoding="utf-8")
ok("enabled=excluded.enabled" in _db_src,
   "确认 seed 确实会覆盖 enabled（这条断言在于说明为什么必须改配置）")


print(f"ui-context: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
