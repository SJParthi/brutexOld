//! `/static/*` — native file serving over `brutex/dashboard/static/`.
//!
//! Contract (parity-gated against the live Starlette `StaticFiles` mount in
//! `tests/integration/test_sidecar_parity_static.py`):
//!
//! * body bytes are the file bytes verbatim (sha256-pinned equal),
//! * `content-type` mirrors what Starlette emits for the extensions present
//!   in the tree (Python `mimetypes` value + Starlette's `; charset=utf-8`
//!   suffix for `text/*` media types),
//! * `content-length` is the exact byte count,
//! * NO `cache-control` header — Starlette's StaticFiles sets none, and the
//!   parity comparator asserts absent == absent.
//!
//! HONEST NON-GOALS (Wave 1, documented not hidden): Starlette also emits
//! `etag` + `last-modified` and honours conditional / Range requests;
//! brutexd deliberately does NOT replicate those in this wave — a
//! conditional request is answered with a full 200 (correct, never stale,
//! just less cacheable). The dashboard's own pages never issue Range
//! requests against these assets.
//!
//! Security: path-traversal refusal (component-wise, on the DECODED path,
//! so `%2e%2e` cannot smuggle `..`), symlink refusal on the leaf, and a
//! canonical-containment check that also catches an intermediate symlink
//! escaping the tree. Every refusal is a plain 404 — indistinguishable from
//! an absent file (no oracle for probing the filesystem).

use std::path::{Path as FsPath, PathBuf};

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{Response, StatusCode, header};

use crate::AppState;

/// Map a file extension to the content-type Starlette would emit.
///
/// Python 3.12 `mimetypes` values with Starlette's `; charset=utf-8` suffix
/// on `text/*`. Only `.js` and `.css` exist in the tree today; the fallback
/// is `application/octet-stream`, and the live parity gate would catch any
/// drift if a new extension lands in `static/`.
pub fn content_type_for(name: &str) -> &'static str {
    let ext = name.rsplit('.').next().unwrap_or("");
    match ext.to_ascii_lowercase().as_str() {
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "html" | "htm" => "text/html; charset=utf-8",
        "txt" => "text/plain; charset=utf-8",
        "json" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/vnd.microsoft.icon",
        _ => "application/octet-stream",
    }
}

/// Validate the (percent-decoded) wildcard remainder of `/static/{*path}`.
///
/// Returns the safe relative path, or `None` for anything that must 404:
/// empty/`.`/`..` components, backslashes, NUL bytes. Component-wise on the
/// decoded string, so encoded traversal (`%2e%2e`) is caught identically.
pub fn sanitize_rel_path(rel: &str) -> Option<PathBuf> {
    if rel.is_empty() {
        return None;
    }
    let mut out = PathBuf::new();
    for comp in rel.split('/') {
        if comp.is_empty() || comp == "." || comp == ".." {
            return None;
        }
        if comp.contains('\\') || comp.contains('\0') {
            return None;
        }
        out.push(comp);
    }
    Some(out)
}

fn plain_404() -> Response<Body> {
    let mut resp = Response::new(Body::from("not found\n"));
    *resp.status_mut() = StatusCode::NOT_FOUND;
    resp.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    resp
}

/// Resolve + read a static file under `static_dir`, refusing traversal,
/// symlinked leaves, and any canonical escape from the tree.
async fn load(static_dir: &FsPath, rel: &str) -> Option<(&'static str, Vec<u8>)> {
    let safe_rel = sanitize_rel_path(rel)?;
    let candidate = static_dir.join(&safe_rel);
    // Leaf symlink refusal: lstat the exact path BEFORE following anything.
    let meta = tokio::fs::symlink_metadata(&candidate).await.ok()?;
    if meta.file_type().is_symlink() || !meta.is_file() {
        return None;
    }
    // Canonical containment: catches an intermediate symlinked directory
    // pointing OUTSIDE the tree (the leaf lstat alone cannot see that).
    let canon_dir = tokio::fs::canonicalize(static_dir).await.ok()?;
    let canon = tokio::fs::canonicalize(&candidate).await.ok()?;
    if !canon.starts_with(&canon_dir) {
        return None;
    }
    let bytes = tokio::fs::read(&canon).await.ok()?;
    let name = safe_rel.file_name().and_then(|n| n.to_str()).unwrap_or("");
    Some((content_type_for(name), bytes))
}

/// Serve one static file (GET/HEAD; axum strips the HEAD body).
pub async fn serve(State(state): State<AppState>, Path(rel): Path<String>) -> Response<Body> {
    match load(&state.static_dir, &rel).await {
        Some((ctype, bytes)) => {
            let mut resp = Response::new(Body::from(bytes));
            match header::HeaderValue::from_str(ctype) {
                Ok(value) => {
                    resp.headers_mut().insert(header::CONTENT_TYPE, value);
                }
                Err(_) => {
                    // Unreachable with the static map above; degrade, never panic.
                    tracing::error!(ctype, "brutexd.static.bad_content_type");
                }
            }
            resp
        }
        None => plain_404(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn content_type_matches_starlette_for_tree_extensions() {
        assert_eq!(
            content_type_for("bx-ui.js"),
            "text/javascript; charset=utf-8"
        );
        assert_eq!(content_type_for("bx-theme.css"), "text/css; charset=utf-8");
        assert_eq!(
            content_type_for("UPPER.JS"),
            "text/javascript; charset=utf-8"
        );
        assert_eq!(content_type_for("page.html"), "text/html; charset=utf-8");
        assert_eq!(content_type_for("data.json"), "application/json");
        assert_eq!(content_type_for("noext"), "application/octet-stream");
        assert_eq!(content_type_for(""), "application/octet-stream");
    }

    #[test]
    fn sanitize_accepts_plain_and_nested_paths() {
        assert_eq!(sanitize_rel_path("a.js"), Some(PathBuf::from("a.js")));
        assert_eq!(
            sanitize_rel_path("sub/a.css"),
            Some(PathBuf::from("sub/a.css"))
        );
    }

    #[test]
    fn sanitize_refuses_traversal_and_junk() {
        for bad in [
            "",
            ".",
            "..",
            "../secret",
            "a/../b",
            "a//b",
            "/abs",
            "a/",
            "a\\b",
            "a\0b",
            "..\\win",
        ] {
            assert_eq!(sanitize_rel_path(bad), None, "should refuse {bad:?}");
        }
    }
}
