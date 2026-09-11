# -*- coding: utf-8 -*-
"""桶（系列级 SKU / 兜底名）的回归测试 —— 2026-09-04 用户截图 Lenovo Tab 曲线跳变。

守的性质，每条对应一次真实的错误输出：
  1. 系列级 SKU 命中时要回标题把型号 token 找回来（"LENOVO Tablet M10 5G" → Lenovo Tab M10），
     找不回来的保留系列名但标 is_bucket=1；屏幕尺寸 12.7 绝不能当型号 P12，
     但 Yoga 的 12.7"/16GB RAM **就是** Plus 的规格（尺寸证据不许只用一半）。
  2. 权威表判整机、位置规则判配件的行（钢化膜带 sku_code='Lenovo Tab'）不许挂到产品上。
  3. 桶判据只有一份实现；真单品（Lenovo Tab One / Xiaomi Pad Mini）与**系列基础款**
     （Lenovo Idea Tab / Lenovo Yoga Tab / Redmi Pad）不许被标成桶，且有可持久的纠正通道
     （yaml generic:false / _BASE_MODEL_SKUS / bucket_reason 'manual:'）。
  4. trackable_products / matcher._candidates / **trends.suggest_watch**（用户截图那页的
     下拉）必须排除桶 —— 源码级断言走 ast **且跳过 docstring**，并反向自检一次
     （删掉闸必须变红）；再在临时库上造一个桶断言它不出现。
  5. 拆桶回滚只删本次真的新建出来的产品，且删之前逐张外键表点名检查：
     review/watchlist 等是 ON DELETE CASCADE（静默删评论），strategy_signal 等没有
     CASCADE（IntegrityError 让整份回滚失效）。
  6. 两个工具的 --apply/--rollback 都过采集闸。

跑法： python tests\test_bucket.py
"""
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app import config                                   # noqa: E402

# ★ 必须早于任何 db.get_conn()：连接缓存在线程本地，连上真库就切不走了。
_TMP = Path(tempfile.mkdtemp(prefix="bucket_"))
config.DB_PATH = _TMP / "t.db"

from app import db, skunorm                              # noqa: E402
from app.agents.cleaner import CleanerAgent, identity_for   # noqa: E402

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ─────────────── 1. 系列级 SKU 的识别 ───────────────
GEN = set(skunorm.generic_skus())
for s in ("Lenovo Tab", "Lenovo Idea Tab", "Samsung Galaxy Tab", "Apple iPad",
          "Apple iPad Pro", "Acme Slate Tab", "Redmi Pad", "Xiaomi Pad", "POCO Pad"):
    ok(s in GEN, f"★ {s!r} 是系列级 SKU（表里有以它为前缀的更细 SKU），必须识别为 generic")
for s in ("Lenovo Tab M11", "Lenovo Tab One", "Xiaomi Pad Mini", "Lenovo Idea Tab Pro",
          "Redmi Pad SE", "Samsung Galaxy Tab A11", "Redmi Pad 2"):
    ok(s not in GEN, f"★ {s!r} 是真单品（或只有尺寸扩展），不许被标成 generic —— "
                     f"误标会把它赶出趋势入口与竞品候选池")
ok(all(not any(ch.isdigit() for ch in s) for s in GEN), "generic SKU 都不含数字")

# ★★ 系列级（= 该不该回标题细分）与桶（= 该不该被单品消费方排除）是两件事。
#   系列基础款两者都要：细分照跑（否则 Ideatab Plus 永远留在基础款里 = 把干净单品改脏），
#   桶标不打（否则真单品被永久赶出趋势入口/竞品候选池/曲线下拉，且**无法纠正**）。
for s in ("Lenovo Idea Tab", "Lenovo Yoga Tab", "Redmi Pad"):
    ok(s in GEN and not skunorm.is_bucket_sku(s),
       f"★ {s!r} 是系列基础款：is_generic_sku 要真（细分入口）、is_bucket_sku 要假（不打桶标），"
       f"实得 generic={s in GEN} bucket={skunorm.is_bucket_sku(s)}")
for s in ("Lenovo Tab", "Samsung Galaxy Tab", "Apple iPad", "Lenovo Legion Tab"):
    ok(skunorm.is_bucket_sku(s), f"★ {s!r} 仍然是桶（表里的兄弟是真的不同机型）")

# yaml 的显式 generic 是**可持久的纠正通道**，优先于本文件的任何判据（两个方向都认）
_real_flags = skunorm.sku_generic_flags
ok(isinstance(_real_flags(), dict), "sku_generic_flags 返回 {SKU: bool}")
skunorm.sku_generic_flags = lambda: {"Lenovo Tab": False, "Redmi Pad": True}
ok(skunorm.is_bucket_sku("Lenovo Tab") is False and skunorm.is_bucket_sku("Redmi Pad") is True,
   "★ yaml 的 generic:false/true 覆盖结构判据与基础款白名单")
skunorm.sku_generic_flags = _real_flags
ok(skunorm.is_bucket_sku("Lenovo Tab") is True and skunorm.is_bucket_sku("Redmi Pad") is False,
   "还原后判据回到默认")


