# agents

LangGraph orchestration for the local stack. It contains:

- a router agent that is served as the OpenAI model `agent`
- a decision-model client with a fallback
- four teaching examples for tool calling

```
user turn ─► router ── decide(choice) ──► chat_agent        no tools
               │  (decision model :8002 via gateway,     knowledge_agent   kg MCP:  kg_find, kg_context, kg_tags
               │   503 → task-model fallback;           documents_agent   rag MCP: rag_search, rag_list_documents
               │   confidence < 0.5 → chat)             data_agent        sql_query, sql_schema, get_time
               │                                        memory_agent      save_note, list_notes, search_notes
               │                                        <custom>_agent    tools of your MCP server (mcp_servers.toml)
               ▼
     each route: ReAct loop (model ⇄ ToolNode, ≤ AGENTS_MAX_TOOL_STEPS rounds) → answer
     history: SQLite checkpointer $STACK_DATA/agents.db, one thread per conversation
```

## Run

```sh
just agents::sync                      # uv venv
just agents::chat "Which model got the most requests today?"
just agents::routes                    # routes, tools, which MCP servers answer
just agents::serve                     # foreground server on :$AGENTS_PORT (8092)
just agents::install                   # launchd service local.stack.agents
just agents::status | uninstall | restart | logs
just agents::example 1 "What time is it?"      # 1..4, see below
just agents::mcp-demo                  # examples/my_mcp_server.py on :8099
just agents::test                      # unit tests (no model needed); `-m ollama` = real-model smoke
```

To use Ollama instead of the gateway (no uzu, no gateway):

```sh
AGENTS_LLM_BASE_URL=http://127.0.0.1:11434/v1 AGENTS_LLM_API_KEY=ollama \
AGENTS_CHAT_MODEL=lfm3:1.7b AGENTS_TASK_MODEL=lfm3:1.7b just agents::chat "what time is it?"
```

## Examples (`examples/`)

| # | File | Shows |
|---|---|---|
| 1 | `01_raw_openai_tools.py` | A tool loop with no framework (OpenAI SDK): JSON schemas, `tool_calls`, `role: tool` messages, max steps. Tools: read-only `sql_query` / `sql_schema` over stack.db, `get_time`. |
| 2 | `02_langchain_tools.py` | `@tool` functions (`save_note`, `list_notes`, `search_notes` → table `agent_notes`) in `langchain.agents.create_agent`, with a checkpointer. |
| 3 | `03_mcp_tools.py` | Loads tools from MCP servers (`mcp_servers.toml` or `--url`) with langchain-mcp-adapters. `--list` and `--call` work without an LLM. |
| – | `my_mcp_server.py` | A minimal FastMCP server (`convert_units`, `days_between`) and how to register it with this agent, Claude Code and Open WebUI. |
| 4 | `04_decisions.py` | Decision API: classification (`choice`), yes/no gate (`noul`) and `score`, with automatic fallback. |

The examples import their tools from `stack_agents/tools/`, and the graph uses the same tools.
[TOOLS.md](TOOLS.md) explains how to build your own tool, and when to use each of the three styles.

## Server (`stack-agents serve`)

`GET /health`, `GET /v1/models` (returns `agent`), `POST /v1/chat/completions`. Both streaming
(SSE) and non-streaming responses are supported. The gateway sends `"model": "agent"` here.

- **Thread id** is checked in this order: header `x-thread-id`, then `x-openwebui-chat-id`, then
  body `user` plus a hash of the first user message (`user` is per person, the hash separates
  that person's chats), then a fresh thread. For a known thread, only the newest user message is
  added, because the checkpoint already holds the rest of the history.
- **Steps** (route, tool calls, results) follow `AGENTS_STREAM_STEPS`. `reasoning` (the default)
  sends them as `reasoning_content` deltas, which Open WebUI shows as a collapsible "thinking"
  block. `prefix` sends them as `> …` lines, and `off` leaves them out. Non-streaming responses
  also carry `agent: {route, confidence, fallback, tools, thread_id, trace_id}`.
- Client `system` messages are ignored: each route has its own short prompt.

## Decision client (`stack_agents/decide.py`)

```python
from stack_agents.decide import DecisionClient, choice, yes_no, score
d = await DecisionClient().adecide(text, {"route": choice("…", {"a": "desc", "b": None}),
                                          "ok": yes_no("…"), "level": score("…", ["low", "mid", "high"])})
d.answers["route"].choice, d.answers["ok"].yes, d.answers["level"].level, d.fallback
```

The client calls `POST $AGENTS_DECIDE_URL` (default `$GATEWAY_URL/decisions`).

- If the decision model is unavailable (503, 502, 504, 404, connection error or timeout), the
  client asks the `task` model each question with a strict-JSON prompt. It validates the reply,
  retries once with the error message, and returns the same shape with `"fallback": true`.
- A 400 response raises `DecisionError`.
- If the fallback also fails, the answer has confidence 0, so the router picks `chat`.

## Configuration (env, usually from the repo `.env`)

| Var | Default | |
|---|---|---|
| `AGENTS_PORT` | 8092 | server port (binds 127.0.0.1) |
| `AGENTS_LLM_BASE_URL` / `AGENTS_LLM_API_KEY` | `$GATEWAY_URL` / `$STACK_API_KEY` | OpenAI endpoint |
| `AGENTS_CHAT_MODEL` / `AGENTS_TASK_MODEL` | `chat` / `task` | answer + tool loops / decision fallback |
| `AGENTS_DECIDE_URL` | `$AGENTS_LLM_BASE_URL/decisions` | decision API |
| `AGENTS_ROUTE_MIN_CONFIDENCE` | 0.5 | below this the router picks `chat` |
| `AGENTS_MAX_TOOL_STEPS` / `AGENTS_MAX_HISTORY` | 4 / 12 | tool rounds per turn / messages of history sent |
| `AGENTS_SQL_ROW_CAP` | 50 | `sql_query` row limit |
| `AGENTS_STREAM_STEPS` | reasoning | `reasoning` \| `prefix` \| `off` |
| `AGENTS_MCP_CONFIG` | `agents/mcp_servers.toml` | MCP servers → routes |
| `AGENTS_CHECKPOINT_DB` | `$STACK_DATA/agents.db` | LangGraph checkpoints |
| `AGENTS_TRACING` | 1 | 0 turns tracing off |

## Data

- `stack.db` holds `agent_notes`, written by the memory tools, and `agent_runs`, with one row per
  turn: route, confidence, fallback, tools, latency, error and trace_id. `sql_query` opens
  `stack.db` read-only and hides `rag_vec`.
- `agents.db` holds the LangGraph checkpoints. It is a separate file because their table names
  can't be prefixed.

## Tracing

The Langfuse v3 SDK sends traces to the recorder (`STACK_OTLP_ENDPOINT` minus
`/api/public/otel`) with public key `pk-lf-agents` and tag `app:agents`.

- Each turn has a root span `agent`. The LangChain `CallbackHandler` adds the graph, LLM and tool
  spans.
- httpx is instrumented, so calls to the gateway, the decision API and MCP servers carry a W3C
  `traceparent` header, and the gateway's spans nest under them.
- If the recorder is down, tracing is a silent no-op. The only cost is up to about 3 s of flush
  when the CLI exits.

## Footprint

On an M1 Pro, the idle server process uses about 100 MB RSS. `just agents::install` runs
`.venv/bin/stack-agents` directly, so no `uv` wrapper process sits in front of it (that would
add about 20 MB).
