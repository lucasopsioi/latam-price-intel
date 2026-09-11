# -*- coding: utf-8 -*-
"""评论采集量与正负平衡的回归测试。

用户的判断（2026-08-17，原话）：
  「评论量一定要多，现在太少了，好评和差评都要有很多才有统计效应，否则无效」

这条是对的，而且实测发现**正负比 21:1 至少有一部分是采集偏差** ——
160 个采集页的排序控件明写着「Ordenar por: Mejores evaluaciones」，
默认就是好评优先，我们一直只读了那一栏。
"""
import inspect
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUTF8", "1")

from app.scraping import voc  # noqa: E402

FAIL, PASS = [], [0]


def ok(cond, msg):
    if cond:
        PASS[0] += 1
    else:
        FAIL.append(msg)


# ─────────── 1. 评论总数不能把价格/电话当成计数 ───────────
# 实测：449 个商品页的"评论总数"合计 4.67 亿条。两类误匹配：
#   「$ 429.990 Opiniones de este producto 5 /5 2 comentarios」← 价格贴着栏目标题
#   「…al 5552629999 Comentarios y Sugerencias」            ← 页脚客服电话
# 而 VOC 归因 Agent 正是靠这个数判断「评论量异常高说明什么」——
# 喂它一个虚高一万倍的数，它必然把普通商品判成爆款/刷评。
PRICE_TRAP = ("Celular Honor Magic8 Lite 256GB (17) $ 349.990 -19% $ 369.990 "
              "$ 429.990 Opiniones de este producto 5 /5 2 comentarios 1 0 2 0")
ok(voc.parse_review_total(PRICE_TRAP) == 2,
   f"★ 价格贴着栏目标题时应取真正的计数 2，实得 {voc.parse_review_total(PRICE_TRAP)}")

PHONE_TRAP = ("Contáctanos vía Whatsapp o por teléfono al 5552629999 "
              "Comentarios y Sugerencias:atencion@example.com")
ok(voc.parse_review_total(PHONE_TRAP) is None,
   f"★ 页脚客服电话不是评论数，实得 {voc.parse_review_total(PHONE_TRAP)}")

BIG = "Legion 5 $ 9.999.900 opiniones"
ok(voc.parse_review_total(BIG) is None,
   f"★ 千万级的数必然是价格，实得 {voc.parse_review_total(BIG)}")

ok(voc.parse_review_total("20 comentarios") == 20, "正常计数要取到")
ok(voc.parse_review_total("1.234 avaliações") == 1234, "巴西千分位要认")
ok(voc.parse_review_total("") is None, "空文本不该炸")
ok(voc.parse_review_total(None) is None, "None 不该炸")
ok(voc._MAX_REVIEWS <= 50_000,
   f"评论数上限要收紧到可信范围，实得 {voc._MAX_REVIEWS}")


# ─────────── 2. ★ 必须主动抓差评，不能只吃默认排序 ───────────
ok(hasattr(voc.VocCollector, "_sort_worst_first"),
   "★ 要有「切到最差评价排序」的动作 —— 默认排序是「Mejores evaluaciones」"
   "（实测 160 个页面），只抓它等于系统性漏掉差评")

h = inspect.getsource(voc.VocCollector._harvest)
ok("_sort_worst_first" in h, "_harvest 必须跑第二遍差评采集")
# 第二遍必须在第一遍之后（先默认排序、再切最差），否则会漏掉好评
ok(h.index("_sort_worst_first") > h.index("_go_next"),
   "★ 差评那一遍要在默认那一遍之后 —— 反过来会把好评漏掉")
ok("seen" in h, "两遍之间要去重，同一条评论不能入库两次")

s = inspect.getsource(voc.VocCollector._sort_worst_first)
ok("return False" in s,
   "★ 切不动排序要返回 False 而不是抛异常 —— "
   "站点没有这个控件时，默认那一遍的结果仍然有效，不能因此判为失败")
ok(any(w in " ".join(voc.VocCollector._WORST_TEXTS)
       for w in ("peores", "piores")),
   "词表要覆盖西语与葡语的写法")
