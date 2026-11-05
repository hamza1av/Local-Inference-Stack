"""FalkorDB graph + SQLite ingest state, bundled in one handle (`KG`)."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from functools import cached_property

from falkordb import FalkorDB

from .config import Settings
from .taxonomy import Taxonomy

# (label, property) range indexes for MERGE/lookup speed
RANGE_INDEXES = [
    ("Note", "id"),
    ("Note", "title_lc"),
    ("Tag", "name"),
    ("Ref", "id"),
    ("Fact", "id"),
    ("Repo", "id"),
    ("Commit", "sha"),
]
FULLTEXT = {
    "Note": ("title", "aliases_text", "headings", "body"),
    "Fact": ("text", "why"),
    "Commit": ("subject", "body"),
}
VECTOR_LABELS = ("Note", "Fact")

SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_files (
    graph   TEXT NOT NULL,
    kind    TEXT NOT NULL,          -- note | repo
    id      TEXT NOT NULL,          -- vault-relative path / repo path
    hash    TEXT NOT NULL,
    updated TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (graph, kind, id)
);
CREATE TABLE IF NOT EXISTS kg_runs (
    graph    TEXT NOT NULL,
    started  TEXT NOT NULL,
    seconds  REAL,
    stats    TEXT,
    error    TEXT
);
"""


class KG:
    """Everything a command needs: settings, taxonomy, graph and state DB."""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or Settings.from_env()
        self.lock = threading.RLock()  # one ingest/remember at a time per process
        self._tax: Taxonomy | None = None

    # ---- taxonomy ---------------------------------------------------------
    @property
    def tax(self) -> Taxonomy:
        if self._tax is None:
            self._tax = Taxonomy(self.s.taxonomy)
        return self._tax

    def reload_taxonomy(self) -> Taxonomy:
        self._tax = None
        return self.tax

    # ---- graph ------------------------------------------------------------
    @cached_property
    def client(self) -> FalkorDB:
        return FalkorDB(host=self.s.falkor_host, port=self.s.falkor_port,
                        socket_timeout=30, socket_connect_timeout=5)

    @cached_property
    def graph(self):
        return self.client.select_graph(self.s.graph)

    def rows(self, query: str, params: dict | None = None, readonly: bool = True) -> list[dict]:
        g = self.graph
        res = g.ro_query(query, params or {}) if readonly else g.query(query, params or {})
        cols = [c[1] if isinstance(c, (list, tuple)) else c for c in res.header]
        return [dict(zip(cols, r)) for r in res.result_set]

    def write(self, query: str, params: dict | None = None):
        return self.graph.query(query, params or {})

    def ping(self) -> bool:
        try:
            return bool(self.client.connection.ping())
        except Exception:
            return False

    def graph_exists(self) -> bool:
        return self.s.graph in self.client.list_graphs()

    def indexes(self) -> set[tuple[str, str, str]]:
        out = set()
        if not self.graph_exists():
            return out
        for r in self.rows("CALL db.indexes() YIELD label, properties, types RETURN label, properties, types"):
            for prop in r["properties"]:
                for t in (r["types"] or {}).get(prop, []):
                    out.add((r["label"], prop, str(t).upper()))
        return out

    def ensure_schema(self) -> None:
        have = self.indexes()
        for label, prop in RANGE_INDEXES:
            if (label, prop, "RANGE") not in have:
                self.write(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        for label, props in FULLTEXT.items():
            missing = [p for p in props if (label, p, "FULLTEXT") not in have]
            if missing:
                self.write(f"CREATE FULLTEXT INDEX FOR (n:{label}) ON ({', '.join('n.' + p for p in missing)})")
        for label in VECTOR_LABELS:
            if (label, "embedding", "VECTOR") not in have:
                self.write(f"CREATE VECTOR INDEX FOR (n:{label}) ON (n.embedding) "
                           f"OPTIONS {{dimension: {int(self.s.embed_dim)}, similarityFunction: 'cosine'}}")

    def sync_taxonomy(self) -> None:
        """Upsert every vocabulary tag so facets/aliases are queryable even before use."""
        tags = [{"name": t.name, "facet": t.facet, "aliases": t.aliases} for t in self.tax.tags.values()]
        self.write("""UNWIND $tags AS t MERGE (g:Tag {name: t.name})
                      SET g.facet = t.facet, g.aliases = t.aliases, g.vocab = true""", {"tags": tags})

    def drop_graph(self) -> None:
        if self.graph_exists():
            self.graph.delete()
        self.__dict__.pop("graph", None)

    # ---- SQLite state -----------------------------------------------------
    def sql(self) -> sqlite3.Connection:
        self.s.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.s.db_path, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.executescript(SQL_SCHEMA)
        return con

    def state(self, kind: str) -> dict[str, str]:
        with closing(self.sql()) as con:
            return dict(con.execute("SELECT id, hash FROM kg_files WHERE graph = ? AND kind = ?",
                                    (self.s.graph, kind)))

    def save_state(self, kind: str, items: dict[str, str], removed: list[str] = ()) -> None:
        con = self.sql()
        with con:
            con.executemany("""INSERT INTO kg_files (graph, kind, id, hash) VALUES (?, ?, ?, ?)
                ON CONFLICT (graph, kind, id) DO UPDATE SET hash = excluded.hash, updated = datetime('now')""",
                            [(self.s.graph, kind, k, h) for k, h in items.items()])
            con.executemany("DELETE FROM kg_files WHERE graph = ? AND kind = ? AND id = ?",
                            [(self.s.graph, kind, k) for k in removed])
        con.close()

    def clear_state(self) -> None:
        con = self.sql()
        with con:
            con.execute("DELETE FROM kg_files WHERE graph = ?", (self.s.graph,))
        con.close()

    def log_run(self, started: str, seconds: float, stats: str, error: str | None = None) -> None:
        con = self.sql()
        with con:
            con.execute("INSERT INTO kg_runs (graph, started, seconds, stats, error) VALUES (?, ?, ?, ?, ?)",
                        (self.s.graph, started, seconds, stats, error))
            # keep the log small
            con.execute("""DELETE FROM kg_runs WHERE graph = ? AND rowid NOT IN
                (SELECT rowid FROM kg_runs WHERE graph = ? ORDER BY started DESC LIMIT 200)""",
                        (self.s.graph, self.s.graph))
        con.close()

    def last_run(self) -> dict | None:
        with closing(self.sql()) as con:
            r = con.execute("SELECT started, seconds, stats, error FROM kg_runs WHERE graph = ? "
                            "ORDER BY started DESC LIMIT 1", (self.s.graph,)).fetchone()
        return dict(zip(("started", "seconds", "stats", "error"), r)) if r else None
