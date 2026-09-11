# -*- coding: utf-8 -*-
"""模型输出卫生：字符串 "null" 不许当成有效值落库。

跑法：  python tests\test_llmhygiene.py

为什么单独立一个文件：提示词里写"不确定就填 null"，模型照做但写成
**字符串**。落库后 `IS NOT NULL` 判定为真 —— 后果和这条要求正好相反。
这个坑已经在两个互不相干的地方各栽了一次：
  1. 规格表 13 行拿字符串 "null" 去比芯片档位（相似度全乱）
  2. 情报流建出一台叫 "null" 的 Honor 手机，摆在上市看板上给人看
两次都是同一个成因、不同的调用点，所以闸门收敛到 llm.as_text，
这里守住它 —— 下一个调用点再犯时能被这组断言拦下。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.llm import as_text  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


print("== 各种写法的空值都要归成 None ==")
for raw in ("null", "NULL", "Null", " null ", "none", "None", "N/A", "n/a",
            "NA", "nil", "undefined", "unknown", "-", "--", "", "   "):
    check(f"{raw!r} → None", as_text(raw), None)

print("== 西语/葡语的未指明也算空 ==")
# 拉美站点和西语提示词下，模型经常用本地说法而不是 null
for raw in ("desconocido", "Desconocido", "no especificado", "sin especificar"):
    check(f"{raw!r} → None", as_text(raw), None)

check("真正的 None 仍是 None", as_text(None), None)

print("== 真值不能被误杀 ==")
# ★ 这组是反向保险：闸门开太大会把合法型号名吃掉。
check("普通型号名", as_text("Galaxy S25 Ultra"), "Galaxy S25 Ultra")
check("★含 null 子串的型号不受影响", as_text("Nullify X1"), "Nullify X1")
check("★名字里带连字符的型号", as_text("P50-Pro"), "P50-Pro")
check("★单个 0 是合法值不是空", as_text("0"), "0")
check("★数字 0 同理", as_text(0), "0")
check("两端空白照常去掉", as_text("  Nimbus 13  "), "Nimbus 13")
check("非字符串照常转字符串", as_text(12), "12")

print("== 调用点确实接了闸门 ==")
# 光有函数没用 —— 必须确认出问题的那两处真的调了它
import inspect  # noqa: E402

from app.agents import intel, spec_filler  # noqa: E402

src = inspect.getsource(intel.IntelAgent._discover_new_products)
check("★情报流建产品前过闸", 'as_text(r.get("product"))' in src, True)
check("★情报流品牌名也过闸", 'as_text(r.get("brand"))' in src, True)
check("★规格填充的文本闸门就是同一个", spec_filler._text is as_text, True)

print("== 建产品的长度门槛拦不住 null 字样（所以才需要上面的闸）==")
# 记录这条的用意：说明为什么"len>=3"这个已有的防线不够
check_len = len("null") >= 3
check('★null 字样长度 4，能过 len>=3 的门槛', check_len, True)

print("== ★ 批量对齐：一个实现四处消费 ==")
# knowledge/lessons/llm-batch-result-alignment 08-28 写下后只修了 VOC 一处；
# 2026-09-04 #9168「小米 18 Fold」配上 Apple Watch 摘要，全仓扫出 intel /
# price_audit / spec_filler / brandintel 四处同病。序号是模型自己写的，不许当下标。
import pathlib as _pl  # noqa: E402

_agents = _pl.Path(__file__).resolve().parents[1] / "app/agents"
for _f in sorted(_agents.glob("*.py")):
    _s = _f.read_text(encoding="utf-8")
    check(f"{_f.name}: 不许按模型写的序号取下标",
          'int(item.get("idx"))' in _s, False)
for _name in ("intel.py", "price_audit.py", "spec_filler.py", "brandintel.py"):
    _s = (_agents / _name).read_text(encoding="utf-8")
    check(f"{_name}: 批量结果必须走 llm.pick_by_key", "pick_by_key(" in _s, True)
    check(f"{_name}: prompt 里要有核对码", "(k=" in _s, True)

from app.agents.llm import batch_key, pick_by_key  # noqa: E402

_keys = [batch_key(101), batch_key(102), batch_key(103)]
_picked, _st = pick_by_key(
    [{"idx": 1, "k": _keys[0], "v": "a"},        # 序号错位但核对码对 → 按核对码收进第 0 条
     {"idx": 1, "k": _keys[1], "v": "b"},
     {"idx": 2, "k": "zzzzz", "v": "x"},          # 核对码抄错 → 拒收
     {"idx": 0, "k": _keys[0], "v": "dup"}],      # 同一条回两次 → 只收第一条
    _keys)
check("按核对码定位，不信序号", [(j, it["v"]) for j, it in _picked], [(0, "a"), (1, "b")])
check("统计：收 2 拒 2 漏 1", (_st["accepted"], _st["rejected"], _st["dropped"]), (2, 2, 1))
check("核对码 5 位可抄", len(_keys[0]) == 5 and _keys[0].isalnum(), True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
