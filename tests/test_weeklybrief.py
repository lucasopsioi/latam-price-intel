# -*- coding: utf-8 -*-
"""周期口径、选材打分与导出的回归测试。

周期口径演变：
  · 2026-08-18 用户定 5 日/20 日双段制（周报 + 双周报）
  · 2026-08-27 用户改为**每周一次**（周一起算的自然周），双周报废除
其余要求不变：有图、有文字、含所有国家、产业可选、有价格预警/变化表、
「一定只选最重点的」、要能输出 Word / PPT / PDF。
"""
import os
import pathlib
import sys
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app.agents import weekly as W          # noqa: E402
from app import report_export as RX         # noqa: E402

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ─────────── 1. 周期切分：铺满、不重、不漏 ───────────
d, segs, bad = date(2026, 1, 1), [], []
while d < date(2027, 1, 10):
    kind, s, e_excl, e_inc = W._period_bounds(d.isoformat())
    if not (s <= d.isoformat() < e_excl):
        bad.append((d.isoformat(), kind, s, e_excl))
    segs.append((kind, s, e_excl))
    d += timedelta(days=1)
ok(not bad, f"★ 每一天都必须唯一归属于一期，实测 {len(bad)} 天不落在自己的区间内：{bad[:2]}")

uniq = sorted(set(segs), key=lambda x: x[1])
gaps = [(a[2], b[1]) for a, b in zip(uniq, uniq[1:]) if a[2] != b[1]]
ok(not gaps, f"★ 相邻两期必须首尾相接（右端开区间），实测断裂：{gaps[:2]}")
ok(len(uniq) >= 52, f"一年应切出 52+ 周，实得 {len(uniq)}")
ok(all(k == "weekly" for k, _, _ in uniq),
   "★ 2026-08-27 起只有周报，双周报废除")
ok(all((date.fromisoformat(e) - date.fromisoformat(s)).days == 7
       for _, s, e in uniq), "每期必须正好 7 天")
ok(all(date.fromisoformat(s).weekday() == 0 for _, s, _ in uniq),
   "每期都从周一开始（自然周）")

k, s, _, e = W._period_bounds("2026-08-27")     # 周四
ok((k, s, e) == ("weekly", "2026-08-24", "2026-08-30"),
   f"8/27（周四）应属于「周报 8-24~8-30」，实得 {(k, s, e)}")
k2, s2, _, _ = W._period_bounds("2026-08-31")   # 下周一
ok((k2, s2) == ("weekly", "2026-08-31"),
   f"★ 周一开启新的一周（右端开区间），实得 {(k2, s2)}")


# ─────────── 1b. 周报要自动出：每周一生成上周报告并送手机 ───────────
_sched = (ROOT / "app/scheduler.py").read_text(encoding="utf-8")
ok("_job_weekly_report" in _sched and 'day_of_week' not in _sched,
   "★ 报告每天凌晨刷新（2026-08-28 用户改，取代每周一）—— 不许再按周几触发")
ok("timedelta(days=1)" in _sched,
   "★ 锚点是**昨天**：周一凌晨出上一完整周，其余日子出本周滚动版；"
   "周期吸附保证整周只覆盖更新同一行")
ok("misfire_grace_time=43200" in _sched,
   "周一早上电脑没开要补跑（12 小时宽限），不许静默跳过一整周")
ok("phone_sync.kick_async" in _sched and "EXPORT_DIR / name" in _sched,
   "★ 自动生成后要导出 PDF+PPT 落 exports/ 并触发手机同步 —— 闭环到手机")
ok("def generate_weekly" in _sched and "LLMClient" in _sched,
   "★ 报告必须由软件自己调 MiniMax 生成（LLMClient 从库里取密钥），"
   "不是靠人在命令行里跑（2026-08-27 用户确认）")
_api = (ROOT / "app/api/server.py").read_text(encoding="utf-8")
ok("_sched.generate_weekly" in _api,
   "★ 界面按钮与周一定时任务必须走同一条链路 —— 否则手点出的报告"
   "和自动出的报告会长得不一样")
