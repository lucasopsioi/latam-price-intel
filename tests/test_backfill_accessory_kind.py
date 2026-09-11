# -*- coding: utf-8 -*-
"""product_kind 回填工具的 apply / rollback 往返 —— 在**临时库**上跑，绝不碰 intel.db。

守的性质（每条对应一次真实事故）：
  1. judge() 走的是采集端同一条路径（base._enrich_from_title），不自带词表 —— ast 断言。
  2. 捆绑整机（Slate 12X with keyboard / iPad + Apple Pencil）不许被回填成配件（高价端误杀）。
  3. 三态：待定/冲突默认只报告，不定案配件；开关打开才写 unknown。
  4. 回滚清单先落盘、存改前值；同秒两次 apply 文件名不同；rollback 逐行归位；第二遍扫描为空。

★ 必须在 db 建立第一个连接之前改掉 config.DB_PATH，否则会安静地在生产库上跑。

跑法： python tests\\test_backfill_accessory_kind.py
"""
import ast
import importlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config                          # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="acckind_"))
config.DB_PATH = _TMP / "test.db"                # ★ 必须早于任何 db.get_conn()

from app import db                               # noqa: E402

backfill = importlib.import_module("tools.backfill_accessory_kind")
backfill.ROLLBACK_DIR = _TMP / "rollback"
# ★ 采集闸的服务侧打桩：真 collecting_now() 会去敲 127.0.0.1:8765/api/health，
#   测试机上服务在不在跑是随机的，不打桩这个测试的结果就取决于环境（闸门专测在下面单独翻开关）。
backfill.collecting_now = lambda: (False, "测试打桩：无采集")

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


# ---------------------------------------------------------------- 建库 + 造数
db.init_db()
with db.tx() as c:
    for code, cur in (("CL", "CLP"), ("MX", "MXN"), ("PE", "PEN"), ("CO", "COP")):
        c.execute("INSERT OR IGNORE INTO country(code,name_zh,currency,lang,locale,timezone) "
                  "VALUES(?,?,?,'es','es','America/Santiago')", (code, code, cur))
    for code in ("phone", "tablet", "audio", "wearable", "pc"):
        c.execute("INSERT OR IGNORE INTO category(code,name_zh) VALUES(?,?)", (code, code))
    c.execute("INSERT OR IGNORE INTO channel(id,code,country_code,name,kind) VALUES(1,'rp','CL','Ripley','retailer')")
    # ★ init_db 会把 schema.sql 里 31 个渠道种进来，id 1/2 早被占用（OR IGNORE 会静默跳过）—— 用 9xx
    c.execute("INSERT INTO channel(id,code,country_code,name,kind) VALUES(901,'hw_t','CO','Acme商城 CO','brand_store')")
    c.execute("INSERT OR IGNORE INTO brand(id,name) VALUES(1,'Lenovo')")

