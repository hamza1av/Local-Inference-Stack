//! `gw_requests` logging. Rows go through a bounded channel to one writer thread;
//! a full channel or a broken database only drops rows, never affects requests.

use std::{
    path::{Path, PathBuf},
    sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError},
    time::Duration,
};

use rusqlite::{params, Connection};

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Row {
    pub id: String,
    pub ts: String,
    pub client: String,
    pub model: String,
    pub backend: String,
    pub endpoint: String,
    pub stream: bool,
    pub status: u16,
    pub queue_ms: Option<i64>,
    pub latency_ms: i64,
    pub ttft_ms: Option<i64>,
    pub prompt_tokens: Option<i64>,
    pub completion_tokens: Option<i64>,
    pub error: Option<String>,
    pub trace_id: String,
}

pub const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS gw_requests (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  client TEXT,
  model TEXT,
  backend TEXT,
  endpoint TEXT,
  stream INTEGER,
  status INTEGER,
  queue_ms INTEGER,
  latency_ms INTEGER,
  ttft_ms INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  error TEXT,
  trace_id TEXT
);
CREATE INDEX IF NOT EXISTS gw_requests_ts ON gw_requests(ts);
";

#[derive(Clone)]
pub struct DbWriter {
    tx: Option<SyncSender<Row>>,
}

impl DbWriter {
    pub fn disabled() -> DbWriter {
        DbWriter { tx: None }
    }

    /// Open (or create) the database and start the writer thread. Fails only if the
    /// database cannot be opened at startup; the caller may continue without it.
    pub fn start(path: &Path) -> Result<DbWriter, String> {
        let conn = open(path).map_err(|e| format!("{}: {e}", path.display()))?;
        let (tx, rx) = sync_channel::<Row>(4096);
        let path = path.to_path_buf();
        std::thread::Builder::new()
            .name("gw-sqlite".into())
            .stack_size(256 * 1024)
            .spawn(move || writer(conn, path, rx))
            .map_err(|e| e.to_string())?;
        Ok(DbWriter { tx: Some(tx) })
    }

    pub fn log(&self, row: Row) {
        if let Some(tx) = &self.tx {
            match tx.try_send(row) {
                Ok(()) => {}
                Err(TrySendError::Full(_)) => tracing::warn!("sqlite queue full, request row dropped"),
                Err(TrySendError::Disconnected(_)) => {}
            }
        }
    }
}

pub fn open(path: &Path) -> rusqlite::Result<Connection> {
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let conn = Connection::open(path)?;
    conn.busy_timeout(Duration::from_millis(5000))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.execute_batch(SCHEMA)?;
    Ok(conn)
}

fn insert(conn: &mut Connection, rows: &[Row]) -> rusqlite::Result<()> {
    let tx = conn.transaction()?;
    {
        let mut st = tx.prepare_cached(
            "INSERT OR REPLACE INTO gw_requests (id, ts, client, model, backend, endpoint, stream, status,
               queue_ms, latency_ms, ttft_ms, prompt_tokens, completion_tokens, error, trace_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
        )?;
        for r in rows {
            st.execute(params![
                r.id,
                r.ts,
                r.client,
                r.model,
                r.backend,
                r.endpoint,
                r.stream as i64,
                r.status as i64,
                r.queue_ms,
                r.latency_ms,
                r.ttft_ms,
                r.prompt_tokens,
                r.completion_tokens,
                r.error,
                r.trace_id
            ])?;
        }
    }
    tx.commit()
}

fn writer(mut conn: Connection, path: PathBuf, rx: Receiver<Row>) {
    let mut batch = Vec::with_capacity(64);
    while let Ok(first) = rx.recv() {
        batch.push(first);
        while batch.len() < 256 {
            match rx.try_recv() {
                Ok(r) => batch.push(r),
                Err(_) => break,
            }
        }
        if let Err(e) = insert(&mut conn, &batch) {
            tracing::warn!("sqlite write failed ({} rows dropped): {e}", batch.len());
            // The file may have been removed or locked for too long; try to reopen once.
            if let Ok(c) = open(&path) {
                conn = c;
            }
        }
        batch.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_rows_in_background() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.db");
        let w = DbWriter::start(&path).unwrap();
        for i in 0..3 {
            w.log(Row { id: format!("r{i}"), ts: "2026-01-01T00:00:00.000Z".into(), status: 200, ..Default::default() });
        }
        let conn = open(&path).unwrap();
        let mut n = 0;
        for _ in 0..100 {
            n = conn.query_row("SELECT count(*) FROM gw_requests", [], |r| r.get::<_, i64>(0)).unwrap();
            if n == 3 {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(n, 3);
        let mode: String = conn.query_row("PRAGMA journal_mode", [], |r| r.get(0)).unwrap();
        assert_eq!(mode, "wal");
    }
}
