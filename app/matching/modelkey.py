# -*- coding: utf-8 -*-
"""型号键：把「我方登记名（中文口径）」与「商城标题（西语）」归到同一个键。

═══ 为什么需要它 ═══

原来 `_our_price` 用整个 marketing_name 去 `title LIKE '%…%'`。实测结果：
  我方登记  AceBook D 16 16吋 2024 12th Gen Core
  商城标题  ACME Laptop Acebook D16 16" FHD | Intel Core i5-13420H | 16GB RAM
永远匹配不上，差在三处：**「吋」是中文单位**、**D 16 与 D16 空格不同**、
**"2024 12th Gen Core" 商城根本不写**。

后果是取不到我方价 ⇒ 该产品在 matcher 里直接 return 0 ⇒ 没有任何竞品对照。
实测：电脑 0/11、平板 0/16 完全没有对照，**而两边的货都在库里**。

═══ 为什么返回的是一组键而不是一个 ═══

规范化存在真正的歧义，猜哪一种"对"是徒劳的：
  "AceBook D 16"        → 粘成 d16 才对
  "Slate 12 X 12-inch" → 粘成 x12 就错了（12 是屏幕尺寸）
实测：用一条固定规则去粘，修好了 AceBook 却弄坏了 Slate Tab，
商城的键从 100 个碎成 151 个。⇒ 两边各出一组候选键，**有交集即同款**，
歧义交给数据自己消解。

═══ 变体词必须进键 ═══

第一版把 Vega 70 / Vega 70 Pro / Vega 70 Ultra 全归成 vega-70。
三者价位差一大截，匹配上去算出的价差是彻底错的 —— 而且不报错。
所以 Pro/Ultra/Max/SE… 是型号身份的一部分，必须保留。

═══ 命中率（2026-08-17 实测，114 个我方型号）═══

  旧的 LIKE 整串      55/114 = 48%
  本模块             75/114 = 66%
  理论上限           82/114 = 72%   ← 其余 32 个拉美商城根本没铺货

  按品类：音频 100% / 平板 94% / 电脑 55%（原 0%）/ 穿戴 57% / 手机 47%
"""
from __future__ import annotations

import re

# Acme产品线。★ 顺序重要：长的必须在前，否则 slate 会被 astra 抢先匹配
SERIES = [
    "acebook", "slate", "aceview", "acestation", "astra",
    "nimbus", "vega", "enjoy", "maimang",
    "sonicbuds", "sonicclip", "soniclace", "sonicarc", "freego",
    "watch", "band", "talkband", "eyewear", "sound", "vision", "scale",
]

# 变体词 = 型号身份的一部分，不是噪声
VARIANT = {"pro", "max", "ultra", "plus", "se", "lite", "gt", "fit",
           "kids", "runner", "ultimate", "x", "s", "e", "i", "d", "c", "buds"}

_ACC = str.maketrans("áéíóúüñ", "aeiouun")
_UNIT = re.compile(r"(吋|英寸|寸|pulgadas?|inch(es)?)", re.I)
# 配置串：不剥的话 512GB 会被当成型号号码，
# 把同一个 nimbus 14 拆成 nimbus-14 / nimbus-14-512gb / nimbus-14-512gb-12gb
_CONFIG = re.compile(r"\b\d+\s*(gb|tb|mb|mp|mah|hz|w|nm)\b", re.I)
_NOISE = re.compile(
    r"\b("
    r"acme|tablet|laptop|celular|smartphone|reloj|audifonos|auriculares|"
    r"notebook|portatil|computadora|smart|"
    r"gb|tb|ram|rom|ssd|fhd|oled|amoled|hd|wifi|lte|5g|4g|dual|sim|nfc|"
    r"intel|amd|core|ryzen|snapdragon|kirin|celeron|"
    r"i[3579]|u[3579]|"
    r"nuevo|new|original|garantia|envio|gratis|codigo|precio|edition|"
    r"teclado|lapiz|mouse|pen|funda|case|cargador|correa|"
    r"negro|blanco|gris|azul|verde|dorado|plata|rosa|violeta|morado|blanca|"
    r"black|white|gray|grey|blue|green|gold|silver|pink|violet|space|titanium|"
    r"th|gen|q[1-4]|"
    r"20[12][0-9]"
    r")\b", re.I)


def _base(s: str) -> str:
    s = (s or "").lower().translate(_ACC)
    s = _UNIT.sub(" ", s)
    s = re.sub(r"[|/\\,+\"'’”“()\[\]]", " ", s)
    s = re.sub(r"[-_]", " ", s)
    s = _CONFIG.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _key_from(series: str, rest: str) -> str:
    parts, seen_num = [], False
    for tok in rest.split():
        if re.fullmatch(r"\d+(\.\d+)?", tok):
            if seen_num and parts:
                break                       # 第二个纯数字多半是屏幕尺寸/配置
            parts.append(tok); seen_num = True
        elif re.fullmatch(r"\d+(\.\d+)?[a-z]{1,2}", tok):        # 13i / 12s
            parts.append(tok); seen_num = True
        elif re.fullmatch(r"[a-z]\d+(\.\d+)?[a-z]?", tok):       # d16 / y61 / x1
            parts.append(tok); seen_num = True
        elif tok in VARIANT:
            parts.append(tok)
        elif len(parts) and not re.fullmatch(r"[a-z]{1,4}", tok):
            break
        elif not parts and re.fullmatch(r"[a-z]{1,4}", tok):
            parts.append(tok)               # acebook d / watch gt
        else:
            break
        if len(parts) >= 4:
            break
    return series + ("-" + "-".join(parts) if parts else "")


def model_keys(raw: str | None) -> set[str]:
    """返回一组候选型号键。两个名字的键集合**有交集**即判为同款。"""
    t0 = _base(raw or "")
    if not t0:
        return set()
    hit = None
    for s in SERIES:
        i = t0.find(s)
        if i != -1 and (hit is None or i < hit[1]):
            hit = (s, i)
    if not hit:
        return set()
    series = hit[0]

    keys = set()
    # 两种规范化各出一个键：原样（d 16）与粘合（d16）
    for variant in (t0, re.sub(r"\b([a-z])\s+(\d)", r"\1\2", t0)):
        j = variant.find(series)
        if j == -1:
            continue
        rest = _NOISE.sub(" ", variant[j + len(series):])
        rest = re.sub(r"\s+", " ", rest).strip()
        k = _key_from(series, rest)
        if k:
            keys.add(k)
    return keys


def same_model(a: str | None, b: str | None) -> bool:
    ka, kb = model_keys(a), model_keys(b)
    return bool(ka and kb and (ka & kb))