CUR = {"CL": "CLP", "MX": "MXN", "PE": "PEN", "CO": "COP"}
ROWS = [
    # (id, 国家, 品类, 价格, 标题, 库里 product_kind, 期望桶)
    (1, "CL", "tablet", 8_500, "GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11 J606F- M.T", "device", "flip"),
    (2, "CL", "tablet", 13_990, "ROCK SPACE LAMINA HIDROGEL PARA TABLET LENOVO PAD PLUS", "device", "flip"),
    (3, "CL", "tablet", 749_990, "Acme Slate 12X 2025 Green with keyboard inbox+3rd pencil", "device", "bundle_guard"),
    (4, "PE", "tablet", 2_099, "APPLE IPAD A16 2025 128GB WIFI - PINK + APPLE PENCIL USB-C ORIGINAL", "device", "bundle_guard"),
    (5, "CL", "tablet", 284_990, "Tablet Lenovo Tab M10 Plus 128GB", "device", None),
    (6, "CO", "audio", 79_900, "Audífonos Bluetooth Compatible Para Samsung Galaxy Buds 3 (Genéricos)", "device", "pending"),
    (7, "CL", "tablet", 15_990, "Paquete 2 unidades Marca Genérica Soporte Ajustable para Tablet Lenovo Tab M10", "device", "sku_conflict"),
    (8, "CL", "tablet", 9_990, "Funda Lenovo Tab M10 Plus 10.6 Negro", "accessory", None),     # 不在扫描范围
    (9, "CL", "tablet", 107_990, "ZAGG TECLADO CON FUNDA PRO KEYS CONNECT PARA IPAD AIR 11'", "device", "flip"),
    (10, "CL", "phone", 449_990, "Reacondicionado APPLE iPhone 13 128GB", "device", None),
    (11, "CL", "tablet", 39_990, "4 cuotas sin interés GENÉRICO FUNDA BOLSO SLIM ELEGANTE PARA TABLET ACME SLATETAB 11", "device", "flip"),
    (12, "MX", "tablet", 687, "Xiaomi Pad Mini funda protectora con correa de mano", "device", "pending"),  # SKU 短路 + detect 待定
    (13, "CO", "phone", 8_999_900, "Astra X7 16GB+512GB", "device", None),               # Acme商城：品牌店非配件即整机，不算待定
    (14, "CO", "phone", 200_000, "martphones Celulares básicos Repuestos de celulares Marca Xiaomi Samsung Apple Motorola Honor Inf", "device", "pending"),  # 页面导航截断
    (15, "CO", "phone", 25_900, "Charging para WATCH FIT", "device", "flip"),           # Acme商城的配件仍要翻
]
with db.tx() as c:
    for i, cc, cat, price, title, kind, _ in ROWS:
        c.execute("""INSERT INTO price_obs(id,obs_date,country_code,channel_id,brand_id,category_code,title,
                       sale_price,currency,product_kind,row_hash,condition,is_bundle,audit_status)
                     VALUES(?,'2026-09-04',?,?,1,?,?,?,?,?,?,'new',0,'pending')""",
                  (i, cc, 901 if i in (13, 14, 15) else 1, cat, title, price, CUR[cc], kind, f"h{i}"))

snap = lambda: {r["id"]: r["product_kind"] for r in db.q("SELECT id, product_kind FROM price_obs")}  # noqa: E731
BEFORE = snap()

# ---------------------------------------------------------------- 干跑：分桶
buckets, total = backfill.scan()
check("扫描分母 = device 行数", total, 14)
got = {}
for b, hits in buckets.items():
    for h in hits:
        got[h["id"]] = b
want = {i: b for i, *_, b in ROWS if b}
check("★ 每行落桶与预期一致", got, want)
check("干跑不改库", snap(), BEFORE)
check("捆绑守卫理由写明是捆绑", all("捆绑" in h["_why"] for h in buckets["bundle_guard"]), True)
check("flip 的新值是 accessory", {h["_new"] for h in buckets["flip"]}, {"accessory"})
check("pending / sku_conflict 的新值是 unknown（三态，不定案配件）",
      {h["_new"] for h in buckets["pending"] + buckets["sku_conflict"]}, {"unknown"})

# ---------------------------------------------------------------- 采集闸（--apply / --rollback）
# 为什么要闸：product_kind 是采集端**正在写入**的同一列，并发改判会让当轮入库的行拿到
# 半新半旧的口径，而这件事不报错。口径与 tools/audit_backlog.py 完全一致（同一个 collecting_now）。
# ★ 闸必须早于**回滚清单落盘** —— 清单一旦造出来就是「有清单、无改动」的脏后悔药，
#   下次找"最新那份"会拿到它，真正的后悔药被挤到第二位。
_rb_files = lambda: sorted(p.name for p in backfill.ROLLBACK_DIR.glob("*.json")) \
    if backfill.ROLLBACK_DIR.exists() else []                                    # noqa: E731
before_gate_db, before_gate_files = snap(), _rb_files()
backfill.collecting_now = lambda: (True, "scrape_run #7 status=running（测试）")

