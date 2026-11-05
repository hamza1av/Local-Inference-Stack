"""Notes/memory tools in the LangChain `@tool` style (table `agent_notes` in stack.db).

`@tool` turns a typed, docstring'd Python function into a tool: the function name becomes the
tool name, the docstring the description, the type hints the JSON schema. Compare with the
hand-written schemas in `sql.py` - same result, less boilerplate.

A parameter typed `RunnableConfig` is *injected* by LangChain and hidden from the model; we use
it to record which conversation (thread) wrote a note.
"""

from __future__ import annotations

from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from ..db import connect as _db


def _thread(config: RunnableConfig | None) -> str | None:
    return ((config or {}).get("configurable") or {}).get("thread_id")


def _fmt(rows: list[tuple]) -> str:
    if not rows:
        return "no notes"
    return "\n".join(f"#{i} [{ts[:16]}]{' (' + tags + ')' if tags else ''} {text}" for i, ts, text, tags in rows)


@tool
def save_note(text: str, tags: str = "", config: RunnableConfig = None) -> str:  # type: ignore[assignment]
    """Save a note to long-term memory. tags: optional comma-separated words."""
    with _db() as con:
        cur = con.execute(
            "INSERT INTO agent_notes(ts, thread_id, text, tags) VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), _thread(config), text.strip(), tags.strip()),
        )
    return f"saved note #{cur.lastrowid}"


@tool
def list_notes(limit: int = 10) -> str:
    """List the most recent notes."""
    with _db() as con:
        rows = con.execute(
            "SELECT id, ts, text, tags FROM agent_notes ORDER BY id DESC LIMIT ?", (max(1, min(limit, 50)),)
        ).fetchall()
    return _fmt(rows)


@tool
def search_notes(query: str, limit: int = 5) -> str:
    """Search notes by words (matches text and tags)."""
    words = [w for w in query.lower().split() if len(w) > 1][:8] or [query.lower()]
    # rank by how many query words occur; LIKE is plenty for a personal notes table
    score = " + ".join("(instr(lower(text || ' ' || tags), ?) > 0)" for _ in words)
    with _db() as con:
        rows = con.execute(
            f"SELECT id, ts, text, tags FROM (SELECT *, {score} AS s FROM agent_notes) "
            "WHERE s > 0 ORDER BY s DESC, id DESC LIMIT ?",
            (*words, max(1, min(limit, 20))),
        ).fetchall()
    return _fmt(rows)


NOTE_TOOLS = [save_note, list_notes, search_notes]
