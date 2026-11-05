"""Read-only SQL over the shared `stack.db`, plus a trivial clock tool.

These are *plain Python functions* with hand-written OpenAI JSON schemas: the framework-free
style used by `examples/01_raw_openai_tools.py`. The graph's `data` route wraps the very same
functions as LangChain tools (see `langchain_tools()` at the bottom).

Safety layers for `sql_query` (a model writes the SQL, so assume it can be anything):

1. the file is opened with the URI `file:...?mode=ro`  -> SQLite itself refuses writes
2. only one statement, starting with SELECT or WITH     -> no PRAGMA/ATTACH/DELETE...
3. an authorizer callback allows only read actions      -> defence in depth
4. `rag_vec` (sqlite-vec virtual table) is hidden       -> readers without the extension must not touch it
5. rows are capped and long cells truncated             -> the 9B model has a small context
6. a progress handler aborts queries running > 5 s
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from ..config import settings

HIDDEN_TABLES = re.compile(r"^(rag_vec|sqlite_stat|sqlite_sequence)", re.I)
_READ_ONLY_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}
_CELL_MAX = 300


def _connect(db: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db or settings().stack_db)
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql).strip()


def _authorizer(action: int, arg1, arg2, dbname, source) -> int:  # noqa: ANN001
    if action not in _READ_ONLY_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and arg1 and arg1.lower().startswith("rag_vec"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def sql_schema(db: Path | str | None = None) -> str:
    """Compact schema: one line per table, `table(col type, ...)`. Hidden tables are skipped."""
    try:
        con = _connect(db)
    except (FileNotFoundError, sqlite3.Error) as e:
        return f"error: {e}"
    with con:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]
        lines = []
        for name in names:
            if HIDDEN_TABLES.match(name):
                continue
            cols = con.execute(f'PRAGMA table_info("{name}")').fetchall()
            lines.append(f"{name}(" + ", ".join(f"{c[1]} {c[2]}".strip() for c in cols) + ")")
    con.close()
    return "\n".join(lines) or "(no tables)"


def sql_query(query: str, db: Path | str | None = None, row_cap: int | None = None) -> str:
    """Run one read-only SELECT/WITH statement and return a small text table (or an error string).

    Errors are *returned*, not raised: the model reads them and can fix its query.
    """
    cap = row_cap or settings().sql_row_cap
    sql = _strip_comments(query).rstrip(";").strip()
    if not re.match(r"^(select|with)\b", sql, re.I):
        return "error: only SELECT or WITH queries are allowed"
    if ";" in sql:
        return "error: only one statement is allowed"
    try:
        con = _connect(db)
    except (FileNotFoundError, sqlite3.Error) as e:
        return f"error: {e}"
    deadline = time.monotonic() + 5.0
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    con.set_authorizer(_authorizer)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(cap + 1)
    except sqlite3.Error as e:
        return f"error: {e}"
    finally:
        con.close()
    more = len(rows) > cap
    rows = rows[:cap]

    def cell(v: object) -> str:
        s = "NULL" if v is None else str(v)
        return s if len(s) <= _CELL_MAX else s[:_CELL_MAX] + "…"

    out = [" | ".join(cols)] + [" | ".join(cell(v) for v in r) for r in rows]
    out.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''}{', truncated' if more else ''})")
    return "\n".join(out)


def get_time() -> str:
    """Current local date and time with weekday and timezone."""
    now = datetime.now().astimezone()
    return now.strftime("%A %Y-%m-%d %H:%M:%S %Z (UTC%z)")


# --------------------------------------------------------------------------- OpenAI tool schemas
# This is what the model sees. Keep descriptions short: every token is prompt on a 9B model.

OPENAI_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "sql_query",
            "description": (
                "Run ONE read-only SQLite SELECT on the stack database and get rows back. "
                "Call sql_schema first if you do not know the tables."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "A single SELECT or WITH statement"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql_schema",
            "description": "List the tables and columns of the stack database.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current local date and time.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

#: name -> python callable; the raw loop dispatches tool_calls through this
DISPATCH = {"sql_query": sql_query, "sql_schema": sql_schema, "get_time": get_time}


def langchain_tools(db: Path | str | None = None) -> list:
    """The same functions as LangChain tools (used by the graph's `data` route)."""
    from langchain_core.tools import StructuredTool

    def _q(query: str) -> str:
        return sql_query(query, db=db)

    def _s() -> str:
        return sql_schema(db=db)

    spec = {t["function"]["name"]: t["function"]["description"] for t in OPENAI_TOOLS}
    return [
        StructuredTool.from_function(_q, name="sql_query", description=spec["sql_query"]),
        StructuredTool.from_function(_s, name="sql_schema", description=spec["sql_schema"]),
        StructuredTool.from_function(get_time, name="get_time", description=spec["get_time"]),
    ]
