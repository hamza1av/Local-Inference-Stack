"""Example 03 - tools from MCP servers (langchain-mcp-adapters) in an agent.

Run:   just agents::example 3 "What do my notes say about backups?"        (kg + rag servers)
       uv run python examples/03_mcp_tools.py --list                     (just show the tools)
       uv run python examples/03_mcp_tools.py --url units=http://127.0.0.1:8099/mcp "How many km is 26.2 miles?"
       uv run python examples/03_mcp_tools.py --url units=http://127.0.0.1:8099/mcp \
              --call convert_units '{"value": 5, "from_unit": "kg", "to_unit": "lb"}'

Where the servers come from: `agents/mcp_servers.toml` (kg MCP :8090, rag MCP :8091, ...) or
`--url name=url` (repeatable). Your own server: examples/my_mcp_server.py.

How it works:
  1. `MultiServerMCPClient({name: {"url": ..., "transport": "streamable_http"}})`
  2. `await client.get_tools()` connects, runs MCP `tools/list`, and wraps every MCP tool as a
     LangChain `BaseTool` (name, description and JSON schema come from the server)
  3. from here on they are ordinary tools: hand them to `create_agent` like in example 02
  4. each tool call opens a short MCP session and runs `tools/call` on the server

Keep the tool list short. `tools = [...]` in mcp_servers.toml (or `--only`) filters by name -
the kg server alone has six tools, more than a 9B model handles well in one prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

from stack_agents.llm import chat_model
from stack_agents.tools.mcp import McpServer, load_config


def servers_from_args(urls: list[str], config: str | None) -> list[McpServer]:
    if urls:
        return [McpServer(name=u.split("=", 1)[0], url=u.split("=", 1)[1], route="example") for u in urls]
    return load_config(config)


async def load_tools(servers: list[McpServer], only: list[str] | None = None):
    tools = []
    for s in servers:  # one at a time so a dead server doesn't hide the others
        client = MultiServerMCPClient({s.name: s.connection()})
        try:
            got = await client.get_tools(server_name=s.name)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {s.name} ({s.url}) unreachable: {type(e).__name__}", file=sys.stderr)
            continue
        allow = only or s.tools
        tools += [t for t in got if not allow or t.name in allow]
    return tools


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("question", nargs="*")
    ap.add_argument("--url", action="append", default=[], help="name=http://host:port/mcp (repeatable)")
    ap.add_argument("--config", help="mcp_servers.toml to use (default: agents/mcp_servers.toml)")
    ap.add_argument("--only", nargs="*", help="tool names to keep")
    ap.add_argument("--list", action="store_true", help="list the tools and exit")
    ap.add_argument("--call", nargs=2, metavar=("TOOL", "JSON_ARGS"), help="call one tool directly (no LLM)")
    a = ap.parse_args()

    tools = await load_tools(servers_from_args(a.url, a.config), a.only)
    if a.list or not tools:
        for t in tools:
            params = ", ".join(t.args)  # JSON-schema properties from the MCP server
            print(f"{t.name}({params}): {t.description.strip().splitlines()[0] if t.description else ''}")
        return 0 if tools else 1
    if a.call:
        tool = next((t for t in tools if t.name == a.call[0]), None)
        if tool is None:
            print(f"no tool {a.call[0]}", file=sys.stderr)
            return 1
        out = await tool.ainvoke(json.loads(a.call[1]))  # MCP tools are async-only
        # MCP results are lists of content blocks ({"type": "text", "text": ...}, images, ...)
        print("\n".join(b.get("text", str(b)) for b in out) if isinstance(out, list) else out)
        return 0

    agent = create_agent(chat_model(temperature=0.1), tools=tools,
                         system_prompt="Use the tools to answer. Answer briefly.")
    question = " ".join(a.question) or "What tools do you have?"
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]}, {"recursion_limit": 12})
    for m in result["messages"]:
        for tc in getattr(m, "tool_calls", None) or []:
            print(f"  tool call: {tc['name']}({tc['args']})", file=sys.stderr)
        if m.type == "tool":
            print(f"  result:    {m.text[:200]!r}", file=sys.stderr)
    print(result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
