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
//! escaping the tree.
//!
//! Miss vs refusal (coordinator ruling 2026-07-11, LOW-2): the two 404 kinds
//! are DELIBERATELY split so the front is transparent even for a MISS —
//!
//! * a **genuine miss** (the request sanitizes cleanly, no symlink, but the
//!   file simply is not there) is PROXIED to the FastAPI upstream, so the
//!   body/status/content-type are byte-identical to Starlette's own
//!   `StaticFiles` 404 (pinned by `tests/integration/test_sidecar_parity_static.py`);
//! * a **security refusal** (traversal / backslash / NUL / empty component,
//!   a symlinked leaf, a canonical escape from the tree, or any non-NotFound
//!   filesystem error) stays a LOCAL hard 404 ([`plain_404`]) — the attack
//!   path is NEVER forwarded upstream, and the refusal never probes/serves
//!   outside the tree.
//!
//! This does not open a filesystem-probing oracle beyond what Starlette
//! already exposes (present ⇒ 200, absent ⇒ 404); the added distinction is
//! only "your traversal was refused locally" vs "that file is absent", and a
//! traversal sender already knows they sent one.

use std::path::{Path as FsPath, PathBuf};

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{Request, Response, StatusCode, header};

use crate::AppState;
use crate::proxy;

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

/// The outcome of resolving a `/static/{*path}` request against the tree.
enum Resolved {
    /// A servable file: its content-type + verbatim bytes.
    File(&'static str, Vec<u8>),
    /// A security refusal (traversal / symlink / escape / fs error) — served
    /// as a LOCAL hard 404, never proxied.
    Refuse,
    /// A genuine absence — proxied to the upstream for body parity with
    /// Starlette's own `StaticFiles` 404.
    Miss,
}

/// Resolve a static file under `static_dir`, distinguishing a servable file, a
/// security refusal (traversal, symlinked leaf, canonical escape, or any
/// non-NotFound filesystem error — all fail closed), and a genuine miss.
async fn resolve(static_dir: &FsPath, rel: &str) -> Resolved {
    let Some(safe_rel) = sanitize_rel_path(rel) else {
        return Resolved::Refuse; // traversal / backslash / NUL / empty component
    };
    let candidate = static_dir.join(&safe_rel);
    // Leaf symlink refusal: lstat the exact path BEFORE following anything.
    let meta = match tokio::fs::symlink_metadata(&candidate).await {
        Ok(meta) => meta,
        // A genuinely-absent file is a MISS (proxied); any OTHER error
        // (permission, ENOTDIR, …) fails CLOSED to a local refusal.
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Resolved::Miss,
        Err(_) => return Resolved::Refuse,
    };
    if meta.file_type().is_symlink() {
        return Resolved::Refuse; // symlinked leaf — never followed
    }
    if !meta.is_file() {
        // A directory (or other non-file) is not servable here; treat it as an
        // absence so the upstream answers exactly as it does today.
        return Resolved::Miss;
    }
    // Canonical containment: catches an intermediate symlinked directory
    // pointing OUTSIDE the tree (the leaf lstat alone cannot see that).
    let canon_dir = match tokio::fs::canonicalize(static_dir).await {
        Ok(canon_dir) => canon_dir,
        Err(_) => return Resolved::Refuse,
    };
    let canon = match tokio::fs::canonicalize(&candidate).await {
        Ok(canon) => canon,
        Err(_) => return Resolved::Refuse,
    };
    if !canon.starts_with(&canon_dir) {
        return Resolved::Refuse; // an intermediate symlink escaped the tree
    }
    let bytes = match tokio::fs::read(&canon).await {
        Ok(bytes) => bytes,
        Err(_) => return Resolved::Refuse,
    };
    let name = safe_rel.file_name().and_then(|n| n.to_str()).unwrap_or("");
    Resolved::File(content_type_for(name), bytes)
}

/// Serve one static file (GET/HEAD; axum strips the HEAD body).
///
/// A served file answers natively; a security refusal is a local hard 404; a
/// genuine miss is proxied to the upstream so its 404 is byte-identical to
/// Starlette's own `StaticFiles`. `Path` reads only the URI (a
/// `FromRequestParts` extractor), so the trailing `Request` — needed to
/// forward a miss verbatim — still receives the full body.
pub async fn serve(
    State(state): State<AppState>,
    Path(rel): Path<String>,
    req: Request<Body>,
) -> Response<Body> {
    match resolve(&state.static_dir, &rel).await {
        Resolved::File(ctype, bytes) => {
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
        Resolved::Refuse => plain_404(),
        Resolved::Miss => proxy::forward(State(state), req).await,
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
