# -*- coding: utf-8 -*-
"""VOC 消费者评论抓取。

用户要求：
  "在亚马逊上某个产品是主销款，那它的消费者评论你要把它都抓出来做 agent 分析；
   某些产品评论数非常多，你也要抓出来看一下消费者评论多到底是因为什么。
   所有的产品都要抓，我不在乎时间。"

抓取策略（各站形态不同，统一成三步）：
  1. 进商品详情页，读"评论总数"与星级分布 → 写 review_profile
     这一步就能识别主销款：**评论数是销量最好的公开代理指标**
  2. 点进"查看全部评论"，切最新排序，分页/滚动/点"加载更多"抓到上限
  3. 逐条落库（content_hash 去重，跨天重跑只增量）

★ 为什么评论总数比评论内容更早有价值：
  拿不到销量数据时，评论数是唯一能横向比较"谁卖得动"的公开信号。
  哪怕一条评论内容都没抓到，只要拿到"这款有 2400 条评论、那款只有 30 条"，
  就已经知道谁是主销款了。所以第 1 步和第 2 步分开做、分开落库。
"""
from __future__ import annotations

import logging
import re
import time

from .. import db, livelog
from ..config import load_runtime

log = logging.getLogger("voc")

# 各站"查看全部评论"入口的文案与选择器
REVIEW_ENTRY_TEXTS = [
    "ver todas las opiniones", "ver todas as avaliações", "ver todos os comentários",
    "ver más opiniones", "todas las reseñas", "see all reviews", "ver opiniones",
    "mostrar más", "carregar mais", "ver mais avaliações",
]
REVIEW_CONTAINER_SELECTORS = [
    "[data-hook='review']", ".review", "[class*=review-item]", "[class*=opinion]",
    "[class*=comment-item]", "[class*=ui-review]", "[id*=customer_review]",
    "[class*=avaliacao]", "[data-testid*=review]",
]
LOAD_MORE_SELECTORS = [
    "[class*=load-more]", "[class*=ver-mas]", "[class*=carregar-mais]",
    "a.a-pagination-next", "[class*=andes-pagination__button--next]",
    "button[class*=more]",
]

# 评论总数的文案模式："2,431 opiniones" / "1.234 avaliações" / "543 reseñas"
_TOTAL_PAT = re.compile(
    r"([\d.,]{1,12})\s*(opiniones|opini[õo]es|avalia[çc][õo]es|rese[ñn]as|"
    r"coment[áa]rios|comentarios|ratings?|reviews?|calificaciones)", re.I)
_RATING_PAT = re.compile(r"([0-5][.,]\d)\s*(?:de|out of|/)\s*5", re.I)
_STAR_PAT = re.compile(r"([1-5])\s*(?:estrellas?|estrelas?|stars?)", re.I)


# ★ 评论区的界面文案 —— 它们长在同一批容器里，不滤掉就会被当成"消费者说的话"。
#   实测入库的 721 条里，"0 comentarios / 1 2 3 4 5"（零评论的空状态）、
#   "Ordenar por: Mejores evaluaciones"（排序下拉）这类占了相当比例。
#   把界面文案喂给 VOC 分析，模型会一本正经地总结出根本不存在的口碑。
_CHROME_PAT = re.compile(
    r"^\s*(?:0\s*(?:coment[áa]rios?|comentarios?|opiniones?|opini[õo]es|"
    r"rese[ñn]as?|avalia[çc][õo]es|reviews?)"
    r"|ordenar\s+por|classificar\s+por|sort\s+by|filtrar\s+por"
    r"|mejores?\s+evaluaciones|m[áa]s\s+recientes?|mais\s+recentes?"
    r"|escrib[ie]r?\s+(?:una|uma|um)|escreva\s+uma|write\s+a\s+review"
    # ★ 展开/收起按钮：原来只写了裸的 `todas las opiniones`，但整条模式锚在行首
    #   （^\s*），而页面上的实际文案**前面带动词** ——
    #   实测「Ocultar todas las opiniones」出现 267 次，一次都匹配不上。
    #   这类按钮文案会被当成"消费者说的话"喂给 VOC 分析。
    r"|(?:mostrar|ver|ocultar|cargar|carregar|exibir|mostra)\s+"
    r"(?:todas?|todos|m[áa]s|mais)\b"
    r"|todas\s+las\s+opiniones|ver\s+m[áa]s)\b", re.I)

# 只有数字和标点 = 星级刻度条（"5/5 3 comentarios 1 0 2 0 3 0 4 0 5 3"）
_SCALE_ONLY = re.compile(r"^[\s\d./,%\-—|]*$")

# ★ 评分汇总面板：整站都长这样 ——
#   "4.4 /5  182 comentarios  1 11  2 5  3 11  4 23  5 132  屏幕质量 5.0 …"
#   它带着"屏幕质量""电池续航"这类词，靠"有没有自然语言"根本挡不住，
#   但它是**统计结果**不是消费者的话。特征是星级直方图：
#   1→数量 2→数量 3→数量 连续出现，正常评论里不可能有这种数字串。
_HISTOGRAM = re.compile(r"\b1\b[\s/|]*\d+[\s/|]*\b2\b[\s/|]*\d+[\s/|]*\b3\b")


