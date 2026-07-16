//! # brutexd — the BruteX Rust HTTP front (SERVING track, Wave 1)
//!
//! The strangler-front opener of the Rust-uniformity program
//! (roadmap_final.md §3): brutexd binds the OPERATOR port (127.0.0.1-only),
//! serves two things natively —
//!
//! * `/static/*` — the `brutex/dashboard/static/` tree (byte-identical
//!   bodies; content-type mirrors what Starlette's `StaticFiles` emits for
//!   the extensions present in that tree; path-traversal + symlink refusal),
//! * `/health` — a pinned literal body (NO JSON serializer dependency —
//!   `pyjson` is deliberately deferred to wave 2a),
//!
//! and reverse-proxies EVERY other path verbatim (method, headers, body,
//! query string, trailing slashes — hop-by-hop headers stripped per
//! RFC 7230 §6.1, nothing else touched) to the FastAPI job host on a
//! private loopback port.
//!
//! ## Honesty header
//!
//! This is a **uniformity program, not a performance program** — no latency
//! win is claimed or measured here (roadmap §0). The parity gate
//! (`tests/integration/test_sidecar_parity_static.py`) is the deliverable.
//!
//! ## Never-500 discipline
//!
//! No `unwrap()`/`expect()` on request paths (denied crate-wide below);
//! proxy failures map to a plain-body 502, handler panics are converted to
//! a logged 500-class degrade by `CatchPanicLayer` (never process death).

#![cfg_attr(not(test), deny(clippy::unwrap_used))]
#![cfg_attr(not(test), deny(clippy::expect_used))]

pub mod config;
pub mod health;
pub mod proxy;
pub mod static_files;

use axum::Router;
use axum::body::Body;
use axum::http::{Response, StatusCode, header};
use axum::routing::get;
use tower_http::catch_panic::CatchPanicLayer;

/// Shared per-process state: the validated config + the one upstream client.
#[derive(Clone)]
pub struct AppState {
    /// Absolute path of the static tree served under `/static/*`.
    pub static_dir: std::path::PathBuf,
    /// Loopback port the FastAPI job host listens on.
    pub upstream_port: u16,
    /// Pooled hyper client used by the proxy fallback.
    pub client: proxy::ProxyClient,
}

/// The 500-class body emitted when a handler panics (panic-to-degrade law).
pub const PANIC_BODY: &str = "brutexd: internal error (handler panic — logged)\n";

fn panic_response(err: Box<dyn std::any::Any + Send + 'static>) -> Response<Body> {
    let detail = err
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| err.downcast_ref::<&str>().copied())
        .unwrap_or("<non-string panic payload>");
    tracing::error!(panic = detail, "brutexd.handler_panic");
    let mut resp = Response::new(Body::from(PANIC_BODY));
    *resp.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
    resp.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    resp
}

/// Build the brutexd router: `/health` + `/static/{*path}` native, everything
/// else falls through to the verbatim reverse proxy.
///
/// Deliberately NO route for bare `/static` or `/static/` — those pass
/// through to the proxy so FastAPI keeps answering them exactly as today
/// (maximal transparency; the front claims only what it serves).
pub fn build_router(cfg: &config::Config) -> Router {
    let state = AppState {
        static_dir: cfg.static_dir.clone(),
        upstream_port: cfg.upstream_port,
        client: proxy::build_client(),
    };
    Router::new()
        .route("/health", get(health::health))
        .route("/static/{*path}", get(static_files::serve))
        .fallback(proxy::forward)
        .layer(CatchPanicLayer::custom(panic_response))
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn panic_response_is_500_plain_text() {
        let resp = panic_response(Box::new("boom".to_owned()));
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(
            resp.headers()
                .get(header::CONTENT_TYPE)
                .map(|v| v.as_bytes()),
            Some("text/plain; charset=utf-8".as_bytes())
        );
    }

    #[test]
    fn panic_response_handles_str_and_opaque_payloads() {
        let resp = panic_response(Box::new("static str panic"));
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
        let resp = panic_response(Box::new(42_u8));
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }
}
