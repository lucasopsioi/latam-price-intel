# -*- coding: utf-8 -*-
"""知识库 RAG —— 把 Obsidian vault 变成对话 Agent 能查的记忆。

用户要求（2026-08-11）："里面可以用 RAG 策略，向量检索" + "结合 Obsidian"

知识库在 D:\\workspace\\拉美竞品情报知识库\\（Obsidian vault），
里面沉淀着：渠道 URL 破解记录、卖家身份判据、数据正确性陷阱、
多 Agent 审查发现、每日日志……这些是**数据库里没有的知识**。

问"Coppel 为什么串味"、"Sears 的搜索参数是什么"、"卖家怎么判自营"，
答案在这些 md 里，不在 price_obs 表里。

★ 为什么是**混合检索**而不是纯向量：
  这个知识库里全是专有名词 —— Coppel、`_TRAILING`、WinError 183、
  SKU 型号、`?query=`。向量擅长语义相似，但对精确专名反而不如关键词：
  问「Coppel 为什么串味」，向量可能召回一篇讲 Alkosto 的相似段落
  （两者症状确实像，但用户问的是 Coppel）。
  所以两路都跑：**向量管语义、BM25 管专名**，用 RRF 融合排序。

★ 向量不可用时（没配 Key / 接口失败）自动退化成纯 BM25 ——
  知识库不大，关键词检索本身效果就不错，不该因为 embedding 挂了就整个不能用。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import struct
import sys
from collections import Counter
from pathlib import Path

from . import config, db

log = logging.getLogger("rag")

# 知识库位置：环境变量优先，与 archive.py 的 LATAM_ARCHIVE_DIR 同一套约定。
# ★ Windows 默认值原样保留 —— 改掉的话本机会突然找不到已有的 vault，
#   而 reindex() 只会返回 {"error": ...}，界面上表现为"RAG 忽然查不到东西了"。
_VAULT_DEFAULT = (Path(r"D:\workspace\拉美竞品情报知识库") if sys.platform == "win32"
                  else config.ROOT.parent / "拉美竞品情报知识库")
VAULT = Path(os.environ.get("LATAM_VAULT_DIR") or _VAULT_DEFAULT)
CHUNK_CHARS = 900          # 每块字符数：够放完整一节，又不撑爆上下文
CHUNK_OVERLAP = 150        # 重叠，避免答案正好被切在边界上
EMBED_MODEL = "embo-01"    # MiniMax 的 embedding 模型


# ---------------------------------------------------------------- 切块

def _split_markdown(text: str, path: str) -> list[dict]:
    """按标题切块 —— md 的章节天然是语义单元，比定长切分保真得多。"""
    chunks, heading, buf = [], "", []

    def flush():
        body = "\n".join(buf).strip()
        if len(body) < 40:
            return
        # 太长的节再按字符切，带重叠
        if len(body) <= CHUNK_CHARS:
            chunks.append({"heading": heading, "text": body})
            return
        i = 0
        while i < len(body):
            piece = body[i:i + CHUNK_CHARS]
            if len(piece) >= 40:
                chunks.append({"heading": heading, "text": piece})
            i += CHUNK_CHARS - CHUNK_OVERLAP

    for line in (text or "").split("\n"):
        if re.match(r"^#{1,4}\s+", line):
            flush()
            buf = []
            heading = re.sub(r"^#+\s+", "", line).strip()
        else:
            buf.append(line)
    flush()
    return chunks


# ---------------------------------------------------------------- 分词（中英混合）

_TOKEN = re.compile(r"[a-z0-9_\-./]+", re.I)


def _tokens(s: str) -> list[str]:
    """中英混合分词：英文/数字按词，中文按 2-gram。

    不引第三方分词器 —— 知识库不大，2-gram 对中文检索够用，
    而且专有名词（Coppel、WinError）本来就是英文，走英文分支。
    """
    s = (s or "").lower()
    out = _TOKEN.findall(s)
    cn = re.sub(r"[^\u4e00-\u9fff]+", " ", s)
    for seg in cn.split():
        out.extend(seg[i:i + 2] for i in range(max(len(seg) - 1, 1)))
    return out


# ---------------------------------------------------------------- 索引

def _embed(texts: list[str], llm) -> list[list[float]] | None:
    """调 MiniMax embedding。失败返回 None —— 上层退化成纯 BM25。"""
    if not (llm and llm.available()):
        return None
    try:
        import httpx
        key = getattr(llm, "_key", "") or ""
        if not key:
            return None
        r = httpx.post(
            f"{llm.base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": EMBED_MODEL, "texts": texts, "type": "db"},
            timeout=90)
        if r.status_code != 200:
            log.warning("embedding HTTP %s：%s", r.status_code, r.text[:160])
            return None
        j = r.json()
        vecs = j.get("vectors") or j.get("data") or []
        if vecs and isinstance(vecs[0], dict):        # OpenAI 风格兜底
            vecs = [v.get("embedding") for v in vecs]
        return vecs if len(vecs) == len(texts) else None
    except Exception as e:  # noqa: BLE001
        log.warning("embedding 调用失败：%s", str(e)[:120])
        return None


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def reindex(llm=None, vault: Path | None = None) -> dict:
    """重建知识库索引。增量：文件没变就跳过。"""
    root = Path(vault or VAULT)
    if not root.exists():
        return {"error": f"知识库不存在：{root}"}

    files = sorted(root.rglob("*.md"))
    stats = {"files": 0, "chunks": 0, "embedded": 0, "skipped": 0, "vault": str(root)}
    pending_texts, pending_ids = [], []

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        rel = str(f.relative_to(root))
        sig = str(hash(text))
        old = db.q1("SELECT file_sig FROM kb_chunk WHERE path=? LIMIT 1", (rel,))
        if old and old["file_sig"] == sig:
            stats["skipped"] += 1
            continue

        with db.tx() as conn:
            conn.execute("DELETE FROM kb_chunk WHERE path=?", (rel,))
            for ch in _split_markdown(text, rel):
                cur = conn.execute(
                    """INSERT INTO kb_chunk(path, title, heading, text, file_sig)
                       VALUES(?,?,?,?,?)""",
                    (rel, f.stem, ch["heading"], ch["text"], sig))
                pending_ids.append(cur.lastrowid)
                pending_texts.append(f"{f.stem} {ch['heading']}\n{ch['text']}"[:1800])
                stats["chunks"] += 1
        stats["files"] += 1

    # 批量生成向量（每批 16 条，控制单次请求体积）
    for i in range(0, len(pending_texts), 16):
        batch = pending_texts[i:i + 16]
        vecs = _embed(batch, llm)
        if not vecs:
            break                     # 向量不可用就算了，BM25 照样能查
        with db.tx() as conn:
            for cid, v in zip(pending_ids[i:i + 16], vecs):
                conn.execute("UPDATE kb_chunk SET embedding=? WHERE id=?",
                             (_pack(v), cid))
                stats["embedded"] += 1
    return stats


# ---------------------------------------------------------------- 检索

def _bm25(query: str, rows: list[dict], k: int) -> list[tuple[int, float]]:
    """BM25 关键词检索 —— 专有名词靠它。"""
    q = _tokens(query)
    if not q:
        return []
    docs = [_tokens(f"{r['title']} {r['heading']} {r['text']}") for r in rows]
    n = len(docs)
    avg = sum(len(d) for d in docs) / max(n, 1)
    df = Counter()
    for d in docs:
        df.update(set(d))
    k1, b = 1.5, 0.75
    scored = []
    for i, d in enumerate(docs):
        tf = Counter(d)
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (
                tf[term] + k1 * (1 - b + b * len(d) / max(avg, 1)))
        if s > 0:
            scored.append((i, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def search(query: str, k: int = 5, llm=None) -> list[dict]:
    """混合检索：向量 + BM25，用 RRF 融合。

    ★ RRF（Reciprocal Rank Fusion）而不是分数加权：
      两路的分数量纲完全不同（余弦 0~1 vs BM25 无上界），
      直接加权需要调参且不稳定；RRF 只用**排名**，天然免疫量纲问题。
    """
    rows = db.q("SELECT id, path, title, heading, text, embedding FROM kb_chunk")
    if not rows:
        return []

    ranks: dict[int, float] = {}
    K = 60.0        # RRF 常数，压低尾部排名的影响

    # 一路：BM25
    for rank, (idx, _s) in enumerate(_bm25(query, rows, k * 3)):
        ranks[idx] = ranks.get(idx, 0.0) + 1.0 / (K + rank + 1)

    # 二路：向量（有 embedding 且能算出 query 向量时才跑）
    qv = None
    if llm:
        got = _embed([query], llm)
        qv = got[0] if got else None
    if qv:
        sims = [(i, _cosine(qv, _unpack(r["embedding"])))
                for i, r in enumerate(rows) if r["embedding"]]
        sims.sort(key=lambda x: -x[1])
        for rank, (idx, _s) in enumerate(sims[:k * 3]):
            ranks[idx] = ranks.get(idx, 0.0) + 1.0 / (K + rank + 1)

    top = sorted(ranks.items(), key=lambda kv: -kv[1])[:k]
    out = []
    for idx, score in top:
        r = rows[idx]
        out.append({"path": r["path"], "title": r["title"], "heading": r["heading"],
                    "text": r["text"][:1200], "score": round(score, 5)})
    return out


def stats() -> dict:
    total = db.q1("SELECT COUNT(*) c FROM kb_chunk")["c"]
    embedded = db.q1("SELECT COUNT(*) c FROM kb_chunk WHERE embedding IS NOT NULL")["c"]
    files = db.q1("SELECT COUNT(DISTINCT path) c FROM kb_chunk")["c"]
    return {"chunks": total, "embedded": embedded, "files": files,
            "mode": "混合检索(向量+BM25)" if embedded else "纯 BM25（未生成向量）"}
