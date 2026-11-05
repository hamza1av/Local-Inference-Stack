"""`stack-agents` command line.

    stack-agents chat "How many requests did the gateway serve today?"   [--thread ID] [--json]
    stack-agents serve [--host 127.0.0.1] [--port $AGENTS_PORT]
    stack-agents routes          # routes, their tools and whether the MCP servers answer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if sys.stderr.isatty() else s


async def _chat(text: str, thread: str | None, as_json: bool) -> int:
    from . import tracing
    from .graph import open_graph, stream_turn

    res = None
    async with open_graph() as graph:
        async for kind, val in stream_turn(graph, text, thread):
            if as_json:
                if kind == "done":
                    res = val
                continue
            if kind == "step":
                print(_dim(f"· {val}"), file=sys.stderr, flush=True)
            elif kind == "token":
                if res is None:
                    res = True
                print(val, end="", flush=True)
            elif kind == "done":
                if res is None and val.answer:  # nothing streamed
                    print(val.answer, end="")
                res = val
    tracing.shutdown()
    if as_json:
        print(json.dumps(asdict(res), ensure_ascii=False, indent=2, default=str))
    else:
        print(flush=True)
        tools = ", ".join(t["name"] for t in res.tools) or "none"
        conf = f"{res.confidence:.2f}" if res.confidence is not None else "-"
        print(_dim(f"[route {res.route} · confidence {conf}{' · fallback' if res.fallback else ''} · tools {tools}"
                   f" · thread {res.thread_id}]"), file=sys.stderr)
        if res.error:
            print(f"error: {res.error}", file=sys.stderr)
    return 1 if res.error else 0


async def _routes() -> int:
    from .graph import default_deps

    deps = default_deps()
    for route, desc in deps.routes.items():
        tools, down = ([], []) if route == "chat" else await deps.tools(route)
        names = ", ".join(t.name for t in tools) or "-"
        print(f"{route:10} tools: {names}{'  (down: ' + ', '.join(down) + ')' if down else ''}\n{'':10} {desc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stack-agents", description="LangGraph agents for the local inference stack")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat", help="one agent turn; prints route, tool calls and the answer")
    c.add_argument("text", nargs="+")
    c.add_argument("--thread", help="thread id to continue a conversation")
    c.add_argument("--json", action="store_true", help="print the full result as JSON")
    s = sub.add_parser("serve", help="OpenAI-compatible server (model `agent`)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int)
    sub.add_parser("routes", help="list routes and their tools")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if a.cmd == "chat":
        return asyncio.run(_chat(" ".join(a.text), a.thread, a.json))
    if a.cmd == "routes":
        return asyncio.run(_routes())
    from .server import serve

    serve(a.host, a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
