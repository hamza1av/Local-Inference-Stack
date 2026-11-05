"""Example 01 - tool calling with NO framework: the OpenAI SDK and a while loop.

Run:   just agents::example 1 "Which model got the most requests?"
       uv run python examples/01_raw_openai_tools.py "What time is it?"

Everything a framework does for you, spelled out:

  1. describe each tool as a JSON schema           (stack_agents/tools/sql.py: OPENAI_TOOLS)
  2. send messages + tools to /v1/chat/completions
  3. if the reply has `tool_calls`: run each one, append a `role: tool` message with the result
     (matched by `tool_call_id`), go to 2
  4. if the reply has no tool calls: that's the answer
  5. stop after MAX_STEPS rounds no matter what - small models can loop forever

The tools are the ones the graph's `data` route uses: `sql_query` (read-only SQLite over
stack.db), `sql_schema` and `get_time`. The SQL safety rules live in the tool, not in the
prompt: never trust a model to "only run SELECTs".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from anywhere without installing

from stack_agents.config import settings
from stack_agents.llm import openai_client
from stack_agents.tools.sql import DISPATCH, OPENAI_TOOLS

MAX_STEPS = 5

SYSTEM = (
    "You answer questions about the local LLM stack using its SQLite database. "
    "Use sql_schema to see the tables, then sql_query. Keep answers short."
)


def run_tool(name: str, arguments: str) -> str:
    """Execute one tool call. Arguments arrive as a JSON *string* written by the model."""
    fn = DISPATCH.get(name)
    if fn is None:
        return f"error: unknown tool {name}"  # tell the model, don't crash
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as e:
        return f"error: arguments are not valid JSON ({e})"
    try:
        return str(fn(**args))
    except TypeError as e:  # wrong/missing parameters
        return f"error: {e}"


def ask(question: str, verbose: bool = True) -> str:
    client = openai_client()  # base_url/api_key from AGENTS_LLM_* or the gateway defaults
    model = settings().chat_model
    messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]

    for step in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=OPENAI_TOOLS,       # the JSON schemas the model may call
            tool_choice="auto",       # model decides: answer directly or call tools
            temperature=0.2,
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:        # (4) no tool calls -> final answer
            return msg.content or ""

        # (3) the assistant message WITH its tool_calls must go back into the history,
        #     otherwise the following `tool` messages refer to nothing
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            result = run_tool(call.function.name, call.function.arguments)
            if verbose:
                print(f"  [{step + 1}] {call.function.name}({call.function.arguments}) -> {result[:200]!r}", file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    # (5) budget exhausted: ask once more without tools to force an answer
    messages.append({"role": "user", "content": "Answer now with what you have."})
    resp = client.chat.completions.create(model=model, messages=messages, temperature=0.2)
    return resp.choices[0].message.content or ""


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "How many requests per model are in the stack database?"
    print(ask(q))
