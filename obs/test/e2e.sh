#!/usr/bin/env bash
# End-to-end test: recorder -> spool -> duckdb -> Langfuse (replayer, app tags) -> drain.
# Run via scripts/env-run.sh (just obs::test-e2e). Uses its own compose projects
# (stack-obs-e2e, stack-langfuse-e2e), a temp STACK_DATA and, by default, ports that do
# not clash with a running installation. Everything is removed at the end (down -v).
#
#   E2E_RECORDER_PORT=4320 E2E_LANGFUSE_PORT=3020 ./test/e2e.sh   # use the real ports
#   E2E_KEEP=1 ./test/e2e.sh                                     # leave it running
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; obs="$here/.."
export STACK_DATA="$(mktemp -d "${TMPDIR:-/tmp}/obs-e2e.XXXXXX")"
export OBS_REC_PROJECT=stack-obs-e2e OBS_LF_PROJECT=stack-langfuse-e2e
export RECORDER_PORT="${E2E_RECORDER_PORT:-14320}" RECORDER_HEALTH_PORT="${E2E_RECORDER_HEALTH_PORT:-14321}"
export LANGFUSE_PORT="${E2E_LANGFUSE_PORT:-13020}" LANGFUSE_MINIO_PORT="${E2E_LANGFUSE_MINIO_PORT:-13021}"
export LANGFUSE_URL="http://127.0.0.1:$LANGFUSE_PORT"
export DRAIN_STABLE="${DRAIN_STABLE:-30}"
url="http://127.0.0.1:$RECORDER_PORT"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
cleanup() {
  [[ -n "${E2E_KEEP:-}" ]] && { echo "kept: STACK_DATA=$STACK_DATA"; return; }
  step "cleanup"
  docker compose -f "$obs/langfuse/compose.yml" -p "$OBS_LF_PROJECT" down -v 2>&1 | tail -1 || true
  docker compose -f "$obs/recorder/compose.yml" -p "$OBS_REC_PROJECT" down -v 2>&1 | tail -1 || true
  rm -rf "$STACK_DATA"
}
trap cleanup EXIT
send() { python3 "$here/send_spans.py" --url "$url" "$@" | grep -E '^[0-9a-f]{32}$'; }

step "recorder up on :$RECORDER_PORT (spool $STACK_DATA/spool)"
"$obs/obs.sh" install
curl -fsS "http://127.0.0.1:$RECORDER_HEALTH_PORT/health"; echo

step "send spans (Langfuse path with key, plain OTLP path, no key, UUID key)"
expect=()
for t in $(send --app alpha --traces 4); do expect+=("alpha:$t"); done
for t in $(send --path otlp --no-key --service beta-svc --traces 3); do expect+=("beta-svc:$t"); done
for t in $(send --no-key --traces 2); do expect+=("unknown:$t"); done
for t in $(send --app 0b6f1c2e-1111-4a4a-9c9c-123456789abc --service gamma --traces 2); do expect+=("gamma:$t"); done
for t in $(send --app bigapp --traces 1 --pad 4000000); do expect+=("bigapp:$t"); done
echo "${#expect[@]} traces sent"
for _ in $(seq 20); do [[ $(cat "$STACK_DATA"/spool/traces*.jsonl 2>/dev/null | wc -l) -ge 5 ]] && break; sleep 0.5; done
ls -la "$STACK_DATA/spool"

if command -v duckdb >/dev/null; then
  step "duckdb over the spool"
  for q in summary tokens-per-day slowest-generations errors last-traces; do echo "-- $q"; "$obs/obs.sh" spool "$q" 5; done
  "$obs/obs.sh" spool summary | grep -q alpha || { echo "duckdb: app alpha missing" >&2; exit 1; }
fi

step "Langfuse up, replayer forwards the spool"
"$obs/obs.sh" langfuse
python3 -u "$here/verify_langfuse.py" --timeout 300 "${expect[@]}" || {
  docker compose -f "$obs/langfuse/compose.yml" -p "$OBS_LF_PROJECT" ps -a; exit 1; }

step "memory after startup"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep -E "^($OBS_LF_PROJECT|$OBS_REC_PROJECT)-" | tee "$STACK_DATA/mem.txt"
awk '{v=$2; u=v; sub(/[0-9.]+/,"",u); sub(/[A-Za-z]+/,"",v); if(u=="GiB")v*=1024; if(u=="KiB")v/=1024; s+=v} END {printf "total %.0f MiB\n", s}' "$STACK_DATA/mem.txt"

step "drain: Langfuse off, record more, drain, check ClickHouse"
"$obs/obs.sh" langfuse-off
late=$(send --app delta --traces 2)
"$obs/obs.sh" drain
running=$(docker compose -f "$obs/langfuse/compose.yml" -p "$OBS_LF_PROJECT" ps -q --status running | wc -l | tr -d ' ')
[[ "$running" == 0 ]] || { echo "drain left $running container(s) running" >&2; exit 1; }
docker compose -f "$obs/langfuse/compose.yml" -p "$OBS_LF_PROJECT" up -d --wait clickhouse >/dev/null 2>&1
ids=$(printf "'%s'," $late); ids="${ids%,}"
n=$("$obs/obs.sh" ch "SELECT uniqExact(trace_id) FROM events_core WHERE trace_id IN ($ids)")
echo "late traces in ClickHouse after drain: $n/2"
[[ "$n" == 2 ]] || { echo "drain did not load the late traces" >&2; exit 1; }

step "PASS"