# ★★ 汇总面板的第二形态 —— 第一版 _CHROME_PAT / _HISTOGRAM 都放它过去了，
#   实测 1727 条入库评论里有 151 条（8.7%）是这个：
#       "(0 Opiniones)0.00 / 5.00"
#       "Opiniones\n4.8\n222 opiniones\n190\n23\n4\n2\n2\nEscribe una opinión…"
#       "Opiniones\nEscribe una opinión\n- Todavía no hay opiniones sobre este producto -"
#   为什么前两道闸挡不住：
#     · _CHROME_PAT 锚在行首（^），而这些块前面还有 "(" 或 "Opiniones\n"；
#     · _HISTOGRAM 找的是横排 "1 n 2 n 3"，这里的直方图是**竖排**
#       （190/23/4/2/2 各占一行），数字之间没有 1→2→3 的序号。
#   ★ 最糟的是 "0 Opiniones / 还没有评论" 那一类：**零评论的产品被记了一条评论**，
#     评论量当销量代理指标（方向3）和主销款判定会被直接抬高。
#   判据用"这段话在讲评论这件事本身"，而不是长度 —— 实测汇总块中位长度 145、
#   正常评论 128，靠长度分不开。
_SUMMARY_PAT = re.compile(
    r"(?:\d[\d.,]*\s*(?:opiniones|opini[õo]es|avalia[çc][õo]es|rese[ñn]as|"
    r"comentarios|coment[áa]rios|reviews?)"          # "222 opiniones"：总数口吻
    r"|\d[.,]\d{1,2}\s*/\s*5(?:[.,]\d{1,2})?"        # "4.80 / 5.00"：平均分
    r"|todav[íi]a\s+no\s+hay|ainda\s+n[ãa]o\s+h[áa]|s[ée]\s+el\s+primero"
    r"|seja\s+o\s+primeiro|no\s+hay\s+opiniones"     # 空状态
    r"|escribe\s+una\s+opini|escreva\s+uma|lo\s+que\s+opinan)", re.I)

# ★★ 第三形态：**评分分布控件**（用户 2026-08-27 实测报回）
#   "5 estrellas 91 % 4 estrellas 9 % 3 estrellas 0 % 2 estrellas 0 % 1 estrellas 0 %"
#   实测 44 条入库，全是 Liverpool 墨西哥的商品页。前面三道闸为什么全放它过去：
#     · _CHROME_PAT 锚在行首（^），而这块以 "5 estrellas" 开头，不在词表里；
#     · _SUMMARY_PAT 的词表里**没有 estrellas** —— 它认的是 "222 opiniones"
#       这种"总数口吻"，而分布控件说的是"各星级占比"，措辞完全不同；
#     · _HISTOGRAM 找的是**升序** "1 n 2 n 3"，而这个控件是**降序** 5→4→3→2→1。
#   后果和汇总面板同族：把统计结果当成"消费者说的话"喂给 VOC 分析。
#
# ★ 判据用**结构**而不是阈值 —— 这一条是踩过反例才定下来的：
#   最初按"正文里出现 ≥3 个百分数就判控件"写，实测会误杀 id=4253
#   那条 Falabella 秘鲁的长篇真评论 —— 它在讲快充：
#     "no pasa ni 30 min y ya está completa de 0% a 100%…en 95% va genial"
#   三个百分数、字母占比 0.96，是全库质量最高的评论之一。
#   改成认"刻度行"的形态：`星级标签 + 分隔 + 数字%`，其中标签与数字之间
#   **必须有空白**（所以 "100%" 里的 1 和 00 不成立），重复 ≥3 次才算控件。
#   星词可缺省，是为了接住 "5 ★ 91% / 4 ★ 9%" 和纯数字刻度那两种变体。
_STAR_LABEL = re.compile(r"\b[1-5]\s*(?:estrellas?|estrelas?|stars?|★)", re.I)
_DIST_ROW = re.compile(
    r"\b[1-5]\s*(?:estrellas?|estrelas?|stars?|★)?\s*[:\-–]?\s+\d{1,3}\s*%", re.I)

# ★ Material Icons 的**连字符号字面量**：站点用 <i class=material-icons>keyboard_arrow_up</i>
#   实现图标，innerText 读出来就是这个单词本身。实测 162 条入库。
#   它可能单独成块，也可能粘在真评论前后，所以是"先擦掉再看还剩什么"，
#   而不是"命中就整块丢" —— 后者会连带丢掉粘在一起的真评论。
_ICON_LIGATURE = re.compile(
    r"\b(?:keyboard_arrow_(?:up|down|left|right)|expand_(?:more|less)"
    r"|arrow_(?:drop_)?(?:up|down|back|forward)|chevron_(?:left|right)"
    r"|more_vert|more_horiz|star_(?:border|half|rate)|check_circle)\b", re.I)

# ★ 整块就是一个按钮/栏目标题的：用 fullmatch 而不是 search。
#   实测这三种各自都**一字不差重复上百次**（栏目标题 162、按钮 103），
#   说明它们独立成块、后面不跟正文。用 fullmatch 才不会误伤
#   "Opiniones de mi compra: …" 这种真的以该词开头的评论。
_UI_ONLY = re.compile(
    r"\s*(?:"
    r"(?:opiniones|opini[õo]es|avalia[çc][õo]es|rese[ñn]as|coment[áa]rios?)"
    r"\s+(?:del?|de\s+la|do|da)\s+(?:art[íi]culo|produto|producto|item)"
    r"|escrib[ie]r?\s+(?:una|uma|um)\s+(?:opini[óo]n|opini[ãa]o|rese[ñn]a|avalia[çc][ãa]o)"
    r"|(?:mostrar|ver|exibir)\s+traduc(?:ci[óo]n|[çc][ãa]o)"
    r"|\d+\s*personas?\s+encontr[óo]?\s+este\s+comentario\s+[úu]til\.?"
    r")\s*", re.I)


