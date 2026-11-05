//! Minimal OTLP/HTTP JSON span exporter (no opentelemetry crates).
//!
//! Spans are queued in a bounded channel (dropped when full) and POSTed in batches to
//! `<endpoint>/v1/traces` by a background task. Export failures are logged, never surfaced.

use std::{
    sync::atomic::{AtomicU64, Ordering},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde_json::{json, Value};
use tokio::sync::mpsc;

pub const SERVICE_NAME: &str = "stack-gateway";

/// W3C trace context of the incoming request (if any) and of our span.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TraceCtx {
    pub trace_id: String,
    pub span_id: String,
    pub parent_span_id: Option<String>,
    pub sampled: bool,
}

fn is_hex(s: &str, len: usize) -> bool {
    s.len() == len && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn random_hex(bytes: usize) -> String {
    let mut s = String::with_capacity(bytes * 2);
    while s.len() < bytes * 2 {
        let v = fastrand::u64(1..);
        s.push_str(&format!("{v:016x}"));
    }
    s.truncate(bytes * 2);
    s
}

impl TraceCtx {
    /// Child of `traceparent` when it is valid, otherwise a new root.
    pub fn from_traceparent(header: Option<&str>) -> TraceCtx {
        if let Some(h) = header {
            let parts: Vec<&str> = h.trim().split('-').collect();
            if parts.len() >= 4
                && is_hex(parts[0], 2)
                && parts[0] != "ff"
                && is_hex(parts[1], 32)
                && is_hex(parts[2], 16)
                && is_hex(parts[3], 2)
                && parts[1].bytes().any(|b| b != b'0')
                && parts[2].bytes().any(|b| b != b'0')
            {
                let flags = u8::from_str_radix(parts[3], 16).unwrap_or(1);
                return TraceCtx {
                    trace_id: parts[1].to_string(),
                    span_id: random_hex(8),
                    parent_span_id: Some(parts[2].to_string()),
                    sampled: flags & 1 == 1,
                };
            }
        }
        TraceCtx { trace_id: random_hex(16), span_id: random_hex(8), parent_span_id: None, sampled: true }
    }

    /// traceparent to send downstream: our span is the parent.
    pub fn traceparent(&self) -> String {
        format!("00-{}-{}-{}", self.trace_id, self.span_id, if self.sampled { "01" } else { "00" })
    }
}

pub fn unix_nanos(t: SystemTime) -> u128 {
    t.duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0)
}

/// RFC 3339 UTC with milliseconds, e.g. `2026-11-05T12:34:56.789Z`.
pub fn rfc3339(t: SystemTime) -> String {
    let d = t.duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = d.as_secs() as i64;
    let (days, sod) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    // civil_from_days (H. Hinnant)
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + if month <= 2 { 1 } else { 0 };
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        sod / 3600,
        (sod / 60) % 60,
        sod % 60,
        d.subsec_millis()
    )
}

#[derive(Debug, Clone)]
pub enum AttrValue {
    Str(String),
    Int(i64),
    Bool(bool),
}

#[derive(Debug, Clone)]
pub struct Span {
    pub ctx: TraceCtx,
    pub name: String,
    pub start: SystemTime,
    pub end: SystemTime,
    pub error: Option<String>,
    pub attrs: Vec<(&'static str, AttrValue)>,
}

impl Span {
    pub fn to_json(&self) -> Value {
        let attrs: Vec<Value> = self
            .attrs
            .iter()
            .map(|(k, v)| {
                let v = match v {
                    AttrValue::Str(s) => json!({ "stringValue": s }),
                    AttrValue::Int(i) => json!({ "intValue": i.to_string() }),
                    AttrValue::Bool(b) => json!({ "boolValue": b }),
                };
                json!({ "key": k, "value": v })
            })
            .collect();
        let mut span = json!({
            "traceId": self.ctx.trace_id,
            "spanId": self.ctx.span_id,
            "name": self.name,
            "kind": 2,
            "startTimeUnixNano": unix_nanos(self.start).to_string(),
            "endTimeUnixNano": unix_nanos(self.end).to_string(),
            "attributes": attrs,
            "status": match &self.error {
                Some(m) => json!({ "code": 2, "message": m }),
                None => json!({ "code": 1 }),
            },
        });
        if let Some(p) = &self.ctx.parent_span_id {
            span["parentSpanId"] = json!(p);
        }
        span
    }
}

pub fn batch_json(spans: &[Span]) -> Value {
    json!({
        "resourceSpans": [{
            "resource": { "attributes": [
                { "key": "service.name", "value": { "stringValue": SERVICE_NAME } },
                { "key": "service.version", "value": { "stringValue": env!("CARGO_PKG_VERSION") } },
            ]},
            "scopeSpans": [{
                "scope": { "name": SERVICE_NAME, "version": env!("CARGO_PKG_VERSION") },
                "spans": spans.iter().map(Span::to_json).collect::<Vec<_>>(),
            }],
        }]
    })
}

#[derive(Clone)]
pub struct Exporter {
    tx: Option<mpsc::Sender<Span>>,
    pub dropped: Arc<AtomicU64>,
}

impl Exporter {
    pub fn disabled() -> Exporter {
        Exporter { tx: None, dropped: Arc::new(AtomicU64::new(0)) }
    }

