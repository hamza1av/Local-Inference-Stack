"""Tests use a temp SQLite DB and the real EmbeddingGemma on Ollama ($OLLAMA_URL).

Tests that need embeddings are skipped when Ollama or the model is not reachable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from stack_rag.config import Config

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "docs"


def _base_cfg(tmp: Path, **kw) -> Config:
    cfg = Config.from_env()  # endpoints/model from env/.env
    return cfg.with_(db_path=tmp / "stack.db", data_dir=tmp, inbox=tmp / "inbox", embed_via_gateway=False, **kw)


def _ollama_ok() -> str | None:
    cfg = Config.from_env()
    try:
        r = httpx.post(
            f"{cfg.ollama_url}/v1/embeddings", json={"model": cfg.embed_model, "input": ["ping"]}, timeout=30
        )
        r.raise_for_status()
        return None
    except Exception as e:  # pragma: no cover
        return f"embeddings unavailable at {cfg.ollama_url}: {e}"


_SKIP = _ollama_ok()
needs_embed = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")


@pytest.fixture
def cfg(tmp_path) -> Config:
    return _base_cfg(tmp_path)


@pytest.fixture
def docs(tmp_path) -> Path:
    """A private copy of the example docs (tests modify files)."""
    d = tmp_path / "docs"
    shutil.copytree(EXAMPLES, d)
    return d


@pytest.fixture
def index(cfg):
    from stack_rag.index import Index

    idx = Index(cfg)
    yield idx
    idx.close()


@pytest.fixture(scope="session")
def demo_index(tmp_path_factory):
    """Example docs ingested once per session (read-only tests)."""
    if _SKIP:
        pytest.skip(_SKIP)
    from stack_rag.index import Index

    tmp = tmp_path_factory.mktemp("demo")
    idx = Index(_base_cfg(tmp))
    res = idx.ingest_paths([EXAMPLES])
    assert all(r.status == "added" for r in res), res
    yield idx
    idx.close()


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
