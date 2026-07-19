//! `/health` — pinned literal bytes, served natively by brutexd.
//!
//! DESIGN FACT (verified 2026-07-11): the FastAPI dashboard serves NO health
//! route today (`grep`-verified over `brutex/dashboard/*.py` — no `/health`,
//! `/healthz`, or `/api/v1/health`). This route is therefore NEW and exists
//! ONLY when the Rust front is enabled (`BRUTEX_RUST_FRONT=on`); with the
//! flag OFF (the default) no process serves `/health` and behaviour is
//! byte-identical to before this crate existed.
//!
//! The body is a PINNED LITERAL — no JSON serializer dependency, exactly as
//! the roadmap §3 spec demands (the `pyjson` CPython-compatible serializer
//! is deliberately deferred to wave 2a). The parity gate pins these bytes
//! (`tests/integration/test_sidecar_parity_static.py`), and the crate
//! integration test pins them again Rust-side.

use axum::http::header;
use axum::response::IntoResponse;

/// The pinned `/health` body. Changing this is a contract change — both the
/// Rust and Python parity pins fail if it drifts.
pub const HEALTH_BODY: &str = "brutexd ok\n";

/// Serve the pinned health literal (200, `text/plain; charset=utf-8`).
pub async fn health() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "text/plain; charset=utf-8")],
        HEALTH_BODY,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::StatusCode;
    use axum::response::IntoResponse;

    #[tokio::test]
    async fn health_is_pinned_literal() {
        let resp = health().await.into_response();
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(
            resp.headers()
                .get(header::CONTENT_TYPE)
                .map(|v| v.as_bytes()),
            Some("text/plain; charset=utf-8".as_bytes())
        );
        let body = axum::body::to_bytes(resp.into_body(), 1024).await.unwrap();
        assert_eq!(&body[..], HEALTH_BODY.as_bytes());
    }
}
