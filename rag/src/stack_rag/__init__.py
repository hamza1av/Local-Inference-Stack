"""stack-rag: document RAG in SQLite (FTS5 + sqlite-vec) with EmbeddingGemma.

Python API for other components::

    from stack_rag import search
    hits = search("how do I rotate the api keys", k=5)
    # [{doc_id, title, path, heading, text, score, chunk_id}, ...]
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .index import Index

__all__ = ["search", "get_index"]

_index: Index | None = None
_lock = threading.Lock()


def get_index() -> Index:
    """Process-wide Index built from the environment (`.env`), created on first use."""
    global _index
    with _lock:
        if _index is None:
            from .index import Index

            _index = Index()
        return _index


def search(query: str, k: int = 5, *, max_per_doc: int | None = 2, mmr: bool = False, index: Index | None = None) -> list[dict]:
    """Hybrid (BM25 + vector, RRF) search over the RAG index.

    Returns up to `k` dicts with keys doc_id, title, path, heading, text, score (higher is
    better) and chunk_id. `max_per_doc` caps hits per document (None = no cap); `mmr=True`
    re-ranks for diversity.
    """
    from .retrieval import hybrid

    return hybrid(index or get_index(), query, k, max_per_doc=max_per_doc, mmr=mmr)
