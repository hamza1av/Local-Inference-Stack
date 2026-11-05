"""Shared fixtures: isolated env (temp STACK_DATA/DB), fake chat models, fake decision client."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk

from stack_agents import config


class FakeChat(GenericFakeChatModel):
    """Scripted chat model that also accepts bind_tools() (records the tools it was given)."""

    bound_tools: list = []
    seen: list = []

    def bind_tools(self, tools: Any, **kw: Any):  # noqa: ARG002
        self.bound_tools = [getattr(t, "name", t) for t in tools]
        return self

    def _generate(self, messages: list[BaseMessage], *a: Any, **kw: Any):
        self.seen.append(messages)
        return super()._generate(messages, *a, **kw)

    def _stream(self, messages: list[BaseMessage], *a: Any, **kw: Any):
        """Stream content word by word; tool calls arrive as one chunk (like real servers)."""
        msg = self._generate(messages, *a, **kw).generations[0].message
        words = msg.content.split(" ") if msg.content else []
        for i, w in enumerate(words):
            yield ChatGenerationChunk(message=AIMessageChunk(content=w if i == 0 else " " + w, id=msg.id))
        if getattr(msg, "tool_calls", None):
            yield ChatGenerationChunk(message=AIMessageChunk(content="", id=msg.id, tool_call_chunks=[
                {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                for i, tc in enumerate(msg.tool_calls)
            ]))


def fake_chat(*replies: str | AIMessage) -> FakeChat:
    msgs = [r if isinstance(r, AIMessage) else AIMessage(r) for r in replies]
    return FakeChat(messages=iter(msgs), seen=[])


def tool_call(name: str, args: dict, id: str = "call_1", content: str = "") -> AIMessage:
    return AIMessage(content=content, tool_calls=[{"name": name, "args": args, "id": id, "type": "tool_call"}])


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets its own STACK_DATA and no tracing; nothing touches the real data dir."""
    monkeypatch.setenv("STACK_ROOT", str(tmp_path / "no-repo"))  # -> no .env loaded
    monkeypatch.setenv("STACK_DATA", str(tmp_path))
    monkeypatch.setenv("STACK_DB", str(tmp_path / "stack.db"))
    monkeypatch.setenv("AGENTS_TRACING", "0")
    monkeypatch.setenv("AGENTS_MCP_CONFIG", str(tmp_path / "none.toml"))
    config.settings.cache_clear()
    yield tmp_path
    config.settings.cache_clear()


@pytest.fixture
def stack_db(isolated_env: Path) -> Path:
    """A stack.db with a few gw_requests rows (the gateway's table) and a fake rag_vec table."""
    path = isolated_env / "stack.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE gw_requests (id INTEGER PRIMARY KEY, ts TEXT, client TEXT, model TEXT, backend TEXT,
            endpoint TEXT, stream INTEGER, status INTEGER, queue_ms INTEGER, latency_ms INTEGER, ttft_ms INTEGER,
            prompt_tokens INTEGER, completion_tokens INTEGER, error TEXT, trace_id TEXT);
        INSERT INTO gw_requests(ts, client, model, status, latency_ms, prompt_tokens, completion_tokens)
            VALUES ('2026-11-01T10:00:00', 'owui', 'chat', 200, 1200, 100, 50),
                   ('2026-11-01T11:00:00', 'stack', 'task', 200, 300, 40, 5),
                   ('2026-11-02T09:00:00', 'owui', 'chat', 500, 50, 10, 0);
        CREATE TABLE rag_vec (id INTEGER, embedding BLOB);
        """
    )
    con.commit()
    con.close()
    return path
