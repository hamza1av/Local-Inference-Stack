# gateway — `stack-gateway`

The one OpenAI-compatible endpoint every client uses (`GATEWAY_URL`, default `:8088`).
It is a small Rust binary (about 3 MB, about 3 MB RSS when idle) that runs as the launchd
service `local.stack.gateway`. It does the following:

- authenticates clients by API key and records the client name with every request,
- routes requests by `model` (or by path) to uzu, Ollama, the agents server and other backends,
- queues requests per backend, with interactive requests ahead of background ones,
- passes streaming (SSE) responses through chunk by chunk without buffering them,
- logs one row per request to SQLite (`gw_requests`) and exports one OTLP span per request.

```
just gateway::build      # cargo build --release
just gateway::test       # unit + integration tests (mock backend + mock collector)
just gateway::routes     # print resolved routes
just gateway::run        # foreground, with .env
just gateway::install    # build + launchd (local.stack.gateway)
just gateway::status     # /health JSON
just gateway::logs       # tail the service log
just gateway::stats      # per model/client requests, tokens, latency (last 24h; `stats 168` = week)
just gateway::uninstall
```

## Configuration

**Environment** (`$STACK_ROOT/.env`, falling back to `../.env` and then `./.env`; variables already set in the environment take precedence):

| Variable | Meaning |
|---|---|
| `GATEWAY_BIND` | listen address, default `127.0.0.1:8088` (`.env.example` sets `0.0.0.0:8088`) |
| `GATEWAY_KEYS` | `name:key,name:key`; the name is the client identity in logs and traces. If this is empty, auth is off, which is only allowed on a loopback bind. |
| `STACK_DB` | SQLite file for `gw_requests` (WAL mode, `busy_timeout` 5000). If unset, no request log is written. |
| `STACK_OTLP_ENDPOINT` | OTLP/HTTP base URL; spans go to `<endpoint>/v1/traces`. If unset, tracing is off. |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | used for Basic auth on the trace export, so the export also works when pointed straight at Langfuse |
| `GATEWAY_CAPTURE_CONTENT` | `true` (default) / `false`: whether prompts and outputs go into spans |
| `GATEWAY_CONFIG` | path of the route file; default `$STACK_ROOT/gateway/gateway.toml`, otherwise the built-in copy |
| `GATEWAY_THREADS` | tokio worker threads (default 2) |
| `RUST_LOG` | `error` / `warn` / `info` (default) / `debug` |
| `STACK_CHAT_MODEL`, `STACK_TASK_MODEL`, `STACK_EMBED_MODEL`, `UZU_*_PORT`, `AGENTS_PORT`, `OLLAMA_URL` | used by `gateway.toml` |

**Routes** live in [`gateway.toml`](gateway.toml). Strings can use `${VAR}` and `${VAR:-default}`, and a
model alias that expands to `""` is ignored. Edit the file, then run `just gateway::restart`.

| Model / path | Backend | Concurrency |
|---|---|---|
| `chat`, `$STACK_CHAT_MODEL` | uzu `:$UZU_CHAT_PORT` | 1 |
| `task`, `$STACK_TASK_MODEL` | uzu `:$UZU_TASK_PORT` | 1 |
| `agent` | agents `:$AGENTS_PORT` | 4 |
| `embed`, `embeddinggemma`, `$STACK_EMBED_MODEL` | Ollama `$OLLAMA_URL` (model is rewritten to `$STACK_EMBED_MODEL`) | 4 |
| `POST /v1/decisions`, `/v1/systemone` | uzu decide `:$UZU_DECIDE_PORT`; returns 503 if it is not running | 1 |

Routing works like this: a path listed in some backend's `paths` goes to that backend. Any
other `POST /v1/...` request is routed by its `model` value, and the client's path is appended
to the backend `url` unchanged.

### Adding a backend

```toml
[backends.ollama-chat]
url = "${OLLAMA_URL}"            # base URL, no /v1
models = ["small", "lfm3:1.7b"]  # values of "model" that route here
rewrite_model = "lfm3:1.7b"      # optional: model name the backend expects
max_concurrency = 1              # optional: default 1
# max_queue = 32                 # waiting requests before new ones get 503
# queue_timeout_secs = 120       # max wait in the queue -> 503 + Retry-After
# stream_usage = "inject"        # inject | native (uzu) | none
# health = "/v1/models"          # GET path used by /health
# api_key = "${SOME_KEY}"        # bearer key sent to the backend
# timeout_secs = 600             # time until response headers / full non-stream body
```

