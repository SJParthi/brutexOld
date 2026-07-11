//! The verbatim reverse proxy — EVERY path brutexd does not serve natively
//! is forwarded unchanged to the FastAPI job host on the private loopback
//! port.
//!
//! Transparency laws (each one pinned by the parity gate,
//! `tests/integration/test_sidecar_parity_static.py`, coordinator ruling
//! 2026-07-11):
//!
//! * method, path, query string (encoded characters INCLUDED) and trailing
//!   slashes pass through byte-verbatim — the original `path_and_query` is
//!   reused, never re-encoded or normalized (axum performs no trailing-slash
//!   redirect, and the fallback receives the URI as received);
//! * ALL request/response headers pass through EXCEPT the RFC 7230 §6.1
//!   hop-by-hop set (see [`HOP_BY_HOP`]) plus any header NAMED by the
//!   `Connection` header — those describe the individual TCP hop, not the
//!   end-to-end message, so forwarding them would corrupt connection
//!   management (e.g. an upstream `keep-alive` timeout applied to the
//!   client hop) or double-apply transfer framing;
//! * the `Host` header is preserved verbatim (transparent proxy — Starlette
//!   builds redirect `Location`s from it, so rewriting Host would leak the
//!   private upstream port into redirects);
//! * redirects are NEVER followed — the 3xx + `Location` pass through
//!   (hyper's client does not follow redirects; there is no redirect layer);
//! * streamed/chunked bodies stream through without buffering; brutexd
//!   never fabricates a `content-length` for an unsized upstream body
//!   (`transfer-encoding` is hop-by-hop and re-derived per hop, which is
//!   the RFC-correct framing — the OBSERVABLE payload is unchanged);
//! * an unreachable upstream is a plain-body **502** ([`BAD_GATEWAY_BODY`]),
//!   never a panic and never a hang. (The wave-16 "job host restarting"
//!   503 degrade class is a later, richer refinement — roadmap row 16.)

use std::collections::HashSet;

use axum::body::Body;
use axum::extract::State;
use axum::http::uri::PathAndQuery;
use axum::http::{HeaderMap, HeaderName, Request, Response, StatusCode, Uri, header};
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::rt::TokioExecutor;

use crate::AppState;

/// Pooled hyper client type used for all upstream requests.
pub type ProxyClient = Client<HttpConnector, Body>;

/// Build the one pooled upstream client (loopback HTTP/1.1).
pub fn build_client() -> ProxyClient {
    Client::builder(TokioExecutor::new()).build(HttpConnector::new())
}

/// RFC 7230 §6.1 hop-by-hop headers — stripped in BOTH directions.
///
/// `connection` + everything it names, `keep-alive`, `proxy-authenticate`,
/// `proxy-authorization`, `te`, `trailer`, `transfer-encoding`, `upgrade`.
/// `transfer-encoding` in particular is per-hop framing: hyper de-chunks the
/// upstream body and re-frames the client hop itself; forwarding the header
/// literally would double-declare the framing.
pub const HOP_BY_HOP: [&str; 8] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

/// The pinned plain body served when the upstream is unreachable.
pub const BAD_GATEWAY_BODY: &str =
    "brutexd: upstream unreachable (the python job host may be down or restarting)\n";

/// Header names listed in the `Connection` header (lowercased).
fn connection_named(headers: &HeaderMap) -> HashSet<String> {
    let mut named = HashSet::new();
    for value in headers.get_all(header::CONNECTION) {
        if let Ok(text) = value.to_str() {
            for token in text.split(',') {
                let token = token.trim().to_ascii_lowercase();
                if !token.is_empty() {
                    named.insert(token);
                }
            }
        }
    }
    named
}

/// Remove the hop-by-hop set (+ Connection-named headers) in place.
pub fn strip_hop_by_hop(headers: &mut HeaderMap) {
    let named = connection_named(headers);
    let doomed: Vec<HeaderName> = headers
        .keys()
        .filter(|name| {
            let lower = name.as_str(); // HeaderName is already lowercase
            HOP_BY_HOP.contains(&lower) || named.contains(lower)
        })
        .cloned()
        .collect();
    for name in doomed {
        headers.remove(&name);
    }
}

