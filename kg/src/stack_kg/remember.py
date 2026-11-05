"""Durable memory: one dated line in a vault note, then re-index.

The vault stays the source of truth: the line becomes a (:Fact) node on every ingest,
so `reset` / `ingest --full` rebuild facts exactly. Line format (parsed by vault.FACT_RE):

    - 2026-01-31: Decision: Use FalkorDB for the graph — why: Cypher + vector index in one store
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from . import ingest, vault
from .db import KG

HEADING = "## Remembered"


def _append_section(text: str, heading: str, line: str) -> str:
    i = text.find(heading)
    if i < 0:
        return text.rstrip() + f"\n\n{heading}\n{line}\n"
    nxt = re.search(r"^## ", text[i + len(heading):], re.M)
    end = i + len(heading) + nxt.start() if nxt else len(text)
    return text[:end].rstrip() + f"\n{line}\n" + ("\n" + text[end:] if nxt else "")


def _target(kg: KG, note: str | None) -> Path:
    root = kg.s.vault
    if note:
        n = note.strip()
        hits = kg.rows("""MATCH (x:Note) WHERE x.hash IS NOT NULL AND
            (x.title_lc = $t OR toLower(x.id) = $t OR toLower(x.id) = $t + '.md') RETURN x.id AS id LIMIT 1""",
                       {"t": n.lower()}) if kg.graph_exists() else []
        if hits:
            return (root / hits[0]["id"]).resolve()
        rel = n if n.endswith(".md") else n + ".md"
    else:
        rel = kg.s.remember_note
    p = (root / rel).resolve()
    if root.resolve() not in p.parents:
        raise ValueError("note must be inside the vault")
    return p


def remember(kg: KG, kind: str, text: str, why: str | None = None, note: str | None = None,
             reindex: bool = True) -> str:
    kind = (kind or "").strip().lower()
    if kind not in vault.FACT_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(vault.FACT_KINDS)}")
    text = re.sub(r"\s+", " ", text or "").strip()
    why = re.sub(r"\s+", " ", why or "").strip()
    if not text:
        raise ValueError("text is empty")
    if "\n" in text or text.startswith("-"):
        raise ValueError("text must be one sentence")

    today = dt.date.today().isoformat()
    line = f"- {'[ ] ' if kind == 'todo' else ''}{today}: {vault.FACT_KINDS[kind]}: {text}"
    if why:
        line += f" — why: {why}"

    with kg.lock:
        target = _target(kg, note)
        rel = target.relative_to(kg.s.vault.resolve())
        fid = vault.fact_id(kind, text)
        body = target.read_text() if target.exists() else ""
        if any(f["id"] == fid for f in vault.parse_facts(body, today)):
            return f"Already recorded in {rel}."
        if not body:
            target.parent.mkdir(parents=True, exist_ok=True)
            body = f"---\ntype: log\ncreated: {today}\ntags: []\n---\n# {target.stem}\n\nWritten by `stack-kg remember` / kg_remember.\n"
        target.write_text(_append_section(body, HEADING, line))
        if reindex:
            ingest.run(kg, log=lambda *_: None)
    return f"Remembered ({kind}) in {rel}: {text}"
