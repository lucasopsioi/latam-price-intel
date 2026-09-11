# -*- coding: utf-8 -*-
"""口碑维度表：把评论里说的"方面"收敛成可聚合、可横比的固定维度。

★ 为什么必须有这个文件：
  原来提示词里只是**建议**了 9 个维度、没有约束，模型自由发挥的结果是
  814 条评论里出现了 **43 种**不同的维度名。同一件事被拆成好几个标签：

      音质 / 音效 / 音量 / 隔音      ← 都是"声音"
      做工 / 质量 / 品质            ← 都是"做工"
      外观 / 设计 / 颜色            ← 都是"外观"
      存储 / 内存                   ← 都是"存储"

  后果不是"标签有点乱"，是**雷达图根本画不出来**：
  「音质」排第 9 名（22 次），但把四个同义标签合起来其实是第 5 名（35 次）。
  按错的排名去判断"友商强在哪"，结论会直接反过来。

★ 两个设计决定（都会影响结论，写在这里免得以后当成随手写的）：

  1. **维度分「产品」与「体验」两类**（kind）。
     价格/物流/售后/配件说的不是产品好坏，是**这家零售商**的服务。
     混进产品雷达图会得出"这款手机弱在物流"——那不是手机的问题。
     销售团队 要区分的正是这两件事：产品要改 vs 渠道要谈。

  2. **cats 只用来生成提示词菜单，不用来在入库时过滤**。
     它的作用是"别让模型在耳机上硬凑相机分"，属于引导。
     ★ 但**不能拿它当入库闸门** —— 第一版这么做了，实测当场吃掉 19 条真信号：
       森海塞尔耳机的「App 常崩溃、无法调均衡器」被判成"音频品类没有系统维度"
       而丢弃，那恰恰是最该报给 销售团队 的短板；笔记本的「摄像头」也被丢
       （笔记本当然有摄像头）。
     先验的品类-维度对应表**我写错了 2/21**，而错误的代价是静默丢数据。
     所以：菜单按品类给，入库只要是表里的 code 就收。

★ 同义词归并只做**确定**的合并。像「体验」「产品」「操作」这种
  说了等于没说的词，宁可丢掉也不硬塞进某个维度 —— 塞错比丢掉更贵。
"""
from __future__ import annotations

# code, 中文名, kind(product|experience), 适用品类(空=全品类)
ASPECTS: list[tuple[str, str, str, tuple[str, ...]]] = [
    # ---- 产品本体 ----
    ("battery",     "电池续航", "product", ("phone", "tablet", "wearable", "pc", "audio")),
    ("camera",      "相机",     "product", ("phone", "tablet", "pc")),
    ("screen",      "屏幕",     "product", ("phone", "tablet", "pc", "wearable")),
    ("performance", "性能",     "product", ()),
    ("heat",        "发热",     "product", ("phone", "tablet", "pc")),
    ("build",       "做工质量", "product", ()),
    ("software",    "系统软件", "product", ("phone", "tablet", "pc", "wearable", "audio")),
    ("sound",       "音质",     "product", ("audio", "phone", "tablet", "pc")),
    ("noise_cancel", "降噪",    "product", ("audio",)),
    ("comfort",     "佩戴舒适", "product", ("audio", "wearable")),
    ("connectivity", "连接稳定", "product", ("audio", "wearable", "pc", "phone")),
    ("design",      "外观设计", "product", ()),
    ("portability", "便携重量", "product", ("pc", "tablet", "audio", "wearable")),
    ("keyboard",    "键盘触控", "product", ("pc",)),
    ("storage",     "存储内存", "product", ("phone", "tablet", "pc")),
    ("health",      "健康监测", "product", ("wearable",)),
    # ---- 购买体验（是渠道的事，不是产品的事）----
    ("price",       "价格",     "experience", ()),
    ("service",     "售后",     "experience", ()),
    ("logistics",   "物流",     "experience", ()),
    ("packaging",   "包装配件", "experience", ()),
    ("authenticity", "正品与否", "experience", ()),
]

