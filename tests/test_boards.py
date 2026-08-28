# -*- coding: utf-8 -*-
"""图形看板聚合层的口径测试。

跑法：  python tests\test_boards.py

★ 这个文件守的全是「图会不会骗人」，不是「函数会不会报错」。
  一张画错的图比一张空白的图危险得多 —— 空白会让人去查，
  画错了会让人直接拿去做决策。下面每一条都对应一次真实的踩坑。
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_boards_"))
config.DB_PATH = _TMP / "t.db"

from app import boards, db  # noqa: E402

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

print("== ★ 涨价可信度分档：阈值必须按品类给 ==")
# 由来：27 条涨价里 5 条 >50% 全是秘鲁音频（Smokin Buds 89→299 PEN = +236%）。
# 耳机在音频品类里本来就是整机，product_kind 那道闸拦不住；
# 真正的成因是单价量纲 —— 几十块的波动在低价品上就是百分之几十。
check("手机涨 6% = 可信", boards.tier_of(6, "phone"), "credible")
check("手机涨 12% = 存疑", boards.tier_of(12, "phone"), "suspect")
check("手机涨 30% = 几乎必错", boards.tier_of(30, "phone"), "implausible")
check("★音频涨 12% 仍算可信（低单价品波动本就大）",
      boards.tier_of(12, "audio"), "credible")
check("★音频涨 236% = 几乎必错", boards.tier_of(236, "audio"), "implausible")
check("穿戴阈值介于两者之间", boards.tier_of(14, "wearable"), "suspect")
check("品类未知走默认档", boards.tier_of(12, None), "suspect")
check_true("★同一个幅度在不同品类可以有不同判定",
           boards.tier_of(12, "phone") != boards.tier_of(12, "audio"))
check("负幅度按绝对值判（降价也要分档）", boards.tier_of(-30, "phone"), "implausible")
check("None 幅度不炸", boards.tier_of(None, "phone"), "credible")

print("== 时间窗必须锚在数据上，不能用系统时间 ==")
# 由来：库里只有 5 天数据，按系统时间取"近 14 天的前半窗"落在没有数据的区间，
# 于是所有环比全空，看板一片空白 —— 看起来像功能坏了。
with db.tx() as c:
    c.execute("INSERT OR IGNORE INTO scrape_run(id,started_at,run_date,mode,status) "
              "VALUES(1,datetime('now'),date('now'),'test','ok')")
    c.execute("INSERT INTO channel(id,code,name,country_code,kind,base_url,enabled) "
              "VALUES(900,'t1','店A','MX','retailer','https://a.mx/',1)")
    c.execute("INSERT INTO channel(id,code,name,country_code,kind,base_url,enabled) "
              "VALUES(901,'t2','店B','MX','retailer','https://b.mx/',1)")
    c.execute("INSERT INTO brand(id,name,is_ours,enabled) VALUES(900,'TestBrand',0,1)")

# Acme 是 seed 数据里就有的（is_ours=1），取它**真实的 id**，
# 不要硬塞一个 —— brand.name 有 UNIQUE 约束，硬塞会直接撞。
HW = db.q1("SELECT id FROM brand WHERE name='Acme'")["id"]

DAYS = ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04", "2020-01-05"]


def add(day, ch, brand, price, disc=None, url=None, kind="device",
        seller="self_operated", ram=None, rom=None, cat="phone", n=1):
    with db.tx() as c:
        for i in range(n):
            u = url or f"https://x.mx/p/{brand}/{i}"
            c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                            category_code,title,sale_price,currency,url,audit_status,
                            run_id,row_hash,product_kind,condition,is_bundle,
                            discount_pct,seller_kind,ram_gb,rom_gb)
                         VALUES(?,'MX',?,?,?,?,?,'MXN',?,'ok',1,?,?,'new',0,?,?,?,?)""",
                      (day, ch, brand, cat, f"机型{i}", price, u,
                       f"{day}-{ch}-{brand}-{i}-{price}-{u}", kind, disc, seller, ram, rom))


for d in DAYS:
    add(d, 900, 900, 9000, 20, n=12)
check("数据的最新一天", boards._data_now(), "2020-01-05")
check("可用跨度", boards._available_span(), 4)
check_true("★往回数是从数据最新日算的，不是今天",
           boards._back(2).startswith("2020-01-"), boards._back(2))

print("== ★ 价格带：正面断言 device，配件不许混进来 ==")
# 由来：Acme智利"手机"价格带跑出 P25=中位=P75=17,683 CLP，
# 那 6 行全是「GENERICO PANTALLA COMPATIBLE CON ACME」—— 副厂屏幕，
# product_kind='unknown'，被 `<> 'accessory'` 这个否定式条件放了进来。
add("2020-01-05", 900, HW, 300, kind="unknown", n=8)     # Acme"屏幕"，300 块
band = boards.price_band(country="MX", category="phone", days=7)
names = [i["label"] for i in band["items"]]
check_true("★product_kind=unknown 的配件没有进价格带", "Acme" not in names, str(names))
check_true("正常品牌在", "TestBrand" in names)

