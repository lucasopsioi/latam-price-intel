# -*- coding: utf-8 -*-
"""VOC 抓谁：目标选择必须按国家分区，不能变成"按币值大小排名"。

跑法：  python tests\test_voctargets.py

★ 这个文件的由来（真实事故）：
  评论库里只有智利 1434 条、哥伦比亚 293 条，墨西哥/秘鲁/巴西**一条都没有**。
  第一反应是"那几国没抓到价格页"——查下来正相反，五国价格页全在，
  墨西哥 1028 个还是最多的那个。

  真因是 VOC 目标选择的 `ORDER BY known_reviews DESC, sale_price DESC`：
  首轮所有商品的 known_reviews 都是 0（还没抓过评论），
  于是**全靠 sale_price 这个 tiebreak 决定命运**，而它是**本币**金额：

      COP 均价 1,684,233 │ CLP 318,993 │ MXN 7,922 │ BRL 2,687 │ PEN 1,110

  量纲差三个数量级 ⇒ 排名 = 币值大小排名 ⇒ 前 150 名 = CO 121 + CL 29，
  墨西哥的 1028 个候选**一个都进不去**。

  ★ 而且它会自锁：没被抓过 ⇒ known_reviews 恒为 0 ⇒ 永远靠本币金额比 ⇒
    永远轮不到 ⇒ 下一轮还是 0。低币值国家**永远**拿不到评论。

  本项目其他地方（price_audit / strategy）早立了"跨币种不混算"的规矩，
  这条查询是唯一的漏网之鱼。修法是分区轮转，不是换算成 USD ——
  `sale_price_usd` 全库 14955 行**全是 NULL** 且无人消费，换算兜不住。
"""
import inspect
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_voctgt_"))
config.DB_PATH = _TMP / "t.db"

from app import db  # noqa: E402

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

# ---------------------------------------------------------------- 造数据
# 真实的本币量纲（取自事故当天 run 28 的实测均价），候选数量也照抄：
# 墨西哥候选最多但币值最小 —— 这正是原写法会把它整个饿死的组合。
MARKETS = [
    ("CO", "COP", 1_684_233, 594),
    ("CL", "CLP",   318_993, 658),
    ("MX", "MXN",     7_922, 1028),
    ("BR", "BRL",     2_687, 204),
    ("PE", "PEN",     1_110, 346),
]
RUN_ID = 1

with db.tx() as c:
    # price_obs.run_id 有外键指向 scrape_run，先把这一轮建出来
    c.execute("INSERT OR IGNORE INTO scrape_run(id,started_at,run_date,mode,status) "
              "VALUES(?,datetime('now'),date('now'),'test','ok')", (RUN_ID,))
    # ★ 渠道 id 用序号不用 hash()：Python 的字符串 hash 每个进程都不同
    #   （PYTHONHASHSEED 随机化），拿它当主键会让测试时灵时不灵。
    for idx, (cc, cur, base, n) in enumerate(MARKETS, start=100):
        c.execute("INSERT OR IGNORE INTO country(code,name_zh,currency) VALUES(?,?,?)",
                  (cc, cc, cur))
        # ★ 这里**不能**用 INSERT OR IGNORE：channel 的 code/kind 是 NOT NULL，
        #   而 OR IGNORE 会把 NOT NULL 违规也一起吞掉 —— 渠道压根没建出来，
        #   报错却推迟到后面 price_obs 的外键上，看起来像是别的问题。
        c.execute("INSERT INTO channel(id,code,name,country_code,kind,base_url,enabled) "
                  "VALUES(?,?,?,?,'retailer',?,1)",
                  (idx, f"ch{cc}", f"ch-{cc}", cc, f"https://x.{cc.lower()}/"))
        chan = idx
        for i in range(n):
            # 价格在本国量纲内上下浮动，跨国不可比 —— 这就是现实
            price = base * (1 + (i % 20) / 10.0)
            url = f"https://x.{cc.lower()}/p/{i}"
            c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,title,
                                sale_price,currency,url,audit_status,run_id,row_hash)
                         VALUES(date('now'),?,?,?,?,?,?,'ok',?,?)""",
                      (cc, chan, f"{cc} 机型 {i}", price, cur, url, RUN_ID,
                       f"{cc}-{i}-{RUN_ID}"))

pool = Counter(r["country_code"] for r in db.q(
    "SELECT DISTINCT url, country_code FROM price_obs WHERE run_id=?", (RUN_ID,)))
check("候选池已按真实比例造好", dict(sorted(pool.items())),
      {"BR": 204, "CL": 658, "CO": 594, "MX": 1028, "PE": 346})

# ---------------------------------------------------------------- 取真实 SQL
# ★ 直接从 orchestrator 源码里抠出那条 SQL 来跑。
#   不复制一份到测试里 —— 复制的那份不会跟着代码改，
#   改坏了测试照样绿，等于没测。
from app.agents.orchestrator import Orchestrator  # noqa: E402

_src = inspect.getsource(Orchestrator._collect_voc)
check_true("能从 orchestrator 抠出目标选择 SQL", 'targets = db.q("""' in _src)
SQL = _src.split('targets = db.q("""')[1].split('""",')[0]


