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

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
