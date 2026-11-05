//! In-process tests: gateway + mock backend + mock OTLP collector, all on 127.0.0.1:0.

use std::{
    net::SocketAddr,
    sync::{Arc, Mutex},
    time::Duration,
};

use axum::{
    body::{Body, Bytes},
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use futures_util::StreamExt;
use serde_json::{json, Value};
use stack_gateway::{AppState, Config};
use tokio::sync::Semaphore;

const KEY: &str = "secret-key";
const TP_TRACE: &str = "4bf92f3577b34da6a3ce929d0e0e4736";
const TP_SPAN: &str = "00f067aa0ba902b7";

/// Uzu-style SSE (no space after `data:`), final chunk carries usage.
const SSE_HEAD: &str = "data:{\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\"}}]}\n\n\
data:{\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"Hel\"}}]}\n\n";
const SSE_TAIL: &str = "data:{\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"lo\"}}]}\n\n\
data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":2,\"total_tokens\":13}}\n\n\
data:[DONE]\n\n";


struct Mock {
    /// (path, headers, body) of every request the backend saw
    seen: Mutex<Vec<(String, HeaderMap, Value)>>,
    /// stream requests wait here between head and tail; "hold" requests wait before answering
    gate: Semaphore,
    traces: Mutex<Vec<(HeaderMap, Value)>>,
}

async fn spawn(router: Router) -> SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, router).await.unwrap() });
    addr
}

async fn mock_chat(State(m): State<Arc<Mock>>, headers: HeaderMap, Json(body): Json<Value>) -> Response {
    m.seen.lock().unwrap().push(("/v1/chat/completions".into(), headers, body.clone()));
    let tag = body.pointer("/messages/0/content").and_then(Value::as_str).unwrap_or("").to_string();
    if tag.starts_with("hold") {
        m.gate.acquire().await.unwrap().forget();
    }
    if body.get("stream").and_then(Value::as_bool) == Some(true) {
        let m2 = m.clone();
        let s = futures_util::stream::once(async { Ok::<_, std::io::Error>(Bytes::from(SSE_HEAD)) }).chain(
            futures_util::stream::once(async move {
                m2.gate.acquire().await.unwrap().forget();
                Ok(Bytes::from(SSE_TAIL))
            }),
        );
        return Response::builder()
            .header("content-type", "text/event-stream")
            .body(Body::from_stream(s))
            .unwrap();
    }
    Json(json!({
        "id": "c2", "object": "chat.completion", "model": "real-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": format!("echo {tag}")}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    }))
    .into_response()
}

async fn mock_embed(State(m): State<Arc<Mock>>, headers: HeaderMap, Json(body): Json<Value>) -> Response {
    m.seen.lock().unwrap().push(("/v1/embeddings".into(), headers, body));
    Json(json!({"object":"list","model":"emb","data":[{"object":"embedding","index":0,"embedding":[0.1,0.2,0.3]}],
                "usage":{"prompt_tokens":4,"total_tokens":4}}))
    .into_response()
}

async fn mock_traces(State(m): State<Arc<Mock>>, headers: HeaderMap, Json(body): Json<Value>) -> StatusCode {
    m.traces.lock().unwrap().push((headers, body));
    StatusCode::OK
}

struct Env {
    gw: String,
    mock: Arc<Mock>,
    db: std::path::PathBuf,
    _dir: tempfile::TempDir,
    http: reqwest::Client,
}

