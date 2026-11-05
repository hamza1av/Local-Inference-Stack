---
type: reference
status: active
tags: [rust, llm, security]
created: 2026-01-12
---
# Gateway

A small Rust service in front of every model server. It is the only OpenAI endpoint that
clients use. #api

## Responsibilities
- Route model names (`chat`, `task`, `agent`, `embed`) to [[uzu]], the agents server or Ollama.
- Check per-client API keys and log one row per request into SQLite.
- Send traces to the recorder so [[Langfuse]] can show them later.

## Why Rust
Low idle memory and a single static binary that launchd can keep alive. See [[Decisions]].
