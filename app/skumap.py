# -*- coding: utf-8 -*-
"""权威 SKU 映射表 —— 移植自用户的 PowerQuery M 代码（SKU_Short）。

来源：用户 2026-08-10 提供的 `PowerQuery_SKU_Short_M_Code.txt`
      272 条规则 → 131 个唯一 SKU，覆盖 iPad / Galaxy Tab / Slate Tab /
      Honor Pad / Lenovo Tab / Xiaomi·Redmi·POCO Pad

★ 这是**权威表**，优先级高于 cleaner.py 的通用归一化规则。
  用户明确要求："你要保证每次抓取的数据都会包含这些产品"。
  通用规则只在这张表没命中时兜底。

★ M 语义必须 1:1 保留，任何一处偏差都会让归类结果整体改变：

  1. **归一化管线的顺序**
     lower → 去控制字符 → NBSP→空格 → 去重音 → ,→. → -→空格 → _→空格 → 压缩空白
     先 lower 再去重音，所以只需处理小写重音字母。
     逗号转点是为了让 "12,9" 与 "12.9" 等价（西语用逗号做小数点）。

  2. **配件判定优先于 SKU 匹配** —— 顺序反了会把保护套归到平板 SKU 上

  3. **Rules 顺序敏感**：取第一个匹配（M 的 List.First）。
     所以规则表必须从最具体（`ipad air 13 m4`）排到最泛化（`ipad air`）。
     ⚠ 改动 sku_rules.yaml 的行序 = 改变归类结果。

  4. **每条规则的 keys 是 AND**：所有 key 都要出现（Text.Contains）

  5. **key 的前后空格有意义**：`'ipad 8 '` 带尾部空格是为了不匹配 `ipad 80`。
     解析与匹配都不得 strip。

  6. **book cover / keyboard 只在不含 gb/ram/memoria 时算配件** ——
     含了就是整机捆绑装（与 extract.detect_product_kind 的"捆绑算整机"同源）
"""
from __future__ import annotations

import functools
import re
import unicodedata
from pathlib import Path

import yaml

from .config import CONFIG_DIR
# 句法配件规则的单一实现在 extract（那边还有 detect_product_kind 和回填工具两个
# 消费方）。extract 不 import 任何 app 内模块，这个方向不会成环。
from .scraping.extract import accessory_para_form as _acc_para_form

ACCESSORY = "Accessory"
OTHER_TABLET = "Other Tablet"
OTHER = "Other"

