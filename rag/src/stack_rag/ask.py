"""Retrieve + answer through an OpenAI-compatible chat endpoint, with [n] citations."""

from __future__ import annotations

import re

import httpx

from .index import Index
from .retrieval import hybrid

SYSTEM = (
    "You answer questions using only the numbered context passages provided by the user. "
    "Cite every statement with the passage number in square brackets, e.g. [1] or [2][3]. "
    "If the passages do not contain the answer, say that the documents do not cover it. "
    "Be concise and answer in the language of the question."
)


def format_hits(hits: list[dict], max_chars: int | None = None) -> str:
    """Compact numbered passages: `[n] Title › heading (path)` + text."""
    out = []
    for i, h in enumerate(hits, 1):
        head = h["title"] + (f" › {h['heading']}" if h["heading"] and h["heading"] != h["title"] else "")
        text = h["text"] if max_chars is None or len(h["text"]) <= max_chars else h["text"][:max_chars].rstrip() + " …"
        out.append(f"[{i}] {head} ({h['path']})\n{text}")
    return "\n\n".join(out)


def sources(hits: list[dict]) -> str:
    lines = []
    for i, h in enumerate(hits, 1):
        lines.append(f"[{i}] {h['title']}" + (f" › {h['heading']}" if h["heading"] else "") + f" — {h['path']}")
    return "\n".join(lines)


def ask(idx: Index, question: str, k: int = 5, model: str | None = None, timeout: float = 300.0) -> dict:
    cfg = idx.cfg
    hits = hybrid(idx, question, k)
    if not hits:
        return {"answer": "No documents in the index match this question.", "hits": []}
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Context passages:\n\n{format_hits(hits)}\n\nQuestion: {question}"},
    ]
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}"} if cfg.llm_api_key else {}
    r = httpx.post(
        f"{cfg.llm_base_url}/chat/completions",
        headers=headers,
        json={"model": model or cfg.llm_model, "messages": messages, "temperature": 0.2, "stream": False},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"chat request to {cfg.llm_base_url} failed: {r.status_code} {r.text[:300]}")
    content = r.json()["choices"][0]["message"].get("content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    content = re.sub(r"[【［]\s*(\d+)\s*[】］]", r"[\1]", content)  # small models emit 【1】 / ［1］
    return {"answer": content, "hits": hits}
