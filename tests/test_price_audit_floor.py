# -*- coding: utf-8 -*-
"""价格审计：品类价格地板 + 取行契约（ORDER BY id / run_all 循环 / 灰区留 pending）。

在**临时库**上跑，绝不碰 intel.db。跑法： python tests\\test_price_audit_floor.py

守的性质（每条对应一次真实误导性输出）：
  1. 8,500 / 13,990 CLP 的钢化膜、水凝膜在 CL 平板桶里必须被剔 —— 它们曾以
     0.61× "型号基线"（基线本身是配件价）通过，上了 Lenovo Tab 的价格曲线。
  2. 地板不能误杀真机：CL 21,990 Smart Band（0.105）、BR 323 儿童平板（0.28）、
     CL 8,990 JBL Tune 110（音频无地板）。
  3. MX 没有 accepted 时地板从未剔除行回落，仍然生效。
  4. 地板样本要先剔配件词，否则中位数被配件拉低、地板失效。
  5. 取行 ORDER BY id 不按国家：LIMIT 名额不许按字母序分给 BR/CL。
  6. run_all 循环到该日取尽、每轮一条 agent_run、第二遍处理 0 行。
  7. gray_policy=keep_pending 时灰区留 pending 且循环能终止（靠游标）。
  8. ★★ wearable 条件地板（2026-09-05）：白牌真手表**不许**被地板剔除，
     而同一个桶里的表带/贴膜/充电器/汽车罩**必须**照剔。用例是真库里被误杀的原标题。
     守卫只对 wearable 开 —— 开给 tablet 会放回「Bolso Tablet」，本文件把这条也钉死。
  9. ★ 第三方且无基线的 accept 分支必须看 product_kind：accessory→剔、unknown→灰区、
     只有 device 才 accept（干跑实测这条路放进过 570 条配件 + 1,094 条 unknown）。
 10. ★ 地板剔除不计入「判定型剔除率」，否则告警常亮 = 等于没有告警。
 11. ★ run_all 必须把 warning 汇出来：三处调用点都写着 r.get("warning")，
     不汇出就是三行死代码（本文件用 ast 对着真实调用点校验键集合）。
"""
import ast
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_floor_"))
config.DB_PATH = _TMP / "t.db"          # ★ 必须早于任何 db.get_conn()

from app import db  # noqa: E402
from app.agents import price_audit as pa  # noqa: E402
from app.agents.price_audit import PriceAuditAgent  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def check_true(name, cond, hint=""):
    check(f"{name}{(' — ' + hint) if hint else ''}", bool(cond), True)


db.init_db()
with db.tx() as c:
    c.execute("INSERT OR IGNORE INTO scrape_run(id,started_at,run_date,mode,status) "
              "VALUES(1,datetime('now'),date('now'),'test','ok')")
    for cid, cc, kind, dst in ((900, "CL", "retailer", "official"), (901, "CL", "marketplace", "third_party"),
                               (902, "CO", "retailer", "official"), (903, "BR", "retailer", "official"),
                               (904, "MX", "retailer", "official"), (905, "PE", "retailer", "official")):
        c.execute("INSERT INTO channel(id,code,name,country_code,kind,base_url,enabled,default_seller_type) "
                  "VALUES(?,?,?,?,?,?,1,?)", (cid, f"t{cid}", f"店{cid}", cc, kind, f"https://{cid}.x/", dst))
    c.execute("INSERT INTO brand(id,name,is_ours,enabled) VALUES(900,'TestBrand',0,1)")

CUR = {"CL": "CLP", "CO": "COP", "BR": "BRL", "MX": "MXN", "PE": "PEN"}
_seq = [0]


