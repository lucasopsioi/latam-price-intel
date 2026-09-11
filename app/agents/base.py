# -*- coding: utf-8 -*-
"""Agent 基类 —— 核心是【留痕】（用户硬性要求）。

每个 Agent 的每一步都写进 agent_step：
  送进模型的提示词、模型原始回复、解析结果、这一步的结论与理由。
出错时能完整复盘"这个价格为什么被剔掉"、"这条新品为什么被判成重要"，
而不是只看到一个结论。

设计约定：
  1. Agent 不直接抛异常打断流水线 —— 单步失败标 degraded 继续走
  2. 所有 LLM 返回一律过 llm.as_dicts/as_dict 归一化，不信任结构
  3. 能用规则判死的绝不调模型（省 token，且规则可复现）
"""
from __future__ import annotations

import json
import logging
import time

from .. import db
from .llm import LLMClient

log = logging.getLogger("agent")

# ★ 进度回传钩子。
#   起因：VOC 分析实际跑了 20 分钟、每 5 秒推进一个产品，界面上却始终显示
#   "启动中…" —— 因为只有 API 层在任务开始时写过一次 progress。
#   看起来完全一样的两种状态（真卡死 / 正常跑着）不能长得一样，
#   否则唯一的处理办法就是等，等到最后才知道白等。
#   每个 Agent 本来就步步 log_step，把这条线接到任务进度上，
#   11 个 Agent 一次性都有了实时进度，以后新增的也自动有。
_progress_sink = None


def set_progress_sink(fn) -> None:
    """fn(step_name: str) -> None；传 None 解除。由 API 任务层设置。"""
    global _progress_sink
    _progress_sink = fn


class BaseAgent:
    name = "base"
    role = "base"
    description = ""

    def __init__(self, llm: LLMClient | None = None, cfg: dict | None = None,
                 parent_run_id: int | None = None, plan_id: int | None = None):
        self.cfg = cfg or {}
        self.llm = llm
        self.parent_run_id = parent_run_id
        self.plan_id = plan_id
        self.run_id: int | None = None
        self._step_no = 0
        self.tokens = 0

    # ------------------------------------------------ 运行生命周期

    def start(self, input_summary: str = "") -> int:
        with db.tx() as conn:
            cur = conn.execute("""
                INSERT INTO agent_run(agent_name,role,parent_run_id,plan_id,input_summary)
                VALUES(?,?,?,?,?)
            """, (self.name, self.role, self.parent_run_id, self.plan_id,
                  input_summary[:500]))
            self.run_id = cur.lastrowid
        self._step_no = 0
        return self.run_id

    def finish(self, status: str = "ok", output_summary: str = "",
               items_in: int = 0, items_out: int = 0, error: str = "") -> None:
        if self.run_id is None:
            return
        with db.tx() as conn:
            conn.execute("""
                UPDATE agent_run SET finished_at=datetime('now'), status=?,
                  output_summary=?, items_in=?, items_out=?, tokens_used=?, error=?
                WHERE id=?
            """, (status, output_summary[:1000], items_in, items_out,
                  self.tokens, error[:500] or None, self.run_id))

    # ------------------------------------------------ 留痕

    def log_step(self, step_name: str, *, input_ref: str = "", prompt: str = "",
                 raw: str = "", parsed=None, decision: str = "", reason: str = "",
                 tokens: int = 0, duration_ms: int = 0, status: str = "ok") -> None:
        """每一步都记。prompt 截断存档（不含密钥——密钥在 header 里，从不进 prompt）。"""
        if self.run_id is None:
            return
        self._step_no += 1
        self.tokens += tokens
        if _progress_sink is not None:
            try:
                _progress_sink(f"{self.role}｜{step_name[:60]}（第 {self._step_no} 步）")
            except Exception:  # noqa: BLE001
                pass        # 进度是附带品，绝不能因为它把 Agent 干挂了
        try:
            parsed_str = (json.dumps(parsed, ensure_ascii=False)[:4000]
                          if parsed is not None else None)
        except Exception:  # noqa: BLE001
            parsed_str = str(parsed)[:4000]
        with db.tx() as conn:
            conn.execute("""
                INSERT INTO agent_step(run_id,step_no,step_name,input_ref,prompt_digest,
                    raw_response,parsed_result,decision,reason,tokens,duration_ms,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (self.run_id, self._step_no, step_name[:80], input_ref[:200],
                  (prompt or "")[:4000], (raw or "")[:4000], parsed_str,
                  decision[:40], reason[:500], tokens, duration_ms, status))

    # ------------------------------------------------ LLM 调用（自动留痕）

    def ask_json(self, step_name: str, prompt: str, *, system: str = "",
                 input_ref: str = "", default=None):
        """调模型并自动留痕。返回解析后的结果。"""
        if not self.llm or not self.llm.available():
            self.log_step(step_name, input_ref=input_ref, decision="skipped",
                          reason="未配置 API Key，跳过该步（走规则兜底）",
                          status="degraded")
            return default
        t0 = time.time()
        parsed, raw, tokens = self.llm.chat_json(prompt, system, default=default)
        ok = parsed is not None and parsed != default
        self.log_step(step_name, input_ref=input_ref, prompt=prompt, raw=raw,
                      parsed=parsed, decision="ok" if ok else "parse_failed",
                      reason="" if ok else "模型返回无法解析为 JSON，已回退默认值",
                      tokens=tokens, duration_ms=int((time.time() - t0) * 1000),
                      status="ok" if ok else "degraded")
        return parsed if parsed is not None else default

    # ------------------------------------------------ 子类实现

    def run(self, *a, **kw):
        raise NotImplementedError


def summarize_for_human(rows: list[dict], keys: list[str], limit: int = 12) -> str:
    """把一批记录压成给模型看的紧凑文本。控制 token 消耗。"""
    out = []
    for r in rows[:limit]:
        parts = [f"{k}={r.get(k)}" for k in keys if r.get(k) not in (None, "")]
        out.append("; ".join(parts))
    if len(rows) > limit:
        out.append(f"…（另有 {len(rows) - limit} 条同类，已省略）")
    return "\n".join(out)
