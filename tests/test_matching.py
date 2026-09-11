# -*- coding: utf-8 -*-
"""竞品匹配引擎测试（离线，用临时库）。

验证三条规则真的按"硬闸 + 排序分"工作，而不是加权凑分。

跑法： python tests\test_matching.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_match_"))
config.DB_PATH = _TMP / "t.db"

from app import db  # noqa: E402
from app.matching import CompetitorMatcher, chipset_tier, spec_similarity  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def check_true(name, cond, hint=""):
    check(f"{name}{(' (' + hint + ')') if hint else ''}", bool(cond), True)


db.init_db()

print("== 芯片档位映射 ==")
check("Snapdragon 8 Gen 3", chipset_tier("Snapdragon 8 Gen 3"), 10)
check("A17 Pro 同档", chipset_tier("A17 Pro"), 10)
check("Kirin 9020 同档", chipset_tier("Kirin 9020"), 10)
check("Snapdragon 685 低档", chipset_tier("Snapdragon 685"), 4)
check("带后缀模糊匹配", chipset_tier("Snapdragon 8 Gen 3 Mobile Platform"), 10)
check("认不出返回 None", chipset_tier("SuperChip 9999"), None)
check("空值", chipset_tier(None), None)

print("== ★ 芯片必须比档位不比字符串 ==")
# 字符串毫无相似但同档 → 高分
same_tier = spec_similarity({"chipset": "Snapdragon 8 Gen 3"},
                            {"chipset": "A17 Pro"}, "phone")
# 字符串很像但差三档 → 低分
diff_tier = spec_similarity({"chipset": "Snapdragon 8 Gen 3"},
                            {"chipset": "Snapdragon 685"}, "phone")
check_true("同档得满分", same_tier["dims"].get("chipset_tier") == 1.0,
           str(same_tier["dims"]))
# ★ 不能写 `(dims.get(k) or 1)` —— 0.0 是 falsy，`0.0 or 1` 返回 1，
#   在"0 是合法值"的场景下这个默认值写法永远是错的。必须显式判 None。
_tier = diff_tier["dims"].get("chipset_tier")
check_true("差三档接近 0", _tier is not None and _tier <= 0.2, str(diff_tier["dims"]))

print("== ★ 缺失维度退出计算，不当成 0 ==")
partial = spec_similarity({"chipset": "Kirin 9020", "ram_gb": 12},
                          {"chipset": "Snapdragon 8 Gen 3", "ram_gb": 12}, "phone")
check_true("缺失项被记录", "rom_gb" in partial["missing"], str(partial["missing"]))
check_true("分数按剩余权重归一化（不被缺失拉低）", partial["score"] >= 0.9,
           f"score={partial['score']}")
check_true("置信度反映覆盖率", 0 < partial["confidence"] < 1,
           f"conf={partial['confidence']}")

none_spec = spec_similarity({}, {}, "phone")
check("完全无规格 → 中性值", none_spec["score"], 0.5)
check("完全无规格 → 置信度 0", none_spec["confidence"], 0.0)

print("== 造数据：我方产品 + 友商产品 + 价格 ==")
with db.tx() as conn:
    conn.execute("""INSERT INTO my_product(id,marketing_name,category_code,chipset,screen)
                    VALUES(1,'Vega 80 Pro','phone','Kirin 9020','6.8英寸 OLED')""")
    conn.execute("INSERT INTO my_sku(product_id,ram_gb,rom_gb) VALUES(1,12,512)")
    conn.execute("""INSERT INTO my_pricing(product_id,country_code,rrp_local,
                    currency,on_sale) VALUES(1,'MX',27999,'MXN',1)""")

    samsung = db.q1("SELECT id FROM brand WHERE name='Samsung'")["id"]
    moto = db.q1("SELECT id FROM brand WHERE name='Motorola'")["id"]
    ch = db.q1("SELECT id FROM channel WHERE country_code='MX' LIMIT 1")["id"]

    # 三个候选：价格接近+规格接近 / 价格接近但规格差 / 规格接近但价格差3倍
    cands = [
        (101, samsung, 'Galaxy S26 Ultra', 'galaxys26ultra',
         'Snapdragon 8 Elite', 12, 512, 6.9, 26399),   # 应入选
        (102, moto, 'Moto G15', 'motog15',
         'Helio G85', 4, 128, 6.7, 26999),             # 价格接近但规格差三档 → 出局
        (103, samsung, 'Galaxy Z Fold8 Ultra', 'galaxyzfold8ultra',
         'Snapdragon 8 Elite', 16, 512, 8.0, 50999),   # 规格接近但价格差 82% → 出局
    ]
    for pid, bid, name, key, chip, ram, rom, scr, price in cands:
        conn.execute("""INSERT INTO rival_product(id,brand_id,category_code,model_name,
                        model_key,chipset,ram_gb,rom_gb,screen_size)
                        VALUES(?,?,'phone',?,?,?,?,?,?)""",
                     (pid, bid, name, key, chip, ram, rom, scr))
        for d in range(3):
            conn.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,
                brand_id,category_code,rival_product_id,title,sale_price,currency,
                seller_type,is_in_stock,condition,is_bundle,audit_status,row_hash)
                VALUES(date('now',?),'MX',?,?,'phone',?,?,?,'MXN','official',1,'new',0,
                       'accepted',?)""",
                         (f"-{d} day", ch, bid, pid, name, price,
                          db.row_hash(pid, d)))

