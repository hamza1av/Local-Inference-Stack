#!/usr/bin/env bash
# One-shot setup on the host Mac. Safe to re-run.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"; cd "$root"
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
has_mod() { just --summary 2>/dev/null | tr ' ' '\n' | grep -qx "$1::install"; }

step "Homebrew packages"
command -v brew >/dev/null || { echo "install Homebrew first: https://brew.sh"; exit 1; }
brew bundle --file Brewfile

step ".env"
./scripts/gen-env.sh
set -a; . ./.env; set +a
mkdir -p "$STACK_DATA"/{logs,spool,rag/inbox,kg/vault}

step "OrbStack"
open -ga OrbStack || true
for _ in $(seq 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
orbctl config set memory_mib "${ORB_MEMORY_MIB:-4096}" >/dev/null 2>&1 || true
orbctl config set app.start_at_login true >/dev/null 2>&1 || true

step "Ollama + embedding model"
brew services start ollama >/dev/null 2>&1 || true
for _ in $(seq 15); do curl -fs "$OLLAMA_URL/api/version" >/dev/null && break; sleep 1; done
ollama pull "$STACK_EMBED_MODEL"

step "uzu models"
[[ -x "$UZU_DIR/target/release/cli" ]] || just uzu-build
just models-install

# components in dependency order; each one is optional
for m in obs gateway kg rag agents; do
  if has_mod "$m"; then step "$m"; just "$m::install"; fi
done

step "Open WebUI"
just up

step "Tailscale"
if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then
  tailscale serve --bg "${OWUI_PORT:-3000}" >/dev/null
  echo "Open WebUI: https://$(tailscale status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
else
  echo "Tailscale not logged in: skipped (later: tailscale serve --bg ${OWUI_PORT:-3000})"
fi

step "Check"
./scripts/doctor.sh || true
echo; echo "Open http://localhost:${OWUI_PORT:-3000} and sign up first: that account becomes admin."
