"""MCP server (streamable HTTP on RAG_MCP_PORT, path /mcp) + GET /health + inbox watcher.

Works with the MCP Python SDK 2.x (`MCPServer`) and 1.x (`FastMCP`).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import anyio

from .ask import format_hits
from .config import Config
from .index import Index
from .retrieval import hybrid

log = logging.getLogger("stack_rag.server")

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _MCP

    _V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCP

    _V2 = False
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

INSTRUCTIONS = (
    "Search the user's own document collection (notes, manuals, PDFs, saved snippets). "
    "Use rag_search before answering questions about the user's documents and cite sources as [n]. "
    "Use rag_list_documents to see what is indexed and rag_add_text to save new text for later."
)


def _security(cfg: Config) -> TransportSecuritySettings:
    extra = [h.strip() for h in os.environ.get("RAG_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if "*" in extra:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "host.docker.internal:*", "host.orb.internal:*", *extra]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts],
    )


def build(idx: Index):
    """Create the MCP server object with the rag tools bound to `idx`."""
    cfg = idx.cfg
    if _V2:
        mcp = _MCP("rag", instructions=INSTRUCTIONS)
    else:
        mcp = _MCP(
            "rag",
            instructions=INSTRUCTIONS,
            host=cfg.mcp_host,
            port=cfg.mcp_port,
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=_security(cfg),
        )

    @mcp.tool(structured_output=False)
    async def rag_search(query: str, k: int = 5) -> str:
        """Search the user's documents and return the most relevant passages with sources.

        Use this whenever the user asks about something that may be written in their own
        documents, notes, manuals or saved snippets. Write `query` as a short natural-language
        question or a few keywords (e.g. "backup schedule for the NAS"). Each result is
        numbered [1], [2], ... and shows the document title, section and file path, followed
        by the passage text. Base your answer on these passages and cite them as [1], [2].
        If nothing relevant comes back, say the documents do not cover it; try once more
        with different keywords before giving up.

        Args:
            query: what to look for, in plain words.
            k: number of passages to return (1-10, default 5).
        """
        k = max(1, min(int(k), 10))
        hits = await anyio.to_thread.run_sync(lambda: hybrid(idx, query, k))
        if not hits:
            return f'No passages found for "{query}".'
        return f'{len(hits)} passages for "{query}":\n\n' + format_hits(hits)

    @mcp.tool(structured_output=False)
    async def rag_list_documents() -> str:
        """List every document in the user's searchable collection.

        Use this to see which documents exist (for example when the user asks "what do you
        know about" or "which files do I have"). Returns one line per document:
        `[id] title — path (N chunks)`. To read content, call rag_search with a query.
        """
        docs = await anyio.to_thread.run_sync(idx.list_documents)
        if not docs:
            return "The collection is empty."
        lines = [f"[{d['id']}] {d['title']} — {d['path']} ({d['chunks']} chunks)" for d in docs[:300]]
        more = f"\n… and {len(docs) - 300} more" if len(docs) > 300 else ""
        return f"{len(docs)} documents:\n" + "\n".join(lines) + more

    @mcp.tool(structured_output=False)
    async def rag_add_text(title: str, text: str) -> str:
        """Save a piece of text into the user's searchable collection.

        Use this only when the user asks you to remember, save or store something (a note,
        a summary, meeting minutes, an answer worth keeping). The text becomes searchable
        with rag_search right away. Saving again with the same title replaces the earlier
        version.

        Args:
            title: short descriptive title, e.g. "Wi-Fi setup notes".
            text: the content to save; markdown headings are fine.
        """
        if not text.strip():
            return "Nothing saved: the text is empty."
        res = await anyio.to_thread.run_sync(lambda: idx.add_text(title.strip() or "untitled", text))
        verb = {"added": "Saved", "updated": "Updated", "unchanged": "Already saved (unchanged):"}.get(res.status, res.status)
        return f'{verb} "{res.title}" as document {res.doc_id} ({res.chunks} chunks), key {res.path}.'

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        try:
            st = await anyio.to_thread.run_sync(idx.stats)
            return JSONResponse(
                {"status": "ok", "documents": st["documents"], "chunks": st["chunks"],
                 "embed_model": st["embed_model"], "embed_dim": st["embed_dim"], "inbox": st["inbox"]}
            )
        except Exception as e:  # pragma: no cover
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    return mcp


class Watcher(threading.Thread):
    """Polls the inbox and syncs it into the index (add/update by hash, remove deleted)."""

    def __init__(self, idx: Index, inbox: Path, interval: float):
        super().__init__(name="rag-inbox", daemon=True)
        self.idx, self.inbox, self.interval = idx, inbox, interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        log.info("watching %s every %.0fs", self.inbox, self.interval)
        while not self.stop_event.is_set():
            try:
                rep = self.idx.sync_dir(self.inbox)
                changes = {k: len(v) for k, v in rep.items() if v}
                if changes:
                    log.info("inbox sync: %s", changes)
            except Exception:
                log.exception("inbox sync failed")
            self.stop_event.wait(self.interval)

    def stop(self) -> None:
        self.stop_event.set()


def serve(cfg: Config | None = None, watch: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "mcp.server.streamable_http", "mcp.server.lowlevel.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = cfg or Config.from_env()
    idx = Index(cfg)
    watcher = Watcher(idx, cfg.inbox, cfg.watch_interval) if watch else None
    if watcher:
        watcher.start()
    mcp = build(idx)
    log.info("rag MCP on http://%s:%s/mcp (db %s)", cfg.mcp_host, cfg.mcp_port, cfg.db_path)
    try:
        if _V2:
            mcp.run(
                transport="streamable-http",
                host=cfg.mcp_host,
                port=cfg.mcp_port,
                streamable_http_path="/mcp",
                stateless_http=True,
                json_response=True,
                transport_security=_security(cfg),
            )
        else:
            mcp.run(transport="streamable-http")
    finally:
        if watcher:
            watcher.stop()
            watcher.join(timeout=30)
        idx.close()
