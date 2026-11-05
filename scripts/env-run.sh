#!/usr/bin/env bash
# Run a command with the repo's .env loaded (used by launchd services and just recipes).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
set -a; [[ -f "$root/.env" ]] && . "$root/.env"; set +a
export STACK_ROOT="$root"
exec "$@"
