# obs: always-on trace recorder + Langfuse on demand (see obs/README.md)
# Recipes run in obs/; env-run.sh loads the repo .env and sets STACK_ROOT.

run := "../scripts/env-run.sh ./obs.sh"

# start the always-on recorder (OTLP on 127.0.0.1:$RECORDER_PORT)
install:
    {{run}} install

# stop recorder, Langfuse and the nightly job (spool + Langfuse data are kept)
uninstall:
    {{run}} uninstall

# recorder health, spool size, Langfuse and nightly job state
status:
    {{run}} status

# start Langfuse, wait until healthy and the replayer runs, print URL + login
langfuse:
    {{run}} langfuse

# stop Langfuse (data kept)
langfuse-off:
    {{run}} langfuse-off

# start Langfuse, wait until the spool is loaded, stop it again (left on if it was running)
drain:
    {{run}} drain

# launchd job local.stack.obs-nightly: drain every day at 03:30
nightly-install:
    {{run}} nightly-install

nightly-uninstall:
    {{run}} nightly-uninstall

# delete all Langfuse data (asks; optionally the spool too). `just obs::purge --yes` skips the questions
purge *args:
    {{run}} purge {{args}}

# duckdb over the spool, no Langfuse needed: `just obs::spool` lists queries, `just obs::spool last-traces 50`
spool query="" n="":
    {{run}} spool {{query}} {{n}}

# replayer metrics (sent/queued spans) while Langfuse runs
metrics:
    {{run}} metrics

# send synthetic test traces to the recorder
test-send app="testapp" traces="5":
    ../scripts/env-run.sh python3 test/send_spans.py --app {{app}} --traces {{traces}}

# end-to-end test on the configured ports (starts/stops everything it needs; see test/e2e.sh)
test-e2e:
    ../scripts/env-run.sh ./test/e2e.sh
