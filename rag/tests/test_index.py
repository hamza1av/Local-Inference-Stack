"""Ingest / incremental update / delete / re-embed against a temp DB with real embeddings."""

import os
import time

import pytest
from conftest import needs_embed

from stack_rag import db
from stack_rag.index import Index

pytestmark = needs_embed


def counts(idx: Index) -> tuple[int, int, int, int]:
    q = lambda s: idx.con.execute(s).fetchone()[0]  # noqa: E731
    return (
        q("SELECT COUNT(*) FROM rag_documents"),
        q("SELECT COUNT(*) FROM rag_chunks"),
        q("SELECT COUNT(*) FROM rag_vec"),
        q("SELECT COUNT(*) FROM rag_fts WHERE rag_fts MATCH 'the OR a OR and'"),
    )


def test_ingest_is_incremental(index, docs):
    res = index.ingest_paths([docs])
    assert sorted(r.status for r in res) == ["added"] * 4
    d, c, v, _ = counts(index)
    assert (d, c == v) == (4, True) and c >= 8

    again = index.ingest_paths([docs])
    assert {r.status for r in again} == {"unchanged"}
    assert counts(index)[:3] == (d, c, v)

    # change one file: only that document is rebuilt, no orphan vectors / FTS rows
    f = docs / "sourdough.md"
    f.write_text(f.read_text() + "\n## Storage\n\nWrap the cooled loaf in a linen towel; freeze slices for later.\n")
    res = index.ingest_file(f)
    assert res.status == "updated"
    d2, c2, v2, _ = counts(index)
    assert d2 == 4 and c2 == v2
    orphans = index.con.execute(
        "SELECT COUNT(*) FROM rag_vec WHERE chunk_id NOT IN (SELECT id FROM rag_chunks)"
    ).fetchone()[0]
    assert orphans == 0
    fts = index.con.execute("SELECT COUNT(*) FROM rag_fts WHERE rag_fts MATCH 'linen'").fetchone()[0]
    assert fts == 1


def test_remove_by_id_path_and_folder(index, docs):
    index.ingest_paths([docs])
    doc = index.list_documents()[0]
    assert index.remove(doc["id"]) == [doc["path"]]
    assert index.remove(str(docs / "sourdough.md")) == [str((docs / "sourdough.md").resolve())]
    assert len(index.remove(str(docs))) == 2
    assert counts(index)[:3] == (0, 0, 0)
    assert index.con.execute("SELECT COUNT(*) FROM rag_fts WHERE rag_fts MATCH 'router'").fetchone()[0] == 0
    assert index.remove("does-not-exist") == []


def test_sync_dir_adds_updates_and_removes(index, docs):
    inbox = index.cfg.inbox
    inbox.mkdir(parents=True)
    a = inbox / "a.md"
    a.write_text("# Alpha\n\nThe alpha note mentions the purple elephant.\n")
    (inbox / ".hidden.md").write_text("# hidden\n")
    old = time.time() - 10
    os.utime(a, (old, old))
    rep = index.sync_dir(inbox)
    assert rep["added"] == [str(a.resolve())]
    assert index.sync_dir(inbox) == {"added": [], "updated": [], "removed": [], "error": []}

    a.write_text("# Alpha\n\nNow it mentions a green giraffe.\n")
    os.utime(a, (old + 1, old + 1))
    assert index.sync_dir(inbox)["updated"] == [str(a.resolve())]

    fresh = inbox / "b.txt"
    fresh.write_text("still being written")
    assert index.sync_dir(inbox, settle=60)["added"] == []  # too new, next round

    a.unlink()
    rep = index.sync_dir(inbox, settle=0)
    assert rep["removed"] == [str(a.resolve())]
    assert rep["added"] == [str(fresh.resolve())]
    assert [d["path"] for d in index.list_documents()] == [str(fresh.resolve())]


def test_add_text_replaces_same_title(index):
    r1 = index.add_text("Wi-Fi notes", "The guest network password is on the fridge.")
    assert r1.status == "added" and r1.path == "text:wi-fi-notes"
    assert index.add_text("Wi-Fi notes", "The guest network password is on the fridge.").status == "unchanged"
    r2 = index.add_text("Wi-Fi notes", "The guest password now lives in the password manager.")
    assert r2.status == "updated" and r2.doc_id == r1.doc_id
    assert counts(index)[0] == 1


def test_refuses_to_mix_models_and_reembeds(cfg, docs):
    with Index(cfg) as idx:
        idx.ingest_paths([docs / "sourdough.md"])
    small = cfg.with_(embed_dim=256)
    with pytest.raises(db.EmbeddingMismatch):
        Index(small)
    with pytest.raises(db.EmbeddingMismatch):
        Index(cfg.with_(embed_model="other-model"))
    with Index(small, check=False) as idx:
        n = idx.reembed()
        assert n == idx.stats()["chunks"] > 0
    with Index(small) as idx:  # now consistent
        st = idx.stats()
        assert (st["embed_dim"], st["vectors"]) == (256, n)
        from stack_rag.retrieval import hybrid

        assert hybrid(idx, "how long does the dough proof in the fridge", 1)[0]["title"] == "Sourdough Bread Notes"
