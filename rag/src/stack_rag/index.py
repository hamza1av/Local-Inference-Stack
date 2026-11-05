"""The RAG index: ingest, delete, list, stats, re-embed, inbox sync."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

from . import db
from .chunking import Chunk, chunk_markdown, chunk_pages
from .config import Config
from .embed import Embedder
from .loaders import Loaded, load, load_markdown, supported

log = logging.getLogger("stack_rag")


@dataclass
class IngestResult:
    path: str
    status: str  # added | updated | unchanged | skipped | error
    doc_id: int | None = None
    chunks: int = 0
    title: str = ""
    error: str = ""


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "untitled"


class Index:
    """One SQLite connection + embedder. Thread-safe via a lock (the MCP server and the
    inbox watcher share one instance)."""

    def __init__(self, cfg: Config | None = None, embedder: Embedder | None = None, check: bool = True):
        self.cfg = cfg or Config.from_env()
        self.con = db.connect(self.cfg.db_path)
        db.init(self.con, self.cfg.embed_model, self.cfg.embed_dim)
        self._embedder = embedder
        self.lock = threading.RLock()
        if check:
            db.check_embedding(self.con, self.cfg.embed_model, self.cfg.embed_dim)

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder(self.cfg)
        return self._embedder

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- chunking -------------------------------------------------------------
    def chunk(self, loaded: Loaded) -> list[Chunk]:
        mt, ov = self.cfg.chunk_tokens, self.cfg.chunk_overlap
        if loaded.kind == "pdf":
            return chunk_pages(loaded.pages, mt, ov)
        chunks = chunk_markdown(loaded.text, mt, ov)
        # "Title > Section" -> "Section" when the top heading is the document title
        t = loaded.title.strip().lower()
        for c in chunks:
            first, sep, rest = c.heading.partition(" > ")
            if first.strip().lower() == t:
                c.heading = rest
        return chunks

    # --- writing --------------------------------------------------------------
    def _store(self, key: str, sha: str, loaded: Loaded, mtime: float | None, size: int | None) -> IngestResult:
        """Embed (outside the write lock) and replace the document's chunks in one transaction."""
        chunks = self.chunk(loaded)
        vectors = self.embedder.embed_docs(loaded.title, [c.text for c in chunks]) if chunks else []
        with self.lock:
            con = self.con
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT id FROM rag_documents WHERE path=?", (key,)).fetchone()
                if row:
                    doc_id = row["id"]
                    self._delete_chunks(doc_id)
                    con.execute(
                        "UPDATE rag_documents SET sha256=?, title=?, mtime=?, size=?, meta=?, "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (sha, loaded.title, mtime, size, json.dumps(loaded.meta), doc_id),
                    )
                    status = "updated"
                else:
                    doc_id = con.execute(
                        "INSERT INTO rag_documents(path, sha256, title, mtime, size, meta) VALUES (?,?,?,?,?,?)",
                        (key, sha, loaded.title, mtime, size, json.dumps(loaded.meta)),
                    ).lastrowid
                    status = "added"
                for ord_, (c, v) in enumerate(zip(chunks, vectors)):
                    cid = con.execute(
                        "INSERT INTO rag_chunks(doc_id, ord, heading, text) VALUES (?,?,?,?)",
                        (doc_id, ord_, c.heading, c.text),
                    ).lastrowid
                    con.execute(
                        "INSERT INTO rag_vec(chunk_id, embedding) VALUES (?, ?)", (cid, sqlite_vec.serialize_float32(v))
                    )
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        return IngestResult(key, status, doc_id, len(chunks), loaded.title)

    def _delete_chunks(self, doc_id: int) -> None:
        ids = [r[0] for r in self.con.execute("SELECT id FROM rag_chunks WHERE doc_id=?", (doc_id,))]
        if ids:
            self.con.executemany("DELETE FROM rag_vec WHERE chunk_id=?", [(i,) for i in ids])
            self.con.execute("DELETE FROM rag_chunks WHERE doc_id=?", (doc_id,))

    def ingest_file(self, path: Path, force: bool = False) -> IngestResult:
        path = path.expanduser().resolve()
        key = str(path)
        if not supported(path):
            return IngestResult(key, "skipped", error="unsupported file type")
        try:
            st = path.stat()
            data = path.read_bytes()
            sha = sha256_bytes(data)
            with self.lock:
                row = self.con.execute("SELECT id, sha256, title FROM rag_documents WHERE path=?", (key,)).fetchone()
                if row and row["sha256"] == sha and not force:
                    # keep mtime/size current so the watcher's fast path skips it next time
                    self.con.execute(
                        "UPDATE rag_documents SET mtime=?, size=? WHERE id=?", (st.st_mtime, st.st_size, row["id"])
                    )
                    return IngestResult(key, "unchanged", row["id"], title=row["title"])
            loaded = load(path)
            return self._store(key, sha, loaded, st.st_mtime, st.st_size)
        except db.EmbeddingMismatch:
            raise
        except Exception as e:  # one bad file must not stop a batch
            log.warning("ingest %s failed: %s", key, e)
            return IngestResult(key, "error", error=f"{type(e).__name__}: {e}")

    def ingest_paths(self, paths: list[Path], force: bool = False) -> list[IngestResult]:
        results = []
        for p in paths:
            p = p.expanduser()
            if p.is_dir():
                files = sorted(f for f in p.rglob("*") if f.is_file() and supported(f) and not _hidden(f, p))
                results.extend(self.ingest_file(f, force) for f in files)
            elif p.exists():
                results.append(self.ingest_file(p, force))
            else:
                results.append(IngestResult(str(p), "error", error="not found"))
        return results

    def add_text(self, title: str, text: str, key: str | None = None) -> IngestResult:
        """Store a text snippet (markdown allowed) as its own document, key `text:<slug>`."""
        key = key or f"text:{slug(title)}"
        sha = sha256_bytes(text.encode())
        with self.lock:
            row = self.con.execute("SELECT id, sha256 FROM rag_documents WHERE path=?", (key,)).fetchone()
        if row and row["sha256"] == sha:
            return IngestResult(key, "unchanged", row["id"], title=title)
        loaded = load_markdown(text, fallback_title=title)
        loaded.title = title
        loaded.meta["source"] = "text"
        return self._store(key, sha, loaded, time.time(), len(text))

    def remove(self, ref: str | int) -> list[str]:
        """Remove by id, exact path/key, or every document under a directory path."""
        with self.lock:
            rows = self._resolve(ref)
            if not rows:
                return []
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for r in rows:
                    self._delete_chunks(r["id"])
                    self.con.execute("DELETE FROM rag_documents WHERE id=?", (r["id"],))
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
            return [r["path"] for r in rows]

    def _resolve(self, ref: str | int) -> list[sqlite3.Row]:
        q = "SELECT id, path FROM rag_documents"
        if isinstance(ref, int) or str(ref).isdigit():
            return self.con.execute(q + " WHERE id=?", (int(ref),)).fetchall()
        rows = self.con.execute(q + " WHERE path=?", (ref,)).fetchall()
        if rows:
            return rows
        p = Path(ref).expanduser()
        if p.is_absolute() or p.exists():
            p = p.resolve()
            rows = self.con.execute(q + " WHERE path=?", (str(p),)).fetchall()
            if rows:
                return rows
            prefix = str(p).rstrip("/") + "/"
            return [r for r in self.con.execute(q).fetchall() if r["path"].startswith(prefix)]
        return []

    # --- inbox ----------------------------------------------------------------
    def sync_dir(self, root: Path, settle: float = 2.0) -> dict[str, list[str]]:
        """Bring the index in line with a folder: add/update changed files (by hash, with an
        mtime+size fast path), remove documents whose files disappeared. Files modified in
        the last `settle` seconds are left for the next round (still being written)."""
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        prefix = str(root) + "/"
        with self.lock:
            known = {
                r["path"]: r
                for r in self.con.execute("SELECT id, path, mtime, size FROM rag_documents").fetchall()
                if r["path"].startswith(prefix)
            }
        seen: set[str] = set()
        report: dict[str, list[str]] = {"added": [], "updated": [], "removed": [], "error": []}
        now = time.time()
        for f in sorted(root.rglob("*")):
            if not f.is_file() or not supported(f) or _hidden(f, root):
                continue
            key = str(f.resolve())
            seen.add(key)
            st = f.stat()
            k = known.get(key)
            if k and k["mtime"] == st.st_mtime and k["size"] == st.st_size:
                continue
            if now - st.st_mtime < settle:
                continue
            res = self.ingest_file(f)
            if res.status in report:
                report[res.status].append(key)
        gone = [p for p in known if p not in seen]
        for p in gone:
            report["removed"].extend(self.remove(p))
        return report

    # --- reading --------------------------------------------------------------
    def list_documents(self) -> list[dict]:
        with self.lock:
            rows = self.con.execute(
                "SELECT d.id, d.path, d.title, d.updated_at, d.size, COUNT(c.id) AS chunks FROM rag_documents d "
                "LEFT JOIN rag_chunks c ON c.doc_id = d.id GROUP BY d.id ORDER BY d.title COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self.lock:
            return self._stats()

    def _stats(self) -> dict:
        one = lambda q: self.con.execute(q).fetchone()[0]  # noqa: E731
        meta = db.get_meta(self.con)
        size = sum(
            p.stat().st_size for p in (self.cfg.db_path, Path(str(self.cfg.db_path) + "-wal")) if p.exists()
        )
        return {
            "db": str(self.cfg.db_path),
            "documents": one("SELECT COUNT(*) FROM rag_documents"),
            "chunks": one("SELECT COUNT(*) FROM rag_chunks"),
            "vectors": one("SELECT COUNT(*) FROM rag_vec"),
            "embed_model": meta.get("embed_model"),
            "embed_dim": int(meta.get("embed_dim", 0) or 0),
            "configured_model": self.cfg.embed_model,
            "configured_dim": self.cfg.embed_dim,
            "db_bytes": size,
            "inbox": str(self.cfg.inbox),
        }

    # --- maintenance ----------------------------------------------------------
    def reembed(self, progress=None) -> int:
        """Rebuild all vectors with the configured model/dim (drops and recreates rag_vec)."""
        with self.lock:
            rows = self.con.execute(
                "SELECT c.id, c.text, d.title FROM rag_chunks c JOIN rag_documents d ON d.id = c.doc_id ORDER BY c.id"
            ).fetchall()
        vecs: list[list[float]] = []
        step = 64
        for i in range(0, len(rows), step):
            part = rows[i : i + step]
            vecs.extend(self.embedder.embed_pairs([(r["title"], r["text"]) for r in part]))
            if progress:
                progress(min(i + step, len(rows)), len(rows))
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                self.con.execute("DROP TABLE IF EXISTS rag_vec")
                db.create_vec(self.con, self.cfg.embed_dim)
                self.con.executemany(
                    "INSERT INTO rag_vec(chunk_id, embedding) VALUES (?, ?)",
                    [(r["id"], sqlite_vec.serialize_float32(v)) for r, v in zip(rows, vecs)],
                )
                db.set_meta(self.con, self.cfg.embed_model, self.cfg.embed_dim)
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
        return len(rows)


def _hidden(f: Path, root: Path) -> bool:
    try:
        parts = f.relative_to(root).parts
    except ValueError:
        parts = f.parts
    return any(p.startswith((".", "~")) for p in parts) or f.name.endswith((".tmp", ".part", ".crdownload"))
