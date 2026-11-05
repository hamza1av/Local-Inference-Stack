"""`ask` end to end with a small chat model on Ollama (RAG_TEST_CHAT_MODEL, default lfm3:1.7b)."""

import os

import httpx
import pytest
from conftest import needs_embed

from stack_rag.ask import ask, format_hits

pytestmark = needs_embed

MODEL = os.environ.get("RAG_TEST_CHAT_MODEL", "lfm3:1.7b")


def _chat_available(base: str) -> bool:
    try:
        tags = httpx.get(f"{base}/api/tags", timeout=5).json()
        return any(m["name"] == MODEL for m in tags.get("models", []))
    except Exception:
        return False


def test_format_hits_numbers_and_sources(demo_index):
    from stack_rag.retrieval import hybrid

    text = format_hits(hybrid(demo_index, "guest password", 2))
    assert text.startswith("[1] Home Network Handbook") and "\n\n[2] " in text


def test_ask_cites_sources(demo_index):
    base = demo_index.cfg.ollama_url
    if not _chat_available(base):
        pytest.skip(f"chat model {MODEL} not on {base}")
    idx = demo_index
    old = idx.cfg
    idx.cfg = old.with_(llm_base_url=f"{base}/v1", llm_model=MODEL, llm_api_key="")
    try:
        res = ask(idx, "How often is the guest Wi-Fi password rotated?", k=3)
    finally:
        idx.cfg = old
    assert res["hits"] and res["hits"][0]["title"] == "Home Network Handbook"
    assert "three months" in res["answer"].lower() or "3 months" in res["answer"].lower()
    assert "[" in res["answer"]
