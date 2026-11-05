# agents: LangGraph router agent, OpenAI-compatible server (model `agent`) on $AGENTS_PORT.
# Recipes run inside agents/; commands get the repo .env via ../scripts/env-run.sh.

set positional-arguments := true

env_run := "../scripts/env-run.sh"
bin := source_directory() / ".venv/bin"

# create/refresh the virtualenv (uv)
sync:
    uv sync

# install the server as launchd service local.stack.agents (runs the venv binary directly: no uv wrapper process)
install: sync
    ../scripts/launchd.sh install agents --workdir "{{source_directory()}}" -- "{{bin}}/stack-agents" serve

uninstall:
    ../scripts/launchd.sh uninstall agents

# launchd state + /health
status:
    @launchctl list | awk 'NR==1 || /local\.stack\.agents$/' || true
    @{{env_run}} sh -c 'curl -sf -m 3 "http://127.0.0.1:$AGENTS_PORT/health" && echo || echo "agents: not answering on :$AGENTS_PORT"'

restart:
    ../scripts/launchd.sh restart agents

# run the server in the foreground
serve *args:
    {{env_run}} uv run stack-agents serve "$@"

# one agent turn, e.g. `just agents::chat "how many requests today?"` (add --thread ID to continue)
chat *args:
    {{env_run}} uv run stack-agents chat "$@"

# routes and their tools (shows which MCP servers answer)
routes:
    {{env_run}} uv run stack-agents routes

# run a teaching example: 1 raw OpenAI tools, 2 LangChain @tool, 3 MCP tools, 4 decisions
example n *args:
    f=$(ls examples/0"$1"_*.py | head -1); shift; {{env_run}} uv run python "$f" "$@"

# start the demo MCP server (examples/my_mcp_server.py) on $MY_MCP_PORT (8099)
mcp-demo *args:
    {{env_run}} uv run python examples/my_mcp_server.py "$@"

# tests: fast unit tests; `just agents::test -m ollama` for the real-model smoke tests
test *args:
    uv run pytest -q "$@"

logs:
    {{env_run}} sh -c 'tail -f "$STACK_DATA/logs/agents.log"'