async fn setup(extra_toml: &str) -> Env {
    let mock = Arc::new(Mock { seen: Mutex::default(), gate: Semaphore::new(0), traces: Mutex::default() });
    let backend = spawn(
        Router::new()
            .route("/v1/chat/completions", post(mock_chat))
            .route("/v1/embeddings", post(mock_embed))
            .route("/v1/models", get(|| async { Json(json!({"object":"list","data":[]})) }))
            .route("/v1/traces", post(mock_traces))
            .with_state(mock.clone()),
    )
    .await;
    // a port with nothing listening
    let dead = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap()
    };
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("stack.db");
    let toml = format!(
        r#"
        [backends.mock]
        url = "http://{backend}"
        models = ["chat", "small"]
        rewrite_model = "real-model"
        stream_usage = "inject"
        max_concurrency = 1
        max_queue = 3
        queue_timeout_secs = 5

        [backends.embed]
        url = "http://{backend}"
        models = ["embed"]
        rewrite_model = "emb"
        max_concurrency = 4

        [backends.decide]
        url = "http://{dead}"
        paths = ["/v1/decisions", "/v1/systemone"]
        {extra_toml}
        "#
    );
    let envs: Vec<(String, String)> = vec![
        ("GATEWAY_KEYS".into(), format!("tester:{KEY},other:k2")),
        ("STACK_DB".into(), db.display().to_string()),
        ("STACK_OTLP_ENDPOINT".into(), format!("http://{backend}")),
        ("LANGFUSE_PUBLIC_KEY".into(), "pk-lf-test".into()),
        ("LANGFUSE_SECRET_KEY".into(), "sk-test".into()),
    ];
    let cfg = Config::from_parts(&toml, &|k| envs.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone())).unwrap();
    let state = AppState::start(cfg);
    let gw = spawn(stack_gateway::router(state)).await;
    Env { gw: format!("http://{gw}"), mock, db, _dir: dir, http: reqwest::Client::builder().no_proxy().build().unwrap() }
}

impl Env {
    fn post(&self, path: &str, body: Value) -> reqwest::RequestBuilder {
        self.http.post(format!("{}{path}", self.gw)).bearer_auth(KEY).json(&body)
    }

