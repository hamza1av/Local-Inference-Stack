"""Retrieval shared by CLI and MCP: hybrid find, context, tags, stats, read-only Cypher."""

from __future__ import annotations

import re

from . import embed
from .db import KG

SUBJECT_FACETS = ["topic", "tech"]
MIN_CONF = 0.6
RRF_K = 60
SEARCH_LABELS = ("Note", "Fact", "Commit")


def ft_query(text: str) -> str | None:
    """Free text -> safe RediSearch query: OR of (prefix) terms, ranking does the rest."""
    toks = [t for t in re.split(r"[^\w]+", text.lower()) if len(t) >= 2]
    if not toks:
        return None
    return "|".join(f"{t}*" if len(t) >= 3 else t for t in dict.fromkeys(toks))


RETURN = """
    OPTIONAL MATCH (n)-[r:TAGGED]->(g:Tag) WHERE r.confidence >= $minc
    WITH n, score, collect(g.name) AS tags
    RETURN labels(n)[0] AS label, coalesce(n.id, n.sha) AS id, coalesce(n.title, n.subject) AS title,
           coalesce(n.type, n.kind, n.repo) AS type, n.status AS status, n.updated AS updated,
           score, tags, coalesce(n.excerpt, n.why, substring(n.body, 0, 300)) AS excerpt, n.path AS path
"""


def _tag_filter(tag: str | None) -> str:
    if not tag:
        return ""
    return "MATCH (n)-[tf:TAGGED]->(:Tag {name: $tag}) WHERE tf.confidence >= $minc WITH DISTINCT n, score"


