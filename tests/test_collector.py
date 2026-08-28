# -*- coding: utf-8 -*-
"""采集链路离线测试：假引擎注入 HTML，跑真适配器 + 真数据库。

不联网，但走的是和真实采集完全相同的代码路径
（build_search_url → parse_listings → parse_detail → _persist）。
用临时库，跑完自清，不污染 data/intel.db。

跑法： python tests\test_collector.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ★ 必须在 import app.db 之前改掉 DB 路径，否则会连到生产库
from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_test_"))
config.DB_PATH = _TMP / "test.db"
config.PROFILE_DIR = _TMP / "profiles"
config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

from app import db  # noqa: E402
from app.scraping.channels import build_adapter  # noqa: E402
from app.scraping.collector import Collector  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r}  want={want!r}")


def check_true(name, cond, hint=""):
    check(name + (f" ({hint})" if hint else ""), bool(cond), True)


# ---------------------------------------------------------------- 假页面

LIVERPOOL_LIST = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"Product","name":"Samsung Galaxy S25 Ultra 512GB 12GB RAM Negro",
  "brand":{"name":"Samsung"},"url":"/producto/galaxy-s25-ultra-512",
  "offers":{"@type":"Offer","price":"32999.00","priceCurrency":"MXN",
            "availability":"http://schema.org/InStock"}},
 {"@type":"Product","name":"Apple iPhone 16 Pro 256GB Titanio",
  "brand":{"name":"Apple"},"url":"/producto/iphone-16-pro-256",
  "offers":{"@type":"Offer","price":"27999.00","priceCurrency":"MXN",
            "availability":"http://schema.org/InStock"}}]}
</script></head><body></body></html>"""

LIVERPOOL_DETAIL = """<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Samsung Galaxy S25 Ultra 512GB 12GB RAM Negro",
 "brand":{"name":"Samsung"},
 "offers":{"@type":"Offer","price":"32999.00","priceCurrency":"MXN",
  "availability":"http://schema.org/InStock",
  "seller":{"name":"Samsung Tienda Oficial"}}}
</script></head><body>
<div class="seller-name">Samsung Tienda Oficial</div>
<p>12 meses sin intereses</p>
<table><tr><th>Memoria RAM</th><td>12 GB</td></tr>
       <tr><th>Almacenamiento</th><td>512 GB</td></tr>
       <tr><th>Pantalla</th><td>6.9 pulgadas</td></tr></table>
</body></html>"""

AMAZON_LIST = """<html><body>
<div data-component-type="s-search-result">
  <h2><a class="a-link-normal" href="/dp/B0TEST1"><span>Motorola Moto G84 5G 256GB 12GB RAM</span></a></h2>
  <span class="a-price"><span class="a-offscreen">$5,499.00</span>
    <span class="a-price-whole">5,499</span><span class="a-price-fraction">00</span></span>
  <span class="a-price a-text-price"><span class="a-offscreen">$7,999.00</span></span>
  <div>12 meses sin intereses</div>
</div>
<div data-component-type="s-search-result">
  <h2><a class="a-link-normal" href="/dp/B0TEST2"><span>Xiaomi Redmi Note 14 Pro 256GB Reacondicionado</span></a></h2>
  <span class="a-price"><span class="a-offscreen">$4,199.00</span></span>
</div>
</body></html>"""

IPHONE_DETAIL = """<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Apple iPhone 16 Pro 256GB Titanio Natural",
 "brand":{"name":"Apple"},
 "offers":{"@type":"Offer","price":"27999.00","priceCurrency":"MXN",
  "availability":"http://schema.org/InStock","seller":{"name":"Liverpool"}}}
</script></head><body><div class="seller-name">Vendido por Liverpool</div></body></html>"""

# ★ 回归夹具：详情页带「相关推荐」区块，推荐商品也有自己的 JSON-LD。
#   取第一个会把 32999 的 Galaxy 价格写到 27999 的 iPhone 上。
IPHONE_DETAIL_WITH_RECOMMENDS = """<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Samsung Galaxy S25 Ultra 512GB 12GB RAM Negro",
 "brand":{"name":"Samsung"},
 "offers":{"@type":"Offer","price":"32999.00","priceCurrency":"MXN",
  "seller":{"name":"Samsung Tienda Oficial"}}}
</script>
<script type="application/ld+json">
{"@type":"Product","name":"Apple iPhone 16 Pro 256GB Titanio Natural",
 "brand":{"name":"Apple"},
 "offers":{"@type":"Offer","price":"27999.00","priceCurrency":"MXN",
  "availability":"http://schema.org/InStock","seller":{"name":"Liverpool"}}}
</script></head><body>Productos relacionados</body></html>"""

