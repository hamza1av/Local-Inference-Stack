"""Tools from MCP servers, configured in `agents/mcp_servers.toml`.

Each `[servers.<name>]` entry says where the server is and which graph route gets its tools:

    [servers.kg]
    url   = "http://127.0.0.1:${KG_MCP_PORT}/mcp"   # ${VAR} expanded from the environment
    route = "knowledge"                              # chat|knowledge|documents|data|memory|<new>
    tools = ["kg_find", "kg_context", "kg_tags"]     # allow-list (keep it small for a 9B model)
    # description = "..."                            # required when `route` is a new route name

A server with a *new* route name adds that route to the router (the description becomes the
router's option text), so your own MCP server gets its own branch in the graph.

Tool lists are fetched lazily and cached; a server that is down just yields no tools (the route
then answers without them and says so) - it never breaks the agent.
"""

from __future__ import annotations

import logging
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.tools import BaseTool

from ..config import settings

log = logging.getLogger(__name__)

_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand(s: str) -> str:
    s_ = settings()
    defaults = {"KG_MCP_PORT": str(s_.kg_mcp_port), "RAG_MCP_PORT": str(s_.rag_mcp_port)}
    return _VAR.sub(lambda m: os.environ.get(m[1]) or m[2] or defaults.get(m[1], ""), s)


@dataclass
class McpServer:
    name: str
    url: str
    route: str
    tools: list[str] = field(default_factory=list)  # empty = all tools of the server
    description: str | None = None
    transport: str = "streamable_http"
    headers: dict[str, str] = field(default_factory=dict)

    def connection(self) -> dict:
        c: dict = {"url": self.url, "transport": self.transport}
        if self.headers:
            c["headers"] = self.headers
        return c


def load_config(path: Path | str | None = None) -> list[McpServer]:
    p = Path(path or settings().mcp_config)
    if not p.is_file():
        return []
    data = tomllib.loads(p.read_text())
    out = []
    for name, cfg in (data.get("servers") or {}).items():
        if cfg.get("enabled", True) is False:
            continue
        out.append(
            McpServer(
                name=name,
                url=_expand(cfg["url"]),
                route=cfg.get("route", name),
                tools=list(cfg.get("tools", [])),
                description=cfg.get("description"),
                transport=cfg.get("transport", "streamable_http"),
                headers={k: _expand(v) for k, v in (cfg.get("headers") or {}).items()},
            )
        )
    return out


class McpTools:
    """Loads (and caches for `ttl` seconds) the tools of each configured MCP server."""

    def __init__(self, servers: list[McpServer] | None = None, ttl: float = 60.0):
        self.servers = load_config() if servers is None else servers
        self.ttl = ttl
        self._cache: dict[str, tuple[float, list[BaseTool]]] = {}

    def routes(self) -> dict[str, str | None]:
        """route name -> description (None for built-in routes)."""
        return {s.route: s.description for s in self.servers}

    async def server_tools(self, server: McpServer) -> list[BaseTool]:
        hit = self._cache.get(server.name)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        from langchain_mcp_adapters.client import MultiServerMCPClient  # heavy-ish, import lazily

        try:
            client = MultiServerMCPClient({server.name: server.connection()})
            tools = await client.get_tools(server_name=server.name)
        except Exception as e:  # noqa: BLE001 - server down, wrong URL, ...
            log.warning("MCP server %s (%s) unavailable: %s", server.name, server.url, _root_cause(e))
            # cache failures briefly so a dead server doesn't add latency to every turn
            self._cache[server.name] = (time.monotonic() - self.ttl + 10, [])
            return []
        if server.tools:
            tools = [t for t in tools if t.name in server.tools]
        self._cache[server.name] = (time.monotonic(), tools)
        return tools

    async def for_route(self, route: str) -> tuple[list[BaseTool], list[str]]:
        """(tools, names of servers that were unreachable) for one route."""
        tools, down = [], []
        for s in self.servers:
            if s.route != route:
                continue
            got = await self.server_tools(s)
            if not got:
                down.append(s.name)
            tools.extend(got)
        return tools, down


def _root_cause(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"
