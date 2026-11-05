//! Configuration: environment (`.env`) + `gateway.toml` routes.

use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    time::Duration,
};

use serde::Deserialize;

/// The default route file, compiled in so the binary works without one on disk.
pub const DEFAULT_TOML: &str = include_str!("../gateway.toml");

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StreamUsage {
    /// Add `stream_options.include_usage = true` when the client did not set it.
    Inject,
    /// Backend sends usage in its final chunk by itself (uzu).
    Native,
    /// Do nothing.
    None,
}

#[derive(Debug, Clone)]
pub struct Backend {
    pub name: String,
    pub url: String,
    pub models: Vec<String>,
    pub paths: Vec<String>,
    pub rewrite_model: Option<String>,
    pub max_concurrency: usize,
    pub max_queue: usize,
    pub queue_timeout: Duration,
    pub stream_usage: StreamUsage,
    pub health: String,
    pub api_key: Option<String>,
    pub timeout: Duration,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub bind: String,
    /// key -> client name
    pub keys: HashMap<String, String>,
    pub db_path: Option<PathBuf>,
    pub otlp_endpoint: Option<String>,
    pub langfuse_public_key: String,
    pub langfuse_secret_key: String,
    pub capture_content: bool,
    pub max_content_bytes: usize,
    pub backends: Vec<Backend>,
}

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct RawServer {
    bind: Option<String>,
    capture_content: Option<bool>,
    max_content_bytes: Option<usize>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawBackend {
    url: String,
    #[serde(default)]
    models: Vec<String>,
    #[serde(default)]
    paths: Vec<String>,
    rewrite_model: Option<String>,
    max_concurrency: Option<usize>,
    max_queue: Option<usize>,
    queue_timeout_secs: Option<f64>,
    stream_usage: Option<StreamUsage>,
    health: Option<String>,
    api_key: Option<String>,
    timeout_secs: Option<f64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawConfig {
    #[serde(default)]
    server: RawServer,
    #[serde(default)]
    backends: toml::map::Map<String, toml::Value>,
}

/// Expand `${VAR}` and `${VAR:-default}`; unset variables become "" (or the default).
pub fn expand_env(s: &str, env: &dyn Fn(&str) -> Option<String>) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(start) = rest.find("${") {
        out.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        let Some(end) = after.find('}') else {
            out.push_str(&rest[start..]);
            return out;
        };
        let expr = &after[..end];
        let (name, default) = match expr.split_once(":-") {
            Some((n, d)) => (n, Some(d)),
            None => (expr, None),
        };
        match env(name).filter(|v| !v.is_empty()) {
            Some(v) => out.push_str(&v),
            None => out.push_str(default.unwrap_or("")),
        }
        rest = &after[end + 1..];
    }
    out.push_str(rest);
    out
}

fn expand_value(v: &mut toml::Value, env: &dyn Fn(&str) -> Option<String>) {
    match v {
        toml::Value::String(s) => *s = expand_env(s, env),
        toml::Value::Array(a) => a.iter_mut().for_each(|x| expand_value(x, env)),
        toml::Value::Table(t) => t.iter_mut().for_each(|(_, x)| expand_value(x, env)),
        _ => {}
    }
}

/// `name:key,name:key` -> key -> name. Entries without a name get the name "default".
pub fn parse_keys(s: &str) -> HashMap<String, String> {
    s.split(',')
        .map(str::trim)
        .filter(|e| !e.is_empty())
        .filter_map(|e| match e.split_once(':') {
            Some((n, k)) if !k.trim().is_empty() => Some((k.trim().to_string(), n.trim().to_string())),
            Some(_) => None,
            None => Some((e.to_string(), "default".to_string())),
        })
        .collect()
}

fn truthy(s: &str) -> bool {
    !matches!(s.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off" | "")
}

impl Config {
    /// Build from TOML text and an environment lookup (pure; used by tests).
    pub fn from_parts(toml_text: &str, env: &dyn Fn(&str) -> Option<String>) -> Result<Config, String> {
        let mut value: toml::Value = toml::from_str(toml_text).map_err(|e| format!("gateway.toml: {e}"))?;
        expand_value(&mut value, env);
        let raw: RawConfig = value.try_into().map_err(|e| format!("gateway.toml: {e}"))?;

        let mut backends = Vec::new();
        let mut seen_models: HashMap<String, String> = HashMap::new();
        for (name, v) in raw.backends {
            let b: RawBackend = v.try_into().map_err(|e| format!("backend {name}: {e}"))?;
            let url = b.url.trim_end_matches('/').to_string();
            if url.is_empty() {
                return Err(format!("backend {name}: url is empty"));
            }
            let mut models = Vec::new();
            for m in b.models.into_iter().map(|m| m.trim().to_string()).filter(|m| !m.is_empty()) {
                if models.contains(&m) {
                    continue;
                }
                if let Some(other) = seen_models.get(&m) {
                    tracing::warn!("model alias {m:?} of backend {name} already routed to {other}; ignored");
                    continue;
                }
                seen_models.insert(m.clone(), name.clone());
                models.push(m);
            }
            backends.push(Backend {
                url,
                models,
                paths: b.paths,
                rewrite_model: b.rewrite_model.filter(|m| !m.trim().is_empty()),
                max_concurrency: b.max_concurrency.unwrap_or(1).max(1),
                max_queue: b.max_queue.unwrap_or(32),
                queue_timeout: Duration::from_secs_f64(b.queue_timeout_secs.unwrap_or(120.0).max(0.0)),
                stream_usage: b.stream_usage.unwrap_or(StreamUsage::Inject),
                health: b.health.unwrap_or_else(|| "/v1/models".into()),
                api_key: b.api_key.filter(|k| !k.is_empty()),
                timeout: Duration::from_secs_f64(b.timeout_secs.unwrap_or(600.0).max(1.0)),
                name,
            });
        }

        let bind = env("GATEWAY_BIND")
            .filter(|s| !s.is_empty())
            .or(raw.server.bind.filter(|s| !s.is_empty()))
            .unwrap_or_else(|| "127.0.0.1:8088".into());
        let capture_content = env("GATEWAY_CAPTURE_CONTENT")
            .map(|v| truthy(&v))
            .or(raw.server.capture_content)
            .unwrap_or(true);

        Ok(Config {
            bind,
            keys: parse_keys(&env("GATEWAY_KEYS").unwrap_or_default()),
            db_path: env("STACK_DB").filter(|s| !s.is_empty()).map(PathBuf::from),
            otlp_endpoint: env("STACK_OTLP_ENDPOINT")
                .filter(|s| !s.is_empty())
                .map(|s| s.trim_end_matches('/').to_string()),
            langfuse_public_key: env("LANGFUSE_PUBLIC_KEY").unwrap_or_default(),
            langfuse_secret_key: env("LANGFUSE_SECRET_KEY").unwrap_or_default(),
            capture_content,
            max_content_bytes: raw.server.max_content_bytes.unwrap_or(64 * 1024),
            backends,
        })
    }

    /// Load `.env` and the route file from disk. Precedence for `.env`:
    /// `$STACK_ROOT/.env`, then `../.env`, then `./.env` (existing env vars always win).
    /// Route file: `$GATEWAY_CONFIG`, `$STACK_ROOT/gateway/gateway.toml`, `./gateway.toml`,
    /// otherwise the compiled-in default.
    pub fn load() -> Result<Config, String> {
        let root = std::env::var("STACK_ROOT").ok().filter(|s| !s.is_empty()).map(PathBuf::from);
        let env_candidates: Vec<PathBuf> = root
            .iter()
            .map(|r| r.join(".env"))
            .chain([PathBuf::from("../.env"), PathBuf::from(".env")])
            .collect();
        if let Some(p) = env_candidates.iter().find(|p| p.is_file()) {
            if let Err(e) = dotenvy::from_path(p) {
                return Err(format!("{}: {e}", p.display()));
            }
        }
        let toml_path = std::env::var("GATEWAY_CONFIG")
            .ok()
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .or_else(|| root.as_ref().map(|r| r.join("gateway/gateway.toml")).filter(|p| p.is_file()))
            .or_else(|| Some(PathBuf::from("gateway.toml")).filter(|p| p.is_file()));
        let text = match &toml_path {
            Some(p) => std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))?,
            None => DEFAULT_TOML.to_string(),
        };
        tracing::info!(
            "routes from {}",
            toml_path.as_deref().map(Path::display).map(|d| d.to_string()).unwrap_or("built-in default".into())
        );
        Config::from_parts(&text, &|k| std::env::var(k).ok())
    }

    pub fn backend_for_model(&self, model: &str) -> Option<&Backend> {
        self.backends.iter().find(|b| b.models.iter().any(|m| m == model))
    }

    pub fn backend_for_path(&self, path: &str) -> Option<&Backend> {
        self.backends.iter().find(|b| b.paths.iter().any(|p| p == path))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        move |k| m.get(k).cloned()
    }

    #[test]
    fn expands_env_with_defaults() {
        let env = env_of(&[("A", "x"), ("EMPTY", "")]);
        assert_eq!(expand_env("${A}-${B:-d}-${EMPTY:-e}-${NONE}", &env), "x-d-e-");
        assert_eq!(expand_env("no vars", &env), "no vars");
        assert_eq!(expand_env("broken ${A", &env), "broken ${A");
    }

    #[test]
    fn parses_keys() {
        let k = parse_keys("owui:abc, me:def ,bare,bad:");
        assert_eq!(k.get("abc").unwrap(), "owui");
        assert_eq!(k.get("def").unwrap(), "me");
        assert_eq!(k.get("bare").unwrap(), "default");
        assert_eq!(k.len(), 3);
    }

    #[test]
    fn default_routes() {
        let env = env_of(&[
            ("STACK_CHAT_MODEL", "org/Chat-9B"),
            ("STACK_TASK_MODEL", "org/Task-1B"),
            ("STACK_EMBED_MODEL", "embeddinggemma"),
            ("UZU_CHAT_PORT", "9000"),
            ("OLLAMA_URL", "http://127.0.0.1:11434/"),
            ("GATEWAY_KEYS", "me:k1"),
        ]);
        let c = Config::from_parts(DEFAULT_TOML, &env).unwrap();
        let chat = c.backend_for_model("chat").unwrap();
        assert_eq!(chat.name, "uzu-chat");
        assert_eq!(chat.url, "http://127.0.0.1:9000");
        assert_eq!(chat.max_concurrency, 1);
        assert_eq!(chat.stream_usage, StreamUsage::Native);
        assert_eq!(c.backend_for_model("org/Chat-9B").unwrap().name, "uzu-chat");
        assert_eq!(c.backend_for_model("task").unwrap().url, "http://127.0.0.1:8001");
        assert_eq!(c.backend_for_model("org/Task-1B").unwrap().name, "uzu-task");
        let agent = c.backend_for_model("agent").unwrap();
        assert_eq!((agent.url.as_str(), agent.max_concurrency), ("http://127.0.0.1:8092", 4));
        let emb = c.backend_for_model("embed").unwrap();
        assert_eq!(emb.url, "http://127.0.0.1:11434");
        assert_eq!(emb.rewrite_model.as_deref(), Some("embeddinggemma"));
        assert_eq!(emb.models, vec!["embed", "embeddinggemma"]); // deduped
        assert_eq!(c.backend_for_path("/v1/decisions").unwrap().name, "uzu-decide");
        assert_eq!(c.backend_for_path("/v1/systemone").unwrap().url, "http://127.0.0.1:8002");
        assert!(c.backend_for_model("nope").is_none());
        assert_eq!(c.bind, "127.0.0.1:8088");
        assert!(c.capture_content);
        assert_eq!(c.keys.get("k1").unwrap(), "me");
    }

    #[test]
    fn empty_model_alias_is_ignored() {
        let c = Config::from_parts(DEFAULT_TOML, &env_of(&[])).unwrap();
        assert_eq!(c.backend_for_model("chat").unwrap().models, vec!["chat"]);
        assert!(c.backend_for_model("").is_none());
    }

    #[test]
    fn duplicate_alias_first_wins() {
        let toml = r#"
            [backends.a]
            url = "http://a"
            models = ["m"]
            [backends.b]
            url = "http://b"
            models = ["m", "n"]
        "#;
        let c = Config::from_parts(toml, &env_of(&[])).unwrap();
        assert_eq!(c.backend_for_model("m").unwrap().name, "a");
        assert_eq!(c.backend_for_model("n").unwrap().name, "b");
    }

    #[test]
    fn new_backend_is_a_few_lines() {
        let toml = r#"
            [backends.ollama-chat]
            url = "${OLLAMA_URL}"
            models = ["small"]
            rewrite_model = "lfm3:1.7b"
            max_concurrency = 2
        "#;
        let c = Config::from_parts(toml, &env_of(&[("OLLAMA_URL", "http://o:1"), ("GATEWAY_CAPTURE_CONTENT", "false")]))
            .unwrap();
        let b = c.backend_for_model("small").unwrap();
        assert_eq!(b.url, "http://o:1");
        assert_eq!(b.stream_usage, StreamUsage::Inject);
        assert_eq!(b.max_queue, 32);
        assert!(!c.capture_content);
    }

    #[test]
    fn rejects_unknown_keys() {
        assert!(Config::from_parts("[backends.a]\nurl='x'\nmodle=['a']", &env_of(&[])).is_err());
    }
}
