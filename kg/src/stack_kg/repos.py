"""Optional git repos (KG_REPOS, colon separated) -> graph.

  (:Repo {id: path, name, remote, branch, head})
  (:Commit {sha, short, subject, body, author, date})-[:IN]->(:Repo)   last KG_REPO_COMMITS commits
  (:Commit)-[:TAGGED]->(:Tag)                                          rules on the message
  (:Note)-[:MENTIONS]->(:Repo)                                         repo name appears in the note
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from .db import KG
from .vault import Note

SEP = "\x01"  # not whitespace (str.strip would eat \x1c-\x1f)
REC = "\x02"


def git(cwd: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _safe_remote(url: str) -> str:
    """Drop credentials from https://user:token@host/... remotes."""
    return re.sub(r"^(\w+://)[^/@]+@", r"\1", url)


def inspect(path: Path, n_commits: int) -> dict | None:
    top = git(path, "rev-parse", "--show-toplevel")
    if not top:
        return None
    head = git(path, "rev-parse", "HEAD")
    log = git(path, "log", f"-n{n_commits}", f"--format=%H{SEP}%h{SEP}%an{SEP}%aI{SEP}%s{SEP}%b{REC}")
    commits = []
    for rec in log.split(REC):
        parts = rec.strip("\n").split(SEP)
        if len(parts) < 6:
            continue
        sha, short, author, date, subject, body = parts[:6]
        commits.append({"sha": sha, "short": short, "author": author, "date": date[:19],
                        "subject": subject, "body": body.strip()[:1000]})
    return {
        "id": str(Path(top).resolve()),
        "name": Path(top).name,
        "remote": _safe_remote(git(path, "remote", "get-url", "origin")),
        "branch": git(path, "branch", "--show-current") or "(detached)",
        "head": head,
        "commits": commits,
    }


def ingest(kg: KG, full: bool = False, log=print) -> dict:
    state = kg.state("repo")
    seen: dict[str, str] = {}
    changed = 0
    for p in kg.s.repos:
        info = inspect(p, kg.s.repo_commits)
        if info is None:
            log(f"repos: skipping {p} (not a git repo)")
            continue
        h = hashlib.sha1(f"{info['head']}|{info['branch']}|{kg.s.repo_commits}|{kg.tax.version}".encode()).hexdigest()
        seen[info["id"]] = h
        if not full and state.get(info["id"]) == h and kg.rows("MATCH (r:Repo {id: $id}) RETURN r.id", {"id": info["id"]}):
            continue
        _write_repo(kg, info)
        changed += 1
    gone = [rid for rid in state if rid not in seen]
    for rid in gone:
        kg.write("MATCH (c:Commit)-[:IN]->(r:Repo {id: $id}) DETACH DELETE c", {"id": rid})
        kg.write("MATCH (r:Repo {id: $id}) DETACH DELETE r", {"id": rid})
    # repos not tracked in state (e.g. state was cleared) but still in the graph
    for r in kg.rows("MATCH (r:Repo) RETURN r.id AS id"):
        if r["id"] not in seen:
            kg.write("MATCH (c:Commit)-[:IN]->(r:Repo {id: $id}) DETACH DELETE c", {"id": r["id"]})
            kg.write("MATCH (r:Repo {id: $id}) DETACH DELETE r", {"id": r["id"]})
    kg.save_state("repo", seen, gone)
    return {"repos": len(seen), "repos_changed": changed}


def _write_repo(kg: KG, info: dict) -> None:
    tax = kg.tax
    kg.write("MERGE (r:Repo {id: $id}) SET r.name = $name, r.name_lc = toLower($name), r.remote = $remote, "
             "r.branch = $branch, r.head = $head",
             {k: info[k] for k in ("id", "name", "remote", "branch", "head")})
    kg.write("MATCH (c:Commit)-[:IN]->(:Repo {id: $id}) DETACH DELETE c", {"id": info["id"]})
    commits = []
    for c in info["commits"]:
        hits = tax.rule_hits(c["subject"] + " " + c["body"], "")
        commits.append({**c, "tags": [{"name": h.tag, "facet": h.facet, "confidence": h.confidence} for h in hits]})
    kg.write("""MATCH (r:Repo {id: $id}) UNWIND $commits AS c
        MERGE (x:Commit {sha: c.sha})
        SET x.short = c.short, x.subject = c.subject, x.title = c.subject, x.body = c.body,
            x.author = c.author, x.date = c.date, x.updated = substring(c.date, 0, 10), x.repo = r.name
        MERGE (x)-[:IN]->(r)
        WITH x, c UNWIND c.tags AS t
        MERGE (g:Tag {name: t.name}) ON CREATE SET g.facet = t.facet, g.vocab = false, g.aliases = []
        MERGE (x)-[e:TAGGED]->(g) SET e.by = 'rule', e.confidence = t.confidence, e.source = 'message'""",
             {"id": info["id"], "commits": commits})


def link_mentions(kg: KG, notes: list[Note]) -> int:
    """(:Note)-[:MENTIONS]->(:Repo) when a repo name (>= 3 chars) appears as a word in a note.
    Computed as a diff against the graph, so it stays right when repos come and go."""
    repos = kg.rows("MATCH (r:Repo) RETURN r.id AS id, r.name AS name")
    want: set[tuple[str, str]] = set()
    for r in repos:
        if len(r["name"]) < 3:
            continue
        rx = re.compile(rf"(?<![\w-]){re.escape(r['name'])}(?![\w-])", re.IGNORECASE)
        for n in notes:
            if rx.search(n.title) or rx.search(n.body):
                want.add((n.id, r["id"]))
    have = {(e["n"], e["r"]) for e in kg.rows("MATCH (n:Note)-[:MENTIONS]->(r:Repo) RETURN n.id AS n, r.id AS r")}
    add = [{"n": a, "r": b} for a, b in want - have]
    rem = [{"n": a, "r": b} for a, b in have - want]
    if add:
        kg.write("""UNWIND $e AS e MATCH (n:Note {id: e.n}), (r:Repo {id: e.r})
                    MERGE (n)-[:MENTIONS]->(r)""", {"e": add})
    if rem:
        kg.write("""UNWIND $e AS e MATCH (n:Note {id: e.n})-[m:MENTIONS]->(r:Repo {id: e.r})
                    DELETE m""", {"e": rem})
    return len(add) + len(rem)
