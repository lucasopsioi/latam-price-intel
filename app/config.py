# -*- coding: utf-8 -*-
"""配置加载：YAML 静态配置 + 数据库里的动态设置（含 API Key）。

密钥纪律（工作区铁律）：
  - Key 只在内存与数据库里流转，任何日志/接口返回一律走 mask()
  - 不打印、不索取、不写进 YAML（YAML 会进 git / 会被随手打开）
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
EXPORT_DIR = ROOT / "exports"
PROFILE_DIR = DATA_DIR / "browser_profiles"

for _d in (DATA_DIR, LOG_DIR, EXPORT_DIR, PROFILE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "intel.db"


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    # 显式 encoding：本机 ANSI=cp936，不指定会读崩含中文的 YAML
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_channels() -> dict:
    return _load_yaml("channels.yaml")


def load_brands() -> dict:
    return _load_yaml("brands.yaml")


def load_runtime() -> dict:
    """运行参数（可被设置页覆盖）"""
    base = {
        "scrape": {
            # ★ Selenium(undetected) 为主：实测比 Playwright 更难被风控识别
            #   （Liverpool 用 Playwright 重试两次仍被拦，Selenium 一次通过）。
            #   慢是可以接受的 —— 目标是 24 小时内跑完且不被封，不是跑得快。
            "engine": "selenium",
            "fallback_engine": "playwright",
            "headless": True,
            # 抓取间隔放宽：慢一点换不被封
            "min_delay": 4.0,
            "max_delay": 11.0,
            "request_timeout": 60,
            "device_pool": "mixed",          # phone | tablet | mixed
            "rotate_device_every": 8,
            "persist_cookies": True,
            "block_cooldown": 120,
            # 用户明确表示不在乎耗时，只要 24 小时内完成 → 取全量
            "max_products_per_query": 40,
            # 单个「渠道×品牌」单元最多花多少秒在详情页上。到点就停，
            # 剩下的用列表页数据入库（价格仍有），并如实记录跳过数。
            # 没有这个闸，一个被反复拦截的渠道能吃掉半小时，把后面全挤掉。
            "detail_budget_seconds": 420,
            # ★ 并行度 = 同时抓几个【不同域名】的站点。
            #   同一域名永远只有一个浏览器在跑（组内串行），
            #   因为同域并发正是反爬要抓的特征；跨域并行则互不影响
            #   （Liverpool 与 Falabella 是两套独立风控）。
            #   每个 worker 一个 Chrome 实例，约 300~500MB 内存。
            "parallel_workers": 5,
            # JS 挑战页（Imperva/Cloudflare 的 "Un momento…"）最多守多久。
            # 这类挑战由浏览器执行 JS 后自动跳转，真实用户也是等几秒 ——
            # 给足时间就能过，之前当成拦截放弃是白白损失。
            # 人机验证（CAPTCHA）不适用：那个不硬闯，直接跳过。
            "challenge_wait_seconds": 25,
            "max_pages_per_channel": 3,
            "proxy": "",
            "concurrency": 1,                # Selenium 主力时建议串行，最不像机器人
        },
        "voc": {
            # 消费者评论抓取（用户要求：主销款评论全抓，做 Agent 分析）
            "enabled": True,
            "max_reviews_per_product": 300,
            "max_seconds_per_product": 240,
            # 阶段闸：没有这两个，VOC 可以吃掉整整一天把后面的阶段挤没
            "max_products_per_run": 150,
            "stage_budget_seconds": 21600,     # 6 小时

            "min_reviews_to_analyze": 5,
            # 评论数超过这个值的产品，单独做"为什么评论这么多"的归因分析
            "hot_product_review_threshold": 100,
        },
        "schedule": {
            "daily_time": "07:30",
            "enabled": False,
        },
        "agents": {
            "provider": "minimax",
            "minimax_base_url": "https://api.minimaxi.com/v1",
            "minimax_model": "MiniMax-Text-01",
            "temperature": 0.2,
            "max_tokens": 8192,
            "timeout": 180,
            "enable_intel": True,
            "enable_price_audit": True,
            "enable_cleaner": True,
            "enable_spec_filler": True,
        },
        "matching": {
            # 竞品判定阈值（见 app/matching/matcher.py）
            "spec_min": 0.55,
            "price_band_pct": 0.30,   # 价格带 ±30%
            "total_min": 0.60,
            "top_n_per_country": 8,
        },
        "telegram": {
            "enabled": False,
            "daily_time": "08:30",
            "include_sections": ["new_products", "price_moves", "promos"],
        },
    }
    user = _load_yaml("runtime.yaml")
    return _deep_merge(base, user)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def save_runtime(cfg: dict) -> None:
    """写回 runtime.yaml（UTF-8，允许中文；非 .json/.ps1/.cmd 故无 cp936 铁律限制）"""
    path = CONFIG_DIR / "runtime.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def mask(secret: str | None) -> str:
    """密钥掩码：只露头尾，中间固定长度星号（不泄露真实长度）"""
    if not secret:
        return ""
    s = str(secret)
    if len(s) <= 8:
        return "*" * 8
    return f"{s[:4]}{'*' * 8}{s[-4:]}"


# 环境自愈：本机 cp936，强制 Python 走 UTF-8，否则中文输出会崩
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
