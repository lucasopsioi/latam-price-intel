# -*- coding: utf-8 -*-
"""口碑维度归一 + 评论真伪闸门的回归测试。

跑法：  python tests\test_vocaspects.py

两块内容，都来自实测事故：

★ 维度归一（app/voc_aspects.py）
  提示词只"建议"维度、不约束 ⇒ 814 条评论跑出 **43 种**维度名，
  音质/音效/音量/隔音 各算一个 ⇒ 雷达图上"音质"被拆散、排名失真
  （合并前排第 9，合并后其实是第 5）。

★ 评论真伪（app/scraping/voc.py 的 looks_like_review）
  1727 条入库"评论"里有 **151 条（8.7%）是页面汇总面板**：
      "(0 Opiniones)0.00 / 5.00"
      "Opiniones\\n4.8\\n222 opiniones\\n190\\n23\\n4\\n2\\n2\\n…"
  最糟的是 "0 Opiniones / 还没有评论" —— **零评论的产品被记了一条评论**，
  而评论量正是我们当销量代理指标用的东西。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import voc_aspects  # noqa: E402
from app.agents.voc_agent import _aspect_pairs  # noqa: E402
from app.scraping.voc import looks_like_review  # noqa: E402

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


print("== ★ 同义词必须并到同一个维度（事故的核心）==")
for group, code in [
    (["音质", "音效", "音量", "隔音", "声音", "sonido", "som"], "sound"),
    (["做工", "质量", "品质", "材质", "calidad", "qualidade"], "build"),
    (["外观", "设计", "颜色", "diseno"], "design"),
    (["存储", "内存", "容量", "ram"], "storage"),
    (["电池", "续航", "电量", "bateria"], "battery"),
]:
    for w in group:
        check(f"{w} → {code}", voc_aspects.normalize(w), code)

print("== 大小写/空格/口音不影响归一 ==")
check("大写", voc_aspects.normalize("BATERIA"), "battery")
check("带空格", voc_aspects.normalize("  音 质  "), "sound")
check("已经是 code 的原样返回", voc_aspects.normalize("battery"), "battery")

print("== ★ 说了等于没说的词要丢掉，不能硬塞进某个维度 ==")
for w in ["产品", "体验", "整体", "其他", "general", "todo"]:
    check(f"{w} → 丢弃", voc_aspects.normalize(w), None)
check("表外的词丢弃", voc_aspects.normalize("玄学加成"), None)
check("空值", voc_aspects.normalize(""), None)

print("== ★ 产品维度与体验维度必须分开 ==")
# 价格/物流/售后是渠道的事。混进产品雷达图会得出"这款手机弱在物流"
for c in ("price", "logistics", "service", "packaging", "authenticity"):
    check(f"{c} 属于体验", voc_aspects.ASPECT_KIND[c], "experience")
for c in ("battery", "camera", "screen", "sound", "heat"):
    check(f"{c} 属于产品", voc_aspects.ASPECT_KIND[c], "product")

print("== 品类菜单：只用来引导，不当入库闸门 ==")
check_true("耳机菜单含降噪", "noise_cancel" in voc_aspects.for_category("audio"))
check_true("耳机菜单不含键盘", "keyboard" not in voc_aspects.for_category("audio"))
check_true("手机菜单含相机", "camera" in voc_aspects.for_category("phone"))
check_true("品类未知给全部", len(voc_aspects.for_category(None))
           == len(voc_aspects.ASPECT_CODES))
# ★ 反例：第一版拿 cats 当闸门，当场吃掉 19 条真信号
check_true("★耳机的「系统」不能被吃掉（App 崩溃是真短板）",
           _aspect_pairs([{"code": "系统", "s": "negative"}], "audio")
           == [("software", "negative")])
check_true("★笔记本的「相机」不能被吃掉（笔记本有摄像头）",
           _aspect_pairs([{"code": "相机", "s": "positive"}], "pc")
           == [("camera", "positive")])

print("== 逐维度情感：不能照抄整条 ==")
pairs = _aspect_pairs([{"code": "camera", "s": "positive"},
                       {"code": "battery", "s": "negative"}], "phone")
check("一条评论可以夸相机同时骂电池", dict(pairs),
      {"camera": "positive", "battery": "negative"})
check("老格式（只有维度名）情感留空不猜",
      _aspect_pairs(["电池"], "phone"), [("battery", None)])
check("非法情感值归成未判",
      _aspect_pairs([{"code": "battery", "s": "很棒"}], "phone"), [("battery", None)])
check("重复维度只留一次",
      _aspect_pairs([{"code": "音质", "s": "positive"},
                     {"code": "音效", "s": "negative"}], "audio"),
      [("sound", "positive")])
check("空输入", _aspect_pairs(None, "phone"), [])

print("== ★ 页面汇总面板不能被当成评论（151 条事故）==")
FAKES = [
    "(0 Opiniones)0.00 / 5.00",
    "(222 Opiniones)4.80 / 5.00",
    "Opiniones\nEscribe una opinión\nOpiniones\n"
    "- Todavía no hay opiniones sobre este producto -\nSé el primero en escribir",
    "Opiniones\n4.8\n222 opiniones\n190\n23\n4\n2\n2\nEscribe una opinión\n"
    "Lo que opinan nuestros clientes",
    "Avaliações\n120 avaliações\n4,5 / 5\nSeja o primeiro a comentar",
]
for f in FAKES:
    check_true(f"拦下：{f[:38]!r}", not looks_like_review(f))

print("== ★ 竖排直方图（190/23/4/2/2 各占一行）也要拦 ==")
check_true("竖排数字块", not looks_like_review("Calidad\n190\n23\n4\n2\n2\n1"))

print("== 真评论不能被误杀 ==")
REAL = [
    "Benjamín\nEnviado hace 10 meses\nPondría 5 estrellas pero…\n"
    "Comprador verificado\nDos semanas desde la compra y todo de maravilla",
    "音质优秀，但App体验差；与手机连接不稳定，App常崩溃，无法调整均衡器。",
    "Excelente producto, la batería dura todo el día y la cámara es muy buena",
    "Muito bom, chegou rápido e funciona perfeitamente como esperado",
]
for r in REAL:
    check_true(f"放行：{r[:34]!r}", looks_like_review(r))

print("== 评论里顺口提到分数不算汇总面板 ==")
# 这条是**反向保险**：闸门不能一见到数字加斜杠就杀
check_true("★正常评论里的「我给4/5」不该被误杀",
           looks_like_review("Muy buen producto en general, "
                             "la batería podría durar más pero cumple bien"))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
