# -*- coding: utf-8 -*-
"""情报抽取的回归测试（2026-09-04 用户截图：「小米在 IFA 2026 发布 Galaxy Z Fold 8 竞品」）。

三个根因（都不报错、页面看不出异常，只能靠断言守）：

  ① **喂给模型的正文被切到 120 字**：「The Xiaomi 18 Fold was showcased…」在第 128 字，
     模型根本看不到机型名，只能照搬三星站的标题框架 —— 这是代码缺陷，不是模型问题。
  ② **全球科技站的动态从不挂品牌**，模型抽出的 brand 回写时被丢弃。
     实测重要度≥3 的动态 91% 无品牌（493/542）。和品类那次是同一个病根：
     属性来自**检索词**而不是内容本身。
  ③ 摘要 prompt 没要求「主语=做事的品牌、宾语=它自己的机型、参照物进括号」。

跑法： python tests\test_intel.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


src = (ROOT / "app/agents/intel.py").read_text(encoding="utf-8")

from app import db  # noqa: E402
from app.agents.brandintel import _alias_map  # noqa: E402
from app.agents.intel import _BODY_CHARS, _prompt_line, _verified_brand  # noqa: E402

# 截图那条的真实标题与 RSS 正文（机型名在第 128 字）
TITLE = "Xiaomi unveils its Galaxy Z Fold 8 rival at IFA 2026"
BODY = ("More than a month after Samsung announced the Galaxy Z Fold 8, Xiaomi has "
        "unveiled its first passport-style foldable phone. The Xiaomi 18 Fold was "
        "showcased at the ongoing IFA 2026 event in Berlin, Germany, featuring more "
        "rounded corners and an additional rear camera. The device sports a 5.38-inch "
        "cover display and a 7.58-inch inner [&hellip;]")

# ─────────── ① 正文必须喂满 ───────────
ok(BODY.find("Xiaomi 18 Fold") > 120,
   "（测试前提）机型名确实在第 120 字之后，否则这条测试守不住任何东西")
ok(_BODY_CHARS >= 500,
   f"★ 喂给模型的正文至少 500 字（RSS 摘录一般 300~500），实得 {_BODY_CHARS}")
ok("'summary', '')[:120]" not in src and "body[:_BODY_CHARS]" in src,
   "★ 120 字截断不许回来 —— 它切掉的正是机型名；正文长度只能走 _BODY_CHARS")
line = _prompt_line(0, {"source": "SamMobile", "title": TITLE, "summary": BODY})
ok("Xiaomi 18 Fold" in line,
   "★ prompt 行里必须看得到「Xiaomi 18 Fold」，模型没看到就只能照搬标题")
ok(_prompt_line(3, {"source": "X", "title": "t", "summary": ""}) == "3. [X] t",
   "正文为空时不要拖一个空的 « — »")

# ─────────── 序号核对：模型返回的内容必须属于它声称的那条 ───────────
# 2026-09-04 实测：一批 15 条里模型把 #9168（Xiaomi beats Apple to market with
# Xiaomi 18 Fold reveal）的序号配上了另一条「苹果发布 Apple Watch Series 12」的内容。
# 品牌核对拦不住 —— 原文里恰好有 "Apple's event"。品牌对、内容错，人眼看不出来。
ok("(k=ab12c)" in _prompt_line(0, {"source": "S", "title": "t", "summary": ""}, "ab12c"),
   "★ 每条要带核对码，模型抄回后用来核对序号没错位")
ok('"k":"' in src and "pick_by_key(" in src and "rejected" in src,
   "★ 核对码抄错的整条拒收 —— 这是唯一能拦住序号错位的手段；"
   "必须走 llm.pick_by_key 这一个实现，不许各写各的")
ok('j = int(item.get("idx"))' not in src,
   "★ 定位只认核对码，不用模型写的序号（1-based / 丢条目后重编号都实测见过，"
   "见 knowledge/lessons/llm-batch-result-alignment）")
_llm = (ROOT / "app/agents/llm.py").read_text(encoding="utf-8")
ok("j in seen" in _llm, "同一条目回两次只收第一条（实现在 llm.pick_by_key）")
ok("抽取序号核对" in src, "拒收要留痕（log_step），不能静默丢")

# ─────────── ③ 摘要纪律写进 prompt ───────────
ok("主语必须是" in src and "参照物" in src,
   "★ prompt 要明说：主语=做事的公司，宾语=它自己的产品，参照物只能进括号")
ok("禁止写成「小米发布 Galaxy Z Fold 8 竞品」" in src,
   "★ 反例要原样写进 prompt —— 抽象规则挡不住三星站的标题框架")
ok("从**正文**里找" in src,
   "★ product 要从正文抽，标题常常不写机型名")
ok("brand 填做事的那家公司" in src,
   "★ brand 必须是做事的那家，不是被拿来对比的那家")

# ─────────── ② 品牌回写：模型报的品牌必须原文可核对 ───────────
ok("UPDATE dynamics SET brand_id=?" in src,
   "★ 模型抽出的 brand 必须回写，原来直接扔掉")
ok("_verified_brand(" in src and "rebound" in src,
   "回写要走核对函数；已挂品牌若原文无据而模型报的有据，才改绑")

alias_map = _alias_map()
name2id = {b["name"].lower(): b["id"] for b in db.q("SELECT id,name FROM brand")}
xiaomi = name2id.get("xiaomi")
text = f"{TITLE} {BODY}"
ok(xiaomi is not None, "（测试前提）品牌表里要有 Xiaomi")
ok(_verified_brand("Xiaomi", text, alias_map, name2id) == xiaomi,
   "模型报小米、原文有小米 → 挂小米")
ok(_verified_brand("xiaomi", text, alias_map, name2id) == xiaomi,
   "大小写不敏感")
ok(_verified_brand("Motorola", text, alias_map, name2id) is None,
   "★ 模型报的品牌原文没提 → 不采信（品牌对、摘要错比丢掉危险得多）")
for junk in ("null", None, "", "Netlist"):
    ok(_verified_brand(junk, text, alias_map, name2id) is None,
       f"垃圾值 {junk!r} 不能挂出品牌")

# ★ 中文品牌名：模型有时返回「苹果」而不是 Apple（run 339 实测 苹果×9 小米×3 Acme 三星），
#   英文别名表解析不到 ⇒ 一整批 Apple 新闻全部无主。代码侧必须能解析，且仍要原文核对。
apple = name2id.get("apple")
ok(_verified_brand("苹果", "Apple Watch Series 12: Three new features", alias_map, name2id)
   == apple, "★ 模型报「苹果」、原文有 Apple → 挂 Apple")
ok(_verified_brand("Acme", "Recordamos a Acme por sus móviles", alias_map, name2id)
   == name2id.get("acme"), "★ 模型报「Acme」、西语原文有 Acme → 挂 Acme")
ok(_verified_brand("苹果", "Samsung Galaxy S26 launches", alias_map, name2id) is None,
   "中文名解析后**仍要原文核对**：原文没 Apple 就不挂")

# ★ 短名品牌：_alias_map 的 len>=3 过滤曾把 "HP" 本名一起剔掉，
#   一篇只写 HP 不写产品线的新闻永远核对不到。本名必须无条件保留。
hp = name2id.get("hp")
ok(hp is not None and "HP" in (alias_map.get(hp) or []),
   f"★ 品牌本名要在可核对别名表里（HP 曾被 len>=3 剔掉），实得 {alias_map.get(hp)}")
ok(_verified_brand("HP", "HP launches a new laptop lineup", alias_map, name2id) == hp,
   "只写 HP 不写产品线的新闻也要能挂上")
ok(_verified_brand("HP", "the new chip is faster", alias_map, name2id) is None,
   "词边界：chip 里的 hp 不能命中")

# ─────────── 真库实据 ───────────
row = db.q1("SELECT summary_zh, brand_id FROM dynamics WHERE id=10255")
if row:
    ok(row["brand_id"] == xiaomi,
       f"★ 截图那条（#10255）要挂到小米，实得 brand_id={row['brand_id']}")
    s = row["summary_zh"] or ""
    ok("Fold 8竞品" not in s and "Fold 8 竞品" not in s,
       f"★ 截图那条的摘要不能再是「Galaxy Z Fold 8 竞品」，实得「{s[:50]}」")
    ok("18 Fold" in s,
       f"★ 摘要要写出小米自己的机型 Xiaomi 18 Fold，实得「{s[:50]}」")

r = db.q1("""SELECT COUNT(*) n, SUM(brand_id IS NULL) nb FROM dynamics
             WHERE created_at >= datetime('now','-30 day') AND importance >= 3""")
rate = (r["nb"] or 0) / r["n"] if r["n"] else 0
# 修前 91%。剩余无品牌的两类都合理：① 原文根本没提追踪品牌（Google/Pixel/Amazon/
# Anker/Sonos 不在 26 个追踪品牌里，实测 111/193）；② 提了追踪品牌但做事方不是它
# （"Pixel 11 Pro Fold 对比 Galaxy Z Fold 8" 做事的是 Google，不能挂三星）。
# 所以这里只做回归绊线，真正的不变量在下面。
ok(rate <= 0.5,
   f"★ 重要度≥3 的动态无品牌率要 ≤50%（修前 91%），实得 {rate:.0%}（{r['nb']}/{r['n']}）"
   f"—— 回升说明回写又被绕过了")

# ★ 硬不变量：标题以追踪品牌名**开头**的动态（"Xiaomi unveils…" / "Apple Watch Series 12
#   will…"），做事方几乎必是它，必须挂到该品牌。这一条不受"未追踪品牌"稀释。
bname = {b["id"]: b["name"] for b in db.q("SELECT id,name FROM brand WHERE is_ours=0")}
rows = db.q("""SELECT id, title, brand_id FROM dynamics
               WHERE created_at >= datetime('now','-30 day') AND importance >= 3""")
lead = [(x, bid) for x in rows for bid, nm in bname.items()
        if (x["title"] or "").lower().startswith(nm.lower() + " ")]
bound = sum(1 for x, bid in lead if x["brand_id"] == bid)
if lead:
    ok(bound / len(lead) >= 0.85,
       f"★ 标题以追踪品牌开头的动态要 ≥85% 挂到该品牌，实得 {bound}/{len(lead)}："
       + "; ".join(f"#{x['id']} {(x['title'] or '')[:36]}"
                   for x, bid in lead if x["brand_id"] != bid)[:300])

print(f"intel: {PASS[0]} 通过, {len(FAIL)} 失败")
for m in FAIL:
    print("  [FAIL]", m)
sys.exit(1 if FAIL else 0)
