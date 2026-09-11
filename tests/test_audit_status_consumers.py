# -*- coding: utf-8 -*-
"""源码级断言：分析消费方不许再用 `audit_status <> 'rejected'`（pending 当通过）。

★ 走 ast，不走字符串搜索：注释不进语法树（assertions-that-verify-nothing 那一课 ——
  字符串搜全文会匹配到自己写的注释，断言恒真）。SQL 文本是 Python 字符串常量，在树里。

例外只认一种：SQL 字符串里带 `-- audit-self` 标记 = 审计自身的取样口径
（建基线 / 地板样本含待审行）。标记写在字符串**里**，所以它也在语法树里，能被这里看见。

过渡期允许名单（ALLOW）：S5 ⑤~⑧ 与 trends.py（另一路负责）尚未切换的站点。
  · 名单外出现任何一处 → 红
  · 名单里的计数与实际不符（有人切了却没删名单）→ 红，名单只许缩
  · 环境变量 AUDIT_CONSUMER_STRICT=1 → 忽略名单，全仓必须为 0（S5 全部落地后跑这个）

同文件顺带守 S1：不许有不带 obs_date 的 `PriceAuditAgent(...).run(` 裸调（审"今天"= 跨午夜
批次一条不审）。已知三处在 RUN_ALLOW 里，等 S1(c) 落地后删掉。

跑法： python tests\\test_audit_status_consumers.py
"""
import ast
import os
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=SyntaxWarning)   # 别人文件里 "\p" 之类的旧写法，与本测试无关

ROOT = Path(__file__).resolve().parent.parent
STRICT = os.environ.get("AUDIT_CONSUMER_STRICT") == "1"

PAT = re.compile(r"""(?:<>|!=)\s*['"]rejected['"]""")
EXEMPT_MARK = "audit-self"

# 过渡期允许名单：相对路径 → 尚未切换站点数的**上限**（S5 步骤号）。
# 用上限不用精确值：trends.py 另一路正在改、计数随时会变；名单项一旦实际为 0 必须删掉（下面有守卫）。
ALLOW = {
    "app/trends.py": 3,                  # S5 ①  另一路负责（product_series / 篮子指数 / suggest_watch）
    # S5 ④ app/api/server.py（/api/trend/candidates 的品牌下拉）已切 = 'accepted'（2026-09-04），从名单删除
    "app/agents/weekly.py": 6,           # S5 ⑤  周报分析查询
    "app/agents/strategy.py": 7,         # S5 ⑥
    "app/agents/brandintel.py": 1,       # S5 ⑥
    "app/matching/matcher.py": 1,        # S5 ⑦  候选取样（旧注释说 = accepted 恒为 0，S1 后不成立）
    "app/agents/pricemove.py": 2,        # S5 ⑧  变动检测（须先确认审计在它之前按 obs_dates 循环到 0）
    "app/agents/orchestrator.py": 1,     # S5    VOC 选目标，可不切
    "app/dashboard.py": 1,               # trackable_products —— 另一路在改，本轮不碰
}
RUN_ALLOW: dict = {}                     # S1(c) 已落地（2026-09-04）：三处都改成 run_all(obs_date=d)，
                                         # 名单清空 —— 再出现裸调 .run() 就红

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def _docstring_ids(tree: ast.AST) -> set[int]:
    """docstring 节点的 id 集合：docstring 描述规则本身（"不许再用 <> 'rejected'"），不是 SQL。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def scan_neq_rejected(src: str) -> list[tuple[int, str]]:
    """返回 [(行号, 片段)]：字符串常量里出现 <> 'rejected' 且该常量不带 audit-self 标记。
    docstring 不算（它们不是会被执行的 SQL）。"""
    hits = []
    tree = ast.parse(src)
    docs = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            s = node.value
            n = len(PAT.findall(s))
            if not n or EXEMPT_MARK in s:
                continue
            frag = PAT.search(s)
            line = node.lineno + s[:frag.start()].count("\n")
            for _ in range(n):
                hits.append((line, s.strip().splitlines()[0][:70]))
    return hits


def _is_price_audit_ctor(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
    return name == "PriceAuditAgent"


def scan_bare_run(src: str) -> list[int]:
    """PriceAuditAgent(...).run(...) 没传 obs_date（无位置参数、无 obs_date 关键字）的行号。"""
    out = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run" and _is_price_audit_ctor(node.func.value)):
            has_date = bool(node.args) or any(k.arg == "obs_date" for k in node.keywords)
            if not has_date:
                out.append(node.lineno)
    return out


# ---------------------------------------------------------------- 先证明扫描器本身会红
print("== 扫描器自检（把违规写法加回去一次，确认会红）==")
bad = "x = db.q('''SELECT 1 FROM price_obs WHERE audit_status <> 'rejected' AND a=1''')"
check("裸 <> 'rejected' 被抓到", len(scan_neq_rejected(bad)), 1)
check("<>'rejected'（无空格）被抓到", len(scan_neq_rejected("s = \"a <>'rejected'\"")), 1)
check('!= "rejected" 被抓到', len(scan_neq_rejected('s = "a != \\"rejected\\""')), 1)
check("注释里的写法不算（这就是为什么要走 ast）",
      len(scan_neq_rejected("# audit_status <> 'rejected'\nx = 1")), 0)
check("docstring 里描述规则不算",
      len(scan_neq_rejected("def f():\n    '''不许再用 <> \\'rejected\\''''\n    return 1")), 0)
check("但 docstring 之外的普通字符串照抓",
      len(scan_neq_rejected("def f():\n    '''doc'''\n    return \"a <> 'rejected'\"")), 1)
check("带 audit-self 标记的 SQL 放行",
      len(scan_neq_rejected("s = '''a <> 'rejected' -- audit-self: 建基线'''")), 0)
check("f-string 里的 SQL 也扫", len(scan_neq_rejected('s = f"""WHERE x <> \'rejected\'{po_f}"""')), 1)
check("同一常量两处算两处",
      len(scan_neq_rejected("s = '''a <> 'rejected' OR b <> 'rejected' '''")), 2)
