#!/usr/bin/env bash
# Manage host services as per-user launchd agents (label local.stack.<name>).
#
#   launchd.sh install <name> [--workdir DIR] [--no-keepalive] -- <command> [args...]
#   launchd.sh uninstall <name>
#   launchd.sh restart <name>
#   launchd.sh list
#
# The command runs via `env-run.sh`, which loads the repo's .env first.
# Logs: $STACK_DATA/logs/<name>.log
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
set -a; [[ -f "$root/.env" ]] && . "$root/.env"; set +a
: "${STACK_DATA:=$HOME/.local/share/inference-stack}"
agents="$HOME/Library/LaunchAgents"
uid="$(id -u)"

xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

cmd="${1:-}"; shift || true
case "$cmd" in
  install)
    name="$1"; shift
    workdir="$root"; keepalive=true
    while [[ $# -gt 0 && "$1" != "--" ]]; do
      case "$1" in
        --workdir) workdir="$2"; shift 2;;
        --no-keepalive) keepalive=false; shift;;
        *) echo "unknown option $1" >&2; exit 2;;
      esac
    done
    [[ "${1:-}" == "--" ]] && shift
    [[ $# -gt 0 ]] || { echo "missing command" >&2; exit 2; }
    label="local.stack.$name"; plist="$agents/$label.plist"
    mkdir -p "$agents" "$STACK_DATA/logs"
    args="    <string>$root/scripts/env-run.sh</string>"$'\n'
    for a in "$@"; do args+="    <string>$(printf '%s' "$a" | xml)</string>"$'\n'; done
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$args  </array>
  <key>WorkingDirectory</key><string>$workdir</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><$keepalive/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$STACK_DATA/logs/$name.log</string>
  <key>StandardErrorPath</key><string>$STACK_DATA/logs/$name.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$uid" "$plist"
    echo "installed $label (log: $STACK_DATA/logs/$name.log)";;
  uninstall)
    label="local.stack.$1"
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
    rm -f "$agents/$label.plist"; echo "removed $label";;
  restart)
    launchctl kickstart -k "gui/$uid/local.stack.$1";;
  list)
    launchctl list | awk 'NR==1 || /local\.stack\./';;
  *) sed -n '2,9p' "$0"; exit 2;;
esac
