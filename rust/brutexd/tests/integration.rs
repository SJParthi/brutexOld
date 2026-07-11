//! brutexd integration tests — the Rust-side twin of the Python parity gate.
//!
//! Drives the REAL router (`brutexd::build_router`) served on an ephemeral
//! loopback listener, against a REAL in-process upstream (a tiny axum app
//! standing in for the FastAPI job host). Everything the Python parity
//! suite pins over the wire is pinned here too, so the crate gates itself
//! even where the Python integration test is skipped (no cargo in env).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::net::SocketAddr;
use std::path::PathBuf;

use axum::Router;
use axum::body::Body;
use axum::extract::Request;
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Redirect, Response};
use axum::routing::{any, get};
use http_body_util::BodyExt;
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::rt::TokioExecutor;

type TestClient = Client<HttpConnector, Body>;

/// The upstream stand-in for the FastAPI job host.
fn upstream_router() -> Router {
    async fn echo(req: Request) -> Response {
        let (parts, body) = req.into_parts();
        let body = body.collect().await.unwrap().to_bytes();
        let host = parts
            .headers
            .get(header::HOST)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("<none>")
            .to_owned();
        let xhop = parts.headers.contains_key("x-per-hop");
        let te = parts.headers.contains_key(header::TRANSFER_ENCODING);
        let text = format!(
            "method={} uri={} host={} x-per-hop={} te={} body={}",
            parts.method,
            parts.uri,
            host,
            xhop,
            te,
            String::from_utf8_lossy(&body),
        );
        ([(header::CONTENT_TYPE, "text/plain; charset=utf-8")], text).into_response()
    }

    async fn stream() -> Response {
        // An UNSIZED (chunked) body: the proxy must stream it through
        // without fabricating a content-length.
        let chunks: Vec<Result<_, std::io::Error>> = vec![
            Ok::<_, std::io::Error>(bytes::Bytes::from_static(b"chunk-one|")),
            Ok(bytes::Bytes::from_static(b"chunk-two|")),
            Ok(bytes::Bytes::from_static(b"chunk-three")),
        ];
        let stream_body = Body::from_stream(tokio_stream::iter(chunks));
        Response::new(stream_body)
    }

    async fn error422() -> Response {
        (
            StatusCode::UNPROCESSABLE_ENTITY,
            [(header::CONTENT_TYPE, "application/json")],
            r#"{"detail":[{"loc":["query","x"],"msg":"field required"}]}"#,
        )
            .into_response()
    }

    Router::new()
        .route("/test/echo", any(echo))
        .route("/test/echo/", any(echo))
        .route(
            "/test/redirect",
            get(|| async { Redirect::temporary("/test/echo") }),
        )
        .route("/test/stream", get(stream))
        .route("/test/error422", get(error422))
}

struct Harness {
    front_addr: SocketAddr,
    upstream_addr: SocketAddr,
    static_dir: tempfile::TempDir,
    client: TestClient,
}

async fn spawn(router: Router) -> SocketAddr {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    addr
}

async fn harness() -> Harness {
    let upstream_addr = spawn(upstream_router()).await;

    let static_dir = tempfile::tempdir().unwrap();
    std::fs::write(
        static_dir.path().join("app.js"),
        b"console.log('parity');\n",
    )
    .unwrap();
    std::fs::write(static_dir.path().join("style.css"), b"body{color:red}\n").unwrap();
    // A secret OUTSIDE the tree + a symlink INSIDE pointing at it.
    let secret = static_dir.path().join("..").join("brutexd-secret.txt");
    std::fs::write(&secret, b"secret\n").unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&secret, static_dir.path().join("link.js")).unwrap();

    let cfg = brutexd::config::Config {
        listen: "127.0.0.1:0".parse().unwrap(),
        upstream_port: upstream_addr.port(),
        static_dir: static_dir.path().to_path_buf(),
    };
    let front_addr = spawn(brutexd::build_router(&cfg)).await;

    Harness {
        front_addr,
        upstream_addr,
        static_dir,
        client: Client::builder(TokioExecutor::new()).build(HttpConnector::new()),
    }
}

async fn get_raw(
    client: &TestClient,
    addr: SocketAddr,
    path_and_query: &str,
) -> (StatusCode, HeaderMap, bytes::Bytes) {
    request_raw(client, addr, "GET", path_and_query, None, &[]).await
}

async fn request_raw(
    client: &TestClient,
    addr: SocketAddr,
    method: &str,
    path_and_query: &str,
    body: Option<&[u8]>,
    extra_headers: &[(&str, &str)],
) -> (StatusCode, HeaderMap, bytes::Bytes) {
    let uri = format!("http://{addr}{path_and_query}");
    let mut builder = axum::http::Request::builder().method(method).uri(uri);
    for (name, value) in extra_headers {
        builder = builder.header(*name, *value);
    }
    let req = builder
        .body(body.map_or_else(Body::empty, |b| Body::from(b.to_vec())))
        .unwrap();
    let resp = client.request(req).await.unwrap();
    let (parts, body) = resp.into_parts();
    let bytes = body.collect().await.unwrap().to_bytes();
    (parts.status, parts.headers, bytes)
}

