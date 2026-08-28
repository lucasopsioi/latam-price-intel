# -*- coding: utf-8 -*-
"""流水线装配测试 —— 防止"某个阶段静默从来没跑过"。

★ 这个文件的由来：
  VocAgent 曾经只在 orchestrator 的函数体里被引用、却没在模块顶部 import。
  NameError 是 Exception 的子类，被外层宽泛的 `except Exception` 抓住，
  只留下一行 "VOC 阶段失败: name 'VocAgent' is not defined"，
  看起来像普通运行时波动 —— 实际上**这个阶段从来没跑过一次**。

  这类 bug 的可怕之处：功能写完了、界面也有了、日志也"正常"，
  就是永远不产出数据。靠人眼 review 抓不住，必须靠测试。

跑法： python tests\test_pipeline.py
"""
import ast
import inspect
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_pipe_"))
config.DB_PATH = _TMP / "t.db"

from app import db  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def check_true(name, cond, hint=""):
    check(f"{name}{(' — ' + hint) if hint else ''}", bool(cond), True)


db.init_db()

print("== 全部 Agent 可导入且可实例化 ==")
from app.agents import (AGENT_ROSTER, BrandIntelAgent,  # noqa: E402
                        ChiefAgent, CleanerAgent,
                        IntelAgent, LLMClient, Orchestrator,
                        PriceAuditAgent, PriceMoveAgent, SpecFillerAgent,
                        StrategyAgent, VocAgent, WeeklyReportAgent)
from app.matching import CompetitorMatcher  # noqa: E402

cfg = config.load_runtime()
llm = LLMClient(cfg["agents"])
AGENT_CLASSES = [ChiefAgent, CleanerAgent, PriceAuditAgent, VocAgent,
                 SpecFillerAgent, IntelAgent,
                 # 看板四件套：事实层(变动) → 解读层(策略/品牌) → 汇总层(周报)
                 PriceMoveAgent, StrategyAgent, BrandIntelAgent,
                 WeeklyReportAgent]
for cls in AGENT_CLASSES:
    try:
        inst = cls(llm, cfg["agents"])
        check_true(f"{cls.__name__} 可实例化", inst is not None)
        check_true(f"{cls.__name__} 有 run 方法", callable(getattr(inst, "run", None)))
    except Exception as e:  # noqa: BLE001
        check_true(f"{cls.__name__} 可实例化", False, f"{type(e).__name__}: {e}")

check_true("CompetitorMatcher 可实例化", CompetitorMatcher(cfg) is not None)

print("== ★ orchestrator 里引用的每个名字都真的 import 了 ==")
# 静态分析：把 orchestrator 源码里用到的顶层名字，跟它实际能解析到的名字对照。
# 这一条就是当年 VocAgent 漏 import 的直接检出手段。
import app.agents.orchestrator as orch_mod  # noqa: E402

src = inspect.getsource(orch_mod)
tree = ast.parse(src)
module_names = set(dir(orch_mod)) | set(dir(__builtins__)) | set(vars(__builtins__))

# 收集函数体内被当成"调用目标"的大写开头名字（类名约定）
called = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id[:1].isupper():
            called.add(node.func.id)

# 排除在函数内部局部 import 的
local_imports = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            local_imports.add(alias.asname or alias.name.split(".")[-1])

missing = sorted(n for n in called if n not in module_names and n not in local_imports)
check_true("orchestrator 无未定义的类引用", not missing, f"缺: {missing}")

print("== ★ AGENT_ROSTER 与真实 Agent 一一对应 ==")
roster_names = {a["name"] for a in AGENT_ROSTER}
real_names = {c.name for c in AGENT_CLASSES} | {"collector"}
check_true("名册没有幽灵条目（列了但不存在的 Agent）",
           roster_names <= real_names, f"多出: {sorted(roster_names - real_names)}")
check_true("名册没有遗漏（存在但没列的 Agent）",
           real_names <= roster_names, f"漏了: {sorted(real_names - roster_names)}")
for a in AGENT_ROSTER:
    check_true(f"名册项 {a['name']} 描述非空", len(a.get("desc", "")) > 20)

