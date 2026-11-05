# rag: document RAG in SQLite (FTS5 + sqlite-vec) with EmbeddingGemma — see rag/README.md

dir := source_directory()
envrun := dir / ".." / "scripts" / "env-run.sh"
launchd := dir / ".." / "scripts" / "launchd.sh"
cli := envrun + " uv run --quiet --project " + dir + " stack-rag"

# install the MCP server + inbox watcher as launchd service local.stack.rag-mcp (:RAG_MCP_PORT)
install:
    uv sync --quiet --project {{dir}}
    {{envrun}} sh -c 'mkdir -p "$STACK_DATA/rag/inbox"'
    {{launchd}} install rag-mcp --workdir {{dir}} -- uv run --project {{dir}} stack-rag serve

# remove the launchd service (keeps the index in $STACK_DB)
uninstall:
    {{launchd}} uninstall rag-mcp

# service state, /health and index stats
status:
    @launchctl list | awk 'NR==1 || /local\.stack\.rag-mcp/'
    @{{envrun}} sh -c 'curl -fsS "http://127.0.0.1:${RAG_MCP_PORT:-8091}/health" && echo || echo "rag-mcp: not responding on :${RAG_MCP_PORT:-8091}"'
    @{{cli}} stats

# ingest files or folders (md, txt, pdf, html), e.g. `just rag::ingest ~/Documents/manuals`
[no-cd]
[positional-arguments]
ingest +paths:
    {{cli}} ingest "$@"

# hybrid search, e.g. `just rag::search "backup schedule" 5`
[no-cd]
[positional-arguments]
search query k="5":
    {{cli}} search "$1" -k "$2"

# retrieve + answer with [n] citations (chat model via the gateway; RAG_LLM_* to override)
[no-cd]
[positional-arguments]
ask question k="5":
    {{cli}} ask "$1" -k "$2"

# ingest the bundled sample documents and run a sample search
demo:
    {{cli}} ingest {{dir}}/examples/docs
    {{cli}} search "how often is the guest wifi password changed" -k 3

# run the server in the foreground (no launchd), e.g. for debugging
serve:
    {{cli}} serve

# follow the service log
logs:
    {{envrun}} sh -c 'tail -f "$STACK_DATA/logs/rag-mcp.log"'

# run the tests (temp DB, needs Ollama with the embedding model)
test *args:
    cd {{dir}} && uv run --quiet pytest {{args}}
