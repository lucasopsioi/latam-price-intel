# -*- coding: utf-8 -*-
"""VOC 评论抽取测试 —— 防止"抓到一堆界面文案，还当成消费者口碑分析"。

★ 这个文件的由来（2026-08-11 数据体检）：
  库里 721 条"评论"，体检下来：
    · 61.4% 是**另一条的子串** —— `[class*=opinion]` 这类子串选择器会同时命中
      外层包裹层、列表层和每条评论（三层 class 都含 opinion），一条评论存三遍；
    · 一部分根本不是评论，是"0 comentarios"（零评论空状态）、
      "Ordenar por: Mejores evaluaciones"（排序下拉）、星级直方图；
    · rating 只解析出 6%，author / review_date **一条都没有**。

  后果不是"数字大了点"：VOC 归因 Agent 的专职工作就是判断
  "评论量异常高说明什么"（真热销 / 大促冲量 / 刷评 —— 含义完全相反），
  喂它一个虚高 2.5 倍的数，它会**言之凿凿地**给出错误归因。

  这些函数是纯文本函数，不需要浏览器就能测 —— 所以没有任何理由不测。

跑法： python tests\test_voc.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scraping.voc import (dedupe_nested, looks_like_review,  # noqa: E402
                              parse_review_fields, parse_review_total,
                              parse_avg_rating)

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


print("== ★ 去嵌套：容器与子元素同时命中时只留最内层 ==")
# 真实形态：外层容器的文字里，原样含着每一条评论的文字
CONTAINER = ("Ordenar por:\nMejores evaluaciones\n"
             "Leandro\nhace 2 meses\nTop equipo\n"
             "Marcos\nhace 5 meses\nEl equipo es excelente, la calidad de los materiales")
LEAF_A = "Leandro\nhace 2 meses\nTop equipo"
LEAF_B = "Marcos\nhace 5 meses\nEl equipo es excelente, la calidad de los materiales"

out = dedupe_nested([CONTAINER, LEAF_A, LEAF_B])
check("容器被丢掉", CONTAINER in out, False)
check("叶子 A 保留", LEAF_A in out, True)
check("叶子 B 保留", LEAF_B in out, True)
check("剩下两条", len(out), 2)

# 同层级完全重复（翻页时同一条被重复抓到）
check("同层重复去掉", len(dedupe_nested([LEAF_A, LEAF_A, LEAF_B])), 2)
# 谁都不含谁 → 一条都不能丢
check("互不包含时全留", len(dedupe_nested([LEAF_A, LEAF_B])), 2)
# 空输入不能炸
check("空输入", dedupe_nested([]), [])
check("全空白输入", dedupe_nested(["", "   ", "\n"]), [])

print("== ★ 界面文案不能当成消费者口碑 ==")
# 零评论的空状态：这个产品一条评论都没有，抓到的是"0 条评论"+星级刻度
check("零评论空状态", looks_like_review("0 comentarios\n1\n2\n3\n4\n5"), False)
check("葡语零评论", looks_like_review("0 avaliações\n1\n2\n3\n4\n5"), False)
check("排序下拉", looks_like_review("Ordenar por:\nMejores evaluaciones"), False)
check("葡语排序", looks_like_review("Classificar por:\nMais recentes"), False)
check("纯星级刻度", looks_like_review("5\n/5\n1\n0\n2\n0\n3\n0\n4\n0\n5\n1"), False)
check("太短", looks_like_review("Bueno"), False)
check("空", looks_like_review(""), False)
check("None", looks_like_review(None), False)

# ★ 评分汇总面板：带着"屏幕质量""电池续航"这类自然语言词，
#   靠"有没有字母"根本挡不住 —— 必须靠星级直方图特征（1→N 2→N 3 连续数字串）
check("评分汇总面板(有自然语言词)",
      looks_like_review("4.4 /5 182 comentarios 1 11 2 5 3 11 4 23 5 132 "
                        "Calidad de la pantalla 5.0 Duración Batería 4.5"), False)
check("评分汇总面板(小样本)",
      looks_like_review("5 /5 1 comentario 1 0 2 0 3 0 4 0 5 1 "
                        "Calidad de la pantalla 5.0"), False)

print("== 真评论必须放行 ==")
check("西语真评论",
      looks_like_review("excelente equipo\npor joel\nhace 1 año\n"
                        "excelente equipo, salvo sus temperaturas que son elevadas"), True)
check("英语真评论",
      looks_like_review("Love it\npor DatGuy24\nhace 2 semanas\n"
                        "Amazing smartwatch and even better fitness and health device."), True)
check("带有用数的真评论",
      looks_like_review("Guido\nhace 1 año\nEs igual al de la foto pero se nota "
                        "que es usado\n2 personas encontraron este comentario útil"), True)
# 差评同样要放行（只挡界面文案，不能挡负面内容）
check("负面真评论",
      looks_like_review("Pésimo\npor Ana\nhace 3 días\n"
                        "Llegó con la pantalla rota y el cargador no era original"), True)

print("== ★ 相对时间要换算成绝对日期 ==")
# 口碑有时效：一年前的差评和上周的差评，对"本周竞争态势"的意义完全不同。
# 站上只给相对时间，基准日期显式传入（避免跨天跑出不同结果、也才测得了）
BASE = "2026-08-11"
check("hace 1 año", parse_review_fields("x\npor a\nhace 1 año\nbody", BASE)["review_date"],
      "2025-08-11")
check("hace 2 semanas", parse_review_fields("x\npor a\nhace 2 semanas\nbody", BASE)["review_date"],
      "2026-07-28")
check("hace 3 días", parse_review_fields("x\npor a\nhace 3 días", BASE)["review_date"],
      "2026-08-08")
check("hace 5 meses", parse_review_fields("x\npor a\nhace 5 meses\nbody", BASE)["review_date"],
      "2026-03-14")
check("葡语 há 2 meses", parse_review_fields("x\npor a\nhá 2 meses\nbody", BASE)["review_date"],
      "2026-06-12")
check("没有日期时为 None",
      parse_review_fields("solo texto sin fecha aqui", BASE)["review_date"], None)

print("== 作者与有用数 ==")
check("作者", parse_review_fields("Top\npor joel\nhace 1 año\nbody", BASE)["author"], "joel")
check("作者含空格",
      parse_review_fields("Top\npor Electronic Deals\nhace 1 año\nbody", BASE)["author"],
      "Electronic Deals")
check("无作者时为 None",
      parse_review_fields("Guido\nhace 1 año\nEs igual al de la foto", BASE)["author"], None)
check("有用数",
      parse_review_fields("x\nhace 1 año\nbody\n2 personas encontraron este comentario útil",
                          BASE)["helpful_count"], 2)
check("无有用数时为 None",
      parse_review_fields("x\npor a\nhace 1 año\nbody", BASE)["helpful_count"], None)

# 正文要把结构行剥掉，但**不能把评论本身剥没**
f = parse_review_fields("excelente equipo\npor joel\nhace 1 año\n"
                        "salvo sus temperaturas que son elevadas", BASE)
check_true("正文保留了评论主体", "temperaturas" in f["content"])
check_true("正文剥掉了作者行", "por joel" not in f["content"])
check_true("正文非空", len(f["content"]) > 0)
# 极端：整条只有结构行，剥完不能变成空
f2 = parse_review_fields("por joel\nhace 1 año", BASE)
check_true("只有结构行时正文不为空", len(f2["content"]) > 0)

print("== 评论总数 / 平均分 ==")
check("西语总数", parse_review_total("2,431 opiniones"), 2431)
check("葡语总数", parse_review_total("1.234 avaliações"), 1234)
check("没有总数", parse_review_total("sin comentarios aún"), None)
check("平均分", parse_avg_rating("4.4 de 5"), 4.4)
check("平均分斜杠", parse_avg_rating("4,2 / 5"), 4.2)

print("== ★ 端到端：真实脏数据进来，出去必须只剩真评论 ==")
# 这一组直接照搬体检时从库里捞出来的形态
RAW = [
    # 容器（含着下面两条的全文）
    "Ordenar por:\nMejores evaluaciones\nLeandro\nhace 2 meses\nTop equipo\n"
    "Marcos\nhace 5 meses\nEl equipo es excelente, la calidad de los materiales",
    "Leandro\nhace 2 meses\nTop equipo",
    "Marcos\nhace 5 meses\nEl equipo es excelente, la calidad de los materiales",
    # 评分汇总面板
    "4.4 /5 182 comentarios 1 11 2 5 3 11 4 23 5 132 Calidad de la pantalla 5.0",
    # 零评论空状态
    "0 comentarios\n1\n2\n3\n4\n5",
]
kept = [t for t in dedupe_nested(RAW) if looks_like_review(t)]
check("端到端只剩 2 条真评论", len(kept), 2)
check_true("留下的是 Leandro", any("Leandro" in t for t in kept))
check_true("留下的是 Marcos", any("Marcos" in t for t in kept))
check_true("汇总面板被滤掉", not any("182 comentarios" in t for t in kept))

print("== ★ _extract_visible 这条真实代码路径必须真的跑得通 ==")
# 上面测的都是纯函数。但线上真正执行的是 VocCollector._extract_visible，
# 它负责把这些函数串起来 —— 串错了（名字写错、参数顺序反了）纯函数测试一条都不会红，
# 而线上表现是"抓取正常但评论永远是 0 条"，跟网络问题长得一模一样。
# 这正是 VocAgent 那次 NameError 的翻版，所以这条路径要拿假页面走一遍。
from app.scraping.voc import VocCollector  # noqa: E402


class _FakeNode:
    def __init__(self, text):
        self._t = text

    def inner_text(self):          # Playwright 形态
        return self._t

    @property
    def text(self):                # Selenium 形态
        return self._t


class _FakePage:
    """只实现 _extract_visible 用到的那一个方法。"""

    def __init__(self, blocks):
        self._nodes = [_FakeNode(b) for b in blocks]

    def query_selector_all(self, sel):
        # 第一个选择器就命中，模拟真实站点
        return self._nodes if "review" in sel or "opinion" in sel else []


col = VocCollector(engine=None, run_id=0, cfg={"voc": {}})
got = col._extract_visible(_FakePage(RAW), is_selenium=False)

check("代码路径产出 2 条", len(got), 2)
check_true("每条都有 content", all(r.get("content") for r in got))
check_true("字段被解析出来了（作者或日期至少有一个）",
           any(r.get("author") or r.get("review_date") for r in got))
check_true("界面文案没混进来",
           not any("182 comentarios" in (r.get("content") or "") for r in got))
check_true("返回的是 dict 不是别的", all(isinstance(r, dict) for r in got))
# rating 键必须存在（_persist 会读它，缺键会 KeyError/写入 None 混乱）
check_true("每条都带 rating 键", all("rating" in r for r in got))

# 空页面不能炸
check("空页面返回空列表", col._extract_visible(_FakePage([]), is_selenium=False), [])

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
