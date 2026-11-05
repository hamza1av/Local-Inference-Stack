"""EmbeddingGemma client (OpenAI-compatible /embeddings on Ollama or the gateway).

EmbeddingGemma expects task prompts; documents and queries are embedded differently.
It is trained with Matryoshka representation learning, so the first 512/256/128 dims of a
768-dim vector are a valid smaller embedding once renormalised.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import httpx

from .config import Config

QUERY_PROMPT = "task: search result | query: {q}"
DOC_PROMPT = "title: {title} | text: {text}"
MATRYOSHKA_DIMS = (768, 512, 256, 128)


def format_query(q: str) -> str:
    return QUERY_PROMPT.format(q=q.strip())


def format_doc(title: str | None, text: str) -> str:
    return DOC_PROMPT.format(title=(title or "none").strip() or "none", text=text.strip())


def truncate(vec: Sequence[float], dim: int) -> list[float]:
    v = list(vec[:dim])
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    def __init__(self, cfg: Config, client: httpx.Client | None = None):
        self.cfg = cfg
        if cfg.embed_dim not in MATRYOSHKA_DIMS:
            raise ValueError(f"STACK_EMBED_DIM must be one of {MATRYOSHKA_DIMS}, got {cfg.embed_dim}")
        if cfg.embed_via_gateway:
            self.url = f"{cfg.gateway_url}/embeddings"
            self.headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        else:
            self.url = f"{cfg.ollama_url}/v1/embeddings"
            self.headers = {}
        self.client = client or httpx.Client(timeout=120.0)

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        bs = max(1, self.cfg.embed_batch)
        for i in range(0, len(inputs), bs):
            batch = inputs[i : i + bs]
            r = self.client.post(
                self.url, headers=self.headers, json={"model": self.cfg.embed_model, "input": batch}
            )
            if r.status_code >= 400:
                raise RuntimeError(f"embedding request to {self.url} failed: {r.status_code} {r.text[:300]}")
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            if len(data) != len(batch):
                raise RuntimeError(f"embedding endpoint returned {len(data)} vectors for {len(batch)} inputs")
            for d in data:
                v = d["embedding"]
                if len(v) < self.cfg.embed_dim:
                    raise RuntimeError(
                        f"model {self.cfg.embed_model} returned {len(v)} dims, STACK_EMBED_DIM={self.cfg.embed_dim}"
                    )
                out.append(truncate(v, self.cfg.embed_dim))
        return out

    def embed_query(self, q: str) -> list[float]:
        return self._embed([format_query(q)])[0]

    def embed_docs(self, title: str | None, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([format_doc(title, t) for t in texts])

    def embed_pairs(self, items: Sequence[tuple[str | None, str]]) -> list[list[float]]:
        return self._embed([format_doc(t, x) for t, x in items])