def letter_ratio(text: str) -> float:
    """非空白字符里字母的占比。控件行数字多、字母少。

    ★ 这条是**后备闸**，不是主判据。实测它抓不干净、也不能调紧：
      垃圾行（只有"5.0 11 Ago 2026 Bernarda B."）落在 0.545~0.583，
      而一条**真评论**「genial e increible genial el telefono......」
      （用户拿句点填充）落在 0.567，**正好夹在垃圾行中间**。
      阈值往上抬一点就误杀它 ⇒ 0.55 这条线只能抓最极端的一批，
      剩下的交给 strip_page_head()：抬头剥完变成空正文，被长度闸自然拦掉。
    """
    # ★ 标点连击先折成一个：真评论里的 "telefono......"（40 个句点）
    #   会把字母占比从 0.85 拖到 0.567，离 0.55 的闸只剩 0.017。
    #   控件行没有标点连击，折叠对它分毫无影响 ⇒ 只加安全边际、不放水。
    body = re.sub(r"([^\w\s])\1{2,}", r"\1", text or "")
    body = re.sub(r"\s+", "", body)
    if not body:
        return 0.0
    return len(re.findall(r"[^\W\d_]", body)) / len(body)


def review_reject_reason(text: str) -> str | None:
    """这段文字**为什么**不像评论；像评论就返回 None。

    ★ 为什么要把"理由"暴露出来，而不是只给一个布尔：
      清理历史脏数据时，「被拦下」这一个信号是**不够分辨**的 ——
      实测按布尔清理会把 "Muy buen equipo"、"Entrega super rapida"
      这类**真实短评**和 "keyboard_arrow_up" 一起删掉：
      前者栽在"至少要有连续 8 个字母"那条老启发式上（legacy 数据，
      入库时还没有这条规则），后者才是界面文案。
      两者该有完全不同的处置（前者留着，后者删掉），
      所以判据必须能说出自己是哪一条。
    """
    # 图标连字不是人话，先擦掉再判断 —— 擦完为空的就是纯图标块
    raw = re.sub(r"[ \t]+", " ", (text or "")).strip()
    t = _ICON_LIGATURE.sub(" ", raw)
    t = re.sub(r"[ \t]+", " ", t).strip()
    if len(t) < 15:
        # ★ 必须分清两种"太短"：擦掉图标才变短的是**纯图标块**（该删），
        #   本来就短的是**真实短评**（"Top equipo"，该留）。
        #   混为一谈实测会误删 162 条 keyboard_arrow_up 之外的 193 条真短评。
        return "icon_only" if _ICON_LIGATURE.search(raw) else "too_short"
    # 整块就是一个按钮 / 栏目标题（"Opiniones del artículo"、"Escribir una opinión"）
    if _UI_ONLY.fullmatch(t):
        return "ui_only"
    if _CHROME_PAT.search(t):
        return "chrome"
    if _SCALE_ONLY.match(t):
        return "scale_only"
    flat = re.sub(r"\s+", " ", t)
    if _HISTOGRAM.search(flat):
        return "histogram"
    if _SUMMARY_PAT.search(t):
        return "summary"
    # ★ 评分分布控件：星级标签重复 ≥3 次，或"标签 数字%"的刻度行重复 ≥3 次。
    #   两条都要，因为控件有两种形态：带星词的（"5 estrellas 91 %"）
    #   和纯数字刻度的（"5  91 %"）。用重复次数而不是单次命中 ——
    #   真评论说得出 "le doy 5 estrellas"，但不会连说三个星级标签。
    if len(_STAR_LABEL.findall(flat)) >= 3 or len(_DIST_ROW.findall(flat)) >= 3:
        return "rating_dist"
    # ★ 后备闸：字母太少不是人话（阈值为什么只能是 0.55 见 letter_ratio 注释）
    if letter_ratio(t) < 0.55:
        return "low_letter"
    # 竖排直方图：大半的行都是光秃秃的数字（"190\n23\n4\n2\n2"）
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    if len(lines) >= 4:
        bare = sum(1 for ln in lines if re.fullmatch(r"[\d.,]{1,7}", ln))
        if bare >= len(lines) * 0.5:
            return "vertical_hist"
    # 至少要有连续 8 个字母的自然语言片段，挡掉 "1 0 2 0 3 0" 混着单词的刻度条
    if not (re.search(r"[^\W\d_]{8,}", t)
            or len(re.findall(r"[^\W\d_]{3,}", t)) >= 4):
        return "not_language"
    return None


# ★ 哪些理由属于"这是界面控件/页面文案，不是消费者的话"。
#   清理历史数据时**只能删这几类** —— too_short / not_language 是
#   启发式对短评论的误伤面，删了就是把真口碑删掉（实测会误删约 100 条）。
UI_NOISE_REASONS = frozenset({
    "icon_only", "ui_only", "chrome", "scale_only", "histogram", "summary",
    "rating_dist", "low_letter", "vertical_hist"})


