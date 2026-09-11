# -*- coding: utf-8 -*-
"""合并工具的档位守卫（2026-09-07）。

背景：tools/merge_polluted_models.py 把归一化后同名的产品合并。归一化会把
「Redmi Note 15 Pro Plus」洗成「Redmi Note 15 Pro」，于是它和**真正的** Pro
落进同一组被合并 —— 那是两款不同的机器、两个价位段。

这是同一课的第二次复发：cleaner._TRAILING 早就写着「紧贴前一个词的 + 是型号的
一部分」（S26 与 S26+ 合并实测出现 88% 的假价差），但那条规则守的是**标题解析**，
守不住**产品合并**这条路径。规则单一实现多处消费做不到时，每条路径都要自己的闸。

守的三条性质：
  ① 档位词集合不一致的组拒绝合并（只改名）；
  ② 闸门要**接线**到写库分支 —— 第一版写完后写库分支仍遍历 groups 而不是 merges，
     等于闸门只在打印时生效（"闸门写了没接线"，assertions-that-verify-nothing 同族）；
  ③ 逗号名不参与合并（情报 Agent 从新闻标题建的多机型垃圾产品，会把真产品连累）。

跑法： python tests\test_merge_guard.py
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r} want={want!r}")


def ok(name, cond, hint=""):
    check(f"{name}{(' (' + hint + ')') if hint else ''}", bool(cond), True)


SRC_PATH = ROOT / "tools/merge_polluted_models.py"
SRC = SRC_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SRC)

print("== 档位词表 ==")
sys.argv = ["merge_polluted_models.py"]          # 干跑模式，import 不写库
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("mpm", SRC_PATH)
mpm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mpm)

TIER = mpm._TIER_RE
for w in ("Plus", "Pro", "Max", "Ultra", "Lite", "Mini", "FE"):
    ok(f"档位词表含 {w}", bool(TIER.search(f"Redmi Note 15 {w}")))
# 词边界：不能在别的词里命中
ok("★ 两头要有词界：Proyector 不算 Pro", not TIER.search("Proyector"))
ok("★ Maxima 不算 Max", not TIER.search("Maxima"))
ok("★ Plusvalia 不算 Plus", not TIER.search("Plusvalia"))


def tiers(name):
    return tuple(sorted(w.lower() for w in TIER.findall(name or "")))


print("== 分组判据 ==")
check("Pro 与 Pro Plus 档位不同",
      tiers("Redmi Note 15 Pro") != tiers("Redmi Note 15 Pro Plus"), True)
check("大小写不敏感", tiers("REDMI NOTE 15 PRO"), tiers("Redmi Note 15 pro"))
check("顺序不敏感", tiers("Galaxy S26 Pro Max"), tiers("Galaxy S26 Max Pro"))
check("无档位词的两个名字算同档",
      tiers("Galaxy A57") == tiers("Galaxy A57 +"), True)

print("== ★ 闸门必须接线到写库分支（不是只在打印时生效）==")
# 找到 main() 里 `with db.tx()` 之后的第一个 for 循环，它遍历的必须是 merges
fn = next(n for n in ast.walk(TREE)
          if isinstance(n, ast.FunctionDef) and n.name == "main")
withs = [n for n in ast.walk(fn) if isinstance(n, ast.With)]
ok("main 里有 db.tx() 事务", bool(withs))
merge_loop_iters = []
for w in withs:
    for node in w.body:
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Call):
            f = node.iter.func
            if isinstance(f, ast.Attribute) and f.attr == "items" \
                    and isinstance(f.value, ast.Name):
                merge_loop_iters.append(f.value.id)
ok("★ 写库循环遍历的是 merges 不是 groups",
   merge_loop_iters and merge_loop_iters[0] == "merges",
   f"实得 {merge_loop_iters}；遍历 groups = 守卫被绕过，闸门只在打印时生效")

# 反向自检：把 merges 改回 groups，断言必须变红
_mut = SRC.replace("for k, v in merges.items():\n            keep = _pick_keep(v)",
                   "for k, v in groups.items():\n            keep = _pick_keep(v)")
ok("（自检）源码里确实存在那处写库循环", _mut != SRC)
_mt = ast.parse(_mut)
_mfn = next(n for n in ast.walk(_mt)
            if isinstance(n, ast.FunctionDef) and n.name == "main")
_mit = []
for w in [n for n in ast.walk(_mfn) if isinstance(n, ast.With)]:
    for node in w.body:
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Call):
            f = node.iter.func
            if isinstance(f, ast.Attribute) and f.attr == "items" \
                    and isinstance(f.value, ast.Name):
                _mit.append(f.value.id)
ok("★ 反向自检：改回 groups 后断言会红",
   not (_mit and _mit[0] == "merges"),
   "断言抓不到这个改动就等于没写")

print("== 逗号名不参与合并 ==")
ok("源码里有逗号名分流", "multi_name" in SRC and '"," in' in SRC)
ok("逗号名在 continue 之前被收走",
   SRC.index("multi_name.append") < SRC.index("groups[(r[\"brand_id\"]"))

print("== 被拦下的组仍要改名（守卫只挡合并，不挡改名）==")
ok("blocked 分支里有 UPDATE ... model_name",
   "for k, v in blocked:" in SRC
   and "UPDATE OR IGNORE rival_product" in SRC[SRC.index("for k, v in blocked:"):])
ok("★ blocked 改名用 OR IGNORE（撞 UNIQUE 时保持原名，不能让整个事务回滚）",
   "UPDATE OR IGNORE rival_product" in SRC[SRC.index("for k, v in blocked:"):])
ok("blocked 分支不搬外键、不删行",
   "REF_TABLES" not in SRC[SRC.index("for k, v in blocked:"):],
   "被拦下的组一旦搬外键就等于合并了")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