SHOPEE_EMPTY = "<html><body><div>Nenhum resultado encontrado</div></body></html>"


class FakeEngine:
    """按 URL 关键字返回预置页面，其余返回 (None, None) 模拟被拦。"""

    def __init__(self, pages: dict, blocked_status="blocked"):
        self.pages = pages
        self.last_status = "ok"
        self.last_engine = "playwright"
        self.forced_fallback_hosts = set()
        self.fetched = []
        self._blocked_status = blocked_status

    def warm_up(self, *_a, **_kw):
        pass

    def fetch(self, url, **_kw):
        self.fetched.append(url)
        for key, html in self.pages.items():
            if key in url:
                self.last_status = "ok"
                from bs4 import BeautifulSoup
                return BeautifulSoup(html, "lxml").get_text(" ", strip=True), html
        self.last_status = self._blocked_status
        return None, None


# ---------------------------------------------------------------- 准备

db.init_db()
MX = db.q1("SELECT * FROM country WHERE code='MX'")
BR = db.q1("SELECT * FROM country WHERE code='BR'")
CH_LIV = db.q1("SELECT * FROM channel WHERE code='liverpool' AND country_code='MX'")
CH_AMZ = db.q1("SELECT * FROM channel WHERE code='amazon' AND country_code='MX'")
CH_SHOPEE = db.q1("SELECT * FROM channel WHERE code='shopee' AND country_code='BR'")
BR_SAMSUNG = db.q1("SELECT * FROM brand WHERE name='Samsung'")
BR_MOTO = db.q1("SELECT * FROM brand WHERE name='Motorola'")

print("== 基础配置 ==")
check_true("6国已灌入", MX and BR)
check_true("Liverpool 渠道存在", CH_LIV is not None)
check("Liverpool 适配器", CH_LIV["adapter"], "liverpool")
check("Shopee 强制兜底", CH_SHOPEE["adapter"], "shopee")

print("\n== 搜索 URL 构造 ==")
liv = build_adapter(CH_LIV, MX)
check("Liverpool 搜索URL", liv.build_search_url("celular Samsung"),
      "https://www.liverpool.com.mx/tienda?s=celular%20Samsung")
meli_ch = db.q1("SELECT * FROM channel WHERE code='meli' AND country_code='MX'")
meli = build_adapter(meli_ch, MX)
check("MercadoLibre 路径式URL", meli.build_search_url("Galaxy S25 Ultra"),
      "https://listado.mercadolibre.com.mx/galaxy-s25-ultra")

print("\n== JSON-LD 列表解析（Liverpool）==")
items = liv.parse_listings(LIVERPOOL_LIST, "")
check("解析条数", len(items), 2)
if len(items) == 2:
    s25 = items[0]
    check("标题", s25.title, "Samsung Galaxy S25 Ultra 512GB 12GB RAM Negro")
    check("价格 MXN", s25.sale_price, 32999.0)
    check("品牌", s25.brand_guess, "Samsung")
    check("RAM", s25.ram_gb, 12)
    check("ROM", s25.rom_gb, 512)
    check("颜色", s25.color, "negro")
    check("链接绝对化", s25.url, "https://www.liverpool.com.mx/producto/galaxy-s25-ultra-512")
    check("来源", s25.source, "jsonld")

print("\n== 详情页补全 ==")
if items:
    d = liv.parse_detail(LIVERPOOL_DETAIL, "Samsung Tienda Oficial 12 meses sin intereses", items[0])
    check("卖家名", d.seller_name, "Samsung Tienda Oficial")
    check("卖家类型", d.seller_type, "official")
    check("分期已抓", d.installments is not None, True)
    check("规格表已抓", "Memoria RAM" in d.specs, True)
    check("详情已标记", d.detail_fetched, True)

print("\n== Amazon 拆价解析（整数/小数分两个span）==")
amz = build_adapter(CH_AMZ, MX)
aitems = amz.parse_listings(AMAZON_LIST, "")
check("Amazon 条数", len(aitems), 2)
if len(aitems) == 2:
    check("现价", aitems[0].sale_price, 5499.0)
    check("划线原价", aitems[0].list_price, 7999.0)
    check("翻新识别", aitems[1].condition, "refurb")
    check("RAM/ROM", (aitems[0].ram_gb, aitems[0].rom_gb), (12, 256))

