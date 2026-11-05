# Local inference stack — `just` lists everything. See README.md / docs/ARCHITECTURE.md.
set dotenv-load := true
set dotenv-path := ".env"

# component modules (each lives in <component>/mod.just; missing ones are skipped)
mod? gateway
mod? obs
mod? kg
mod? rag
mod? agents

compose_core := "docker compose -f core/compose.yml --env-file .env"
uzu := env_var_or_default("UZU_DIR", env_var("HOME") / "Projects/HOST_LLM/uzu") / "target/release/cli"

default:
    @just --list --list-submodules

# first-time setup: tools, .env, data dirs, everything installed and started
install:
    ./scripts/install.sh

# stop and remove services (keeps data); --purge also deletes data and volumes
uninstall *args:
    ./scripts/uninstall.sh {{args}}

# create/complete .env (keeps existing values)
env:
    ./scripts/gen-env.sh

# check prerequisites, memory budget and health of every service
doctor:
    ./scripts/doctor.sh

# --- core UI (Open WebUI + SearXNG) -------------------------------------------
up:
    {{compose_core}} up -d

down:
    {{compose_core}} down

update:
    {{compose_core}} pull && {{compose_core}} up -d

# --- models (uzu, native, launchd) --------------------------------------------
# build uzu (once / after git pull). Needs full Xcode for the Metal compiler; if
# xcode-select points at the Command Line Tools we use Xcode.app via DEVELOPER_DIR.
uzu-build:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! xcrun --find metal >/dev/null 2>&1 && [[ -d /Applications/Xcode.app ]]; then
      export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
    fi
    xcrun --find metal >/dev/null 2>&1 || { echo "Metal compiler missing: install Xcode, then: xcodebuild -downloadComponent MetalToolchain"; exit 1; }
    cd "$UZU_DIR" && cargo build --release -p cli

# install chat + task model servers as launchd agents
models-install:
    ./scripts/launchd.sh install uzu-chat -- {{uzu}} server --model "$STACK_CHAT_MODEL" --port "$UZU_CHAT_PORT"
    ./scripts/launchd.sh install uzu-task -- {{uzu}} server --model "$STACK_TASK_MODEL" --port "$UZU_TASK_PORT"

models-uninstall:
    ./scripts/launchd.sh uninstall uzu-chat
    ./scripts/launchd.sh uninstall uzu-task
    ./scripts/launchd.sh uninstall uzu-decide

# start the decision model (another ~6 GB: on 16 GB stop the chat model first or use a 4B chat model)
decide-on:
    ./scripts/launchd.sh install uzu-decide -- {{uzu}} server --model "$STACK_DECIDE_MODEL" --port "$UZU_DECIDE_PORT"

decide-off:
    ./scripts/launchd.sh uninstall uzu-decide

# restart one host service, e.g. `just restart uzu-chat`
restart name:
    ./scripts/launchd.sh restart {{name}}

# list host services
services:
    ./scripts/launchd.sh list

# follow a host service log, e.g. `just logs gateway`
logs name:
    tail -f "$STACK_DATA/logs/{{name}}.log"

# pull the embedding model into Ollama
embed-pull:
    ollama pull "$STACK_EMBED_MODEL"
