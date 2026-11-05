"""MCP smoke test: run `stack-rag serve` as a subprocess and talk JSON-RPC over streamable HTTP."""

import os
import shutil
import subprocess
import sys
import time

import httpx
import pytest
from conftest import EXAMPLES, free_port, needs_embed

pytestmark = needs_embed

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def server(tmp_path):
    port = free_port()
    inbox = tmp_path / "inbox"
    env = {
        **os.environ,
        "STACK_DB": str(tmp_path / "stack.db"),
        "RAG_INBOX": str(inbox),
        "RAG_WATCH_INTERVAL": "1",
        "RAG_EMBED_VIA_GATEWAY": "false",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "stack_rag.cli", "serve", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            pytest.fail(proc.stdout.read())
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield base, inbox
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


class Rpc:
    def __init__(self, base: str):
        self.url = f"{base}/mcp"
        self.n = 0
        self.headers = dict(HEADERS)

    def __call__(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        r = httpx.post(
            self.url, headers=self.headers, timeout=120,
            json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "error" not in body, body
        return body["result"]

    def tool(self, name: str, **args) -> str:
        res = self("tools/call", {"name": name, "arguments": args})
        assert not res.get("isError"), res
        return "\n".join(c["text"] for c in res["content"] if c["type"] == "text")


def test_mcp_tools_and_watcher(server):
    base, inbox = server
    rpc = Rpc(base)
    init = rpc(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
    )
    assert init["serverInfo"]["name"] == "rag"
    rpc.headers["mcp-protocol-version"] = init["protocolVersion"]

    tools = {t["name"]: t for t in rpc("tools/list")["tools"]}
    assert set(tools) == {"rag_search", "rag_list_documents", "rag_add_text"}
    assert "cite" in tools["rag_search"]["description"]
    assert set(tools["rag_search"]["inputSchema"]["properties"]) == {"query", "k"}

    assert "empty" in rpc.tool("rag_list_documents")
    out = rpc.tool("rag_add_text", title="Parking", text="The car is parked on level 3, spot 42, next to the lift.")
    assert out.startswith("Saved") and "text:parking" in out

    hits = rpc.tool("rag_search", query="where did I park the car", k=3)
    assert hits.startswith("1 passages") or "[1] Parking" in hits
    assert "level 3" in hits

    # watcher: a file dropped into the inbox becomes searchable without any call
    shutil.copy(EXAMPLES / "sourdough.md", inbox / "sourdough.md")
    old = time.time() - 30
    os.utime(inbox / "sourdough.md", (old, old))
    for _ in range(150):
        if httpx.get(f"{base}/health").json()["documents"] == 2:
            break
        time.sleep(0.2)
    else:
        pytest.fail("watcher did not ingest the inbox file")
    listing = rpc.tool("rag_list_documents")
    assert "2 documents" in listing and "Sourdough Bread Notes" in listing
    hits = rpc.tool("rag_search", query="starter feeding schedule", k=2)
    assert "[1] Sourdough Bread Notes" in hits and str(inbox) in hits

    # removed from the inbox -> removed from the index
    (inbox / "sourdough.md").unlink()
    for _ in range(150):
        if httpx.get(f"{base}/health").json()["documents"] == 1:
            break
        time.sleep(0.2)
    else:
        pytest.fail("watcher did not remove the deleted file")
