# -*- coding: utf-8 -*-
"""品类归属与挂接完整性的回归测试（2026-09-03 用户：从底层梳理，要绝对准确）。

三个根因（都不报错、页面看不出异常，只能靠断言守）：

  ① **品类来自搜索意图而非商品本身**：collector 把采集计划的 category 直接盖在
     每条结果上（collector.py `category=category`）。搜"Samsung 穿戴"返回的手机
     就被盖成穿戴 —— 用户点名「穿戴里为什么会有 OPPO A5」。

  ② **跨午夜的采集批次后处理全丢**：清洗/变动检测默认 obs_date=db.today()，
     而采集 12:30 开跑、跨午夜到次日才结束，此时 today() 已翻页 ⇒ 去处理
     "第二天的数据"，观测却标着开跑那天 ⇒ 一条都匹配不上。
     实测 10 天挂接率 0~2%（正常日 95%），77,883 条观测没挂上产品。

  ③ **规则层明确弃权时无人接手**：extract.crosscheck_category 遇到"标题无任何
     品类证据"就不动（3501 个产品里 706 个落在这个分支），保留着错误的搜索意图品类。
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ─────────── ① 跨午夜：后处理必须按批次的真实观测日期 ───────────
orch = (ROOT / "app/agents/orchestrator.py").read_text(encoding="utf-8")
ok("obs_dates" in orch and "WHERE run_id=?" in orch,
   "★ 后处理要按**本批次真实写入的观测日期**跑，不能用 today() —— "
   "跨午夜的批次会让清洗去处理第二天的空数据")
ok("_for_dates" in orch,
   "★ 跨午夜批次会写入两个 obs_date，必须逐个日期跑，只跑一个就漏一半")
ok(orch.index("obs_dates") < orch.index('_stage("清洗"'),
   "观测日期要在清洗之前算好")
for stage in ('_stage("清洗"', '_stage("价格变动检测"'):
    i = orch.index(stage)
    seg = orch[i:i + 400]
    ok("_for_dates" in seg, f"{stage} 必须走 _for_dates（逐日期）")

# ─────────── ② LLM 品类层：只接手规则层弃权的部分 ───────────
cat = (ROOT / "app/agents/categorizer.py").read_text(encoding="utf-8")
ok("crosscheck_category" in cat and "无任何品类证据" in cat,
   "★ LLM 层只接手**规则层弃权**的产品，不与规则层抢判定 —— "
   "两层互相覆盖会让错误无法追溯")
ok("category_source" in cat and "llm:" in cat,
   "每次判定要写来源（llm:模型名），否则每轮重复判同一批产品")
ok("reclass_audit" in cat,
   "★ 改动必须留痕 —— 几千条品类改动没有痕迹，事后发现判错了查不回依据")
ok("no_llm" in cat or "不猜" in cat,
   "★ LLM 不可用时保持现状，不猜不清空")
ok("CategoryAgent" in orch,
   "★ 品类判定要进采集流水线，不能只是一次性脚本")
ok(orch.index("CategoryAgent") < orch.index("PriceAuditAgent("),
   "★ 品类判定必须在价格审计**之前** —— 审计按品类判合理价区间，"
   "品类错了审计跟着错")

# ─────────── ③ 建产品的前置条件 ───────────
cl = (ROOT / "app/agents/cleaner.py").read_text(encoding="utf-8")
ok("po.category_code IS NOT NULL" in cl,
   "★ 品类为空的观测不建产品 —— rival_product.category_code 是 NOT NULL，"
   "硬塞会让整批挂接失败（实测 crosscheck 判『待定』时会清空品类）")
ok("_sku_whitelist" in cl,
   "sku_code 只认权威表白名单（渠道内部货号不能当型号名）")

# ─────────── ④ 库内实据：分类结果与挂接率 ───────────
from app import db  # noqa: E402

tot = db.q1("SELECT COUNT(*) c FROM rival_product")["c"]
# ★★ 2026-09-07 改口径：原来断言「≥95% 经过 **LLM** 判定」，测的是手段不是性质。
#   标题里明写着 "Smartphone …"/"Caixa de Som …" 的产品，规则层从明确设备名就能
#   确认品类，再花 token 让 LLM 复核是纯浪费 —— 而且这条断言逼着人往那个方向做。
#   真正要守的性质是：**没有产品还停在"采集时搜哪个品类"的原始值上、无据可查**。
#   ⇒ 判据改成「有判定来源」（llm: 或 rule:），并单独守住 LLM 层的下限
#     （规则层弃权的那部分必须有人接，不能靠把闸门放宽来凑数）。
ver = db.q1("SELECT COUNT(*) c FROM rival_product WHERE category_source LIKE 'llm:%' "
            "OR category_source LIKE 'rule:%'")["c"]
llm_n = db.q1("SELECT COUNT(*) c FROM rival_product "
              "WHERE category_source LIKE 'llm:%'")["c"]
ok(tot > 0 and ver / tot >= 0.99,
   f"★ 每个产品的品类都要有据可查（llm: 或 rule:），不许停在搜索意图的原始值上，"
   f"实得 {ver}/{tot}")
ok(tot > 0 and llm_n / tot >= 0.80,
   f"★ 规则层弃权的那部分必须由 LLM 接手 —— LLM 判定占比过低说明闸门被放宽了"
   f"（拿规则层确认去凑数），实得 {llm_n}/{tot}")

cats = {r["code"] for r in db.q("SELECT code FROM category WHERE enabled=0")}
ok({"other", "accessory"} <= cats,
   "★ other / accessory 要作为**禁用品类**存在 —— 它们是合法值（category_code "
   "有 NOT NULL 约束）但自动排除出分析，比置 NULL 更能保留『它到底是什么』")

# ★ 排除今天：采集进行中时今天的观测还没到清洗阶段，挂接率天然是 0，
#   混进来会把断言压到线下（实测 85.0% 报失败，排除后 89.6%）。
#   但**昨天及更早**必须达标 —— 09-03 只挂 24% 就是靠这条断言抓出来的。
link = db.q1("""SELECT COUNT(*) n, SUM(rival_product_id IS NOT NULL) l
                FROM price_obs po JOIN brand b ON b.id=po.brand_id
                WHERE b.is_ours=0 AND po.product_kind='device'
                  AND po.obs_date < date('now','localtime')""")
rate = link["l"] / link["n"] if link["n"] else 0
ok(rate >= 0.85,
   f"★ 非我方整机观测的产品挂接率要 ≥85%，实得 {rate:.0%} "
   f"（{link['l']}/{link['n']}）—— 低于此说明后处理又漏跑了")

# 报告只遍历启用品类，禁用品类不许出现在分析里
from app.agents.weekly import WeeklyReportAgent  # noqa: E402

ag = WeeklyReportAgent()
active = set(ag._active_cats())
ok(not (active & {"other", "accessory"}),
   f"★ 报告的品类全集不能包含 other/accessory，实得 {sorted(active)}")
ok(len(active) >= 5, f"五个分析品类都要在，实得 {sorted(active)}")

# ─────────── ⑤ RAM/ROM：手机平板必须有配置才可比 ───────────
ex = (ROOT / "app/scraping/extract.py").read_text(encoding="utf-8")
ok("len(cands) == 2" in ex and "lo <= 32" in ex,
   "★ 并列写法要能解析：「Galaxy A17, 256GB, 8GB」「Poco C85 6GB 128GB」—— "
   "不带 RAM 字样也不带 + 号，三条旧规则都接不住，RAM 整条丢掉")
ok("lo != hi" in ex,
   "两值相等时无从分辨（『8GB 8GB』），不猜")

from app.scraping.extract import parse_ram_rom  # noqa: E402

for title, want in [
    ("Celular Samsung Galaxy A17, 256GB, 8GB, 50MP", (8, 256)),
    ("Smartphone Xiaomi Poco C85 6GB 128GB", (6, 128)),
    ("Tablet Xiaomi Redmi Pad SE 4GB 64GB Wi-Fi", (4, 64)),
    ("Galaxy S24 256GB 8GB RAM", (8, 256)),
    ("HONOR MAGIC 8 PRO 12+512 GB", (12, 512)),
    ("Motorola Edge 70 Fusion Plus512GB 5G", (None, 512)),
]:
    ok(parse_ram_rom(title) == want,
       f"RAM/ROM 解析：{title[:44]} 应得 {want}，实得 {parse_ram_rom(title)}")

dbcols = {c["name"] for c in db.q("PRAGMA table_info(price_obs)")}
ok("spec_source" in dbcols,
   "★ 推断来的 RAM 要标来源（product-spec）—— 第一手解析与推断值必须可区分")

# ★ 推断口径按来源分开验，两种来源的安全前提不同：
#   'product-spec'（安卓）：RAM 随容量变，只有观测 ROM == 产品 ROM 才敢推
#   'hubweb.cn'（苹果）：同机型 RAM **不随容量变**（iPhone 17 Pro Max
#     256G/512G/1T 都是 12G），所以按机型填即可，容量不必相等 ——
#     用同一条断言卡两者会误报（实测误报 51 条全是正确的苹果回填）。
android_bad = db.q1("""SELECT COUNT(*) c FROM price_obs po
                       JOIN rival_product rp ON rp.id=po.rival_product_id
                       WHERE po.spec_source='product-spec' AND po.rom_gb IS NOT NULL
                         AND rp.rom_gb IS NOT NULL AND po.rom_gb <> rp.rom_gb""")["c"]
ok(android_bad == 0,
   f"★ 安卓侧只有同变体（观测 ROM == 产品 ROM）才可推断 RAM，"
   f"实测有 {android_bad} 条跨变体推断 —— 同机型不同变体 RAM 不同，猜就是编造")

apple_bad = db.q1("""SELECT COUNT(*) c FROM price_obs po
                     JOIN rival_product rp ON rp.id=po.rival_product_id
                     JOIN brand b ON b.id=rp.brand_id
                     WHERE po.spec_source='hubweb.cn' AND b.name<>'Apple'""")["c"]
ok(apple_bad == 0,
   f"★ hubweb.cn 是苹果专用源，不能用到别的品牌上（实测 {apple_bad} 条越界）")



# ─────────── ⑥ 促销角标：新增词表（2026-09-03 实测 3.6 万条标题受影响）───────────
# 这批角标不剥的后果不是"名字难看"，而是**型号归一化把角标当主语**，
# 产出「4 Cuotas Sin Interes」「Dias R 4 Cuotas」这种假产品，
# 再把不同商品的观测并到同一个假产品下 —— 价格曲线直接失真。
from app.scraping.extract import detect_condition, strip_ui_chrome  # noqa: E402

for title, must_start in [
    ("4 cuotas sin interés GENÉRICO PANTALLA COMPATIBLE CON OPPO", "GENÉRICO"),
    ("CUPÓN XIAOMI10 4 y 10 cuotas sin interés XIAOMI SMARTPHONE", "XIAOMI"),
    ("Días R 4 cuotas sin interés DELL NOTEBOOK DELL 5430", "DELL"),
    ("regalo a $1 CUPÓN INTEL10 4 cuotas sin interés ACER NITRO", "ACER"),
    # 被上游截断成半个词的角标残片
    ("sta Previa Días R Reacondicionado 4 cuotas sin interés VIVO Y29", "VIVO"),
    ("ncluye regalo a $1 CUPÓN ACER10 4 cuotas sin interés ACER 5", "ACER"),
    ("Previa TARJETAZO Reacondicionado 4 cuotas sin interés VIVO C", "VIVO"),
    # 2026-09-04 现行犯：2 字残片「a Previa」+ 五层叠加 + Ripley 品牌芯片评分「ACER 5.0」
    # → 建出假产品 #9204「A Previa Tarjetazo Cupon」（obs 218761）
    ("a Previa TARJETAZO CUPÓN ACER10 4 cuotas sin interés ACER 5.0 NOTEBOOK ACER NITRO LITE 16", "NOTEBOOK ACER"),
    # 赠品尾巴
    ("Celular HONOR 600 512GB 5G Naranja + Audifonos Clip", "Celular HONOR 600"),
]:
    got = strip_ui_chrome(title)
    ok(got.startswith(must_start),
       f"角标剥离：{title[:40]} 应以「{must_start}」开头，实得「{got[:40]}」")

# ★ 反向守卫：这些**不能**被剥（型号里的 + / 真商品名 / 中间位置的成色词）
for title in ("Samsung Galaxy Tab A11+ 128GB", "Apple iPad Air 11+ WiFi",
              "Dias Rojos Samsung Galaxy S25", "Previamente Usado Samsung",
              "Estación de carga Anker", "Celular Reacondicionado HONOR 600 512GB"):
    ok(strip_ui_chrome(title) == title,
       f"★ 不能误剥真商品名：{title}")

ok(detect_condition("Reacondicionado 4 cuotas VIVO Y29") == "refurb",
   "★ 行首成色词被剥掉后，成色仍要由 detect_condition 独立解析出来 —— "
   "剥的是角标位置，不是信息本身")
ok(detect_condition("MacBook Pro 15 (A1990) Reacondicionada LikeShop") == "refurb",
   "★ 成色词要收阴性/复数：432 行「Reacondicionada」曾以 condition='new' 进价格基线")
ok(detect_condition("Samsung Galaxy S25 diseño renovado") == "new",
   "renovado 要排除「diseño renovado」这类营销语（extra 里有页面文本）")

import re as _re  # noqa: E402

_JUNKNAME = _re.compile(
    r"(cuotas|cup[óo]n|previa|patrocinado|^add |dias r|regalo a|tarjetazo)", _re.I)
_bad = [r["model_name"] for r in db.q("SELECT model_name FROM rival_product")
        if _JUNKNAME.search(r["model_name"] or "")]
ok(not _bad,
   f"★ 产品表里不能有以促销角标命名的假产品，实测 {len(_bad)} 个：{_bad[:3]}")

# ─────────── ⑦ 限流自适应：撞 195 次 429 换来的教训 ───────────
# 旧逻辑是"固定退避 60 秒 + 无限重试"，结果 2026-09-03 那轮撞了 195 次 429
# （白等 3 小时还在继续压），把 IP 级限流坐实，之后连 Selenium 都拿不到页。
# 限流不是"等一下就好"的抖动，是站方在说**今天到额了**。
gsm = (ROOT / "app/scraping/specsource/gsmarena.py").read_text(encoding="utf-8")
ok("_streak" in gsm and "max_block_streak" in gsm,
   "★ 必须有连续限流计数与上限 —— 固定退避+无限重试会把限流坐实")
ok("give_up" in gsm,
   "★ 连续挨打要能收工，把剩下的留给下一轮（已取到的都已落库，天然可续）")
ok("2 ** (self._streak - 1)" in gsm,
   "退避要指数增长，不是固定 60 秒")
fs = (ROOT / "tools/fetch_specs.py").read_text(encoding="utf-8")
ok(fs.count("g.give_up") >= 2,
   "★ give_up 要在品牌循环与产品循环两处都生效，否则收工不了")
ns = (ROOT / "tools/nightly_specs.py").read_text(encoding="utf-8")
ok("collection_busy" in ns and "rate_limited" in ns,
   "★ 夜间任务要先让路采集、再探限流，两道闸都过了才开跑")
ok('"--delay", "20"' in ns,
   "夜间补全用 20 秒间隔（6 秒那次把额度打爆了）")
ok("_revoke_stale_inferences" in ns,
   "★ 补完规格必须刷新推断 —— 规格补全会改产品 ROM，"
   "旧的同变体推断会悬空（实测一次就有 76 条）")

# ─────────── ⑧ 跨品类竞品匹配：库内不变量（2026-09-03 用户截图事故）───────────
# 这条断言在防什么：
#   用户看到「我方 SonicBuds 5（audio）」的竞品列表里出现「Xiaomi Watch 5
#   Active（wearable）」。两边的 category_code 当时都是**对的**，匹配器的候选池
#   SQL 也一直有 rp.category_code=? 硬约束 —— 引擎自己产不出这种行。
#   真正的漏洞是**改品类的路径不触发下游重算**：匹配落库时两边同品类（合法），
#   之后 LLM 重分类把友商品类改了，那一行当场变成跨品类脏行。
#   而它**没有任何外在症状**：不报错、不变色、computed_at 也不动，
#   界面上和正常匹配长得一模一样 —— 只能靠断言守，或者等用户截图。
#
# 口径与两处防线（matcher.rebuild_all 的收尾自检、main.py cmd_doctor 的离线体检）
# 完全一致：只查 is_excluded=0 的行。被人工排除的历史脏行用户看不到，
# 而且删掉反而丢了"用户说过这不是竞品"这个判断。
#
# 一旦这条挂掉，说明又有一条改品类/改指 rival_product_id 的路径没做下游失效，
# 查 reclass_audit 找是谁改的，跑 CompetitorMatcher().rebuild_all() 清掉。
xcat = db.q1("""
    SELECT COUNT(*) c FROM competitor_match cm
    JOIN my_product    mp ON mp.id = cm.my_product_id
    JOIN rival_product rp ON rp.id = cm.rival_product_id
    WHERE mp.category_code <> rp.category_code
      AND cm.is_excluded = 0""")["c"]
ok(xcat == 0,
   f"★ 库里不许有**跨品类**竞品匹配（我方与友商品类不同），实测 {xcat} 条 —— "
   f"耳机的竞品是手表这类脏行不报错不变色，只能靠这条断言守")

# 配套守住**防线本身**：品类一改，基于旧品类算出的匹配必须当场作废。
# 只断言"库里是 0"是不够的 —— 谁把 categorizer 里的失效动作删了，
# 库里也要等到下一次重分类才脏，那时又是一次截图事故。
ok("DELETE FROM competitor_match" in cat,
   "★ CategoryAgent 改判品类时必须**当场作废**受影响的竞品匹配 —— "
   "品类是匹配的输入，输入变了还留着旧结论就是跨品类脏行")
ok("is_confirmed=0" in cat and "is_excluded=0" in cat,
   "作废的守卫要和 matcher._persist 一致：只清算法自己写的，人工确认/排除的不动")
mt = (ROOT / "app/matching/matcher.py").read_text(encoding="utf-8")
ok("cross_category" in mt and "mp.category_code <> rp.category_code" in mt,
   "★ rebuild_all 收尾要自检跨品类不变量并把告警冒出来 —— "
   "外部工具（重分类、合并改指）绕过 Agent 直接改库时，这是唯一的哨兵")

print(f"category: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
