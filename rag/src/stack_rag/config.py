"""Settings from the environment (repo-root `.env`, see docs/ARCHITECTURE.md).

Precedence: real environment > `$STACK_ROOT/.env` > defaults. `.env` never overrides
variables that are already set, so tests and one-off runs can point anything elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

_loaded = False


def _find_root() -> Path | None:
    if root := os.environ.get("STACK_ROOT"):
        return Path(root)
    # rag/src/stack_rag/config.py -> repo root is 3 levels above the package dir
    for base in (Path.cwd(), Path(__file__).resolve()):
        for p in (base, *base.parents):
            if (p / ".env.example").is_file():
                return p
    return None


def load_env() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    root = _find_root()
    if root and (root / ".env").is_file():
        from dotenv import load_dotenv

        load_dotenv(root / ".env", override=False)


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _data_dir() -> Path:
    return Path(os.environ.get("STACK_DATA") or Path.home() / ".local/share/inference-stack")


@dataclass(frozen=True)
class Config:
    db_path: Path
    data_dir: Path
    inbox: Path
    # embeddings
    ollama_url: str = "http://127.0.0.1:11434"
    embed_model: str = "embeddinggemma"
    embed_dim: int = 768
    embed_via_gateway: bool = False
    gateway_url: str = "http://127.0.0.1:8088/v1"
    api_key: str = ""
    embed_batch: int = 16
    # chunking (approximate tokens, 1 token ~ 4 chars)
    chunk_tokens: int = 450
    chunk_overlap: int = 60
    # answer LLM
    llm_base_url: str = "http://127.0.0.1:8088/v1"
    llm_model: str = "chat"
    llm_api_key: str = ""
    # server
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8091
    watch_interval: float = 10.0

    @classmethod
    def from_env(cls) -> Config:
        load_env()
        e = os.environ.get
        data = _data_dir()
        gateway = (e("GATEWAY_URL") or "http://127.0.0.1:8088/v1").rstrip("/")
        api_key = e("STACK_API_KEY") or ""
        return cls(
            db_path=Path(e("STACK_DB") or data / "stack.db"),
            data_dir=data,
            inbox=Path(e("RAG_INBOX") or data / "rag" / "inbox"),
            ollama_url=(e("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/"),
            embed_model=e("STACK_EMBED_MODEL") or "embeddinggemma",
            embed_dim=int(e("STACK_EMBED_DIM") or 768),
            embed_via_gateway=_bool(e("RAG_EMBED_VIA_GATEWAY")),
            gateway_url=gateway,
            api_key=api_key,
            embed_batch=int(e("RAG_EMBED_BATCH") or 16),
            chunk_tokens=int(e("RAG_CHUNK_TOKENS") or 450),
            chunk_overlap=int(e("RAG_CHUNK_OVERLAP") or 60),
            llm_base_url=(e("RAG_LLM_BASE_URL") or gateway).rstrip("/"),
            llm_model=e("RAG_LLM_MODEL") or "chat",
            llm_api_key=e("RAG_LLM_API_KEY") or api_key,
            mcp_host=e("RAG_MCP_HOST") or "127.0.0.1",
            mcp_port=int(e("RAG_MCP_PORT") or 8091),
            watch_interval=float(e("RAG_WATCH_INTERVAL") or 10),
        )

    def with_(self, **kw) -> Config:
        return replace(self, **kw)
