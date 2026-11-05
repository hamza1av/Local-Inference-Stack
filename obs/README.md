# obs: always-on trace recorder + Langfuse on demand

Langfuse is great to look at traces, but it is a 7-container stack (~1.8 GB RAM). On a
16 GB laptop that also runs local models it should not run all day. So recording and
viewing are split:

```
 gateway / agents / rag / kg / your scripts
        │  OTLP/HTTP (JSON or protobuf), header x-langfuse-public-key: pk-lf-<app>
        ▼
 recorder  127.0.0.1:4320   compose project stack-obs, always on, ~31 MB
        │  appends, rotates (50 MB × 41 files ≈ 2 GB cap)
        ▼
 $STACK_DATA/spool/traces*.jsonl ─────────► duckdb queries (obs/queries, no Langfuse needed)
        │
        ▼  (only while Langfuse runs)
 replayer  remembers file offsets + persistent send queue, tags app:<name>
        ▼
 Langfuse  127.0.0.1:3020   compose project stack-langfuse, on demand
           web, worker, ClickHouse, Postgres, Redis, MinIO
```

* The **recorder** is an OpenTelemetry Collector (contrib 0.162.0) with an OTLP/HTTP
  receiver and a file exporter. It accepts the Langfuse SDK path
  `/api/public/otel/v1/traces` *and* the plain OTLP path `/v1/traces` (any POST path is
  treated as traces). It does not check auth. Request headers `x-langfuse-public-key`,
  `x-langfuse-sdk-name`, `x-langfuse-sdk-version` are copied into resource attributes
  `langfuse.ingest.*` (the `Authorization` header is never stored).
* The **replayer** (same collector image, in the Langfuse project) reads the spool from
  the last stored offset, tags every trace and sends it to Langfuse. Offsets and the send
  queue live in the `replay_state` volume: nothing is sent twice or lost across
  restarts; when Langfuse is slow the reader blocks, it never drops; failed sends retry
  forever.
* **App tag**: every trace gets the tag `app:<name>` (filter by it in the Langfuse UI):
  1. public key `pk-lf-<name>` → `<name>` (UUID-style keys of real Langfuse projects are skipped),
  2. else `service.name` (unless it is the SDK default `unknown_service:*`),
  3. else `unknown`.
  Tags the app sent itself (`langfuse.trace.tags`, `langfuse.tags`,
  `ai.telemetry.metadata.tags`, `tag.tags`, as list, JSON string or `a,b`) are kept.

Everything lands in one Langfuse project `stack`; apps are told apart by tag.

## Commands

| `just obs::…` | |
|---|---|
| `install` | start the recorder (restarts with OrbStack) |
| `uninstall` | stop recorder, Langfuse and the nightly job; spool and Langfuse data are kept |
| `status` | recorder health, spool size, Langfuse / replayer / nightly job state |
| `langfuse` | start Langfuse, wait until it is healthy and the replayer runs, print URL + login |
| `langfuse-off` | stop Langfuse, keep its data |
| `drain` | start Langfuse, wait until the spool is fully loaded, stop it (left on if it was already running) |
| `nightly-install` / `nightly-uninstall` | launchd job `local.stack.obs-nightly`: `drain` daily at 03:30 (missed runs start at wake), log `$STACK_DATA/logs/obs-nightly.log` |
| `purge` | delete all Langfuse data incl. replay offsets (asks; then asks about the spool). A fresh Langfuse replays the whole spool again |
| `spool [query] [N]` | duckdb over the spool; without a query: list |
| `metrics` | replayer counters (sent / queued spans) while Langfuse runs |
| `test-send [app] [n]`, `test-e2e` | synthetic traces; full end-to-end test (own projects, temp data, alt ports, cleans up) |

Login: `LANGFUSE_ADMIN_EMAIL` / `LANGFUSE_ADMIN_PASSWORD` from `.env`, project keys
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`. These are applied by Langfuse's headless
init on the **first** start only; changing them later needs `purge` (or the UI).

**How `drain` knows it is done:** it polls the replayer's internal metrics
(`otelcol_exporter_queue_size`, `otelcol_exporter_sent_spans`, scraped from inside the
compose network, nothing published) and the total row count of Langfuse's ClickHouse
tables. When the queue is empty and neither number changed for `DRAIN_STABLE` seconds
(default 45: Langfuse ingests asynchronously through its worker), it stops Langfuse.
`DRAIN_TIMEOUT` (default 3600 s) bounds it. Anything still queued inside Langfuse (Redis/
MinIO) survives the stop and is processed on the next start.

## Spool queries (duckdb, no Langfuse needed)

```sh
just obs::spool                      # list
just obs::spool summary              # traces / spans / errors per app
just obs::spool tokens-per-day       # tokens per app per day
just obs::spool slowest-generations 10
just obs::spool errors
just obs::spool last-traces 50
```

`queries/_spans.sql` defines a view `spans` (one row per span: `app`, `trace_id`, `name`,
`start_ts`, `duration_ms`, `model`, `input_tokens`, `output_tokens`, `is_generation`,
`is_error`, `attrs` / `res` maps). Ad hoc:

```sh
SPOOL_GLOB="$STACK_DATA/spool/traces*.jsonl" duckdb -c ".read obs/queries/_spans.sql" \
  -c "select app, attrs['gen_ai.request.model'], count(*) from spans group by all"