# M 代码里 Text.Replace 的对照表，顺序与原文一致
_ACCENTS = [("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
            ("ü", "u"), ("ñ", "n"), (",", "."), ("-", " "), ("_", " ")]

_CONTROL = {c: " " for c in range(32)}
_CONTROL[160] = " "        # NBSP —— M 代码单独处理了这个


def normalize(raw: str | None) -> str:
    """与 M 代码的 T 完全一致的归一化。"""
    if not raw:
        return ""
    t = str(raw).lower()
    t = t.translate(_CONTROL)                       # Text.Clean + NBSP
    for a, b in _ACCENTS:
        t = t.replace(a, b)
    # M: SplitAny by space/tab/lf/cr → 去空 → Combine(" ")
    return " ".join(x for x in re.split(r"[ \t\n\r]+", t) if x)


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    path = CONFIG_DIR / "sku_rules.yaml"
    if not path.exists():
        return {"accessory_prefixes": [], "accessory_contains": [], "rules": []}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def reload_rules() -> None:
    _load.cache_clear()


def is_accessory(t: str) -> tuple[bool, str]:
    """配件判定。t 必须是 normalize() 之后的文本。返回 (是否配件, 依据)。"""
    cfg = _load()
    for p in cfg.get("accessory_prefixes") or []:
        if t.startswith(p):                          # ★ StartsWith 不是 Contains
            return True, f"以配件前缀「{p.strip()}」开头"
    for w in cfg.get("accessory_contains") or []:
        if w in t:
            return True, f"含配件词「{w}」"
    # ★ 葡语/西语「主语位置配件词 + para/de」句法规则（extract 里的单一实现）。
    #   yaml 词表移植自用户 PowerQuery，天生只有西语+英语 ——
    #   「Capa para Tablet Acme Slate Tab」一个词都拦不住，往下走就命中
    #   Slate Tab 的 SKU 规则被判成整机（实测把Acme巴西平板 ASP 拉到 20 美元）。
    #   放在 yaml 之后：权威表明确说了算的仍以权威表为准；这里只补它的语言盲区。
    why = _acc_para_form(t)
    if why:
        return True, why
    # acme m pen / m pencil：不在 slate 语境里才算配件（笔单卖 vs 平板带笔）
    for pen in ("acme m pen", "acme m pencil"):
        if pen in t and "slate" not in t:
            return True, f"含「{pen}」且不在 slate 语境 → 笔单卖"
    # ★ book cover / keyboard：含 gb/ram/memoria 说明是整机捆绑，不算配件
    if ("book cover" in t or "keyboard" in t) and not any(
            k in t for k in ("gb", "ram", "memoria")):
        return True, "含 book cover/keyboard 且无 gb/ram/memoria → 纯配件"
    return False, ""


def classify(title: str | None, category: str | None = None) -> dict:
    """把商品标题归到权威 SKU。

    返回 {sku, kind, matched_keys, reason}
      kind: sku | accessory | other_tablet | other | empty | out_of_scope

    ★★ category 必须传，否则这张**只覆盖平板**的表会去认领别的品类的商品。
      实测事故：AirPods 的评论全被记到「Apple iPad Air」名下 ——
      规则 key 是 `apple air`，而归一化后的标题
      "apple airpods 4 con cancelacion activa de ruido" **含有子串 "apple air"**，
      于是耳机命中了 iPad 的规则，sku_code 被写成 Apple iPad Air、
      product_kind 判成 device，一路带到 rival_product 和评论归属。
      后果：一个平板产品下面挂着 24 条耳机评论，口碑维度图上
      iPad 会显示"降噪好、佩戴舒适"。

      ★ 为什么不改成按词边界匹配：这张表和 key 的语义都来自用户的 PowerQuery
        （M 里就是 Text.Contains 子串匹配），本项目刻意与它保持一致
        —— 两边口径必须对得上。在**平板**数据上 `apple air` 是条好规则，
        问题从来不是规则，是我们拿平板的表去问音频的商品。
        所以修的是作用域，不是匹配语义。

      ★ 传 None 时**保守放行**（保持老行为），但只应出现在真的不知道品类的
        场合（比如离线工具）。采集链路上一律要传。
    """
    if not title or not str(title).strip():
        return {"sku": None, "kind": "empty", "matched_keys": [], "reason": "标题为空"}

    if category and category not in COVERED_CATEGORIES:
        return {"sku": None, "kind": "out_of_scope", "matched_keys": [],
                "reason": f"权威表只覆盖 {'/'.join(sorted(COVERED_CATEGORIES))}，"
                          f"不判定 {category} 品类"}

    t = normalize(title)

    acc, why = is_accessory(t)
    if acc:
        return {"sku": ACCESSORY, "kind": "accessory", "matched_keys": [],
                "reason": why}

    # ★ 顺序敏感：取第一个全部 key 都命中的规则
    for r in _load().get("rules") or []:
        keys = r.get("keys") or []
        if keys and all(k in t for k in keys):
            return {"sku": r["sku"], "kind": "sku", "matched_keys": keys,
                    "reason": f"命中规则 {keys}"}

    if "tablet" in t or "tableta" in t:
        return {"sku": OTHER_TABLET, "kind": "other_tablet", "matched_keys": [],
                "reason": "含 tablet/tableta 但未命中任何 SKU 规则"}
    return {"sku": OTHER, "kind": "other", "matched_keys": [],
            "reason": "未命中任何 SKU 规则，也不含 tablet 字样"}


@functools.lru_cache(maxsize=1)
def all_skus() -> list[str]:
    """规则表里的全部唯一 SKU，保持原始顺序。"""
    seen, out = set(), []
    for r in _load().get("rules") or []:
        if r["sku"] not in seen:
            seen.add(r["sku"])
            out.append(r["sku"])
    return out


# ★ 这张权威表**只覆盖平板品类**（iPad / Galaxy Tab / Slate Tab / Pad …）。
#   这一点在表本身里是隐含的，代码里必须显式化 —— 否则跑手机品类时
#   会拿 "Samsung Galaxy Tab S11 Ultra" 去搜手机，每个单元白跑 8 次查询。
COVERED_CATEGORIES = {"tablet"}


def search_terms(brand: str | None = None,
                 category: str | None = None) -> list[str]:
    """把 SKU 表转成搜索词 —— 保证每轮抓取都覆盖到这些产品。

    用户要求："保证每次抓取的数据都会包含这些产品"。
    只靠"品类词+品牌名"（tablet Samsung）搜不全：一次搜索只返回二三十条，
    热销款会把长尾型号挤掉，所以必须拿具体型号名去搜。

    category 传了且不在本表覆盖范围内时返回空 —— 见 COVERED_CATEGORIES。
    """
    if category and category not in COVERED_CATEGORIES:
        return []
    out = []
    for sku in all_skus():
        if brand and brand_of(sku).lower() != brand.lower() \
                and not sku.lower().startswith(brand.lower()):
            continue
        # SKU 名本身就是很好的搜索词（"Samsung Galaxy Tab S11 Ultra"）
        out.append(sku)
    return out


def brand_of(sku: str) -> str:
    """SKU 的品牌（表里 SKU 名以品牌打头）。Redmi/POCO 归到 Xiaomi。"""
    first = (sku or "").split()[:1]
    b = first[0] if first else ""
    return {"Redmi": "Xiaomi", "POCO": "Xiaomi"}.get(b, b)