def looks_like_review(text: str) -> bool:
    """这段文字像不像一条真实评论。宁可漏，也不要把界面文案当口碑。

    ★ 闸门顺序有讲究：**先擦图标再量长度**。
      Material Icons 的连字（keyboard_arrow_up）粘在正文上会把一段
      本来 12 字的空壳撑过 15 字的长度闸；擦掉之后长度闸才量的是真正文。
    """
    return review_reject_reason(text) is None


# ★ 页面把「评分 日期 标题 作者」和正文拼在同一个文本块里，例如：
#     "2.0 23 Mar 2026 Pretenciosos para lo que ofrecen Anónimo Vinieron defectuosos…"
#     "5.0 11 Ago 2026 alfonso f. Buen cambio de tener un iphone 13"
#   实测 108 条入库正文带着这段抬头。两个后果：
#     ① 周报直接引用会以"5.0 11 Ago 2026 alfonso f."开头，读者当成乱码；
#     ② 更贵的是**只有抬头、没有正文**的空壳行（"5.0 11 Ago 2026 Bernarda B."）——
#        它长度过闸、字母占比也可能过闸，于是被当成一条评论送进 LLM，
#        模型只能凭空编一句译文（实测编出"设备有缺陷""赞助内容"）。
#   剥掉抬头后空壳自然变成空串，被 _persist 的长度闸拦下，不用另设规则。
#
# ★ 作者名是可靠的**右边界**：拉美站上作者要么是 "Anónimo"，
#   要么是 "名 + 首字母."（Carlos T. / alfonso f. / MARISELA A.）。
#   首字母**大小写都有**（实测 "alfonso f."），所以不能只认大写。
#   窗口卡在 90 字以内：标题最长实测 77 字，再宽就有啃到正文的风险。
_PAGE_HEAD = re.compile(r"^\s*\d(?:[.,]\d)?\s+\d{1,2}\s+[A-Za-zÁ-úñ]{3,}\.?\s+\d{4}\b[\s,·|-]*")
_HEAD_AUTHOR = re.compile(
    r"^.{0,90}?\b(?:an[óo]nimo|[^\W\d_][^\W\d_'’-]{1,20}\s+[^\W\d_]\.)(?=\s|$)\s*", re.I)


def strip_page_head(text: str) -> str:
    """剥掉「评分 日期 [标题] 作者」抬头，只留消费者写的正文。

    ★ 分两步而不是一条大正则：**先认抬头，再认作者**。
      作者形态（"名 + 首字母."）单独拿出来用会误伤正文里的
      "…del modelo Francisco R. mala calidad…" 这种；
      只有在"这块确实以 评分+日期 开头"成立之后再找作者才安全 ——
      评分+日期是页面拼接的确凿结构信号，正文里不会自己长出来。
    ★ 找不到作者就只剥评分+日期，**宁可留标题也不啃正文**。
    """
    t = (text or "").strip()
    m = _PAGE_HEAD.match(t)
    if not m:
        return t
    rest = t[m.end():]
    m2 = _HEAD_AUTHOR.match(rest)
    return (rest[m2.end():] if m2 else rest).strip()


def dedupe_nested(blocks: list[str]) -> list[str]:
    """去掉"容器"，只留最内层的评论。

    ★ 这是 VOC 数据一直不可信的根因：
      `[class*=opinion]` 这类子串选择器会**同时命中**外层包裹层、列表层
      和每一条评论 —— 因为它们的 class 里都含 opinion。于是同一条评论
      按嵌套层级重复入库，实测 721 条记录里 61.4% 是别人的子串。

      后果不是"多了几条"，而是**评论量整体虚高约 2.5 倍**：
      VOC 归因 Agent 专门判断"评论量异常高说明什么"，
      喂给它一个虚高的数它必然给出错误结论（把正常品判成爆款/刷评）。

      判据用文本包含而非 DOM 关系：容器的文字必然包含其子节点的文字，
      这条在任何站点结构下都成立，不用为每个站写一套 DOM 规则。
    """
    cleaned = [b.strip() for b in blocks if b and b.strip()]
    out = []
    for i, a in enumerate(cleaned):
        # a 里含着别人的全文 → a 是容器，丢掉，留里面那条
        if any(j != i and b and b in a and len(b) < len(a)
               for j, b in enumerate(cleaned)):
            continue
        out.append(a)
    # 同层级的完全重复也去掉（翻页时同一条被重复抓到）
    seen, uniq = set(), []
    for t in out:
        k = re.sub(r"\s+", " ", t)[:200]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    return uniq


# 相对时间："hace 2 meses" / "há 3 semanas" / "hace 1 año"
_REL_DATE = re.compile(
    r"\b(?:hace|h[áa])\s+(\d+)\s*(d[íi]as?|semanas?|m[êe]s|meses|a[ñn]os?|anos?)\b", re.I)
_AUTHOR = re.compile(r"\bpor\s+([^\n]{1,40}?)\s*(?:\n|$)", re.I)
_HELPFUL = re.compile(r"(\d+)\s*personas?\s+encontraron", re.I)

_REL_UNIT_DAYS = {"dia": 1, "día": 1, "dias": 1, "días": 1, "semana": 7,
                  "semanas": 7, "mes": 30, "meses": 30, "mês": 30,
                  "ano": 365, "anos": 365, "año": 365, "años": 365}


