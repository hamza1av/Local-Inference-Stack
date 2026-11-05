#!/usr/bin/env bash
# Stop and remove every service. Data stays unless --purge.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"; cd "$root"
purge=false; [[ "${1:-}" == "--purge" ]] && purge=true
set -a; [[ -f .env ]] && . ./.env; set +a
has_mod() { just --summary 2>/dev/null | tr ' ' '\n' | grep -qx "$1::uninstall"; }

for m in agents rag kg gateway obs; do has_mod "$m" && just "$m::uninstall" || true; done
just models-uninstall || true
v=""; $purge && v="-v"
docker compose -f core/compose.yml --env-file .env down $v || true
tailscale serve reset >/dev/null 2>&1 || true
if $purge; then
  read -r -p "Delete $STACK_DATA (database, spool, documents)? [y/N] " a
  [[ "$a" == y ]] && rm -rf "$STACK_DATA"
fi
echo "done"
