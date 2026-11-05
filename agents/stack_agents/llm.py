"""Model factories. Everything talks OpenAI-compatible HTTP to the gateway (or Ollama in tests)."""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI, OpenAI

from .config import settings


def chat_model(*, model: str | None = None, temperature: float = 0.3, background: bool = False) -> ChatOpenAI:
    """The conversational model (`chat` on the gateway). Used for answers and tool loops."""
    s = settings()
    return ChatOpenAI(
        model=model or s.chat_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=temperature,
        timeout=180,
        max_retries=1,
        default_headers={"x-stack-priority": "background"} if background else None,
    )


def task_model(*, background: bool = False) -> ChatOpenAI:
    """The small `task` model: classification fallback, short structured outputs."""
    s = settings()
    return chat_model(model=s.task_model, temperature=0.0, background=background)


def openai_client() -> OpenAI:
    s = settings()
    return OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=180, max_retries=1)


def async_openai_client() -> AsyncOpenAI:
    s = settings()
    return AsyncOpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=180, max_retries=1)