ok(len(voc.VocCollector._WORST_TEXTS) >= 5,
   "各站文案不同，词表不能只写一种")


# ─────────── 3. 采集量上限要够做统计 ───────────
import yaml  # noqa: E402

cfg = yaml.safe_load((ROOT / "config/runtime.yaml").read_text(encoding="utf-8"))
v = (cfg or {}).get("voc", {})
ok(int(v.get("max_reviews_per_product", 0)) >= 500,
   f"单品评论上限要够（差评常在靠后的页），实得 {v.get('max_reviews_per_product')}")
ok(int(v.get("max_products_per_run", 0)) >= 300,
   f"每轮商品数要够，实得 {v.get('max_products_per_run')}")


# ─────────── 4. 界面文案不许当评论入库 ───────────
# 这条已有实现，这里守住不被回退：排序下拉「Ordenar por: Mejores evaluaciones」
# 本身就长在评论容器里，不滤掉会被当成"消费者说的话"喂给 VOC 分析。
for chrome in ["Ordenar por: Mejores evaluaciones",
               "0 comentarios",
               "Mostrar todas las opiniones"]:
    ok(not voc.looks_like_review(chrome),
       f"★ 界面文案不能当评论：{chrome!r}")
ok(voc.looks_like_review(
    "Excelente producto, la batería dura todo el día y la cámara es muy buena"),
   "真实评论要能通过")




# ─────────── 5. VOC 目标分配：既要发现新渠道也要保住高产渠道 ───────────
# 实测两个连着的坑：
#   ① 只按国家分区 → 组内 `sale_price DESC` 全局排名，而各渠道价格量级不同
#      （MX：Liverpool 最高 17.1 万 / Sanborns 13.1 万 / Sears 8.9 万），
#      前 30 名被前两家占满，**Sears 的 4769 条价格观测一次都没被 VOC 试过**。
#      这与当初"按币值大小排名"是同一个病根：在不可比的组之间做全局排名。
#   ② 加了渠道分区后名额被**平摊**（18 个渠道各 17 个），
#      Falabella 智利从 45 掉到 17 —— 而它是唯一稳定出评论的渠道。
orch = (ROOT / "app/agents/orchestrator.py").read_text(encoding="utf-8")
m = re.search(r'targets = db\.q\("""(.*?)"""', orch, re.S)
ok(m is not None, "找不到 VOC 目标查询")
if m:
    sql = m.group(1)
    ok("PARTITION BY country_code, channel_id" in sql,
       "★ 必须同时按国家与渠道分区，否则价格量级大的渠道会吃掉全部名额")
    ok("yield_rate" in sql or "yr" in sql,
       "★ 要按渠道历史产出加权，否则名额被平摊给 0 产出的渠道")
    ok("AVG(fetched_reviews)" in sql,
       "★ 产出要按**每商品平均评论数**算，不能用命中率 —— "
       "Falabella 智利 6.3 条/商品 vs Alkosto 1.9 条/商品，"
       "按命中率两家都是 1.00，按量差 3 倍")
    ok("rn / MAX" in sql or "rn /" in sql,
       "★ 产出率要**除进排名里**，只当同分 tiebreaker 时 "
       "`ORDER BY rn` 会绝对主导（300÷18 每家 17 个），等于没加")
    # 未试过的渠道必须有机会（否则永远发现不了新渠道）
    ok("1.0" in sql, "未试过的渠道要给乐观初值，保证能被发现")



# ─────────── 6. ★ 评分分布控件不许当评论入库（2026-08-27 用户实测报回） ───────────
# 库里长这样的行有 44 条（Liverpool 墨西哥）：
#   "5 estrellas 91 % 4 estrellas 9 % 3 estrellas 0 % 2 estrellas 0 % 1 estrellas 0 %"
# 老的三道闸为什么全放它过去，见 voc.py 里 _STAR_LABEL 上面的注释。
for widget in [
    "5 estrellas 91 % 4 estrellas 9 % 3 estrellas 0 % 2 estrellas 0 % 1 estrellas 0 %",
    "5 estrellas 100 % 4 estrellas 0 % 3 estrellas 0 % 2 estrellas 0 % 1 estrellas 0 %",
    "5 ★ 96 % 4 ★ 2 % 3 ★ 2 % 2 ★ 0 % 1 ★ 0 %",
]:
    ok(not voc.looks_like_review(widget), f"★ 评分分布控件不能当评论：{widget[:44]!r}")

