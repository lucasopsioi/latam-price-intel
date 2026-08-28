# -*- coding: utf-8 -*-
"""上市事件分类的回归测试。

守两件事：
  1. `_record_launch` 必须真的挂在 IntelAgent 上（它曾被缩进成死代码）
  2. 只有真上市才进上市表（曾经 12 条里 10 条是噪声）
"""
import ast
import inspect
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app.agents import intel as I  # noqa: E402

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ────────── 1. 死代码回归：方法被缩进进了别的函数里 ──────────
# 实测事故：`_record_launch` 被缩进到模块级函数 `_covered_countries()` 的
# return 之后，成了永不执行的嵌套函数。于是每次 self._record_launch(...) 都抛
# AttributeError，被上层 except 吞掉记成一行 warning ——
# **上市看板从 2026-08-12 起停止产生任何新数据，日志里 41 次失败没人看见。**
ok(hasattr(I.IntelAgent, "_record_launch"),
   "★ IntelAgent 必须有 _record_launch 方法 —— "
   "它曾被误缩进到 _covered_countries() 内部变成死代码，"
   "调用处 AttributeError 被 except 吞掉，看板静默停更")
ok(callable(getattr(I, "_covered_countries", None)),
   "_covered_countries 应是模块级函数")

# 结构级：类里的方法不许出现在别的函数体内部
src = pathlib.Path(I.__file__).read_text(encoding="utf-8")
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "_covered_countries":
        nested = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
        ok(not nested,
           f"★ _covered_countries 内部不该嵌套函数定义，实得 {nested} —— "
           f"这正是上次把方法写死的形态")

# 调用处必须存在（否则测了个寂寞）
run_src = inspect.getsource(I.IntelAgent)
ok("_record_launch" in run_src, "IntelAgent 里应有对 _record_launch 的调用")


# ────────── 2. 分类：只有真上市才算上市 ──────────
# 全部取自库里那 12 条真实数据。
CASES = [
    # (文本, 期望类型)
    ("Galaxy Z Fold 8 上市，市场反应平淡。", "launch"),
    ("AirTag 2 发布，新增范围、扬声器等升级。", "launch"),
    ("Bose旗舰无线耳机降价50美元，促销中。", "promo"),
    ("Acer Chromebook Plus Spin 714降价38%，现售499美元。", "promo"),
    ("Motorola Moto Watch (2026)评测：外观可能具有欺骗性。", "rumor"),  # 含"可能"，先判传闻
    ("苹果或为折叠屏iPhone采用“Ultra”命名。", "rumor"),
    ("Acer首款Googlebook可能避免预算Chromebook的缺陷。", "rumor"),
    ("AirPods Pro 3等设备发布新公测固件。", "software"),
    ("iPhone 18 Pro将带来三项相机升级。", "other"),
    ("三星宣布Galaxy Buds将增加助听器功能并通过FDA审批。", "other"),
]
for text, want in CASES:
    got = I._event_kind(text)
    ok(got == want, f"分类错：{text[:34]} → 期望 {want}，实得 {got}")

# 关键性质：这 10 条里只有 2 条能进上市表
n_launch = sum(1 for t, _ in CASES if I._event_kind(t) == "launch")
ok(n_launch == 2,
   f"★ 这批真实数据里应只有 2 条真上市，实得 {n_launch} —— "
   f"原来的逻辑（没国家码就算全球首发）会把 12 条全放进来，83% 是噪声")

# 降价新闻绝不能算上市：会让「距全球首发 N 天」整体失真
for t in ["某机型降价 500 元", "限时促销立减 20%", "Gran oferta: 30% descuento"]:
    ok(I._event_kind(t) != "launch", f"降价/促销不该判为上市：{t}")

# 固件/系统更新不是产品上市
for t in ["发布新公测固件", "推送 HarmonyOS 5 更新", "beta 版本发布"]:
    ok(I._event_kind(t) != "launch", f"软件/固件不该判为上市：{t}")

ok(I._event_kind("") == "other", "空文本应落到 other，不该炸")
ok(I._event_kind(None) == "other", "None 应落到 other，不该炸")


# ────────── 3. 判定必须是确定性代码，不是让模型自己报 ──────────
rec = inspect.getsource(I.IntelAgent._record_launch)
ok("_event_kind" in rec,
   "★ _record_launch 必须调用确定性分类器。"
   "本项目已验证「让模型自我否决」不可靠（自评不达标仍照交 54~62%），"
   "分类不能交给模型自报")
ok('!= "launch"' in rec or "!= 'launch'" in rec,
   "非上市的必须 return 0 不入库")


# ────────── 4. 接口只返回真上市，且要报出挡掉多少 ──────────
server = (ROOT / "app/api/server.py").read_text(encoding="utf-8")
tree2 = ast.parse(server)
fn = next((n for n in ast.walk(tree2)
           if isinstance(n, ast.FunctionDef) and n.name == "get_launches"), None)
ok(fn is not None, "server.py 应有 get_launches")
if fn:
    body = ast.get_source_segment(server, fn) or ""
    ok("global_launch" in body and "country_available" in body,
       "接口应只放行真上市类型")
    ok("filtered_non_launch" in body or "filtered_note" in body,
       "★ 被挡掉多少条必须报出来 —— 静默过滤等于谎报太平")


print(f"launchkind: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
