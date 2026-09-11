# -*- coding: utf-8 -*-
"""tools/audit_backlog.py 的干跑 / 闸门 / apply / 回滚往返 —— 在**临时库**上跑，绝不碰 intel.db。

★ 为什么要单独测：回滚清单是真跑唯一的后悔药，没被执行过的回滚路径等于没有；
  闸门只测"坏的要拦住"会得到一个把一切都拦住的闸门，正向用例（该放行时放行）也要有。
★ 必须在 db 建立第一个连接之前改掉 config.DB_PATH，否则会安静地在生产库上跑。

跑法： python tests\\test_audit_backlog.py
"""
import importlib
import json
import shutil
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="intel_backlog_"))
config.DB_PATH = _TMP / "t.db"

from app import db  # noqa: E402

ab = importlib.import_module("tools.audit_backlog")
ab.LOG_DIR = _TMP / "logs"
ab._collecting = lambda: False          # 测试机不一定有服务；闸门的服务侧单独打桩

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
    c.execute("INSERT INTO channel(id,code,name,country_code,kind,base_url,enabled,default_seller_type) "
              "VALUES(900,'t','店','CL','retailer','https://a/',1,'official')")
    c.execute("INSERT INTO channel(id,code,name,country_code,kind,base_url,enabled,default_seller_type) "
              "VALUES(901,'m','市','CL','marketplace','https://b/',1,'third_party')")
    c.execute("INSERT INTO brand(id,name,is_ours,enabled) VALUES(900,'TestBrand',0,1)")

_seq = [0]


def add(day, price, title, *, status="pending", seller="official", ch=900, model=None, rom=None,
        cat="tablet", reason=None, n=1):
    ids = []
    with db.tx() as c:
        for _ in range(n):
            _seq[0] += 1
            cur = c.execute("""INSERT INTO price_obs(obs_date,country_code,channel_id,brand_id,
                                  category_code,title,model_guess,rom_gb,sale_price,currency,url,
                                  audit_status,audit_reason,run_id,row_hash,product_kind,condition,
                                  is_bundle,is_in_stock,seller_type,seller_kind)
                               VALUES(?,'CL',?,900,?,?,?,?,?,'CLP',?,?,?,1,?,'device','new',0,1,?,?)""",
                            (day, ch, cat, title, model or title[:30], rom, price, f"https://x/{_seq[0]}",
                             status, reason, f"h{_seq[0]}", seller,
                             "third_party" if seller == "third_party" else "self_operated"))
            ids.append(cur.lastrowid)
    return ids


OLD, TODAY = "2020-01-10", db.today()
# 地板样本：40 条 accepted 平板围绕 284,990
for i in range(40):
    add(OLD, 284_990 * (0.7 + 0.6 * i / 39), "Tablet Lenovo Tab M10 128GB", status="accepted")
# 待审：3 条正常、2 条钢化膜（地板剔）、2 条灰区（第三方 0.25× 型号基线）、1 条缺货口径
add(OLD, 100_000, "Celular Test Phone X 128GB", model="Test Phone X", rom=128, cat="phone", status="accepted", n=3)
normal = add(OLD, 250_000, "Tablet Lenovo Tab M10 128GB", reason="卖家名「Falabella」= 该渠道自营主体", n=3)
lamina = add(OLD, 8_500, "GENÉRICO LÁMINA MICA VIDRIO TEMPLADO TABLET LENOVO TAB P11", n=2)
gray = add(OLD, 25_000, "Celular Test Phone X 128GB", model="Test Phone X", rom=128, cat="phone",
           seller="third_party", ch=901, n=2)
today_rows = add(TODAY, 250_000, "Tablet Lenovo Tab M10 128GB", n=2)
# ★ 穿戴：地板 209,990 × 0.08 = 16,799。同一个桶里一条白牌真表（要放行）+ 一条表带（要剔），
#   用来守住条件地板在**工具链**上也生效（干跑清单要报"守卫放行"，真跑不许把真表剔掉）。
for i in range(40):
    add(OLD, 209_990 * (0.7 + 0.6 * i / 39), "Smartwatch Samsung Galaxy Watch7 44mm",
        status="accepted", cat="wearable")
watch = add(OLD, 12_990, "GOSHGG SMARTWATCH SERIE 6", cat="wearable")
strap = add(OLD, 9_990, "GENÉRICO CORREA RELOJ REPUESTO SILICONA ACME HONOR BAND 8",
            cat="wearable")
PENDING_OLD = len(normal) + len(lamina) + len(gray) + len(watch) + len(strap)