backfill.APPLY = True
check("★ 采集中 main(--apply) 返回 2", backfill.main(), 2)
backfill.APPLY = False
check("★ 采集中 rollback 返回 2", backfill.rollback("不存在的清单.json"), 2)
check("★ 被拦时库零变化", snap(), before_gate_db)
check("★ 被拦时回滚目录没有新文件（闸在落盘之前）", _rb_files(), before_gate_files)
check("★ 被拦时 apply_() 直接调用也不落盘、不改库",
      (backfill.apply_(backfill.selected_for_apply(buckets)), snap(), _rb_files()),
      (None, before_gate_db, before_gate_files))

backfill.FORCE = True
check("--force 放行（闸门返回 False，不再拦）",
      backfill._blocked("--apply", *backfill.collecting_now()), False)
backfill.FORCE = False
check("不带 --force 时确实拦", backfill._blocked("--apply", *backfill.collecting_now()), True)
backfill.collecting_now = lambda: (False, "测试打桩：无采集")
check("解除采集后不拦", backfill._blocked("--apply", *backfill.collecting_now()), False)

# ---------------------------------------------------------------- apply（默认范围：只 flip）
sel = backfill.selected_for_apply(buckets)
check("默认写库范围只含 flip", sorted(h["id"] for h in sel), [1, 2, 9, 11, 15])
p1 = backfill.apply_(sel)
after = snap()
check("flip 行改成 accessory", {after[i] for i in (1, 2, 9, 11, 15)}, {"accessory"})
check("★ 捆绑整机没被误杀", {after[3], after[4]}, {"device"})
check("待定/冲突默认不写", {after[6], after[7], after[12], after[14]}, {"device"})
check("★ 品牌商城整机不进待定", after[13], "device")
check("其余原样", (after[5], after[8], after[10]), ("device", "accessory", "device"))

man = json.loads(p1.read_text(encoding="utf-8"))
check("清单行数", len(man["rows"]), 5)
check("清单存的是改前的值", {r["old"] for r in man["rows"]}, {"device"})
check("清单带标题与依据", all(r["title"] and r["reason"] for r in man["rows"]), True)

# 第二遍：flip 集合必须为空（跑两遍第二遍应变小）
b2, _ = backfill.scan()
check("★ apply 后再扫 flip 为空", b2["flip"], [])
check("apply 后 pending/sku_conflict 仍在报告（没写就还在）", (len(b2["pending"]), len(b2["sku_conflict"])), (3, 1))

# ---------------------------------------------------------------- 开关：pending + sku_conflict
p2 = backfill.apply_(backfill.selected_for_apply(b2, include_pending=True, include_sku_conflict=True))
after2 = snap()
check("开关打开后待定/冲突置 unknown", (after2[6], after2[7], after2[12], after2[14]), ("unknown",) * 4)
check("品牌商城整机开关打开也不动", after2[13], "device")
check("★ 同秒/连跑两份清单文件名不同", p1 != p2 and p1.exists() and p2.exists(), True)
paths = {backfill._manifest_path() for _ in range(3)}
check("_manifest_path 连续三次不重名", len(paths), 3)
b3, _ = backfill.scan()
check("第三遍：三桶全空", (b3["flip"], b3["pending"], b3["sku_conflict"]), ([], [], []))

# ---------------------------------------------------------------- rollback（逆序：先撤第二份再撤第一份）
backfill.rollback(str(p2))
backfill.rollback(str(p1))
check("★ 回滚后与改前逐行一致", snap(), BEFORE)

# ---------------------------------------------------------------- 单一实现：ast 断言（注释不进语法树）
src = (ROOT / "tools/backfill_accessory_kind.py").read_text(encoding="utf-8")
tree = ast.parse(src)


