"""`stack-kg` command line."""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from . import ingest, query, remember, render
from .config import EXAMPLES
from .db import KG


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stack-kg", description="Knowledge graph over a markdown folder (FalkorDB).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="index KG_VAULT (+ KG_REPOS) incrementally")
    p.add_argument("--full", action="store_true", help="rewrite every note, not only changed ones")
    p.add_argument("--no-embed", action="store_true", help="skip embeddings (text search only)")

    p = sub.add_parser("find", help="hybrid search (full-text + meaning)")
    p.add_argument("query", nargs="*")
    p.add_argument("-t", "--tag")
    p.add_argument("-n", "--limit", type=int, default=8)
    p.add_argument("--mode", choices=["hybrid", "text", "semantic"], default="hybrid")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("context", help="a note, tag or repo with everything around it")
    p.add_argument("name", nargs="+")
    p.add_argument("--json", action="store_true")

    sub.add_parser("tags", help="tags by facet with counts")
    sub.add_parser("stats", help="node/edge counts, last ingest")

    p = sub.add_parser("cypher", help="read-only Cypher query")
    p.add_argument("query")

    p = sub.add_parser("remember", help="append a fact line to the vault and re-index")
    p.add_argument("kind", choices=["decision", "gotcha", "fact", "todo", "question"])
    p.add_argument("text", nargs="+")
    p.add_argument("--why")
    p.add_argument("--note", help="note title/path to append to (default KG_REMEMBER_NOTE)")

    p = sub.add_parser("reset", help="drop the graph and ingest state (vault untouched)")
    p.add_argument("-y", "--yes", action="store_true")

    p = sub.add_parser("demo", help="copy the example notes into KG_VAULT if it is empty, then ingest")

    sub.add_parser("serve", help="MCP server (streamable HTTP) + /health + periodic ingest")

    a = ap.parse_args(argv)
    kg = KG()

    if a.cmd == "serve":
        from .server import serve
        serve(kg)
        return 0
    if a.cmd == "ingest":
        s = ingest.run(kg, full=a.full, with_embeddings=not a.no_embed, log=lambda *_: None)
        print(json.dumps(s))
        return 1 if "embed_error" in s else 0
    if a.cmd == "find":
        rows = query.find(kg, " ".join(a.query), a.tag, a.limit, a.mode)
        print(json.dumps(rows, default=str, indent=1) if a.json else render.find(rows))
    elif a.cmd == "context":
        c = query.context(kg, " ".join(a.name))
        print(json.dumps(c, default=str, indent=1) if a.json else render.context(c))
    elif a.cmd == "tags":
        print(render.tags(query.tags(kg)))
    elif a.cmd == "stats":
        print(render.stats(query.stats(kg)))
    elif a.cmd == "cypher":
        print(render.cypher(query.cypher(kg, a.query)))
    elif a.cmd == "remember":
        try:
            print(remember.remember(kg, a.kind, " ".join(a.text), a.why, a.note))
        except ValueError as e:
            print(f"Not saved: {e}", file=sys.stderr)
            return 2
    elif a.cmd == "reset":
        if not a.yes and input(f"Drop graph {kg.s.graph!r} and its ingest state? [y/N] ").lower() != "y":
            return 1
        ingest.reset(kg)
        print(f"graph {kg.s.graph} dropped")
    elif a.cmd == "demo":
        v = kg.s.vault
        if v.exists() and any(v.rglob("*.md")):
            print(f"{v} already has notes; not copying examples")
        else:
            v.mkdir(parents=True, exist_ok=True)
            for f in sorted(EXAMPLES.glob("*.md")):
                shutil.copy2(f, v / f.name)
            print(f"copied {len(list(EXAMPLES.glob('*.md')))} example notes to {v}")
        print(json.dumps(ingest.run(kg, log=lambda *_: None)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
