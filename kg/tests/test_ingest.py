from __future__ import annotations

import subprocess

from stack_kg import ingest, query, repos


def count(kg, q, **p):
    return kg.rows(q, p)[0]["c"]


def test_sample_vault_ingest(ingested):
    kg = ingested
    assert kg.stats["notes"] == 9 and kg.stats["changed"] == 9
    assert kg.stats["embedded"] >= 9 and "embed_error" not in kg.stats
    assert count(kg, "MATCH (n:Note) WHERE n.hash IS NOT NULL RETURN count(n) AS c") == 9
    # frontmatter tags, type/status, inline #tags and rule tags with confidences
    tags = {r["t"]: (r["by"], r["c"]) for r in kg.rows(
        "MATCH (:Note {id: 'Gateway.md'})-[r:TAGGED]->(g:Tag) RETURN g.name AS t, r.by AS by, r.confidence AS c")}
    assert tags["rust"] == ("you", 1.0)
    assert tags["reference"] == ("frontmatter", 1.0) and tags["active"] == ("frontmatter", 1.0)
    assert tags["api"] == ("you", 1.0)  # inline #api
    assert kg.rows("MATCH (g:Tag {name: 'rust'}) RETURN g.facet AS f")[0]["f"] == "tech"
    # wikilinks: resolved links + a Ref for the missing note
    links = {r["t"] for r in kg.rows("MATCH (:Note {id: 'Local Inference Stack.md'})-[:LINKS_TO]->(m) "
                                     "RETURN coalesce(m.title, m.id) AS t")}
    assert {"uzu", "Gateway", "RAG", "FalkorDB", "Memory Budget", "Decisions", "Open WebUI"} <= links
    assert count(kg, "MATCH (r:Ref {id: 'open webui'}) RETURN count(r) AS c") == 1
    # headings and aliases
    n = kg.rows("MATCH (n:Note {id: 'RAG.md'}) RETURN n.headings AS h, n.aliases AS a")[0]
    assert "Pipeline" in n["h"] and n["a"] == ["Retrieval"]
    # facts from list lines, tagged with their kind
    assert count(kg, "MATCH (f:Fact {kind: 'decision'})-[:IN_NOTE]->(:Note {id: 'Decisions.md'}) RETURN count(f) AS c") == 4
    assert count(kg, "MATCH (f:Fact {kind: 'gotcha'})-[:TAGGED]->(:Tag {name: 'gotcha'}) RETURN count(f) AS c") == 1
    assert count(kg, "MATCH (f:Fact {kind: 'todo'}) RETURN count(f) AS c") == 1


def test_incremental_update_delete_and_full(kg):
    s = ingest.run(kg, log=lambda *_: None)
    assert s["changed"] == 9
    assert ingest.run(kg, log=lambda *_: None)["changed"] == 0
    assert kg.state("note")["uzu.md"]  # hashes live in SQLite (kg_files)

    # change one note -> only that one is rewritten, new tag appears
    p = kg.s.vault / "uzu.md"
    p.write_text(p.read_text() + "\nWorks well with #python clients.\n")
    s = ingest.run(kg, log=lambda *_: None)
    assert s["changed"] == 1 and s["embedded"] == 1
    assert count(kg, "MATCH (:Note {id: 'uzu.md'})-[:TAGGED]->(:Tag {name: 'python'}) RETURN count(*) AS c") == 1

    # delete a note -> node gone, links to it become a Ref, its facts are gone
    (kg.s.vault / "Decisions.md").unlink()
    s = ingest.run(kg, log=lambda *_: None)
    assert s["removed"] == 1
    assert count(kg, "MATCH (n:Note {id: 'Decisions.md'}) RETURN count(n) AS c") == 0
    assert count(kg, "MATCH (f:Fact {kind: 'decision'}) RETURN count(f) AS c") == 0
    assert count(kg, "MATCH (:Note {id: 'Gateway.md'})-[:LINKS_TO]->(r:Ref {id: 'decisions'}) RETURN count(r) AS c") == 1
    assert "Decisions.md" not in kg.state("note")

    # a new note named like a missing link turns the Ref into a real link
    (kg.s.vault / "Open WebUI.md").write_text("# Open WebUI\nChat front end, talks to the [[Gateway]].\n")
    ingest.run(kg, log=lambda *_: None)
    assert count(kg, "MATCH (r:Ref {id: 'open webui'}) RETURN count(r) AS c") == 0
    assert count(kg, "MATCH (:Note {id: 'Local Inference Stack.md'})-[:LINKS_TO]->(:Note {id: 'Open WebUI.md'}) "
                     "RETURN count(*) AS c") == 1

    # --full rewrites everything but embeddings stay (content unchanged)
    s = ingest.run(kg, full=True, log=lambda *_: None)
    assert s["changed"] == 9 and s["embedded"] == 0


def test_reset_then_ingest_rebuilds(kg):
    ingest.run(kg, log=lambda *_: None)
    ingest.reset(kg)
    assert not kg.graph_exists() and kg.state("note") == {}
    assert ingest.run(kg, log=lambda *_: None)["changed"] == 9


def test_graph_lost_but_state_kept_is_repaired(kg):
    ingest.run(kg, log=lambda *_: None)
    kg.drop_graph()  # e.g. volume wiped; SQLite still has the hashes
    s = ingest.run(kg, log=lambda *_: None)
    assert s["changed"] == 9


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_repos_commits_and_mentions(kg, tmp_path):
    repo = tmp_path / "demo-service"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.invalid")
    _git(repo, "config", "user.name", "Dev")
    for i, msg in enumerate(["Initial commit", "Add RAG retrieval endpoint", "Fix Python packaging"]):
        (repo / f"f{i}.txt").write_text(msg)
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", msg)
    (kg.s.vault / "Service.md").write_text("# Service\nThe demo-service repo hosts the API.\n")
    kg.s.repos = [repo]
    s = ingest.run(kg, log=lambda *_: None)
    assert s["repos"] == 1 and s["repos_changed"] == 1
    assert count(kg, "MATCH (c:Commit)-[:IN]->(:Repo {name: 'demo-service'}) RETURN count(c) AS c") == 3
    assert count(kg, "MATCH (:Note {id: 'Service.md'})-[:MENTIONS]->(:Repo) RETURN count(*) AS c") == 1
    assert count(kg, "MATCH (:Commit {subject: 'Add RAG retrieval endpoint'})-[:TAGGED]->(:Tag {name: 'rag'}) "
                     "RETURN count(*) AS c") == 1
    assert ingest.run(kg, log=lambda *_: None)["repos_changed"] == 0
    c = query.context(kg, "demo-service")
    assert c["kind"] == "repo" and len(c["commits"]) == 3 and c["notes"][0]["title"] == "Service"
    hits = query.find(kg, "retrieval endpoint", limit=10)
    assert any(h["label"] == "Commit" for h in hits)

    kg.s.repos = []
    ingest.run(kg, log=lambda *_: None)
    assert count(kg, "MATCH (n) WHERE n:Repo OR n:Commit RETURN count(n) AS c") == 0
    assert repos.link_mentions(kg, []) == 0
