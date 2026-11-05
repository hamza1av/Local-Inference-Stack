# kg — knowledge graph over your notes (FalkorDB + MCP)

Turns a folder of markdown notes (Obsidian-style) and, optionally, a few git repos into a
graph in FalkorDB, and serves it to agents over MCP. The graph is only an index: everything
in it can be rebuilt from the notes (`stack-kg reset && stack-kg ingest`).

```
KG_VAULT/*.md ──┐                          ┌─ kg_find / kg_context / kg_tags / kg_stats / kg_cypher
KG_REPOS (git) ─┼─ stack-kg ingest ─► FalkorDB (graph KG_GRAPH, :6390) ◄─ stack-kg serve :8090/mcp
Ollama embed ───┘        │                   ▲                                     │
                  SQLite kg_files      kg_remember ── appends a line to the vault ─┘
```

## Quick start

```sh
just env            # once, at the repo root: creates .env
just kg::up         # FalkorDB + browser UI (container stack-kg)
just kg::demo       # copies examples/vault into KG_VAULT if it is empty, then ingests
just kg::find why is the gateway written in rust
just kg::context Decisions
just kg::install    # MCP server as launchd agent local.stack.kg-mcp
just kg::status
```

Point `KG_VAULT` (in `.env`) at your own notes folder instead of using the demo. Keep it
free of confidential material: the graph can be read by every agent you connect.

## What gets indexed

| From | Graph |
|---|---|
| each `*.md` file | `(:Note {id: relative path, title, aliases, type, status, headings, body, excerpt, updated})` |
| frontmatter `tags:`, inline `#tags` | `(:Note)-[:TAGGED {by: 'you', confidence: 1.0}]->(:Tag {name, facet})` |
| frontmatter `type:` / `status:` | `TAGGED {by: 'frontmatter', confidence: 1.0}` |
| regex rules in `taxonomy.yaml` | `TAGGED {by: 'rule', confidence: 0.9 (title) / 0.6 (body, >= 2 hits)}` |
| `[[wikilinks]]` (by file name, title or alias) | `(:Note)-[:LINKS_TO]->(:Note)`, or `->(:Ref)` while the target does not exist |
| list lines `- 2026-01-31: Decision: text — why: reason` | `(:Fact {kind, text, why, created})-[:IN_NOTE]->(:Note)`; kinds: decision, gotcha, fact, todo, question |
| `KG_REPOS` (colon separated paths) | `(:Repo)<-[:IN]-(:Commit)` for the last `KG_REPO_COMMITS` (30) commits; `(:Note)-[:MENTIONS]->(:Repo)` when a note names the repo |

Ingest is incremental: the hash of every file (plus the taxonomy version) is kept in
`$STACK_DB` table `kg_files`; only changed files are rewritten, deleted files are removed
(links to them turn back into `Ref`s), and a missing graph is repaired automatically.
`--full` rewrites everything. Each run is logged in `kg_runs`.

Searches only use tag edges with confidence >= 0.6. Edit `taxonomy.yaml` (facets `topic`,
`tech`, `type`, `status`; aliases; regexes) to fit your notes — a changed taxonomy re-tags
every note on the next ingest.

### Search

`find` runs a full-text query (FalkorDB/RediSearch) and a vector query (EmbeddingGemma via
Ollama `/v1/embeddings`, vector index of `STACK_EMBED_DIM` dims, cosine) per label (Note,
Fact, Commit) and fuses the rankings with reciprocal rank fusion. Hits found only by the
vector side are marked `~meaning-only` so a model knows to double-check them. Vector hits
farther than `KG_MAX_DISTANCE` (default 0.8 cosine distance) are dropped. If Ollama is down,
search falls back to text and ingest leaves the vectors for the next run.

EmbeddingGemma prompts: queries `task: search result | query: …`, documents
`title: … | text: …`.

### Remember

`stack-kg remember decision "Use X" --why "because Y"` (or the MCP tool) appends
`- 2026-11-05: Decision: Use X — why: because Y` under `## Remembered` in
`KG_VAULT/Remembered.md` (`KG_REMEMBER_NOTE`; `--note <title>` for another note), then
re-indexes. Duplicates are detected. Since the line lives in the vault, facts survive
`reset` and `ingest --full`.

## CLI

```
stack-kg ingest [--full] [--no-embed]
stack-kg find <words…> [-t TAG] [-n 8] [--mode hybrid|text|semantic] [--json]
stack-kg context <note title | alias | #tag | repo>
stack-kg tags | stats
stack-kg cypher "MATCH (n:Note) RETURN n.title LIMIT 5"     # read-only
stack-kg remember <decision|gotcha|fact|todo|question> <text…> [--why …] [--note …]
stack-kg reset [-y] | demo | serve
```