ASPECT_CODES = [a[0] for a in ASPECTS]
ASPECT_ZH = {a[0]: a[1] for a in ASPECTS}
ASPECT_KIND = {a[0]: a[2] for a in ASPECTS}
ASPECT_CATS = {a[0]: a[3] for a in ASPECTS}

# 实测跑出来的 43 种写法 → 固定维度。左边全部小写去空格后匹配。
# 只列**确定**的；拿不准的不进表，走 normalize 返回 None。
_SYNONYMS: dict[str, str] = {}


def _syn(code: str, *words: str) -> None:
    for w in words:
        _SYNONYMS[w.lower().replace(" ", "")] = code


_syn("battery", "电池", "续航", "电量", "battery", "bateria", "bateria", "duracion")
_syn("camera", "相机", "摄像头", "拍照", "camera", "camara", "câmera", "foto")
_syn("screen", "屏幕", "显示", "display", "pantalla", "tela", "画面")
_syn("performance", "性能", "速度", "流畅", "processador", "rendimiento", "cpu",
     "cpu插槽", "主板", "运行", "velocidad")
_syn("heat", "发热", "散热", "温度", "calentamiento", "aquecimento")
_syn("build", "做工", "质量", "品质", "材质", "耐用", "calidad", "qualidade",
     "acabado", "结实")
_syn("software", "系统", "软件", "应用", "ui", "android", "ios", "sistema",
     "操作", "系统软件")
_syn("sound", "音质", "音效", "音量", "隔音", "声音", "sonido", "som", "audio")
_syn("noise_cancel", "降噪", "主动降噪", "anc", "cancelacionderuido")
_syn("comfort", "舒适度", "佩戴", "舒适", "comodidad", "conforto", "ajuste")
_syn("connectivity", "连接", "蓝牙", "信号", "wifi", "conexion", "conexao",
     "bluetooth", "配对")
_syn("design", "外观", "设计", "颜色", "手感", "diseno", "design", "cor", "美观")
_syn("portability", "便携性", "便携", "重量", "轻便", "peso", "portabilidad")
_syn("keyboard", "键盘", "触控板", "按键", "teclado", "trackpad")
_syn("storage", "存储", "内存", "容量", "ram", "almacenamiento", "memoria")
_syn("health", "健康监测", "心率", "血氧", "计步", "睡眠", "salud")
_syn("price", "价格", "性价比", "便宜", "贵", "precio", "preco", "价位")
_syn("service", "售后", "客服", "保修", "维修", "garantia", "servicio", "soporte")
_syn("logistics", "物流", "配送", "发货", "快递", "envio", "entrega", "送货")
_syn("packaging", "包装", "配件", "赠品", "充电器", "embalaje", "caja", "accesorios")
_syn("authenticity", "正品", "假货", "翻新", "original", "falso", "generico")

# 说了等于没说的词：明确丢弃，不要硬塞。
_TOO_VAGUE = {"产品", "体验", "整体", "其他", "综合", "general", "producto", "todo"}


def normalize(raw: str) -> str | None:
    """把模型写的维度名归一成固定 code。归不了就返回 None（丢掉，不猜）。"""
    if not raw:
        return None
    key = str(raw).strip().lower().replace(" ", "")
    if not key or key in _TOO_VAGUE:
        return None
    if key in ASPECT_CODES:
        return key
    return _SYNONYMS.get(key)


def for_category(category_code: str | None) -> list[str]:
    """某个品类下**有意义**的维度。品类未知就给全部。"""
    if not category_code:
        return list(ASPECT_CODES)
    return [c for c in ASPECT_CODES
            if not ASPECT_CATS[c] or category_code in ASPECT_CATS[c]]


def prompt_menu(category_code: str | None = None) -> str:
    """给提示词用的维度清单 —— 让模型**从固定表里选**，而不是自由发挥。"""
    codes = for_category(category_code)
    return "、".join(f"{c}({ASPECT_ZH[c]})" for c in codes)