def status_map():
    return {r["id"]: (r["audit_status"], r["audit_reason"], r["audit_by"])
            for r in db.q("SELECT id, audit_status, audit_reason, audit_by FROM price_obs")}


BEFORE = status_map()

# ---------------------------------------------------------------- 干跑
print("== 干跑：只读连接、封 tx、不改库 ==")
saved = (db.q, db.q1, db.tx)
ab.ReadOnlyDB(config.DB_PATH).install()
try:
    with db.tx():
        pass
    tx_blocked = False
except RuntimeError:
    tx_blocked = True
check_true("干跑模式下 db.tx 会炸（任何写库企图当场暴露）", tx_blocked)
dates = ab.pending_dates(False, None)
check("默认排除今天", [d for d, _ in dates], [OLD])
check("pending 计数", dates[0][1], PENDING_OLD)
check("--include-today 才含今天", [d for d, _ in ab.pending_dates(True, None)], [OLD, TODAY])
plan = ab.rehearse(ab.PriceAuditAgent(None, {}), dates, batch=3)
rec = plan["dates"][0]
check("预演：accepted（3 条正常 + 1 条被守卫放行的白牌真表）", rec["accepted"], 4)
check("预演：地板剔除 3（2 钢化膜 + 1 表带）", rec["floor"], 3)
check("预演：rejected 含地板", rec["rejected"], 3)
check("预演：灰区 2", rec["gray"], 2)
check("预演：轮数 = ceil(9/3)", rec["rounds"], 3)
check("预演：token 估算 = gray×135", rec["tokens_est"], 2 * ab.TOK_PER_GRAY)
check_true("预演给出地板中位数表", ("CL", "tablet") in plan["floors"]["2020-01"])
check("★预演单列「守卫放行」（低于地板但带整机证据）", rec["veto"], 1)
check_true("守卫放行也留了样例（成批放行必须看得见）", bool(plan["samples"]["veto"]))
check("守卫放行的是那块白牌真表",
      [s[3] for s in plan["samples"]["veto"]], ["GOSHGG SMARTWATCH SERIE 6"])
ab.print_plan(plan, 3, no_llm=True)
db.q, db.q1, db.tx = saved
check("干跑没改库", status_map(), BEFORE)

# ---------------------------------------------------------------- 参数默认值
print("== ★ --threshold 默认必须是 0 ==")
# 500 的语义是"剩这么少就别跑了"，但它是在每个观测日开始前用**全库** pending 判的 ——
# 于是所有 ≤499 条 pending 的旧观测日永远轮不到，而这些行在 = 'accepted' 口径下等同不存在。
check("默认 threshold = 0（审到一条不剩）", ab.build_parser().parse_args([]).threshold, 0)
check("显式传值仍然生效", ab.build_parser().parse_args(["--threshold", "500"]).threshold, 500)

# ---------------------------------------------------------------- 闸门
print("== 闸门：双保险，且该放行时放行 ==")
busy, why = ab.collecting_now()
check("无采集时放行", busy, False)
with db.tx() as c:
    c.execute("INSERT INTO scrape_run(id,started_at,run_date,mode,status) "
              "VALUES(2,datetime('now'),date('now'),'daily','running')")
busy, why = ab.collecting_now()
check("scrape_run running → 拦", busy, True)
check_true("拦的理由说得出来", "scrape_run #2" in why, why)
with db.tx() as c:
    c.execute("UPDATE scrape_run SET status='ok' WHERE id=2")
ab._collecting = lambda: True
check("/api/health task_running → 拦", ab.collecting_now()[0], True)
ab._collecting = lambda: False
check("解除后放行", ab.collecting_now()[0], False)
check("main --apply 在采集中 exit 2", (lambda: (setattr(ab, "_collecting", lambda: True),
                                                ab.main(["--apply", "--no-llm"]))[1])(), 2)
ab._collecting = lambda: False

# ---------------------------------------------------------------- apply（--no-llm）
print("== apply --no-llm：规则层全量、灰区留 pending、每轮留痕、回滚清单先落盘 ==")
args = Namespace(no_llm=True, include_today=False, dates=None, threshold=0, batch=3, force=True)
runs0 = db.q1("SELECT COUNT(*) n FROM agent_run WHERE agent_name='price_audit'")["n"]
rc = ab.apply_backlog(args)
check("退出码 0", rc, 0)
after = status_map()
check("正常行 accepted", {after[i][0] for i in normal}, {"accepted"})
check("钢化膜 rejected", {after[i][0] for i in lamina}, {"rejected"})
check_true("钢化膜理由是地板", all(after[i][1].startswith(ab.FLOOR_REASON_PREFIX) for i in lamina))
check("★表带 rejected（同一个穿戴桶里的配件照剔）", {after[i][0] for i in strap}, {"rejected"})
check_true("表带理由是地板", all(after[i][1].startswith(ab.FLOOR_REASON_PREFIX) for i in strap))
check("★白牌真表 accepted（条件地板放行，真跑也不许杀）", {after[i][0] for i in watch}, {"accepted"})
check_true("真表理由不是地板",
           not any((after[i][1] or "").startswith(ab.FLOOR_REASON_PREFIX) for i in watch))