Run it with the repo's `.env`: `../scripts/env-run.sh uv run --project kg stack-kg …`
(the `just kg::*` recipes do this).

## MCP server

`stack-kg serve`: streamable HTTP (stateless, JSON responses) on
`http://127.0.0.1:$KG_MCP_PORT/mcp`, `GET /health`, and a re-ingest every
`KG_INGEST_INTERVAL` seconds (default 600, `0` = off) in the same process.

| Tool | Arguments | Returns |
|---|---|---|
| `kg_find` | `query`, `tag=None`, `limit=8` | one line per hit + excerpt; `[decision]` etc. for facts; `~meaning-only` marks |
| `kg_context` | `name` (note title/alias, tag, repo) | metadata, tags, links in/out, facts, related notes |
| `kg_tags` | – | tags per facet with counts and aliases |
| `kg_stats` | – | node/edge counts, last ingest |
| `kg_cypher` | `query` | read-only (`GRAPH.RO_QUERY`), max 50 rows, one JSON object per line |
| `kg_remember` | `kind`, `text`, `why=None` | confirmation; writes to the vault |

Host-header checks (DNS rebinding protection) allow `127.0.0.1`, `localhost` and
`host.docker.internal`; add more with `KG_MCP_ALLOWED_HOSTS=name:*,other:*`.

### Register it

**Claude Code**

```sh
claude mcp add --transport http stack-kg http://127.0.0.1:8090/mcp
```

**Open WebUI** (0.6.31+): Admin Settings → External Tools → add a connection of type
*MCP (Streamable HTTP)*, URL `http://host.docker.internal:8090/mcp`, auth *None*. Then
enable the tool for a model (Workspace → Models → Tools) and use native function calling.
Older versions: put [mcpo](https://github.com/open-webui/mcpo) in front
(`uvx mcpo --port 8095 --server-type streamable-http -- http://127.0.0.1:8090/mcp`) and add
`http://host.docker.internal:8095` as an OpenAPI tool server.

**LangGraph** (`langchain-mcp-adapters`)

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

client = MultiServerMCPClient({"kg": {"transport": "streamable_http", "url": "http://127.0.0.1:8090/mcp"}})
tools = await client.get_tools()          # kg_find, kg_context, kg_tags, kg_stats, kg_cypher, kg_remember
agent = create_react_agent(model, tools)
```

**curl**

```sh
curl -s http://127.0.0.1:8090/health
curl -s http://127.0.0.1:8090/mcp -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"kg_find","arguments":{"query":"memory budget"}}}'
```

## FalkorDB

`compose.yml` (project `stack-kg`): `falkordb/falkordb:v4.22.1`, Redis protocol on
`127.0.0.1:$FALKORDB_PORT` (6390), browser on `127.0.0.1:$FALKORDB_UI_PORT` (3011, `just kg::ui`;
connect to host `localhost`, port `6379` inside the UI). AOF + snapshots in the
`stack-kg_falkordb_data` volume, `mem_limit: 512m`, 2 query threads. `KG_BROWSER=0` skips the
UI (saves ~45 MB).

## Memory (measured on the sample vault)

| Process | RSS |
|---|---|
| `stack-kg serve` (Python) | ~70–85 MB, plus ~20 MB for the `uv run` wrapper |
| FalkorDB container | ~70 MB (redis-server ~38 MB + browser ~47 MB) |

## Settings (`.env`)

| Variable | Default | |
|---|---|---|
| `KG_VAULT` | `$STACK_DATA/kg/vault` | markdown folder |
| `KG_GRAPH` | `kg` | graph name |
| `KG_REPOS` | empty | git repos, colon separated |
| `FALKORDB_PORT` / `FALKORDB_UI_PORT` | 6390 / 3011 | |
| `KG_MCP_PORT` | 8090 | |
| `OLLAMA_URL`, `STACK_EMBED_MODEL`, `STACK_EMBED_DIM` | `http://127.0.0.1:11434`, `embeddinggemma`, 768 | |
| `STACK_DB` | `$STACK_DATA/stack.db` | ingest state (`kg_` tables) |
| optional: `KG_INGEST_INTERVAL` (600), `KG_REMEMBER_NOTE` (`Remembered.md`), `KG_REPO_COMMITS` (30), `KG_MAX_DISTANCE` (0.8), `KG_TAXONOMY`, `KG_MCP_HOST` (127.0.0.1), `KG_MCP_ALLOWED_HOSTS`, `KG_BROWSER` (1), `FALKORDB_HOST` | | |

## Tests

```sh
just kg::up
just kg::test          # pytest: real FalkorDB (temporary graphs kg_test_*), real embeddinggemma,
                       # MCP over HTTP with the official client
```
