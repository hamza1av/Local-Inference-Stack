"""Tests run against the real FalkorDB from kg/compose.yml (FALKORDB_PORT, default 6390) and
the real embedding model in Ollama, each test module on its own temporary graph."""

from __future__ import annotations

import shutil
import uuid

import httpx
import pytest

from stack_kg.config import EXAMPLES, Settings
from stack_kg.db import KG


def _settings(tmp_path) -> Settings:
    vault = tmp_path / "vault"
    shutil.copytree(EXAMPLES, vault)
    return Settings.from_env(vault=vault, db_path=tmp_path / "stack.db", graph=f"kg_test_{uuid.uuid4().hex[:8]}",
                             repos=[], ingest_interval=0)


@pytest.fixture(scope="session", autouse=True)
def services():
    s = Settings.from_env()
    kg = KG(s)
    if not kg.ping():
        pytest.exit(f"FalkorDB not reachable on {s.falkor_host}:{s.falkor_port} (just kg::up)", 2)
    try:
        httpx.get(f"{s.ollama_url}/api/tags", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.exit(f"Ollama not reachable at {s.ollama_url} (embeddings are part of the tests)", 2)


@pytest.fixture
def kg(tmp_path):
    k = KG(_settings(tmp_path))
    yield k
    k.drop_graph()


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    """One ingested sample vault shared by read-only tests."""
    from stack_kg import ingest

    k = KG(_settings(tmp_path_factory.mktemp("kg")))
    k.stats = ingest.run(k, log=lambda *_: None)
    yield k
    k.drop_graph()
