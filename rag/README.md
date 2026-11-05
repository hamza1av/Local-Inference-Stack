# rag — document RAG in SQLite with EmbeddingGemma

Your own documents (markdown, text, PDF, HTML) searchable by every part of the stack:
a CLI, a Python API for the other components and an MCP server (`:8091/mcp`) for Open WebUI,
Claude Code, LangGraph agents or any MCP client.

```
 files / inbox ──► loaders ──► heading-aware chunks ──► EmbeddingGemma (Ollama, or via the gateway)
                                     │                         │
                                     ▼                         ▼
                 stack.db:  rag_chunks ─► rag_fts (FTS5/BM25)  rag_vec (sqlite-vec, cosine)
                                     └──────── hybrid search: RRF fusion (+ per-doc cap / MMR) ──► hits [n]
```

- **Storage**: tables prefixed `rag_` in the shared `$STACK_DB` (WAL, `busy_timeout=5000`):
  `rag_documents` (path, sha256, title, mtime, size, meta), `rag_chunks` (doc, ord, heading,
  text), `rag_fts` (FTS5, external content, kept in sync by triggers), `rag_vec`
  (`vec0`, `float[$STACK_EMBED_DIM]`, cosine) and `rag_meta` (embedding model + dim).
- **Embeddings**: EmbeddingGemma with its task prompts — queries are embedded as
  `task: search result | query: …`, chunks as `title: <doc title or none> | text: …`.
  `STACK_EMBED_DIM` may be 768, 512, 256 or 128 (Matryoshka: truncate + renormalise; 256
  saves 3× space with a small quality loss). The model and dim are stored in `rag_meta`; a
  different config is refused until you run `stack-rag reembed`.
- **Chunking**: markdown is split at headings (code fences respected), chunks keep the
  heading path (`Router > Firmware updates`), ~450 tokens with ~60 tokens of overlap when a
  section has to be split; tiny neighbouring sections are merged. PDFs are chunked per page
  (`p. 3`). HTML is converted to markdown-ish text first (scripts/nav/footer dropped).
- **Search**: BM25 (FTS5) and vector KNN (sqlite-vec) each return ~30 candidates, fused with
  reciprocal rank fusion (`1/(60+rank)`), at most 2 hits per document by default, optional
  MMR for diversity. BM25 catches exact tokens (IPs, error codes, names), vectors catch
  paraphrases (`tiny flies in wet soil` → "fungus gnats").

## Usage

```sh
just rag::demo                              # ingest rag/examples/docs and run a sample search
just rag::ingest ~/Documents/manuals x.pdf  # files or folders (md, txt, pdf, html); unchanged files are skipped
just rag::search "backup schedule" 5
just rag::ask "how often is the guest wifi password changed?"
just rag::install                           # launchd service local.stack.rag-mcp (MCP server + inbox watcher)
just rag::status | rag::uninstall | rag::logs | rag::serve (foreground) | rag::test
```

Or the CLI directly (`uv run --project rag stack-rag …`, `.env` is loaded automatically):

| command | what it does |
|---|---|
| `ingest <path…> [--force]` | add/update files or folders (incremental by sha256) |
| `watch [--dir D] [--once]` | poll the inbox (`$STACK_DATA/rag/inbox`) — add/update changed files, remove documents whose file was deleted |
| `search "q" -k 5 [--mmr] [--max-per-doc N] [--json]` | hybrid search |
| `ask "q" -k 5 [--model M] [--base-url URL]` | retrieve + answer with `[n]` citations and a source list |
| `list [--json]`, `rm <id\|path\|folder\|text:key>…`, `stats` | manage the index |
| `reembed` | rebuild all vectors after changing `STACK_EMBED_MODEL` / `STACK_EMBED_DIM` |
| `serve [--port P] [--no-watch]` | MCP server on `RAG_MCP_PORT` + inbox watcher in the same process |

Drop files into `$STACK_DATA/rag/inbox` (subfolders are fine) and the running server picks
them up within `RAG_WATCH_INTERVAL` seconds; deleting the file removes it from the index.
Files and folders starting with `.` or `~` and partial downloads are ignored. Documents
ingested from elsewhere (`stack-rag ingest`) are never touched by the watcher.

### Settings (`.env`, all optional)

| variable | default | |
|---|---|---|
| `STACK_DB`, `STACK_DATA` | from `.env` | database and data dir (inbox = `$STACK_DATA/rag/inbox`, override `RAG_INBOX`) |
| `OLLAMA_URL`, `STACK_EMBED_MODEL`, `STACK_EMBED_DIM` | `http://127.0.0.1:11434`, `embeddinggemma`, `768` | embeddings |
| `RAG_EMBED_VIA_GATEWAY` | `false` | `true` → `$GATEWAY_URL/embeddings` with `Bearer $STACK_API_KEY` (traced/logged by the gateway) |
| `RAG_LLM_BASE_URL`, `RAG_LLM_MODEL`, `RAG_LLM_API_KEY` | `$GATEWAY_URL`, `chat`, `$STACK_API_KEY` | chat endpoint for `ask` (any OpenAI-compatible server, e.g. `http://127.0.0.1:11434/v1`) |
| `RAG_MCP_PORT`, `RAG_MCP_HOST` | `8091`, `127.0.0.1` | MCP server bind |
| `RAG_MCP_ALLOWED_HOSTS` | – | extra `Host` headers to accept (`name:*`), `*` disables the DNS-rebinding check |
| `RAG_WATCH_INTERVAL` | `10` | inbox poll interval (s) |
| `RAG_CHUNK_TOKENS`, `RAG_CHUNK_OVERLAP`, `RAG_EMBED_BATCH` | `450`, `60`, `16` | chunking / batching |

