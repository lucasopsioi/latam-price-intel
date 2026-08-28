# -*- coding: utf-8 -*-
"""规格相似度计算 —— 回答"这两台机器竞争力是不是大差不差"。

★ 核心思路：不比字符串，比"档位"和"相对差异"。
  Snapdragon 8 Gen 3 与 A17 Pro 字符串毫无相似，实际是同档旗舰；
  Snapdragon 685 与 Snapdragon 8 Gen 3 字符串很像，实际差三档。
  所以芯片先映射到 1~10 档（config/brands.yaml → chipset_tiers），比档位差。

★ 缺失值不当成 0：
  友商规格常常抓不全。把缺失当 0 会让"未知芯片"变成"最差芯片"，
  于是所有规格不全的机器都被判成不是竞品 —— 这是静默的系统性偏差。
  正确做法：缺失的维度**退出计算**并按剩余权重归一化，同时把
  "有多少维度参与了比较"作为置信度一起报出来。
"""
from __future__ import annotations

import re

from ..config import load_brands

_TIER_CACHE: dict[str, int] | None = None


def _tier_table() -> dict[str, int]:
    """芯片名（小写归一）→ 档位"""
    global _TIER_CACHE
    if _TIER_CACHE is None:
        table: dict[str, int] = {}
        for tier, names in (load_brands().get("chipset_tiers") or {}).items():
            for n in (names or []):
                table[_norm_chip(n)] = int(tier)
        _TIER_CACHE = table
    return _TIER_CACHE


def _norm_chip(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"\b(qualcomm|mediatek|apple|acme|hisilicon|samsung|intel|amd)\b", " ", s)
    s = re.sub(r"\b(processor|chipset|cpu|soc|bionic)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def chipset_tier(name: str | None) -> int | None:
    """把芯片名映射到 1~10 档。认不出返回 None（≠ 最低档）。"""
    if not name:
        return None
    key = _norm_chip(name)
    if not key:
        return None
    table = _tier_table()
    if key in table:
        return table[key]
    # 模糊匹配：页面上常写成 "Snapdragon 8 Gen 3 Mobile Platform"
    best, best_len = None, 0
    for k, tier in table.items():
        if len(k) >= 5 and (k in key or key in k) and len(k) > best_len:
            best, best_len = tier, len(k)
    return best


def _ratio_score(a: float | None, b: float | None, tolerance: float = 0.35) -> float | None:
    """两个数值的接近度 0~1。相对差异超过 tolerance 记 0。"""
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    diff = abs(a - b) / max(a, b)
    return max(0.0, 1.0 - diff / tolerance) if diff < tolerance else 0.0


def _tier_score(a: int | None, b: int | None) -> float | None:
    """档位差 0→1.0, 1→0.75, 2→0.45, 3→0.15, ≥4→0"""
    if a is None or b is None:
        return None
    return {0: 1.0, 1: 0.75, 2: 0.45, 3: 0.15}.get(abs(a - b), 0.0)


# 各维度的比较方式
_DIMENSIONS = {
    "chipset_tier": ("tier", None),
    "ram_gb": ("ratio", 0.5),        # 8G vs 12G 仍算接近
    "rom_gb": ("ratio", 0.6),        # 256 vs 512 算接近（同一档产品常见）
    "screen_size": ("ratio", 0.12),  # 屏幕对体验影响大，容忍度小
    "battery_mah": ("ratio", 0.25),
    "camera_main_mp": ("ratio", 0.6),
}


def spec_similarity(mine: dict, rival: dict, category: str) -> dict:
    """返回 {score, confidence, dims, missing}。

    mine / rival 用同一套键：chipset, ram_gb, rom_gb, screen_size,
                             battery_mah, camera_main_mp
    """
    weights = ((load_brands().get("categories") or {}).get(category) or {}).get(
        "spec_weights") or {}
    if not weights:
        weights = {"chipset_tier": 0.35, "ram_gb": 0.2, "rom_gb": 0.2, "screen_size": 0.25}

    mine = dict(mine or {})
    rival = dict(rival or {})
    mine["chipset_tier"] = chipset_tier(mine.get("chipset"))
    rival["chipset_tier"] = chipset_tier(rival.get("chipset"))

    dims, missing = {}, []
    total_w, got_w, acc = 0.0, 0.0, 0.0

    for key, w in weights.items():
        if key == "extra":          # 音频等无标准化数值规格的产业，见下面兜底
            continue
        total_w += w
        kind, tol = _DIMENSIONS.get(key, ("ratio", 0.35))
        if kind == "tier":
            s = _tier_score(mine.get(key), rival.get(key))
        else:
            s = _ratio_score(_num(mine.get(key)), _num(rival.get(key)), tol)
        if s is None:
            missing.append(key)     # ★ 缺失退出计算，不记 0
            continue
        dims[key] = round(s, 3)
        got_w += w
        acc += w * s

    if got_w <= 0:
        # 一个维度都比不了（音频类常见）→ 退回价格带判定，规格分给中性值
        return {"score": 0.5, "confidence": 0.0, "dims": {},
                "missing": missing,
                "note": "无可比规格维度，规格分取中性值 0.5，判定主要依赖价格带与可得性"}

    score = acc / got_w                    # 按实际参与的权重归一化
    confidence = got_w / total_w if total_w else 0.0
    return {"score": round(score, 3), "confidence": round(confidence, 3),
            "dims": dims, "missing": missing,
            "note": (f"{len(dims)} 个维度参与比较，缺失 {missing}" if missing
                     else f"{len(dims)} 个维度全部可比")}


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None
