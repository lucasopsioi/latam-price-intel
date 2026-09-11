# -*- coding: utf-8 -*-
"""LLM 客户端（MiniMax 为主，兼容 OpenAI 格式的其它服务）。

密钥纪律：Key 只从数据库 setting 表取（DPAPI 解密后在内存里），
         绝不写日志、绝不进 prompt 存档、绝不回传给前端。
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from .. import db

log = logging.getLogger("llm")


# 供应商注册表。两家都是 OpenAI 风格（Bearer + choices[0].message.content），
# 只有接口路径不同。加新供应商 = 加一行，不改 chat()。
PROVIDERS = {
    "minimax": {
        "base": "https://api.minimaxi.com/v1",
        "path": "/text/chatcompletion_v2",
        "key_setting": "minimax_api_key",
        "model_setting": "llm_model",
        "model_default": "MiniMax-Text-01",
        "base_setting": "llm_base_url",
    },
    "deepseek": {
        "base": "https://api.deepseek.com",
        "path": "/chat/completions",
        "key_setting": "deepseek_api_key",
        "model_setting": "llm_model_deepseek",
        "model_default": "deepseek-chat",     # 官方别名恒指最新 chat 模型；
                                              # 要点名 V4 Pro 就在设置里填它的模型 id
        "base_setting": "llm_base_url_deepseek",
    },
}


class LLMClient:
    def __init__(self, cfg: dict, provider: str | None = None):
        self.cfg = cfg or {}
        self.provider = provider or cfg.get("provider", "minimax")
        pv = PROVIDERS.get(self.provider) or PROVIDERS["minimax"]
        self.base_url = (db.get_setting(pv["base_setting"])
                         or cfg.get(f"{self.provider}_base_url")
                         or pv["base"]).rstrip("/")
        self._path = pv["path"]
        self.model = (db.get_setting(pv["model_setting"])
                      or cfg.get(f"{self.provider}_model", pv["model_default"]))
        self.timeout = cfg.get("timeout", 180)
        self.temperature = cfg.get("temperature", 0.2)
        self.max_tokens = cfg.get("max_tokens", 8192)
        self._key = db.get_setting(pv["key_setting"], "")
        self.total_tokens = 0

    def available(self) -> bool:
        return bool(self._key)

    def chat(self, prompt: str, system: str = "", *,
             temperature: float | None = None) -> tuple[str, int]:
        """返回 (回复文本, token数)。失败返回 ("", 0)，不抛异常 ——
        单个 Agent 的 LLM 失败不能让整条流水线崩掉。"""
        if not self.available():
            return "", 0
        url = f"{self.base_url}{self._path}"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model, "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens,
        }
        for attempt in range(2):
            try:
                r = httpx.post(url, json=payload, timeout=self.timeout,
                               headers={"Authorization": f"Bearer {self._key}",
                                        "Content-Type": "application/json"})
                if r.status_code != 200:
                    log.warning("LLM HTTP %s: %s", r.status_code, r.text[:200])
                    if r.status_code in (401, 403):
                        return "", 0          # Key 无效，重试没意义
                    time.sleep(2)
                    continue
                data = r.json()
                choices = data.get("choices") or []
                if not choices:
                    log.warning("LLM 返回无 choices: %s", str(data)[:200])
                    return "", 0
                text = (choices[0].get("message") or {}).get("content") or ""
                tokens = ((data.get("usage") or {}).get("total_tokens")) or 0
                self.total_tokens += tokens
                return text, tokens
            except Exception as e:  # noqa: BLE001
                log.warning("LLM 调用失败(第%d次): %s", attempt + 1, str(e)[:150])
                time.sleep(2)
        return "", 0

    def chat_json(self, prompt: str, system: str = "", default=None):
        """要求模型返回 JSON。返回 (解析结果, 原始回复, tokens)。
        模型经常在 JSON 外面裹 ```json 或加解释文字，这里用括号配平抠出来。"""
        raw, tokens = self.chat(prompt, system)
        if not raw:
            return default, "", tokens
        parsed = extract_json(raw)
        return (parsed if parsed is not None else default), raw, tokens


def report_llm(cfg: dict) -> "LLMClient":
    """出报告用的 LLM（2026-08-28 用户：报告 Agent 用 DeepSeek，
    搜索/研判类 Agent 继续用 MiniMax —— 写长中文报告吃写作能力，
    结构化研判吃的是便宜和快，两边要的不是同一种模型）。

    ★ 诚实降级：想用的供应商没配 Key 就回落 MiniMax，并在日志里写明 ——
      绝不静默换供应商让报告"看起来还是那个模型写的"。"""
    want = (cfg or {}).get("report_provider") or "deepseek"
    c = LLMClient(cfg, provider=want)
    if c.available():
        return c
    fallback = LLMClient(cfg)
    if want != fallback.provider:
        log.warning("报告供应商 %s 未配置 Key，回落 %s（在设置页填 %s 即自动启用）",
                    want, fallback.provider,
                    PROVIDERS.get(want, {}).get("key_setting", "?"))
    return fallback


def extract_json(text: str):
    """从模型回复里抠出 JSON。括号配平法，比正则可靠。"""
    if not text:
        return None
    t = text.strip()
    # 去掉 markdown 代码围栏
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = t.find(opener)
        if start < 0:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:  # noqa: BLE001
                        break
    return None


# ---------------------------------------------------------------- 归一化
# 模型返回的结构经常变形（该给数组给了对象、该给对象给了字符串）。
# 所有 Agent 拿到结果后一律先过这三个函数，绝不直接信任结构。

def as_dicts(value) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        for k in ("items", "results", "data", "list", "products"):
            if isinstance(value.get(k), list):
                return as_dicts(value[k])
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for v in value:
            if isinstance(v, dict):
                return v
    return {}


def as_strs(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and not isinstance(v, (dict, list))]
    if isinstance(value, dict):
        return as_strs(list(value.values()))
    return []


def as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ★ 提示词里写"不确定就填 null"，模型经常照做但写成**字符串** "null"/"N/A"。
#   落库后果和这条要求正好相反：`IS NOT NULL` 判定为真 ⇒ 这行看起来"有值"。
#   实测两处栽过：规格表 13 行拿字符串 "null" 去比芯片档位；
#   情报流建出一个叫 "null" 的 Honor 机型摆在看板上。
#   凡是**模型填的文本字段**都要过这道闸，别在各模块各写一份。
NULLISH = {"null", "none", "nul", "n/a", "na", "nil", "undefined",
           "unknown", "desconocido", "no especificado", "sin especificar",
           "-", "--", ""}


# ─────────────────────────────────────────────────────────────────────
# 批量问模型时的结果对齐（一次问 N 条，第 k 条的答案必须回到第 k 行）
#
# knowledge/lessons/llm-batch-result-alignment：实测 262 个批次里 25.6% 返回与
# 送入不符，序号是**模型自己写的**（1-based、丢条目后重编号都见过），
# `chunk[int(item["idx"])]` 照信不误 ⇒ 差评行拿到好评的译文、#9168 小米 18 Fold
# 配上 Apple Watch 的摘要。品牌核对拦不住（原文恰好提到 Apple）。
# 这一课 08-28 写下后只修了 VOC 一处；2026-09-04 全仓扫出 intel / price_audit /
# spec_filler / brandintel 四处同病 —— 所以做成**一个实现四处消费**，
# 不许再各写各的。
#
# 用法：
#   keys = [batch_key(r["id"]) for r in chunk]         # 进 prompt：f"{j}. (k={keys[j]}) …"
#   prompt 的 JSON 规格里加 K_SPEC，规则里加 K_RULE
#   for j, item in pick_by_key(parsed, keys)[0]: ...    # 定位只认核对码
# ─────────────────────────────────────────────────────────────────────
K_SPEC = '"k":"该条括号里的 k 值原样抄回"'
K_RULE = "★ k 必须一字不差抄回，用于核对序号没有错位；抄错的整条作废。"


def batch_key(ident) -> str:
    """每条的核对码：稳定标识的 md5 前 5 位。进 prompt，要求模型原样抄回。"""
    return db.row_hash(str(ident))[:5]


def pick_by_key(parsed, keys: list[str]) -> tuple[list[tuple[int, dict]], dict]:
    """按核对码把模型返回的条目对回送入的条目。**不用模型写的序号**。

    返回 ([(j, item), ...], {"accepted", "rejected", "dropped"})。
    rejected = 核对码抄错/抄不出/同一条回了两次；dropped = 模型没答的条目。
    调用方对 rejected/dropped 的正确处理是「保留原值、留痕」，不是猜。
    """
    keymap = {k: j for j, k in enumerate(keys)}
    seen: set[int] = set()
    out: list[tuple[int, dict]] = []
    rejected = 0
    for item in as_dicts(parsed):
        j = keymap.get(str(item.get("k") or "").strip().lower())
        if j is None or j in seen:
            rejected += 1
            continue
        seen.add(j)
        out.append((j, item))
    return out, {"accepted": len(seen), "rejected": rejected,
                 "dropped": len(keys) - len(seen)}


def as_text(value) -> str | None:
    """模型填的文本字段：把"null"字样归一成真正的 None。"""
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in NULLISH else s
