"""Embeddings via Ollama's OpenAI-compatible `/v1/embeddings` (EmbeddingGemma by default).

EmbeddingGemma expects task prompts:
  query     "task: search result | query: <text>"
  document  "title: <title or 'none'> | text: <text>"

Vectors live on the nodes (`n.embedding`, vecf32) behind a FalkorDB vector index. A node
is (re)embedded when `n.emb_key` (content hash + model) is missing or stale, so a run
where Ollama is down just leaves the work for the next run.
"""

from __future__ import annotations

import re

import httpx

from .db import KG

DOC_CHARS = 1800  # EmbeddingGemma has a 2k token window; stay well inside it
BATCH = 16


class EmbedError(RuntimeError):
    pass


def query_text(q: str) -> str:
    return f"task: search result | query: {q.strip()}"


def doc_text(title: str | None, text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()[:DOC_CHARS]
    return f"title: {(title or 'none').strip()} | text: {text}"


def vectors(kg: KG, texts: list[str], timeout: float = 60.0) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        try:
            r = httpx.post(f"{kg.s.ollama_url}/v1/embeddings",
                           json={"model": kg.s.embed_model, "input": texts[i:i + BATCH]}, timeout=timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise EmbedError(f"embedding request failed ({kg.s.ollama_url}, {kg.s.embed_model}): {e}") from e
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        for d in data:
            if len(d["embedding"]) != kg.s.embed_dim:
                raise EmbedError(f"model returned {len(d['embedding'])} dims, STACK_EMBED_DIM={kg.s.embed_dim}")
            out.append(d["embedding"])
    return out


def query_vector(kg: KG, q: str) -> list[float]:
    return vectors(kg, [query_text(q)], timeout=15.0)[0]


# label -> (key property, Cypher for title, Cypher for text, Cypher for content hash)
SPECS = {
    "Note": ("id", "n.title", "coalesce(n.headings, '') + ' ' + coalesce(n.body, '')", "n.hash"),
    "Fact": ("id", "n.kind", "n.text + coalesce(' (why: ' + n.why + ')', '')", "n.id + coalesce(n.why, '')"),
}


def run(kg: KG, log=print) -> int:
    """Embed every Note/Fact whose vector is missing or stale. Returns how many were embedded."""
    done = 0
    model = kg.s.embed_model
    for label, (key, title, text, h) in SPECS.items():
        todo = kg.rows(f"""MATCH (n:{label}) WITH n, {h} + '|' + $m AS ek
            WHERE n.emb_key IS NULL OR n.emb_key <> ek
            RETURN n.{key} AS k, {title} AS title, {text} AS text, ek""", {"m": model})
        for i in range(0, len(todo), 64):
            chunk = todo[i:i + 64]
            vecs = vectors(kg, [doc_text(r["title"], r["text"]) for r in chunk])
            kg.write(f"""UNWIND $rows AS r MATCH (n:{label} {{{key}: r.k}})
                SET n.embedding = vecf32(r.v), n.emb_key = r.ek""",
                     {"rows": [{"k": r["k"], "v": v, "ek": r["ek"]} for r, v in zip(chunk, vecs)]})
            done += len(chunk)
        if todo:
            log(f"embed: {label} {len(todo)}")
    return done