```

## Sending traces

Endpoint for everything: `STACK_OTLP_ENDPOINT=http://127.0.0.1:4320/api/public/otel`.
Name your app with the public key `pk-lf-<app>`; the secret key is not checked by the
recorder (any value works).

**Langfuse SDK** (v3+ is OpenTelemetry based): point the base URL at the recorder:

```sh
LANGFUSE_HOST=http://127.0.0.1:4320        # python (LANGFUSE_BASE_URL in newer SDKs)
LANGFUSE_BASEURL=http://127.0.0.1:4320     # js/ts
LANGFUSE_PUBLIC_KEY=pk-lf-myapp
LANGFUSE_SECRET_KEY=sk-lf-anything
```

The SDK posts spans to `<base>/api/public/otel/v1/traces` with header
`x-langfuse-public-key`. SDK features that call other Langfuse REST endpoints (prompt
management, scores, datasets, media upload) need the real Langfuse
(`http://127.0.0.1:3020`, only while it runs).

**Plain OpenTelemetry** (any language):

```sh
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4320/v1/traces   # or .../api/public/otel/v1/traces
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf                           # http/json works too
OTEL_EXPORTER_OTLP_HEADERS=x-langfuse-public-key=pk-lf-myapp         # or set OTEL_SERVICE_NAME=myapp
```

Langfuse reads `gen_ai.*` semantic conventions (`gen_ai.request.model`,
`gen_ai.usage.input_tokens`, …) and `langfuse.*` attributes (`langfuse.trace.name`,
`langfuse.user.id`, `langfuse.session.id`, `langfuse.trace.tags`,
`langfuse.observation.type=generation`, `langfuse.observation.input/output`).

## Memory (measured with `docker stats`, OrbStack on an M4 Pro, after startup + a few traces)

| container | limit | measured |
|---|---|---|
| **recorder** (always on) | 64 MiB (`GOMEMLIMIT` 44 MiB) | **31 MiB** idle, 35-47 MiB while taking requests (incl. a 31 MB one) |
| langfuse-web | 1280 MiB (Node heap 1024) | 930-975 MiB |
| langfuse-worker | 768 MiB (heap 384) | 435-490 MiB |
| clickhouse | 768 MiB | 115-145 MiB |
| replayer | 256 MiB (`GOMEMLIMIT` 200 MiB) | 120-160 MiB |
| postgres | 256 MiB | 65-80 MiB |
| minio | 256 MiB | 55-60 MiB |
| redis | 128 MiB (maxmemory 96 MB) | 10 MiB |
| **Langfuse stack total** | 3.7 GiB of limits | **~1.8 GiB** |

So: ~31 MiB all day, ~1.8 GiB only while Langfuse runs (`langfuse-off` / `drain` give it back).

ClickHouse: pinned to 26.3.62.3 (LTS; Langfuse 4.5 migrations use full-text index syntax that 25.8 rejects), caches cut to a few MB (`langfuse/clickhouse/low-memory.xml`),
server memory capped at 80 % of its container limit, all system log tables
(`query_log`, `trace_log`, `metric_log`, `asynchronous_metric_log`, `part_log`,
`processors_profile_log`, `text_log`, …) disabled, `max_threads=2` per query.
Langfuse web is the big one: with a 384 or 640 MB heap it dies (`JavaScript heap out of memory`) during init / first ingestion, 1024 MB is stable.

## Ports / limits

| What | Default | Env |
|---|---|---|
| recorder OTLP | 127.0.0.1:4320 | `RECORDER_PORT` |
| recorder health (`GET /health`) | 127.0.0.1:4321 | `RECORDER_HEALTH_PORT` |
| Langfuse UI/API | 127.0.0.1:3020 | `LANGFUSE_PORT`, `LANGFUSE_URL` |
| MinIO S3 API (presigned media URLs, browser) | 127.0.0.1:3021 | `LANGFUSE_MINIO_PORT` |

ClickHouse, Postgres, Redis, the worker and the replayer are not published.
Optional knobs: `RECORDER_MEM_LIMIT`, `RECORDER_ROTATE_MB` (50), `RECORDER_MAX_BACKUPS`
(40), `LANGFUSE_VERSION` (4), `LANGFUSE_CLICKHOUSE_VERSION`, `LANGFUSE_WEB_HEAP_MB`,
`LANGFUSE_WORKER_HEAP_MB`, `DRAIN_STABLE`, `DRAIN_TIMEOUT`, `OBS_NIGHTLY_HOUR/MINUTE`.

## Limits / gotchas

* Rotation deletes the oldest spool files when the cap is reached, replayed or not.
  Keep the nightly drain on (`just obs::nightly-install`) or raise `RECORDER_MAX_BACKUPS`.
* Requests above 32 MiB are rejected (413); the recorder stays within 64 MiB.
* Do not truncate/edit `traces.jsonl` while the recorder runs (it writes at its own
  offset; the file gets a hole of NUL bytes). Stop it first or delete whole files.
* Langfuse v4 runs in "events only" mode: the old `GET /api/public/traces` endpoint is
  gone, use `GET /api/public/v2/observations` (see `test/verify_langfuse.py`).