def find(kg: KG, text: str | None = None, tag: str | None = None, limit: int = 8,
         mode: str = "hybrid", labels: tuple[str, ...] = SEARCH_LABELS) -> list[dict]:
    """Hybrid search: full-text (BM25-ish) + vector (EmbeddingGemma), fused by reciprocal rank.

    Each result carries `via`: ["ft"], ["vec"] or ["ft", "vec"]; vec-only = found by meaning."""
    text = (text or "").strip()
    tag_name = kg.tax.resolve(tag) if tag and tag.strip() else None
    limit = max(1, min(int(limit), 50))
    params = {"minc": MIN_CONF, "tag": tag_name, "maxd": kg.s.max_distance,
              "k": max(limit * 3, 20), "lim": max(limit * 2, 10)}
    tf = _tag_filter(tag_name)

    heads: list[tuple[str, str, str]] = []  # (mode, label, head)
    ftq = ft_query(text) if text and mode in ("hybrid", "text") else None
    if ftq:
        params["ftq"] = ftq
        heads += [("ft", lb, f"CALL db.idx.fulltext.queryNodes('{lb}', $ftq) YIELD node AS n, score")
                  for lb in labels]
    if text and mode in ("hybrid", "semantic"):
        try:
            params["qvec"] = embed.query_vector(kg, text)
            heads += [("vec", lb, f"CALL db.idx.vector.queryNodes('{lb}', 'embedding', $k, vecf32($qvec)) "
                                  f"YIELD node AS n, score WITH n, score WHERE score <= $maxd")
                      for lb in labels if lb in ("Note", "Fact")]
        except embed.EmbedError:
            if not ftq and (q := ft_query(text)):  # embeddings down: fall back to text
                params["ftq"] = q
                heads += [("ft", lb, f"CALL db.idx.fulltext.queryNodes('{lb}', $ftq) YIELD node AS n, score")
                          for lb in labels]
    if not text:
        heads += [("all", lb, f"MATCH (n:{lb}) WITH n, 0.0 AS score") for lb in labels]

    hits = []
    for mode_, label, head in heads:
        order = "score ASC" if mode_ == "vec" else "score DESC"
        if mode_ == "all":
            order = "updated DESC"
        q = f"{head}\n{tf}\n{RETURN}\nORDER BY {order} LIMIT $lim"
        try:
            rows = kg.rows(q, params)
        except Exception as e:  # index/graph missing (nothing ingested yet)
            if any(x in str(e).lower() for x in ("index", "not exist", "empty key", "unknown")):
                continue
            raise
        for rank, r in enumerate(rows):
            hits.append({**r, "_mode": mode_, "_rank": rank})

    fused: dict[tuple, dict] = {}
    for r in hits:
        f = fused.setdefault((r["label"], r["id"]), {**r, "score": 0.0, "via": set()})
        if r["_mode"] != "all":
            f["score"] += 1.0 / (RRF_K + r["_rank"])
        f["via"].add(r["_mode"])
    out = []
    for f in fused.values():
        f.pop("_mode"), f.pop("_rank")
        f["via"] = sorted(f["via"] - {"all"})
        f["score"] = round(f["score"], 4)
        out.append(f)
    out.sort(key=lambda r: (r["score"], r["updated"] or ""), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- context
def _note_context(kg: KG, nid: str) -> dict:
    p = {"id": nid, "minc": MIN_CONF, "facets": SUBJECT_FACETS}
    base = kg.rows("""MATCH (n:Note {id: $id})
        OPTIONAL MATCH (n)-[r:TAGGED]->(g:Tag)
        RETURN n {.id, .title, .type, .status, .updated, .path, .excerpt, .headings, .aliases} AS note,
               collect({tag: g.name, facet: g.facet, by: r.by, confidence: r.confidence}) AS tags""", p)
    if not base:
        return {"kind": "none", "target": nid}
    res = {"kind": "note", **base[0]}
    res["tags"] = sorted((t for t in res["tags"] if t["tag"]), key=lambda t: -t["confidence"])
    res["links"] = kg.rows("""MATCH (n:Note {id: $id})-[:LINKS_TO]->(m)
        RETURN coalesce(m.title, m.id) AS title, m:Ref AS missing ORDER BY title""", p)
    res["backlinks"] = kg.rows("""MATCH (m:Note)-[:LINKS_TO]->(n:Note {id: $id})
        RETURN m.title AS title ORDER BY title""", p)
    res["facts"] = kg.rows("""MATCH (f:Fact)-[:IN_NOTE]->(n:Note {id: $id})
        RETURN f.kind AS kind, f.text AS text, f.why AS why, f.created AS created
        ORDER BY created DESC LIMIT 10""", p)
    res["repos"] = kg.rows("MATCH (n:Note {id: $id})-[:MENTIONS]->(r:Repo) RETURN r.name AS name, r.branch AS branch", p)
    res["related"] = kg.rows("""
        MATCH (n:Note {id: $id})-[r1:TAGGED]->(g:Tag)<-[r2:TAGGED]-(m:Note)
        WHERE m <> n AND g.facet IN $facets AND r1.confidence >= $minc AND r2.confidence >= $minc
        WITH m, collect(g.name) AS shared, sum(r1.confidence * r2.confidence) AS w
        WHERE size(shared) >= 2
        RETURN m.title AS title, shared, w ORDER BY w DESC LIMIT 6""", p)
    return res


def _tag_context(kg: KG, tag: str, limit: int = 15) -> dict:
    p = {"t": tag, "minc": MIN_CONF}
    info = kg.rows("MATCH (g:Tag {name: $t}) RETURN g.facet AS facet, g.aliases AS aliases", p)[0]
    items = kg.rows("""MATCH (n)-[r:TAGGED]->(:Tag {name: $t}) WHERE r.confidence >= $minc
        RETURN labels(n)[0] AS label, n.kind AS kind, coalesce(n.title, n.subject) AS title, n.updated AS updated,
               r.by AS by, r.confidence AS confidence
        ORDER BY confidence DESC, updated DESC""", p)
    co = kg.rows("""MATCH (:Tag {name: $t})<-[r1:TAGGED]-(n)-[r2:TAGGED]->(o:Tag)
        WHERE r1.confidence >= $minc AND r2.confidence >= $minc AND o.name <> $t
        RETURN o.name AS tag, count(n) AS c ORDER BY c DESC LIMIT 10""", p)
    return {"kind": "tag", "tag": tag, **info, "count": len(items), "items": items[:limit], "co_tags": co}


def _repo_context(kg: KG, rid: str) -> dict:
    p = {"id": rid}
    r = kg.rows("MATCH (r:Repo {id: $id}) RETURN r {.*} AS repo", p)[0]["repo"]
    commits = kg.rows("""MATCH (c:Commit)-[:IN]->(:Repo {id: $id})
        RETURN c.short AS sha, c.subject AS subject, c.author AS author, c.date AS date
        ORDER BY date DESC LIMIT 10""", p)
    notes = kg.rows("MATCH (n:Note)-[:MENTIONS]->(:Repo {id: $id}) RETURN n.title AS title ORDER BY title", p)
    return {"kind": "repo", "repo": r, "commits": commits, "notes": notes}


def context(kg: KG, name: str) -> dict:
    """Everything around a note (title, alias or path), a tag, or a repo name."""
    name = (name or "").strip()
    if not name:
        return {"kind": "none", "target": name}
    if name.startswith("#") or name.lower().startswith("tag:"):  # explicitly a tag
        tag = kg.tax.resolve(name.lstrip("#"))
        if kg.graph_exists() and kg.rows("MATCH (g:Tag {name: $t}) RETURN g.name LIMIT 1", {"t": tag}):
            return _tag_context(kg, tag)
    lc = name.lower().removesuffix(".md").lstrip("#")
    try:
        m = kg.rows("""MATCH (n:Note) WHERE n.hash IS NOT NULL AND
                (n.title_lc = $t OR $t IN n.aliases_lc OR toLower(n.id) = $t + '.md' OR toLower(n.id) = $raw)
            RETURN n.id AS id LIMIT 1""", {"t": lc, "raw": name.lower()})
    except Exception:
        return {"kind": "none", "target": name}
    if m:
        return _note_context(kg, m[0]["id"])
    tag = kg.tax.resolve(name)
    if kg.rows("MATCH (g:Tag {name: $t}) RETURN g.name LIMIT 1", {"t": tag}):
        return _tag_context(kg, tag)
    r = kg.rows("MATCH (r:Repo) WHERE r.name_lc = $t RETURN r.id AS id LIMIT 1", {"t": lc})
    if r:
        return _repo_context(kg, r[0]["id"])
    best = find(kg, name, limit=1, labels=("Note",))
    if best:
        return {**_note_context(kg, best[0]["id"]), "guessed_from": name}
    return {"kind": "none", "target": name}


def tags(kg: KG) -> list[dict]:
    return kg.rows("""MATCH (g:Tag)
        OPTIONAL MATCH (g)<-[r:TAGGED]-() WHERE r.confidence >= $minc
        WITH g, count(r) AS uses WHERE uses > 0 OR g.vocab
        RETURN g.name AS name, g.facet AS facet, g.aliases AS aliases, uses
        ORDER BY facet, uses DESC, name""", {"minc": MIN_CONF})


def stats(kg: KG) -> dict:
    if not kg.graph_exists():
        return {"graph": kg.s.graph, "nodes": {}, "edges": {}, "embedded": 0}
    nodes = {r["l"]: r["c"] for r in kg.rows("MATCH (n) RETURN labels(n)[0] AS l, count(n) AS c")}
    edges = {r["t"]: r["c"] for r in kg.rows("MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS c")}
    emb = kg.rows("MATCH (n) WHERE n.emb_key IS NOT NULL RETURN count(n) AS c")[0]["c"]
    untagged = kg.rows("""MATCH (n:Note) WHERE n.hash IS NOT NULL
        OPTIONAL MATCH (n)-[r:TAGGED]->(g:Tag) WHERE g.facet IN $f AND r.confidence >= $minc
        WITH n, count(g) AS k WHERE k = 0
        RETURN count(n) AS c""", {"f": SUBJECT_FACETS, "minc": MIN_CONF})[0]["c"]
    return {"graph": kg.s.graph, "nodes": nodes, "edges": edges, "embedded": emb,
            "notes_without_topic": untagged, "last_ingest": kg.last_run()}


MAX_CYPHER_ROWS = 50


HIDDEN_PROPS = {"embedding", "body", "emb_key", "hash", "title_lc", "aliases_lc", "aliases_text", "mtime", "name_lc"}


def _plain(v):
    """Nodes/edges/paths -> small dicts (no vectors or full bodies)."""
    if hasattr(v, "labels") and hasattr(v, "properties"):
        return {"_labels": list(v.labels or []), **{k: _plain(x) for k, x in v.properties.items()
                                                     if k not in HIDDEN_PROPS}}
    if hasattr(v, "relation") and hasattr(v, "properties"):
        return {"_type": v.relation, **{k: _plain(x) for k, x in v.properties.items()}}
    if hasattr(v, "nodes") and hasattr(v, "edges") and callable(v.nodes):
        return {"_path": [_plain(n) for n in v.nodes()]}
    if isinstance(v, list):
        return [_plain(x) for x in v][:50]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items() if k not in HIDDEN_PROPS}
    if isinstance(v, str) and len(v) > 500:
        return v[:500] + "…"
    return v


def cypher(kg: KG, query: str, max_rows: int = MAX_CYPHER_ROWS) -> list[dict]:
    """Read-only Cypher: executed with GRAPH.RO_QUERY (writes are rejected by the server)."""
    rows = kg.rows(query, readonly=True)
    out = [{k: _plain(v) for k, v in r.items()} for r in rows[:max_rows]]
    if len(rows) > max_rows:
        out.append({"_truncated": f"{len(rows) - max_rows} more rows, add LIMIT or filter"})
    return out
