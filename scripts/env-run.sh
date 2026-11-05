#!/usr/bin/env bash
# Run a command with the repo's .env loaded (used by launchd services and just recipes).
# Variables already set in the caller's environment win over .env, so
# `STACK_DB=/tmp/test.db scripts/env-run.sh …` works as expected.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$root/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
    [[ -n "${!key+x}" ]] && continue
    val="${val%\"}"; val="${val#\"}"
    export "$key=$val"
  done < "$root/.env"
fi
export STACK_ROOT="$root"
exec "$@"