def add(day, cc, cat, price, title, *, ch=None, status="pending", kind="device",
        seller="official", model=None, rom=None, stock=1, n=1):
    ids = []
    with db.tx() as c:
        for i in range(n):
            _seq[0] += 1
            cur = c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                                  category_code,title,model_guess,rom_gb,sale_price,currency,url,
                                  audit_status,run_id,row_hash,product_kind,condition,is_bundle,
                                  is_in_stock,seller_type,seller_kind)
                               VALUES(?,?,?,900,?,?,?,?,?,?,?,?,1,?,?,'new',0,?,?,?)""",
                            (day, cc, ch or {"CL": 900, "CO": 902, "BR": 903, "MX": 904, "PE": 905}[cc],
                             cat, title, model or title[:30], rom, price, CUR[cc],
                             f"https://x/{_seq[0]}", status, f"h{_seq[0]}", kind, stock, seller,
                             "third_party" if seller == "third_party" else "self_operated"))
            ids.append(cur.lastrowid)
    return ids


D = "2026-08-20"

# ---------------------------------------------------------------- 造地板样本（每组 40 条 accepted）
def spread(center, n=40):
    # 围绕中位对称分布：中位数恰好 = center
    return [center * (0.7 + 0.6 * i / (n - 1)) for i in range(n)]


for p in spread(284_990):
    add(D, "CL", "tablet", p, "Tablet Lenovo Tab M10 Plus 128GB", status="accepted")
for p in spread(1_799_945):
    add(D, "CO", "tablet", p, "Tablet Samsung Galaxy Tab A9 64GB", status="accepted")
for p in spread(209_990):
    add(D, "CL", "wearable", p, "Smartwatch Galaxy Watch7 44mm", status="accepted")
for p in spread(1_149):
    add(D, "BR", "tablet", p, "Tablet Multilaser M10 64GB Android 13", status="accepted")
for p in spread(51_990):
    add(D, "CL", "audio", p, "Audífonos Bluetooth JBL Tune 520BT", status="accepted")
# BR 穿戴：地板 = 1,990 × 0.08 = 159.2 BRL，用来复现 Amazon BR 的两条误杀（142 / 119 BRL）
for p in spread(1_990):
    add(D, "BR", "wearable", p, "Smartwatch Samsung Galaxy Watch7 44mm", status="accepted")
# MX 平板：0 条 accepted，只有 pending —— 地板必须从未剔除行回落
for p in spread(8_999):
    add(D, "MX", "tablet", p, "Tablet Xiaomi Redmi Pad SE 128GB")
# ★ 样本剔配件：CL 平板再塞 40 条 accepted 的"配件标题"低价行，中位数不许被它们拉下去
for _ in range(40):
    add(D, "CL", "tablet", 5_000, "Funda para Tablet Lenovo Tab M10 negra", status="accepted")
# PE 手机：只有 10 条，两种口径都不足 30 → 无地板
for p in spread(1_200, 10):
    add(D, "PE", "phone", p, "Celular Samsung Galaxy A16 128GB", status="accepted")

agent = PriceAuditAgent(None, {})
floors = agent._build_category_floors(D)
meta = agent._floor_meta

print("== 品类地板：样本口径 ==")
check("CL 平板中位 = 284,990（配件标题的 40 条 accepted 没进样本）",
      round(floors[("CL", "tablet")]), 284_990)
check("CL 平板样本来源 accepted", meta[("CL", "tablet")]["src"], "accepted")
check("MX 平板 accepted=0 → 回落到未剔除行", meta[("MX", "tablet")]["src"], "non_rejected")
check("MX 回落中位 = 8,999", round(floors[("MX", "tablet")]), 8_999)
check_true("PE 手机样本不足 30 → 无地板", ("PE", "phone") not in floors)
check_true("audio 没有地板（不可分，故意缺席）", ("CL", "audio") not in floors and "audio" not in pa.FLOOR_X)

print("== 用户三个案例 + 反例 ==")
bl = agent._build_baselines(D)


def verdict(cc, cat, price, title, **kw):
    ids = add(D, cc, cat, price, title, **kw)
    r = db.q1("SELECT po.*, c.name channel_name, c.kind channel_kind, c.default_seller_type "
              "FROM price_obs po JOIN channel c ON c.id=po.channel_id WHERE po.id=?", (ids[0],))
    return agent._rule_check(r, bl, floors)


v, why = verdict("CL", "tablet", 8_500, "GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11")
check("CL 8,500 钢化膜 → rejected", v, "rejected")
check_true("理由是地板", why.startswith(pa.FLOOR_REASON_PREFIX), why)
check_true("理由写明比例 3.0%", "3.0%" in why, why)
v, why = verdict("CL", "tablet", 13_990, "ROCK SPACE LAMINA HIDROGEL PARA TABLET LENOVO PAD PLUS")
check("CL 13,990 水凝膜 → rejected", v, "rejected")
v, why = verdict("CO", "tablet", 150_000, "Vidrio Templado Tablet Lenovo Tab M10")
check("CO 150,000 → rejected（0.083 < 0.10）", v, "rejected")
v, why = verdict("CL", "wearable", 21_990, "Pulsera Inteligente Xiaomi Smart Band 9 Active")
check_true("CL 21,990 Smart Band（0.105）不被地板杀", not why.startswith(pa.FLOOR_REASON_PREFIX), why)
v, why = verdict("BR", "tablet", 323, "Tablet Infantil Multi Kid Pad 64GB Android 13")
check_true("BR 323 儿童平板（0.28）不被地板杀", not why.startswith(pa.FLOOR_REASON_PREFIX), why)
v, why = verdict("CL", "audio", 8_990, "Audífonos JBL Tune 110 con cable")
check("CL 8,990 JBL Tune 110 → accepted（音频无地板）", v, "accepted")
v, why = verdict("MX", "tablet", 315, "Mica Cristal Templado Galaxy Tab S9")
check("MX 315 mica → rejected（回落地板 899.9 生效）", v, "rejected")
check_true("MX 理由是地板", why.startswith(pa.FLOOR_REASON_PREFIX), why)
v, why = verdict("MX", "tablet", 1_699, "Tablet Lenovo Tab M8 32GB")
check_true("MX 1,699 真平板（0.19）不被地板杀", not why.startswith(pa.FLOOR_REASON_PREFIX), why)

print("== ★★ wearable 条件地板：白牌真手表不许被剔（2026-09-05 误杀复盘，全是真库原标题）==")
# CL 穿戴地板 = 209,990 × 0.08 = 16,799；下面每一条都在地板之下，但都是真机。
REAL_WATCHES_CL = [
    (14_990, "Reloj Smartwatch Inteligente T2 Pro Active Dorado 2025"),
    (14_990, "FITPRO RELOJ INTELIGENTE SMARTWATCH T2 PRO ACTIVE GRIS 2025"),
    (13_990, "-30% GOSHGG SMARTWATCH SERIE 6"),
    (12_990, "SMART BRACELET 1.0 SMARTWATCH RELOJ INTELIGENTE SMART BRACELET FITNES DEPORTIVO"),
    (12_990, "Reloj inteligente smartwatch i7promax Azul SERIE 7 / Realiza Llamadas"),
    (9_990, "Reloj Smartwatch Active T2 Pro IP67 Reloj Inteligente"),
    (15_990, "Smart Watch Reloj Inteligente Kbod S8 Ultra IP65 Naranja"),
    (15_490, "LENOVO Lenovo LP40 Pro Smartwatch Toumi Watch S9 PRO 3.4"),
]
for price, title in REAL_WATCHES_CL:
    v, why = verdict("CL", "wearable", price, title)
    check_true(f"CL {price:,} 真手表不被地板杀 — {title[:34]}",
               not why.startswith(pa.FLOOR_REASON_PREFIX), why)
# Amazon BR 的两条（葡语），地板 159.2 BRL
for price, title in ((142, "Smartwatch PEJE Relógio Smartwatch com Pulseira Extra - Tela Touch HD 1.28"),
                     (119, "SmartWatch, relogio smartwatch feminino com 1.83 Ecrã tátil, à prova d'água")):
    v, why = verdict("BR", "wearable", price, title)
    check_true(f"BR {price} 真手表不被地板杀 — {title[:30]}",
               not why.startswith(pa.FLOOR_REASON_PREFIX), why)

print("== ★ 同一个桶里的配件必须照剔（守卫不能把地板整条废掉）==")
KILL_WEARABLE_CL = [
    (9_990, "GENÉRICO CORREA RELOJ REPUESTO SILICONA ACME HONOR BAND 8 ROSADO"),
    (8_990, "Mica protector de pantalla Smartwatch AMAZFIT GTS"),
    (8_990, "GENÉRICO CARGADOR SAMSUNG GALAXY WATCH CLASSIC 4 3 ACTIVE2 CABLE R500 NEGRO"),
    (10_990, "JOIGO CORREA JOIGO CON HEBILLA COMPATIBLE CON SAMSUNG GALAXY WATCH 22MM NEGRO"),
    (14_547, "Funda Cubre Auto Gris Plomo Protectora - Kia 4"),
]
for price, title in KILL_WEARABLE_CL:
    v, why = verdict("CL", "wearable", price, title)
    check(f"CL {price:,} 配件仍被剔 — {title[:34]}", v, "rejected")
    check_true("理由是地板", why.startswith(pa.FLOOR_REASON_PREFIX), why)

print("== ★ 守卫只对 wearable 开：开给别的品类会放回背包/箱包（反向自检）==")
bolso = {"sale_price": 9_990, "currency": "CLP", "country_code": "CL", "category_code": "tablet",
         "title": "Bolso Tablet Tucano Nina Shoulder bag", "model_guess": "Bolso Tablet",
         "rom_gb": None, "obs_date": D, "is_bundle": 0, "condition": "new",
         "is_in_stock": 1, "seller_type": "official", "product_kind": "device"}
check("平板包被地板剔除（守卫没开给 tablet）", agent._rule_check(bolso, bl, floors)[0], "rejected")
check_true("守卫对它不放行", agent._floor_veto_hit(bolso, floors) is None)
# ★ 反向自检：把 tablet 加进守卫名单，同一行必须变成不剔 —— 证明"是名单在起作用"，
#   而不是别的条件恰好挡住了它（不会红的断言等于没写）。
_saved_scope = pa.FLOOR_DEVICE_EVIDENCE
pa.FLOOR_DEVICE_EVIDENCE = frozenset({"wearable", "tablet"})
check_true("把 tablet 加进名单后同一行被放行（证明名单确实在起作用）",
           not agent._rule_check(bolso, bl, floors)[1].startswith(pa.FLOOR_REASON_PREFIX))
pa.FLOOR_DEVICE_EVIDENCE = _saved_scope
check("恢复名单后又被剔", agent._rule_check(bolso, bl, floors)[0], "rejected")

print("== ★ 守卫的两个条件缺一不可 ==")
watch_row = {"sale_price": 12_990, "currency": "CLP", "country_code": "CL",
             "category_code": "wearable", "title": "GOSHGG SMARTWATCH SERIE 6",
             "model_guess": "GOSHGG", "rom_gb": None, "obs_date": D, "is_bundle": 0,
             "condition": "new", "is_in_stock": 1, "seller_type": "official",
             "product_kind": "device"}
check_true("真手表：守卫放行且给得出依据", bool(agent._floor_veto_hit(watch_row, floors)))
check_true("① 分类器判配件就不放行（标题换成 correa 打头）",
           agent._floor_veto_hit(dict(watch_row, title="Correa para GOSHGG Smartwatch Serie 6"),
                                 floors) is None)
check_true("② 没有本品类整机证据就不放行（标题只剩型号）",
           agent._floor_veto_hit(dict(watch_row, title="GOSHGG SERIE 6"), floors) is None)
check_true("③ 价格没低于地板时守卫不该报（它只描述「低于地板但放行」）",
           agent._floor_veto_hit(dict(watch_row, sale_price=209_990), floors) is None)
check_true("④ 没有地板（audio）时守卫不报",
           agent._floor_veto_hit(dict(watch_row, category_code="audio", sale_price=100),
                                 floors) is None)

print("== ★ 地板必须在型号基线之前：配件占多数的桶里基线会自毁 ==")
r = {"sale_price": 8_500, "currency": "CLP", "country_code": "CL", "category_code": "tablet",
     "model_guess": "Lenovo Tab", "rom_gb": None, "obs_date": D, "is_bundle": 0,
     "condition": "new", "is_in_stock": 1, "seller_type": "official"}
fake_bl = {agent._bkey(r): 13_990.0}          # 桶中位 = 配件价，8,500 是 0.61×"合理带"
check("有配件桶基线时旧逻辑会 accepted（复现事故）", agent._rule_check(r, fake_bl)[0], "accepted")
check("加地板后 → rejected", agent._rule_check(r, fake_bl, floors)[0], "rejected")
check("不传 floors 向后兼容（干跑脚本还在这样调）", agent._rule_check(r, {})[0], "accepted")

print("== 第三方且无基线：策略常量 + ★product_kind 闸 ==")
r3 = dict(r, sale_price=200_000, seller_type="third_party", category_code="phone",
          product_kind="device")
saved = pa.NO_BASELINE_THIRD_PARTY
pa.NO_BASELINE_THIRD_PARTY = "accept"
v, why = agent._rule_check(r3, {}, floors)
check("accept 策略 + device → accepted", v, "accepted")
check_true("理由以 NO_BASELINE_REASON 开头（--recheck-no-baseline 靠它找回）",
           why.startswith(pa.NO_BASELINE_REASON), why)
# ★ 这三条对应干跑实测：4,238 条走 accept 分支的行里 accessory 570 + unknown 1,094，
#   标题是表带/书籍/裙子/电视 —— 它们"没有基线"恰恰是因为不是整机，不是因为是新品。
v, why = agent._rule_check(dict(r3, product_kind="accessory"), {}, floors)
check("accept 策略 + accessory → rejected（不许再进 accepted）", v, "rejected")
check_true("剔除理由说得出是配件", "配件" in why, why)
v, why = agent._rule_check(dict(r3, product_kind="unknown"), {}, floors)
check("accept 策略 + unknown → gray（留待复核，不进 accepted）", v, "gray")
check_true("灰区理由标明 unknown", "unknown" in why, why)
r3_nokind = {k: v2 for k, v2 in r3.items() if k != "product_kind"}
check("accept 策略 + 缺 product_kind 列 → gray（默认不放行）",
      agent._rule_check(r3_nokind, {}, floors)[0], "gray")
check("accept 策略 + product_kind 为 None → gray",
      agent._rule_check(dict(r3, product_kind=None), {}, floors)[0], "gray")
check("★官方渠道无基线不受这条闸影响（只管第三方）",
      agent._rule_check(dict(r3, seller_type="official", product_kind="unknown"),
                        {}, floors)[0], "accepted")
pa.NO_BASELINE_THIRD_PARTY = "gray"
check("gray 策略 → gray（旧行为）", agent._rule_check(r3, {}, floors)[0], "gray")
pa.NO_BASELINE_THIRD_PARTY = "reject"
check("reject 策略 → rejected", agent._rule_check(r3, {}, floors)[0], "rejected")
pa.NO_BASELINE_THIRD_PARTY = saved

print("== 取行 ORDER BY id：名额不按国家字母序分 ==")
D2 = "2026-08-21"
mx = add(D2, "MX", "phone", 5_000, "Celular Motorola Moto G15 128GB", n=3)
br = add(D2, "BR", "phone", 1_500, "Celular Samsung Galaxy A16 128GB", n=3)
res = agent.run(obs_date=D2, limit=2)
check("只取 2 行", res["total"], 2)
done = {r["id"] for r in db.q("SELECT id FROM price_obs WHERE obs_date=? AND audit_status<>'pending'", (D2,))}
check("★处理的是 id 最小的两条（MX），不是字母序靠前的 BR", done, set(mx[:2]))
check("返回游标 last_id", res["last_id"], mx[1])
check_true("返回 run_id（agent_run 留痕）", res["run_id"] is not None)

print("== run_all：循环到取尽、每轮一条 agent_run、第二遍 0 行 ==")
runs0 = db.q1("SELECT COUNT(*) n FROM agent_run WHERE agent_name='price_audit'")["n"]
rounds = []
agg = agent.run_all(D2, batch=3, on_round=lambda x: rounds.append(x["total"]))
check("剩余 4 行按 3/1 两轮", rounds, [3, 1])
check("累计 total", agg["total"], 4)
check("该日 pending 清零", agg["pending_left"], 0)
runs1 = db.q1("SELECT COUNT(*) n FROM agent_run WHERE agent_name='price_audit'")["n"]
check("每轮一条 agent_run", runs1 - runs0, 2)
check("run_ids 与轮数一致", len(agg["run_ids"]), 2)
agg2 = agent.run_all(D2, batch=3)
check("★第二遍处理 0 行（跑两遍第二遍应变小）", agg2["total"], 0)
check("第二遍 0 轮", agg2["rounds"], 0)

print("== gray_policy=keep_pending：灰区留 pending，游标保证终止 ==")
D3 = "2026-08-22"
# 3 条 accepted 建型号基线 100,000；第三方 25,000 → 0.25× → 灰区（CL phone 样本不足 30，无地板）
add(D3, "CL", "phone", 100_000, "Celular Test Phone X 128GB", model="Test Phone X", rom=128, status="accepted", n=3)
g = add(D3, "CL", "phone", 25_000, "Celular Test Phone X 128GB", model="Test Phone X", rom=128,
        seller="third_party", ch=901, n=2)
ok_ids = add(D3, "CL", "phone", 98_000, "Celular Test Phone X 128GB", model="Test Phone X", rom=128, n=2)
agg3 = agent.run_all(D3, batch=1, gray_policy="keep_pending", max_rounds=50)
check("4 轮各取 1 行（没有因灰区留 pending 而死循环）", agg3["rounds"], 4)
check("灰区 2 条留 pending", agg3["gray_pending"], 2)
check("pending_left = 灰区数", agg3["pending_left"], 2)
st = {r["id"]: r["audit_status"] for r in db.q("SELECT id, audit_status FROM price_obs WHERE obs_date=?", (D3,))}
check("灰区行仍是 pending", {st[i] for i in g}, {"pending"})
check("正常行已 accepted", {st[i] for i in ok_ids}, {"accepted"})
check("未中止（max_rounds 足够）", agg3["stopped"], "")

print("== 旧行为保留：无 Key 且 gray_policy=llm → 灰区保留并标注 ==")
add(D3, "CL", "phone", 24_000, "Celular Test Phone X 128GB", model="Test Phone X", rom=128,
    seller="third_party", ch=901)
r4 = agent.run(obs_date=D3, limit=10, after_id=max(ok_ids))
check("取到 1 条新灰区行", r4["total"], 1)
check("无 Key 时灰区被保留（accepted）", r4["accepted"], 1)
row = db.q1("SELECT audit_reason FROM price_obs WHERE obs_date=? ORDER BY id DESC LIMIT 1", (D3,))
check_true("理由标明未经模型复核", "未经模型复核" in (row["audit_reason"] or ""), row["audit_reason"])

print("== 缺货判定仍在地板之前（口径定义先于价格判断）==")
v, why = verdict("CL", "tablet", 8_500, "Lámina para Lenovo Tab", stock=0)
check("缺货优先于地板", "缺货" in why, True)

print("== ★ 地板剔除不计入判定型剔除率（否则告警常亮 = 等于没有告警）==")
D4 = "2026-08-23"
add(D4, "CL", "tablet", 250_000, "Tablet Lenovo Tab M10 Plus 128GB")
add(D4, "CL", "tablet", 8_500, "GENÉRICO LÁMINA VIDRIO TEMPLADO TABLET LENOVO TAB P11", n=20)
add(D4, "CL", "wearable", 12_990, "GOSHGG SMARTWATCH SERIE 6")     # 守卫放行的白牌真表
r4 = agent.run(obs_date=D4, limit=100)
check("D4 取到 22 行", r4["total"], 22)
check("地板剔除 20 条", r4["floor_rejected"], 20)
check("全部剔除都是地板", r4["rejected"], 20)
check("判定型剔除 0 条", r4["judged_rejected"], 0)
check("★没有告警（地板不算判定型）", r4["warning"], "")
check("守卫放行计数出现在返回值里", r4["floor_vetoed"], 1)
check("被守卫放行的真手表进了 accepted", r4["accepted"], 2)
# ★ 守卫计数只数"真的因此活下来"的行：捆绑装先按口径剔掉了，守卫放没放行都一样，
#   算进来会把这个诊断数字灌水（诊断数字虚高 = 下次真出问题时看不出来）
D4b = "2026-08-26"
add(D4b, "CL", "wearable", 12_990, "GOSHGG SMARTWATCH SERIE 6 + Correa de regalo")
with db.tx() as c:
    c.execute("UPDATE price_obs SET is_bundle=1 WHERE obs_date=?", (D4b,))
r4b = agent.run(obs_date=D4b, limit=10)
check("捆绑装被剔", r4b["rejected"], 1)
check("★捆绑装不算进守卫放行", r4b["floor_vetoed"], 0)
# 复现事故：把地板算进判定型的话，这一轮的告警率会是 90.9%，每轮都 degraded
check_true("若把地板计入判定型则会触发告警（复现「常亮」）",
           r4["floor_rejected"] / (r4["accepted"] + r4["floor_rejected"]) > pa.ALERT_REJECT_RATE)

print("== ★ 真的判定型剔除仍然要告警，且 run_all 必须把 warning 汇出来 ==")
D5 = "2026-08-24"
add(D5, "CL", "tablet", 250_000, "Tablet Lenovo Tab M10 Plus 128GB")
add(D5, "CL", "tablet", 8_500, "GENÉRICO LÁMINA VIDRIO TEMPLADO TABLET LENOVO TAB P11", n=20)
add(D5, "CL", "phone", 200_000, "Celular Test Phone X 128GB", model="Test Phone X", rom=128,
    seller="third_party", ch=901)                                  # 2.0× 基线 → 第三方溢价
agg5 = agent.run_all(D5, batch=100)
check("判定型剔除 1 条（第三方溢价）", agg5["judged_rejected"], 1)
check("地板剔除 20 条单列", agg5["floor_rejected"], 20)
check("判定型剔除率 = 1/(1+1)", round(agg5["judged_reject_rate"], 4), 0.5)
check_true("★run_all 汇出了 warning（三处调用点读的就是这个键）", bool(agg5["warning"]), agg5["warning"])
check_true("warning 里写明观测日", D5 in agg5["warning"], agg5["warning"])
check_true("warning 里写明地板另计", "地板" in agg5["warning"], agg5["warning"])
agg4 = agent.run_all("2026-08-25", batch=100)     # 没有观测的一天
check("空日 run_all 不告警", agg4["warning"], "")
check("空日 total=0", agg4["total"], 0)

print("== run() 的键集合：空结果与正常结果必须一致（缺键会静默变成 None）==")
r_empty = agent.run(obs_date="2026-01-01")
check("空结果 total=0", r_empty["total"], 0)
check("★键集合一致", sorted(r_empty), sorted(r4))

print("== ★ ast：真实调用点从 run_all 结果里读的键，run_all 必须都给（不搜字符串）==")


def run_all_keys_read(path: Path) -> set:
    """扫描一个源文件：凡是从 **PriceAuditAgent** 的 run_all(...) 赋值出来的名字，
    它身上读过的字符串键。

    ★ 走 ast 不走字符串搜索 —— 注释里到处写着 "warning"，
      全文搜索会匹配到我自己写的注释，断言恒真（assertions-that-verify-nothing）。
    ★ 必须认接收者：orchestrator 里还有一个 `archive.run_all(keep_snapshots=…)`，
      只按方法名匹配会把归档的返回键（facts/snapshot/dir）算成价格审计的契约。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set = set()
    scopes = [n for n in ast.walk(tree)
              if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))]

    def own(scope):
        """只走**本作用域自己**的节点，不下钻进嵌套函数。

        ★ 非走不可：orchestrator 的 _audit_day 是嵌套函数，
          ast.walk(外层函数) 会把它一起吃进来 —— 于是外层里同名的 r
          （另一个 Agent 的返回值）身上读的键也被算成"读 run_all 的键"，
          实测多出 d/dir/facts/snapshot 四个假阳性。作用域串味 = 断言在验别的东西。
        """
        stack = [c for c in getattr(scope, "body", [])
                 if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        while stack:
            n = stack.pop()
            yield n
            for c in ast.iter_child_nodes(n):
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                stack.append(c)

    def _is_agent(node) -> bool:
        """接收者是不是 PriceAuditAgent —— 直接构造，或整个文件里绑过它的名字。"""
        if isinstance(node, ast.Call):
            f = node.func
            return (isinstance(f, ast.Name) and f.id == "PriceAuditAgent") or \
                   (isinstance(f, ast.Attribute) and f.attr == "PriceAuditAgent")
        return isinstance(node, ast.Name) and node.id in agent_names

    agent_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_agent(node.value):
            agent_names.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def _is_price_audit_run_all(value) -> bool:
        return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                   and c.func.attr == "run_all" and _is_agent(c.func.value)
                   for c in ast.walk(value))

    for scope in scopes:
        names = set()
        for node in own(scope):
            if isinstance(node, ast.Assign) and _is_price_audit_run_all(node.value):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        if not names:
            continue
        for node in own(scope):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in names and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value)
            elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                  and node.value.id in names and isinstance(node.slice, ast.Constant)
                  and isinstance(node.slice.value, str)):
                out.add(node.slice.value)
    return out