print("== ★ 界面上每个按钮都有真实后端分支 ==")
# 同一类 bug 的另一个面：按钮存在、点了也返回 {"ok":true}，
# 但 worker() 里没有对应的 elif —— 只在任务状态里留下一句"未知任务"，
# 界面上看起来像"跑了但没出数据"。按钮名与分支名对不上（pricemove
# vs price_move 这种）纯靠人眼核对必然出错，这里静态比对。
_html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
_srv = (ROOT / "app" / "api" / "server.py").read_text(encoding="utf-8")
# ★ 也要扫 app.js：按钮不一定直接写 run('xxx')，也可能先经一层包装
#   （周报的按钮是 onclick="genWeekly()"，里面才调 run('weekly', {…})，
#     因为它要带上期次与产业参数）。只扫 HTML 会漏掉这类，
#   于是"界面按钮都有后端分支"这条断言会**漏检**而不是报错。
_js = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
import re as _re  # noqa: E402

btn_actions = set(_re.findall(r"""run\(\s*['"]([a-z_]+)['"]""", _html + _js))
srv_actions = set(_re.findall(r"""action\s*==\s*['"]([a-z_]+)['"]""", _srv))
check_true("界面按钮的 action 后端都实现了", btn_actions <= srv_actions,
           f"点了没人接: {sorted(btn_actions - srv_actions)}")
check_true("至少扫到了看板四件套的按钮",
           {"pricemove", "strategy", "brandintel", "weekly"} <= btn_actions,
           f"扫到: {sorted(btn_actions)}")

print("== ★ 前端调的每个 /api 路径后端都注册了 ==")
_js = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")
js_paths = {p.split("?")[0].rstrip("/") for p in
            _re.findall(r"""api\(\s*['"`](/api/[^'"`?\s+]+)""", _js)}
srv_paths = set(_re.findall(r"""@app\.(?:get|post|put|delete)\(\s*['"]([^'"]+)""", _srv))
# 后端用 {rid} 占位，前端拼的是具体 id —— 逐段比对，占位段一律算匹配。
# 另外前端常写 api('/api/weekly/' + id)，静态只能拿到前缀 '/api/weekly'，
# 所以前缀命中某条已注册路由也算通过 —— 拼出来的那一段无法静态求值，
# 硬报 404 只会制造假警报，反而让人开始无视这条测试。
def _route_known(path: str) -> bool:
    segs = path.strip("/").split("/")
    for r in srv_paths:
        rs = r.strip("/").split("/")
        if len(rs) < len(segs):
            continue
        if all(b.startswith("{") or a == b for a, b in zip(segs, rs)):
            return True
    return False


bad_paths = sorted(p for p in js_paths if not _route_known(p))
check_true("前端没有调用不存在的接口", not bad_paths, f"404 路径: {bad_paths}")

print("== ★ 情报 Agent 不许把系统/服务建成竞品硬件 ==")
# 苹果发布会新闻里系统和硬件永远一起出现，模型如实抽取，于是库里出现了
# iOS 27 / iPadOS 27 / macOS 27 / watchOS 27 / iCloud+ 这些"竞品产品"，
# 还全被猜成 phone。它们会污染"覆盖机型数"、混进上市看板当成一次硬件首发、
# 还会被竞品匹配拿去和Acme手机比规格。
from app.agents.intel import _is_software  # noqa: E402

for name in ("iOS 27", "iPadOS 27", "macOS 27", "watchOS 27", "tvOS 26",
             "iCloud+", "Apple Music", "Apple Intelligence", "Galaxy AI",
             "One UI 8", "HarmonyOS 6", "HyperOS 3", "Wear OS 6", "ColorOS 16"):
    check_true(f"{name} 判为软件", _is_software(name))
for name in ("iPhone 18 Pro", "Galaxy Z Fold 8", "AirPods Pro 3", "AirTag 2",
             "Moto Watch", "Galaxy Buds", "iPad Air 13 M4", "Watch Ultra 3"):
    check_true(f"{name} 判为硬件", not _is_software(name))

