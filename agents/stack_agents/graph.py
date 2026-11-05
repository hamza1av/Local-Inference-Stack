"""The agent graph: route the user turn with a decision model, then run a small tool loop.

    START -> router --(choice question via decide())--> <route>_agent -> END
               routes: chat | knowledge | documents | data | memory | <custom from mcp_servers.toml>

* router     one `choice` question to the decision API (falls back to the `task` model).
             Confidence below AGENTS_ROUTE_MIN_CONFIDENCE -> `chat` (no tools = no harm).
* routes     each route only sees *its own* 2-4 tools (small tool sets keep a 9B model accurate):
               knowledge -> kg MCP     (kg_find, kg_context, kg_tags)
               documents -> rag MCP    (rag_search, rag_list_documents)
               data      -> sql_query, sql_schema, get_time       (stack_agents/tools/sql.py)
               memory    -> save_note, list_notes, search_notes   (stack_agents/tools/notes.py)
               chat      -> no tools
             The loop is a classic ReAct loop built from LangGraph's prebuilt `ToolNode`:
             model -> tool calls? -> ToolNode -> model ... at most AGENTS_MAX_TOOL_STEPS rounds.
* memory     the conversation is checkpointed (SQLite, $STACK_DATA/agents.db) per thread id.

Streaming: answer tokens come from LLM calls tagged `agent_answer`; route/tool steps are sent
as custom stream events (`{"step": "..."}`) - see `stream_turn()`.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from . import db, tracing
from .config import ROUTES, settings
from .decide import DecisionClient, choice

ANSWER_TAG = "agent_answer"

ROUTE_OPTIONS: dict[str, str] = {
    "chat": "Small talk, general knowledge, writing, translation, coding or anything that needs no lookup",
    "knowledge": "The user's own notes, projects, people, decisions or tickets (personal knowledge graph)",
    "documents": "Content of the user's uploaded documents, files or PDFs (document search)",
    "data": "Usage statistics of the local LLM stack (requests, models, tokens, latency, errors) or the current date/time",
    "memory": "Asks to remember/save a note, or to recall/list what was noted earlier",
}

ROUTE_PROMPTS: dict[str, str] = {
    "chat": "",
    "knowledge": "Look things up with the knowledge-graph tools before answering. Name the notes you used.",
    "documents": "Search the document library with rag_search and answer from the hits. Cite titles. "
    "If nothing relevant is found, say so.",
    "data": "Answer with SQL over the stack's SQLite database: call sql_schema first, then sql_query. "
    "gw_requests has one row per LLM request (ts, model, latency_ms, tokens, status). Report numbers plainly.",
    "memory": "Use save_note to remember things and search_notes/list_notes to recall them. Confirm briefly.",
}

BASE_PROMPT = "You are a helpful assistant running locally. Answer concisely, in the user's language. Today is {today}."


class AgentState(MessagesState):
    route: str
    confidence: float
    fallback: bool


ToolProvider = Callable[[str], Awaitable[tuple[list[BaseTool], list[str]]]]


# --------------------------------------------------------------------------- helpers


def text_of(msg: BaseMessage) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    return " ".join(p.get("text", "") for p in c if isinstance(p, dict))


def conversation(messages: list[AnyMessage], limit: int) -> list[BaseMessage]:
    """History for the model: user turns + final answers only (no old tool traffic), last `limit`."""
    keep = [
        m for m in messages
        if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls and text_of(m).strip())
    ]
    keep = keep[-limit:]
    while keep and not isinstance(keep[0], HumanMessage):
        keep = keep[1:]
    return keep


def _emit(event: dict) -> None:
    try:
        get_stream_writer()(event)
    except Exception:  # noqa: BLE001 - not streaming
        pass


def _short(s: Any, n: int = 160) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


async def react_loop(
    llm: BaseChatModel, tools: list[BaseTool], messages: list[BaseMessage], config: dict, max_steps: int
) -> list[BaseMessage]:
    """model -> ToolNode -> model ... Returns only the new messages (AI, Tool, ..., final AI)."""
    model = llm.bind_tools(tools).with_config(tags=[ANSWER_TAG])
    tool_node = ToolNode(tools, handle_tool_errors=True)
    new: list[BaseMessage] = []
    for _ in range(max_steps):
        ai = await model.ainvoke(messages + new, config)
        new.append(ai)
        if not getattr(ai, "tool_calls", None):
            return new
        for tc in ai.tool_calls:
            _emit({"step": f"tool {tc['name']}({_short(tc['args'], 120)})"})
        out = await tool_node.ainvoke({"messages": messages + new}, config)
        for tm in out["messages"]:
            _emit({"step": f"result {tm.name}: {_short(text_of(tm))}"})
        new.extend(out["messages"])
    # tool budget exhausted: one last call without tools forces a plain answer
    final = await llm.with_config(tags=[ANSWER_TAG]).ainvoke(
        messages + new + [HumanMessage("Tool budget used up. Answer now with what you have.")], config
    )
    return new + [final]


# --------------------------------------------------------------------------- graph


@dataclass
class Deps:
    llm: BaseChatModel
    decider: DecisionClient
    tools: ToolProvider
    routes: dict[str, str] = field(default_factory=lambda: dict(ROUTE_OPTIONS))
    prompts: dict[str, str] = field(default_factory=lambda: dict(ROUTE_PROMPTS))
    min_confidence: float = 0.5
    max_steps: int = 4
    max_history: int = 12


def default_deps() -> Deps:
    from .llm import chat_model
    from .tools.mcp import McpTools
    from .tools.notes import NOTE_TOOLS
    from .tools.sql import langchain_tools as sql_tools

    s = settings()
    mcp = McpTools()
    local = {"data": sql_tools(), "memory": list(NOTE_TOOLS)}

    async def provider(route: str) -> tuple[list[BaseTool], list[str]]:
        tools, down = await mcp.for_route(route)
        return local.get(route, []) + tools, down

    routes = dict(ROUTE_OPTIONS)
    prompts = dict(ROUTE_PROMPTS)
    for name, desc in mcp.routes().items():
        if name not in routes and desc:  # custom route from mcp_servers.toml
            routes[name] = desc
            prompts[name] = f"Use your tools for: {desc}."
    return Deps(
        llm=chat_model(),
        decider=DecisionClient(),
        tools=provider,
        routes=routes,
        prompts=prompts,
        min_confidence=s.route_min_confidence,
        max_steps=s.max_tool_steps,
        max_history=s.max_history,
    )


def build_graph(deps: Deps | None = None, checkpointer: Any = None):
    deps = deps or default_deps()

    async def router(state: AgentState) -> dict:
        hist = conversation(state["messages"], 3)
        if not hist:
            return {"route": "chat", "confidence": 1.0, "fallback": False}
        # the last user turn plus a little context (follow-ups like "and the second one?")
        ctx = "\n".join(f"{'User' if isinstance(m, HumanMessage) else 'Assistant'}: {_short(text_of(m), 400)}" for m in hist)
        d = await deps.decider.adecide(
            ctx, {"route": choice("Which capability does the user's LAST message need?", deps.routes)}
        )
        ans = d.answers["route"]
        route, conf = ans.choice, float(ans.confidence)
        if conf < deps.min_confidence or route not in deps.routes:
            route = "chat"
        _emit({"step": f"route {route} (choice {ans.choice}, confidence {conf:.2f}{', fallback' if d.fallback else ''})"})
        return {"route": route, "confidence": conf, "fallback": d.fallback}

    def make_route(route: str):
        async def run(state: AgentState, config) -> dict:
            tools, down = ([], []) if route == "chat" else await deps.tools(route)
            prompt = BASE_PROMPT.format(today=datetime.now().strftime("%A %Y-%m-%d"))
            if tools and (extra := deps.prompts.get(route)):
                prompt += " " + extra
            if down and not tools:
                prompt += f" Note: the {', '.join(down)} service is unreachable right now; say so briefly and answer as well as you can."
                _emit({"step": f"{', '.join(down)} unavailable, answering without tools"})
            msgs = [SystemMessage(prompt), *conversation(state["messages"], deps.max_history)]
            if tools:
                new = await react_loop(deps.llm, tools, msgs, config, deps.max_steps)
            else:
                new = [await deps.llm.with_config(tags=[ANSWER_TAG]).ainvoke(msgs, config)]
            return {"messages": new}

        run.__name__ = route
        return run

    g = StateGraph(AgentState)
    g.add_node("router", router)
    for r in deps.routes:
        # node names get a suffix: a node called "data" yields checkpoint namespaces like
        # "data:<uuid>", which tracing SDKs mistake for data: URIs
        g.add_node(f"{r}_agent", make_route(r))
        g.add_edge(f"{r}_agent", END)
    g.add_edge(START, "router")
    g.add_conditional_edges("router", lambda s: s["route"], {r: f"{r}_agent" for r in deps.routes})
    return g.compile(checkpointer=checkpointer)


@contextlib.asynccontextmanager
async def open_graph(deps: Deps | None = None, checkpoint_db: str | None = None):
    """Graph with the SQLite checkpointer ($STACK_DATA/agents.db) - use as `async with`."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = checkpoint_db or str(settings().checkpoint_db)
    if path != ":memory:":
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        yield build_graph(deps, checkpointer=saver)


