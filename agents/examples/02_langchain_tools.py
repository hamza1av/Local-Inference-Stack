"""Example 02 - LangChain `@tool` functions in a prebuilt LangGraph ReAct agent.

Run:   just agents::example 2 "Remember that the spare key is in the blue box"
       just agents::example 2 "Where is the spare key?"

Compare with example 01: there we wrote JSON schemas, parsed arguments and kept the message list
ourselves. Here:

  * `@tool` builds the schema from the function signature + docstring
    (stack_agents/tools/notes.py: save_note, list_notes, search_notes -> table `agent_notes`)
  * `create_agent(model, tools)` is the whole loop from example 01 as a LangGraph graph:
        model --tool_calls--> tools --> model --no tool_calls--> END
  * a checkpointer + `thread_id` gives the agent conversation memory across calls

`create_agent` lives in `langchain.agents` since LangChain/LangGraph 1.0 (it replaces
`langgraph.prebuilt.create_react_agent`). The router graph (stack_agents/graph.py) builds the
same loop by hand from `ToolNode` so it can cap the number of tool rounds.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain.agents import create_agent
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from stack_agents.llm import chat_model
from stack_agents.tools.notes import list_notes, save_note, search_notes


# A tool is just a typed function with a docstring. This one is defined right here to show the
# anatomy; the three note tools are imported from the package (the graph's `memory` route uses them).
@tool
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


TOOLS = [save_note, list_notes, search_notes, word_count]

SYSTEM = (
    "You manage the user's personal notes. To remember something call save_note. "
    "To recall, call search_notes with a few keywords (or list_notes). Answer briefly."
)


def build_agent(model=None, checkpointer=None):
    return create_agent(
        model or chat_model(temperature=0.1),
        tools=TOOLS,
        system_prompt=SYSTEM,
        checkpointer=checkpointer,
    )


def ask(question: str, thread_id: str = "example-02", agent=None, verbose: bool = True) -> str:
    agent = agent or build_agent(checkpointer=InMemorySaver())
    config = {
        "configurable": {"thread_id": thread_id},  # conversation key for the checkpointer
        "recursion_limit": 12,                      # graph steps (each model or tool call = 1): loop guard
    }
    result = agent.invoke({"messages": [{"role": "user", "content": question}]}, config)
    if verbose:
        for m in result["messages"]:
            for tc in getattr(m, "tool_calls", None) or []:
                print(f"  tool call: {tc['name']}({tc['args']})", file=sys.stderr)
            if m.type == "tool":
                print(f"  result:    {m.content[:200]!r}", file=sys.stderr)
    return result["messages"][-1].content


if __name__ == "__main__":
    print(ask(" ".join(sys.argv[1:]) or "Remember that the spare key is in the blue box."))
