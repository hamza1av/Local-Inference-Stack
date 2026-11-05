---
type: reference
status: active
tags: [llm, deployment]
aliases: [Stack, Overview]
created: 2026-01-10
---
# Local Inference Stack

A self-hosted LLM setup for one laptop with 16 GB of unified memory. Models run natively
on the host (Metal); containers only run plain services. #architecture

## Components
- [[uzu]] serves the chat and task models.
- [[Gateway]] is the only OpenAI-compatible endpoint clients talk to.
- [[RAG]] answers questions over dropped documents.
- [[FalkorDB]] holds the knowledge graph you are reading right now.
- [[LangGraph Agents]] combine the tools.
- [[Langfuse]] shows traces when needed.
- [[Open WebUI]] is the chat front end.

## Constraints
See [[Memory Budget]] and the [[Decisions]] log.