# 换成 device 但价格低到离谱 → 哨兵要拦住并**说明原因**
add("2020-01-05", 901, HW, 300, kind="device", n=8)
band2 = boards.price_band(country="MX", category="phone", days=7)
flagged = [f["label"] for f in band2.get("flagged", [])]
check_true("★价格低到离谱的品牌被哨兵拦下", "Acme" in flagged, str(flagged))
check_true("★拦下时必须给出原因，不能静默丢弃",
           bool(band2["flagged"] and band2["flagged"][0].get("suspect")))
check_true("被拦的不出现在图里", "Acme" not in [i["label"] for i in band2["items"]])

print("== 价格带用分位数，不用均值 ==")
for i, p in enumerate([100, 200, 300, 400, 900]):
    add("2020-01-04", 900, 900, p, url=f"https://q.mx/{i}", cat="tablet")
tb = boards.price_band(country="MX", category="tablet", days=7, min_n=3)
if tb["items"]:
    it = tb["items"][0]
    check("中位数是 300 不是均值 380", it["med"], 300)
    check_true("P25 < 中位 < P75", it["p25"] <= it["med"] <= it["p75"])

print("== ★ 自营 vs 三方：必须同配置比 ==")
# 由来：Acer Aspire Lite 三方 309,990（8G/128G）vs 自营 479,990（16G/512G），
# 算出 +162% 的"三方加价" —— 那根本是两台不同的机器。
with db.tx() as c:
    c.execute("INSERT INTO rival_product(id,brand_id,category_code,model_name,model_key) "
              "VALUES(900,900,'pc','TestBook','testbook')")
    for day, seller, price, ram, rom in [
            ("2020-01-05", "self_operated", 20000, 16, 512),
            ("2020-01-05", "third_party", 12000, 8, 128),
            ("2020-01-05", "self_operated", 11000, 8, 128),
    ]:
        c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                        category_code,title,sale_price,currency,url,audit_status,run_id,
                        row_hash,product_kind,condition,is_bundle,seller_kind,
                        ram_gb,rom_gb,rival_product_id)
                     VALUES(?,'MX',900,900,'pc','TestBook',?,'MXN',?,'ok',1,?,
                            'device','new',0,?,?,?,900)""",
                  (day, price, f"https://s.mx/{seller}{ram}{rom}",
                   f"ss-{seller}-{ram}-{rom}", seller, ram, rom))
sp = boards.seller_spread(country="MX", category="pc", days=7)
gaps = {i["label"]: i["gap_pct"] for i in sp["items"]}
check_true("★只比同配置：8+128 那组算出 +9.1%",
           any(abs(v - 9.1) < 0.5 for v in gaps.values()), str(gaps))
check_true("★不会拿 16+512 的自营去比 8+128 的三方（那会算出 -40%）",
           not any(v < -30 for v in gaps.values()), str(gaps))
check_true("标签里带上配置，读者能看出比的是什么",
           any("8+128" in k for k in gaps), str(list(gaps)))

print("== ★ 折扣热力：样本不足留空，不能填 0 ==")
heat = boards.discount_heat(days=7, min_n=1000)   # 门槛拉到没有格子能过
check_true("★样本不足的格子是 None 不是 0",
           all(c["v"] is None for c in heat["cells"]), "有格子填了 0")
check_true("中性点是大盘中位而不是 0", heat["center"] > 0, str(heat["center"]))
heat2 = boards.discount_heat(days=7, min_n=1)
check_true("门槛放开后有格子有值", any(c["v"] is not None for c in heat2["cells"]))

print("== ★ 促销收缩：控住商品篮子，剔除构成效应 ==")
# 由来：哥伦比亚跑出 vivo −43pp、Apple −37.9pp —— 全国所有品牌两天内一起减促。
# 真相是前窗只抓到 Falabella（有折扣率 89%），后窗多出 Alkosto 975 条与 Claro 405 条。
# 造一模一样的局：同一批商品促销没变，但后窗多进来一个"低折扣率"的新店。
with db.tx() as c:
    c.execute("DELETE FROM price_obs")
for d in ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04", "2020-01-05"]:
    # 店A 的同一批 20 件商品，全程都在促销 —— 真实行为没变
    for i in range(20):
        add(d, 900, 900, 9000, disc=20, url=f"https://a.mx/p{i}")
# 店B 只在后窗出现，而且几乎不打折
for d in ["2020-01-04", "2020-01-05"]:
    for i in range(40):
        add(d, 901, 900, 9000, disc=None, url=f"https://b.mx/p{i}")

ps = boards.promo_shrink(days=4, min_basket=10)
check_true("★控住篮子后判定为「没有减促」", ps.get("no_movement") is True, str(ps.get("note"))[:80])
check_true("★空态说明里点出了这是构成变化，不是商家行为",
           "构成" in (ps.get("note") or ""), (ps.get("note") or "")[:100])

print("== 数据太短时拒绝出图，而不是给个假数 ==")
with db.tx() as c:
    c.execute("DELETE FROM price_obs")
add("2020-01-05", 900, 900, 9000, disc=20, n=30)
short = boards.promo_shrink(days=14)
check_true("★只有一天数据时明确报不足", short["insufficient"] is True)
check_true("★并说清缺什么", "天" in short["note"], short["note"][:60])

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