def pick(limit):
    return Counter(r["country_code"] for r in db.q(SQL, (RUN_ID, limit)))


print("== ★ 每个国家都必须分到名额（核心断言）==")
for limit in (40, 150, 300):
    got = pick(limit)
    check(f"LIMIT {limit}：五国全都有份", sorted(got), ["BR", "CL", "CO", "MX", "PE"])
    check(f"LIMIT {limit}：总数对得上", sum(got.values()), limit)
    # 轮转分配 ⇒ 各国名额应当基本相等（差距不超过 1）
    check_true(f"LIMIT {limit}：名额均衡", max(got.values()) - min(got.values()) <= 1,
               str(dict(sorted(got.items()))))

print("== ★ 墨西哥不能因为比索面值小就被饿死 ==")
# 这是事故的原始症状：MX 候选最多(1028)、币值最小(7922)，原写法给它 0 个
got150 = pick(150)
check_true("墨西哥拿到名额", got150.get("MX", 0) > 0, str(dict(sorted(got150.items()))))
check_true("秘鲁拿到名额", got150.get("PE", 0) > 0)
check_true("巴西拿到名额", got150.get("BR", 0) > 0)
check_true("★哥伦比亚不再垄断（原来 121/150）", got150.get("CO", 0) < 60, str(got150.get("CO")))

print("== ★ 反例：旧写法在同一份数据上确实会饿死三个国家 ==")
# 把旧 SQL 原样跑一遍，证明这个测试真的能抓住那个 bug
# （如果反例也通过，说明测试数据没复现问题，断言就是摆设）
OLD_SQL = """
    SELECT DISTINCT po.url, po.country_code, po.sale_price
    FROM price_obs po
    LEFT JOIN review_profile rpf ON rpf.product_url = po.url
    WHERE po.run_id = ? AND po.url IS NOT NULL AND po.url <> ''
      AND po.audit_status <> 'rejected'
    ORDER BY COALESCE(rpf.total_reviews,0) DESC, po.sale_price DESC
    LIMIT ?
"""
old = Counter(r["country_code"] for r in db.q(OLD_SQL, (RUN_ID, 150)))
check_true("★旧写法确实只喂饱高币值国家", set(old) <= {"CO", "CL"}, str(dict(old)))
check("★旧写法给墨西哥 0 个", old.get("MX", 0), 0)

print("== 分区内部按本币排是对的（同国同币，量纲一致）==")
mx = db.q("""SELECT url, sale_price FROM price_obs
             WHERE run_id=? AND country_code='MX' ORDER BY sale_price DESC LIMIT 3""",
          (RUN_ID,))
check_true("同国内部高价在前", mx[0]["sale_price"] >= mx[-1]["sale_price"])

print("== 已抓过的商品在冷却期内不重复抓 ==")
target = db.q1("SELECT url, country_code FROM price_obs WHERE country_code='MX' LIMIT 1")
with db.tx() as c:
    c.execute("""INSERT INTO review_profile(product_url,country_code,total_reviews,
                        fetched_reviews,last_fetched)
                 VALUES(?,?,999,999,datetime('now'))""",
              (target["url"], target["country_code"]))
picked = {r["url"] for r in db.q(SQL, (RUN_ID, 300))}
check_true("★刚抓过的不再进入目标", target["url"] not in picked)

# 冷却期外的应当重新进入，且因 known_reviews=999 排到本国最前
with db.tx() as c:
    c.execute("UPDATE review_profile SET last_fetched=datetime('now','-30 day') "
              "WHERE product_url=?", (target["url"],))
rows = db.q(SQL, (RUN_ID, 300))
picked = {r["url"] for r in rows}
check_true("★冷却期过后重新进入目标", target["url"] in picked)
mx_rows = [r for r in rows if r["country_code"] == "MX"]
check_true("★已知评论量高的排本国第一", mx_rows and mx_rows[0]["url"] == target["url"],
           str(mx_rows[0]["url"]) if mx_rows else "无 MX 行")

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
