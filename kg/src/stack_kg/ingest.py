"""One ingest run: vault notes -> repos -> repo mentions -> embeddings.

Incremental: a note is rewritten only when its content hash (file bytes + taxonomy version)
differs from the one stored in SQLite (`kg_files`) or the node is missing from the graph.
Deleted files are removed. `full=True` rewrites everything.
"""

from __future__ import annotations

import datetime as dt
import json
import time

from . import embed, repos, vault
from .db import KG


def run(kg: KG, full: bool = False, log=print, with_embeddings: bool = True) -> dict:
    with kg.lock:
        started = dt.datetime.now().isoformat(timespec="seconds")
        t0 = time.monotonic()
        try:
            stats = _run(kg, full, log, with_embeddings)
        except Exception as e:
            kg.log_run(started, time.monotonic() - t0, "{}", f"{type(e).__name__}: {e}")
            raise
        stats["seconds"] = round(time.monotonic() - t0, 2)
        kg.log_run(started, stats["seconds"], json.dumps(stats))
        return stats


def _run(kg: KG, full: bool, log, with_embeddings: bool) -> dict:
    tax = kg.reload_taxonomy()
    kg.ensure_schema()
    kg.sync_taxonomy()

    notes = vault.scan(kg.s.vault, tax)
    current = {n.id: n for n in notes}
    state = kg.state("note")
    in_graph = {r["id"] for r in kg.rows("MATCH (n:Note) WHERE n.hash IS NOT NULL RETURN n.id AS id")}

    removed = sorted((set(state) | in_graph) - set(current))
    for nid in removed:
        vault.delete_note(kg, nid)

    resolve = vault.resolver(notes)
    changed = [n for n in notes if full or state.get(n.id) != n.hash or n.id not in in_graph]
    for n in changed:
        vault.extract(n, tax)
        vault.write_note(kg, n, resolve)
    vault.cleanup(kg)
    kg.save_state("note", {n.id: n.hash for n in changed}, removed)

    stats = {"notes": len(notes), "changed": len(changed), "removed": len(removed)}
    stats |= repos.ingest(kg, full=full, log=log)
    stats["mention_edges_changed"] = repos.link_mentions(kg, notes)

    if with_embeddings:
        try:
            stats["embedded"] = embed.run(kg, log=log)
        except embed.EmbedError as e:
            stats["embedded"] = 0
            stats["embed_error"] = str(e)
            log(f"embed: skipped ({e}); text search still works, vectors follow on the next run")
    log(f"ingest: {stats}")
    return stats


def reset(kg: KG) -> None:
    """Drop the graph and its ingest state (the vault itself is untouched)."""
    with kg.lock:
        kg.drop_graph()
        kg.clear_state()