check("裸 .run() 被抓到", scan_bare_run("PriceAuditAgent(llm, cfg).run()"), [1])
check("传了 obs_date 的放行", scan_bare_run("PriceAuditAgent(llm, cfg).run(obs_date=d)"), [])
check("位置参数也算传了日期", scan_bare_run("PriceAuditAgent(llm, cfg).run(d)"), [])
check("run_all 不在此规则内", scan_bare_run("PriceAuditAgent(llm, cfg).run_all(obs_date=d)"), [])
check("别的 Agent 的 .run() 不误报", scan_bare_run("CleanerAgent(llm, cfg).run()"), [])

# ---------------------------------------------------------------- 全仓扫描
print(f"== 全仓扫描 app/**/*.py（{'严格模式' if STRICT else '过渡期名单模式'}）==")
found: dict[str, list[tuple[int, str]]] = {}
for p in sorted(ROOT.glob("app/**/*.py")):
    rel = p.relative_to(ROOT).as_posix()
    hits = scan_neq_rejected(p.read_text(encoding="utf-8"))
    if hits:
        found[rel] = hits

for rel, hits in found.items():
    allowed = 0 if STRICT else ALLOW.get(rel, 0)
    if len(hits) > allowed:
        print(f"  {rel}: {len(hits)} 处（名单上限 {allowed}）")
        for line, frag in hits:
            print(f"      L{line}: {frag}")
    check(f"{rel} 的 <> 'rejected' 站点数 ≤ 名单上限", len(hits) <= allowed, True)
if not STRICT:
    for rel, n in ALLOW.items():
        if rel not in found:
            check(f"名单项 {rel} 已经没有站点了 —— 请把它从 ALLOW 删掉", 0, n)

for must_clean in ("app/boards.py", "app/notify.py", "app/api/server.py"):
    check(f"{must_clean} 已切 = 'accepted'", len(found.get(must_clean, [])), 0)
check("app/dashboard.py 只剩 trackable_products 那一处", len(found.get("app/dashboard.py", [])), 1)
pa_hits = scan_neq_rejected((ROOT / "app/agents/price_audit.py").read_text(encoding="utf-8"))
check("price_audit.py 的两处 <> 'rejected' 都带了 audit-self 标记", pa_hits, [])
check("price_audit.py 确实还有带标记的审计自身口径（标记不是白加的）",
      (ROOT / "app/agents/price_audit.py").read_text(encoding="utf-8").count(EXEMPT_MARK) >= 2, True)

print("== S1：PriceAuditAgent(...).run() 必须传 obs_date ==")
bare: dict[str, list[int]] = {}
for p in sorted(list(ROOT.glob("app/**/*.py")) + list(ROOT.glob("tools/*.py"))):
    rel = p.relative_to(ROOT).as_posix()
    lines = scan_bare_run(p.read_text(encoding="utf-8"))
    if lines:
        bare[rel] = lines
for rel, lines in bare.items():
    allowed = 0 if STRICT else RUN_ALLOW.get(rel, 0)
    if len(lines) > allowed:
        print(f"  {rel}: 裸调 .run() 在 L{lines}（名单上限 {allowed}）")
    check(f"{rel} 裸调 .run() 数 ≤ 名单上限", len(lines) <= allowed, True)
if not STRICT:
    for rel, n in RUN_ALLOW.items():
        if rel not in bare:
            check(f"RUN_ALLOW 项 {rel} 已修好 —— 请从名单删掉", 0, n)
check("tools/audit_backlog.py 自己不裸调 run()", bare.get("tools/audit_backlog.py", []), [])

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