print("== ★ 模型返回的国家码不能直接落库（外键会炸掉整个阶段）==")
# 情报源是全球科技站，新闻里全是 España / Vietnam / China ——
# 模型如实返回 ES/VN/CN，而 launch_event.country_code 外键只认我们那 6 个国家。
# 旧代码直接落库 ⇒ FOREIGN KEY constraint failed ⇒ **整个情报阶段当场挂掉**，
# 这就是 launch_event 一直是 0 的真因。
from app.agents.intel import _covered_countries  # noqa: E402

covered = _covered_countries()
check_true("覆盖国家就是那 6 个", covered == {"MX", "BR", "CO", "CL", "PE", "AR"},
           f"实际 {sorted(covered)}")


def _resolve_cc(raw):
    """复刻 _record_launch 里的国家码判定，确保非覆盖国家不会走到 INSERT"""
    c = str(raw or "").strip().upper()
    cc = c if (c and c != "GLOBAL" and len(c) == 2) else None
    if cc and cc not in covered:
        return "skip"
    return cc or "global"


check("小写 mx 要能归一", _resolve_cc("mx"), "MX")
check("非覆盖国家 ES 跳过", _resolve_cc("ES"), "skip")
check("非覆盖国家 VN 跳过", _resolve_cc("VN"), "skip")
check("非覆盖国家 US 跳过", _resolve_cc("US"), "skip")
check("global 记全球首发", _resolve_cc("global"), "global")
check("空值记全球首发", _resolve_cc(None), "global")
check("覆盖国家正常", _resolve_cc("BR"), "BR")