check("灰区留 pending（--no-llm）", {after[i][0] for i in gray}, {"pending"})
check("今天的行没被碰", {after[i][0] for i in today_rows}, {"pending"})
runs1 = db.q1("SELECT COUNT(*) n FROM agent_run WHERE agent_name='price_audit'")["n"]
# 9 行 / 批 3：3 个满轮 + 1 轮空转 —— 行数整除批大小时，游标要再取一次才知道已经取尽
# （run() 取到 0 行也会留一条 agent_run，这是"跑了但没东西"与"没跑"的区别）
check("每轮一条 agent_run（3 满轮 + 1 轮空转）", runs1 - runs0, 4)
logs = sorted(ab.LOG_DIR.glob("audit_backlog_*.jsonl"))
rb = [p for p in logs if p.name.endswith("_rollback.jsonl")]
jl = [p for p in logs if not p.name.endswith("_rollback.jsonl")]
check("落了 1 份日志 + 1 份回滚清单", (len(jl), len(rb)), (1, 1))
rounds = [json.loads(l) for l in jl[0].read_text(encoding="utf-8").splitlines() if '"round"' in l]
check("JSONL 每轮一行", len(rounds), 3)
check_true("每行带 agent_run_id", all(r["agent_run_id"] for r in rounds))
rb_recs = [json.loads(l) for l in rb[0].read_text(encoding="utf-8").splitlines()]
check("回滚清单覆盖该日全部 pending（含灰区）", sorted(r["id"] for r in rb_recs),
      sorted(normal + lamina + gray + watch + strap))
check("★apply 的清单 w 为空（判决在落盘之后才算出来，清单必须先落盘）",
      {r["w"] for r in rb_recs}, {None})
check("回滚清单存的是改前值（pending + 采集侧写的卖家理由）",
      {(r["s"], r["r"]) for r in rb_recs if r["id"] in normal},
      {("pending", "卖家名「Falabella」= 该渠道自营主体")})

print("== ★ 第二遍 apply（--no-llm）：in = 灰区数、acc = rej = 0，**不是**「取到 0 行」==")
# ★ 验收条款②改口径（2026-09-05）：--no-llm 下灰区是**故意留 pending** 的，
#   第二遍必然把它们重新取出来、重新判成灰区、再留下 —— in 不可能是 0。
#   写成"处理 0 行"会让人把正常状态当异常，或者反过来把"真漏审"当成"那是灰区"。
#   判据是"没有新定案"（acc+rej=0），不是"没有取到行"。
runs2 = db.q1("SELECT COUNT(*) n FROM agent_run WHERE agent_name='price_audit'")["n"]
rc = ab.apply_backlog(args)
check("退出码 0", rc, 0)
runs3 = db.q1("SELECT COUNT(*) n FROM agent_run WHERE agent_name='price_audit'")["n"]
check("第二遍跑了 1 轮（把灰区重新取了一遍）", runs3 - runs2, 1)
check("★第二遍 in = 灰区数", ab.LAST_SUMMARY["total"], len(gray))
check("★第二遍 acc = 0（没有新定案）", ab.LAST_SUMMARY["accepted"], 0)
check("★第二遍 rej = 0（没有新定案）", ab.LAST_SUMMARY["rejected"], 0)
check("★第二遍灰区仍是灰区", ab.LAST_SUMMARY["gray_pending"], len(gray))
check("库状态不变", status_map(), after)

print("== 阈值：今天以前 pending < 阈值即停 ==")
args_t = Namespace(no_llm=True, include_today=False, dates=None, threshold=999, batch=3, force=True)
runs4 = db.q1("SELECT COUNT(*) n FROM agent_run WHERE agent_name='price_audit'")["n"]
ab.apply_backlog(args_t)
check("阈值触发时一轮都不跑", db.q1("SELECT COUNT(*) n FROM agent_run WHERE agent_name='price_audit'")["n"] - runs4, 0)

