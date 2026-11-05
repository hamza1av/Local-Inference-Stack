import sqlite3

import httpx
from langchain_core.messages import AIMessage, HumanMessage

from stack_agents.decide import DecisionClient
from stack_agents.graph import Deps, build_graph, open_graph, run_turn, stream_turn
from stack_agents.tools.notes import NOTE_TOOLS
from stack_agents.tools.sql import langchain_tools

from .conftest import fake_chat, tool_call


def decider(route: str, confidence: float = 0.9) -> DecisionClient:
    """Decision API that always answers `route` (no fallback)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "d3", "answers": {"route": {
            "type": "choice", "choice": route, "probabilities": {route: confidence}, "confidence": confidence}}})

    return DecisionClient("http://x/decisions", "k", transport=httpx.MockTransport(handler), task_llm=fake_chat())


def deps(llm, route="chat", confidence=0.9, down=()):
    local = {"data": langchain_tools(), "memory": list(NOTE_TOOLS)}

    async def provider(r):
        return local.get(r, []), list(down) if r in ("knowledge", "documents") else []

    return Deps(llm=llm, decider=decider(route, confidence), tools=provider, max_steps=3)


async def test_chat_route_answers_without_tools():
    llm = fake_chat("Hello there!")
    res = await run_turn(build_graph(deps(llm, "chat")), "hi")
    assert res.route == "chat" and res.answer == "Hello there!" and res.tools == []
    assert llm.bound_tools == []


async def test_low_confidence_goes_to_chat():
    llm = fake_chat("plain answer")
    res = await run_turn(build_graph(deps(llm, "data", confidence=0.2)), "how many requests?")
    assert res.route == "chat" and res.confidence == 0.2 and res.answer == "plain answer"


async def test_data_route_runs_sql_tool_loop(stack_db):
    llm = fake_chat(
        tool_call("sql_query", {"query": "SELECT count(*) AS n FROM gw_requests"}),
        "There were 3 requests.",
    )
    res = await run_turn(build_graph(deps(llm, "data")), "How many requests went through the gateway?")
    assert res.route == "data"
    assert llm.bound_tools == ["sql_query", "sql_schema", "get_time"]  # only this route's tools
    assert res.tools[0]["name"] == "sql_query" and "3" in res.tools[0]["result"]
    assert res.answer == "There were 3 requests."
    # the tool result was fed back to the model
    assert any(m.type == "tool" for m in llm.seen[-1])


async def test_memory_route_saves_note_and_logs_run(isolated_env):
    llm = fake_chat(tool_call("save_note", {"text": "Car service in March"}), "Noted.")
    res = await run_turn(build_graph(deps(llm, "memory")), "Remember: car service in March", thread_id="t9")
    assert res.answer == "Noted." and res.tools[0]["result"] == "saved note #1"
    con = sqlite3.connect(isolated_env / "stack.db")
    assert con.execute("SELECT text, thread_id FROM agent_notes").fetchone() == ("Car service in March", "t9")
    assert con.execute("SELECT route, tools, thread_id FROM agent_runs").fetchone() == ("memory", "save_note", "t9")


async def test_tool_budget_forces_final_answer(stack_db):
    llm = fake_chat(*(tool_call("get_time", {}, id=f"c{i}") for i in range(3)), "Gave up looping.")
    res = await run_turn(build_graph(deps(llm, "data")), "time?")
    assert len(res.tools) == 3 and res.answer == "Gave up looping."
    assert "Tool budget" in llm.seen[-1][-1].content


async def test_unreachable_mcp_route_degrades_to_plain_answer():
    llm = fake_chat("The knowledge service is down, but generally ...")
    res = await run_turn(build_graph(deps(llm, "knowledge", down=["kg"])), "what did I decide about X?")
    assert res.route == "knowledge" and res.answer.startswith("The knowledge service")
    assert "kg service is unreachable" in llm.seen[0][0].content
    assert any("unavailable" in s for s in res.steps)


async def test_router_fallback_when_decision_api_is_off():
    task = fake_chat('{"choice": "memory", "confidence": 0.9}')

    def off(req):
        return httpx.Response(503, json={})

    d = Deps(llm=fake_chat(tool_call("list_notes", {}), "You have no notes."),
             decider=DecisionClient("http://x", "k", transport=httpx.MockTransport(off), task_llm=task),
             tools=deps(None).tools)
    res = await run_turn(build_graph(d), "what did I note?")
    assert res.route == "memory" and res.fallback is True and res.tools[0]["result"] == "no notes"


async def test_checkpointer_keeps_thread_history(tmp_path):
    llm = fake_chat("Nice to meet you, Ada.", "Your name is Ada.")
    async with open_graph(deps(llm, "chat"), str(tmp_path / "agents.db")) as g:
        await run_turn(g, "My name is Ada.", thread_id="t1")
        # an OpenAI client resends the full history; only the new user message must be added
        hist = [HumanMessage("My name is Ada."), AIMessage("Nice to meet you, Ada."), HumanMessage("What's my name?")]
        res = await run_turn(g, hist, thread_id="t1")
        assert res.answer == "Your name is Ada."
        st = (await g.aget_state({"configurable": {"thread_id": "t1"}})).values["messages"]
        assert [m.type for m in st] == ["human", "ai", "human", "ai"]
    sent = [m.content for m in llm.seen[-1]]
    assert sent[1:] == ["My name is Ada.", "Nice to meet you, Ada.", "What's my name?"]


async def test_stream_emits_steps_and_tokens(stack_db):
    llm = fake_chat(tool_call("get_time", {}), "It is noon.")
    kinds, tokens = [], []
    async for kind, val in stream_turn(build_graph(deps(llm, "data")), "time?"):
        kinds.append(kind)
        if kind == "token":
            tokens.append(val)
    assert kinds[0] == "step" and kinds[-1] == "done"
    assert "".join(tokens) == "It is noon."
    assert val.answer == "It is noon."