# 同一批容器里捞上来的其它页面文案（实测入库 275+162 条）
for chrome2 in ["Opiniones del artículo", "Escribir una opinión",
                "keyboard_arrow_up", "1 persona encontró este comentario útil."]:
    ok(not voc.looks_like_review(chrome2), f"★ 页面文案不能当评论：{chrome2!r}")

# ★★ 反向守住：不许为了滤控件而误杀真评论。
#   下面每一条都是**库里真实存在**的评论，任何一条被拦下都比漏放几条控件贵。
REAL_REVIEWS = [
    # 用户点名必须通过的
    "Excelente producto, la batería dura todo el día",
    # ★ id=4253：讲快充的长评，带 3 个百分数（0% / 100% / 95%）。
    #   "正文里出现 >=3 个百分数就判控件"这条规则会误杀它 ——
    #   所以判据必须是"星级标签 + 空白 + 数字%"的**刻度行结构**，
    #   而不是裸数百分号。
    "Compré este modelo recientemente, no pasa ni 30 min y ya está completa "
    "de 0% a 100%, si buscamos cuidar la batería en 95% va genial",
    # ★ id=1290：用户拿 40 个句点填充，字母占比被拖到 0.567，
    #   距 0.55 的闸只剩 0.017 ⇒ letter_ratio 必须先折叠重复标点再算。
    "genial e increible\ngenial el telefono"
    "....................................................\n"
    "Publicado originalmente en Samsung Spain",
    # 单次提到星级的真评论，不能被"星级标签"规则连坐
    "le doy 5 estrellas porque el equipo es excelente y la cámara muy buena",
    "La batería baja 5 % por hora con el brillo alto, pero por lo demás cumple",
]
for real in REAL_REVIEWS:
    ok(voc.looks_like_review(real), f"★ 真评论被误杀：{real[:52]!r}")

# 拒绝理由要能分辨"界面文案"和"启发式误伤面" ——
# 清理历史数据时靠它区分该删的和该留的（按布尔清会误删约 100 条真短评）
ok(voc.review_reject_reason("keyboard_arrow_up") in voc.UI_NOISE_REASONS,
   "★ 图标连字要归到界面噪声（该删）")
ok(voc.review_reject_reason("Hermoso Muy bonito") not in voc.UI_NOISE_REASONS,
   "★ 真实短评不能归到界面噪声 —— 它栽的是长度/自然语言启发式，不是界面文案")
ok(voc.review_reject_reason("Muy buen equipo") not in voc.UI_NOISE_REASONS,
   "★ 'Muy buen equipo' 是真评论，不许被当界面文案删掉")


# ─────────── 7. ★ 正文里的页面抬头要在采集侧剥掉 ───────────
# 站点把「评分 日期 标题 作者」和正文拼在一个文本块里，实测 108 条这样入库。
HEAD_CASES = [
    ("5.0 11 Ago 2026 alfonso f. Buen cambio de tener un iphone 13",
     "Buen cambio de tener un iphone 13"),
    ("2.0 23 Mar 2026 Pretenciosos para lo que ofrecen Anónimo Vinieron defectuosos.",
     "Vinieron defectuosos."),
    ("5.0 20 Nov 2025 El mejor ipad Anónimo Súper rápido, con iOS 26 se siente bien",
     "Súper rápido, con iOS 26 se siente bien"),
]
for raw, want in HEAD_CASES:
    got = voc.strip_page_head(raw)
    ok(got == want, f"★ 抬头没剥干净：{raw[:40]!r} -> {got[:44]!r}（期望 {want[:44]!r}）")

