"""Real smoke tests: examples and server against a local model, example 03 against my_mcp_server.py.

The `ollama` tests need an OpenAI-compatible server with a tool-calling model. Defaults:
Ollama at 127.0.0.1:11434 with `lfm3:1.7b`; override with SMOKE_BASE_URL / SMOKE_MODEL /
SMOKE_TASK_MODEL. They are skipped when the server is not reachable.

    uv run pytest -m ollama            # only these
    uv run pytest -m "not ollama"      # everything else (fast, no model needed)
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from stack_agents import config

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:11434/v1")
MODEL = os.environ.get("SMOKE_MODEL", "lfm3:1.7b")
TASK = os.environ.get("SMOKE_TASK_MODEL", MODEL)


def _up() -> bool:
    try:
        ids = [m["id"] for m in httpx.get(f"{BASE}/models", timeout=2).json()["data"]]
    except Exception:  # noqa: BLE001
        return False
    return any(i == MODEL or i.startswith(MODEL) for i in ids)


needs_model = pytest.mark.skipif(not _up(), reason=f"{MODEL} not available at {BASE}")


def load_example(name: str):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), ROOT / "examples" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def model_env(monkeypatch, stack_db):
    monkeypatch.setenv("AGENTS_LLM_BASE_URL", BASE)
    monkeypatch.setenv("AGENTS_LLM_API_KEY", "smoke")
    monkeypatch.setenv("AGENTS_CHAT_MODEL", MODEL)
    monkeypatch.setenv("AGENTS_TASK_MODEL", TASK)
    monkeypatch.setenv("AGENTS_DECIDE_URL", "http://127.0.0.1:9/decisions")  # decision model off -> fallback
    config.settings.cache_clear()
    return stack_db


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def units_server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "examples" / "my_mcp_server.py"), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.2)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def run_example3(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "examples" / "03_mcp_tools.py"), *args],
                          capture_output=True, text=True, timeout=180, env={**os.environ})


# ---------------------------------------------------------------- no model needed


def test_example3_lists_and_calls_my_mcp_server(units_server):
    out = run_example3("--url", f"units={units_server}", "--list")
    assert out.returncode == 0, out.stderr
    assert "convert_units(value, from_unit, to_unit)" in out.stdout and "days_between" in out.stdout
    out = run_example3("--url", f"units={units_server}", "--call", "convert_units",
                       '{"value": 100, "from_unit": "C", "to_unit": "F"}')
    assert out.stdout.strip() == "100 C = 212 F"


async def test_graph_route_loads_mcp_tools(units_server, tmp_path, monkeypatch):
    """A custom route from mcp_servers.toml becomes a router option with the server's tools."""
    cfg = tmp_path / "mcp.toml"
    cfg.write_text(f'[servers.units]\nurl = "{units_server}"\nroute = "units"\ndescription = "unit conversion"\n')
    monkeypatch.setenv("AGENTS_MCP_CONFIG", str(cfg))
    config.settings.cache_clear()
    from stack_agents.graph import default_deps

    deps = default_deps()
    assert deps.routes["units"] == "unit conversion"
    tools, down = await deps.tools("units")
    assert sorted(t.name for t in tools) == ["convert_units", "days_between"] and down == []


# ---------------------------------------------------------------- real model


@pytest.mark.ollama
@needs_model
def test_example1_raw_openai_loop(model_env):
    ex = load_example("01_raw_openai_tools.py")
    answer = ex.ask("How many rows are in the gw_requests table? Use sql_query.", verbose=False)
    assert "3" in answer


@pytest.mark.ollama
@needs_model
def test_example2_langchain_tools(model_env):
    import sqlite3

    ex = load_example("02_langchain_tools.py")
    ex.ask("Remember that the spare key is in the blue box.", verbose=False)
    rows = sqlite3.connect(model_env).execute("SELECT text FROM agent_notes").fetchall()
    assert rows and "blue box" in rows[0][0].lower()


@pytest.mark.ollama
@needs_model
def test_example3_agent_with_my_mcp_server(model_env, units_server):
    out = run_example3("--url", f"units={units_server}", "How many kilometers are 10 miles?")
    assert out.returncode == 0, out.stderr
    assert "convert_units" in out.stderr and "16.09" in out.stdout


@pytest.mark.ollama
@needs_model
def test_server_against_model(model_env, tmp_path):
    from stack_agents.server import create_app

    with TestClient(create_app(checkpoint_db=str(tmp_path / "agents.db"))) as c:
        r = c.post("/v1/chat/completions", json={"model": "agent", "messages": [
            {"role": "user", "content": "Remember that the bins go out on Tuesday."}]})
        j = r.json()
        assert r.status_code == 200 and j["choices"][0]["message"]["content"]
        assert j["agent"]["fallback"] is True  # decision API is off in this test
        r = c.post("/v1/chat/completions", json={"model": "agent", "stream": True, "messages": [
            {"role": "user", "content": "Say hello in one short sentence."}]})
        assert r.text.rstrip().endswith("data: [DONE]") and '"content"' in r.text