# ─────────────── 2. 回标题找型号 token（全部取自 #2292/#2291/#2274/#1109 真实标题）───────────────
REFINE = [
    ("Lenovo Tab", "LENOVO Tablet M10 5G Cel 10.6 pulgadas 128 GB", "Lenovo Tab M10"),
    ("Lenovo Tab", "Tablet Lenovo Tab M11 128GB 4GB RAM + Lapiz", "Lenovo Tab M11"),
    ("Lenovo Tab", "GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11 J606F", "Lenovo Tab P11"),
    ("Lenovo Tab", "ROCK SPACE LAMINA HIDROGEL PARA TABLET LENOVO TAB K10", "Lenovo Tab K10"),
    ("Lenovo Tab", "Lenovo Tablet Pro Mediatek 8GB 256GB 12.7", "Lenovo Tab Pro"),
    ("Lenovo Tab", "Tablet Lenovo Tab Play Kids 64GB", "Lenovo Tab Play"),
    # ★ 12.7" / 16GB RAM 是 Yoga Tab Plus 的规格（基础款是 11.1" + 12GB RAM）。
    #   只看 'plus' 一个词的旧写法把这 12 条（629,990~799,990 CLP）并进了 11.1" 的基础款
    #   #2934 —— 同一个函数上面已经用 12.7 去否决 Tab P12，尺寸证据只用了一半。
    ("Lenovo Tab", "Lenovo Tablet Yoga Snapdragon 8 G3 16GB 256GB 12.7", "Lenovo Yoga Tab Plus"),
    ("Lenovo Tab", "Lenovo Tablet Yoga Snapdragon 8 G3 16GB RAM 256GB 12.7'' 144Hz", "Lenovo Yoga Tab Plus"),
    # 基础款自己（11.1"/12GB）不许被这条证据带走
    ("Lenovo Tab", "Tablet Lenovo Yoga Tab 12GB RAM 256GB 11 + Teclado y Lapiz", "Lenovo Yoga Tab"),
    # 同一份证据，两个入口（yaml 命中 'yoga tab' 规则 vs 落到 'lenovo tab' 兜底）必须同名
    ("Lenovo Yoga Tab", "Tablet Lenovo Yoga Tab 12.7 3.2K 16GB RAM 512GB", "Lenovo Yoga Tab Plus"),
    ("Lenovo Tab", "Tablet Lenovo Tab P12 128GB", "Lenovo Tab P12"),
    ("Lenovo Idea Tab", "Tablet IdeaTab Plus 12 8GB 256GB", "Lenovo Idea Tab Plus"),
    ("Samsung Galaxy Tab", "Samsung Tablet Galaxy Tab A11 64GB", "Samsung Galaxy Tab A11"),
    ("Samsung Galaxy Tab", "Tablet Samsung Galaxy Tab S10 Lite 128GB", "Samsung Galaxy Tab S10 Lite"),
    ("Samsung Galaxy Tab", "Galaxy Tab S10 FE+ 256GB Gris", "Samsung Galaxy Tab S10 FE+"),
    ("Samsung Galaxy Tab", "Galaxy Tab S9+ 12.4 256GB", "Samsung Galaxy Tab S9+"),
    ("Redmi Pad", "Xiaomi Redmi Pad 2 128GB Wifi", "Redmi Pad 2"),
    ("Redmi Pad", "Xiaomi Redmi Pad SE 4GB 128GB Gris", "Redmi Pad SE"),
    ("Redmi Pad", "Redmi Pad SE 8.7 4GB 64GB", "Redmi Pad SE 8.7"),
    ("Lenovo Tab", "LENOVO Tablet M11 k10 8GB+128GB WiFi y Teclado", "Lenovo Tab M11"),  # 先出现的型号说了算
    ("Lenovo Tab", "Tablet Lenovo Pad M10 Plus 4GB+128GB 10.6", "Lenovo Tab M10"),      # yaml 口径：M10 Plus 归 M10
    ("Lenovo Tab", "Tablet Lenovo Tab P12 Pro Snapdragon 8GB RAM 256GB 12.6", "Lenovo Tab P12 Pro"),
    ("Redmi Pad", "Xiaomi Redmi Pad Pro 12.1 8GB 256GB", "Redmi Pad Pro"),
    ("Redmi Pad", "Redmi Pad 2 Pro 12.1 256GB", "Redmi Pad 2 Pro"),
    ("Xiaomi Pad", "Xiaomi Pad 7 Pro 12GB 256GB", "Xiaomi Pad 7 Pro"),
    ("POCO Pad", "POCO Pad X1 8GB 256GB", "POCO Pad X1"),
]
for sku, title, want in REFINE:
    got = skunorm.refine_series_sku(sku, title)
    ok(got == want, f"★ 细分：{sku!r} + {title[:50]!r} 应得 {want!r}，实得 {got!r}")

# 找不回型号的保留系列名（返回 None 由调用方标桶），尺寸不是型号
NO_REFINE = [
    ("Lenovo Tab", 'Tablet Lenovo Tab Full HD 10.1" Android 14 ZAEH0151BR'),
    ("Lenovo Tab", "Tablet lenovo 128GB + obsequio"),
    ("Lenovo Tab", "Lenovo Tablet Lenovo Tab MediaTek Helio G85 4GB RAM 128GB 10\""),
    ("Lenovo Tab", "Tablet Lenovo Tab P 12.7 pulgadas 256GB"),     # 12.7 是屏幕尺寸
    ("Samsung Galaxy Tab", "Tablet Samsung 11 pulgadas 128GB"),
    ("Samsung Galaxy Tab", "Tablet Samsung Galaxy Tab A 8.4 32GB"),  # 8.4 是尺寸
    ("Samsung Galaxy Tab", "SAMSUNG BATERIA PARA TABLET SAMSUNG TAB S5E 10.5 T725"),  # S5e ≠ S5
    ("Apple iPad", "Apple iPad 11 pulgadas 128GB"),                   # 没登记细分器
    # ★ 假朋友：2015 年的 Yoga Tab 3，16GB 是**存储**、RAM 只有 2GB。裸 `16gb` 会把它
    #   判成 2025 年的旗舰 Plus —— #2934 里真有这条（807 PEN）。
    ("Lenovo Yoga Tab", "TABLET YOGA TAB 3 8 ANDROID 5.1 16GB 2GB BT P-N ZA090081VE"),
]
for sku, title in NO_REFINE:
    got = skunorm.refine_series_sku(sku, title)
    ok(got is None, f"★ 不该细分：{sku!r} + {title[:50]!r} 应得 None，实得 {got!r}")

# 具体 SKU 不再二次加工（权威表说了算）
ok(skunorm.refine_series_sku("Lenovo Tab M10", "Tablet Lenovo Tab M10 Plus 128GB") is None,
   "具体 SKU（非 generic）不细分 —— 那是权威表的口径")
ok(skunorm.refine_series_sku(None, "x") is None and skunorm.refine_series_sku("Lenovo Tab", "") is None,
   "空输入不炸")

# 细分出的名字与表里直接命中的同键：M10 → 'lenovotabm10'
ok(skunorm.refine_series_sku("Lenovo Tab", "LENOVO Tablet M10 5G").lower().replace(" ", "")
   == "Lenovo Tab M10".lower().replace(" ", ""),
   "★ 细分名必须与 yaml 直接命中的产品同键，否则各建一个产品")