`stream_usage = "inject"` adds `stream_options.include_usage = true` to streaming chat requests,
so the backend reports token counts at the end of the stream (OpenAI and Ollama behave this way).
uzu always sends `usage` in its final chunk and ignores the request's `model` (it answers with
the id of the model it serves), so its routes use `native` and need no rewrite.

## Examples

```bash
set -a; . ./.env; set +a; G=http://127.0.0.1:8088

# chat
curl -s $G/v1/chat/completions -H "Authorization: Bearer $ME_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"chat","messages":[{"role":"user","content":"Hello"}]}'

# streaming, as a background job (waits behind interactive requests)
curl -sN $G/v1/chat/completions -H "Authorization: Bearer $ME_API_KEY" -H 'content-type: application/json' \
  -H 'x-stack-priority: background' \
  -d '{"model":"task","stream":true,"messages":[{"role":"user","content":"Summarize: ..."}]}'

# embeddings
curl -s $G/v1/embeddings -H "Authorization: Bearer $ME_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"embed","input":["first text","second text"]}'

# decisions (needs `just decide-on`; otherwise 503)
curl -s $G/v1/decisions -H "Authorization: Bearer $ME_API_KEY" -H 'content-type: application/json' \
  -d '{"state":"The parcel arrived damaged.","questions":{"route":{"type":"choice","instructions":"Which team?",
       "criteria":{"returns":"Returns and damage","billing":"Payments"}}}}'

curl -s $G/v1/models -H "Authorization: Bearer $ME_API_KEY"   # configured aliases
curl -s $G/health                                            # open; backend up/down, in flight, queued
```

## Priority and queueing

Each backend allows `max_concurrency` requests in flight. Requests beyond that wait in a queue:

- The `x-stack-priority: interactive | background` header sets the priority (default `interactive`).
  Interactive requests are always served first, and requests within the same class are served in arrival order (FIFO).
- When the queue is full (`max_queue`), the gateway returns `503` with `Retry-After: 5`. After
  `queue_timeout_secs` it returns `503` with `Retry-After: 10`. An unreachable backend gives `503`
  with `Retry-After: 30`. All errors use OpenAI-style JSON (`{"error":{"message","type","code"}}`).
- A client that disconnects while queued leaves the queue right away. If a client disconnects
  during a stream, the upstream request is cancelled and the slot is freed.
- The wait time is logged (`queue_ms`), and the service log notes any wait over 50 ms.

## What is logged and traced

Every response carries an `x-request-id` header. The gateway reuses the client's ID if one is sent, otherwise it generates one.

**SQLite `gw_requests`** has one row per request: `id, ts (UTC RFC 3339), client, model (as
requested), backend, endpoint, stream, status, queue_ms, latency_ms, ttft_ms (stream: first
content/reasoning/tool-call delta), prompt_tokens, completion_tokens, error, trace_id`. Status
`499` means the client disconnected. Rows go through a channel to a writer thread, so a slow or
broken database never blocks or fails a request; rows are dropped instead.

**OTLP** sends one span per request, as OTLP/HTTP JSON, to `$STACK_OTLP_ENDPOINT/v1/traces`. Spans are
batched every 500 ms or 128 spans. The buffer holds 2048 spans; when it is full, new spans are dropped and
counted in `/health` as `traces_dropped`. Each export carries the headers `x-langfuse-public-key: pk-lf-gateway`
and Basic `LANGFUSE_PUBLIC_KEY:LANGFUSE_SECRET_KEY`. The resource is `service.name=stack-gateway`.
Span attributes:

- `langfuse.observation.type` (`generation`, or `embedding` for embeddings)
- `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`
- `gen_ai.usage.input_tokens` / `output_tokens`, `gen_ai.request.temperature` / `max_tokens`
- `langfuse.observation.input` / `output`: JSON strings, cut at 64 KB. Input is the messages / input / decision state;
  output is the assistant message, or for embeddings only the count and dimensions. Leave these
  out with `GATEWAY_CAPTURE_CONTENT=false`.
- `langfuse.observation.completion_start_time` (time of the first token)
- `user.id` / `langfuse.user.id` (= client), `langfuse.trace.name` (root spans only)
- `gateway.request_id`, `gateway.backend`, `gateway.priority`, `gateway.queue_ms`, `gateway.ttft_ms`, `http.response.status_code`
- on errors: `langfuse.observation.level=ERROR` and the span status is set to error

If the request carries a W3C `traceparent`, the gateway span becomes its child, so an agent's
trace contains its LLM calls as generations. Downstream requests get a `traceparent` whose parent
is the gateway span, plus `x-request-id` and `x-stack-client: <client>`. The client's
`Authorization` header is never forwarded.
