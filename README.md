# Local Inference Stack

Plug-and-play local LLM host for a **16 GB Apple Silicon Mac**: uzu models on Metal, a Rust
gateway in front, Open WebUI for people on the tailnet, plus RAG, a knowledge graph,
LangGraph agents and on-demand Langfuse tracing.

```bash
git clone … && cd Local-Inference-Stack
just install        # brew bundle, .env with secrets, models, services, Open WebUI
just doctor         # health + memory budget
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ports, model names, data layout and the
contracts between components.
