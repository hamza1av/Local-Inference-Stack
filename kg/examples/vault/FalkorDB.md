---
type: reference
tags: [knowledge-graph, falkordb]
aliases: [graph database]
created: 2026-01-15
---
# FalkorDB

Graph database on top of Redis, queried with openCypher. It runs as a small container and
stores the knowledge graph: notes, tags, links and remembered facts.

## Indexes
- Full-text index on note titles and bodies.
- Vector index on note embeddings (cosine), filled from Ollama.
- Hybrid search fuses both rankings with reciprocal rank fusion.

## Operations
Persistence is AOF plus snapshots. The browser UI is optional and can be switched off to
save memory. The graph is only an index: it can be rebuilt from the notes at any time.

- 2026-01-15: Gotcha: The browser UI inside the container connects to port 6379, not the published port — why: it runs in the same container