# ─────────────── 3. 桶判据（单一实现）───────────────
BV = skunorm.bucket_verdict
ok(BV("Lenovo Tab", "skumap") == (1, "sku_rules:generic"), "系列级权威 SKU → 桶")
ok(BV("Lenovo Tab M10", "skumap") == (0, None), "具体权威 SKU 不是桶")
ok(BV("Lenovo Tab One", "skumap") == (0, None), "★ Lenovo Tab One 是真单品不是桶")
ok(BV("Tab Full Hd", "nubimetrics/fallback") == (1, "nubimetrics:fallback"),
   "nubimetrics 前 4 词兜底名、无数字/变体、含描述词 → 桶")
for junk in ("Over Ear Hd", "Akg Tipo C", "Con Cancelacion Ruido", "In Ear Con",
             "Generico Compatible Con Samsung", "True Wireless Momentum", "Usb Tipo C"):
    ok(BV(junk, "nubimetrics/fallback") == (1, "nubimetrics:fallback"),
       f"★ 描述词组成的兜底名是桶：{junk!r}（全库 169 个里的真实样本）")
ok(BV("Sony", "nubimetrics/fallback", brand="Sony") == (1, "nubimetrics:fallback"),
   "型号名就是品牌名 → 什么都没剥出来 → 桶")
ok(BV('" Xiaomi Mi', "nubimetrics/fallback") == (1, "nubimetrics:fallback"), "残留引号开头 → 桶")
for real in ("iPhone Air", "iPhone Xr", "Studio Buds", "Solo Buds", "Powerbeats Fit",
             "Icon Xt ANC", "Vivomove Trend", "Fenix E", "Gts", "Openwear Stereo"):
    ok(BV(real, "nubimetrics/fallback") == (0, None),
       f"★ 真单品的兜底名不许标桶：{real!r} —— 误标会把它赶出趋势入口与竞品候选池")
ok(BV("Redmi A7", "nubimetrics/fallback") == (0, None), "兜底名含数字 → 不是桶")
ok(BV("Magic Pro", "nubimetrics/fallback") == (0, None), "兜底名含变体词 → 不是桶")
ok(BV("Tab Full Hd", None) == (0, None), "同名但来源不是 fallback → 不判（来源不明不猜）")
ok(BV("白牌-未识别", None) == (1, "whitelabel"), "白牌桶")
for base in ("Lenovo Idea Tab", "Lenovo Yoga Tab", "Redmi Pad"):
    ok(BV(base, "skumap") == (0, None),
       f"★ 系列基础款 {base!r} 不是桶 —— 误标它会把一款真机永久赶出单品消费方，"
       f"而人工改 is_bucket 会被 mark_buckets --apply 打回来")
ok(BV("Lenovo Legion Tab", "skumap") == (1, "sku_rules:generic"),
   "没有证据说剩下的是一款 → 仍按桶处理（不许顺手把没证据的也摘了）")
ok(BV("Lenovo Tab M10", "skumap", {"kind": "sku", "generic": True}) == (1, "sku_rules:generic"),
   "★ yaml 显式 generic: true 时以它为准")
ok(BV("Lenovo Tab", "skumap", {"kind": "sku", "generic": False}) == (0, None),
   "★ yaml 显式 generic: false 是纠正通道 —— 只认 true 的话误标就永远改不回来")
ok(BV("", None) == (0, None) and BV(None, None) == (0, None), "空名不炸")


# ─────────────── 4. identity_for：入库与重算共用的身份判定 ───────────────
def ident(title, sku_code="Lenovo Tab", brand="Lenovo", cat="tablet"):
    return identity_for({"title": title, "sku_code": sku_code, "brand_name": brand,
                         "category_code": cat, "aliases": []})


i = ident("LENOVO Tablet M10 5G Cel 10.6 pulgadas 128 GB")
ok(i["kind"] == "device" and i["model"] == "Lenovo Tab M10" and i["is_bucket"] == 0
   and i["source"] == "skumap/refined" and i["key"] == "lenovotabm10",
   f"★ 系列级 SKU 回标题细分：实得 {i}")

i = ident('Tablet Lenovo Tab Full HD 10.1" Android 14 ZAEH0151BR')
ok(i["kind"] == "device" and i["model"] == "Lenovo Tab" and i["is_bucket"] == 1
   and i["bucket_reason"] == "sku_rules:generic",
   f"★ 找不回型号 → 保留系列名 + is_bucket=1：实得 {i}")

# 2026-09-04 起 extract.accessory_para_form（权威表前置闸共用）认得 lámina/mica 这类主语位置强名词，
# 钢化膜在权威表这一层就判配件 —— 不再是「权威表整机 / 位置规则配件」的冲突，而是两边一致的配件。
i = ident("GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11 J606F")
ok(i["kind"] == "accessory" and i["conflict"] is False and i["source"] == "skumap" and not i["model"],
   f"★ 钢化膜带 sku_code='Lenovo Tab' 也不许挂产品（8,500 CLP 拽曲线的那条）；"
   f"权威表前置闸直接判配件（非冲突、不给型号）：实得 {i}")
# 冲突路径本身仍要被守住：配件词在主语窗口（4 词）之外、权威表命中具体 SKU、位置规则判配件
i = ident("Paquete 2 unidades Marca Genérica Soporte Ajustable para Tablet Lenovo Tab M10")
ok(i["kind"] == "accessory" and i["conflict"] is True and i["model"] == "Lenovo Tab M10",
   f"★ 权威表整机 / 位置规则配件 → 标成「冲突」而不是定案配件（三态）：实得 {i}")
i = ident("Funda para Tablet Lenovo Tab M10 Plus")
ok(i["kind"] == "accessory" and i["conflict"] is False,
   f"权威表自己判的配件不是冲突：实得 {i}")

i = ident("Tablet Lenovo Tab M11 128GB 4GB RAM + Lapiz")
ok(i["kind"] == "device" and i["model"] == "Lenovo Tab M11" and i["is_bucket"] == 0,
   f"平板+笔套装是整机，且按当前 yaml 规则直接命中 M11：实得 {i}")

i = ident("Galaxy Tab S9+ Book Cover Keyboard 256GB", sku_code="Samsung Galaxy Tab S9+", brand="Samsung")
ok(i["kind"] == "device" and i["model"] == "Samsung Galaxy Tab S9+",
   f"含 gb 的 book cover 是整机捆绑（yaml 语义）：实得 {i}")

