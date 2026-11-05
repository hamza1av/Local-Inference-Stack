"""Hybrid ranking sanity on the example docs (real EmbeddingGemma)."""

import pytest
from conftest import needs_embed

import stack_rag
from stack_rag.retrieval import _bm25, _knn, hybrid

pytestmark = needs_embed

CASES = [
    # (query, expected document title, expected heading fragment or None)
    ("when are snapshots copied to the external disk", "Home Network Handbook", "NAS and backups"),
    ("which wifi channels do not overlap", "Home Network Handbook", "Wi-Fi"),
    ("how long should the dough proof in the fridge", "Sourdough Bread Notes", "Dough"),
    ("how often should I oil my chain", "Bicycle Maintenance Checklist", None),
    ("tiny flies in wet potting soil", "Houseplant Care Guide", "p. 2"),
    ("my bread is dense and gummy inside", "Sourdough Bread Notes", None),
    ("192.168.1.10", "Home Network Handbook", "DHCP"),  # exact token: BM25 should carry it
]


@pytest.mark.parametrize("query,title,heading", CASES)
def test_top_hit(demo_index, query, title, heading):
    hits = hybrid(demo_index, query, 3)
    assert hits, query
    assert hits[0]["title"] == title, [(h["title"], h["heading"]) for h in hits]
    if heading:
        assert heading in hits[0]["heading"], hits[0]["heading"]


def test_fusion_uses_both_lists(demo_index):
    # semantic only: no keyword overlap with the text ("flies" vs "gnats")
    q = "insects living in houseplant soil"
    assert _knn(demo_index, demo_index.embedder.embed_query(q), 5)
    # lexical-only query string still returns something through BM25
    assert _bm25(demo_index, "banneton", 5)
    hits = hybrid(demo_index, "banneton", 1)
    assert hits[0]["title"] == "Sourdough Bread Notes"


def test_result_shape_and_python_api(demo_index):
    hits = stack_rag.search("router firmware update", k=4, index=demo_index)
    assert 0 < len(hits) <= 4
    assert set(hits[0]) >= {"doc_id", "title", "path", "heading", "text", "score"}
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)


def test_max_per_doc_and_mmr(demo_index):
    capped = hybrid(demo_index, "home network wifi router backup", 6, max_per_doc=1)
    assert len({h["doc_id"] for h in capped}) == len(capped)
    uncapped = hybrid(demo_index, "home network wifi router backup", 6, max_per_doc=None)
    assert sum(h["title"] == "Home Network Handbook" for h in uncapped) >= 3
    diverse = hybrid(demo_index, "home network wifi router backup", 4, max_per_doc=None, mmr=True)
    assert len(diverse) == 4 and len({h["chunk_id"] for h in diverse}) == 4


def test_empty_query(demo_index):
    assert hybrid(demo_index, "   ", 5) == []
