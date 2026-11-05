#!/usr/bin/env bash
# obs component driver (recorder, Langfuse on demand, drain, nightly job, spool queries).
# Run via scripts/env-run.sh (mod.just does): needs the repo .env loaded.
#
#   obs.sh install | uninstall | status
#   obs.sh langfuse | langfuse-off | drain | purge [--yes]
#   obs.sh nightly-install | nightly-uninstall
#   obs.sh spool [query] [N]       # duckdb over the spool; no query = list
#   obs.sh metrics | ch <sql>      # debugging helpers
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="${STACK_ROOT:-$(cd "$here/.." && pwd)}"
: "${STACK_DATA:?STACK_DATA not set: run via scripts/env-run.sh}"
spool="$STACK_DATA/spool"
# project names come from the compose files (stack-obs / stack-langfuse); the e2e test
# overrides them to stay away from a real installation
rec=(docker compose -f "$here/recorder/compose.yml" ${OBS_REC_PROJECT:+-p "$OBS_REC_PROJECT"})
lf=(docker compose -f "$here/langfuse/compose.yml" ${OBS_LF_PROJECT:+-p "$OBS_LF_PROJECT"})
label="local.stack.obs-nightly"
plist="$HOME/Library/LaunchAgents/$label.plist"
health="http://127.0.0.1:${RECORDER_HEALTH_PORT:-4321}/health"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { echo "obs: $*" >&2; exit 1; }
need_docker() {
  docker info >/dev/null 2>&1 && return 0
  # launchd at 03:30: give OrbStack a chance to come up
  open -ga OrbStack 2>/dev/null || true
  for _ in $(seq 30); do docker info >/dev/null 2>&1 && return 0; sleep 2; done
  die "container runtime not running"
}
running() { [[ -n "$("${lf[@]}" ps -q --status running "$1" 2>/dev/null)" ]]; }

# --- helpers that run inside the compose network (nothing extra is published) ---
ch() { # ClickHouse query → TSV
  docker exec -i "$("${lf[@]}" ps -q clickhouse)" clickhouse-client --user clickhouse \
    --password "$LANGFUSE_CLICKHOUSE_PASSWORD" -q "$1"
}
replayer_metrics() { # Prometheus text of the replayer's internal telemetry
  docker exec "$("${lf[@]}" ps -q langfuse-web)" node -e \
    'fetch("http://replayer:8888/metrics").then(r=>r.text()).then(t=>process.stdout.write(t)).catch(()=>process.exit(1))'
}
metric() { # sum of a metric family over all label sets (0 when absent)
  awk -v m="$1" '$1 ~ "^"m"(_total)?({|$)" {s+=$NF} END {printf "%d\n", s}'
}

recorder_up() {
  mkdir -p "$spool"
  "${rec[@]}" up -d --remove-orphans
  for _ in $(seq 30); do curl -fs -m 2 -o /dev/null "$health" && { echo "recorder: OTLP http://127.0.0.1:${RECORDER_PORT:-4320} (health $health)"; return; }; sleep 1; done
  die "recorder did not become healthy ($health)"
}

langfuse_up() {
  need_docker; mkdir -p "$spool"
  say "starting Langfuse (first start: image pulls + migrations take a few minutes)"
  "${lf[@]}" up -d --wait --wait-timeout "${LANGFUSE_WAIT:-600}" || {
    "${lf[@]}" ps -a; die "Langfuse did not become healthy: ${lf[*]} logs"; }
  running replayer || die "replayer not running: ${lf[*]} logs replayer"
}

