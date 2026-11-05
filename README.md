# Local Inference Stack

A local LLM host you can set up with a few commands, built for a **16 GB Apple Silicon Mac**
(M1 Pro):

- **uzu** models run natively on Metal: Qwen3.5 9B for chat, LFM2.5 1.2B for small tasks, and
  the d3-flash decision model when you turn it on
- **gateway** (Rust, ~3 MB RAM): the single OpenAI-compatible endpoint, with API keys, a
  priority queue, a SQLite request log and traces
- **Open WebUI** + SearXNG for the people on your tailnet
- **rag**: SQLite (FTS5 + sqlite-vec) with **EmbeddingGemma** from Ollama, exposed as an MCP server
- **kg**: a generic knowledge graph in FalkorDB over a markdown folder (and optional git repos),
  exposed as an MCP server
- **agents**: a LangGraph router that uses the decision model to pick a route, with tool loops,
  an OpenAI endpoint (model `agent`) and teaching examples for building your own tools
- **obs**: an always-on trace recorder (~31 MB). Langfuse runs **only when you start it** and
  then replays everything that was recorded

```
tailnet ─► tailscale serve ─► Open WebUI :3000 ─┐
scripts / Claude Code / opencode ──────────────►├─► gateway :8088 ─┬─► uzu chat :8000 / task :8001 / decide :8002
                                                                   ├─► Ollama :11434 (EmbeddingGemma)
                                                                   └─► agents :8092 ─► kg MCP :8090 ─► FalkorDB
                                                                                    └─► rag MCP :8091 ─► stack.db
every component ─OTLP─► recorder :4320 ─► spool ─► (on demand) Langfuse :3020
```

Details and contracts between components: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Install

Prerequisites: Homebrew, **Xcode.app** (uzu needs its Metal compiler), Tailscale logged in
(MagicDNS + HTTPS certificates enabled) if you want to share the stack on your tailnet.

```bash
just install      # brew bundle, .env with generated secrets, uzu build + models,
                  # recorder, gateway, kg, rag, agents, Open WebUI, tailscale serve
just doctor       # health of every service + memory use
```

Then open the printed URL and **sign up first**: that account becomes admin.
Everything is a launchd agent or a `restart: unless-stopped` container, so it survives a reboot
(for start without login: System Settings → Users & Groups → Automatic login, and OrbStack
"start at login").

### One-time Open WebUI settings (Admin Panel)
1. **Settings → Models**: give `chat` / `agent` friendly names, make them public, and set
   *Function Calling = Native* (uzu parses real tool calls).
2. **Settings → External Tools** (MCP, streamable HTTP): add `http://host.docker.internal:8091/mcp`
   (documents) and `http://host.docker.internal:8090/mcp` (knowledge graph), and assign them to
   the users/groups who should have them.
3. **Users**: approve people as they sign up (new accounts start as *pending*).

## Memory budget (16 GB)

| Always on | ~RAM |
|---|---|
| Qwen3.5 9B (4-bit) + KV cache | 6–7 GB |
| LFM2.5 1.2B task model | ~1 GB |
| EmbeddingGemma in Ollama (unloads when idle) | ~0.6 GB |
| Open WebUI + SearXNG (containers) | ~1.2 GB |
| FalkorDB + recorder (containers) | ~0.1 GB |
| gateway + kg/rag/agents Python servers | ~0.25 GB |
| **On demand:** Langfuse (`just obs::langfuse`) | ~1.8 GB |
| **On demand:** d3-flash decision model (`just decide-on`) | ~6 GB, so stop the chat model or switch `STACK_CHAT_MODEL` to `qwen3.5:4b` first |

Cap the OrbStack VM: `orbctl config set memory_mib 4096` (`just install` does this).

## Everyday commands

| Task | Command |
|---|---|
| Health + memory | `just doctor` |
| Host services / logs | `just services`, `just logs gateway` (`uzu-chat`, `kg-mcp`, …) |
| Request stats (SQLite) | `just gateway::stats 24` |
| Add documents | drop files into `~/.local/share/inference-stack/rag/inbox`, or `just rag::ingest <paths>` |
| Ask the documents | `just rag::search "…"`, `just rag::ask "…"` |
| Knowledge graph | notes in `$KG_VAULT` (re-ingested every 10 min), `just kg::find "…"`, `just kg::demo` |
| Talk to the agent | `just agents::chat "…"` or pick model `agent` in Open WebUI |
| Tool examples | `just agents::example 1` … `4`, `just agents::mcp-demo` (see [agents/TOOLS.md](agents/TOOLS.md)) |
| Decision model | `just decide-on` / `just decide-off` (the agents fall back to the task model while it is off) |
| Look at traces | `just obs::spool` (duckdb, no Langfuse needed) or `just obs::langfuse` → http://localhost:3020 |
| Load traces into Langfuse and stop it | `just obs::drain`; nightly: `just obs::nightly-install` |
| Update | `git pull && just install` (idempotent); Open WebUI only: `just update` |
| Remove | `just uninstall` (keeps data) / `just uninstall --purge` |

## Using the stack from other clients

```bash
export OPENAI_BASE_URL=http://<host>:8088/v1 OPENAI_API_KEY=$ME_API_KEY   # from .env
curl $OPENAI_BASE_URL/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H 'content-type: application/json' -d '{"model":"chat","messages":[{"role":"user","content":"hi"}]}'
```

Model names: `chat`, `task`, `agent`, `embed`. Background jobs should send
`x-stack-priority: background` so people in Open WebUI go first (uzu serves one request
per model at a time). MCP servers for Claude Code:
`claude mcp add --transport http stack-kg http://127.0.0.1:8090/mcp` (same for rag on 8091).

## Components

| Dir | README |
|---|---|
| `gateway/` | [gateway/README.md](gateway/README.md): routes (`gateway.toml`), adding a backend, logging/tracing |
| `obs/` | [obs/README.md](obs/README.md): recorder, on-demand Langfuse, drain, duckdb queries |
| `rag/` | [rag/README.md](rag/README.md): ingest, hybrid search, MCP, vs. Open WebUI's built-in RAG |
| `kg/` | [kg/README.md](kg/README.md): graph model, taxonomy, MCP registration |
| `agents/` | [agents/README.md](agents/README.md), [agents/TOOLS.md](agents/TOOLS.md): graph, decisions, building tools |
| `core/` | Open WebUI + SearXNG compose |
| `scripts/` | install, doctor, `.env` generation, launchd helper |

## Notes
- After rebuilding uzu or the gateway, macOS may ask again to allow incoming connections.
- `OWUI_BIND=0.0.0.0` exposes Open WebUI directly on the LAN; the default is localhost +
  `tailscale serve`.
- Open WebUI env vars only seed its config on first start; later changes go through the Admin UI.
- The kg vault (`KG_VAULT`) and RAG inbox are yours to fill. Keep confidential client material
  out of them if the stack is shared with others.