ok('"pdf", "docx", "pptx"' in _sched,
   "自动链路要一次出齐 PDF/Word/PPT")
ok('"llm": llm.available()' in _sched,
   "★ 返回值要如实标出模型是否真的可用 —— 退化成纯事实文案时"
   "不能假装是模型写的")

# ─── 供应商分工（2026-08-28 用户：报告用 DeepSeek，搜索/研判用 MiniMax）───
ok("report_llm" in _sched,
   "★ 出报告必须走 report_llm（写作型供应商），不再直接 LLMClient")
_llm = (ROOT / "app/agents/llm.py").read_text(encoding="utf-8")
ok('"deepseek"' in _llm and "chat/completions" in _llm,
   "★ LLMClient 要认 deepseek 供应商（OpenAI 风格 /chat/completions）")
ok("def report_llm" in _llm and "回落" in _llm,
   "★ DeepSeek Key 未配时要**诚实回落** MiniMax 并写日志，"
   "绝不静默换供应商")
ok("deepseek_api_key" in _llm,
   "DeepSeek Key 从库里取（设置页填，掩码回显），与 MiniMax 同一套纪律")
from app.agents.llm import LLMClient as _LC, PROVIDERS as _PV
ok("deepseek" in _PV and "minimax" in _PV, "供应商注册表要有两家")
_d = _LC({}, provider="deepseek")
ok(_d.base_url.startswith("https://api.deepseek.com") and _d._path == "/chat/completions",
   f"deepseek 端点应为官方地址+/chat/completions，实得 {_d.base_url}{_d._path}")
_m = _LC({})
ok(_m.provider == "minimax" and _m._path == "/text/chatcompletion_v2",
   "★ 不传 provider 时行为必须与从前完全一致 —— 搜索/研判 Agent 不受影响")
_api2 = (ROOT / "app/api/server.py").read_text(encoding="utf-8")
ok("deepseek_api_key" in _api2 and '"DeepSeek"' in _api2,
   "设置页要有 DeepSeek Key 字段与连通测试")

# ─────────── 1c. 历史周报：所有回看窗口相对**报告期末**而非今天 ───────────
# 用户 2026-08-28：「不管今天的数据，先看历史数据输出周报，比如上一周的」。
# 出历史周报时若窗口仍相对今天，会把周期之后发生的事混进来 ——
# 读者以为在看上周，实际混着这周，而且没有任何提示。
_wk = (ROOT / "app/agents/weekly.py").read_text(encoding="utf-8")
ok("_asof_date" in _wk and "self._asof = eff_end" in _wk,
   "★ 必须有 as-of 基准（报告期末），并在 run() 里钉死")
ok(_wk.count("'now'") <= 1,
   f"★ 除报告自身的创建时间戳外，不许再有相对今天的窗口，实测 "
   f"{_wk.count(chr(39) + 'now' + chr(39))} 处")
ok("BETWEEN date(?,'-30 day') AND ?" in _wk,
   "★ 回看窗口要有**上界** —— 只改下界的话未来数据照样泄进来")
ok("_dt.date.fromisoformat(self._asof_date)" in _wk,
   "★ 曲线的 LOCF 补点也要补到报告期末 —— 实测 8/17~8/23 的报告 "
   "x 轴一路画到 8/28")

# ─────────── 2. 选材打分：重要度不是幅度 ───────────
base = {"cat": "phone", "change_pct": -10}
plain = W._score_move(base, set())
official = W._score_move({**base, "is_official": 1}, set())
hits = W._score_move({**base, "is_official": 1, "rival_product_id": 7}, {7})
ok(hits > official > plain,
   f"★ 打到我方对位 > 官方渠道 > 普通，实得 {hits} / {official} / {plain}")
ok(W._score_move({"cat": "phone", "change_pct": -2}, set()) == 0,
   "低于噪声阈值的不进榜")