i = ident("Funda para Tablet Lenovo Tab M10 Plus", sku_code=None)
ok(i["kind"] == "accessory", f"权威表判配件 → accessory：实得 {i}")

i = ident("Samsung Galaxy S25 Ultra 512GB Negro Desbloqueado", sku_code=None,
          brand="Samsung", cat="phone")
ok(i["kind"] == "device" and i["model"] and i["is_bucket"] == 0 and i["key"],
   f"非平板走 nubimetrics/本地兜底，正常出名：实得 {i}")

i = ident("Xiaomi Redmi Pad 2 128GB", sku_code="Redmi Pad", brand="Xiaomi")
ok(i["model"] == "Redmi Pad 2" and i["is_bucket"] == 0, f"Redmi Pad 系列细分到 Redmi Pad 2：实得 {i}")

i = ident("Lenovo Tablet Yoga Snapdragon 8 G3 16GB RAM 256GB 12.7'' 144Hz")
ok(i["kind"] == "device" and i["model"] == "Lenovo Yoga Tab Plus" and i["source"] == "skumap/refined"
   and i["is_bucket"] == 0,
   f"★ 12.7\"/16GB RAM 是 Plus 的规格：这 12 条不许再并进 11.1\"/12GB 的基础款 #2934：实得 {i}")
i = ident("Lenovo Tablet Yoga Tab 12GB RAM 256GB 11 pulgadas + Teclado")
ok(i["kind"] == "device" and i["model"] == "Lenovo Yoga Tab" and i["is_bucket"] == 0,
   f"★ 基础款 Yoga Tab 是一款真机，不是桶（细分照跑、桶标不打）：实得 {i}")
i = ident("LENOVO Tablet Legion Tab 8.8 2560x1600 256GB 12GB RAM Negro")
ok(i["kind"] == "device" and i["model"] == "Lenovo Legion Tab" and i["is_bucket"] == 1
   and i["bucket_reason"] == "sku_rules:generic",
   f"★ 细分出来的名字本身仍是系列级（Legion Tab ⊂ Legion Tab Gen 3）→ 更窄但仍是桶：实得 {i}")
i = ident("Tablet Lenovo Tab P12 Pro Snapdragon 8GB RAM 256GB 12.6")
ok(i["is_bucket"] == 0 and i["model"] == "Lenovo Tab P12 Pro", f"细分出的具体型号不是桶：实得 {i}")

# 权威表判整机、位置规则判配件 → 冲突；但 yaml 自己就判配件的（capa para）不是冲突
i = ident("Capa para Tablet Lenovo Tab M7 - Skull Armor - Gshield")
ok(i["kind"] == "accessory" and i["conflict"] is False and i["source"] == "skumap",
   f"葡语 capa para 由权威表前置闸判配件（非冲突）：实得 {i}")


# ─────────────── 5. 单品消费方排除桶：源码级断言走 ast，且**跳过 docstring** ───────────────
def _find_fn(src: str, func: str):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            return node
    return None


def _all_str_constants(src: str, func: str) -> str:
    """★ 旧写法（本轮之前就是这个）：函数内**全部** str 常量，docstring 也算进去。
    留在这里当反面教材 —— 下面会证明它守不住任何东西。"""
    fn = _find_fn(src, func)
    return "" if fn is None else "\n".join(
        n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str))


def _sql_constants(src: str, func: str) -> str:
    """函数内真的进了 SQL 的字符串常量 —— **跳过 docstring**（函数自己的和内嵌函数的）。

    ★ 为什么必须跳：app/dashboard.py trackable_products 的 docstring 里就写着
      「排除桶（rp.is_bucket=1）」。收全部 str 常量的话，把 WHERE 子句整条删掉，
      断言照样绿 —— 断言被自己写的解释文字击穿，这是本项目第五次同类
      （knowledge/lessons/assertions-that-verify-nothing：越重要的性质旁边注释写得越认真，
      注释越认真断言越容易失效）。
    """
    fn = _find_fn(src, func)
    if fn is None:
        return ""
    skip = set()
    for sub in ast.walk(fn):
        body = getattr(sub, "body", None)
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            skip.add(id(body[0].value))
    return "\n".join(n.value for n in ast.walk(fn)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip)


for path, func in ((ROOT / "app/dashboard.py", "trackable_products"),
                   (ROOT / "app/matching/matcher.py", "_candidates"),
                   (ROOT / "app/trends.py", "suggest_watch")):
    src = path.read_text(encoding="utf-8")
    ok("is_bucket" in _sql_constants(src, func),
       f"★ {path.name}:{func} 的 SQL 常量里必须有 is_bucket 闸（ast 查，且不算 docstring）")
    # ★ 反向自检：把闸**真的删掉一次**，同一条断言必须变红。不会红的断言等于没写。
    #   只删 SQL 子句那一行，docstring/注释里提到 is_bucket 的照留 —— 这才是原来的现场。
    mutated = "\n".join(ln for ln in src.splitlines() if "COALESCE(rp.is_bucket" not in ln)
    ok("is_bucket" not in _sql_constants(mutated, func),
       f"★ 反向自检：删掉 {func} 的 COALESCE(rp.is_bucket…) 之后断言必须失败，实际它仍然通过")

# ★ 把"被 docstring 击穿"这件事本身钉住（用合成源码，不依赖别的文件当时怎么写注释）
_TRAP_SRC = (
    "def f(country=''):\n"
    '    """哪些产品能看趋势。★ 排除桶（rp.is_bucket=1）：桶不是一款产品。"""\n'
    "    where = [\"po.sale_price IS NOT NULL\"]\n"
    "    return db.q('SELECT 1 FROM price_obs po WHERE ' + ' AND '.join(where))\n")
ok("is_bucket" in _all_str_constants(_TRAP_SRC, "f"),
   "★ 旧写法（含 docstring）在**没有任何闸**的源码上照样判绿 —— 它守的是注释")
ok("is_bucket" not in _sql_constants(_TRAP_SRC, "f"),
   "★ 新写法跳过 docstring：没有真闸就必须判红")


