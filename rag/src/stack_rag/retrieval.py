"""Hybrid retrieval: FTS5 BM25 + sqlite-vec cosine KNN, fused with reciprocal rank fusion.

    score(chunk) = sum over lists of 1 / (RRF_K + rank)

Then optional MMR (diversity over the chunk vectors) and a per-document cap so one long
document does not fill every slot.
"""

from __future__ import annotations

import math
import re
from array import array

import sqlite_vec

from .index import Index

RRF_K = 60
_STOP = set(
    """a an and are as at be but by can do does for from how i if in into is it its me my no not of on or our
    so that the their then there these they this to was we what when where which who why will with you your
    der die das und oder ein eine ist sind zu den dem des im in mit von für auf wie was wer warum nicht""".split()
)


def fts_query(q: str) -> str:
    toks = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1]
    keep = [t for t in toks if t not in _STOP] or toks
    seen: list[str] = []
    for t in keep:
        if t not in seen:
            seen.append(t)
    return " OR ".join(f'"{t}"' for t in seen[:32])


def _bm25(idx: Index, q: str, n: int) -> list[int]:
    fq = fts_query(q)
    if not fq:
        return []
    rows = idx.con.execute(
        "SELECT rowid FROM rag_fts WHERE rag_fts MATCH ? ORDER BY bm25(rag_fts, 2.0, 1.0) LIMIT ?", (fq, n)
    ).fetchall()
    return [r[0] for r in rows]


def _knn(idx: Index, qv: list[float], n: int) -> list[int]:
    rows = idx.con.execute(
        "SELECT chunk_id FROM rag_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(qv), n),
    ).fetchall()
    return [r[0] for r in rows]


def _vectors(idx: Index, ids: list[int]) -> dict[int, list[float]]:
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    out = {}
    for cid, blob in idx.con.execute(f"SELECT chunk_id, embedding FROM rag_vec WHERE chunk_id IN ({qs})", ids):
        out[cid] = array("f", blob).tolist()
    return out


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1)


def hybrid(
    idx: Index,
    query: str,
    k: int = 5,
    *,
    max_per_doc: int | None = 2,
    mmr: bool = False,
    mmr_lambda: float = 0.7,
    candidates: int | None = None,
) -> list[dict]:
    query = query.strip()
    if not query or k <= 0:
        return []
    n = candidates or max(30, k * 6)
    qv = idx.embedder.embed_query(query)  # network call outside the lock
    with idx.lock:
        lexical = _bm25(idx, query, n)
        semantic = _knn(idx, qv, n)
        fused: dict[int, float] = {}
        for ranked in (lexical, semantic):
            for rank, cid in enumerate(ranked, 1):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        order = sorted(fused, key=fused.get, reverse=True)
        if not order:
            return []
        qs = ",".join("?" * len(order))
        rows = {
            r["id"]: r
            for r in idx.con.execute(
                "SELECT c.id, c.doc_id, c.heading, c.text, d.title, d.path FROM rag_chunks c "
                f"JOIN rag_documents d ON d.id = c.doc_id WHERE c.id IN ({qs})",
                order,
            )
        }
        order = [c for c in order if c in rows]
        vecs = _vectors(idx, order) if mmr else {}

    picked: list[int] = []
    per_doc: dict[int, int] = {}

    def allowed(cid: int) -> bool:
        return max_per_doc is None or per_doc.get(rows[cid]["doc_id"], 0) < max_per_doc

    if mmr and vecs:
        top = max(fused[c] for c in order)
        pool = [c for c in order if c in vecs]
        while pool and len(picked) < k:
            best, best_val = None, -1e9
            for c in pool:
                if not allowed(c):
                    continue
                rel = fused[c] / top
                red = max((_cos(vecs[c], vecs[p]) for p in picked), default=0.0)
                val = mmr_lambda * rel - (1 - mmr_lambda) * red
                if val > best_val:
                    best, best_val = c, val
            if best is None:
                break
            pool.remove(best)
            picked.append(best)
            per_doc[rows[best]["doc_id"]] = per_doc.get(rows[best]["doc_id"], 0) + 1
    else:
        for c in order:
            if len(picked) >= k:
                break
            if allowed(c):
                picked.append(c)
                per_doc[rows[c]["doc_id"]] = per_doc.get(rows[c]["doc_id"], 0) + 1

    return [
        {
            "doc_id": rows[c]["doc_id"],
            "chunk_id": c,
            "title": rows[c]["title"],
            "path": rows[c]["path"],
            "heading": rows[c]["heading"],
            "text": rows[c]["text"],
            "score": round(fused[c], 6),
        }
        for c in picked
    ]
