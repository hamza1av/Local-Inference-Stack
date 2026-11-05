//! stack-gateway: OpenAI-compatible gateway of the local inference stack.

pub mod app;
pub mod config;
pub mod db;
pub mod otlp;
pub mod queue;

pub use app::{router, AppState};
pub use config::Config;

/// Serve until the future `shutdown` resolves.
pub async fn serve(
    listener: tokio::net::TcpListener,
    state: std::sync::Arc<AppState>,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> std::io::Result<()> {
    axum::serve(listener, router(state)).with_graceful_shutdown(shutdown).await
}
