# -*- coding: utf-8 -*-
"""product_series 的审计门 / 多币种指数 / 最小点数 —— 在**临时库**上跑，绝不碰 intel.db。

守的性质（每条对应 2026-09-04 #2292「Lenovo Tab」那张在 18 万与 8,500 CLP 之间来回跳的图）：
  1. 只画 audit_status='accepted'：pending-only 的产品返回**空 series**，
     不是把 pending 点画上去（8,500 CLP 的钢化膜就是以 pending 身份上图的）
  2. 多币种产品：mixed_currency=True 且每条线都带 index（该线首个真实观测=100），
     纵轴标签是「指数（基期=100）」；每条渠道线的绝对价位 pts 在后端置空
     （前端把所有线放一根轴，后端不给绝对价就画不出错图）
  3. 单币种：纵轴标签标该币种
  4. 真实观测 < MIN_POINTS 的线不画，但要列在 dropped_series 里（只画能画的、要说没画谁）
  5. 合成线（by_channel=False）跨币种时绝对价位置空、指数仍在；
     ★ 合成指数走**逐日配对链式**，不是"各线自基期指数的中位数"
     （后者会让晚上架的渠道线以 100 进入中位数，把合成线往下拽 —— §8 复现）
  6. 被挡掉的 pending 条数要报出来 —— 空图要能说清是"待审"不是"没数据"
  7. 源码级（ast）：SQL 用 = 'accepted'；_watch_label / suggest_watch 走 display_label
  8. ★ 晚入的渠道线不许造成合成指数跳变（构成效应，composition-vs-level-effects）
  9. ★ 跨品类挂接的观测不进曲线（po.category_code ≠ rp.category_code）——
     音频品类刻意无价格地板，这类行审计永远抓不到，只能在消费方挡

★ 必须在 db 建立第一个连接**之前**改掉 config.DB_PATH：连接缓存在线程本地，
  一旦连上真库就再也切不走了，测试会安静地在生产库上跑。

跑法： python tests\\test_trend_product_series.py
"""
import ast
import inspect
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config                              # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="trendseries_"))
config.DB_PATH = _TMP / "test.db"                   # ★ 必须早于任何 db.get_conn()

from app import db, trends                          # noqa: E402

PASS = 0
FAIL: list[str] = []


def ok(cond, msg):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(msg)


# ---------------------------------------------------------------- 建库 + 造数
# init_db 会带上 YAML 里的种子维度，所以维度行一律 OR IGNORE；业务行用 9xxx 的 id 避撞
db.init_db()
with db.tx() as c:
    c.execute("INSERT OR IGNORE INTO country(code,name_zh,currency,lang,locale,timezone) "
              "VALUES('CL','智利','CLP','es','es-CL','America/Santiago')")
    c.execute("INSERT OR IGNORE INTO country(code,name_zh,currency,lang,locale,timezone) "
              "VALUES('MX','墨西哥','MXN','es','es-MX','America/Mexico_City')")
    c.execute("INSERT OR IGNORE INTO category(code,name_zh) VALUES('tablet','平板')")
    c.execute("INSERT OR IGNORE INTO brand(id,name,is_ours) VALUES(9001,'LenovoTest',0)")
    c.execute("INSERT OR IGNORE INTO channel(id,code,country_code,name,kind) "
              "VALUES(9001,'t_ripley','CL','Ripley·测','retailer')")
    c.execute("INSERT OR IGNORE INTO channel(id,code,country_code,name,kind) "
              "VALUES(9002,'t_liverpool','MX','Liverpool·测','retailer')")
    c.execute("INSERT OR IGNORE INTO channel(id,code,country_code,name,kind) "
              "VALUES(9003,'t_paris','CL','Paris·测','retailer')")
    # 产品 9001：两国两币种（型号名自带品牌前缀，复刻权威 SKU 的形态）；产品 9002：只有 pending
    # 产品 9003：同币种双渠道，B 渠道**晚一天**才上架 —— 用来复现构成效应（§8）
    for pid, model in ((9001, "LenovoTest Tab P11"), (9002, "LenovoTest Tab M10"),
                       (9003, "LenovoTest Tab Late")):
        c.execute("INSERT INTO rival_product(id,brand_id,category_code,model_name,model_key) "
                  "VALUES(?,9001,'tablet',?,?)", (pid, model, model.lower().replace(" ", "")))