m = CompetitorMatcher()
result = m.rebuild_all()
check_true("匹配已产出", result["matches"] > 0, str(result))

matches = db.q("""SELECT cm.*, rp.model_name FROM competitor_match cm
                  JOIN rival_product rp ON rp.id=cm.rival_product_id
                  WHERE cm.my_product_id=1 ORDER BY cm.total_score DESC""")
names = [x["model_name"] for x in matches]
print(f"     入选竞品: {names}")

print("== ★ 三条规则是「与」不是加权和 ==")
check_true("规格+价格都接近的入选", "Galaxy S26 Ultra" in names)
check_true("★价格接近但规格差三档的被挡住", "Moto G15" not in names,
           "加权和会让它靠价格分蒙混过关")
check_true("★规格接近但价差82%的被挡住", "Galaxy Z Fold8 Ultra" not in names,
           "这是不同价位段的产品，不是竞品")

if matches:
    top = matches[0]
    check("排名第一是 S26 Ultra", top["model_name"], "Galaxy S26 Ultra")
    check_true("留了判定证据", bool(top["reasons"]) and len(top["reasons"]) > 20)
    check_true("价差已记录", top["price_gap_pct"] is not None)

print("== 人工确认/排除不被自动重算覆盖 ==")
with db.tx() as conn:
    conn.execute("""UPDATE competitor_match SET is_confirmed=1, total_score=0.99
                    WHERE my_product_id=1 AND rival_product_id=101""")
m2 = CompetitorMatcher()
m2.rebuild_all()
kept = db.q1("""SELECT total_score, is_confirmed FROM competitor_match
                WHERE my_product_id=1 AND rival_product_id=101""")
check_true("人工确认标记仍在", kept and kept["is_confirmed"] == 1)
check_true("人工调整的分数未被覆盖", kept and abs(kept["total_score"] - 0.99) < 1e-6,
           f"score={kept['total_score'] if kept else None}")

print("== ★ 规格全缺时不应一律出局 ==")
with db.tx() as conn:
    conn.execute("DELETE FROM competitor_match")
    conn.execute("""UPDATE rival_product SET chipset=NULL, ram_gb=NULL, rom_gb=NULL,
                    screen_size=NULL WHERE id=101""")
m3 = CompetitorMatcher()
m3.rebuild_all()
no_spec = db.q("""SELECT cm.*, rp.model_name FROM competitor_match cm
                  JOIN rival_product rp ON rp.id=cm.rival_product_id
                  WHERE cm.rival_product_id=101""")