fn bad_gateway(detail: &str) -> Response<Body> {
    tracing::warn!(detail, "brutexd.proxy.upstream_error");
    let mut resp = Response::new(Body::from(BAD_GATEWAY_BODY));
    *resp.status_mut() = StatusCode::BAD_GATEWAY;
    resp.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    resp
}

/// Forward one request verbatim to the upstream job host.
pub async fn forward(State(state): State<AppState>, mut req: Request<Body>) -> Response<Body> {
    // Reuse the ORIGINAL path+query bytes — no re-encoding, no normalization.
    let path_and_query: PathAndQuery = match req.uri().path_and_query() {
        Some(pq) => pq.clone(),
        None => PathAndQuery::from_static("/"),
    };
    let uri = match Uri::builder()
        .scheme("http")
        .authority(format!("127.0.0.1:{}", state.upstream_port))
        .path_and_query(path_and_query)
        .build()
    {
        Ok(uri) => uri,
        Err(err) => return bad_gateway(&format!("uri rebuild failed: {err}")),
    };
    *req.uri_mut() = uri;
    // Host is preserved verbatim (never rewritten to the private authority) —
    // Starlette builds redirect Locations from it, so rewriting would leak the
    // upstream port. NIT #4 (theoretical): a client that sends NO Host header
    // is impossible over conforming HTTP/1.1 (RFC 7230 §5.4 mandates it), and
    // axum would already have rejected such a request; if one ever arrived,
    // hyper's client derives Host from the upstream authority (127.0.0.1:port)
    // — a benign default that never reaches an end-to-end redirect, since the
    // absent-Host case carries no operator-facing Host to preserve. Not
    // defended against further by design (do-not-over-engineer ruling).
    strip_hop_by_hop(req.headers_mut());
    match state.client.request(req).await {
        Ok(resp) => {
            let (mut parts, body) = resp.into_parts();
            strip_hop_by_hop(&mut parts.headers);
            Response::from_parts(parts, Body::new(body))
        }
        Err(err) => bad_gateway(&err.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    #[test]
    fn strips_the_rfc7230_hop_by_hop_set() {
        let mut headers = HeaderMap::new();
        for name in HOP_BY_HOP {
            headers.insert(HeaderName::from_static(name), HeaderValue::from_static("x"));
        }
        headers.insert("x-end-to-end", HeaderValue::from_static("keep"));
        headers.insert(header::HOST, HeaderValue::from_static("127.0.0.1:9080"));
        strip_hop_by_hop(&mut headers);
        assert_eq!(headers.len(), 2);
        assert!(headers.contains_key("x-end-to-end"));
        assert!(headers.contains_key(header::HOST));
    }

    #[test]
    fn strips_connection_named_headers() {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::CONNECTION,
            HeaderValue::from_static("close, X-Per-Hop"),
        );
        headers.insert("x-per-hop", HeaderValue::from_static("hop"));
        headers.insert("x-keep", HeaderValue::from_static("keep"));
        strip_hop_by_hop(&mut headers);
        assert!(!headers.contains_key("x-per-hop"));
        assert!(!headers.contains_key(header::CONNECTION));
        assert!(headers.contains_key("x-keep"));
    }

    #[test]
    fn connection_named_handles_junk_values() {
        let mut headers = HeaderMap::new();
        headers.insert(header::CONNECTION, HeaderValue::from_static(" , ,close"));
        let named = connection_named(&headers);
        assert_eq!(named.len(), 1);
        assert!(named.contains("close"));
    }

    #[test]
    fn bad_gateway_shape_is_pinned() {
        let resp = bad_gateway("test");
        assert_eq!(resp.status(), StatusCode::BAD_GATEWAY);
        assert_eq!(
            resp.headers()
                .get(header::CONTENT_TYPE)
                .map(|v| v.as_bytes()),
            Some("text/plain; charset=utf-8".as_bytes())
        );
    }
}
