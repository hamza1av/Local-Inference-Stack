---
type: decision
tags: [architecture]
created: 2026-01-22
---
# Decisions

Architecture decisions for the [[Local Inference Stack]], newest last.

## Log
- 2026-01-10: Decision: Run models natively on the host instead of in containers — why: containers on macOS have no Metal GPU access
- 2026-01-12: Decision: Write the gateway in Rust — why: low idle memory and one binary for launchd
- 2026-01-15: Decision: Use FalkorDB for the knowledge graph — why: Cypher, full-text and vector search in one small container
- 2026-01-20: Decision: Keep Langfuse off by default and record traces to a spool — why: it needs several GB of RAM
