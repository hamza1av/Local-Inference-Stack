#!/usr/bin/env bash
# Health + budget check. Exit code = number of failed required checks.
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"; cd "$root"
set -a; [[ -f .env ]] && . ./.env; set +a
fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; fail=$((fail+1)); }
http() { curl -fsS -m 3 -o /dev/null "$@" 2>/dev/null; }
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }

GATEWAY_BIND="${GATEWAY_BIND:-0.0.0.0:8088}"; GATEWAY_PORT="${GATEWAY_BIND##*:}"
section "Host"
mem_gb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
echo "  $(sysctl -n machdep.cpu.brand_string), ${mem_gb} GB"
[[ -f .env ]] && ok ".env present" || bad ".env missing (just env)"
for t in docker just uv cargo ollama sqlite3; do command -v $t >/dev/null && ok "$t" || bad "$t not installed (brew bundle)"; done
command -v duckdb >/dev/null && ok duckdb || warn "duckdb not installed (optional: spool queries)"
[[ -x "${UZU_DIR:-}/target/release/cli" ]] && ok "uzu built" || bad "uzu not built (just uzu-build)"
docker info >/dev/null 2>&1 && ok "OrbStack/Docker running" || bad "container runtime not running"
orb_mem=$(orbctl config show 2>/dev/null | awk '/^memory_mib:/{print $2}')
[[ -n "$orb_mem" ]] && { [[ $orb_mem -le 4096 ]] && ok "OrbStack memory cap ${orb_mem} MiB" || warn "OrbStack memory cap ${orb_mem} MiB (16 GB host: orbctl config set memory_mib 4096)"; }

section "Models"
for p in "chat:${UZU_CHAT_PORT:-8000}:req" "task:${UZU_TASK_PORT:-8001}:req" "decide:${UZU_DECIDE_PORT:-8002}:opt"; do
  IFS=: read -r n port need <<<"$p"
  if http "http://127.0.0.1:$port/v1/models"; then ok "uzu $n :$port"
  elif [[ $need == req ]]; then bad "uzu $n :$port down (just logs uzu-$n)"
  else warn "uzu $n :$port off (just decide-on)"; fi
done
if http "${OLLAMA_URL:-http://127.0.0.1:11434}/api/version"; then
  ollama list 2>/dev/null | grep -q "^${STACK_EMBED_MODEL:-embeddinggemma}" && ok "ollama + ${STACK_EMBED_MODEL}" || bad "embedding model missing (just embed-pull)"
else bad "ollama down (brew services start ollama)"; fi

section "Services"
chk() { # name url required
  if http "$2"; then ok "$1"; elif [[ $3 == req ]]; then bad "$1 down ($2)"; else warn "$1 off ($2)"; fi; }
chk "gateway"        "http://127.0.0.1:${GATEWAY_PORT:-8088}/health" req
chk "Open WebUI"     "http://127.0.0.1:${OWUI_PORT:-3000}/health" req
chk "recorder"       "http://127.0.0.1:${RECORDER_PORT:-4320}/" opt
chk "kg MCP"         "http://127.0.0.1:${KG_MCP_PORT:-8090}/health" opt
chk "rag MCP"        "http://127.0.0.1:${RAG_MCP_PORT:-8091}/health" opt
chk "agents"         "http://127.0.0.1:${AGENTS_PORT:-8092}/health" opt
chk "Langfuse (on demand)" "${LANGFUSE_URL:-http://127.0.0.1:3020}/api/public/health" opt
(echo > /dev/tcp/127.0.0.1/${FALKORDB_PORT:-6390}) 2>/dev/null && ok "FalkorDB :${FALKORDB_PORT:-6390}" || warn "FalkorDB off"

section "Memory (RSS)"
total=0
while read -r pid label; do
  [[ "$pid" =~ ^[0-9]+$ ]] || continue
  kb=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' '); [[ -z "$kb" ]] && continue
  total=$((total+kb)); printf '  %-28s %6d MB\n' "$label" $((kb/1024))
done < <(launchctl list | awk '/local\.stack\./{print $1, $3}')
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null | grep '^stack-' | while read -r n u _; do printf '  %-28s %s\n' "$n" "$u"; done
echo "  host services total: $((total/1024)) MB (note: uzu weights are mmapped/wired and may show low RSS)"
[[ ${STACK_DATA:-} && -d "$STACK_DATA/spool" ]] && echo "  spool: $(du -sh "$STACK_DATA/spool" | cut -f1)"

echo; [[ $fail -eq 0 ]] && echo "all required checks passed" || echo "$fail check(s) failed"
exit $fail
