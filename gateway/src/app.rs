//! HTTP surface: auth, routing, queueing, proxying, accounting.

use std::{
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::{Duration, Instant, SystemTime},
};

use axum::{
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, State},
    http::{header, HeaderMap, HeaderValue, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use futures_util::{Stream, StreamExt};
use serde_json::{json, Value};

use crate::{
    config::{Backend, Config, StreamUsage},
    db::{DbWriter, Row},
    otlp::{random_hex, rfc3339, AttrValue, Exporter, Span, TraceCtx},
    queue::{Limiter, Permit, Priority, QueueError},
};

pub struct BackendRt {
    pub cfg: Backend,
    pub limiter: Limiter,
}

pub struct AppState {
    pub cfg: Config,
    pub backends: Vec<BackendRt>,
    pub client: reqwest::Client,
    pub db: DbWriter,
    pub otlp: Exporter,
}

impl AppState {
    /// Build state, start the SQLite writer and the OTLP exporter (needs a tokio runtime).
    pub fn start(cfg: Config) -> Arc<AppState> {
        let client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(3))
            .pool_idle_timeout(Duration::from_secs(30))
            .pool_max_idle_per_host(4)
            .tcp_nodelay(true)
            .build()
            .expect("http client");
        let db = match &cfg.db_path {
            Some(p) => DbWriter::start(p).unwrap_or_else(|e| {
                tracing::warn!("request log disabled: {e}");
                DbWriter::disabled()
            }),
            None => DbWriter::disabled(),
        };
        let otlp = match &cfg.otlp_endpoint {
            Some(ep) => Exporter::start(
                ep,
                client.clone(),
                cfg.langfuse_public_key.clone(),
                cfg.langfuse_secret_key.clone(),
            ),
            None => Exporter::disabled(),
        };
        let backends = cfg
            .backends
            .iter()
            .map(|b| BackendRt { cfg: b.clone(), limiter: Limiter::new(b.max_concurrency, b.max_queue) })
            .collect();
        Arc::new(AppState { cfg, backends, client, db, otlp })
    }

    fn backend(&self, name: &str) -> &BackendRt {
        self.backends.iter().find(|b| b.cfg.name == name).expect("configured backend")
    }
}

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/models", get(models))
        .fallback(proxy)
        .layer(DefaultBodyLimit::max(64 * 1024 * 1024))
        .with_state(state)
}

// ---------------------------------------------------------------------------- helpers

pub fn error_response(status: StatusCode, code: &str, message: &str, request_id: &str) -> Response {
    let kind = if status.is_client_error() { "invalid_request_error" } else { "server_error" };
    let mut r = (
        status,
        Json(json!({ "error": { "message": message, "type": kind, "param": null, "code": code } })),
    )
        .into_response();
    if let Ok(v) = HeaderValue::from_str(request_id) {
        r.headers_mut().insert("x-request-id", v);
    }
    r
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Client name for the request, or None when the key is missing/unknown.
/// With no keys configured every request is accepted as `anonymous`.
pub fn authenticate(cfg: &Config, headers: &HeaderMap) -> Option<String> {
    if cfg.keys.is_empty() {
        return Some("anonymous".into());
    }
    let token = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer ").or_else(|| v.strip_prefix("bearer ")))
        .map(str::trim)?;
    let mut found = None;
    for (key, name) in &cfg.keys {
        if constant_time_eq(key.as_bytes(), token.as_bytes()) {
            found = Some(name.clone());
        }
    }
    found
}

fn request_id(headers: &HeaderMap) -> String {
    headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.:".contains(&b)))
        .map(str::to_string)
        .unwrap_or_else(|| random_hex(16))
}

fn truncate(mut s: String, max: usize) -> String {
    if s.len() > max {
        let mut cut = max;
        while !s.is_char_boundary(cut) {
            cut -= 1;
        }
        s.truncate(cut);
        s.push_str("…[truncated]");
    }
    s
}

fn as_i64(v: &Value) -> Option<i64> {
    v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))
}

/// (prompt, completion) tokens from an OpenAI-style or uzu decision `usage` object.
fn usage_tokens(usage: &Value) -> (Option<i64>, Option<i64>) {
    let p = usage.get("prompt_tokens").or_else(|| usage.get("input_tokens")).and_then(as_i64);
    let c = usage.get("completion_tokens").or_else(|| usage.get("output_tokens")).and_then(as_i64);
    (p, c)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Chat,
    Embeddings,
    Decision,
    Other,
}