# ─────────────── 6. 临时库：迁移幂等 + 桶真的被排除 ───────────────
db.init_db()
db.init_db()                                              # 第二次必须是空操作
cols = {r["name"] for r in db.q("PRAGMA table_info(rival_product)")}
ok({"is_bucket", "bucket_reason"} <= cols, f"迁移应加上两列，实得 {sorted(cols & {'is_bucket', 'bucket_reason'})}")
ok(sum(1 for t, c, _ in db.MIGRATIONS if (t, c) == ("rival_product", "is_bucket")) == 1,
   "MIGRATIONS 里 is_bucket 只登记一次")

with db.tx() as c:
    c.execute("INSERT OR IGNORE INTO country(code,name_zh,currency,lang,locale,timezone) "
              "VALUES('CL','智利','CLP','es','es-CL','America/Santiago')")
    c.execute("INSERT OR IGNORE INTO category(code,name_zh,enabled) VALUES('tablet','平板',1)")
    c.execute("UPDATE category SET enabled=1 WHERE code='tablet'")
    c.execute("INSERT OR IGNORE INTO channel(id,code,country_code,name,kind) "
              "VALUES(1,'rp','CL','Ripley','retailer')")
    c.execute("INSERT OR IGNORE INTO brand(id,name,is_ours) VALUES(9,'Lenovo',0)")
    c.execute("INSERT INTO rival_product(id,brand_id,category_code,model_name,model_key,is_bucket,bucket_reason) "
              "VALUES(101,9,'tablet','Lenovo Tab','lenovotab',1,'sku_rules:generic')")
    c.execute("INSERT INTO rival_product(id,brand_id,category_code,model_name,model_key) "
              "VALUES(102,9,'tablet','Lenovo Tab M10','lenovotabm10')")
    for i, (pid, price) in enumerate([(101, 8500), (101, 159990), (101, 179990),
                                      (102, 149990), (102, 152990), (102, 149990)]):
        c.execute("""INSERT INTO price_obs(id,obs_date,country_code,channel_id,brand_id,category_code,
                       rival_product_id,title,sale_price,currency,product_kind,audit_status,
                       is_in_stock,condition,is_bundle,row_hash)
                     VALUES(?,date('now',?),'CL',1,9,'tablet',?,'t',?,'CLP','device','pending',1,'new',0,?)""",
                  (i + 1, f"-{i % 3} day", pid, price, f"h{i}"))

from app import dashboard                                # noqa: E402
from app.matching.matcher import CompetitorMatcher       # noqa: E402

ids = {r["id"] for r in dashboard.trackable_products(days=30)}
ok(102 in ids and 101 not in ids, f"★ trackable_products 应只返回非桶 #102，实得 {sorted(ids)}")
cands = {r["id"] for r in CompetitorMatcher()._candidates("tablet", "CL", "CLP")}
ok(102 in cands and 101 not in cands, f"★ matcher._candidates 应只返回非桶 #102，实得 {sorted(cands)}")

# ★★ 用户 2026-09-04 截图那页的下拉走的是这个（/api/trend/candidates → suggest_watch），
#   上一轮只给 trackable_products 加了闸，桶照样排在下拉第 6 位。
from app import trends                                 # noqa: E402

sug = {r["id"] for r in trends.suggest_watch(50)}
ok(102 in sug and 101 not in sug,
   f"★ suggest_watch（曲线页下拉）应只返回非桶 #102，实得 {sorted(sug)}")

# _upsert_rival：新建带标记；已有行只允许 1→0
pid, new = CleanerAgent._upsert_rival(9, "Lenovo Idea Tab", "lenovoideatab", "tablet",
                                      name_source="skumap", is_bucket=1, bucket_reason="sku_rules:generic")
row = db.q1("SELECT is_bucket, bucket_reason, name_source FROM rival_product WHERE id=?", (pid,))
ok(new and row["is_bucket"] == 1 and row["bucket_reason"] == "sku_rules:generic"
   and row["name_source"] == "skumap", f"新建产品带桶标记与来源，实得 {row}")
pid2, new2 = CleanerAgent._upsert_rival(9, "Lenovo Tab M10", "lenovotabm10", "tablet",
                                        is_bucket=1, bucket_reason="x")
row = db.q1("SELECT is_bucket FROM rival_product WHERE id=?", (pid2,))
ok(pid2 == 102 and not new2 and row["is_bucket"] == 0,
   "★ 已有行不许 0→1 自动升级（打标交给 tools/mark_buckets.py）")
pid3, _ = CleanerAgent._upsert_rival(9, "Lenovo Tab", "lenovotab", "tablet", is_bucket=0)
row = db.q1("SELECT is_bucket, bucket_reason FROM rival_product WHERE id=?", (pid3,))
ok(pid3 == 101 and row["is_bucket"] == 0 and row["bucket_reason"] is None,
   "已有桶行遇到非桶身份 → 1→0 摘标（细分成功）")

# ─────────────── 7. 拆桶工具：计划 → apply → 幂等 → rollback 往返（临时库）───────────────
# 没被执行过的回滚路径等于没有（knowledge/lessons：回滚清单文件名按秒命名同秒覆盖那次
# 就是这么抓出来的）。全部标题取自 #2292 的真实观测。
import tools.resplit_buckets as resplit                  # noqa: E402
import tools.mark_buckets as markb                       # noqa: E402

resplit.RECOMPUTE = False          # 临时库上不跑 PriceMoveAgent / rebuild_all
resplit.ROLLBACK_DIR = _TMP        # 回滚清单不落进项目 data/backfill
markb.ROLLBACK_DIR = _TMP
# ★ 采集闸在 §8 单独测（它要联网查 /api/health，放这里会让本节的结果取决于
#   本机此刻在不在采集 —— 测试必须是确定的）。
resplit.FORCE = markb.FORCE = True

