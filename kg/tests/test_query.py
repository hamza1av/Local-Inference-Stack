from __future__ import annotations

import pytest

from stack_kg import ingest, query, remember, render


def titles(rows):
    return [r["title"] for r in rows]


def test_hybrid_find_text_and_meaning(ingested):
    rows = query.find(ingested, "why is the gateway written in rust")
    assert rows[0]["title"] in ("Write the gateway in Rust", "Gateway")
    assert {"Gateway", "Write the gateway in Rust"} <= set(titles(rows))
    top = next(r for r in rows if r["title"] == "Gateway")
    assert top["via"] == ["ft", "vec"]  # found by words and by meaning -> fused


def test_meaning_only_hits_are_marked(ingested):
    rows = query.find(ingested, "laptop running out of RAM", mode="semantic")
    assert "Memory Budget" in titles(rows[:3])
    assert all(r["via"] == ["vec"] for r in rows)
    assert "~meaning-only" in render.find(rows)


def test_unrelated_query_finds_little(ingested):
    rows = query.find(ingested, "chocolate cake baking recipe", mode="semantic")
    assert rows == []


def test_tag_filter_and_alias(ingested):
    rows = query.find(ingested, "", tag="decisions", limit=20)  # alias of 'decision'
    assert rows and all("decision" in r["tags"] for r in rows)
    assert {r["label"] for r in rows} == {"Note", "Fact"}
    rows = query.find(ingested, "container", tag="falkordb")
    assert rows and all("falkordb" in r["tags"] for r in rows)


def test_context_note_tag_and_fallback(ingested):
    c = query.context(ingested, "Overview")  # alias
    assert c["kind"] == "note" and c["note"]["title"] == "Local Inference Stack"
    assert any(x["title"] == "Open WebUI" and x["missing"] for x in c["links"])
    assert {"Decisions"} <= {x["title"] for x in c["backlinks"]}
    c = query.context(ingested, "Decisions")
    assert len(c["facts"]) == 4 and "why" in render.context(c)
    c = query.context(ingested, "#docker")
    assert c["kind"] == "tag" and c["count"] >= 1
    c = query.context(ingested, "observability")  # not a title -> tag
    assert c["kind"] == "tag"
    c = query.context(ingested, "the thing that serves the models")
    assert c["kind"] == "note" and c.get("guessed_from")
    assert render.context(query.context(ingested, "")).startswith("Nothing found")


def test_tags_and_stats(ingested):
    t = {r["name"]: r for r in query.tags(ingested)}
    assert t["rag"]["facet"] == "topic" and t["rag"]["uses"] >= 1
    s = query.stats(ingested)
    assert s["nodes"]["Note"] == 9 and s["edges"]["LINKS_TO"] > 10 and s["embedded"] >= 9
    assert "graph kg_test_" in render.stats(s)


def test_cypher_is_read_only_and_capped(ingested):
    rows = query.cypher(ingested, "MATCH (n:Note) RETURN n ORDER BY n.title")
    assert rows[0]["n"]["_labels"] == ["Note"] and "embedding" not in rows[0]["n"]
    rows = query.cypher(ingested, "UNWIND range(1, 80) AS i RETURN i")
    assert len(rows) == 51 and "_truncated" in rows[-1]
    with pytest.raises(Exception):
        query.cypher(ingested, "CREATE (:Hack {x: 1})")
    with pytest.raises(Exception):
        query.cypher(ingested, "MATCH (n:Note) DETACH DELETE n")
    assert ingested.rows("MATCH (n:Note) RETURN count(n) AS c")[0]["c"] == 9


def test_remember_is_rebuild_safe(kg):
    ingest.run(kg, log=lambda *_: None)
    msg = remember.remember(kg, "decision", "Bind every service to 127.0.0.1", why="nothing is exposed by accident")
    assert "Remembered.md" in msg
    line = (kg.s.vault / "Remembered.md").read_text().splitlines()[-1]
    assert line.endswith(": Decision: Bind every service to 127.0.0.1 — why: nothing is exposed by accident")
    assert "Already recorded" in remember.remember(kg, "decision", "Bind every  service to 127.0.0.1")
    remember.remember(kg, "todo", "Add a backup job", note="Gateway")
    assert "- [ ] " in (kg.s.vault / "Gateway.md").read_text()

    def facts():
        return {(r["k"], r["t"]) for r in kg.rows("MATCH (f:Fact) RETURN f.kind AS k, f.text AS t")}

    before = facts()
    assert ("decision", "Bind every service to 127.0.0.1") in before and ("todo", "Add a backup job") in before
    ingest.reset(kg)
    ingest.run(kg, log=lambda *_: None)
    assert facts() == before
    rows = query.find(kg, "which address should services listen on")
    assert "Bind every service to 127.0.0.1" in titles(rows)


def test_remember_validates(kg):
    with pytest.raises(ValueError):
        remember.remember(kg, "opinion", "x", reindex=False)
    with pytest.raises(ValueError):
        remember.remember(kg, "fact", "   ", reindex=False)
    with pytest.raises(ValueError):
        remember.remember(kg, "fact", "x", note="../outside", reindex=False)