impl Kind {
    fn of(path: &str) -> Kind {
        match path {
            "/v1/chat/completions" | "/v1/completions" => Kind::Chat,
            "/v1/embeddings" => Kind::Embeddings,
            "/v1/decisions" | "/v1/systemone" => Kind::Decision,
            _ => Kind::Other,
        }
    }
    fn op(self) -> &'static str {
        match self {
            Kind::Chat => "chat",
            Kind::Embeddings => "embeddings",
            Kind::Decision => "decision",
            Kind::Other => "request",
        }
    }
}

// ---------------------------------------------------------------------------- accounting

/// Everything known about one request; turned into a SQLite row + a span when finished.
struct ReqCtx {
    state: Arc<AppState>,
    id: String,
    trace: TraceCtx,
    client: String,
    model: String,
    sent_model: Option<String>,
    response_model: Option<String>,
    backend: String,
    endpoint: String,
    kind: Kind,
    stream: bool,
    priority: Priority,
    start_sys: SystemTime,
    start: Instant,
    queue_ms: Option<i64>,
    ttft: Option<Duration>,
    first_token_at: Option<SystemTime>,
    prompt_tokens: Option<i64>,
    completion_tokens: Option<i64>,
    temperature: Option<f64>,
    max_tokens: Option<i64>,
    input: Option<String>,
    output: Option<String>,
    status: u16,
    error: Option<String>,
}

impl ReqCtx {
    fn capture(&self, v: &Value) -> Option<String> {
        self.state.cfg.capture_content.then(|| truncate(v.to_string(), self.state.cfg.max_content_bytes))
    }

    fn finish(self) {
        let latency = self.start.elapsed();
        let end = self.start_sys + latency;
        let ttft_ms = self.ttft.map(|d| d.as_millis() as i64);
        tracing::info!(
            "{} {} {} {}->{} {} queue={}ms ttft={} total={}ms tokens={}/{}{}",
            self.id,
            self.client,
            self.endpoint,
            self.model,
            self.backend,
            self.status,
            self.queue_ms.unwrap_or(0),
            ttft_ms.map(|t| format!("{t}ms")).unwrap_or_else(|| "-".into()),
            latency.as_millis(),
            self.prompt_tokens.map(|t| t.to_string()).unwrap_or_else(|| "-".into()),
            self.completion_tokens.map(|t| t.to_string()).unwrap_or_else(|| "-".into()),
            self.error.as_deref().map(|e| format!(" error={e}")).unwrap_or_default(),
        );
        self.state.db.log(Row {
            id: self.id.clone(),
            ts: rfc3339(self.start_sys),
            client: self.client.clone(),
            model: self.model.clone(),
            backend: self.backend.clone(),
            endpoint: self.endpoint.clone(),
            stream: self.stream,
            status: self.status,
            queue_ms: self.queue_ms,
            latency_ms: latency.as_millis() as i64,
            ttft_ms,
            prompt_tokens: self.prompt_tokens,
            completion_tokens: self.completion_tokens,
            error: self.error.clone(),
            trace_id: self.trace.trace_id.clone(),
        });

        let s = |v: &str| AttrValue::Str(v.to_string());
        let obs_type = if self.kind == Kind::Embeddings { "embedding" } else { "generation" };
        let mut attrs: Vec<(&'static str, AttrValue)> = vec![
            ("langfuse.observation.type", s(obs_type)),
            ("gen_ai.operation.name", s(self.kind.op())),
            ("gen_ai.request.model", s(&self.model)),
            (
                "gen_ai.response.model",
                s(self.response_model.as_deref().or(self.sent_model.as_deref()).unwrap_or(&self.model)),
            ),
            ("user.id", s(&self.client)),
            ("langfuse.user.id", s(&self.client)),
            ("gateway.request_id", s(&self.id)),
            ("gateway.backend", s(&self.backend)),
            ("gateway.priority", s(self.priority.as_str())),
            ("gateway.stream", AttrValue::Bool(self.stream)),
            ("http.route", s(&self.endpoint)),
            ("http.response.status_code", AttrValue::Int(self.status as i64)),
        ];
        if self.trace.parent_span_id.is_none() {
            attrs.push(("langfuse.trace.name", s(&format!("{} {}", self.kind.op(), self.model))));
        }
        if let Some(q) = self.queue_ms {
            attrs.push(("gateway.queue_ms", AttrValue::Int(q)));
        }
        if let Some(t) = ttft_ms {
            attrs.push(("gateway.ttft_ms", AttrValue::Int(t)));
        }
        if let Some(t) = self.first_token_at {
            attrs.push(("langfuse.observation.completion_start_time", s(&rfc3339(t))));
        }
        if let Some(t) = self.prompt_tokens {
            attrs.push(("gen_ai.usage.input_tokens", AttrValue::Int(t)));
        }
        if let Some(t) = self.completion_tokens {
            attrs.push(("gen_ai.usage.output_tokens", AttrValue::Int(t)));
        }
        if let Some(t) = self.temperature {
            attrs.push(("gen_ai.request.temperature", s(&t.to_string())));
        }
        if let Some(t) = self.max_tokens {
            attrs.push(("gen_ai.request.max_tokens", AttrValue::Int(t)));
        }
        if let Some(i) = self.input {
            attrs.push(("langfuse.observation.input", AttrValue::Str(i)));
        }
        if let Some(o) = self.output {
            attrs.push(("langfuse.observation.output", AttrValue::Str(o)));
        }
        if let Some(e) = &self.error {
            attrs.push(("langfuse.observation.level", s("ERROR")));
            attrs.push(("langfuse.observation.status_message", s(e)));
        }
        let error = self.error.or_else(|| (self.status >= 500).then(|| format!("HTTP {}", self.status)));
        self.state.otlp.export(Span {
            ctx: self.trace,
            name: format!("{} {}", self.kind.op(), self.model),
            start: self.start_sys,
            end,
            error,
            attrs,
        });
    }
}