with db.tx() as c:
    # 8,500：冲突（权威表命中 M10 判整机、位置规则判配件）。钢化膜原样本改放 id 13/14 ——
    # 2026-09-04 起它在权威表前置闸就判配件，走的是「配件摘出」不是「冲突待定」。
    c.execute("UPDATE price_obs SET title=? WHERE id=1",
              ("Paquete 2 unidades Marca Genérica Soporte Ajustable para Tablet Lenovo Tab M10",))
    c.execute("UPDATE price_obs SET title=? WHERE id=2",
              ("LENOVO Tablet M10 5G Cel 10.6 pulgadas 128 GB de 6 GB RAM",))               # → 并入 #102
    c.execute("UPDATE price_obs SET title=? WHERE id=3",
              ("Tablet Lenovo Tab P11 2nd Gen 6GB+128GB WiFi 11.5 Gris",))                  # → 新建 P11
    c.execute("UPDATE price_obs SET title=? WHERE id IN (4,5,6)", ("Tablet Lenovo Tab M10 128GB",))
    more = [(7, 149990, 'Tablet Lenovo Tab Full HD 10.1" Android 14 ZAEH0151BR', "device"),   # 留桶
            (8, 19990, "Capa para Tablet Lenovo Tab M7 - Skull Armor - Gshield", "device"),    # 配件摘出
            (9, 799990, "Lenovo Tablet Yoga Snapdragon 8 G3 16GB RAM 256GB 12.7'' 144Hz", "device"),  # → Yoga Tab Plus（新建）
            (10, 24990, "Tablet Lenovo Tab K10 64GB Wifi", "device"),                          # 低尾（整机价离谱低）
            (11, 139990, "Tablet Lenovo Tab 128GB + Protector", "device"),                      # 留桶（无型号）
            (12, 9990, "Funda Lenovo Tab M10 Plus 10.6 Negro", "accessory"),                    # 历史已判配件仍挂着
            (13, 8500, "GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11 J606F- M.T", "device"),  # 配件摘出
            (14, 24990, "ROCK SPACE LAMINA HIDROGEL PARA TABLET LENOVO TAB K10", "device"),     # 配件摘出
            (15, 449990, "LENOVO Tablet Legion Tab 8.8 2560x1600 256GB 12GB RAM Negro", "device")]  # 桶→桶
    for oid, price, title, kind in more:
        c.execute("""INSERT INTO price_obs(id,obs_date,country_code,channel_id,brand_id,category_code,
                       rival_product_id,title,sale_price,currency,product_kind,audit_status,
                       is_in_stock,condition,is_bundle,row_hash,sku_code)
                     VALUES(?,date('now'),'CL',1,9,'tablet',101,?,?,'CLP',?,'pending',1,'new',0,?,'Lenovo Tab')""",
                  (oid, title, price, kind, f"h{oid}"))


def _plan(pid=101):
    cands = resplit.candidates({pid})
    kidx = {(r["brand_id"], r["model_key"], r["category_code"]): r
            for r in db.q("SELECT id, brand_id, model_key, category_code, model_name FROM rival_product")}
    return resplit.plan_product(cands[0], kidx), cands[0]


pl, cand = _plan()
ok(cand["bucket_reason"] == "sku_rules:generic", f"点名产品也要带判据理由，实得 {cand['bucket_reason']!r}")
ok(set(pl["stay"]) == {7, 11, 15},
   f"★ 留桶应为 ZAEH0151BR(7)、无型号(11) 与 Legion(15，目标桶不存在不新建)，实得 {sorted(pl['stay'])}")
ok([d["id"] for d in pl["pending"]] == [1], f"支架套装（权威表整机/位置规则配件）→ 冲突待定，实得 {pl['pending']}")
ok(sorted(d["id"] for d in pl["detach"]) == [8, 12, 13, 14],
   f"capa para + 历史已判配件 + 钢化膜/水凝膜（权威表前置闸判配件）→ 摘出，实得 {pl['detach']}")
ok(any(d["id"] == 12 and d["why"] == resplit._HIST_ACC_WHY for d in pl["detach"]),
   "★ product_kind 已是 accessory 的行不许再被 identity_for 当整机重挂（#6938 51 条贴膜差点建出 Xiaomi Pad 4）")
ok([d["id"] for d in pl["lowtail"]] == [10] and pl["lowtail"][0]["new"] == "Lenovo Tab K10",
   f"整机价 24,990 < 20% 中位 → 低尾留桶，不拿它新建 K10，实得 {pl['lowtail']}")
g = pl["groups"]
ok(set(g) == {"9|lenovotabm10|tablet", "9|lenovotabp11|tablet", "9|lenovoyogatabplus|tablet"},
   f"分组键应逐字对齐 UNIQUE 键，实得 {sorted(g)}")
ok(g["9|lenovotabm10|tablet"]["target_id"] == 102 and [o["id"] for o in g["9|lenovotabm10|tablet"]["obs"]] == [2],
   "M10 观测并入已有 #102（不新建、不撞键）")
ok(g["9|lenovotabp11|tablet"]["target_id"] is None and g["9|lenovotabp11|tablet"]["is_bucket"] == 0,
   "P11 无同键产品 → 新建，且不是桶")
ok(g["9|lenovoyogatabplus|tablet"]["is_bucket"] == 0
   and [o["id"] for o in g["9|lenovoyogatabplus|tablet"]["obs"]] == [9],
   f"★ 12.7\"/16GB RAM 的 Yoga 观测归 Yoga Tab Plus（真单品，可新建），"
   f"实得 {g['9|lenovoyogatabplus|tablet']}")
ok(pl["before"].get("CLP") and pl["before"]["CLP"]["n"] == 11 and pl["before"]["CLP"]["raw"] > 90,
   f"价差体检按币种、只算库里非配件非捆绑（11 条，8,500↔799,990）：实得 {pl['before']}")

# 桶→桶：目标桶一旦存在（更窄），Legion 观测应并入它。
# ★ 用 Legion 演这条：Legion Tab 的兄弟 Legion Tab Gen 3 是真的世代差异，判据仍认它是桶；
#   Yoga Tab / Idea Tab / Redmi Pad 已按证据摘成系列基础款（见 §1/§3）。
with db.tx() as c:
    c.execute("INSERT INTO rival_product(id,brand_id,category_code,model_name,model_key,is_bucket,bucket_reason) "
              "VALUES(104,9,'tablet','Lenovo Legion Tab','lenovolegiontab',1,'sku_rules:generic')")
pl, _ = _plan()
ok(set(pl["stay"]) == {7, 11} and g != pl["groups"]
   and pl["groups"].get("9|lenovolegiontab|tablet", {}).get("target_id") == 104,
   f"★ 目标桶存在时 Legion 观测并入 #104（更窄的桶是真收益），实得 stay={sorted(pl['stay'])} "
   f"groups={sorted(pl['groups'])}")

