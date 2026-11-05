# Knowledge graph: FalkorDB (container stack-kg) + `stack-kg` MCP server (launchd kg-mcp).
# Recipes run in kg/; every command gets the repo's .env through env-run.sh.

kg_dir := source_directory()
run := kg_dir / "../scripts/env-run.sh"
compose := run + " docker compose -f " + kg_dir / "compose.yml"
cli := run + " uv run --project " + kg_dir + " stack-kg"

default:
    @just --list kg

# start FalkorDB (+ browser UI)
up:
    {{compose}} up -d --wait

# stop FalkorDB (data stays in the volume)
down:
    {{compose}} down

# FalkorDB + MCP server as launchd agent local.stack.kg-mcp (port KG_MCP_PORT, path /mcp)
install: up
    uv sync --project {{kg_dir}} --no-dev
    {{run}} mkdir -p "$KG_VAULT"
    {{kg_dir}}/../scripts/launchd.sh install kg-mcp --workdir {{kg_dir}} -- uv run --project {{kg_dir}} stack-kg serve
    @echo "MCP: http://127.0.0.1:$({{run}} printenv KG_MCP_PORT)/mcp"

# remove the launchd agent and stop FalkorDB (graph data and vault are kept)
uninstall:
    {{kg_dir}}/../scripts/launchd.sh uninstall kg-mcp
    {{compose}} down

# container, launchd agent and MCP health
status:
    @{{compose}} ps
    @launchctl list | awk 'NR==1 || /local\.stack\.kg-mcp/'
    @{{run}} sh -c 'curl -sf "http://127.0.0.1:$KG_MCP_PORT/health" && echo || echo "kg-mcp: not responding on :$KG_MCP_PORT"'

# index KG_VAULT (+ KG_REPOS); `just kg::ingest --full` rewrites everything
ingest *args:
    {{cli}} ingest {{args}}

# hybrid search, e.g. `just kg::find why rust gateway` or `just kg::find -t decision falkordb`
find +args:
    {{cli}} find {{args}}

# a note, tag or repo with everything around it
context +name:
    {{cli}} context {{name}}

# node/edge counts and last ingest
stats:
    {{cli}} stats

# copy the example notes into KG_VAULT if it has none, then ingest
demo:
    {{cli}} demo

# MCP server in the foreground (for debugging; the installed one runs under launchd)
serve:
    {{cli}} serve

# open the FalkorDB browser (connect to host "localhost", port 6379)
ui:
    {{run}} sh -c 'open "http://127.0.0.1:$FALKORDB_UI_PORT"'

# drop the graph and its ingest state (vault untouched)
reset:
    {{cli}} reset

# tests against the running FalkorDB (temp graphs) and Ollama
test *args:
    {{run}} uv run --project {{kg_dir}} pytest {{kg_dir}}/tests {{args}}