print("\n== ★ 详情页 JSON-LD 张冠李戴防护 ==")
# 场景：iPhone 的详情页里，推荐位的 Galaxy JSON-LD 排在前面。
# 正确行为 = 认出属于 iPhone 的那个（价格 27999 与列表页一致），
# 而不是无脑取第一个（32999 的 Galaxy）。
iphone_listing = [i for i in items if "iPhone" in i.title][0]
before_price = iphone_listing.sale_price
d2 = liv.parse_detail(IPHONE_DETAIL_WITH_RECOMMENDS, "Productos relacionados", iphone_listing)
check("价格未被推荐位污染", d2.sale_price, 27999.0)
check("标题未被张冠李戴", "iPhone" in d2.title, True)
check("品牌未被污染", d2.brand_guess, "Apple")
check_true("卖家未被污染", d2.seller_name != "Samsung Tienda Oficial",
           f"seller={d2.seller_name}")
check("原价保持一致", before_price, 27999.0)

print("\n== 全链路：采集 → 入库 ==")
run_id = db.start_run("test", {"note": "offline"})
engine = FakeEngine({"liverpool.com.mx/tienda": LIVERPOOL_LIST,
                     "galaxy-s25-ultra-512": LIVERPOOL_DETAIL,
                     "iphone-16-pro-256": IPHONE_DETAIL})
col = Collector(engine, run_id, {"max_products_per_query": 20})
rows, status = col.collect_unit(CH_LIV, MX, BR_SAMSUNG, "phone")
check("采集状态", status, "ok")
check_true("写入行数>0", rows > 0, f"rows={rows}")

obs = db.q("SELECT * FROM price_obs ORDER BY id")
check("入库条数", len(obs), 2)
if obs:
    s = [o for o in obs if "Galaxy S25" in o["title"]][0]
    check("入库价格", s["sale_price"], 32999.0)
    check("入库币种", s["currency"], "MXN")
    check("入库国家", s["country_code"], "MX")
    check("卖家类型入库", s["seller_type"], "official")
    check("品牌识别", s["brand_id"], BR_SAMSUNG["id"])
    a = [o for o in obs if "iPhone" in o["title"]]
    check_true("品牌词搜出的他家产品也入库", len(a) == 1)
    if a:
        apple = db.q1("SELECT id FROM brand WHERE name='Apple'")
        check("★ iPhone 被判给 Apple 而非搜索用的 Samsung", a[0]["brand_id"], apple["id"])

print("\n== 幂等：同日重跑不产生重复行 ==")
rows2, _ = col.collect_unit(CH_LIV, MX, BR_SAMSUNG, "phone")
check("重跑新增行数", rows2, 0)
check("总行数不变", len(db.q("SELECT id FROM price_obs")), 2)

print("\n== ★ 详情页缓存命中后仍须幂等 ==")
# 回归：缓存命中会跳过详情页请求。如果标题不从缓存回填，
# row_hash 会变（详情页补全过的长标题退回成列表页短标题），
# 同一商品每天新增一行 —— 幂等性静默失效、数据翻倍。
cache_rows = db.q("SELECT url, specs_json FROM product_page_cache")
check_true("缓存已写入", len(cache_rows) > 0, f"n={len(cache_rows)}")
check_true("★ 标题已进缓存（参与 row_hash）",
           all("_cached_title" in (r["specs_json"] or "") for r in cache_rows))
rows3, _ = col.collect_unit(CH_LIV, MX, BR_SAMSUNG, "phone")
check("第三次重跑新增行数", rows3, 0)
check("总行数仍不变", len(db.q("SELECT id FROM price_obs")), 2)
titles = {r["title"] for r in db.q("SELECT title FROM price_obs")}
check_true("标题保持详情页的完整版本",
           any("Titanio Natural" in t for t in titles), str(sorted(titles))[:120])

print("\n== 被拦：如实上报，不静默当成空 ==")
blocked_engine = FakeEngine({}, blocked_status="blocked")
col2 = Collector(blocked_engine, run_id, {"max_products_per_query": 20})
_, st = col2.collect_unit(CH_AMZ, MX, BR_MOTO, "phone")
check("被拦状态", st, "blocked")

wall_engine = FakeEngine({}, blocked_status="login_wall")
col3 = Collector(wall_engine, run_id, {"max_products_per_query": 20})
# ★ 登录墙这条语义仍然要在，但**不能再拿 MercadoLibre 举例** ——
#   它已经改成只走官方 API，根本不碰引擎，永远走不到登录墙分支。
#   换一个仍走网页的渠道来守这条语义。
_, st2 = col3.collect_unit(CH_LIV, MX, BR_SAMSUNG, "phone")
check("登录墙状态", st2, "login_wall")