# --------------------------------------------------------------------------- running a turn


@dataclass
class TurnResult:
    thread_id: str
    answer: str = ""
    route: str = "chat"
    confidence: float | None = None
    fallback: bool = False
    tools: list[dict] = field(default_factory=list)  # [{name, args, result}]
    steps: list[str] = field(default_factory=list)
    trace_id: str | None = None
    error: str | None = None


async def _inputs(graph, thread_id: str, messages: list[BaseMessage]) -> dict:
    """New thread: take the whole client history. Known thread: only the new user message
    (the checkpoint already has the rest - OpenAI-style clients resend everything)."""
    if graph.checkpointer is None:
        return {"messages": [m for m in messages if not isinstance(m, SystemMessage)]}
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    if state.values.get("messages"):
        last_user = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        return {"messages": [last_user] if last_user else []}
    return {"messages": [m for m in messages if not isinstance(m, SystemMessage)]}


def _collect(result: TurnResult, new_messages: list[BaseMessage]) -> None:
    calls: dict[str, dict] = {}
    for m in new_messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                calls[tc["id"]] = {"name": tc["name"], "args": tc["args"], "result": None}
        elif isinstance(m, ToolMessage) and m.tool_call_id in calls:
            calls[m.tool_call_id]["result"] = text_of(m)
    result.tools = list(calls.values())
    finals = [m for m in new_messages if isinstance(m, AIMessage) and not m.tool_calls]
    result.answer = text_of(finals[-1]) if finals else ""