/// Finishes the request exactly once; if dropped unfinished the client went away.
struct Tracked(Option<Box<ReqCtx>>);

impl Tracked {
    fn get(&mut self) -> &mut ReqCtx {
        self.0.as_mut().expect("request already finished")
    }
    fn finish(&mut self) {
        if let Some(c) = self.0.take() {
            c.finish();
        }
    }
    fn fail(&mut self, status: StatusCode, error: impl Into<String>) {
        let c = self.get();
        c.status = status.as_u16();
        c.error = Some(error.into());
        self.finish();
    }
}

impl Drop for Tracked {
    fn drop(&mut self) {
        if let Some(c) = self.0.as_mut() {
            c.status = 499;
            c.error.get_or_insert_with(|| "client disconnected".into());
        }
        self.finish();
    }
}

// ---------------------------------------------------------------------------- handlers

async fn health(State(st): State<Arc<AppState>>) -> Response {
    let checks = st.backends.iter().map(|b| {
        let url = format!("{}{}", b.cfg.url, b.cfg.health);
        let req = st.client.get(url).timeout(Duration::from_millis(1500));
        async move {
            let up = matches!(req.send().await, Ok(r) if r.status().is_success());
            let (in_flight, queued) = b.limiter.stats();
            (
                b.cfg.name.clone(),
                json!({
                    "up": up,
                    "url": b.cfg.url,
                    "models": b.cfg.models,
                    "paths": b.cfg.paths,
                    "in_flight": in_flight,
                    "queued": queued,
                    "max_concurrency": b.cfg.max_concurrency,
                }),
            )
        }
    });
    let results = futures_util::future::join_all(checks).await;
    let backends: serde_json::Map<String, Value> = results.into_iter().collect();
    Json(json!({
        "status": "ok",
        "service": "stack-gateway",
        "version": env!("CARGO_PKG_VERSION"),
        "backends": backends,
        "traces_dropped": st.otlp.dropped.load(std::sync::atomic::Ordering::Relaxed),
    }))
    .into_response()
}

async fn models(State(st): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    let rid = request_id(&headers);
    if authenticate(&st.cfg, &headers).is_none() {
        return error_response(StatusCode::UNAUTHORIZED, "invalid_api_key", "missing or invalid API key", &rid);
    }
    let data: Vec<Value> = st
        .backends
        .iter()
        .flat_map(|b| {
            b.cfg.models.iter().map(move |m| json!({ "id": m, "object": "model", "created": 0, "owned_by": b.cfg.name }))
        })
        .collect();
    let mut r = Json(json!({ "object": "list", "data": data })).into_response();
    r.headers_mut().insert("x-request-id", HeaderValue::from_str(&rid).unwrap());
    r
}

