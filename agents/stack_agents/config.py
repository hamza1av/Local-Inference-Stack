"""Settings, read once from the environment.

The stack keeps one `.env` at the repo root (see docs/ARCHITECTURE.md). We load it with
python-dotenv *without* overriding variables that are already set, so
`AGENTS_LLM_BASE_URL=http://127.0.0.1:11434/v1 stack-agents chat ...` wins over `.env`.

LLM endpoint overrides (handy for testing against Ollama instead of the gateway):

    AGENTS_LLM_BASE_URL   default $GATEWAY_URL (http://127.0.0.1:8088/v1)
    AGENTS_LLM_API_KEY    default $STACK_API_KEY
    AGENTS_CHAT_MODEL     default "chat"   (answers, tool loops)
    AGENTS_TASK_MODEL     default "task"   (decision fallback, small/fast)
    AGENTS_DECIDE_URL     default $AGENTS_LLM_BASE_URL/decisions
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROUTES = ("chat", "knowledge", "documents", "data", "memory")


def _repo_root() -> Path:
    if root := os.environ.get("STACK_ROOT"):
        return Path(root)
    return Path(__file__).resolve().parents[2]  # agents/stack_agents/config.py -> repo


def load_env() -> None:
    env = _repo_root() / ".env"
    if env.is_file():
        load_dotenv(env, override=False)


def _get(name: str, default: str = "") -> str:
    val = os.environ.get(name, "")
    return val if val != "" else default


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    chat_model: str
    task_model: str
    decide_url: str
    decide_timeout: float
    stack_data: Path
    stack_db: Path
    checkpoint_db: Path
    port: int
    kg_mcp_port: int
    rag_mcp_port: int
    mcp_config: Path
    route_min_confidence: float
    max_tool_steps: int
    max_history: int
    sql_row_cap: int
    stream_steps: str  # "reasoning" | "prefix" | "off"
    otlp_endpoint: str
    langfuse_secret: str
    tracing: bool

    @property
    def recorder_base(self) -> str:
        """Langfuse SDK wants the *host*; it appends /api/public/otel/v1/traces itself."""
        ep = self.otlp_endpoint.rstrip("/")
        for suffix in ("/api/public/otel/v1/traces", "/api/public/otel"):
            if ep.endswith(suffix):
                return ep[: -len(suffix)]
        return ep


@lru_cache(maxsize=1)
def settings() -> Settings:
    load_env()
    data = Path(os.path.expanduser(_get("STACK_DATA", "~/.local/share/inference-stack")))
    base = _get("AGENTS_LLM_BASE_URL", _get("GATEWAY_URL", "http://127.0.0.1:8088/v1")).rstrip("/")
    here = Path(__file__).resolve().parents[1]
    return Settings(
        llm_base_url=base,
        llm_api_key=_get("AGENTS_LLM_API_KEY", _get("STACK_API_KEY", "none")),
        chat_model=_get("AGENTS_CHAT_MODEL", "chat"),
        task_model=_get("AGENTS_TASK_MODEL", "task"),
        decide_url=_get("AGENTS_DECIDE_URL", base + "/decisions"),
        decide_timeout=float(_get("AGENTS_DECIDE_TIMEOUT", "20")),
        stack_data=data,
        stack_db=Path(_get("STACK_DB", str(data / "stack.db"))),
        checkpoint_db=Path(_get("AGENTS_CHECKPOINT_DB", str(data / "agents.db"))),
        port=int(_get("AGENTS_PORT", "8092")),
        kg_mcp_port=int(_get("KG_MCP_PORT", "8090")),
        rag_mcp_port=int(_get("RAG_MCP_PORT", "8091")),
        mcp_config=Path(_get("AGENTS_MCP_CONFIG", str(here / "mcp_servers.toml"))),
        route_min_confidence=float(_get("AGENTS_ROUTE_MIN_CONFIDENCE", "0.5")),
        max_tool_steps=int(_get("AGENTS_MAX_TOOL_STEPS", "4")),
        max_history=int(_get("AGENTS_MAX_HISTORY", "12")),
        sql_row_cap=int(_get("AGENTS_SQL_ROW_CAP", "50")),
        stream_steps=_get("AGENTS_STREAM_STEPS", "reasoning"),
        otlp_endpoint=_get("STACK_OTLP_ENDPOINT", "http://127.0.0.1:4320/api/public/otel"),
        langfuse_secret=_get("LANGFUSE_SECRET_KEY", ""),
        tracing=_get("AGENTS_TRACING", "1") not in ("0", "false", "off", "no"),
    )
