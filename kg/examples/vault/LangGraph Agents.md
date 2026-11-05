---
type: idea
status: draft
tags: [agents, langgraph, python]
created: 2026-01-18
---
# LangGraph Agents

An agents server built with LangGraph that exposes itself as the model `agent` through
the [[Gateway]].

## Tools
- The knowledge graph over MCP (search, context, remember) from [[FalkorDB]].
- Document search from [[RAG]].
- Web search.

## Open points
- Question: How many tool calls can a 9B model chain before it loses track?
- Todo: Add a tracing callback that reports each tool call to Langfuse