def parse_review_fields(text: str, today: str | None = None) -> dict:
    """从评论文本块里拆出作者/日期/有用数/正文。

    ★ 为什么非要解析日期：入库的 721 条 review_date 全是 NULL，
      于是"一年前的评论"和"上周的评论"在分析里权重一样。
      口碑是有时效的 —— 拿一年前的差评去判断这周的竞争态势会得出反的结论。
      站上只给相对时间（"hace 1 año"），这里换算成绝对日期。
      换算基准显式传入，方便测试，也避免跨天跑出不同结果。
    """
    from datetime import date, timedelta

    t = (text or "").strip()
    base = date.fromisoformat(today) if today else date.today()

    out: dict = {"author": None, "review_date": None,
                 "helpful_count": None, "content": t}

    m = _AUTHOR.search(t)
    if m:
        out["author"] = m.group(1).strip()[:60] or None

    m = _REL_DATE.search(t)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = _REL_UNIT_DAYS.get(unit)
        if days:
            out["review_date"] = (base - timedelta(days=n * days)).isoformat()

    m = _HELPFUL.search(t)
    if m:
        out["helpful_count"] = int(m.group(1))

    # 正文：把结构行（作者/时间/有用数）剥掉，剩下的才是消费者说的话
    body = [ln.strip() for ln in t.split("\n") if ln.strip()]
    body = [ln for ln in body
            if not _AUTHOR.fullmatch(ln + "\n") and not _REL_DATE.fullmatch(ln)
            and not _HELPFUL.search(ln)]
    # ★ 上面是**按行**剥，碰不到「评分 日期 标题 作者」——
    #   那段抬头和正文拼在**同一行**里（"5.0 11 Ago 2026 alfonso f. Buen cambio…"）。
    #   注意顺序：星级由调用方在**原始文本**上取（_rating_from），
    #   所以这里剥掉抬头不会把评分一起弄丢。
    out["content"] = strip_page_head("\n".join(body) if body else t)[:2000]
    return out


# 一个拉美零售 SKU 的评论数上限。实测最多的商品页是 2114 条，
# 留一个数量级的余量。超过这个数的几乎必然是价格或电话号码。
_MAX_REVIEWS = 50_000
# 紧挨在数字前面的货币符号 —— 有它就说明这个数是价格不是计数
_MONEY_BEFORE = re.compile(r"(?:[$€£]|R\$|S/\.?|COP|CLP|MXN|PEN|BRL)\s*$", re.I)
# 页脚的「意见与建议」客服入口，前面挨着的是电话号码
_FOOTER_HINT = re.compile(r"^\s*y\s+sugerencias", re.I)


def parse_review_total(text: str) -> int | None:
    """从页面文本解析评论总数。

    ★★ 原来只按数值范围过滤（`0 < v < 10_000_000`），实测两类误匹配都能过闸，
      而且**取的是最大值**，所以误匹配必然胜出：

        「$ 429.990 Opiniones de este producto 5 /5 2 comentarios」
          ↑ 价格紧跟着栏目标题「Opiniones de este producto」，
            于是 429990 被当成评论数，而真正的 2 条在后面。
        「…al 5552629999 Comentarios y Sugerencias」
          ↑ 页脚客服电话 + 「意见与建议」入口。

      后果：449 个商品页的"评论总数"合计 4.67 亿条，
      而 VOC 归因 Agent 正是靠这个数判断「评论量异常高说明什么」——
      喂给它一个虚高一万倍的数，它必然给出错误结论（把普通商品判成爆款/刷评）。

    ⇒ 三道闸：① 数字前紧挨货币符号的排除 ② 后面跟「y sugerencias」的排除
      ③ 上限收到 5 万（实测最大的商品页是 2114 条）。
      并且**取最小的合理值而不是最大值** —— 页面上真正的计数总是那个小数，
      大数几乎必然是价格。
    """
    if not text:
        return None
    cands = []
    head = text[:6000]
    for m in _TOTAL_PAT.finditer(head):
        raw = m.group(1).replace(".", "").replace(",", "")
        if not raw.isdigit():
            continue
        v = int(raw)
        if not (0 < v <= _MAX_REVIEWS):
            continue
        if _MONEY_BEFORE.search(head[max(0, m.start() - 8):m.start()]):
            continue                      # 价格
        if _FOOTER_HINT.match(head[m.end():m.end() + 20]):
            continue                      # 页脚「意见与建议」
        cands.append(v)
    return min(cands) if cands else None