print("\n== ★ MercadoLibre 没 token 时必须报「缺凭据」，不许爬网页 ==")
# 由来：robots.txt 把 ClaudeBot/Claude-User 与 GPTBot 并列在一组下共用
# `Disallow: /`（全站禁）。原来的实现是"API 不行就降级爬网页"——
# 那不是降级，是越过站方明确的拒绝。现在改成只走官方 API。
import inspect as _insp  # noqa: E402

from app.scraping.channels.meli import MeliAdapter  # noqa: E402

_meli_src = _insp.getsource(MeliAdapter.collect)
check_true("★collect 里不再碰抓取引擎", "engine.fetch" not in _meli_src)
# 注意别误伤 force_fallback —— 那是基类的**引擎**兜底（Playwright↔Selenium），
# 和"网页取数兜底"完全是两回事，它该留着。
check_true("★没有残留的网页取数兜底方法",
           not any(n for n in dir(MeliAdapter)
                   if "web_fallback" in n.lower() or "_web_" in n.lower()))
_a = MeliAdapter({"base_url": "https://x", "search_url": "https://x/{q}"},
                 {"code": "MX", "currency": "MXN"})
_items, _st = _a.collect(None, "samsung")
check("★无 token 返回空", _items, [])
check("★状态说明「只走API」而不是失败", _st, "no_token:api_only")

print("\n== ★ 挑战页必须按本地语言识别（否则误判成 empty）==")
from app.scraping.browser import BLOCK_MARKERS  # noqa: E402

# Ripley 秘鲁站标题「Un momento…」是 Imperva 的西语挑战页。
# 漏了这个词就会当成"页面正常但没商品"→ 报 empty，
# 诊断指错方向（"需要写专用适配器"），且不触发冷却换设备重试。
for word in ("un momento", "aguarde um momento", "verificando su navegador",
             "just a moment"):
    check_true(f"拦截词表含「{word}」", word in BLOCK_MARKERS)
check_true("★西语与英语变体都覆盖",
           any("momento" in m for m in BLOCK_MARKERS)
           and any("moment" in m and "momento" not in m for m in BLOCK_MARKERS))

print("\n== ★ 解析低产 ≠ 搜索没生效（两种故障要分开报）==")
from app.scraping.relevance import LOW_YIELD_HINT, check_yield  # noqa: E402

_html_many = "".join(f'<a href="/p/prod-{i}">x</a>' for i in range(80))
r = check_yield(_html_many, 4)
check_true("★页面80链接只解析4条 → 报低产", r is not None)
if r:
    check("统计到的链接数", r["links_on_page"], 80)
    check_true("理由点明是解析器不匹配", "专用适配器" in r["reason"], r["reason"][:50])
    check_true("★理由明确排除「没货」与「搜索没生效」",
               "没货" in r["reason"] and "搜索没生效" in r["reason"])
check("解析条数正常时不报", check_yield(_html_many, LOW_YIELD_HINT + 1), None)
check("页面本来就没几个商品时不报", check_yield('<a href="/p/a">x</a>', 1), None)
check("空 HTML 不报", check_yield("", 0), None)

print("\n== ★ 小样本不做整批作废判定（防误杀）==")
from app.scraping.relevance import MIN_BATCH_SIZE, check_batch  # noqa: E402

check_true("样本量下限 >= 8", MIN_BATCH_SIZE >= 8, str(MIN_BATCH_SIZE))
# 实测 Alkosto：解析出 4 条、其中 1 条相关 → 25% < 34% → 被判「搜索没生效」，
# 而 search_url 其实是对的
_r = check_batch(["Celular Samsung Galaxy A17", "Lavadora LG 20kg",
                  "Televisor 55 pulgadas", "Bicicleta montaña"], "celular samsung")
check("★4 条样本不判搜索失效", _r["verdict"], "too_few")
check_true("如实说明是样本不足", "样本不足" in _r["reason"], _r["reason"][:40])
# 样本足够时仍要能挡住真正的搜索失效
_junk = ["Colchón matrimonial", "Botas de trabajo", "Zapatos Flexi",
         "Sofá 3 puestos", "Bicicleta BMX", "Licuadora Oster",
         "Refrigerador 300L", "Mesa comedor"]
check("★样本足够时仍挡住真失效", check_batch(_junk, "celular samsung")["verdict"],
      "search_ineffective")
check_true("失效理由同时列出两种可能",
           "解析器认不出" in check_batch(_junk, "celular samsung")["reason"])