DAYS = ["2026-09-01", "2026-09-02", "2026-09-03"]
OBS = []   # (pid, day, cc, channel, currency, price, audit)
for d, p in zip(DAYS, (100_000, 110_000, 121_000)):          # CL Ripley：每天 +10%
    OBS.append((9001, d, "CL", 9001, "CLP", p, "accepted"))
for d, p in zip(DAYS, (5_000, 5_000, 4_500)):                 # MX Liverpool：末日 −10%
    OBS.append((9001, d, "MX", 9002, "MXN", p, "accepted"))
OBS.append((9001, DAYS[1], "CL", 9003, "CLP", 99_000, "accepted"))   # Paris 只有 1 个点 → 不画
# 同一产品同一渠道还有一张 pending 的"钢化膜价"：绝不能上图
OBS.append((9001, DAYS[2], "CL", 9001, "CLP", 8_500, "pending"))
for d in DAYS:                                                # 产品 9002 全 pending
    OBS.append((9002, d, "CL", 9001, "CLP", 150_000, "pending"))

# ★ §8 构成效应用例。多用一天（往**前**加 08-31，不动 MAX(obs_date)，
#   否则 LOCF 会给产品 9001 的线多延续出一天，把 §1 的指数断言拽偏）：
#     A 线（Ripley）四天全在架：100k → 120k → 120k → 120k（第 2 天涨 20%，之后持平）
#     B 线（Paris）  第 3 天才上架：  缺  →  缺  →  50k →  50k（全程没动过价）
#   旧口径「各线自基期指数取中位数」：100 → 120 → median(120,100)=110 ⇒ **120→110，假跌 8.3%**，
#   而那天 A 持平、B 是新上架的 —— 没有任何一条线降过价。
DAY0 = "2026-08-31"
LATE = [(9003, DAY0, 9001, 100_000), (9003, DAYS[0], 9001, 120_000),
        (9003, DAYS[1], 9001, 120_000), (9003, DAYS[2], 9001, 120_000),
        (9003, DAYS[1], 9003, 50_000), (9003, DAYS[2], 9003, 50_000)]
for pid, d, ch, price in LATE:
    OBS.append((pid, d, "CL", ch, "CLP", price, "accepted"))

with db.tx() as c:
    for i, (pid, d, cc, ch, cur, price, audit) in enumerate(OBS):
        c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                        category_code,rival_product_id,title,sale_price,currency,
                        product_kind,condition,is_bundle,audit_status,row_hash)
                     VALUES(?,?,?,9001,'tablet',?,?,?,?,'device','new',0,?,?)""",
                  (d, cc, ch, pid, f"t{i}", price, cur, audit, f"h{i}"))
    # ★ §9 跨品类挂接：同一台平板（rp.category_code='tablet'）身上挂了一条
    #   `po.category_code='audio'` 的耳机观测 —— 真实形态是 iPhone 15（#3）CL 曲线里的
    #   「APPLE Audifonos EarPods USB C iphone 15」22,990 CLP。它 product_kind='device'、
    #   audit_status='accepted'（音频品类刻意没有价格地板 ⇒ 审计抓不到），
    #   不在消费方挡就会成为 CL 渠道线当天的 MIN(sale_price)。
    for _d, _p, _a, _h in ((DAYS[0], 9_900, "accepted", "hx1"),
                           (DAYS[1], 7_700, "pending", "hx2")):
        c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                        category_code,rival_product_id,title,sale_price,currency,
                        product_kind,condition,is_bundle,audit_status,row_hash)
                     VALUES(?,'CL',9001,9001,'audio',9001,?,?,'CLP',
                            'device','new',0,?,?)""",
                  (_d, "LenovoTest Audifonos Buds para Tab P11", _p, _a, _h))

# ---------------------------------------------------------------- 1. 多币种：index + 标签
r = trends.product_series(9001, days=10)
ok(r["mixed_currency"] is True, "两国两币种应判 mixed_currency=True")
ok(r["indexed_required"] is True, "多币种要显式告诉前端必须走指数")
ok(r["y_label"] == "指数（基期=100）", f"多币种纵轴标签应为指数，实得 {r['y_label']!r}")
names = [s["name"] for s in r["series"]]
ok(len(r["series"]) == 2, f"应画 2 条线（Paris 只有 1 点不画），实得 {names}")
ok(all(isinstance(s.get("index"), list) and len(s["index"]) == len(r["xs"])
       for s in r["series"]), "每条线必须带 index 且与 xs 等长")