# 扫描器自检：把"读一个不存在的键"真的写一次，确认扫得到（不会红的断言等于没写）
_probe = _TMP / "probe_consumer.py"
_probe.write_text("def f(cfg, d):\n"
                  "    r = PriceAuditAgent(None, cfg).run_all(obs_date=d) or {}\n"
                  "    return r.get('no_such_key'), r['warning']\n", encoding="utf-8")
check("扫描器能抓到读键", run_all_keys_read(_probe), {"no_such_key", "warning"})
_probe2 = _TMP / "probe_other.py"
_probe2.write_text("def f(cfg, d):\n"
                   "    r = PriceAuditAgent(None, cfg).run(obs_date=d) or {}\n"
                   "    return r.get('warning')\n", encoding="utf-8")
check("★.run() 的结果不算 run_all 契约", run_all_keys_read(_probe2), set())
_probe3 = _TMP / "probe_archive.py"
_probe3.write_text("def f(keep):\n    r = archive.run_all(keep_snapshots=keep)\n"
                   "    return r['facts'], r.get('snapshot')\n", encoding="utf-8")
check("★别的模块的同名 run_all 不算（orchestrator 里就有 archive.run_all）",
      run_all_keys_read(_probe3), set())
_probe4 = _TMP / "probe_nested.py"
_probe4.write_text("def outer(cfg, d):\n    r = {'facts': 1}\n"
                   "    def inner():\n"
                   "        r = PriceAuditAgent(None, cfg).run_all(obs_date=d)\n"
                   "        return r.get('warning')\n"
                   "    return inner(), r['facts']\n", encoding="utf-8")
check("★嵌套函数里的同名变量不串味（orchestrator._audit_day 就是这个形态）",
      run_all_keys_read(_probe4), {"warning"})

provided = set(agg5)
for rel in ("app/agents/orchestrator.py", "tools/postprocess_date.py", "tools/audit_backlog.py"):
    read = run_all_keys_read(ROOT / rel)
    missing = read - provided
    if missing:
        print(f"  {rel} 读了 run_all 没给的键：{sorted(missing)}")
    check(f"{rel} 读的键 run_all 都给得出", missing, set())
    check_true(f"{rel} 确实读了键（扫描器不是空转）", bool(read), ",".join(sorted(read)))
check_true("★orchestrator 读的就是 warning 这个键（这条曾经是死代码）",
           "warning" in run_all_keys_read(ROOT / "app/agents/orchestrator.py"))
check_true("★postprocess_date 也读 warning",
           "warning" in run_all_keys_read(ROOT / "tools/postprocess_date.py"))

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