async fn proxy(
    State(st): State<Arc<AppState>>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let rid = request_id(&headers);
    let path = uri.path().to_string();
    if method != Method::POST || !path.starts_with("/v1/") {
        return error_response(StatusCode::NOT_FOUND, "not_found", &format!("no route for {method} {path}"), &rid);
    }
    let Some(client) = authenticate(&st.cfg, &headers) else {
        return error_response(StatusCode::UNAUTHORIZED, "invalid_api_key", "missing or invalid API key", &rid);
    };

    let parsed: Option<Value> = serde_json::from_slice(&body).ok().filter(Value::is_object);
    let model = parsed.as_ref().and_then(|v| v.get("model")).and_then(Value::as_str).unwrap_or("").to_string();

    let backend = match st.cfg.backend_for_path(&path) {
        Some(b) => b,
        None => {
            let Some(v) = &parsed else {
                return error_response(StatusCode::BAD_REQUEST, "invalid_json", "request body must be a JSON object", &rid);
            };
            if model.is_empty() {
                let _ = v;
                return error_response(StatusCode::BAD_REQUEST, "missing_model", "\"model\" is required", &rid);
            }
            match st.cfg.backend_for_model(&model) {
                Some(b) => b,
                None => {
                    return error_response(
                        StatusCode::NOT_FOUND,
                        "model_not_found",
                        &format!("model {model:?} is not routed by this gateway (see GET /v1/models)"),
                        &rid,
                    )
                }
            }
        }
    };
    let backend = st.backend(&backend.name);
    let bcfg = &backend.cfg;
    let kind = Kind::of(&path);
    let stream = parsed.as_ref().and_then(|v| v.get("stream")).and_then(Value::as_bool).unwrap_or(false);

    // Rewrite the body only when needed; otherwise forward the client's bytes untouched.
    let mut sent_model = None;
    let upstream_body: Bytes = match parsed.clone() {
        Some(mut v) => {
            let mut changed = false;
            if let Some(m) = &bcfg.rewrite_model {
                if v.get("model").and_then(Value::as_str) != Some(m.as_str()) {
                    v["model"] = json!(m);
                    changed = true;
                }
                sent_model = Some(m.clone());
            }
            if stream && kind == Kind::Chat && bcfg.stream_usage == StreamUsage::Inject {
                let so = v.get("stream_options");
                if so.and_then(|o| o.get("include_usage")).is_none() {
                    let mut o = so.filter(|o| o.is_object()).cloned().unwrap_or_else(|| json!({}));
                    o["include_usage"] = json!(true);
                    v["stream_options"] = o;
                    changed = true;
                }
            }
            if changed {
                Bytes::from(serde_json::to_vec(&v).unwrap_or_default())
            } else {
                body.clone()
            }
        }
        None => body.clone(),
    };

    let trace = TraceCtx::from_traceparent(headers.get("traceparent").and_then(|v| v.to_str().ok()));
    let priority = Priority::from_header(headers.get("x-stack-priority").and_then(|v| v.to_str().ok()));
    let mut ctx = ReqCtx {
        state: st.clone(),
        id: rid.clone(),
        trace,
        client: client.clone(),
        model: if model.is_empty() { kind.op().to_string() } else { model.clone() },
        sent_model,
        response_model: None,
        backend: bcfg.name.clone(),
        endpoint: path.clone(),
        kind,
        stream,
        priority,
        start_sys: SystemTime::now(),
        start: Instant::now(),
        queue_ms: None,
        ttft: None,
        first_token_at: None,
        prompt_tokens: None,
        completion_tokens: None,
        temperature: parsed.as_ref().and_then(|v| v.get("temperature")).and_then(Value::as_f64),
        max_tokens: parsed
            .as_ref()
            .and_then(|v| v.get("max_completion_tokens").or_else(|| v.get("max_tokens")))
            .and_then(as_i64),
        input: None,
        output: None,
        status: 0,
        error: None,
    };
    if let Some(v) = &parsed {
        let inp = match kind {
            Kind::Chat => v.get("messages").or_else(|| v.get("prompt")).cloned(),
            Kind::Embeddings => v.get("input").cloned(),
            Kind::Decision => Some(json!({ "state": v.get("state"), "questions": v.get("questions") })),
            Kind::Other => Some(v.clone()),
        };
        ctx.input = inp.and_then(|i| ctx.capture(&i));
    }
    let mut tracked = Tracked(Some(Box::new(ctx)));

    // ---- queue
    let q_start = Instant::now();
    let permit = match backend.limiter.acquire(priority, bcfg.queue_timeout).await {
        Ok(p) => p,
        Err(e) => {
            let (msg, retry) = match e {
                QueueError::Full => (format!("backend {} is busy: queue full", bcfg.name), "5"),
                QueueError::Timeout => (format!("backend {} is busy: timed out in queue", bcfg.name), "10"),
            };
            tracked.get().queue_ms = Some(q_start.elapsed().as_millis() as i64);
            tracked.fail(StatusCode::SERVICE_UNAVAILABLE, msg.clone());
            let mut r = error_response(StatusCode::SERVICE_UNAVAILABLE, "backend_busy", &msg, &rid);
            r.headers_mut().insert(header::RETRY_AFTER, HeaderValue::from_static(retry));
            return r;
        }
    };
    let waited = q_start.elapsed();
    tracked.get().queue_ms = Some(waited.as_millis() as i64);
    if waited > Duration::from_millis(50) {
        tracing::info!("{rid} waited {}ms in queue for {} ({})", waited.as_millis(), bcfg.name, priority.as_str());
    }

    // ---- upstream request
    let url = format!("{}{}", bcfg.url, path);
    let mut req = st
        .client
        .post(&url)
        .header(header::CONTENT_TYPE, "application/json")
        .header("traceparent", tracked.get().trace.traceparent())
        .header("x-request-id", &rid)
        .header("x-stack-client", &client)
        .body(upstream_body);
    if let Some(a) = headers.get(header::ACCEPT) {
        req = req.header(header::ACCEPT, a);
    }
    if let Some(ts) = headers.get("tracestate") {
        req = req.header("tracestate", ts);
    }
    if let Some(k) = &bcfg.api_key {
        req = req.bearer_auth(k);
    }
    let resp = match tokio::time::timeout(bcfg.timeout, req.send()).await {
        Ok(Ok(r)) => r,
        Ok(Err(e)) => {
            let msg = if e.is_connect() {
                format!("backend {} is not available", bcfg.name)
            } else {
                format!("backend {} request failed: {}", bcfg.name, e)
            };
            tracked.fail(StatusCode::SERVICE_UNAVAILABLE, msg.clone());
            let mut r = error_response(StatusCode::SERVICE_UNAVAILABLE, "backend_unavailable", &msg, &rid);
            r.headers_mut().insert(header::RETRY_AFTER, HeaderValue::from_static("30"));
            return r;
        }
        Err(_) => {
            let msg = format!("backend {} did not answer within {}s", bcfg.name, bcfg.timeout.as_secs());
            tracked.fail(StatusCode::GATEWAY_TIMEOUT, msg.clone());
            return error_response(StatusCode::GATEWAY_TIMEOUT, "backend_timeout", &msg, &rid);
        }
    };

    let status = resp.status();
    tracked.get().status = status.as_u16();
    let content_type = resp.headers().get(header::CONTENT_TYPE).cloned();
    let is_sse = content_type
        .as_ref()
        .and_then(|v| v.to_str().ok())
        .is_some_and(|s| s.starts_with("text/event-stream"));

    let mut builder = Response::builder().status(status.as_u16()).header("x-request-id", rid.as_str());
    if let Some(ct) = &content_type {
        builder = builder.header(header::CONTENT_TYPE, ct);
    }

    if is_sse {
        let tap = SseTap {
            inner: Box::pin(resp.bytes_stream()),
            tracked,
            permit: Some(permit),
            line: Vec::new(),
            text: String::new(),
            reasoning: String::new(),
            tool_calls: 0,
            capture: st.cfg.capture_content,
            max: st.cfg.max_content_bytes,
            started: Instant::now(),
        };
        return builder
            .header(header::CACHE_CONTROL, "no-cache")
            .header("x-accel-buffering", "no")
            .body(Body::from_stream(tap))
            .unwrap_or_else(|_| error_response(StatusCode::INTERNAL_SERVER_ERROR, "internal", "response", &rid));
    }

    // ---- buffered response
    let remaining = bcfg.timeout.saturating_sub(tracked.get().start.elapsed());
    let bytes = match tokio::time::timeout(remaining, resp.bytes()).await {
        Ok(Ok(b)) => b,
        Ok(Err(e)) => {
            let msg = format!("backend {} response failed: {e}", bcfg.name);
            tracked.fail(StatusCode::BAD_GATEWAY, msg.clone());
            return error_response(StatusCode::BAD_GATEWAY, "backend_error", &msg, &rid);
        }
        Err(_) => {
            let msg = format!("backend {} did not finish within {}s", bcfg.name, bcfg.timeout.as_secs());
            tracked.fail(StatusCode::GATEWAY_TIMEOUT, msg.clone());
            return error_response(StatusCode::GATEWAY_TIMEOUT, "backend_timeout", &msg, &rid);
        }
    };
    drop(permit);
    {
        let c = tracked.get();
        match serde_json::from_slice::<Value>(&bytes) {
            Ok(v) => {
                if let Some(u) = v.get("usage") {
                    (c.prompt_tokens, c.completion_tokens) = usage_tokens(u);
                }
                c.response_model = v.get("model").and_then(Value::as_str).map(str::to_string);
                if !status.is_success() {
                    c.error = Some(
                        v.pointer("/error/message")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                            .unwrap_or_else(|| truncate(v.to_string(), 512)),
                    );
                }
                let out = match kind {
                    _ if !status.is_success() => Some(v.clone()),
                    Kind::Chat => v.pointer("/choices/0/message").or_else(|| v.pointer("/choices/0/text")).cloned(),
                    Kind::Embeddings => {
                        let data = v.get("data").and_then(Value::as_array);
                        Some(json!({
                            "embeddings": data.map(Vec::len).unwrap_or(0),
                            "dimensions": data
                                .and_then(|d| d.first())
                                .and_then(|e| e.get("embedding"))
                                .and_then(Value::as_array)
                                .map(Vec::len),
                        }))
                    }
                    _ => Some(v.clone()),
                };
                c.output = out.and_then(|o| c.capture(&o));
            }
            Err(_) if !status.is_success() => {
                c.error = Some(truncate(String::from_utf8_lossy(&bytes).into_owned(), 512));
            }
            Err(_) => {}
        }
    }
    tracked.finish();
    builder
        .body(Body::from(bytes))
        .unwrap_or_else(|_| error_response(StatusCode::INTERNAL_SERVER_ERROR, "internal", "response", &rid))
}

