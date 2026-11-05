#!/usr/bin/env bash
# Create .env from .env.example: fill GENERATED secrets, expand ${VARS} to absolute values.
# Existing .env is kept; keys missing from it are appended (so upgrades add new settings).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
example="$root/.env.example"
target="$root/.env"

rand() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }

# bash 3.2 compatible (stock macOS): no associative arrays
has_key() { [[ -f "$target" ]] && grep -q "^$1=" "$target"; }
if [[ -f "$target" ]]; then
  # make existing values available for ${VAR} expansion of new keys
  set -a; . "$target"; set +a
fi

added=0
out=""
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]]; then
    key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
    has_key "$key" && continue
    val="${val//GENERATED_HEX64/$(openssl rand -hex 32)}"
    while [[ "$val" == *GENERATED* ]]; do val="${val/GENERATED/$(rand 32)}"; done
    val="$(eval "printf '%s' \"$val\"")"   # expand ${HOME}, ${STACK_DATA}, ...
    export "$key=$val"
    out+="$key=$val"$'\n'; added=$((added+1))
  elif [[ ! -f "$target" ]]; then
    out+="$line"$'\n'
  fi
done < "$example"

if [[ -f "$target" ]]; then
  [[ $added -gt 0 ]] && printf '\n# added %s\n%s' "$(date +%F)" "$out" >> "$target"
else
  printf '%s' "$out" > "$target"; chmod 600 "$target"
fi
echo ".env: $added value(s) written"