async def stream_turn(graph, messages: list[BaseMessage] | str, thread_id: str | None = None) -> AsyncIterator[tuple[str, Any]]:
    """Run one turn. Yields ("step", str), ("token", str) and finally ("done", TurnResult)."""
    if isinstance(messages, str):
        messages = [HumanMessage(messages)]
    tid = thread_id or uuid.uuid4().hex
    res = TurnResult(thread_id=tid)
    t0 = time.monotonic()
    tracing.setup()
    with tracing.run_span("agent", input=text_of(messages[-1]) if messages else "", session_id=tid) as span:
        res.trace_id = span.get("trace_id")
        config = {"configurable": {"thread_id": tid}, "callbacks": tracing.callbacks(),
                  "metadata": {"langfuse_session_id": tid}, "recursion_limit": 50}
        try:
            inp = await _inputs(graph, tid, messages)
            final: dict = {}
            before: int | None = None
            async for mode, data in graph.astream(inp, config, stream_mode=["messages", "custom", "values"]):
                if mode == "values":
                    if before is None:  # first snapshot = history + this turn's input
                        before = len(data.get("messages", []))
                    final = data
                elif mode == "custom" and isinstance(data, dict) and "step" in data:
                    res.steps.append(data["step"])
                    yield "step", data["step"]
                elif mode == "messages":
                    chunk, meta = data
                    if ANSWER_TAG in (meta.get("tags") or []) and isinstance(chunk.content, str) and chunk.content:
                        yield "token", chunk.content
            res.route = final.get("route", "chat")
            res.confidence, res.fallback = final.get("confidence"), final.get("fallback", False)
            _collect(res, final.get("messages", [])[before or 0:])
        except Exception as e:  # noqa: BLE001
            res.error = f"{type(e).__name__}: {e}"
        span["output"] = res.answer or res.error
    db.log_run(thread_id=tid, route=res.route, confidence=res.confidence, fallback=res.fallback,
               tools=[t["name"] for t in res.tools], latency_ms=int((time.monotonic() - t0) * 1000),
               error=res.error, trace_id=res.trace_id)
    yield "done", res


async def run_turn(graph, messages: list[BaseMessage] | str, thread_id: str | None = None) -> TurnResult:
    res = None
    async for kind, val in stream_turn(graph, messages, thread_id):
        if kind == "done":
            res = val
    assert res is not None
    return res


__all__ = ["ROUTES", "AgentState", "Deps", "TurnResult", "build_graph", "default_deps", "open_graph",
           "run_turn", "stream_turn"]
