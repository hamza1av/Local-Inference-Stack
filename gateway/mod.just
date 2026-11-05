# gateway — Rust OpenAI-compatible gateway (`just gateway::<recipe>`). See gateway/README.md.
# Recipes run in gateway/; commands go through ../scripts/env-run.sh so .env is loaded.

env := "../scripts/env-run.sh"
bin := source_directory() / "target/release/stack-gateway"

# list gateway recipes
default:
    @just --list gateway

# build the release binary
build:
    cargo build --release

# unit + integration tests (mock backend/collector, no network)
test:
    cargo test

# print the resolved routes
routes: build
    {{env}} {{bin}} --check

# run in the foreground (uses .env; stop the launchd service first if it holds the port)
run: build
    {{env}} {{bin}}

# build and install as launchd service local.stack.gateway
install: build
    ../scripts/launchd.sh install gateway --workdir "{{source_directory()}}" -- "{{bin}}"

# remove the launchd service
uninstall:
    ../scripts/launchd.sh uninstall gateway

# restart the launchd service (e.g. after editing gateway.toml)
restart:
    ../scripts/launchd.sh restart gateway

# health JSON (backends up/down, in flight, queued)
status:
    @{{env}} bash -c 'curl -fsS -m 5 "http://127.0.0.1:${GATEWAY_BIND##*:}/health" | python3 -m json.tool || { echo "gateway not reachable on :${GATEWAY_BIND##*:}"; exit 1; }'

# follow the service log
logs:
    {{env}} bash -c 'tail -f "$STACK_DATA/logs/gateway.log"'

# requests / tokens / latency per model and client over the last 24h (from gw_requests)
stats hours="24":
    @{{env}} bash -c 'sqlite3 -readonly -header -column -cmd ".parameter set @h {{hours}}" "$STACK_DB" < stats.sql'
