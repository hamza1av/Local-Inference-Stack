---
type: reference
tags: [performance]
created: 2026-01-21
---
# Memory Budget

Target: 16 GB unified memory, with room left for a browser and an editor.

| Part | Approx. RSS |
|---|---|
| chat model (9B, 4-bit) | 6.5 GB |
| task model | 0.8 GB |
| embeddings (Ollama) | 0.7 GB |
| containers (UI, graph, recorder) | 1 GB |
| gateway, MCP servers, agents | 0.3 GB |

Everything heavy that is not needed all the time (a second 9B model, [[Langfuse]]) starts
on demand. #memory