def _calls(func_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return {n.func.attr for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    return set()


check("★ capture_path 调的是 base._enrich_from_title（采集端同一入口）",
      "_enrich_from_title" in _calls("capture_path"), True)
check("judge 复用 capture_path + 捆绑守卫", {"looks_like_device_bundle"} <= _calls("judge")
      and any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "capture_path"
              for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "judge"
              for n in ast.walk(f)), True)
own_words = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and isinstance(n.value, (ast.List, ast.Tuple))
             and sum(1 for e in n.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)) >= 5]
check("★ 工具里不许自带词表（≥5 个字符串常量的列表/元组）", own_words, [])
check("工具里不许自己 re.compile 规则", "re.compile" in src.replace("re.compile 规则", ""), False)

# ---------------------------------------------------------------- 闸门位置：ast 断言 + 反向自检
# ★ 这里**必须**走 ast：本文件与被测文件的注释里都密集出现 collecting_now / db.tx / 回滚清单
#   这几个词，任何字符串搜索都会匹配到注释而恒真（知识页 assertions-that-verify-nothing，
#   本项目已犯四次，共同点正是"越重要的性质旁边注释写得越认真"）。
_WRITE_ATTRS = {"tx", "write_text", "touch", "execute"}
_WRITE_NAMES = {"_manifest_path"}


def gate_before_write(source: str, func_name: str) -> bool:
    """func_name 里 collecting_now() 是否排在**任何落盘/写库动作之前**。

    写动作 = db.tx() / *.write_text() / *.touch() / *.execute() / _manifest_path()。
    函数里没有写动作时，只要求闸门存在（main 就是这种：它把写委托给 apply_）。
    """
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.FunctionDef) and node.name == func_name):
            continue
        gate, write = [], []
        for n in ast.walk(node):
            if not isinstance(n, ast.Call):
                continue
            pos, f = (n.lineno, n.col_offset), n.func
            if isinstance(f, ast.Name) and f.id == "collecting_now":
                gate.append(pos)
            elif isinstance(f, ast.Name) and f.id in _WRITE_NAMES:
                write.append(pos)
            elif isinstance(f, ast.Attribute) and f.attr in _WRITE_ATTRS:
                write.append(pos)
        if not gate:
            return False
        return not write or min(gate) < min(write)
    return False


for fn in ("main", "apply_", "rollback"):
    check(f"★ {fn}() 在写库/落盘之前调了 collecting_now（ast，不是搜字符串）",
          gate_before_write(src, fn), True)

# 反向自检：把违规写法真的喂进去一次，检测器必须报 False —— 不会红的断言等于没写
check("反向自检①：闸在 db.tx 之后 → False", gate_before_write(
    "def apply_(h):\n"
    "    with db.tx() as c:\n"
    "        pass\n"
    "    busy, why = collecting_now()\n", "apply_"), False)
check("反向自检②：闸在 _manifest_path 之后 → False", gate_before_write(
    "def apply_(h):\n"
    "    p = _manifest_path()\n"
    "    busy, why = collecting_now()\n"
    "    p.write_text('x')\n", "apply_"), False)
check("反向自检③：整个函数没有闸 → False", gate_before_write(
    "def rollback(p):\n"
    "    with db.tx() as c:\n"
    "        c.execute('UPDATE price_obs SET product_kind=?', ('device',))\n", "rollback"), False)
check("反向自检④：闸在最前面 → True", gate_before_write(
    "def rollback(p):\n"
    "    busy, why = collecting_now()\n"
    "    with db.tx() as c:\n"
    "        pass\n", "rollback"), True)
check("★ 闸门与 audit_backlog 是同一个实现（import 来的，不是本地重写）",
      any(isinstance(n, ast.ImportFrom) and n.module == "tools.audit_backlog"
          and any(a.name == "collecting_now" for a in n.names) for n in ast.walk(tree)), True)
check("★ 工具里不许自己定义 collecting_now（那就成了第二套判据）",
      [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "collecting_now"], [])

try:
    db.get_conn().close()
except Exception:                                 # noqa: BLE001
    pass

print(f"\n{PASS} pass / {FAIL} fail   （临时库：{_TMP}）")
sys.exit(1 if FAIL else 0)