# 只有抬头、没有正文的空壳行：剥完必须短到过不了 _persist 的长度闸。
# 这类行被送进 LLM 会让模型凭空编译文（实测编出"设备有缺陷""赞助内容"）。
for shell in ["5.0 11 Ago 2026 Bernarda B.", "5.0 09 Ago 2026 ESTHEFANNY G.",
              "5.0 11 Ago 2026 MARIA FERNANDA GONZALEZ B."]:
    ok(len(voc.strip_page_head(shell)) < 15,
       f"★ 空壳行剥完应为空：{shell!r} -> {voc.strip_page_head(shell)!r}")

# 没有抬头的正文一个字都不能动
for intact in ["Excelente producto, la batería dura todo el día",
               "Vinieron defectuosos. Dejaron de funcionar a los pocos dias"]:
    ok(voc.strip_page_head(intact) == intact, f"★ 无抬头的正文被改了：{intact[:40]!r}")


# ─────────── 8. ★★ 批量翻译不许错位（库里 19% 已译行栽在这上面） ───────────
# 老写法让模型回填 0~11 的序号再 `chunk[idx]` 写回。方向对（按 id 匹配不是 zip），
# 但 **idx 是模型自己写的**：模型丢条目后会重新编号，于是 idx 说谎而代码照信。
# 留痕里的现行犯 agent_step id=4401：12 条进、11 条回，后面全体错一格 ——
# 差评行拿到 positive、好评行拿到 negative，全程不报错。
# 实测 262 个批次里 67 个（25.6%）返回条数与 prompt 不符。
from app.agents.voc_agent import VocAgent  # noqa: E402

agent_src = (ROOT / "app/agents/voc_agent.py").read_text(encoding="utf-8")
ok("chunk[int(item.get(\"idx\"))]" not in agent_src
   and "chunk[int(item.get('idx'))]" not in agent_src,
   "★ 不许再用模型给的序号直接当下标（chunk[idx]）—— 序号会说谎")
ok('"head"' in agent_src,
   "★ 提示词必须要求回传原文指纹 head，落库前逐条对账")

_chunk = [{"id": 5450, "content": "Muy buen equipo, la cámara excelente",
           "category_code": "phone"},
          {"id": 5455, "content": "ALVARO El celular mejor de lo que espere",
           "category_code": "phone"},
          {"id": 5456, "content": "Katheryn El equipo se reiniciaba constantemente",
           "category_code": "phone"},
          {"id": 5457, "content": "En malas condiciones, llegó sucio y dañado",
           "category_code": "phone"}]
_by_id = {r["id"]: r for r in _chunk}


def _match(item, seen=None):
    r, how = VocAgent._match_review(item, _by_id, _chunk, seen or set())
    return (r["id"] if r else None), how


ok(_match({"id": 5456, "head": "Katheryn El"})[0] == 5456,
   "id 与指纹都对时正常写回")
# 历史真实形态：模型重新编号 ⇒ id 指向邻居。指纹必须揭穿它并找回正主。
ok(_match({"id": 5456, "head": "En malas condiciones"})[0] == 5457,
   "★ 错位一格必须被指纹纠正回 5457（这正是 5455/5456 被写反的成因）")
ok(_match({"id": 2, "head": "ALVARO El celular"})[0] == 5455,
   "★ 模型拿行序号当 id 时，要靠指纹找回正主")
ok(_match({"id": 9999, "head": "Zzz otra cosa totalmente"})[0] is None,
   "★ id 与指纹都对不上必须丢弃 —— 宁可留空重译，不能把 A 的情感写给 B")
ok(_match({"id": 5456, "head": "Katheryn El"}, seen={5456})[0] is None,
   "★ 同一行被返回两次时，第二次不能覆盖")
ok(_match({"id": 5455})[0] == 5455,
   "模型没回 head（老格式）时，id 有效就仍接受，不能整批瘫痪")
ok(_match({"id": "#5456", "head": "Katheryn El"})[0] == 5456,
   "id 带 # 前缀或为字符串时要能归一")

print(f"reviewvolume: {PASS[0]} 通过, {len(FAIL)} 失败")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
