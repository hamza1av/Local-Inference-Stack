"""`stack-rag` command line."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import Config
from .db import EmbeddingMismatch


def _index(cfg: Config, check: bool = True):
    from .index import Index

    return Index(cfg, check=check)


def cmd_ingest(cfg, a):
    with _index(cfg) as idx:
        res = idx.ingest_paths([Path(p) for p in a.paths], force=a.force)
    counts: dict[str, int] = {}
    for r in res:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in ("added", "updated") or a.verbose:
            print(f"{r.status:9} {r.chunks:4} chunks  {r.title}  ({r.path})")
        elif r.status == "error":
            print(f"error     {r.path}: {r.error}", file=sys.stderr)
    print(", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing to ingest")
    return 1 if counts.get("error") else 0


def cmd_watch(cfg, a):
    inbox = Path(a.dir).expanduser() if a.dir else cfg.inbox
    interval = a.interval or cfg.watch_interval
    print(f"watching {inbox} every {interval:.0f}s (Ctrl-C to stop)")
    with _index(cfg) as idx:
        while True:
            rep = idx.sync_dir(inbox)
            for k, v in rep.items():
                for p in v:
                    print(f"{time.strftime('%H:%M:%S')} {k:8} {p}", flush=True)
            if a.once:
                return 0
            time.sleep(interval)


def cmd_search(cfg, a):
    from .retrieval import hybrid

    with _index(cfg) as idx:
        hits = hybrid(idx, a.query, a.k, max_per_doc=None if a.max_per_doc == 0 else a.max_per_doc, mmr=a.mmr)
    if a.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("no results")
    for i, h in enumerate(hits, 1):
        print(f"[{i}] {h['score']:.4f}  {h['title']}" + (f" › {h['heading']}" if h["heading"] else ""))
        print(f"    {h['path']}")
        text = " ".join(h["text"].split())
        print(f"    {text[:300]}{' …' if len(text) > 300 else ''}\n")
    return 0


def cmd_ask(cfg, a):
    from .ask import ask, sources

    if a.base_url:
        cfg = cfg.with_(llm_base_url=a.base_url.rstrip("/"))
    with _index(cfg) as idx:
        res = ask(idx, a.question, a.k, model=a.model)
    print(res["answer"])
    if res["hits"]:
        print("\nSources:\n" + sources(res["hits"]))
    return 0


def cmd_list(cfg, a):
    with _index(cfg) as idx:
        docs = idx.list_documents()
    if a.json:
        print(json.dumps(docs, ensure_ascii=False, indent=2))
        return 0
    for d in docs:
        print(f"{d['id']:5}  {d['chunks']:4} chunks  {d['title']}  ({d['path']})")
    print(f"{len(docs)} documents")
    return 0


def cmd_rm(cfg, a):
    rc = 0
    with _index(cfg) as idx:
        for ref in a.refs:
            removed = idx.remove(ref)
            if not removed:
                print(f"not found: {ref}", file=sys.stderr)
                rc = 1
            for p in removed:
                print(f"removed {p}")
    return rc


def cmd_stats(cfg, a):
    with _index(cfg, check=False) as idx:
        st = idx.stats()
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        for k, v in st.items():
            print(f"{k:17} {v}")
        if (st["embed_model"], st["embed_dim"]) != (st["configured_model"], st["configured_dim"]):
            print("!! embedding config changed: run `stack-rag reembed`")
    return 0


def cmd_reembed(cfg, a):
    with _index(cfg, check=False) as idx:
        n = idx.reembed(progress=lambda i, t: print(f"\r{i}/{t}", end="", flush=True))
    print(f"\nre-embedded {n} chunks with {cfg.embed_model} / {cfg.embed_dim} dims")
    return 0


def cmd_serve(cfg, a):
    from .server import serve

    if a.port:
        cfg = cfg.with_(mcp_port=a.port)
    if a.host:
        cfg = cfg.with_(mcp_host=a.host)
    serve(cfg, watch=not a.no_watch)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stack-rag", description="Document RAG in SQLite with EmbeddingGemma.")
    p.add_argument("--db", help="SQLite file (default $STACK_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="ingest files or folders (md, txt, pdf, html)")
    s.add_argument("paths", nargs="+")
    s.add_argument("--force", action="store_true", help="re-chunk and re-embed even if unchanged")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("watch", help="poll the inbox folder and keep the index in sync")
    s.add_argument("--dir", help="folder to watch (default $STACK_DATA/rag/inbox)")
    s.add_argument("--interval", type=float)
    s.add_argument("--once", action="store_true", help="sync once and exit")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("search", help="hybrid search (BM25 + vectors)")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--max-per-doc", type=int, default=2, help="0 = unlimited")
    s.add_argument("--mmr", action="store_true", help="diversify results (MMR)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("ask", help="retrieve and answer with [n] citations")
    s.add_argument("question")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--model", help="chat model (default $RAG_LLM_MODEL or 'chat')")
    s.add_argument("--base-url", help="OpenAI-compatible base URL (default $RAG_LLM_BASE_URL or $GATEWAY_URL)")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("list", help="list documents")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("rm", help="remove documents by id, path/key or folder")
    s.add_argument("refs", nargs="+")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("stats", help="index statistics")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("reembed", help="rebuild all vectors with the configured model/dim")
    s.set_defaults(fn=cmd_reembed)

    s = sub.add_parser("serve", help="MCP server (streamable HTTP /mcp, GET /health) + inbox watcher")
    s.add_argument("--port", type=int)
    s.add_argument("--host")
    s.add_argument("--no-watch", action="store_true")
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    cfg = Config.from_env()
    if a.db:
        cfg = cfg.with_(db_path=Path(a.db).expanduser())
    try:
        return a.fn(cfg, a) or 0
    except EmbeddingMismatch as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