# apply：先落回滚清单再改库
derived = resplit.derived_impact([pl])
resplit.apply([pl], derived)
rb = sorted(_TMP.glob("resplit_buckets_*.json"))
ok(len(rb) == 1, f"回滚清单应恰好落盘一份，实得 {rb}")
row = lambda oid: db.q1("SELECT rival_product_id AS pid, model_guess AS mg, product_kind AS pk FROM price_obs WHERE id=?", (oid,))  # noqa: E731
ok(row(2) == {"pid": 102, "mg": "Lenovo Tab M10", "pk": "device"}, f"M10 观测已并入 #102，实得 {row(2)}")
new_p = db.q1("SELECT id, model_name, is_bucket, name_source, bucket_reason FROM rival_product WHERE model_key='lenovotabp11'")
ok(new_p and new_p["model_name"] == "Lenovo Tab P11" and new_p["is_bucket"] == 0
   and new_p["name_source"] == "skumap/refined" and new_p["bucket_reason"] is None, f"新建 P11 产品：实得 {new_p}")
ok(row(3)["pid"] == new_p["id"] and row(3)["mg"] == "Lenovo Tab P11", f"P11 观测挂到新产品，实得 {row(3)}")
ok(row(1) == {"pid": None, "mg": None, "pk": "unknown"}, f"冲突待定 → 置空 + unknown（三态，不定案配件），实得 {row(1)}")
ok(row(8) == {"pid": None, "mg": None, "pk": "accessory"}, f"配件摘出 → 置空 + accessory，实得 {row(8)}")
ok(row(12) == {"pid": None, "mg": None, "pk": "accessory"}, f"历史已判配件 → 只摘挂接，实得 {row(12)}")
ok(row(13) == {"pid": None, "mg": None, "pk": "accessory"} and row(14) == {"pid": None, "mg": None, "pk": "accessory"},
   f"★ 钢化膜/水凝膜 → 置空 + accessory（8,500 CLP 不再拽 #2292 的曲线），实得 {row(13)} {row(14)}")
yoga_p = db.q1("SELECT id, model_name, is_bucket FROM rival_product WHERE model_key='lenovoyogatabplus'")
ok(yoga_p and yoga_p["model_name"] == "Lenovo Yoga Tab Plus" and yoga_p["is_bucket"] == 0
   and row(9)["pid"] == yoga_p["id"],
   f"★ Yoga 观测挂到新建的 Yoga Tab Plus（不再进 11.1\" 的基础款），实得 {yoga_p} {row(9)}")
ok(row(15)["pid"] == 104 and row(7)["pid"] == 101 and row(10)["pid"] == 101 and row(11)["pid"] == 101,
   f"Legion→#104、ZAEH/无型号/低尾整机留在 #101，实得 {row(15)} {row(7)} {row(10)} {row(11)}")

# 幂等：第二遍的处理集合必须变小到空
pl2, _ = _plan()
ok(not pl2["groups"] and not pl2["detach"] and not pl2["pending"] and set(pl2["stay"]) == {7, 11},
   f"★ 跑两遍，第二遍应无事可做（否则跳过逻辑没生效），实得 groups={list(pl2['groups'])} "
   f"detach={pl2['detach']} pending={pl2['pending']} stay={pl2['stay']}")

# rollback：观测归位、新建产品删除、留桶的没被碰
n_before = db.q1("SELECT COUNT(*) c FROM rival_product")["c"]
resplit.rollback(str(rb[0]))
ok(row(2) == {"pid": 101, "mg": None, "pk": "device"} and row(3)["pid"] == 101
   and row(1) == {"pid": 101, "mg": None, "pk": "device"} and row(8) == {"pid": 101, "mg": None, "pk": "device"}
   and row(9)["pid"] == 101 and row(12) == {"pid": 101, "mg": None, "pk": "accessory"}
   and row(13) == {"pid": 101, "mg": None, "pk": "device"} and row(14) == {"pid": 101, "mg": None, "pk": "device"},
   f"★ 回滚后全部观测归位到 #101（含历史配件行的 product_kind 原样），实得 {[row(i) for i in (1, 2, 3, 8, 9, 12, 13, 14)]}")
ok(db.q1("SELECT COUNT(*) c FROM rival_product WHERE model_key IN ('lenovotabp11','lenovoyogatabplus')")["c"] == 0
   and db.q1("SELECT COUNT(*) c FROM rival_product")["c"] == n_before - 2,
   "回滚删掉了本次新建的 P11 与 Yoga Tab Plus（且只删它们）")


# ─────────── 7b. 回滚的外键安全：CASCADE 会静默删评论、无 CASCADE 会让整份回滚失效 ───────────
# 现场：apply 与 rollback 之间流水线照常在跑，给新建产品挂上了一条评论 / 一条策略信号。
#   · review 是 ON DELETE CASCADE ⇒ 删产品 = **静默**删评论（评论按存档铁律不可再生）
#   · strategy_signal 没有 CASCADE ⇒ 抛 IntegrityError ⇒ 事务回滚 ⇒ 连观测归位一起没了，
#     而"事务回滚"与"什么都没干"在日志里长得一模一样。
pl3, _ = _plan()
resplit.apply([pl3], resplit.derived_impact([pl3]))
rb2 = [p for p in sorted(_TMP.glob("resplit_buckets_*.json")) if p != rb[0]]
ok(len(rb2) == 1, f"第二次 apply 应再落一份回滚清单，实得 {rb2}")
p11_id = db.q1("SELECT id FROM rival_product WHERE model_key='lenovotabp11'")["id"]
yoga_id = db.q1("SELECT id FROM rival_product WHERE model_key='lenovoyogatabplus'")["id"]
with db.tx() as c:
    c.execute("INSERT INTO review(rival_product_id,country_code,content,content_hash) VALUES(?,?,?,?)",
              (p11_id, "CL", "Excelente tablet, la batería dura todo el día", "rb-hash-1"))
    c.execute("INSERT INTO strategy_signal(signal_date,country_code,rival_product_id,signal_type,summary_zh) "
              "VALUES(date('now'),'CL',?,'clearance','老品清库')", (yoga_id,))
try:
    resplit.rollback(str(rb2[0]))
    raised = None
except Exception as e:  # noqa: BLE001
    raised = e
ok(raised is None,
   f"★ 回滚不许抛：strategy_signal 没有 CASCADE，删产品会 IntegrityError 让整份回滚原地失效，实得 {raised!r}")