// ---------------------------------------------------------------------------- SSE passthrough

/// Forwards upstream SSE bytes unchanged, chunk by chunk, while watching the events for
/// TTFT, usage and (optionally) output text. Holds the backend permit until the stream ends.
struct SseTap {
    inner: Pin<Box<dyn Stream<Item = reqwest::Result<Bytes>> + Send>>,
    tracked: Tracked,
    permit: Option<Permit>,
    line: Vec<u8>,
    text: String,
    reasoning: String,
    tool_calls: usize,
    capture: bool,
    max: usize,
    started: Instant,
}

impl SseTap {
    fn observe(&mut self, chunk: &[u8]) {
        self.line.extend_from_slice(chunk);
        while let Some(pos) = self.line.iter().position(|&b| b == b'\n') {
            let line: Vec<u8> = self.line.drain(..=pos).collect();
            self.event_line(&line);
        }
        if self.line.len() > 4 * 1024 * 1024 {
            self.line.clear(); // not SSE we understand; stop buffering
        }
    }

    fn event_line(&mut self, line: &[u8]) {
        let line = line.strip_suffix(b"\n").unwrap_or(line);
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        let Some(data) = line.strip_prefix(b"data:") else { return };
        let data = data.trim_ascii();
        if data.is_empty() || data == b"[DONE]" {
            return;
        }
        let Ok(v) = serde_json::from_slice::<Value>(data) else { return };
        let c = self.tracked.get();
        if c.response_model.is_none() {
            c.response_model = v.get("model").and_then(Value::as_str).map(str::to_string);
        }
        if let Some(u) = v.get("usage").filter(|u| u.is_object()) {
            let (p, cpl) = usage_tokens(u);
            c.prompt_tokens = p.or(c.prompt_tokens);
            c.completion_tokens = cpl.or(c.completion_tokens);
        }
        if let Some(msg) = v.pointer("/error/message").and_then(Value::as_str) {
            c.error = Some(msg.to_string());
        }
        let Some(delta) = v.pointer("/choices/0/delta").or_else(|| v.pointer("/choices/0")) else { return };
        let content = delta.get("content").and_then(Value::as_str).or_else(|| delta.get("text").and_then(Value::as_str));
        let reasoning = delta
            .get("reasoning_content")
            .or_else(|| delta.get("reasoning"))
            .and_then(Value::as_str);
        let tools = delta.get("tool_calls").and_then(Value::as_array).map(Vec::len).unwrap_or(0);
        let produced = content.is_some_and(|s| !s.is_empty()) || reasoning.is_some_and(|s| !s.is_empty()) || tools > 0;
        if produced && c.ttft.is_none() {
            c.ttft = Some(c.start.elapsed());
            c.first_token_at = Some(SystemTime::now());
        }
        if self.capture {
            let max = self.max;
            let push = |buf: &mut String, s: Option<&str>| {
                if let Some(s) = s {
                    if buf.len() < max {
                        buf.push_str(s);
                    }
                }
            };
            push(&mut self.text, content);
            push(&mut self.reasoning, reasoning);
        }
        if delta.pointer("/tool_calls/0/function/name").is_some() {
            self.tool_calls += 1;
        }
    }

