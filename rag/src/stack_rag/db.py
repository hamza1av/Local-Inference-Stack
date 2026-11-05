"""SQLite schema (tables prefixed `rag_`) with FTS5 and sqlite-vec.

    rag_meta        key/value: embed_model, embed_dim (the vectors in rag_vec were made with these)
    rag_documents   one row per source (file path or `text:<slug>` for rag_add_text)
    rag_chunks      ordered chunks of a document (heading = markdown heading path / page)
    rag_fts         FTS5 index over rag_chunks (external content, kept in sync by triggers)
    rag_vec         vec0 virtual table, chunk_id -> float[dim], cosine distance
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_documents (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,
    sha256    TEXT NOT NULL,
    title     TEXT NOT NULL,
    mtime     REAL,
    size      INTEGER,
    meta      TEXT NOT NULL DEFAULT '{}',
    added_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS rag_chunks (
    id      INTEGER PRIMARY KEY,
    doc_id  INTEGER NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rag_chunks_doc ON rag_chunks(doc_id, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
    heading, text, content='rag_chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS rag_chunks_ai AFTER INSERT ON rag_chunks BEGIN
    INSERT INTO rag_fts(rowid, heading, text) VALUES (new.id, new.heading, new.text);
END;
CREATE TRIGGER IF NOT EXISTS rag_chunks_ad AFTER DELETE ON rag_chunks BEGIN
    INSERT INTO rag_fts(rag_fts, rowid, heading, text) VALUES ('delete', old.id, old.heading, old.text);
END;
CREATE TRIGGER IF NOT EXISTS rag_chunks_au AFTER UPDATE ON rag_chunks BEGIN
    INSERT INTO rag_fts(rag_fts, rowid, heading, text) VALUES ('delete', old.id, old.heading, old.text);
    INSERT INTO rag_fts(rowid, heading, text) VALUES (new.id, new.heading, new.text);
END;
"""


class EmbeddingMismatch(RuntimeError):
    pass


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init(con: sqlite3.Connection, model: str, dim: int) -> None:
    """Create tables idempotently. A fresh DB records (model, dim); an existing one must match."""
    con.executescript(SCHEMA)
    meta = get_meta(con)
    if "embed_model" not in meta:
        create_vec(con, dim)
        set_meta(con, model, dim)
    elif not vec_exists(con):
        create_vec(con, int(meta["embed_dim"]))


def get_meta(con: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM rag_meta")}


def set_meta(con: sqlite3.Connection, model: str, dim: int) -> None:
    con.executemany(
        "INSERT INTO rag_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [("embed_model", model), ("embed_dim", str(dim))],
    )


def vec_exists(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name='rag_vec'").fetchone() is not None


def create_vec(con: sqlite3.Connection, dim: int) -> None:
    con.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS rag_vec USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding float[{int(dim)}] distance_metric=cosine)"
    )


def check_embedding(con: sqlite3.Connection, model: str, dim: int) -> None:
    meta = get_meta(con)
    have = (meta.get("embed_model"), int(meta.get("embed_dim", 0) or 0))
    if have != (model, dim):
        raise EmbeddingMismatch(
            f"the index was embedded with {have[0]} / {have[1]} dims but the config says "
            f"{model} / {dim} dims; run `stack-rag reembed` to rebuild the vectors"
        )