for s in r["series"]:
    first = next((v for v in s["index"] if v is not None), None)
    ok(first == 100.0, f"{s['name']} 指数基期应为该线首个真实观测=100，实得 {first}")
    ok(len(s["index"]) == len(s["pts"]) == len(s["filled"]),
       f"{s['name']} index/pts/filled 必须逐位对齐")
cl = next(s for s in r["series"] if s["country"] == "CL")
mx = next(s for s in r["series"] if s["country"] == "MX")
cl_idx = [v for v in cl["index"] if v is not None]
mx_idx = [v for v in mx["index"] if v is not None]
ok(cl_idx == [100.0, 110.0, 121.0], f"CL 线指数应为 100/110/121，实得 {cl_idx}")
ok(mx_idx == [100.0, 100.0, 90.0], f"MX 线指数应为 100/100/90，实得 {mx_idx}")
ok(8_500 not in [v for v in cl["pts"] if v is not None],
   "★ pending 的 8,500 钢化膜价绝不能出现在线上（回归：#2292 就是这么跳的）")
# ★ 多币种时每条渠道线的绝对价位必须在后端置空：画单品曲线的前端把所有线放一根轴，
#   后端不给绝对价，前端就**不可能**把 CLP 与 MXN 画到同一根轴上（与 _compose 同一纪律）。
ok(all(v is None for v in cl["pts"]) and all(v is None for v in mx["pts"]),
   f"多币种时渠道线的 pts 必须全为 None，实得 CL={cl['pts']} MX={mx['pts']}")
ok(isinstance(cl["filled"], list) and len(cl["filled"]) == len(cl["index"]) == len(r["xs"])
   and all(isinstance(v, bool) for v in cl["filled"]),
   "置空绝对价后 filled 仍须保留并与 index / xs 逐位对齐（前端靠它标延续点）")
ok(bool(r["dropped_series"]) and r["dropped_series"][0]["n_points"] == 1
   and "Paris" in r["dropped_series"][0]["name"],
   f"只有 1 个点的 Paris 线要列在 dropped_series 里，实得 {r['dropped_series']}")
ok(r["pending_obs"] == 1, f"被挡掉的 pending 条数应为 1，实得 {r['pending_obs']}")
ok("待价格审计" in r["note"], "note 要说明有观测因待审计未上图")
ok("未画" in r["note"], "note 要说明有线因观测不足未画")
ok("币种" in r["note"], "note 要说明为什么只能看指数")
ok(r["product"]["label"] == "LenovoTest Tab P11",
   f"product.label 应去重品牌词，实得 {r['product'].get('label')!r}")

# ---------------------------------------------------------------- 2. 单币种：标该币种
r_cl = trends.product_series(9001, days=10, country="CL")
ok(r_cl["mixed_currency"] is False, "只看 CL 应是单币种")
ok(r_cl["y_label"] == "价格（CLP）", f"单币种纵轴标签应标币种，实得 {r_cl['y_label']!r}")
ok(all(any(v is not None for v in s.get("index") or []) for s in r_cl["series"]),
   "单币种时 index 也要给（可选口径）")
ok([v for v in r_cl["series"][0]["pts"] if v is not None] == [100_000, 110_000, 121_000],
   "单币种时绝对价位照常给")

# ---------------------------------------------------------------- 3. pending-only → 空 series
r_p = trends.product_series(9002, days=10)
ok(r_p["series"] == [],
   f"★ 只有 pending 观测的产品必须返回空 series（不是把 pending 点画上去），"
   f"实得 {len(r_p['series'])} 条")
ok(r_p["pending_obs"] == 3, f"pending 条数应报 3，实得 {r_p['pending_obs']}")
ok("待审计" in r_p["note"], "空图必须说清是待审计，不是没数据")
ok(r_p["audit_filter"] == "accepted", "返回里要标明审计口径")

# ---------------------------------------------------------------- 4. 合成线（by_channel=False）
r_c = trends.product_series(9001, days=10, by_channel=False)
ok(len(r_c["series"]) == 1, "合成线应只有一条")
s = r_c["series"][0]
ok(r_c["mixed_currency"] is True and all(v is None for v in s["pts"]),
   "跨币种合成线的绝对价位必须置空（与 _compose 同一条纪律）")