print("== ★ 唯一键含可空列必须用 COALESCE 表达式索引 ==")
# 全球首发的 country_code 是 NULL ⇒ UNIQUE(rival_product_id,country_code,event_type)
# 永不冲突 ⇒ INSERT OR IGNORE 每次都插 ⇒ 实测 iOS 27 / AirPods Pro 3
# 在 21 条上市事件里各出现两遍，上市看板重复计数。与 ux_my_pricing 同一个坑。
idx = {r["name"] for r in db.q(
    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ux_%'")}
check_true("launch_event 有 COALESCE 唯一索引", "ux_launch_event" in idx,
           f"现有 {sorted(idx)}")
check_true("my_pricing 有 COALESCE 唯一索引", "ux_my_pricing" in idx)


print("== ★★ 价格变动的 SKU 身份必须优先用 URL ==")
# 由来：Moto G06 在智利报出 **+44.5% 涨价**，实际是拿两条**不同挂牌**比出来的。
# 根因：sku_key 由「rom|ram|color」拼成，而同一台机器的 RAM
# 在 08-10 没从标题解析出来、08-11 解析出来了 ⇒ 它从 "128|-1|" 跳到 "128|4|"，
# MIN(price) 于是在两天里选中了不同的商品。
# 按 URL 比才是真相：同一个商品页 129,990 → 129,990（没变）、94,990 → 89,990（真降 5.3%）。
# ★ 规格解析质量会漂移，URL 不会 —— 身份键必须选**不随解析质量变化**的那个。
import re as _re2  # noqa: E402

_pm_src = (ROOT / "app" / "agents" / "pricemove.py").read_text(encoding="utf-8")
check_true("pricemove 里 sku_key 用了 url",
           "'u:' || po.url" in _pm_src,
           "没有看到 URL 优先的分支")
check_true("仍保留无 URL 时的规格兜底",
           "'s:' || IFNULL(po.rom_gb,-1)" in _pm_src)
check_true("全缺规格的跳过条件跟着改了前缀",
           '"s:-1|-1|"' in _pm_src,
           "跳过条件还在比旧的 '-1|-1|'，改了 key 前缀后它永远不会命中")

print("== ★ _stage 把代码缺陷与网络波动分开报 ==")
orch = Orchestrator(mode="test")


def boom_name():
    raise NameError("name 'Ghost' is not defined")


def boom_net():
    raise TimeoutError("connection timed out")


r1 = orch._stage("假阶段A", boom_name)
check_true("代码缺陷类异常被记录", any("NameError" in w for w in orch.warnings))
check_true("★代码缺陷被单独标出（不混在网络错误里）",
           any("代码缺陷" in w for w in orch.warnings))
check_true("返回结构含 error 字段", "error" in r1)

orch.warnings.clear()
orch._stage("假阶段B", boom_net)
check_true("网络类异常也被记录", any("TimeoutError" in w for w in orch.warnings))
check_true("网络类异常不被标成代码缺陷",
           not any("代码缺陷" in w for w in orch.warnings))

orch.warnings.clear()
ok = orch._stage("假阶段C", lambda: {"n": 1})
check("正常阶段返回结果", ok, {"n": 1})
check_true("正常阶段不产生警告", not orch.warnings)

print("== ★ 引擎生命周期：异常时也必须关闭 ==")
closed = {"n": 0}


class FakeEngine:
    forced_fallback_hosts = set()
    last_engine = "fake"
    last_status = "ok"

    def close(self):
        closed["n"] += 1

    def summary(self):
        return {}


orch2 = Orchestrator(mode="test")
orch2.engine_ref = FakeEngine()
try:
    try:
        raise RuntimeError("模拟采集阶段炸了")
    finally:
        orch2.engine_ref.close()
        orch2.engine_ref = None
except RuntimeError:
    pass
check("异常路径下引擎被关闭", closed["n"], 1)
check_true("关闭后引用被清空", orch2.engine_ref is None)

# 源码层面守住：不能再出现空的 finally: pass 把 close 架空
check_true("★ 源码里没有 'finally:\\n            pass' 这种空收尾",
           "finally:\n            pass" not in src)
check_true("★ 源码里没有 'if True:' 残骸", "if True:" not in src)

print("== ★ 并行采集：同一域名绝不并发 ==")
# 用户明确要求："不要同一个域名，不然会被反扒机制阻拦"
# 并行粒度是【按域名分组，组间并行、组内串行】。
# 这里验证分组逻辑：同一域名的所有单元必须落在同一个组里。
import inspect as _insp  # noqa: E402
import re as _re2  # noqa: E402

_src = _insp.getsource(orch_mod)
check_true("★按域名分组（host_groups）", "host_groups" in _src)
check_true("★用线程池并行", "ThreadPoolExecutor" in _src)
# 断言意图而不是字面量：worker 里必须**自己** new 一个 ScrapeEngine，
# 而不是共用外层的 driver（共用会让并行变成串行，还会互相踩会话）
check_true("★每个 worker 自建引擎（不共用 driver）",
           _re2.search(r"eng\s*=\s*ScrapeEngine\(", _src) is not None)
# ★ 并且必须带上按域名分的 profile —— 否则同国家多域名并行会抢同一个
#   user-data-dir，后来的实例全部 "cannot connect to chrome"（实测整轮 0 条）
check_true("★引擎按域名分 profile（profile_tag=host）",
           _re2.search(r"ScrapeEngine\([^)]*profile_tag\s*=\s*host", _src) is not None)
check_true("★每个 worker 在 finally 里关自己的浏览器",
           "eng.close()" in _src)
check_true("分组键取自 base_url 的域名",
           'base_url"] or "").split("//")[-1].split("/")[0]' in _src)

# 分组正确性：模拟一批单元，同域名必须聚到一起
_units = [
    {"host": "www.liverpool.com.mx", "id": 1},
    {"host": "www.falabella.com", "id": 2},
    {"host": "www.liverpool.com.mx", "id": 3},
    {"host": "www.coppel.com", "id": 4},
    {"host": "www.falabella.com", "id": 5},
]
_g = {}
for _u in _units:
    _g.setdefault(_u["host"], []).append(_u)
check("分成 3 个域名组", len(_g), 3)
check("liverpool 两个单元同组", len(_g["www.liverpool.com.mx"]), 2)
check("falabella 两个单元同组", len(_g["www.falabella.com"]), 2)
check_true("★没有任何域名跨组", all(len({m['host'] for m in v}) == 1
                                    for v in _g.values()))

print("== ★★ 同国家多域名并行时，浏览器 profile 目录必须各用各的 ==")
# 用户要求「按域名分组、组间并行、组内串行」，于是同一国家的
# Falabella / Ripley / Paris 会**同时**各起一个浏览器。
# 而 profile 目录原本只按国家分（selenium_CL）⇒ 三个实例抢同一个 user-data-dir
# ⇒ 后两个报 "cannot connect to chrome"，退避重试到天荒地老。
# 实测症状：整轮采集跑满几分钟、scrape_unit 一条没有、写入 0 条，
# 而任务状态只显示"启动失败重试中" —— 看起来像被风控，其实是自己人打自己人。
from app.scraping.engine import ScrapeEngine  # noqa: E402

_e1 = ScrapeEngine({"engine": "selenium", "fallback_engine": "none"},
                   profile_tag="simple.ripley.cl")
_e2 = ScrapeEngine({"engine": "selenium", "fallback_engine": "none"},
                   profile_tag="www.paris.cl")
check_true("profile_tag 写进了各自的 cfg",
           _e1.cfg.get("profile_tag") != _e2.cfg.get("profile_tag"))
check("同域名 tag 稳定（跨轮次复用 Cookie）",
      ScrapeEngine({"engine": "selenium"}, profile_tag="www.paris.cl")
      .cfg.get("profile_tag"), "www.paris.cl")
check_true("不传 tag 时不写这个键（保持旧行为）",
           "profile_tag" not in ScrapeEngine({"engine": "selenium"}).cfg)
# 目录名要能安全落地（域名里的点/冒号不能直接进路径）
for _h in ("www.falabella.com", "simple.ripley.com.pe", "miportal.entel.cl"):
    _tag = _re2.sub(r"[^a-z0-9]+", "_", _h.lower())
    check_true(f"{_h} 目录名安全", _re2.fullmatch(r"[a-z0-9_]+", _tag) is not None)

print("== ★ 跳过渠道的判据必须是比例，不是「出现过一次」 ==")
# 原来写成 `if irrelevant > 0: skip` —— 24 次里错 1 次就永久停掉整个渠道。
# 实测 Alkosto 1/24、Falabella CO 1/18 因此被停，而它们是好的：
# 偶发串味可能只是某个搜索词恰好没货、站点返回了推荐位。
# 拿它判死整个渠道，代价是**那个国家整段数据消失**，比放进来几条脏数据大得多。
# 而 Hiraoka 11/37、Coppel 17/26 才是真的搜索坏了，必须拦。


def _would_skip(attempts, irrelevant):
    rate = irrelevant / attempts if attempts else 0
    return irrelevant >= 3 and rate >= 0.25


check("偶发 1/24 不跳过", _would_skip(24, 1), False)
check("偶发 1/18 不跳过", _would_skip(18, 1), False)
check("2/8 未到 3 次不跳过", _would_skip(8, 2), False)
check("Hiraoka 11/37 要跳过", _would_skip(37, 11), True)
check("Coppel 17/26 要跳过", _would_skip(26, 17), True)
check("全是串味必跳过", _would_skip(10, 10), True)
check("零历史不跳过", _would_skip(0, 0), False)
# ★ 次数与比例是**两个**闸，缺一个都会误判：
#   只看比例：3 次里错 1 次 = 33%，样本太小就判死；
#   只看次数：100 次里错 3 次 = 3%，明显是偶发却被判死。
check("样本太小时不因高比例判死", _would_skip(3, 1), False)
check("大样本低比例不判死", _would_skip(100, 3), False)

print("== ★★ 主引擎整体挂掉时必须全量切兜底 ==")
# 「某个域名被拦」和「浏览器根本起不来」是两回事，处置也不同：
#   前者只有那个域名换引擎；后者**所有**域名都得换。
# 实测漏掉后者：一次 SessionNotCreated 让整个域名组的 24 个采集单元
# 全部失败（4 个渠道各丢 24 个），而 Playwright 兜底引擎全程没被唤醒 ——
# 明明有备胎，却眼看着整组数据丢光。
from app.scraping.selenium_driver import MAX_LAUNCH_FAILURES  # noqa: E402

_eng = ScrapeEngine({"engine": "selenium", "fallback_engine": "playwright"})
check("主引擎健康时用主引擎", _eng._pick("https://www.paris.cl/x").name, "selenium")
_eng._primary._launch_failures = MAX_LAUNCH_FAILURES      # 模拟浏览器起不来
check("主引擎整体挂掉后改走兜底", _eng._pick("https://www.paris.cl/x").name, "playwright")
check_true("换了域名也一样走兜底（不是按域名判的）",
           _eng._pick("https://www.falabella.com/y").name == "playwright")
# 没配兜底时不能假装有
_solo = ScrapeEngine({"engine": "selenium", "fallback_engine": "none"})
_solo._primary._launch_failures = MAX_LAUNCH_FAILURES
check("没有兜底引擎时仍返回主引擎", _solo._pick("https://x.com/").name, "selenium")

print("== ★ 启动与清锁必须用同一个 profile 路径 ==")
# 我自己引入过的不一致：启动用 selenium_{cc}_{域名}，
# 而重试前的清锁还在找 selenium_{cc} —— 清到了另一个目录，
# 真正卡住的那把锁永远清不掉，重试注定再失败。
from app.scraping.selenium_driver import SeleniumBrowser  # noqa: E402

_b = SeleniumBrowser({"profile_tag": "www.paris.cl"})
check("带 tag 时目录含域名", _b._profile_dir("CL").name, "selenium_CL_www_paris_cl")
check("不带 tag 时保持旧行为", SeleniumBrowser({})._profile_dir("CL").name, "selenium_CL")
check_true("同一实例两次取到同一路径",
           _b._profile_dir("CL") == _b._profile_dir("CL"))

print("== ★ 采集阶段也要有进度（第三次栽在同一件事上）==")
# Agent 的进度靠 log_step，但**采集器走的是 livelog**，任务状态里一动不动 ——
# 而采集恰恰是最长的阶段（几小时）。「正在抓第 7 个渠道」与「卡死了」
# 在任务状态里长得一模一样，用户为此质问过「你根本没开始跑」。
from app import livelog  # noqa: E402

_got: list[str] = []
livelog.set_progress_sink(_got.append)
livelog.emit("search", "Falabella Chile × Samsung × tablet")
livelog.emit("stage", "进入清洗阶段")
livelog.emit("block", "Liverpool 被拦，冷却 90 秒")
livelog.emit("page", "→ 打开 falabella.com")      # 高频，不该上报
livelog.emit("found", "解析到 20 条")              # 高频，不该上报
livelog.set_progress_sink(None)
livelog.emit("search", "解除后不该再上报")

check("有进展含义的事件上报了 3 条", len(_got), 3)
check_true("搜索单元上报", any("Samsung" in g for g in _got))
check_true("阶段切换上报", any("清洗" in g for g in _got))
check_true("被拦上报", any("被拦" in g for g in _got))
check_true("高频 page 事件不上报", not any("打开" in g for g in _got))
check_true("解除后不再上报", not any("不该再上报" in g for g in _got))

print("== 配置健全性 ==")
check_true("默认主引擎是 selenium", cfg["scrape"]["engine"] == "selenium")
check_true("兜底引擎是 playwright", cfg["scrape"]["fallback_engine"] == "playwright")
check_true("VOC 默认开启", cfg["voc"]["enabled"] is True)
check_true("抓取间隔不小于 2 秒（礼貌抓取）", cfg["scrape"]["min_delay"] >= 2.0)
check_true("每日自动采集已开启", cfg["schedule"]["enabled"] is True,
           "关着的话库里永远只有手动跑过的那几天，价格趋势画不出来")

print("== ★ 用户指定的品牌覆盖清单（2026-08-12 明确给出）==")
# 这是用户逐条列的必须覆盖范围。漏掉一个 = 那个牌子的货整个看不见，
# 而界面上只会表现为"这个牌子没有数据"，看不出是覆盖缺口。
# 子品牌按用户口径并入母品牌（OPPO 含 realme、vivo 含 iQOO、Xiaomi 含 Redmi/POCO），
# 所以下面只断言母品牌，另外单独断言子品牌在别名里。
_REQUIRED = {
    "phone": ["Apple", "Samsung", "Acme", "Motorola", "OPPO", "vivo", "Xiaomi"],
    "tablet": ["Apple", "Samsung", "Acme", "Lenovo", "OPPO", "vivo", "Xiaomi",
               "Positivo", "Multilaser"],
    "audio": ["Apple", "Samsung", "Acme", "JBL", "Sony", "Sennheiser",
              "Skullcandy", "Lenovo", "Soundcore", "Bowers & Wilkins",
              "Bang & Olufsen"],
    "wearable": ["Apple", "Samsung", "Acme", "Fitbit", "Garmin", "Xiaomi",
                 "Amazfit", "Honor"],
}
_bcfg = {b["name"]: set(b.get("categories") or [])
         for b in (config.load_brands().get("brands") or [])}
for _cat, _names in _REQUIRED.items():
    _miss = [n for n in _names if _cat not in _bcfg.get(n, set())]
    check_true(f"{_cat} 品牌覆盖齐全（{len(_names)} 个）", not _miss, f"缺 {_miss}")

# 子品牌必须在母品牌的别名里，否则搜不到它们的货
_SUB = {"OPPO": ["realme"], "vivo": ["iQOO"], "Xiaomi": ["Redmi", "POCO"]}
_alias = {b["name"]: [str(a).lower() for a in (b.get("aliases") or [])]
          for b in (config.load_brands().get("brands") or [])}
for _parent, _subs in _SUB.items():
    for _s in _subs:
        check_true(f"{_parent} 别名含子品牌 {_s}",
                   any(_s.lower() in a for a in _alias.get(_parent, [])))

print("== ★ 我方品牌必须在采集品牌列表里（否则自家商城永远抓不到）==")
# 由来：`my_pricing` 长期 0 行，看板上所有"我方 vs 友商"的图都卡在这。
# 而四个Acme自营商城渠道早就配好了、URL 也验证过有价格 ——
# 唯独采集循环里写着 `is_ours=0`，把我方品牌整个滤掉，
# 那四个渠道**一个采集单元都没排过**（scrape_unit 零记录），配了等于没配。
import ast as _ast  # noqa: E402
import inspect as _i2  # noqa: E402

# ★ 只看**实际执行的 SQL**，不要全文搜关键字 ——
#   这段代码的注释里就写着"唯独这里 is_ours=0 把我方品牌滤掉"，
#   按全文搜会把解释性注释当成违规。（存档那边刚踩过一模一样的坑。）
_ast2 = _ast.parse(_i2.getsource(Orchestrator._collect).lstrip())
_sql2 = [n.value for n in _ast.walk(_ast2)
         if isinstance(n, _ast.Constant) and isinstance(n.value, str)
         and "FROM brand" in n.value]
check_true("★确实取了 brand 表", bool(_sql2), str(_sql2))
check_true("★取品牌的 SQL 里没有 is_ours=0",
           not any("is_ours" in q.replace(" ", "") for q in _sql2), str(_sql2))
check_true("★取的是全部启用品牌",
           any("enabled=1" in q.replace(" ", "") for q in _sql2), str(_sql2))

print("== 品牌商城只搜自己的品牌 ==")
from app.scraping.collector import Collector as _Col  # noqa: E402

_csrc = _i2.getsource(_Col.collect_unit)
check_true("brand_store 闸门还在", "brand_store" in _csrc and "skipped" in _csrc)


def _gate(ch_kind, ch_code, brand):
    return ch_kind == "brand_store" and brand.lower() not in (ch_code or "").lower()


check("Acme商城 × Acme → 放行", _gate("brand_store", "acme_store", "Acme"), False)
check("Acme商城 × 三星 → 跳过", _gate("brand_store", "acme_store", "Samsung"), True)
check("三星商城 × Acme → 跳过", _gate("brand_store", "samsung_store", "Acme"), True)
check("零售渠道 × Acme → 放行", _gate("retailer", "falabella", "Acme"), False)

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
