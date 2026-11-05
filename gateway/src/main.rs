use std::net::SocketAddr;

use stack_gateway::{AppState, Config};

fn main() {
    let level = std::env::var("RUST_LOG")
        .ok()
        .and_then(|l| l.parse::<tracing::Level>().ok())
        .unwrap_or(tracing::Level::INFO);
    tracing_subscriber::fmt()
        .with_max_level(level)
        .with_target(false)
        .with_ansi(std::env::var_os("NO_COLOR").is_none() && std::io::IsTerminal::is_terminal(&std::io::stderr()))
        .with_writer(std::io::stderr)
        .init();

    if std::env::args().any(|a| a == "--version" || a == "-V") {
        println!("stack-gateway {}", env!("CARGO_PKG_VERSION"));
        return;
    }

    let cfg = match Config::load() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("config error: {e}");
            std::process::exit(2);
        }
    };
    if std::env::args().any(|a| a == "--check") {
        for b in &cfg.backends {
            println!(
                "{:<14} {:<28} models={:?} paths={:?} concurrency={}",
                b.name, b.url, b.models, b.paths, b.max_concurrency
            );
        }
        return;
    }

    let addr: SocketAddr = match cfg.bind.parse() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("invalid GATEWAY_BIND {:?}: {e}", cfg.bind);
            std::process::exit(2);
        }
    };
    if cfg.keys.is_empty() {
        if !addr.ip().is_loopback() {
            eprintln!("refusing to listen on {addr} without GATEWAY_KEYS");
            std::process::exit(2);
        }
        tracing::warn!("GATEWAY_KEYS is empty: authentication disabled (loopback only)");
    }

    let threads = std::env::var("GATEWAY_THREADS").ok().and_then(|t| t.parse().ok()).unwrap_or(2usize);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(threads.max(1))
        .thread_stack_size(512 * 1024)
        .enable_all()
        .build()
        .expect("tokio runtime");

    rt.block_on(async move {
        let listener = match tokio::net::TcpListener::bind(addr).await {
            Ok(l) => l,
            Err(e) => {
                eprintln!("bind {addr}: {e}");
                std::process::exit(1);
            }
        };
        tracing::info!(
            "stack-gateway {} on {addr}: {} backends, {} clients, db={}, traces={}",
            env!("CARGO_PKG_VERSION"),
            cfg.backends.len(),
            cfg.keys.len(),
            cfg.db_path.as_deref().map(|p| p.display().to_string()).unwrap_or("off".into()),
            cfg.otlp_endpoint.as_deref().unwrap_or("off"),
        );
        let state = AppState::start(cfg);
        let shutdown = async {
            let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal");
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {},
                _ = term.recv() => {},
            }
            tracing::info!("shutting down");
        };
        if let Err(e) = stack_gateway::serve(listener, state, shutdown).await {
            eprintln!("server error: {e}");
            std::process::exit(1);
        }
    });
}