print("\n== ★ 型号词搜索不能整批作废（相关性闸误杀）==")
# 实测：Hiraoka 搜「Xiaomi Pad Mini」返回 Redmi Pad 2 / Slate 12X 等同品类型号，
# 相关率 25% ⇒ 被判"搜索失效"整批作废。但那批数据完全可用，而且往往就含目标型号。
# 用具体型号搜时，站点返回同品类其它型号是**正常行为**。
_mixed = ["XIAOMI Tablet Redmi Pad 2 11 128GB", "XIAOMI Tablet Redmi Pad 2 Pro 12.1",
          "ACME Tablet Slate 12 X 256GB", "XIAOMI Celular REDMI 15C 6.9",
          "Xiaomi Pad Mini 8.8 256GB", "Lenovo Tab M11",
          "Samsung Galaxy Tab A9", "Apple iPad 11"]
_r_sku = check_batch(_mixed, "Xiaomi Pad Mini", query_purpose="sku")
check("★型号词不判搜索失效", _r_sku["verdict"], "low_relevance")
check_true("★保留了可用条目而不是整批扔掉", len(_r_sku["kept"]) > 0,
           f"kept={len(_r_sku['kept'])}")
check_true("理由说明了为什么这是正常的", "属正常" in _r_sku["reason"])
_r_track = check_batch(_mixed, "Galaxy Tab S11", query_purpose="track")
check("track 用途同样宽松", _r_track["verdict"], "low_relevance")
# 但品类词搜索仍要严格：搜手机返回床垫必须整批作废
_r_disc = check_batch(_junk, "celular samsung", query_purpose="discover")
check("★品类词搜索仍严格拦截", _r_disc["verdict"], "search_ineffective")
_r_none = check_batch(["Lavadora LG", "Nevera Samsung"], "Xiaomi Pad Mini",
                      query_purpose="sku")
check("型号词零匹配 → too_few 而非失效", _r_none["verdict"], "too_few")

print("\n== ★「该渠道不卖这个牌子」≠「搜索失效」==")
# 实测 Hiraoka 搜「audifonos Apple」返回 QCY/Philips/Miray 的耳机 ——
# 品类 100% 正确，只是这家没 Apple 耳机。这是渠道选品的事实，不是故障。
# 判据是**品类**对不对，不是品牌对不对。
from app.scraping.relevance import category_of_query  # noqa: E402

_hira = ["QCY AUDIFONO BLUETOOTH CROSSKY C30", "PHILIPS Audifono True Wireless TAT350",
         "MIRAY Audifono inalambrico", "JBL Audifono Bluetooth Tune 520BT",
         "SONY Audifono WH-CH520", "XIAOMI Audifono Redmi Buds 6",
         "QCY Audifono T13", "ACME Audifono SonicBuds SE 3"]
_r_brand = check_batch(_hira, "audifonos Apple")
check("★品类对但没这个牌子 → brand_absent", _r_brand["verdict"], "brand_absent")
check_true("理由说明这是事实不是故障", "不是故障" in _r_brand["reason"])
# 品类都不对的仍必须判失效
check("★搜手机返回床垫仍是 search_ineffective",
      check_batch(_junk, "celular samsung")["verdict"], "search_ineffective")

print("\n== ★ 西语复数词尾不能让品类词失效 ==")
# \b(celular)\b 匹配 celular 但不匹配 celulares —— 西语葡语复数会让
# 所有 \b 结尾的品类词漏掉，实测把 8 条耳机全判成"与耳机无关"
for _q, _want in [("audifonos Apple", "audio"), ("celulares samsung", "phone"),
                  ("tablets Lenovo", "tablet"), ("smartwatch Fitbit", "wearable"),
                  ("laptops HP", "pc")]:
    check(f"复数「{_q}」识别品类", category_of_query(_q), _want)
check_true("单数也要能识别", category_of_query("celular samsung") == "phone")

print("\n== ★ 并行启动 driver 要互斥（FileExistsError 竞态）==")
# 5 个 worker 同时首次启动 undetected-chromedriver，会同时往同一路径
# 解压/patch chromedriver.exe → WinError 183。表现为大量渠道"failed"，
# 看起来像被风控，实际是共享资源初始化竞态。
import app.scraping.selenium_driver as _sd  # noqa: E402

check_true("★有全局启动锁", hasattr(_sd, "_LAUNCH_LOCK"))
_sdsrc = _insp.getsource(_sd) if (_insp := __import__("inspect")) else ""
check_true("★uc.Chrome 创建被锁包住",
           "with _LAUNCH_LOCK:" in _sdsrc and "uc.Chrome(**kw)" in _sdsrc)

