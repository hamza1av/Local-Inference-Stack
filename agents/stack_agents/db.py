"""Our tables in the shared stack.db (prefix `agent_`, see docs/ARCHITECTURE.md).

LangGraph checkpoints live in a separate file ($STACK_DATA/agents.db) because their table
names cannot be prefixed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    thread_id TEXT,
    text      TEXT NOT NULL,
    tags      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    thread_id   TEXT,
    route       TEXT NOT NULL,
    confidence  REAL,
    fallback    INTEGER NOT NULL DEFAULT 0,  -- 1 = router used the task-model fallback
    tools       TEXT NOT NULL DEFAULT '',    -- comma separated tool names called
    latency_ms  INTEGER,
    error       TEXT,
    trace_id    TEXT
);
CREATE INDEX IF NOT EXISTS agent_runs_ts ON agent_runs(ts);
"""

_ready: set[Path] = set()


@contextmanager
def connect(db: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Read-write connection with the stack pragmas; commits on success, always closes."""
    path = Path(db or settings().stack_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        if path not in _ready:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            _ready.add(path)
        with con:
            yield con
    finally:
        con.close()


def log_run(*, thread_id: str | None, route: str, confidence: float | None, fallback: bool,
            tools: list[str], latency_ms: int, error: str | None = None, trace_id: str | None = None) -> None:
    try:
        with connect() as con:
            con.execute(
                "INSERT INTO agent_runs(ts, thread_id, route, confidence, fallback, tools, latency_ms, error, trace_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), thread_id, route, confidence,
                 int(fallback), ",".join(tools), latency_ms, error, trace_id),
            )
    except sqlite3.Error:  # bookkeeping must never break a chat turn
        pass