ok(db.q1("SELECT COUNT(*) c FROM review WHERE content_hash='rb-hash-1'")["c"] == 1,
   "★ 评论不许被 ON DELETE CASCADE 静默删掉（评论不可再生）")
ok(db.q1("SELECT COUNT(*) c FROM strategy_signal WHERE rival_product_id=?", (yoga_id,))["c"] == 1,
   "★ 策略信号还在")
ok(db.q1("SELECT COUNT(*) c FROM rival_product WHERE id IN (?,?)", (p11_id, yoga_id))["c"] == 2,
   "★ 被引用的新建产品保留不删（留个空产品是脏，删错是丢数据）")
ok(row(2)["pid"] == 101 and row(3)["pid"] == 101 and row(9)["pid"] == 101 and row(15)["pid"] == 101,
   f"★ 观测照常归位 —— 回滚没有被 IntegrityError 整份撤销，实得 {[row(i) for i in (2, 3, 9, 15)]}")

# 组外占键：apply 复用的既有产品绝不能进 created_ids（否则回滚连它一起删）
rbdata = json.loads(rb2[0].read_text(encoding="utf-8"))
ok(102 not in rbdata["created_ids"] and 104 not in rbdata["created_ids"],
   f"★ 并入目标（既有 #102/#104）不许出现在 created_ids，实得 {rbdata['created_ids']}")
ok(all(nk.get("pre_existing_id") is None for nk in rbdata["new_products"]),
   f"本次两个新键在 apply 之前都不存在，pre_existing_id 应为 None，实得 {rbdata['new_products']}")

# ─────────────── 8. mark_buckets：同一份判据 · 人工裁定不许被翻 · 写库前过采集闸 ───────────────
hits, rows_all = markb.scan()
hit_ids = {h["id"] for h in hits}
ok({101, 104} <= hit_ids and 102 not in hit_ids and pid not in hit_ids,
   f"mark_buckets.scan 应标 101/104（Lenovo Tab / Legion Tab 是桶）、不标 102，"
   f"也不标 #{pid}（Lenovo Idea Tab 是系列基础款），实得 {sorted(hit_ids)}")
ok(all(h["_reason"] == "sku_rules:generic" for h in hits if h["id"] in (101, 104)),
   "判据理由与 identity_for / cleaner 同一份：sku_rules:generic")

changes, stale, manual = markb._changes_and_stale(hits, rows_all)
ok(any(r["id"] == pid for r in stale) and not manual,
   f"★ 按旧判据标着 is_bucket=1 的 Idea Tab 进摘标清单，实得 stale={[r['id'] for r in stale]}")

# ★★ 人工裁定（bucket_reason 以 manual: 开头）两个方向都不许被判据翻 ——
#   没有这条通道，人工把误标改回 0 会被下一次 --apply 打回 1，**误标不可纠正**。
with db.tx() as c:
    c.execute("UPDATE rival_product SET is_bucket=0, "
              "bucket_reason='manual:2026-09-07 人工核过，是一款真机' WHERE id=101")
    c.execute("UPDATE rival_product SET is_bucket=1, "
              "bucket_reason='manual:2026-09-07 人工判定装了两代' WHERE id=102")
hits, rows_all = markb.scan()
changes, stale, manual = markb._changes_and_stale(hits, rows_all)
ok({r["id"] for r in manual} == {101, 102}, f"人工裁定行应被认出来，实得 {[r['id'] for r in manual]}")
ok(101 not in {h["id"] for h in changes}, "★ 人工摘标的 #101 不许被判据打回 1")
ok(102 not in {r["id"] for r in stale}, "★ 人工打标的 #102 不许被判据摘掉")
markb.apply(hits, rows_all)
a101 = db.q1("SELECT is_bucket, bucket_reason FROM rival_product WHERE id=101")
a102 = db.q1("SELECT is_bucket, bucket_reason FROM rival_product WHERE id=102")
ok(a101["is_bucket"] == 0 and a101["bucket_reason"].startswith("manual:")
   and a102["is_bucket"] == 1 and a102["bucket_reason"].startswith("manual:"),
   f"★ --apply 之后人工裁定原样保留（理由也不许被判据覆盖），实得 {a101} {a102}")
ok(db.q1("SELECT is_bucket FROM rival_product WHERE id=?", (pid,))["is_bucket"] == 0,
   "非人工的旧标记照常摘掉（判据不再认 Lenovo Idea Tab 是桶）")
ok(db.q1("SELECT is_bucket FROM rival_product WHERE id=104")["is_bucket"] == 1, "#104 照常打标")


# ─────────────── 9. 采集闸：写库路径一律先问"采集在不在跑" ───────────────
def _calls_gate(path: Path, func: str) -> bool:
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.FunctionDef) and n.name == func:
            return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "_gate" for c in ast.walk(n))
    return False


for _p, _f in ((ROOT / "tools/resplit_buckets.py", "apply"),
               (ROOT / "tools/resplit_buckets.py", "rollback"),
               (ROOT / "tools/mark_buckets.py", "apply"),
               (ROOT / "tools/mark_buckets.py", "rollback")):
    ok(_calls_gate(_p, _f), f"★ {_p.name}:{_f} 写库前必须调 _gate（采集中改库会与采集侧抢锁）")


def _boom():
    raise RuntimeError("服务查不通")


for _mod, _nm in ((resplit, "resplit_buckets"), (markb, "mark_buckets")):
    _real_probe = _mod._collecting_now
    _mod.FORCE = False
    _mod._collecting_now = lambda: (True, "测试：假装 /api/health task_running=true")
    _code = None
    try:
        _mod._gate("--apply")
    except SystemExit as e:
        _code = e.code
    ok(_code == 2, f"★ {_nm}：采集中 _gate 必须 exit 2，实得 {_code}")
    _mod._collecting_now = _boom
    _code = None
    try:
        _mod._gate("--apply")
    except SystemExit as e:
        _code = e.code
    ok(_code == 2, f"★ {_nm}：查不出采集状态也要拒跑（不确定就不写库），实得 {_code}")
    _mod.FORCE = True
    _mod._gate("--apply")                      # --force 放行，不许抛
    _mod._collecting_now = _real_probe

print(f"bucket: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