# ★ 涨跌不对称：实测近 15 天降价 215 条最大 54.1%，
#   涨价 27 条里 5 条 >40% 且 P90 高达 140.9% —— 那条尾巴全是脏数据
#   （89→299 这种，低价侧挂牌解析错）。清仓可以真降 50%，没人 15 天涨 50%。
ok(W._HARD_RISE < W._HARD_DROP,
   f"★ 涨价的可信上限必须比降价严，实得 涨 {W._HARD_RISE} / 降 {W._HARD_DROP}")
ok(W._score_move({"cat": "audio", "change_pct": 236, "is_official": 1}, set()) == 0,
   "★ +236% 必须直接不进榜 —— 只打折不够，它打完折仍能压过真实的 20% 降价")
ok(W._score_move({"cat": "audio", "change_pct": 123.6}, set()) == 0,
   "+123.6% 同样是脏数据")
ok(W._score_move({"cat": "phone", "change_pct": -54.1, "is_official": 1}, set()) > 0,
   "★ -54.1% 是真实清仓（实测降价最大值），不能误杀")


# ─────────── 3. 导出：三种格式 + 中文 ───────────
MD = """# 测试报告

> 周报 · 2026-08-05 ~ 2026-08-19 · 5 国

本期重点关注平板降价。

## ⚠ 价格预警
| 机型 | 国家 | 变动 |
|---|---|---|
| Honor Pad 10 | MX | -36.4% |

## 变动幅度
![chart:t1]

---
口径说明。
"""
CHARTS = [{"question": "deviation", "el": "t1", "title": "变动",
           "xlab": "变动幅度", "opt": {"rows": [
               {"label": "Honor Pad 10 · MX", "v": -36.4},
               {"label": "Redmi Pad 2 · CL", "v": -34.8}]}}]

blocks = RX.parse_blocks(MD)
kinds = [b["t"] for b in blocks]
for want in ("h", "quote", "p", "table", "chart", "hr"):
    ok(want in kinds, f"解析器应认得 {want} 块，实得 {kinds}")
tbl = next(b for b in blocks if b["t"] == "table")
ok(tbl["head"] == ["机型", "国家", "变动"], f"表头解析错：{tbl['head']}")
ok(len(tbl["rows"]) == 1 and tbl["rows"][0][0] == "Honor Pad 10",
   f"★ |---| 分隔线必须被丢掉，不能当成数据行：{tbl['rows']}")

ok(RX._FONT is not None,
   "★ 必须找到中文字体 —— matplotlib 默认字体没有中文字形，"
   "不指定会得到满屏方块，而且**不报错**")

imgs = RX.render_charts(CHARTS)
ok("t1" in imgs and len(imgs["t1"]) > 2000, "deviation 图应能渲染出 PNG")

for fmt in ("docx", "pptx", "pdf"):
    try:
        data, name = RX.export(fmt, "测试报告", "2026-08-05 ~ 2026-08-19", MD, CHARTS)
        ok(len(data) > 5000, f"{fmt} 产物过小（{len(data)} 字节），可能是空文件")
        ok(name.endswith("." + fmt), f"{fmt} 文件名后缀错：{name}")
    except Exception as e:                              # noqa: BLE001
        ok(False, f"{fmt} 导出抛异常：{type(e).__name__}: {e}")

try:
    RX.export("xlsx", "t", "s", MD, [])
    ok(False, "不支持的格式应当报错而不是产出空文件")
except ValueError:
    PASS[0] += 1


# ─────────── 4. 跨币种：报告的图只能画百分比 ───────────
import inspect  # noqa: E402

src = inspect.getsource(W.WeeklyReportAgent._brief_charts)
ok('"question": "deviation"' in src,
   "★ 报告覆盖六国六币种，主图必须画**变动幅度**而不是绝对价 —— "
   "实测绝对价同轴时哥伦比亚的 210 万把其余四国压成贴底的点，"
   "轴标签写「跨国不可比」只是承认问题不是解决问题")
ok("sorted(" in src, "图内应按幅度排序，否则大条夹在小条中间很难读")


print(f"weeklybrief: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
