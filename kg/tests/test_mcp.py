"""MCP smoke test over real streamable HTTP (server in a background thread)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from stack_kg import ingest
from stack_kg.server import build


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(kg):
    ingest.run(kg, log=lambda *_: None)
    kg.s.mcp_port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(build(kg).streamable_http_app(), host="127.0.0.1",
                                        port=kg.s.mcp_port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{kg.s.mcp_port}"
    for _ in range(50):
        try:
            httpx.get(f"{base}/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base
    srv.should_exit = True
    t.join(5)


def test_health(server):
    r = httpx.get(f"{server}/health")
    assert r.status_code == 200 and r.json()["falkordb"] is True and r.json()["status"] == "ok"


def test_rejects_foreign_host_header(server):
    r = httpx.post(f"{server}/mcp", headers={"Host": "evil.example", "accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 421


def test_tools_over_http(server, kg):
    async def run():
        async with streamable_http_client(f"{server}/mcp") as (read, write, _):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                assert "kg_find" in (init.instructions or "")
                tools = {t.name: t for t in (await s.list_tools()).tools}
                assert set(tools) == {"kg_find", "kg_context", "kg_tags", "kg_stats", "kg_cypher", "kg_remember"}
                assert set(tools["kg_find"].inputSchema["required"]) == {"query"}

                async def text(tool, **args):
                    res = await s.call_tool(tool, args)
                    assert not res.isError, res
                    return res.content[0].text

                assert "Gateway" in await text("kg_find", query="rust gateway")
                assert "## Links to" in await text("kg_context", name="Local Inference Stack")
                assert "topic:" in await text("kg_tags")
                assert "Note 9" in await text("kg_stats")
                assert '"c": 9' in await text("kg_cypher", query="MATCH (n:Note) RETURN count(n) AS c")
                assert (await text("kg_cypher", query="CREATE (:X)")).startswith("Error")
                out = await text("kg_remember", kind="gotcha", text="Ollama must be running for vector search",
                                 why="query embeddings come from Ollama")
                assert out.startswith("Remembered")
                assert "Ollama must be running" in await text("kg_find", query="ollama vector search", tag="gotcha")
                assert (await text("kg_remember", kind="nonsense", text="x")).startswith("Error")

    asyncio.run(run())
