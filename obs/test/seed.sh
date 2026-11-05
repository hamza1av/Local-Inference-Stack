#!/usr/bin/env bash
# Send a small mixed data set to the recorder (run via scripts/env-run.sh):
#   alpha  : Langfuse path + x-langfuse-public-key pk-lf-alpha         -> app:alpha
#   beta   : plain /v1/traces, no key, service.name=beta-svc           -> app:beta-svc
#   nokey  : no key, no service.name                                   -> app:unknown
#   uuidkey: real-looking Langfuse key pk-lf-<uuid> + service.name=gamma -> app:gamma
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
url="http://127.0.0.1:${RECORDER_PORT:-4320}"
python3 "$here/send_spans.py" --url "$url" --app alpha --traces "${1:-6}"
python3 "$here/send_spans.py" --url "$url" --path otlp --no-key --service beta-svc --traces 3
python3 "$here/send_spans.py" --url "$url" --no-key --traces 2
python3 "$here/send_spans.py" --url "$url" --app 0b6f1c2e-1111-4a4a-9c9c-123456789abc --service gamma --traces 2
