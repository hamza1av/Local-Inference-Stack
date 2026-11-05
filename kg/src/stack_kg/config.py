"""Settings from the environment (the repo-wide `.env`, see docs/ARCHITECTURE.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
KG_DIR = PKG_DIR.parent.parent  # .../kg
DEFAULT_TAXONOMY = KG_DIR / "taxonomy.yaml"
EXAMPLES = KG_DIR / "examples" / "vault"

_dotenv_loaded = False


def load_dotenv() -> None:
    """Load `$STACK_ROOT/.env` (or the repo root's) without overriding the process env."""
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    root = Path(os.environ.get("STACK_ROOT", KG_DIR.parent))
    env = root / ".env"
    if env.is_file():
        from dotenv import load_dotenv as _load

        _load(env, override=False)


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v or default


@dataclass
class Settings:
    data: Path
    db_path: Path
    vault: Path
    graph: str
    repos: list[Path]
    falkor_host: str
    falkor_port: int
    ollama_url: str
    embed_model: str
    embed_dim: int
    mcp_host: str
    mcp_port: int
    ingest_interval: int
    taxonomy: Path
    remember_note: str = "Remembered.md"
    repo_commits: int = 30
    max_distance: float = 0.8  # cosine distance cut for vector hits (unrelated text is ~0.85+)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        load_dotenv()
        home = Path.home()
        data = Path(_env("STACK_DATA", str(home / ".local/share/inference-stack"))).expanduser()
        repos = [Path(p).expanduser() for p in _env("KG_REPOS", "").split(":") if p.strip()]
        s = cls(
            data=data,
            db_path=Path(_env("STACK_DB", str(data / "stack.db"))).expanduser(),
            vault=Path(_env("KG_VAULT", str(data / "kg" / "vault"))).expanduser(),
            graph=_env("KG_GRAPH", "kg"),
            repos=repos,
            falkor_host=_env("FALKORDB_HOST", "127.0.0.1"),
            falkor_port=int(_env("FALKORDB_PORT", "6390")),
            ollama_url=_env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            embed_model=_env("STACK_EMBED_MODEL", "embeddinggemma"),
            embed_dim=int(_env("STACK_EMBED_DIM", "768")),
            mcp_host=_env("KG_MCP_HOST", "127.0.0.1"),
            mcp_port=int(_env("KG_MCP_PORT", "8090")),
            ingest_interval=int(_env("KG_INGEST_INTERVAL", "600")),
            taxonomy=Path(_env("KG_TAXONOMY", str(DEFAULT_TAXONOMY))).expanduser(),
            remember_note=_env("KG_REMEMBER_NOTE", "Remembered.md"),
            repo_commits=int(_env("KG_REPO_COMMITS", "30")),
            max_distance=float(_env("KG_MAX_DISTANCE", "0.8")),
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        return s