    /// Spawn the export task (needs a tokio runtime).
    pub fn start(endpoint: &str, client: reqwest::Client, public_key: String, secret_key: String) -> Exporter {
        let (tx, rx) = mpsc::channel(2048);
        let url = format!("{}/v1/traces", endpoint.trim_end_matches('/'));
        tokio::spawn(export_loop(rx, url, client, public_key, secret_key));
        Exporter { tx: Some(tx), dropped: Arc::new(AtomicU64::new(0)) }
    }

    pub fn export(&self, span: Span) {
        if !span.ctx.sampled {
            return;
        }
        if let Some(tx) = &self.tx {
            if tx.try_send(span).is_err() {
                self.dropped.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
}

async fn export_loop(
    mut rx: mpsc::Receiver<Span>,
    url: String,
    client: reqwest::Client,
    public_key: String,
    secret_key: String,
) {
    const MAX_BATCH: usize = 128;
    let mut buf: Vec<Span> = Vec::with_capacity(16);
    let mut failing = false;
    loop {
        // Wait for the first span, then linger briefly to batch more.
        match rx.recv().await {
            Some(s) => buf.push(s),
            None => return,
        }
        let deadline = tokio::time::Instant::now() + Duration::from_millis(500);
        while buf.len() < MAX_BATCH {
            match tokio::time::timeout_at(deadline, rx.recv()).await {
                Ok(Some(s)) => buf.push(s),
                Ok(None) | Err(_) => break,
            }
        }
        let body = serde_json::to_vec(&batch_json(&buf)).unwrap_or_default();
        let n = buf.len();
        buf.clear();
        let res = client
            .post(&url)
            .header("content-type", "application/json")
            .header("x-langfuse-public-key", "pk-lf-gateway")
            .basic_auth(&public_key, Some(&secret_key))
            .timeout(Duration::from_secs(5))
            .body(body)
            .send()
            .await;
        match res {
            Ok(r) if r.status().is_success() => {
                if failing {
                    tracing::info!("trace export recovered");
                    failing = false;
                }
            }
            Ok(r) => {
                if !failing {
                    tracing::warn!("trace export to {url}: HTTP {} ({n} spans dropped)", r.status());
                    failing = true;
                }
            }
            Err(e) => {
                if !failing {
                    tracing::warn!("trace export to {url} failed: {e} ({n} spans dropped)");
                    failing = true;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_traceparent() {
        let c = TraceCtx::from_traceparent(Some("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"));
        assert_eq!(c.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736");
        assert_eq!(c.parent_span_id.as_deref(), Some("00f067aa0ba902b7"));
        assert_ne!(c.span_id, "00f067aa0ba902b7");
        assert!(c.sampled);
        assert_eq!(c.traceparent(), format!("00-4bf92f3577b34da6a3ce929d0e0e4736-{}-01", c.span_id));
    }

    #[test]
    fn invalid_traceparent_starts_new_trace() {
        for bad in [
            "garbage",
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
            "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
            "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ] {
            let c = TraceCtx::from_traceparent(Some(bad));
            assert!(c.parent_span_id.is_none(), "{bad}");
            assert!(is_hex(&c.trace_id, 32) && is_hex(&c.span_id, 16));
        }
    }

    #[test]
    fn formats_rfc3339() {
        let t = UNIX_EPOCH + Duration::from_millis(1_793_918_903_123);
        assert_eq!(rfc3339(t), "2026-11-05T22:48:23.123Z");
        assert_eq!(rfc3339(UNIX_EPOCH), "1970-01-01T00:00:00.000Z");
        assert_eq!(rfc3339(UNIX_EPOCH + Duration::from_secs(951_782_400)), "2000-02-29T00:00:00.000Z");
    }

    #[test]
    fn span_json_shape() {
        let s = Span {
            ctx: TraceCtx::from_traceparent(Some("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")),
            name: "chat".into(),
            start: UNIX_EPOCH,
            end: UNIX_EPOCH + Duration::from_secs(1),
            error: None,
            attrs: vec![("a", AttrValue::Int(3)), ("b", AttrValue::Str("x".into()))],
        };
        let v = batch_json(&[s]);
        let span = &v["resourceSpans"][0]["scopeSpans"][0]["spans"][0];
        assert_eq!(span["parentSpanId"], "00f067aa0ba902b7");
        assert_eq!(span["endTimeUnixNano"], "1000000000");
        assert_eq!(span["attributes"][0]["value"]["intValue"], "3");
        assert_eq!(v["resourceSpans"][0]["resource"]["attributes"][0]["value"]["stringValue"], "stack-gateway");
    }
}