print("\n== ★ 判定逻辑改版后，旧缓存必须失效 ==")
# 真实事故：修好「授权经销商≠品牌官方店」后，Coppel 仍有 8 条判成 brand_official，
# 因为命中缓存、沿用的是修复**之前**的结论 —— 表现为"修复没生效"。
from app.scraping.collector import (SELLER_LOGIC_VERSION,  # noqa: E402
                                    _cache_logic_version)

check_true("当前逻辑版本 >= 2", SELLER_LOGIC_VERSION >= 2, str(SELLER_LOGIC_VERSION))
check("★没有版本字段的旧缓存视为版本1",
      _cache_logic_version({"specs_json": '{"_cached_title":"x"}'}), 1)
check("能读出版本号",
      _cache_logic_version({"specs_json": '{"_logic_version":2}'}), 2)
check("坏 JSON 也当版本1（保守失效）",
      _cache_logic_version({"specs_json": "{{{坏"}), 1)
check("空缓存当版本1", _cache_logic_version({}), 1)
# 写入的缓存必须带上当前版本
_cache_rows = db.q("SELECT specs_json FROM product_page_cache")
check_true("★新写入的缓存带逻辑版本号",
           all('"_logic_version"' in (r["specs_json"] or "") for r in _cache_rows)
           if _cache_rows else True,
           f"n={len(_cache_rows)}")

print("\n== ★ Coppel：价格不带货币符号（33 个商品只认出 1 个的根因）==")
# Coppel 文案：「Precio de contado con descuento 26999 pesos, precio original: 29999 pesos」
# 通用价格正则要求 $/MXN 打头，所以整页 33 个商品只认出唯一带 $ 的那条。
from app.scraping.channels.sites import CoppelAdapter  # noqa: E402

_cpl = CoppelAdapter(
    {"name": "Coppel", "adapter": "coppel", "base_url": "https://www.coppel.com",
     "default_seller_type": "official"}, {"code": "MX", "currency": "MXN"})
_html = (
    '<a href="/pdp/celular-samsung-galaxy-s26-liberado-512-gb-violeta-pm-2275733">'
    'Oferta Celular Samsung Galaxy S26+ Liberado 512 GB Violeta '
    'Precio de contado con descuento 26999 pesos, precio original: 29999 pesos</a>'
    '<a href="/pdp/movistar-xiaomi-redmi-note-15-256-gb-negro-pm-2270243">'
    'Movistar Xiaomi Redmi Note 15 256 GB Negro Precio de contado $5,999</a>'
)
_items = _cpl.parse_listings(_html, "")
check("★两种价格写法都解析出来", len(_items), 2)
_by = {i.title[:20]: i for i in _items}
_s26 = next((i for i in _items if "S26" in i.title), None)
check_true("不带$的价格被识别", _s26 is not None and _s26.sale_price == 26999,
           f"got={_s26.sale_price if _s26 else None}")
check_true("★con descuento 是现价、precio original 是原价",
           _s26 is not None and _s26.list_price == 29999,
           f"list={_s26.list_price if _s26 else None}")
_xm = next((i for i in _items if "Redmi" in i.title), None)
check_true("带$的写法仍然可用", _xm is not None and _xm.sale_price == 5999)
check_true("标题从 URL slug 还原（卡片文本混着促销词）",
           _s26 is not None and "Galaxy S26" in _s26.title, str(_s26.title if _s26 else ""))
check_true("slug 尾部的商品ID被剥掉",
           _s26 is not None and "2275733" not in _s26.title)

print("\n== 空结果与被拦必须区分开 ==")
empty_engine = FakeEngine({"shopee.com.br": SHOPEE_EMPTY})
col4 = Collector(empty_engine, run_id, {"max_products_per_query": 20})
_, st3 = col4.collect_unit(CH_SHOPEE, BR, BR_SAMSUNG, "phone")
check("空结果状态", st3, "empty")
check_true("Shopee 已加入强制兜底名单",
           any("shopee" in h for h in empty_engine.forced_fallback_hosts))

print("\n== 采集单元留痕 ==")
db.finish_run(run_id, "ok")
units = db.q("SELECT * FROM scrape_unit WHERE run_id=?", (run_id,))
check_true("每个单元都留痕", len(units) >= 5, f"units={len(units)}")
statuses = {u["status"] for u in units}
check_true("状态语义齐全", {"ok", "blocked", "login_wall", "empty"} <= statuses,
           str(sorted(statuses)))
run = db.q1("SELECT * FROM scrape_run WHERE id=?", (run_id,))
check_true("批次汇总已写", run["pages_blocked"] >= 2, f"blocked={run['pages_blocked']}")