    async fn row(&self, id: &str) -> Value {
        for _ in 0..200 {
            if let Ok(conn) = rusqlite::Connection::open(&self.db) {
                let r = conn.query_row(
                    "SELECT client, model, backend, endpoint, stream, status, queue_ms, latency_ms, ttft_ms,
                            prompt_tokens, completion_tokens, error, trace_id FROM gw_requests WHERE id = ?1",
                    [id],
                    |r| {
                        Ok(json!({
                            "client": r.get::<_, String>(0)?, "model": r.get::<_, String>(1)?,
                            "backend": r.get::<_, String>(2)?, "endpoint": r.get::<_, String>(3)?,
                            "stream": r.get::<_, i64>(4)?, "status": r.get::<_, i64>(5)?,
                            "queue_ms": r.get::<_, Option<i64>>(6)?, "latency_ms": r.get::<_, i64>(7)?,
                            "ttft_ms": r.get::<_, Option<i64>>(8)?, "prompt_tokens": r.get::<_, Option<i64>>(9)?,
                            "completion_tokens": r.get::<_, Option<i64>>(10)?, "error": r.get::<_, Option<String>>(11)?,
                            "trace_id": r.get::<_, String>(12)?,
                        }))
                    },
                );
                if let Ok(v) = r {
                    return v;
                }
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        panic!("no gw_requests row for {id}");
    }

    async fn span(&self, request_id: &str) -> (HeaderMap, Value) {
        for _ in 0..200 {
            for (h, body) in self.mock.traces.lock().unwrap().iter() {
                for s in body["resourceSpans"][0]["scopeSpans"][0]["spans"].as_array().unwrap() {
                    if attr(s, "gateway.request_id") == Some(json!(request_id)) {
                        return (h.clone(), s.clone());
                    }
                }
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        panic!("no span for {request_id}");
    }

    async fn queued(&self, backend: &str) -> u64 {
        let h: Value = self.http.get(format!("{}/health", self.gw)).send().await.unwrap().json().await.unwrap();
        h["backends"][backend]["queued"].as_u64().unwrap()
    }
}

fn attr(span: &Value, key: &str) -> Option<Value> {
    span["attributes"].as_array()?.iter().find(|a| a["key"] == key).map(|a| {
        let v = &a["value"];
        v.get("stringValue").or(v.get("intValue")).or(v.get("boolValue")).cloned().unwrap()
    })
}

#[tokio::test]
async fn stream_passthrough_usage_sqlite_trace() {
    let env = setup("").await;
    let resp = env
        .post("/v1/chat/completions", json!({"model": "chat", "stream": true, "messages": [{"role": "user", "content": "hi"}]}))
        .header("traceparent", format!("00-{TP_TRACE}-{TP_SPAN}-01"))
        .header("x-request-id", "req-stream-1")
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    assert_eq!(resp.headers()["x-request-id"], "req-stream-1");
    assert!(resp.headers()["content-type"].to_str().unwrap().starts_with("text/event-stream"));

    // The head arrives while the backend still holds the tail: no buffering.
    let mut body = resp.bytes_stream();
    let mut got = Vec::new();
    while got.len() < SSE_HEAD.len() {
        let chunk = tokio::time::timeout(Duration::from_secs(5), body.next()).await.expect("head not streamed").unwrap();
        got.extend_from_slice(&chunk.unwrap());
    }
    assert_eq!(got, SSE_HEAD.as_bytes());
    env.mock.gate.add_permits(1);
    while let Some(c) = body.next().await {
        got.extend_from_slice(&c.unwrap());
    }
    assert_eq!(String::from_utf8(got).unwrap(), format!("{SSE_HEAD}{SSE_TAIL}"));

    // backend saw rewritten model, injected include_usage and a child traceparent
    let (_, headers, sent) = env.mock.seen.lock().unwrap()[0].clone();
    assert_eq!(sent["model"], "real-model");
    assert_eq!(sent["stream_options"]["include_usage"], true);
    let tp = headers["traceparent"].to_str().unwrap().to_string();
    assert!(tp.starts_with(&format!("00-{TP_TRACE}-")) && !tp.contains(TP_SPAN), "{tp}");
    assert_eq!(headers["x-stack-client"], "tester");
    assert!(headers.get("authorization").is_none(), "gateway key must not leak downstream");

    let row = env.row("req-stream-1").await;
    assert_eq!(row["client"], "tester");
    assert_eq!(row["model"], "chat");
    assert_eq!(row["backend"], "mock");
    assert_eq!(row["stream"], 1);
    assert_eq!(row["status"], 200);
    assert_eq!(row["prompt_tokens"], 11);
    assert_eq!(row["completion_tokens"], 2);
    assert!(row["ttft_ms"].is_i64());
    assert_eq!(row["trace_id"], TP_TRACE);

    let (h, span) = env.span("req-stream-1").await;
    assert_eq!(h["x-langfuse-public-key"], "pk-lf-gateway");
    assert!(h["authorization"].to_str().unwrap().starts_with("Basic "));
    assert_eq!(h["content-type"], "application/json");
    assert_eq!(span["traceId"], TP_TRACE);
    assert_eq!(span["parentSpanId"], TP_SPAN);
    assert_eq!(format!("00-{TP_TRACE}-{}-01", span["spanId"].as_str().unwrap()), tp);
    assert_eq!(attr(&span, "langfuse.observation.type"), Some(json!("generation")));
    assert_eq!(attr(&span, "gen_ai.request.model"), Some(json!("chat")));
    assert_eq!(attr(&span, "gen_ai.response.model"), Some(json!("real-model")));
    assert_eq!(attr(&span, "gen_ai.usage.input_tokens"), Some(json!("11")));
    assert_eq!(attr(&span, "gen_ai.usage.output_tokens"), Some(json!("2")));
    assert_eq!(attr(&span, "user.id"), Some(json!("tester")));
    assert!(attr(&span, "langfuse.trace.name").is_none(), "child spans must not rename the parent trace");
    let out: Value = serde_json::from_str(attr(&span, "langfuse.observation.output").unwrap().as_str().unwrap()).unwrap();
    assert_eq!(out["content"], "Hello");
    let inp: Value = serde_json::from_str(attr(&span, "langfuse.observation.input").unwrap().as_str().unwrap()).unwrap();
    assert_eq!(inp[0]["content"], "hi");
}

#[tokio::test]
async fn non_stream_chat_and_embeddings() {
    let env = setup("").await;
    let r = env
        .post("/v1/chat/completions", json!({"model": "small", "messages": [{"role": "user", "content": "x"}]}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    let rid = r.headers()["x-request-id"].to_str().unwrap().to_string();
    assert_eq!(rid.len(), 32);
    let v: Value = r.json().await.unwrap();
    assert_eq!(v["choices"][0]["message"]["content"], "echo x");
    let row = env.row(&rid).await;
    assert_eq!((row["prompt_tokens"].clone(), row["completion_tokens"].clone()), (json!(5), json!(3)));
    assert_eq!(row["stream"], 0);
    assert!(row["ttft_ms"].is_null());
    let (_, span) = env.span(&rid).await;
    assert!(span.get("parentSpanId").is_none());
    assert_eq!(attr(&span, "langfuse.trace.name"), Some(json!("chat small")));

    let r = env.post("/v1/embeddings", json!({"model": "embed", "input": "hello"})).send().await.unwrap();
    assert_eq!(r.status(), 200);
    let rid = r.headers()["x-request-id"].to_str().unwrap().to_string();
    let row = env.row(&rid).await;
    assert_eq!(row["backend"], "embed");
    assert_eq!(row["prompt_tokens"], 4);
    let seen = env.mock.seen.lock().unwrap().iter().find(|s| s.0 == "/v1/embeddings").unwrap().2.clone();
    assert_eq!(seen["model"], "emb");
    let (_, span) = env.span(&rid).await;
    assert_eq!(attr(&span, "langfuse.observation.type"), Some(json!("embedding")));
}

#[tokio::test]
async fn auth_models_health_and_errors() {
    let env = setup("").await;
    let gw = &env.gw;
    let h = &env.http;
    // no key / wrong key
    let r = h.post(format!("{gw}/v1/chat/completions")).json(&json!({"model": "chat"})).send().await.unwrap();
    assert_eq!(r.status(), 401);
    let v: Value = r.json().await.unwrap();
    assert_eq!(v["error"]["code"], "invalid_api_key");
    let r = h.get(format!("{gw}/v1/models")).bearer_auth("wrong").send().await.unwrap();
    assert_eq!(r.status(), 401);
    // models
    let v: Value = h.get(format!("{gw}/v1/models")).bearer_auth("k2").send().await.unwrap().json().await.unwrap();
    let ids: Vec<&str> = v["data"].as_array().unwrap().iter().map(|m| m["id"].as_str().unwrap()).collect();
    assert_eq!(ids, vec!["chat", "small", "embed"]);
    // health is open and reports up/down
    let v: Value = h.get(format!("{gw}/health")).send().await.unwrap().json().await.unwrap();
    assert_eq!(v["status"], "ok");
    assert_eq!(v["backends"]["mock"]["up"], true);
    assert_eq!(v["backends"]["decide"]["up"], false);
    // unknown model
    let r = env.post("/v1/chat/completions", json!({"model": "nope", "messages": []})).send().await.unwrap();
    assert_eq!(r.status(), 404);
    // dead backend -> 503 OpenAI error
    for path in ["/v1/decisions", "/v1/systemone"] {
        let r = env.post(path, json!({"state": "s", "questions": {"q": {"type": "noul", "instructions": "?"}}})).send().await.unwrap();
        assert_eq!(r.status(), 503, "{path}");
        assert!(r.headers().contains_key("retry-after"));
        let rid = r.headers()["x-request-id"].to_str().unwrap().to_string();
        let v: Value = r.json().await.unwrap();
        assert_eq!(v["error"]["type"], "server_error");
        assert_eq!(v["error"]["code"], "backend_unavailable");
        let row = env.row(&rid).await;
        assert_eq!((row["status"].clone(), row["backend"].clone()), (json!(503), json!("decide")));
        assert!(row["error"].as_str().unwrap().contains("not available"));
    }
}

#[tokio::test]
async fn priority_queue_order_full_and_disconnect() {
    let env = setup("").await;
    let send = |tag: &str, prio: &str| {
        env.post("/v1/chat/completions", json!({"model": "chat", "messages": [{"role": "user", "content": tag}]}))
            .header("x-stack-priority", prio)
            .send()
    };
    // occupy the single slot
    let first = tokio::spawn(send("hold-0", "interactive"));
    while env.mock.seen.lock().unwrap().is_empty() {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    // a waiter that disconnects while queued must leave the queue
    let gone = tokio::spawn(send("gone", "interactive"));
    while env.queued("mock").await < 1 {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    gone.abort();
    let mut left = false;
    for _ in 0..200 {
        if env.queued("mock").await == 0 {
            left = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(left, "disconnected client still queued");

    let b1 = tokio::spawn(send("hold-b1", "background"));
    while env.queued("mock").await < 1 {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let i1 = tokio::spawn(send("hold-i1", "interactive"));
    while env.queued("mock").await < 2 {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let b2 = tokio::spawn(send("hold-b2", "background"));
    while env.queued("mock").await < 3 {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    // max_queue = 3 -> the next one is rejected
    let r = send("over", "interactive").await.unwrap();
    assert_eq!(r.status(), 503);
    assert_eq!(r.headers()["retry-after"], "5");
    let v: Value = r.json().await.unwrap();
    assert_eq!(v["error"]["code"], "backend_busy");

    env.mock.gate.add_permits(4);
    for t in [first, b1, i1, b2] {
        assert_eq!(t.await.unwrap().unwrap().status(), 200);
    }
    let order: Vec<String> = env
        .mock
        .seen
        .lock()
        .unwrap()
        .iter()
        .map(|s| s.2["messages"][0]["content"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(order, vec!["hold-0", "hold-i1", "hold-b1", "hold-b2"]);
}

#[tokio::test]
async fn queue_timeout_returns_503() {
    let env = setup("").await;
    // separate backend section can't be added twice; use the mock (timeout 5s) with a held request
    let first = tokio::spawn(
        env.post("/v1/chat/completions", json!({"model": "chat", "messages": [{"role": "user", "content": "hold"}]})).send(),
    );
    while env.mock.seen.lock().unwrap().is_empty() {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let started = std::time::Instant::now();
    let r = env
        .post("/v1/chat/completions", json!({"model": "chat", "messages": [{"role": "user", "content": "late"}]}))
        .header("x-stack-priority", "background")
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 503);
    assert_eq!(r.headers()["retry-after"], "10");
    assert!(started.elapsed() >= Duration::from_secs(4));
    let rid = r.headers()["x-request-id"].to_str().unwrap().to_string();
    let row = env.row(&rid).await;
    assert!(row["queue_ms"].as_i64().unwrap() >= 4000);
    env.mock.gate.add_permits(1);
    assert_eq!(first.await.unwrap().unwrap().status(), 200);
}

#[tokio::test]
async fn client_disconnect_mid_stream_frees_slot() {
    let env = setup("").await;
    let resp = env
        .post("/v1/chat/completions", json!({"model": "chat", "stream": true, "messages": [{"role": "user", "content": "x"}]}))
        .header("x-request-id", "req-cut")
        .send()
        .await
        .unwrap();
    let mut body = resp.bytes_stream();
    let _ = body.next().await;
    drop(body);
    let row = env.row("req-cut").await;
    assert_eq!(row["status"], 499);
    // slot is free again: a non-stream request goes through immediately
    let r = env
        .post("/v1/chat/completions", json!({"model": "chat", "messages": [{"role": "user", "content": "y"}]}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
}