    fn done(&mut self, error: Option<String>) {
        self.permit.take();
        if self.tracked.0.is_none() {
            return;
        }
        if self.capture {
            let mut out = json!({ "role": "assistant", "content": std::mem::take(&mut self.text) });
            if !self.reasoning.is_empty() {
                out["reasoning_content"] = json!(std::mem::take(&mut self.reasoning));
            }
            if self.tool_calls > 0 {
                out["tool_calls"] = json!(self.tool_calls);
            }
            let c = self.tracked.get();
            c.output = c.capture(&out);
        }
        match error {
            Some(e) => self.tracked.fail(StatusCode::BAD_GATEWAY, e),
            None => self.tracked.finish(),
        }
    }
}

impl Stream for SseTap {
    type Item = Result<Bytes, std::io::Error>;

    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        match self.inner.poll_next_unpin(cx) {
            Poll::Ready(Some(Ok(b))) => {
                self.observe(&b);
                Poll::Ready(Some(Ok(b)))
            }
            Poll::Ready(Some(Err(e))) => {
                let msg = format!("upstream stream error after {}ms: {e}", self.started.elapsed().as_millis());
                self.done(Some(msg.clone()));
                Poll::Ready(Some(Err(std::io::Error::other(msg))))
            }
            Poll::Ready(None) => {
                if !self.line.is_empty() {
                    let rest = std::mem::take(&mut self.line);
                    self.event_line(&rest);
                }
                self.done(None);
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(keys: &str) -> Config {
        Config::from_parts("", &|k| (k == "GATEWAY_KEYS").then(|| keys.to_string())).unwrap()
    }

    fn auth_headers(v: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert(header::AUTHORIZATION, HeaderValue::from_str(v).unwrap());
        h
    }

    #[test]
    fn auth_maps_key_to_client() {
        let c = cfg("owui:k1,me:k2");
        assert_eq!(authenticate(&c, &auth_headers("Bearer k2")).as_deref(), Some("me"));
        assert_eq!(authenticate(&c, &auth_headers("Bearer k1")).as_deref(), Some("owui"));
        assert_eq!(authenticate(&c, &auth_headers("Bearer nope")), None);
        assert_eq!(authenticate(&c, &auth_headers("k1")), None);
        assert_eq!(authenticate(&c, &HeaderMap::new()), None);
    }

    #[test]
    fn no_keys_means_anonymous() {
        assert_eq!(authenticate(&cfg(""), &HeaderMap::new()).as_deref(), Some("anonymous"));
    }

    #[test]
    fn request_id_sanitized() {
        let mut h = HeaderMap::new();
        h.insert("x-request-id", HeaderValue::from_static("abc-123"));
        assert_eq!(request_id(&h), "abc-123");
        h.insert("x-request-id", HeaderValue::from_static("bad id<>"));
        assert_eq!(request_id(&h).len(), 32);
    }

    #[test]
    fn truncates_on_char_boundary() {
        let s = truncate("ääää".into(), 3);
        assert!(s.starts_with('ä') && s.ends_with("[truncated]"));
    }

    #[test]
    fn usage_variants() {
        assert_eq!(usage_tokens(&json!({"prompt_tokens": 3, "completion_tokens": 4})), (Some(3), Some(4)));
        assert_eq!(usage_tokens(&json!({"input_tokens": 7})), (Some(7), None));
    }
}