ci = [v for v in s["index"] if v is not None]
# 逐日配对链式：d2 的环比 = median(110000/100000, 5000/5000) = 1.05 → 105.0；
# d3 的环比 = median(121000/110000, 4500/5000) = median(1.1, 0.9) = 1.0 → 105.0。
# （旧口径"各线自基期指数的中位数"在这批数据上是 100/105/105.5 —— 差别在 §8 才致命）
ok(ci == [100.0, 105.0, 105.0],
   f"合成指数 = 逐日配对链式（环比中位数连乘），实得 {ci}")
ok(isinstance(s.get("pairs"), list) and len(s["pairs"]) == len(s["index"]),
   f"合成线要带 pairs 且与 index 逐位对齐（_trim 一起裁），实得 {s.get('pairs')}")
ok(s["currency"] == "", "跨币种合成线不该标某一个币种")
ok(s["name"] == "LenovoTest Tab P11", f"合成线名应是去重后的展示名，实得 {s['name']!r}")

r_c2 = trends.product_series(9001, days=10, country="CL", by_channel=False)
pts2 = [v for v in r_c2["series"][0]["pts"] if v is not None]
ok(pts2 == [100_000, 110_000, 121_000], f"单币种合成线应给绝对价位，实得 {pts2}")

# ---------------------------------------------------------------- 5. compare 走产品指数
cmp_ = trends.compare([{"kind": "product", "key": 9001, "country": ""}], days=10)
ok(cmp_["indexed"] is True, "含跨币种产品的对比必须指数化")
cp = [v for v in cmp_["series"][0]["pts"] if v is not None]
ok(cp == [100.0, 105.0, 105.0], f"对比应取产品的合成指数（链式），实得 {cp}")
ok(cmp_.get("audit_filters") == ["accepted"] and cmp_.get("mixed_audit") is False,
   f"只含产品线时口径单一（accepted），实得 {cmp_.get('audit_filters')}")
ok("accepted" in cmp_["note"], f"compare 的 note 要写明审计口径，实得 {cmp_['note']!r}")

# ---------------------------------------------------------------- 6. 关注清单 / 候选 的标签
trends.add_watch("product", 9001, country="CL")
wrows = trends.watchlist()
ok(bool(wrows) and wrows[0]["label"] == "LenovoTest Tab P11 · CL",
   f"关注清单标签应去重品牌词，实得 {[w['label'] for w in wrows]}")
sug = trends.suggest_watch(10)
lab = {x["id"]: x.get("label") for x in sug}
ok(lab.get(9001) == "LenovoTest Tab P11", f"候选标签应去重品牌词，实得 {lab}")

# ---------------------------------------------------------------- 7. 源码级（ast，不搜全文）
tree = ast.parse(inspect.getsource(trends))


