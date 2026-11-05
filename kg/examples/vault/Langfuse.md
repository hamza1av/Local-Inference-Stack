---
type: howto
tags: [observability, langfuse]
created: 2026-01-20
---
# Langfuse

Langfuse is heavy (Postgres, ClickHouse, Redis, object storage), so it is not always on.
A tiny recorder accepts OTLP traces all the time and appends them to a spool folder.

## On demand
Start Langfuse when you want to look at traces; a replayer loads the spool into it.
Stop it again afterwards to give the memory back to the models. See [[Memory Budget]].