# ---------------------------------------------------------------- 清理
try:
    db.get_conn().close()
except Exception:
    pass

print("== ★ 会话失效要能自愈（否则一次进程死亡 = 整国采集全灭）==")
# 由来：手工跑Acme商城采集时重启了服务，服务启动的孤儿回收器按
# user-data-dir 匹配，把**正在用的** Chrome 当孤儿杀了。
# 之后 _ensure_driver 只检查"driver 对象还在"就返回 True，
# 于是同一个国家剩下的单元全部拿一个死会话去用，每个都报
# "not connected to DevTools" —— 每国第一个单元 ok、后三个 failed。
from app.scraping.selenium_driver import SeleniumBrowser  # noqa: E402

_b = SeleniumBrowser.__new__(SeleniumBrowser)


class _DeadDriver:
    @property
    def current_url(self):
        raise Exception("not connected to DevTools")


class _LiveDriver:
    current_url = "https://example.com"


_b._driver = _DeadDriver()
check("★死会话被识别出来", _b._session_alive(), False)
_b._driver = _LiveDriver()
check("活会话正常通过", _b._session_alive(), True)
_b._driver = None
check("driver 为 None 时不炸", _b._session_alive(), False)

_esrc = _insp.getsource(SeleniumBrowser._ensure_driver)
check_true("★_ensure_driver 真的调了探活", "_session_alive" in _esrc)
check_true("★探活失败会重建而不是硬用",
           "_session_alive" in _esrc and "_quit_driver" in _esrc)

print("== ★ 有进程在采集时，服务启动不许回收浏览器 ==")
# 同一次事故的另一半：回收器杀掉了别人正在用的实例。
_main = (ROOT / "main.py").read_text(encoding="utf-8")
check_true("★回收前先判断有没有进程在采集",
           "collecting" in _main and "reap_orphan_browsers" in _main)
check_true("★判据是最近有没有采集单元落库（不是 status=running，"
           "那个会被 reconcile 先改掉）",
           "scrape_unit" in _main and "-3 minute" in _main)
check_true("★僵尸轮次回收也受同一个开关约束",
           _main.index("collecting = bool") < _main.index("reconcile_dangling_runs"))

print("== ★ 体检判定不许给出自信的错误诊断 ==")
# 由来：Ripley 秘鲁被 Cloudflare Managed Challenge 整站 403，返回的是一个
# **有正文的挑战页**，解析自然 0 条 → 旧逻辑一律判 selector_broken/fix_selector。
# 于是体检信誓旦旦地说"选择器坏了"，我照着去改了半天选择器 ——
# 而真正的原因是没过挑战。**一个自信的错误诊断比"不知道"有害得多**：
# 它会主动把人引到错误的方向，还让人以为已经查明原因了。
from app.scraping.health import _verdict  # noqa: E402

check_true("empty 不再断言选择器坏了",
           _verdict({"status": "empty", "items": 0})[0] == "empty",
           f"得到 {_verdict({'status': 'empty', 'items': 0})}")
check_true("empty 的建议动作是去查，不是去修选择器",
           _verdict({"status": "empty", "items": 0})[1] == "investigate")
# 能明确认出来的拦截，必须各归各的（处置方式完全不同）
check_true("captcha → 需本人过验证",
           _verdict({"status": "captcha", "items": 0}) == ("need_captcha", "user_verify"))
check_true("throttled → 降频",
           _verdict({"status": "throttled", "items": 0})[0] == "rate_limited")
check_true("blocked → 降频",
           _verdict({"status": "blocked", "items": 0})[0] == "rate_limited")
check_true("login_wall → 配 token，与反爬无关",
           _verdict({"status": "login_wall", "items": 0}) == ("need_login", "need_login"))
# 搜索串味仍然要判死：它会**报告成功**却污染价格基线，是最危险的一类
check_true("irrelevant 仍判选择器/搜索失效",
           _verdict({"status": "irrelevant", "items": 5})[0] == "selector_broken")
check_true("ok 且条数够 → 健康",
           _verdict({"status": "ok", "items": 10}) == ("healthy", "continue"))
check_true("ok 但条数太少 → 降级而不是健康",
           _verdict({"status": "ok", "items": 1})[0] == "degraded")
check_true("brand_absent 是选品事实不是故障",
           _verdict({"status": "brand_absent", "items": 0})[0] == "healthy")