## Python API

```python
from stack_rag import search

for hit in search("how do I restore a file from a snapshot", k=5):
    print(hit["score"], hit["title"], hit["heading"], hit["path"])
    # keys: doc_id, chunk_id, title, path, heading, text, score (RRF, higher = better)
```

`search(query, k=5, *, max_per_doc=2, mmr=False, index=None)` uses a process-wide index
built from `.env`. For writes use `stack_rag.index.Index` (`ingest_paths`, `add_text`,
`remove`, `sync_dir`, `list_documents`, `stats`). Readers that only need text can also
query `rag_documents` / `rag_chunks` / `rag_fts` without sqlite-vec; do not touch `rag_vec`
without loading the extension.

## MCP server

`stack-rag serve` (the `rag-mcp` launchd service) speaks streamable HTTP at
`http://127.0.0.1:8091/mcp` (stateless, JSON responses) and answers `GET /health` with
document/chunk counts. Tools — descriptions are written so a 9B model uses them correctly:

| tool | returns |
|---|---|
| `rag_search(query, k=5)` | numbered passages `[n] Title › Section (path)` + text, to be cited as `[n]` |
| `rag_list_documents()` | `[id] title — path (N chunks)` per document |
| `rag_add_text(title, text)` | saves a note as document `text:<slug>` (same title replaces it); searchable immediately |

There is no authentication: the server binds to 127.0.0.1 and checks `Host`/`Origin`
(DNS-rebinding protection; `localhost`, `127.0.0.1`, `host.docker.internal` and
`host.orb.internal` are accepted). Only bind it to `0.0.0.0` behind something that
authenticates.

**Open WebUI** (≥ 0.6.31, native MCP): *Admin Settings → External Tools → +*, type
**MCP (Streamable HTTP)**, URL `http://host.docker.internal:8091/mcp`, auth *None*. Enable
the tool per model or per chat. (OrbStack forwards `host.docker.internal` to the host's
loopback; if your container runtime does not, set `RAG_MCP_HOST=0.0.0.0` and
`RAG_MCP_ALLOWED_HOSTS` accordingly.) Use a model with native function calling.

**Claude Code**:

```sh
claude mcp add --transport http rag http://127.0.0.1:8091/mcp      # add -s user for all projects
```

**LangGraph / LangChain** (`langchain-mcp-adapters`):

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({"rag": {"transport": "streamable_http", "url": "http://127.0.0.1:8091/mcp"}})
tools = await client.get_tools()          # rag_search, rag_list_documents, rag_add_text
```

Agents running in the same uv workspace can skip MCP and call `stack_rag.search` directly.

## This vs. Open WebUI's built-in RAG

`core/compose.yml` already points Open WebUI's own RAG at EmbeddingGemma through the
gateway, so both use the same embedding model — but they are separate indexes.

| | Open WebUI knowledge / file upload | `rag` (this component) |
|---|---|---|
| where documents come from | uploaded in the browser, per chat or "Knowledge" collection | files on the Mac: `ingest`, the inbox folder, `rag_add_text` |
| who can use it | only Open WebUI chats | everyone: Open WebUI (as a tool), Claude Code, LangGraph agents, scripts, Python API |
| storage | inside the Open WebUI container volume (its own vector DB) | `stack.db` on the host, inspectable with `sqlite3` |
| retrieval | automatic, injected into every prompt of that chat | the model decides to call `rag_search` (or `ask` / your code does it) |
| search | vector (optionally hybrid + reranker, configured in the UI) | BM25 + vectors with RRF, heading-aware chunks, per-document cap |
| access control | Open WebUI users and groups | none — personal, localhost only |

Use **Open WebUI's RAG** for ad-hoc "chat with this PDF" and for documents shared with
other tailnet users who have their own permissions. Use **`rag`** for your personal,
long-lived document collection that agents and tools should search, for files that live on
disk and change (the inbox keeps it in sync), and when you want citations with exact
file/section paths. Do not ingest the same collection into both unless you need it in
both places.

## Memory and size

Idle RSS of `stack-rag serve` is ~50–60 MB (Python + MCP SDK; `uv run` adds a ~20 MB parent
process). The embedding model lives in Ollama (~0.6 GB while loaded, unloaded after
Ollama's keep-alive). The index costs ~3 KB per chunk at 768 dims (sqlite-vec preallocates
3 MB per 1024 vectors), ~1 KB at 256.

## Tests

```sh
just rag::test        # or: cd rag && uv run pytest
```

Uses a temp DB and the real EmbeddingGemma on Ollama (skipped if unreachable): chunking and
loaders, incremental ingest / update / delete (no orphan vectors or FTS rows), inbox sync,
model/dim mismatch + `reembed` to 256 dims, hybrid ranking sanity on `examples/docs`, an MCP
smoke test over HTTP against a `serve` subprocess (tools, watcher add/remove), and `ask`
with a small Ollama chat model (`RAG_TEST_CHAT_MODEL`, default `lfm3:1.7b`, skipped if absent).

`examples/docs/houseplant-care.pdf` is generated by `examples/make_pdf.py` (stdlib only).
