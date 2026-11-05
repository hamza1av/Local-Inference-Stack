import sqlite3

import pytest

from stack_agents.tools import mcp as mcp_tools
from stack_agents.tools.notes import list_notes, save_note, search_notes
from stack_agents.tools.sql import DISPATCH, OPENAI_TOOLS, get_time, langchain_tools, sql_query, sql_schema


def test_schema_hides_vec_table(stack_db):
    s = sql_schema()
    assert "gw_requests(" in s and "latency_ms INTEGER" in s
    assert "rag_vec" not in s


def test_select_works_and_is_capped(stack_db):
    out = sql_query("SELECT model, count(*) n FROM gw_requests GROUP BY model ORDER BY model")
    assert out.splitlines()[0] == "model | n"
    assert "chat | 2" in out and "(2 rows)" in out
    out = sql_query("SELECT * FROM gw_requests", row_cap=1)
    assert "(1 row, truncated)" in out


@pytest.mark.parametrize(
    "q",
    [
        "DELETE FROM gw_requests",
        "UPDATE gw_requests SET model='x'",
        "SELECT 1; DROP TABLE gw_requests",
        "PRAGMA writable_schema=1",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "WITH x AS (SELECT 1) DELETE FROM gw_requests",
        "SELECT * FROM rag_vec",
    ],
)
def test_writes_and_hidden_tables_are_refused(stack_db, q):
    out = sql_query(q)
    assert out.startswith("error"), out
    assert sqlite3.connect(stack_db).execute("SELECT count(*) FROM gw_requests").fetchone()[0] == 3


def test_comments_are_stripped_and_errors_returned(stack_db):
    assert "(1 row)" in sql_query("-- count\nSELECT count(*) FROM gw_requests /* all */;")
    assert sql_query("SELECT nope FROM gw_requests").startswith("error: no such column")


def test_missing_db_is_an_error_string(isolated_env):
    assert sql_query("SELECT 1").startswith("error: database not found")


def test_openai_schemas_match_dispatch():
    assert {t["function"]["name"] for t in OPENAI_TOOLS} == set(DISPATCH)
    assert [t.name for t in langchain_tools()] == ["sql_query", "sql_schema", "get_time"]
    assert get_time()[:4].isalpha()


def test_notes_roundtrip(isolated_env):
    assert save_note.invoke({"text": "Buy oat milk", "tags": "shopping"}, {"configurable": {"thread_id": "t1"}}) == "saved note #1"
    save_note.invoke({"text": "Dentist on Friday at 9"})
    assert "Dentist" in list_notes.invoke({"limit": 1}) and "oat" not in list_notes.invoke({"limit": 1})
    hit = search_notes.invoke({"query": "milk shopping"})
    assert "#1" in hit and "Dentist" not in hit
    assert search_notes.invoke({"query": "nothing-like-this"}) == "no notes"
    row = sqlite3.connect(isolated_env / "stack.db").execute("SELECT thread_id FROM agent_notes WHERE id=1").fetchone()
    assert row == ("t1",)
    # the injected config parameter is hidden from the model
    assert "config" not in save_note.tool_call_schema.model_json_schema()["properties"]


def test_mcp_config_expansion(tmp_path, monkeypatch):
    cfg = tmp_path / "m.toml"
    cfg.write_text(
        '[servers.kg]\nurl = "http://127.0.0.1:${KG_MCP_PORT:-8090}/mcp"\nroute = "knowledge"\ntools = ["kg_find"]\n'
        '[servers.units]\nurl = "http://127.0.0.1:${MY_PORT:-8099}/mcp"\nroute = "units"\ndescription = "unit maths"\n'
        '[servers.off]\nurl = "http://x"\nenabled = false\n'
    )
    monkeypatch.setenv("KG_MCP_PORT", "9999")
    servers = mcp_tools.load_config(cfg)
    assert [s.name for s in servers] == ["kg", "units"]
    assert servers[0].url == "http://127.0.0.1:9999/mcp" and servers[1].url == "http://127.0.0.1:8099/mcp"
    assert mcp_tools.McpTools(servers).routes() == {"knowledge": None, "units": "unit maths"}


async def test_mcp_server_down_yields_no_tools():
    m = mcp_tools.McpTools([mcp_tools.McpServer("kg", "http://127.0.0.1:9/mcp", "knowledge")])
    tools, down = await m.for_route("knowledge")
    assert tools == [] and down == ["kg"]
