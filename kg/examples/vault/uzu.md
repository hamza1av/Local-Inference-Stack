---
type: reference
tags: [llm, uzu]
aliases: [inference engine]
created: 2026-01-11
---
# uzu

uzu is the inference engine for the chat and task models. It runs natively on Apple
Silicon (Metal) and exposes an OpenAI-style server per model.

## Models
- chat: a 9B model, 4-bit, around 6-7 GB resident.
- task: a sub-1B model for titles, tags and query rewriting.
- decide: an optional second 9B model started only on demand.

## Notes
Start and stop the servers as launchd agents. Only one 9B model fits next to the rest
of the stack, see [[Memory Budget]]. All clients reach uzu through the [[Gateway]].