def _fn(name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(fn):
    return {n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


ps = _fn("product_series")
consts = [n.value for n in ast.walk(ps)
          if isinstance(n, ast.Constant) and isinstance(n.value, str)]
ok(any("audit_status = 'accepted'" in x for x in consts),
   "product_series 的 SQL 必须是 audit_status = 'accepted'")
ok(not any("<> 'rejected'" in x for x in consts),
   "product_series 里不许再出现 audit_status <> 'rejected'（pending 会被当通过）")
ok("_index_of" in _calls(ps), "product_series 必须给每条线算 index")
# ★ 合成指数必须调 _chain_index（与 _basket_series 同一份实现），不许再自己取中位数。
#   两处各写一套的后果是"这边修好了、那边是另一套"，差集永远没人发现。
ok("_chain_index" in _calls(ps),
   "product_series 的合成指数必须走 _chain_index（逐日配对链式），不许各线指数取中位数")
ok("_chain_index" in _calls(_fn("_compose")),
   "_compose 也必须走同一个 _chain_index —— 指数算法只许有一份实现")
ok("display_label" in _calls(_fn("_watch_label")), "_watch_label 必须走 display_label")
ok("display_label" in _calls(_fn("suggest_watch")), "suggest_watch 的候选行要带 label")
ok(trends.MIN_POINTS == 2, f"最小真实观测点数应为 2，实得 {trends.MIN_POINTS}")

# ------------------------------------------------- 8. 构成效应：晚入的渠道线不许造成跳变
# 产品 9003：A 线（Ripley）100k → 120k → 120k（第 2 天涨 20% 后持平）
#            B 线（Paris） 缺  →  50k →  50k（第 2 天才上架，全程没动过价）
# 旧口径「各线自基期指数的中位数」：d1=100（只有 A）、d2=median(120,100)=110、
#   d3=median(120,100)=110 ⇒ 图上是"第 2 天跌 8.3%"，而那天 A 在涨、B 根本没动价。
#   这正是 [[composition-vs-level-effects]]「篮子固定了但今天谁出现没固定」那一课。
r_late = trends.product_series(9003, days=10, country="CL", by_channel=False)
li = [v for v in r_late["series"][0]["index"] if v is not None]
ok(li == [100.0, 120.0, 120.0, 120.0],
   f"★ 晚入的 B 线不许把合成指数拽下来（旧口径会给 100/120/110/110），实得 {li}")
ok(all(li[i] <= li[i + 1] + 1e-9 for i in range(len(li) - 1)),
   f"★ 没有任何一条线降过价，合成指数就不许出现下跌段，实得 {li}")

# 反向自证：同一批数据用旧口径（各线自基期指数取中位数）**必须**跳出那个假跌，
# 否则上面那条断言在这批数据上是恒真的（assertions-that-verify-nothing）。
_by_ch = trends.product_series(9003, days=10, country="CL", by_channel=True, trim=False)
_old = [trends._median([ln["index"][i] for ln in _by_ch["series"]
                        if ln["index"][i] is not None])
        for i in range(len(_by_ch["xs"]))]
_old_v = [v for v in _old if v is not None]
ok(any(_old_v[i] > _old_v[i + 1] + 1e-9 for i in range(len(_old_v) - 1)),
   f"反向自证失败：旧口径在这批数据上没有假跌，那么 §8 的断言就没在守什么，实得 {_old_v}")
ok(len(_by_ch["series"]) == 2, f"§8 的两条渠道线都要留住，实得 {len(_by_ch['series'])}")

# 绝对价位仍是构成敏感的（这是已知且刻意的：level 回答"现在什么价位"，index 回答"涨跌多少"）
lp = [v for v in r_late["series"][0]["pts"] if v is not None]
ok(lp == [100_000, 120_000, 85_000, 85_000],
   f"合成绝对价位仍是当天各渠道最低价的中位数（构成敏感，故只用指数判涨跌），实得 {lp}")

# ------------------------------------------------- 9. 跨品类挂接不进曲线
# 9,900 CLP 的耳机（po.category_code='audio'）挂在平板产品 9001 上、已 accepted。
r_cat = trends.product_series(9001, days=10, country="CL")
cl_pts = [v for v in r_cat["series"][0]["pts"] if v is not None]
ok(9_900 not in cl_pts,
   f"★ 跨品类挂接的 9,900 绝不能成为 CL 渠道线当天的最低价，实得 {cl_pts}")
ok(min(cl_pts) >= 100_000, f"★ CL 线的下沿应回到整机价位，实得 min={min(cl_pts)}")
# 库里 CL 有两条 pending：'tablet' 的 8,500（真该数）与 'audio' 的 7,700（跨品类，不该数）。
# 计数查询必须和取数查询用**同一组过滤**，否则 note 会报出一个图上根本不会出现的数。
ok(r_cat["pending_obs"] == 1,
   f"pending 计数要走同一组过滤：只数 'tablet' 那条，跨品类的 7,700 不算，"
   f"实得 {r_cat['pending_obs']}")
# 反向自证：把品类守卫拿掉，这条 9,900 确实会成为当天最低价 —— 否则上面两条恒真
_raw = db.q("""SELECT MIN(sale_price) p FROM price_obs
               WHERE rival_product_id=9001 AND country_code='CL' AND channel_id=9001
                 AND obs_date=? AND audit_status='accepted'""", (DAYS[0],))
ok(_raw and _raw[0]["p"] == 9_900,
   f"反向自证失败：不加品类守卫时那条 9,900 本该是当天最低价，实得 {_raw}")

print(f"trend_product_series: {PASS} 通过, {len(FAIL)} 失败  （临时库 {config.DB_PATH}）")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
