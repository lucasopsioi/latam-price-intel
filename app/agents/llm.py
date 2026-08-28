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


class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.provider = cfg.get("provider", "minimax")
        self.base_url = (db.get_setting("llm_base_url")
                         or cfg.get("minimax_base_url")
                         or "https://api.minimaxi.com/v1").rstrip("/")
        self.model = db.get_setting("llm_model") or cfg.get("minimax_model", "MiniMax-Text-01")
        self.timeout = cfg.get("timeout", 180)
        self.temperature = cfg.get("temperature", 0.2)
        self.max_tokens = cfg.get("max_tokens", 8192)
        self._key = db.get_setting("minimax_api_key", "")
        self.total_tokens = 0

    def available(self) -> bool:
        return bool(self._key)

    def chat(self, prompt: str, system: str = "", *,
             temperature: float | None = None) -> tuple[str, int]:
        """返回 (回复文本, token数)。失败返回 ("", 0)，不抛异常 ——
        单个 Agent 的 LLM 失败不能让整条流水线崩掉。"""
        if not self.available():
            return "", 0
        url = f"{self.base_url}/text/chatcompletion_v2"
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


def as_text(value) -> str | None:
    """模型填的文本字段：把"null"字样归一成真正的 None。"""
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in NULLISH else s