// ── native routes ────────────────────────────────────────────────────────

#[tokio::test]
async fn health_serves_the_pinned_literal() {
    let h = harness().await;
    let (status, headers, body) = get_raw(&h.client, h.front_addr, "/health").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(&body[..], brutexd::health::HEALTH_BODY.as_bytes());
    assert_eq!(
        headers.get(header::CONTENT_TYPE).map(|v| v.as_bytes()),
        Some("text/plain; charset=utf-8".as_bytes())
    );
}

#[tokio::test]
async fn static_file_bytes_and_content_type() {
    let h = harness().await;
    let (status, headers, body) = get_raw(&h.client, h.front_addr, "/static/app.js").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(&body[..], b"console.log('parity');\n");
    assert_eq!(
        headers.get(header::CONTENT_TYPE).map(|v| v.as_bytes()),
        Some("text/javascript; charset=utf-8".as_bytes())
    );
    assert_eq!(
        headers
            .get(header::CONTENT_LENGTH)
            .and_then(|v| v.to_str().ok()),
        Some("23")
    );
    assert!(headers.get(header::CACHE_CONTROL).is_none());

    let (status, headers, _) = get_raw(&h.client, h.front_addr, "/static/style.css").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers.get(header::CONTENT_TYPE).map(|v| v.as_bytes()),
        Some("text/css; charset=utf-8".as_bytes())
    );
}

#[tokio::test]
async fn static_missing_traversal_and_symlink_all_404() {
    let h = harness().await;
    for path in [
        "/static/absent.js",
        "/static/../Cargo.toml",
        "/static/%2e%2e/brutexd-secret.txt",
        "/static/link.js", // symlinked leaf — refused
    ] {
        let (status, _, body) = get_raw(&h.client, h.front_addr, path).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "path {path} must 404");
        assert!(
            !body.windows(6).any(|w| w == b"secret"),
            "no secret leak via {path}"
        );
    }
    // sanity: the symlink target really exists (the 404 is a REFUSAL).
    assert!(
        std::fs::symlink_metadata(h.static_dir.path().join("link.js"))
            .unwrap()
            .is_symlink()
    );
}

// ── proxy transparency ───────────────────────────────────────────────────

#[tokio::test]
async fn proxy_passes_method_query_host_and_body_verbatim() {
    let h = harness().await;
    let pq = "/test/echo?q=a%20b&x=1";
    let (direct_status, _, direct_body) = request_raw(
        &h.client,
        h.upstream_addr,
        "POST",
        pq,
        Some(b"payload-bytes"),
        &[("host", "operator.example:9080"), ("x-end-to-end", "keep")],
    )
    .await;
    let (front_status, front_headers, front_body) = request_raw(
        &h.client,
        h.front_addr,
        "POST",
        pq,
        Some(b"payload-bytes"),
        &[("host", "operator.example:9080"), ("x-end-to-end", "keep")],
    )
    .await;
    assert_eq!(front_status, direct_status);
    assert_eq!(
        front_body, direct_body,
        "proxied body must be byte-identical"
    );
    // The echo folds method/uri/host/body — equality proves ALL passed verbatim,
    // including the encoded query and the preserved Host header.
    let text = String::from_utf8_lossy(&front_body);
    assert!(text.contains("uri=/test/echo?q=a%20b&x=1"), "got: {text}");
    assert!(text.contains("host=operator.example:9080"), "got: {text}");
    assert!(text.contains("body=payload-bytes"), "got: {text}");
    // content-length passes through on a sized response.
    assert_eq!(
        front_headers
            .get(header::CONTENT_LENGTH)
            .and_then(|v| v.to_str().ok()),
        Some(direct_body.len().to_string().as_str())
    );
}

#[tokio::test]
async fn proxy_preserves_trailing_slash_verbatim() {
    let h = harness().await;
    let fixed_host: [(&str, &str); 1] = [("host", "operator.example:9080")];
    let (_, _, direct) = request_raw(
        &h.client,
        h.upstream_addr,
        "GET",
        "/test/echo/",
        None,
        &fixed_host,
    )
    .await;
    let (status, _, body) = request_raw(
        &h.client,
        h.front_addr,
        "GET",
        "/test/echo/",
        None,
        &fixed_host,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, direct);
    assert!(String::from_utf8_lossy(&body).contains("uri=/test/echo/ "));
}

