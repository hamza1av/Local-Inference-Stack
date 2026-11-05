# Build your own tool

Three ways to give the agent (or any LLM client) a tool. All three end up as the same thing on
the wire: a JSON schema in the `tools` field of `/v1/chat/completions`, and `tool_calls` in the
reply that your code executes.

| Style | Example | Lives in | Use it when |
|---|---|---|---|
| Raw OpenAI SDK | `examples/01_raw_openai_tools.py` | your script | A small script with no framework, or you want to see/own every step (retries, budgets, logging). |
| LangChain `@tool` | `examples/02_langchain_tools.py` | Python, in-process | The tool belongs to this agent (it needs our DB or config, or it's quick to iterate on). |
| MCP server | `examples/my_mcp_server.py` + `03_mcp_tools.py` | its own process | Other clients should use the tool too (Open WebUI, Claude Code, opencode), or it is written in another language or runs elsewhere. |

## Rules for a 9B model

- **Few tools per prompt.** Each graph route gets 2–4 tools. Past 5 or so, a small model picks the wrong tool or invents arguments.
- **One job per tool**, a name that says it (`search_notes`, not `notes`), 1–3 parameters with obvious names and types.
- **Short docstring / description.** It goes into every prompt. One sentence, plus the allowed values if they aren't obvious.
- **Return short plain text.** Cap the rows and truncate long cells (see `sql_query`). The model has to read all of it.
- **Return errors, don't raise them.** `"error: no such column foo"` lets the model correct itself. An exception just ends the turn.
- **Put the safety checks in the tool.** A prompt that says "only SELECT" doesn't protect anything. `sql_query` opens the file read-only, accepts only SELECT/WITH, checks every action with an authorizer, and has a timeout.
- **Cap the loop.** Allow at most N tool rounds, then make one more call without tools to force an answer (`MAX_STEPS`, `AGENTS_MAX_TOOL_STEPS`).

## 1. Raw OpenAI tool calling

```python
TOOLS = [{"type": "function", "function": {
    "name": "get_time", "description": "Get the current local date and time.",
    "parameters": {"type": "object", "properties": {}}}}]

msg = client.chat.completions.create(model="chat", messages=messages, tools=TOOLS).choices[0].message
if msg.tool_calls:
    messages.append(msg.model_dump(exclude_none=True))        # the assistant turn WITH tool_calls
    for call in msg.tool_calls:
        result = DISPATCH[call.function.name](**json.loads(call.function.arguments))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": str(result)})
    # ...call the model again
```

`stack_agents/tools/sql.py` has the full version (`OPENAI_TOOLS` + `DISPATCH`), and the example
has the loop.

## 2. LangChain `@tool`

```python
from langchain_core.tools import tool

@tool
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())
```

The name, description and schema come from the function. A parameter typed `RunnableConfig` is
injected and hidden from the model (`save_note` uses it to read the thread id). To make a tool
available to the graph, add it to a route in `default_deps()` in `stack_agents/graph.py`
(`local = {"data": [...], "memory": [...]}`), and keep that route at 4 tools or fewer.

## 3. MCP server

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("units")

@mcp.tool()
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """Convert a value between units: mm cm m km in ft ..."""
    ...

mcp.settings.port = 8099
mcp.run(transport="streamable-http")      # -> http://127.0.0.1:8099/mcp
```

Start it with `just agents::mcp-demo`. Then register it with each client.

### This agent: `agents/mcp_servers.toml`

```toml
[servers.units]
url = "http://127.0.0.1:${MY_MCP_PORT:-8099}/mcp"
route = "units"                     # new route name -> new router option
description = "Converting units or date arithmetic"
tools = ["convert_units", "days_between"]
```

Use `route = "data"` (or another existing route) to add the tools to that route instead. Check
the result with `just agents::routes`, then restart the service: `just agents::restart`.

### Claude Code

```sh
claude mcp add --transport http units http://127.0.0.1:8099/mcp
```

### Open WebUI

Go to Admin Settings → External Tools → **+**, set the type to *MCP (Streamable HTTP)* and the
URL to `http://host.docker.internal:8099/mcp`. Open WebUI runs in a container, so start the
server with `--host 0.0.0.0`. Then turn the tool on in the chat's tool menu.

### opencode / other clients

Any client that supports streamable-HTTP MCP can use the same URL.

## Decisions as a "tool" for control flow

Branching (which route? is the answer grounded? how urgent is it?) is not a tool the model
calls. Your code asks the question directly with `decide()` (`examples/04_decisions.py`): one
typed question gets one typed answer with probabilities, and you don't parse any JSON. The graph
router works this way.
