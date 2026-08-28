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
ok("_job_weekly_report" in _sched and 'day_of_week="mon"' in _sched,
   "★ 每周一自动生成周报的调度任务必须存在（用户：报告每周一次）")
ok("timedelta(days=7)" in _sched,
   "★ 自动生成的锚点是上一周 —— 周一早上出的是刚结束那周的完整报告")
ok("misfire_grace_time=43200" in _sched,
   "周一早上电脑没开要补跑（12 小时宽限），不许静默跳过一整周")
ok("phone_sync.kick_async" in _sched and "EXPORT_DIR / name" in _sched,
   "★ 自动生成后要导出 PDF+PPT 落 exports/ 并触发手机同步 —— 闭环到手机")

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