# ---------------------------------------------------------------- 回滚
print("== 回滚：按清单恢复到改前 ==")
ab.rollback(rb[0])
restored = status_map()
_touched = normal + lamina + gray + watch + strap
check("回滚后与 apply 前完全一致（status/reason/by 三列）",
      {i: restored[i] for i in _touched}, {i: BEFORE[i] for i in _touched})

print("== ★ 回滚守卫：只回滚仍带本次判决的行 ==")
# apply_backlog 的清单没有 w（判决在落盘之后才算出来），守卫退回 audit_status<>改前值。
# ① 幂等：刚回滚完再回滚一次，一行都不该改（此前是无条件 UPDATE，两者看不出区别）
ab.rollback(rb[0])
check("★第二次回滚不改任何行（已回到改前状态就跳过）", status_map(), restored)
# ② 清单里但本次没被动过的行（灰区一直是 pending）也跳过 —— 上面那条已经覆盖，
#    这里单独确认灰区行的 reason/by 没被写成 None 以外的东西
check("灰区行仍是改前值", {i: status_map()[i] for i in gray}, {i: BEFORE[i] for i in gray})

# ---------------------------------------------------------------- 地板复审
print("== --floor-recheck：只允许 accepted→rejected ==")
sneaked = add(OLD, 9_000, "Lámina Hidrogel Lenovo Tab M10", status="accepted")   # 历史上混进 accepted 的配件
args_f = Namespace(dates=None)
rc = ab.floor_recheck(args_f, apply=False)
check("干跑退出 0", rc, 0)
check("干跑不改库", status_map()[sneaked[0]][0], "accepted")
rc = ab.floor_recheck(args_f, apply=True)
check("apply 后改判 rejected", status_map()[sneaked[0]][0], "rejected")
check("真机 accepted 不动（0.7×~1.3× 中位）",
      db.q1("SELECT COUNT(*) n FROM price_obs WHERE obs_date=? AND category_code='tablet' "
            "AND audit_status='accepted'", (OLD,))["n"], 40)
rb2 = sorted(ab.LOG_DIR.glob("*_recheck_rollback.jsonl"))
check("复审也落了回滚清单", len(rb2), 1)
rb2_recs = [json.loads(line) for line in rb2[0].read_text(encoding="utf-8").splitlines()]
check("★复审清单带 w = 本次写入值（回滚守卫靠它）", {r["w"] for r in rb2_recs}, {"rejected"})

print("== ★ w 守卫：本次判决已被别人改掉的行，回滚必须跳过 ==")
with db.tx() as c:                      # 假装另一条线在复审之后把它改回了 accepted
    c.execute("UPDATE price_obs SET audit_status='accepted', audit_reason='别的审计写的', "
              "audit_by='rule:other' WHERE id=?", (sneaked[0],))
ab.rollback(rb2[0])
check("★不覆盖别人后来写的判决（audit_status 已不是本次写入的 rejected）",
      status_map()[sneaked[0]], ("accepted", "别的审计写的", "rule:other"))
with db.tx() as c:                      # 放回本次判决，验证守卫命中时确实会回滚
    c.execute("UPDATE price_obs SET audit_status='rejected', audit_reason='x', "
              "audit_by='rule:price_audit' WHERE id=?", (sneaked[0],))
ab.rollback(rb2[0])
check("复审回滚还原", status_map()[sneaked[0]][0], "accepted")
ab.rollback(rb2[0])
check("★复审回滚同样幂等", status_map()[sneaked[0]][0], "accepted")

print("== --recheck-no-baseline：'第三方且无基线暂留' 的行用现在的基线/地板重审 ==")
# 暂留时无基线；现在 CL 平板有地板 25,999 → 9,500 的"暂留"行该改判
nb = add(OLD, 9_500, "Protector Lenovo Tab M10 genérico", seller="third_party", ch=901,
         status="accepted", reason=ab.NO_BASELINE_REASON + "（rule:no_baseline）")
keep = add(OLD, 260_000, "Tablet Lenovo Tab M10 128GB", seller="third_party", ch=901,
           status="accepted", reason=ab.NO_BASELINE_REASON + "（rule:no_baseline）")
rc = ab.recheck_no_baseline(Namespace(dates=None), apply=False)
check("干跑不改库", (status_map()[nb[0]][0], status_map()[keep[0]][0]), ("accepted", "accepted"))
rc = ab.recheck_no_baseline(Namespace(dates=None), apply=True)
check("低于地板的暂留行 → rejected", status_map()[nb[0]][0], "rejected")
check("正常价的暂留行不动（现在基线 1.0× 合理带）", status_map()[keep[0]][0], "accepted")

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