#[tokio::test]
async fn proxy_strips_hop_by_hop_request_headers_but_keeps_end_to_end() {
    let h = harness().await;
    let (_, _, body) = request_raw(
        &h.client,
        h.front_addr,
        "GET",
        "/test/echo",
        None,
        &[("connection", "x-per-hop"), ("x-per-hop", "1")],
    )
    .await;
    let text = String::from_utf8_lossy(&body);
    assert!(
        text.contains("x-per-hop=false"),
        "hop header must be stripped: {text}"
    );
    assert!(
        text.contains("te=false"),
        "no transfer-encoding leaked: {text}"
    );
}

#[tokio::test]
async fn proxy_does_not_follow_redirects() {
    let h = harness().await;
    let (d_status, d_headers, _) = get_raw(&h.client, h.upstream_addr, "/test/redirect").await;
    let (status, headers, _) = get_raw(&h.client, h.front_addr, "/test/redirect").await;
    assert_eq!(status, StatusCode::TEMPORARY_REDIRECT);
    assert_eq!(status, d_status);
    assert_eq!(
        headers.get(header::LOCATION).map(|v| v.as_bytes()),
        d_headers.get(header::LOCATION).map(|v| v.as_bytes()),
    );
    assert_eq!(
        headers.get(header::LOCATION).map(|v| v.as_bytes()),
        Some("/test/echo".as_bytes())
    );
}

#[tokio::test]
async fn proxy_streams_chunked_bodies_without_fabricating_length() {
    let h = harness().await;
    let (d_status, d_headers, d_body) = get_raw(&h.client, h.upstream_addr, "/test/stream").await;
    let (status, headers, body) = get_raw(&h.client, h.front_addr, "/test/stream").await;
    assert_eq!(status, d_status);
    assert_eq!(body, d_body);
    assert_eq!(&body[..], b"chunk-one|chunk-two|chunk-three");
    // Neither hop declares a content-length for the unsized body.
    assert!(d_headers.get(header::CONTENT_LENGTH).is_none());
    assert!(
        headers.get(header::CONTENT_LENGTH).is_none(),
        "no fabricated content-length"
    );
}

#[tokio::test]
async fn proxy_passes_4xx_status_and_json_error_bodies() {
    let h = harness().await;
    // 422 with a JSON body.
    let (d_status, d_headers, d_body) = get_raw(&h.client, h.upstream_addr, "/test/error422").await;
    let (status, headers, body) = get_raw(&h.client, h.front_addr, "/test/error422").await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(status, d_status);
    assert_eq!(body, d_body);
    assert_eq!(
        headers.get(header::CONTENT_TYPE).map(|v| v.as_bytes()),
        d_headers.get(header::CONTENT_TYPE).map(|v| v.as_bytes()),
    );
    // 404 from the upstream router passes through untouched.
    let (d_status, _, d_body) = get_raw(&h.client, h.upstream_addr, "/no/such/route").await;
    let (status, _, body) = get_raw(&h.client, h.front_addr, "/no/such/route").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(status, d_status);
    assert_eq!(body, d_body);
}

#[tokio::test]
async fn unreachable_upstream_is_a_plain_502_never_a_panic() {
    // Point a front at a port with nothing listening.
    let static_dir = tempfile::tempdir().unwrap();
    let dead_port = {
        // Bind + drop to find a port that is (very likely) closed.
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let p = l.local_addr().unwrap().port();
        drop(l);
        p
    };
    let cfg = brutexd::config::Config {
        listen: "127.0.0.1:0".parse().unwrap(),
        upstream_port: dead_port,
        static_dir: PathBuf::from(static_dir.path()),
    };
    let addr = spawn(brutexd::build_router(&cfg)).await;
    let client: TestClient = Client::builder(TokioExecutor::new()).build(HttpConnector::new());
    let (status, headers, body) = get_raw(&client, addr, "/api/v1/anything").await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert_eq!(&body[..], brutexd::proxy::BAD_GATEWAY_BODY.as_bytes());
    assert_eq!(
        headers.get(header::CONTENT_TYPE).map(|v| v.as_bytes()),
        Some("text/plain; charset=utf-8".as_bytes())
    );
}

#[tokio::test]
async fn bare_static_and_static_slash_fall_through_to_the_proxy() {
    let h = harness().await;
    // The upstream has no /static route → its 404 body must come back,
    // proving the front did NOT claim these shapes natively.
    let (d_status, _, d_body) = get_raw(&h.client, h.upstream_addr, "/static").await;
    let (status, _, body) = get_raw(&h.client, h.front_addr, "/static").await;
    assert_eq!(status, d_status);
    assert_eq!(body, d_body);
    let (d_status, _, d_body) = get_raw(&h.client, h.upstream_addr, "/static/").await;
    let (status, _, body) = get_raw(&h.client, h.front_addr, "/static/").await;
    assert_eq!(status, d_status);
    assert_eq!(body, d_body);
}
