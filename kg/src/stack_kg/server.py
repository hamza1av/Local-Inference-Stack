"""MCP server (streamable HTTP at /mcp) + GET /health + periodic re-ingest in the same process.

    stack-kg serve            # 127.0.0.1:$KG_MCP_PORT/mcp
"""

from __future__ import annotations

import logging
import os
import threading

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import ingest, remember, render
from . import query as query_mod
from .db import KG

log = logging.getLogger("stack_kg")

INSTRUCTIONS = """\
Knowledge graph over the user's markdown notes (and optionally git repos): notes, tags,
[[links]] between notes, and remembered facts (decisions, gotchas, todos, questions).
How to use it:
1. kg_find("a few keywords") to search. Add tag="..." to filter (kg_tags lists tag names).
2. kg_context("exact note title or tag") to see one note/tag with its links and facts.
3. kg_remember(kind, text, why) to save a decision/gotcha/fact/todo/question for next time.
Answer from the returned text; say so when nothing relevant was found."""


def build(kg: KG) -> FastMCP:
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*"]
    hosts += [h.strip() for h in os.environ.get("KG_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts],
    )
    mcp = FastMCP("kg", instructions=INSTRUCTIONS, host=kg.s.mcp_host, port=kg.s.mcp_port,
                  streamable_http_path="/mcp", stateless_http=True, json_response=True,
                  transport_security=security, log_level="WARNING")

    async def call(fn, *args):
        try:
            return await anyio.to_thread.run_sync(fn, *args)
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:  # keep the model in the loop instead of a protocol error
            log.exception("tool failed")
            return f"Error ({type(e).__name__}): {e}"

    @mcp.tool()
    async def kg_find(query: str, tag: str | None = None, limit: int = 8) -> str:
        """Search the knowledge base (notes, remembered facts, commits) by keywords and meaning.

        Use this first for any question about the user's notes, projects, decisions or setup.

        Args:
            query: 2-6 keywords, e.g. "falkordb memory limit" or "why gateway rust".
            tag: optional tag filter, e.g. "rag", "decision", "python". Use kg_tags to see names.
            limit: max results (default 8).

        Returns one line per hit: title (type · status · date) #tags, then a short excerpt.
        [decision]/[gotcha]/[todo]... = a remembered fact. "~meaning-only" = found by meaning,
        not by the exact words: check it is really relevant.
        """
        return await call(lambda: render.find(query_mod.find(kg, query, tag, limit)))

    @mcp.tool()
    async def kg_context(name: str) -> str:
        """Show one note, tag or repo with everything connected to it.

        Args:
            name: a note title (as shown by kg_find, e.g. "Gateway"), a tag (e.g. "rag")
                  or a repo name.

        Returns metadata, tags, excerpt, section headings, links to/from other notes,
        facts recorded in the note and related notes (shared tags). For a tag: the notes
        that carry it. Call kg_context again on a linked title to follow the graph.
        """
        return await call(lambda: render.context(query_mod.context(kg, name)))

    @mcp.tool()
    async def kg_tags() -> str:
        """List all tags by facet (topic, tech, type, status) with usage counts and aliases.

        Use it to pick a valid `tag` for kg_find."""
        return await call(lambda: render.tags(query_mod.tags(kg)))

    @mcp.tool()
    async def kg_stats() -> str:
        """Size of the knowledge graph (nodes/edges by kind) and when it was last updated."""
        return await call(lambda: render.stats(query_mod.stats(kg)))

    @mcp.tool()
    async def kg_cypher(query: str) -> str:
        """Run a READ-ONLY openCypher query (FalkorDB) for questions kg_find cannot answer.

        Prefer kg_find/kg_context. Writes are rejected. At most 50 rows are returned; use LIMIT.

        Schema:
          (:Note {id, title, type, status, updated, excerpt, headings, path})
          (:Note)-[:TAGGED {by, confidence}]->(:Tag {name, facet})   facets: topic tech type status
          (:Note)-[:LINKS_TO]->(:Note)          (:Note)-[:LINKS_TO]->(:Ref {title})  (missing note)
          (:Fact {kind, text, why, created})-[:IN_NOTE]->(:Note)     (:Fact)-[:TAGGED]->(:Tag)
          (:Repo {name, branch, remote})<-[:IN]-(:Commit {short, subject, author, date})
          (:Note)-[:MENTIONS]->(:Repo)

        Example: MATCH (n:Note)-[:TAGGED]->(:Tag {name: 'rag'}) RETURN n.title LIMIT 10
        """
        return await call(lambda: render.cypher(query_mod.cypher(kg, query)))

    @mcp.tool()
    async def kg_remember(kind: str, text: str, why: str | None = None) -> str:
        """Save one durable piece of knowledge for later (written to the notes, then indexed).

        Use it when a decision is made, a pitfall is found, or a follow-up must not be lost.
        Not for routine progress or chit-chat.

        Args:
            kind: one of "decision", "gotcha", "fact", "todo", "question".
            text: one clear sentence, e.g. "Use FalkorDB for the knowledge graph".
            why: optional reason, e.g. "Cypher and vector search in one small container".
        """
        return await call(lambda: remember.remember(kg, kind, text, why))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        ok = await anyio.to_thread.run_sync(kg.ping)
        last = await anyio.to_thread.run_sync(kg.last_run) if ok else None
        return JSONResponse({"status": "ok" if ok else "degraded", "falkordb": ok, "graph": kg.s.graph,
                             "last_ingest": last}, status_code=200 if ok else 503)

    return mcp


def ingest_loop(kg: KG, interval: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            ingest.run(kg, log=log.info)
            wait = max(interval, 30)
        except Exception as e:  # e.g. FalkorDB container not up yet after login: retry soon
            log.warning("ingest failed: %s", e)
            wait = min(max(interval, 30), 60)
        stop.wait(wait)


def serve(kg: KG | None = None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("mcp", "httpx"):  # one line per request otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)
    kg = kg or KG()
    mcp = build(kg)
    stop = threading.Event()
    if kg.s.ingest_interval > 0:
        threading.Thread(target=ingest_loop, args=(kg, kg.s.ingest_interval, stop), daemon=True,
                         name="kg-ingest").start()
    log.info("kg MCP on http://%s:%s/mcp (graph %s, vault %s)", kg.s.mcp_host, kg.s.mcp_port, kg.s.graph, kg.s.vault)
    try:
        uvicorn.run(mcp.streamable_http_app(), host=kg.s.mcp_host, port=kg.s.mcp_port, log_level="warning")
    finally:
        stop.set()