print("== ★ 接口原始数值不能过「显示串」解析器 ==")
# Entel 智利：接口给的是机器数 "999990.000000"。
# 拿 parse_price（按国家猜千分位/小数点，CLP 无小数位 ⇒ 点当千分位）去解，
# 会读成 999,990,000,000 → price_is_sane 全否 → _from_block 一条都返不出来
# → 整个渠道回落通用解析器 → **营销话术当标题、24 期月供当价格**入库，
# 而界面显示"健康 40 条"。实测真实价 999,990 vs 入库 10,833，差 92 倍。
from app.scraping.channels.sites import _raw_num  # noqa: E402
from app.scraping.extract import parse_price  # noqa: E402

check("原始数值直读", _raw_num("999990.000000"), 999990.0)
check("原始数值直读2", _raw_num("1549990.000000"), 1549990.0)
check("原始数值-已是数字", _raw_num(999990), 999990.0)
check("原始数值-零要挡掉", _raw_num("0"), None)
check("原始数值-负数挡掉", _raw_num("-5"), None)
check("原始数值-None", _raw_num(None), None)
check("原始数值-垃圾", _raw_num("abc"), None)
# 反向记录：这就是当初读错的那条路径，留着说明为什么不能用它
check_true("显示串解析器确实会读错机器数",
           parse_price("999990.000000", "CLP") != 999990,
           f"parse_price 给出 {parse_price('999990.000000', 'CLP')}")
# 显示串仍然要走本地化解析（CLP 的点是千分位）
check("显示串按本地化解析", parse_price("$999.990", "CLP"), 999990)

print("== ★ Entel 抠不到 JSON 时必须报空，不许回落通用解析 ==")
ent = build_adapter({"adapter": "entel_cl", "base_url": "https://miportal.entel.cl"},
                    {"code": "CL", "currency": "CLP"})
junk_html = ("<html><body>Cobertura satelital Este equipo es compatible con SMS "
             "satelitales, WhatsApp y datos. $10.833</body></html>")
check("无 JSON 时返回空而不是垃圾",
      len(ent.parse_listings(junk_html, "Cobertura satelital $10.833")), 0)

print("== ★ 新适配器已注册且可实例化 ==")
from app.scraping.channels import REGISTRY, build_adapter  # noqa: E402

for key in ("alkosto", "vtex"):
    check_true(f"{key} 在注册表里", key in REGISTRY)
a = build_adapter({"adapter": "alkosto", "base_url": "https://www.alkosto.com",
                   "category_urls": '{"phone": "https://www.alkosto.com/c/BI_101_ALKOS"}'},
                  {"code": "CO", "currency": "COP"})
check_true("alkosto 有 collect()", callable(getattr(a, "collect", None)))
check_true("alkosto 声明了 wants_category", getattr(a, "wants_category", False) is True)
# 品类码要从配置 URL 尾部解析出来，解析错会静默返回 0 条（nbHits=0 不报错）
check("alkosto 品类码解析", a._facet_code("phone"), "BI_101_ALKOS")
check("alkosto 未知品类返回 None", a._facet_code("tablet"), None)
check("alkosto 无品类返回 None", a._facet_code(None), None)

v = build_adapter({"adapter": "vtex", "base_url": "https://site.fastshop.com.br"},
                  {"code": "BR", "currency": "BRL"})
check_true("vtex 有 collect()", callable(getattr(v, "collect", None)))
# ★ 零价僵尸 SKU 必须被挡掉：接口的 hideUnavailableItems 参数不生效，
#   放进来会把价格基线整体拽向 0
check("vtex 零价 SKU 被过滤",
      v._from_product({"productName": "僵尸", "items": [
          {"sellers": [{"commertialOffer": {"Price": 0, "AvailableQuantity": 0}}]}]},
          "https://x"), None)
lst = v._from_product({"productName": "iPhone 17", "brand": "Apple", "link": "/p/1",
                       "items": [{"sellers": [{"commertialOffer": {
                           "Price": 5887.78, "ListPrice": 8089.0,
                           "AvailableQuantity": 3}}]}]}, "https://x")
check_true("vtex 正常商品能解析出来", lst is not None)
check("vtex 现价", round(lst.sale_price, 2), 5887.78)
check("vtex 划线价", lst.list_price, 8089.0)
# 不打折时 ListPrice==Price，不能当划线价（否则折扣算成 0% 与"无划线价"混淆）
lst2 = v._from_product({"productName": "X", "items": [{"sellers": [{"commertialOffer": {
    "Price": 100.0, "ListPrice": 100.0, "AvailableQuantity": 1}}]}]}, "https://x")
check("vtex 不打折时无划线价", lst2.list_price, None)

shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