check_true("★规格未知仍能凭价格带匹配", len(no_spec) > 0,
           "否则规格没补全前匹配功能整个不可用")
if no_spec:
    check_true("理由里明确标注规格未校验",
               "规格数据缺失" in (no_spec[0]["reasons"] or ""),
               (no_spec[0]["reasons"] or "")[:80])

# ================================================================
# 2026-09-03「SonicBuds 5 的竞品里出现小米手表」事件的回归测试。
# 匹配器本身没漏（候选池 SQL 有 rp.category_code=? 硬约束），漏的是
# **改品类的路径不触发重算**：落库时两边同品类（合法），之后友商品类被改，
# 这一行就成了跨品类脏行 —— 不报错、不变色、computed_at 也不动。
# ================================================================
XCAT = """SELECT COUNT(*) c FROM competitor_match cm JOIN my_product mp ON mp.id=cm.my_product_id JOIN rival_product rp ON rp.id=cm.rival_product_id WHERE mp.category_code<>rp.category_code AND cm.is_excluded=0"""


def xcat():
    return db.q1(XCAT)["c"]


print("== ★ 已判定为配件的观测不能进候选池 ==")
with db.tx() as conn:
    conn.execute("""INSERT INTO rival_product(id,brand_id,category_code,model_name,
                    model_key,chipset,ram_gb,rom_gb,screen_size)
                    VALUES(104,?,'phone','Funda Galaxy S26 Ultra','fundagalaxys26ultra',
                           'Snapdragon 8 Elite',12,512,6.9)""",
                 (db.q1("SELECT id FROM brand WHERE name='Samsung'")["id"],))
    ch2 = db.q1("SELECT id FROM channel WHERE country_code='MX' LIMIT 1")["id"]
    bid2 = db.q1("SELECT id FROM brand WHERE name='Samsung'")["id"]
    for d in range(3):
        conn.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,
            brand_id,category_code,rival_product_id,title,sale_price,currency,
            seller_type,is_in_stock,condition,is_bundle,audit_status,product_kind,
            row_hash) VALUES(date('now',?),'MX',?,?,'phone',104,
            'Funda Galaxy S26 Ultra',26399,'MXN','official',1,'new',0,'accepted',
            'accessory',?)""", (f"-{d} day", ch2, bid2, db.row_hash(104, 900 + d)))
CompetitorMatcher().rebuild_all()
check("★配件观测不会被当成竞品",
      db.q1("SELECT COUNT(*) c FROM competitor_match WHERE rival_product_id=104")["c"], 0)

print("== ★ 改品类后重算，跨品类脏行必须清零 ==")
with db.tx() as conn:
    conn.execute("DELETE FROM competitor_match")
    conn.execute("""UPDATE rival_product SET chipset='Snapdragon 8 Elite',ram_gb=12,
                    rom_gb=512,screen_size=6.9 WHERE id=101""")
CompetitorMatcher().rebuild_all()
check_true("先有一条同品类合法匹配",
           db.q1("SELECT COUNT(*) c FROM competitor_match WHERE rival_product_id=101")["c"] > 0)
# 模拟品类重分类：友商 101 从 phone 改判成 wearable
with db.tx() as conn:
    conn.execute("UPDATE rival_product SET category_code='wearable' WHERE id=101")
    conn.execute("UPDATE price_obs SET category_code='wearable' WHERE rival_product_id=101")
check_true("改完品类确实产生了跨品类脏行（这就是要防的）",
           xcat() > 0)
res_x = CompetitorMatcher().rebuild_all()
check("★重算后跨品类行清零", xcat(), 0)
check("★rebuild_all 自带跨品类自检", res_x.get("cross_category"), 0)
check_true("干净时不该报警", not res_x.get("warning"), str(res_x.get("warning")))

print("== ★ 自检必须真的能发现外部工具造的脏行 ==")
with db.tx() as conn:
    # 人工确认过的行 rebuild 不会删（人的判断优先），正好用来验证自检会报出来
    conn.execute("""INSERT INTO competitor_match(my_product_id,rival_product_id,
        country_code,total_score,rank_in_country,source,is_confirmed,is_excluded,
        computed_at) VALUES(1,101,'MX',0.9,1,'manual',1,0,datetime('now'))""")
res_w = CompetitorMatcher().rebuild_all()
check("★脏行被自检捕获", res_w.get("cross_category"), 1)
check_true("★并且报了警", "跨品类" in (res_w.get("warning") or ""),
           str(res_w.get("warning")))
with db.tx() as conn:
    conn.execute("DELETE FROM competitor_match WHERE source='manual'")
    conn.execute("UPDATE rival_product SET category_code='phone' WHERE id=101")
    conn.execute("UPDATE price_obs SET category_code='phone' WHERE rival_product_id=101")

print("== ★ 取不到我方价时，旧结论要作废而不是冻结 ==")
CompetitorMatcher().rebuild_all()
check_true("先有匹配",
           db.q1("SELECT COUNT(*) c FROM competitor_match WHERE my_product_id=1 "
                 "AND country_code='MX'")["c"] > 0)
with db.tx() as conn:      # 我方在该国下架 → 取不到锚价
    conn.execute("UPDATE my_pricing SET on_sale=0 WHERE product_id=1 AND country_code='MX'")
CompetitorMatcher().rebuild_all()
check("★旧匹配已作废（不是原地冻结）",
      db.q1("SELECT COUNT(*) c FROM competitor_match WHERE my_product_id=1 "
            "AND country_code='MX' AND source='auto'")["c"], 0)

print("\n== ★ 我方侧改品类同样要作废匹配（镜像窟窿） ==")
# 事故是「友商改品类 → 旧匹配变跨品类」，已在 categorizer._apply 修好。
# 但**我方产品改品类**是同一病根的另一边：products.import_product_list()
# 挂在用户点得到的「导入产品清单」按钮上，改完当场就能产出可见的跨品类脏行。
# 守卫必须与 matcher._persist 的 DELETE 逐字一致，否则会误删人工判断。
_prod = (ROOT / "app/products.py").read_text(encoding="utf-8")
check_true("我方改品类要作废自动匹配",
           "DELETE FROM competitor_match" in _prod,
           "否则用户点一次导入就能造出「我方耳机 vs 友商手表」")
_seg = _prod[_prod.index("DELETE FROM competitor_match"):][:400]     if "DELETE FROM competitor_match" in _prod else ""
for _g in ("source='auto'", "is_confirmed=0", "is_excluded=0"):
    check_true(f"作废守卫含 {_g}", _g in _seg,
               "守卫要与 matcher._persist 一致，缺一个就会误删人工判断")
check_true("作废条数要报给用户", "matches_invalidated" in _prod,
           "静默删除下游数据不可接受")

print("\n== ★ 我方侧改品类同样要作废匹配（镜像窟窿） ==")
# 事故是「友商改品类 → 旧匹配变跨品类」，已在 categorizer._apply 修好。
# 但**我方产品改品类**是同一病根的另一边：products.import_product_list()
# 挂在用户点得到的「导入产品清单」按钮上，改完当场就能产出可见的跨品类脏行，
# 要等下一轮夜间流水线才自愈。守卫必须与 matcher._persist 的 DELETE 逐字一致，
# 否则会误删用户手工确认/排除的判断。
_prod = (ROOT / "app/products.py").read_text(encoding="utf-8")
check_true("我方改品类要作废自动匹配",
           "DELETE FROM competitor_match" in _prod,
           "否则用户点一次导入就能造出「我方耳机 vs 友商手表」")
_seg = (_prod[_prod.index("DELETE FROM competitor_match"):][:400]
        if "DELETE FROM competitor_match" in _prod else "")
for _g in ("source='auto'", "is_confirmed=0", "is_excluded=0"):
    check_true(f"作废守卫含 {_g}", _g in _seg,
               "守卫要与 matcher._persist 一致，缺一个就会误删人工判断")
check_true("作废条数要报给用户", "matches_invalidated" in _prod,
           "静默删除下游数据不可接受")

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