print_login() {
  cat <<EOF
Langfuse   ${LANGFUSE_URL:-http://127.0.0.1:${LANGFUSE_PORT:-3020}}
login      ${LANGFUSE_ADMIN_EMAIL} / ${LANGFUSE_ADMIN_PASSWORD}
project    stack (keys LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in .env); filter traces by tag app:<name>
EOF
}

# Done when the replayer has nothing queued, has not sent anything new and
# ClickHouse row counts did not change for DRAIN_STABLE seconds (Langfuse ingests
# asynchronously: worker batches + queue delay, so a quiet window is required).
wait_caught_up() {
  local stable="${DRAIN_STABLE:-45}" timeout="${DRAIN_TIMEOUT:-3600}" t0 last="" since m q sent failed rows sig line n=0
  t0=$(date +%s); since=$t0
  while :; do
    m="$(replayer_metrics 2>/dev/null || true)"
    q=$(metric otelcol_exporter_queue_size <<<"$m")
    sent=$(metric otelcol_exporter_sent_spans <<<"$m")
    failed=$(metric otelcol_exporter_send_failed_spans <<<"$m")
    rows=$(ch "SELECT sum(rows) FROM system.parts WHERE active AND database = currentDatabase()" 2>/dev/null || echo "?")
    sig="$sent/$rows"
    if [[ -z "$m" || "$rows" == "?" || "$q" != 0 || "$sig" != "$last" ]]; then last="$sig"; since=$(date +%s); fi
    line=$(printf 'replayer sent=%s queued=%s failed=%s  clickhouse rows=%s  quiet=%ss' "$sent" "$q" "$failed" "$rows" $(( $(date +%s) - since )))
    if [[ -t 1 ]]; then printf '\r  %s   ' "$line"; elif (( ++n % 10 == 1 )); then echo "  $line"; fi
    (( $(date +%s) - since >= stable )) && { [[ -t 1 ]] && echo; echo "  caught up: $line"; return 0; }
    (( $(date +%s) - t0 >= timeout )) && { echo; echo "obs: drain timed out after ${timeout}s" >&2; return 1; }
    sleep 3
  done
}

cmd="${1:-status}"; shift || true
case "$cmd" in
  install)
    need_docker; recorder_up;;

  uninstall)
    "$0" nightly-uninstall
    "${lf[@]}" down 2>/dev/null || true
    "${rec[@]}" down
    echo "obs removed (spool kept in $spool, Langfuse data kept in volumes; obs::purge deletes them)";;

  status)
    say "recorder (stack-obs)"
    if curl -fs -m 2 -o /dev/null "$health"; then echo "  up: OTLP http://127.0.0.1:${RECORDER_PORT:-4320}  health $health"
    else echo "  down ($health)"; fi
    say "spool ($spool)"
    if [[ -d "$spool" ]]; then
      n=$(find "$spool" -name 'traces*.jsonl' | wc -l | tr -d ' ')
      echo "  $n file(s), $(du -sh "$spool" | cut -f1)"
    else echo "  missing"; fi
    say "Langfuse (stack-langfuse, on demand)"
    if running langfuse-web; then
      echo "  up: ${LANGFUSE_URL:-http://127.0.0.1:3020}"
      running replayer && echo "  replayer running" || echo "  replayer NOT running"
    else echo "  off (just obs::langfuse)"; fi
    say "nightly drain ($label)"
    if [[ -f "$plist" ]]; then echo "  installed: $(launchctl list 2>/dev/null | awk -v l="$label" '$3==l{print "last exit " $2}')"
    else echo "  not installed (just obs::nightly-install)"; fi;;

  langfuse)
    langfuse_up; print_login;;

  langfuse-off)
    "${lf[@]}" down; echo "Langfuse stopped (data kept)";;

  drain)
    need_docker
    was_up=false; running langfuse-web && running replayer && was_up=true
    $was_up || langfuse_up
    say "$(date '+%F %T') draining spool into Langfuse"
    ok=0; wait_caught_up || ok=$?
    if $was_up; then echo "Langfuse was already running: left on"
    else "${lf[@]}" down; echo "Langfuse stopped"; fi
    say "$(date '+%F %T') drain finished (exit $ok)"
    exit $ok;;

  purge)
    [[ "${1:-}" == "--yes" ]] || {
      read -r -p "Delete ALL Langfuse data (volumes of stack-langfuse incl. replay offsets)? [y/N] " a
      [[ "$a" == y || "$a" == Y ]] || { echo "aborted"; exit 1; }; }
    "${lf[@]}" down -v
    echo "Langfuse data deleted. The spool ($spool) is kept: the next obs::langfuse replays it from the start."
    if [[ "${1:-}" != "--yes" && -d "$spool" ]]; then
      read -r -p "Also delete the recorded spool $spool? [y/N] " a
      [[ "$a" == y || "$a" == Y ]] && find "$spool" -name 'traces*.jsonl' -delete && echo "spool deleted"
    fi;;

  nightly-render) # nightly-render <out.plist>: only write the plist (used by nightly-install)
    out="${1:?usage: obs.sh nightly-render <file>}"
    x() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/|/\\|/g' <<<"$1"; }
    sed -e "s|@LABEL@|$label|g" \
        -e "s|@ENV_RUN@|$(x "$root/scripts/env-run.sh")|g" \
        -e "s|@OBS_SH@|$(x "$here/obs.sh")|g" \
        -e "s|@ROOT@|$(x "$root")|g" \
        -e "s|@LOG@|$(x "$STACK_DATA/logs/obs-nightly.log")|g" \
        -e "s|@HOUR@|${OBS_NIGHTLY_HOUR:-3}|g" -e "s|@MINUTE@|${OBS_NIGHTLY_MINUTE:-30}|g" \
        -e "s|@PATH@|$HOME/.orbstack/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin|g" \
        "$here/launchd/obs-nightly.plist.in" > "$out"
    plutil -lint "$out" >/dev/null || die "invalid plist $out";;

  nightly-install)
    mkdir -p "$HOME/Library/LaunchAgents" "$STACK_DATA/logs"
    "$0" nightly-render "$plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "installed $label: drain daily at ${OBS_NIGHTLY_HOUR:-3}:$(printf %02d "${OBS_NIGHTLY_MINUTE:-30}") (log $STACK_DATA/logs/obs-nightly.log)";;

  nightly-uninstall)
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$plist"; echo "removed $label";;

  spool)
    command -v duckdb >/dev/null || die "duckdb not installed (brew install duckdb)"
    q="${1:-}"
    if [[ -z "$q" ]]; then
      echo "queries (just obs::spool <name> [N]):"
      for f in "$here"/queries/[!_]*.sql; do printf '  %-22s %s\n' "$(basename "$f" .sql)" "$(head -1 "$f" | sed 's/^-- *//')"; done
      exit 0
    fi
    f="$here/queries/$q.sql"; [[ -f "$f" ]] || die "no query $q (just obs::spool lists them)"
    compgen -G "$spool/traces*.jsonl" >/dev/null || die "spool is empty ($spool)"
    SPOOL_GLOB="$spool/traces*.jsonl" N="${2:-}" duckdb -no-agent -c ".read $here/queries/_spans.sql" -c ".read $f" 2>/dev/null \
      || SPOOL_GLOB="$spool/traces*.jsonl" N="${2:-}" duckdb -c ".read $here/queries/_spans.sql" -c ".read $f";;

  metrics) replayer_metrics | grep -E '^otelcol_(exporter|receiver)';;
  ch) ch "$1";;
  *) sed -n '2,9p' "$0"; exit 2;;
esac