def parse_avg_rating(text: str) -> float | None:
    m = _RATING_PAT.search(text or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
        return v if 0 <= v <= 5 else None
    except ValueError:
        return None


class VocCollector:
    """评论抓取器。与价格采集共用同一个引擎实例（复用会话与反爬状态）。"""

    def __init__(self, engine, run_id: int, cfg: dict | None = None):
        self.engine = engine
        self.run_id = run_id
        voc_cfg = (cfg or load_runtime()).get("voc", {})
        self.max_reviews = int(voc_cfg.get("max_reviews_per_product", 300))
        self.max_seconds = int(voc_cfg.get("max_seconds_per_product", 240))
        self.hot_threshold = int(voc_cfg.get("hot_product_review_threshold", 100))
        self.stats = {"products": 0, "reviews": 0, "hot": 0, "failed": 0}

    # ------------------------------------------------ 主入口

    def collect_for_product(self, url: str, *, country: str, channel_id: int,
                            rival_product_id: int | None = None,
                            product_title: str = "") -> dict:
        """抓一个商品的评论。返回统计。"""
        t0 = time.time()
        livelog.emit("voc", f"读评论：{product_title[:44] or url[:44]}")

        result = self.engine.run_on_page(
            url, lambda page: self._harvest(page, url, t0),
            country=country, extra_wait=3.0)

        if result is None:
            self.stats["failed"] += 1
            log.info("[voc] 抓取失败/被拦：%s", url[:90])
            return {"status": self.engine.last_status, "reviews": 0}

        total, avg, reviews = result
        n = self._persist(reviews, url, country, channel_id,
                          rival_product_id, product_title)
        is_hot = bool(total and total >= self.hot_threshold)
        self._save_profile(url, channel_id, country, rival_product_id,
                           total, n, avg, is_hot)

        self.stats["products"] += 1
        self.stats["reviews"] += n
        self.stats["hot"] += 1 if is_hot else 0
        livelog.emit("voc", f"{product_title[:36] or '商品'}：标称 {total or '?'} 条评论，"
                            f"抓到 {n} 条{'（主销款）' if is_hot else ''}",
                     count=n, hot=is_hot)
        return {"status": "ok", "total": total, "fetched": n, "is_hot": is_hot,
                "avg_rating": avg, "seconds": round(time.time() - t0, 1)}

    # ------------------------------------------------ 页面交互

    def _harvest(self, page, url: str, t0: float):
        """在商品页上：读总数 → 进全部评论 → 分页抓到上限。

        page 可能是 Playwright Page 也可能是 Selenium WebDriver，
        所以只用两者都有的能力，并按类型分派。
        """
        is_selenium = not hasattr(page, "query_selector_all")

        text = self._page_text(page, is_selenium)
        total = parse_review_total(text)
        avg = parse_avg_rating(text)

        # 先尝试点进"全部评论"
        self._click_review_entry(page, is_selenium)

        reviews, seen = [], set()
        rounds, no_new = 0, 0
        while (len(reviews) < self.max_reviews
               and time.time() - t0 < self.max_seconds
               and no_new < 2 and rounds < 40):
            rounds += 1
            batch = self._extract_visible(page, is_selenium)
            fresh = 0
            for r in batch:
                key = (r["content"] or "")[:120]
                if key and key not in seen:
                    seen.add(key)
                    reviews.append(r)
                    fresh += 1
            no_new = no_new + 1 if fresh == 0 else 0
            if len(reviews) >= self.max_reviews:
                break
            if not self._go_next(page, is_selenium):
                break

        # ★★ 第二遍：切到「最差评价」排序再抓一次。
        #   实测证据：160 个采集页的排序控件明写着
        #   「Ordenar por: Mejores evaluaciones」—— **默认就是好评优先**。
        #   只抓默认排序等于系统性地漏掉差评：库里正负比 21:1（差评仅 42 条），
        #   而"好评差评都要有量"是能不能做统计的前提 ——
        #   42 条差评摊到 机型×国家×维度 之后，最大的一组只剩 6 条，
        #   差评预警（方向18）因此完全做不了。
        #   ★ 这个偏差不是"数据就是这样"，是**我们只读了其中一栏**。
        if len(reviews) < self.max_reviews and time.time() - t0 < self.max_seconds:
            if self._sort_worst_first(page, is_selenium):
                no_new, rounds = 0, 0
                while (len(reviews) < self.max_reviews
                       and time.time() - t0 < self.max_seconds
                       and no_new < 2 and rounds < 20):
                    rounds += 1
                    fresh = 0
                    for r in self._extract_visible(page, is_selenium):
                        key = (r["content"] or "")[:120]
                        if key and key not in seen:
                            seen.add(key)
                            reviews.append(r)
                            fresh += 1
                    no_new = no_new + 1 if fresh == 0 else 0
                    if not self._go_next(page, is_selenium):
                        break
        return total, avg, reviews[: self.max_reviews]

    # 「最差评价」在各站的写法。★ 这些词只出现在**展开后的**下拉选项里 ——
    # 收起状态的页面文本里只看得到当前选中的「Mejores evaluaciones」，
    # 所以必须真的去点，不能靠文本匹配判断有没有这个选项。
    _WORST_TEXTS = ("peores evaluaciones", "peores calificaciones",
                    "peor calificación", "peores valoraciones",
                    "menor calificación", "piores avaliações",
                    "lowest rating", "worst")
    _SORT_TRIGGER = ("ordenar por", "ordenar", "ordenar avaliações", "sort by")

    def _sort_worst_first(self, page, is_selenium: bool) -> bool:
        """把评论排序切成「最差优先」。切不动就返回 False，不算失败。"""
        try:
            if is_selenium:
                from selenium.webdriver.common.by import By
                # ① 原生 <select>：直接选值最省事
                for sel in page.find_elements(By.CSS_SELECTOR, "select"):
                    for opt in sel.find_elements(By.TAG_NAME, "option"):
                        if any(w in (opt.text or "").lower() for w in self._WORST_TEXTS):
                            page.execute_script("arguments[0].click();", opt)
                            time.sleep(2.5)
                            return True
                # ② 自定义下拉：先点触发器，再点选项
                for trig in self._SORT_TRIGGER:
                    els = page.find_elements(
                        By.XPATH,
                        f"//*[contains(translate(text(),"
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ',"
                        f"'abcdefghijklmnopqrstuvwxyzáéíóú'),'{trig}')]")
                    if not els:
                        continue
                    page.execute_script("arguments[0].click();", els[0])
                    time.sleep(1.2)
                    for w in self._WORST_TEXTS:
                        opts = page.find_elements(
                            By.XPATH,
                            f"//*[contains(translate(text(),"
                            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ',"
                            f"'abcdefghijklmnopqrstuvwxyzáéíóú'),'{w}')]")
                        if opts:
                            page.execute_script("arguments[0].click();", opts[0])
                            time.sleep(2.5)
                            return True
            else:
                for w in self._WORST_TEXTS:
                    loc = page.get_by_text(re.compile(re.escape(w), re.I)).first
                    if loc.count() > 0:
                        loc.click(timeout=4000)
                        page.wait_for_timeout(2500)
                        return True
                for trig in self._SORT_TRIGGER:
                    loc = page.get_by_text(re.compile(re.escape(trig), re.I)).first
                    if loc.count() > 0:
                        loc.click(timeout=4000)
                        page.wait_for_timeout(1200)
                        for w in self._WORST_TEXTS:
                            o = page.get_by_text(re.compile(re.escape(w), re.I)).first
                            if o.count() > 0:
                                o.click(timeout=4000)
                                page.wait_for_timeout(2500)
                                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    def _page_text(page, is_selenium: bool) -> str:
        try:
            if is_selenium:
                return page.find_element("tag name", "body").text
            return page.inner_text("body")
        except Exception:  # noqa: BLE001
            return ""

    def _click_review_entry(self, page, is_selenium: bool) -> None:
        try:
            if is_selenium:
                from selenium.webdriver.common.by import By
                for txt in REVIEW_ENTRY_TEXTS:
                    els = page.find_elements(
                        By.XPATH,
                        f"//*[contains(translate(text(),"
                        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        f"'{txt}')]")
                    if els:
                        page.execute_script("arguments[0].click();", els[0])
                        time.sleep(2.5)
                        return
            else:
                for txt in REVIEW_ENTRY_TEXTS:
                    loc = page.get_by_text(re.compile(txt, re.I)).first
                    if loc.count() > 0:
                        loc.click(timeout=4000)
                        page.wait_for_timeout(2500)
                        return
        except Exception:  # noqa: BLE001
            pass    # 找不到入口就抓当前页可见的精选评论，不算失败

    def _extract_visible(self, page, is_selenium: bool) -> list[dict]:
        out = []
        for sel in REVIEW_CONTAINER_SELECTORS:
            try:
                nodes = (page.find_elements("css selector", sel) if is_selenium
                         else page.query_selector_all(sel))
            except Exception:  # noqa: BLE001
                continue
            if not nodes:
                continue
            blocks, node_of = [], {}
            for node in nodes[:120]:
                try:
                    txt = (node.text if is_selenium else node.inner_text()) or ""
                except Exception:  # noqa: BLE001
                    continue
                txt = txt.strip()
                blocks.append(txt)
                # 留住 文本→节点 的对应关系：星级读不出来时要回到 DOM 上取，
                # 而 dedupe_nested 只认字符串
                node_of.setdefault(txt, node)

            # 先去嵌套（同一选择器会同时命中包裹层和每条评论），再滤界面文案。
            # 顺序不能反：容器文字里往往夹着 "Ordenar por"，先滤会把整个容器
            # 丢掉、连带丢掉里面真正的评论。
            for txt in dedupe_nested(blocks):
                if not looks_like_review(txt):
                    continue
                f = parse_review_fields(txt)
                f["rating"] = (self._rating_from(txt)
                               or self._rating_from_dom(node_of.get(txt), is_selenium))
                out.append(f)
            if out:
                break       # 命中一个选择器就够，别把同一批评论按多个选择器重复收
        return out

    @staticmethod
    def _rating_from(text: str) -> float | None:
        m = _RATING_PAT.search(text[:200]) or _STAR_PAT.search(text[:200])
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None

    @staticmethod
    def _rating_from_dom(node, is_selenium: bool) -> float | None:
        """从 DOM 属性里读单条评论的星级。

        ★ 为什么必须有这个：星级在页面上是**图标/CSS**，不是文字。
          `node.text` 拿到的正文里根本没有星级 —— 实测入库 1727 条评论
          里只有 2 条有 rating（0.1%），纯文本解析在这件事上等于没用。
          后果是"上市后口碑衰减曲线"（要按时间看平均星级）**一条都画不出来**，
          而且从日志上看不出来 —— 评论抓到了、日期也有，就是没有分数。

        ★ 四种取法按可靠性排序，取到就停：
          1. aria-label / title —— 无障碍文案，站方写给读屏软件的，最可靠
          2. itemprop=ratingValue —— schema.org 结构化标注
          3. data-rating / data-score 之类的自定义属性
          4. 亮着的星星个数 —— 最脆，但有些站只有这个
          全都取不到就返回 None。**不猜**：编一个 5 星出来会让口碑曲线
          凭空变好看，比没有曲线糟糕得多。
        """
        if node is None:
            return None

        def _val(s: str | None) -> float | None:
            if not s:
                return None
            m = (_RATING_PAT.search(s) or _STAR_PAT.search(s)
                 or re.search(r"\b([0-5](?:[.,]\d)?)\b", s))
            if not m:
                return None
            try:
                v = float(m.group(1).replace(",", "."))
            except ValueError:
                return None
            return v if 0 <= v <= 5 else None

        try:
            if is_selenium:
                from selenium.webdriver.common.by import By
                for attr in ("aria-label", "title", "data-rating", "data-score"):
                    v = _val(node.get_attribute(attr))
                    if v is not None:
                        return v
                for css, attr in (("[itemprop=ratingValue]", "content"),
                                  ("[aria-label]", "aria-label"),
                                  ("[title]", "title"),
                                  ("[data-rating]", "data-rating")):
                    for el in node.find_elements(By.CSS_SELECTOR, css)[:6]:
                        v = _val(el.get_attribute(attr) or el.get_attribute("textContent"))
                        if v is not None:
                            return v
                lit = node.find_elements(
                    By.CSS_SELECTOR,
                    "[class*=star][class*=full],[class*=star][class*=fill],"
                    "[class*=star][class*=active],[class*=estrella][class*=activ]")
                if lit and len(lit) <= 5:
                    return float(len(lit))
            else:
                for attr in ("aria-label", "title", "data-rating", "data-score"):
                    v = _val(node.get_attribute(attr))
                    if v is not None:
                        return v
                for css, attr in (("[itemprop=ratingValue]", "content"),
                                  ("[aria-label]", "aria-label"),
                                  ("[title]", "title"),
                                  ("[data-rating]", "data-rating")):
                    for el in node.query_selector_all(css)[:6]:
                        v = _val(el.get_attribute(attr) or el.inner_text())
                        if v is not None:
                            return v
                lit = node.query_selector_all(
                    "[class*=star][class*=full],[class*=star][class*=fill],"
                    "[class*=star][class*=active],[class*=estrella][class*=activ]")
                if lit and len(lit) <= 5:
                    return float(len(lit))
        except Exception:  # noqa: BLE001
            return None
        return None

    def _go_next(self, page, is_selenium: bool) -> bool:
        """翻页/加载更多/滚动懒加载，三种形态都试。"""
        for sel in LOAD_MORE_SELECTORS:
            try:
                if is_selenium:
                    els = page.find_elements("css selector", sel)
                    if els and els[0].is_displayed():
                        page.execute_script("arguments[0].click();", els[0])
                        time.sleep(2.2)
                        return True
                else:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click(timeout=4000)
                        page.wait_for_timeout(2200)
                        return True
            except Exception:  # noqa: BLE001
                continue
        # 滚动懒加载兜底
        try:
            if is_selenium:
                page.execute_script("window.scrollBy(0,1400);")
            else:
                page.mouse.wheel(0, 1400)
            time.sleep(1.6)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------ 落库

    def _persist(self, reviews: list[dict], url: str, country: str,
                 channel_id: int, rival_product_id: int | None,
                 product_title: str) -> int:
        # ★ 挂产品这件事不能只靠调用方传参。
        #   实测入库的 721 条评论 rival_product_id **全是 NULL** ——
        #   而其中 719 条的 URL 在 price_obs 里明明挂着产品。
        #   只要传参那一刻链接还没建立（阶段顺序、DISTINCT 取到 NULL 的那行、
        #   或单独触发 VOC），评论就永久变成孤儿：
        #   VOC 洞察按 rival_product_id 分组，孤儿评论一条都进不了分析，
        #   于是 review 有 721 条、voc_insight 恒为 0，界面上看就是"VOC 没抓到"。
        #   这里按 URL 自己兜底找一次，比依赖调用顺序可靠。
        if rival_product_id is None:
            row = db.q1("""SELECT rival_product_id FROM price_obs
                           WHERE url=? AND rival_product_id IS NOT NULL
                           ORDER BY obs_date DESC LIMIT 1""", (url,))
            if row:
                rival_product_id = row["rival_product_id"]

        n = 0
        with db.tx() as conn:
            for r in reviews:
                content = (r.get("content") or "").strip()
                if len(content) < 15:
                    continue
                h = db.row_hash(url, content[:200])
                cur = conn.execute("""
                    INSERT OR IGNORE INTO review(rival_product_id,channel_id,country_code,
                        product_title,product_url,rating,content,content_hash,run_id,
                        author,review_date,helpful_count)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """, (rival_product_id, channel_id, country, product_title[:250],
                      url, r.get("rating"), content, h, self.run_id,
                      r.get("author"), r.get("review_date"), r.get("helpful_count")))
                if cur.rowcount:
                    n += 1
        return n

    @staticmethod
    def _save_profile(url: str, channel_id: int, country: str,
                      rival_product_id: int | None, total: int | None,
                      fetched: int, avg: float | None, is_hot: bool) -> None:
        with db.tx() as conn:
            conn.execute("""
                INSERT INTO review_profile(rival_product_id,channel_id,country_code,
                    product_url,total_reviews,fetched_reviews,avg_rating,is_hot,
                    last_fetched)
                VALUES(?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(product_url) DO UPDATE SET
                  total_reviews=excluded.total_reviews,
                  fetched_reviews=excluded.fetched_reviews,
                  avg_rating=excluded.avg_rating, is_hot=excluded.is_hot,
                  last_fetched=datetime('now')
            """, (rival_product_id, channel_id, country, url, total, fetched,
                  avg, 1 if is_hot else 0))
