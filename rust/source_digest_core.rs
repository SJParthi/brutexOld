// source_digest_core.rs — the ONE crate-source BLAKE3 digest framing
// (DEC-RUST-SOURCE-DIGEST-TWIN-001, docs/MIGRATION.md #1258).
//
// `include!`-ed (COMMON — one source of truth, CLAUDE.md §2 Rule 11) by BOTH
// PyO3 kernel crates from exactly two kinds of sites:
//
//   * `rust/<crate>/build.rs` — computes the digest over the crate checkout at
//     build time and bakes it into the artifact via
//     `cargo:rustc-env=BRUTEX_SOURCE_DIGEST=<hex>`; the crate re-exposes it as
//     `SOURCE_DIGEST` and the PyO3 module exports `__source_digest__`.
//   * `rust/<crate>/tests/source_digest_parity.rs` — pins this framing against
//     the language-neutral golden vector so the Rust side and the Python twin
//     (`brutex/wheel_digest.py`) can NEVER drift apart silently.
//
// ── FRAMING v1 (mirrored byte-for-byte by brutex/wheel_digest.py) ──────────
//
// Digest set (everything else is excluded BY CONSTRUCTION — `target/`,
// `tests/`, `fuzz/`, `python/`, `benches/`, `Cargo.lock`, `build.rs`, …):
//   * `Cargo.toml` at the crate root, iff present (dependency-pin changes
//     alter compiled behaviour, so the manifest is part of the source
//     identity; always present for a real crate build);
//   * every regular file whose relative path ends in `.rs` under `src/`,
//     recursively, any depth. Symlinks are followed (`is_dir`/`is_file`
//     follow, exactly like Python's `Path.is_dir`/`is_file`).
//
// Ordering: entries sorted ascending by the UTF-8 BYTES of the relative path
// (`/`-separated, no leading `./`). UTF-8 byte order equals code-point order,
// so the Python twin's `key=path.encode("utf-8")` sort is identical.
//
// Per-file frame (unambiguous — the 0x00 separator ends the path, the u64-le
// length prefix ends the contents, so no concatenation of frames can collide):
//   path-bytes ‖ 0x00 ‖ u64-le(content length in bytes) ‖ raw content bytes
//
// Digest: BLAKE3 (single-tree, no key/context) over the concatenated frames;
// rendered as 64 lowercase hex chars. An EMPTY digest set hashes the empty
// stream (the well-known BLAKE3 empty digest).
//
// A NON-UTF-8 file name inside the digest set is REJECTED loudly (build
// panic here; `SourceDigestError` in the Python twin) — never skipped, so
// the two sides can never disagree about the digest set.
//
// No top-level `use` statements: this file is textually included into hosts
// that may hold their own imports, so every path is fully qualified.

/// Recursively collect `(relative-path, absolute-path)` pairs for every
/// regular `*.rs` file under `dir`, appending to `out` (unsorted; the caller
/// sorts the final set).
fn push_rs_files(
    dir: &std::path::Path,
    rel_prefix: &str,
    out: &mut Vec<(String, std::path::PathBuf)>,
) {
    let entries = std::fs::read_dir(dir)
        .unwrap_or_else(|err| panic!("source-digest: cannot read dir {}: {err}", dir.display()));
    for entry in entries {
        let entry = entry.unwrap_or_else(|err| {
            panic!("source-digest: cannot read dir entry under {}: {err}", dir.display())
        });
        let path = entry.path();
        let file_name = entry.file_name();
        let name = file_name.to_str().unwrap_or_else(|| {
            panic!(
                "source-digest: non-UTF-8 file name {:?} under {} — rejected (framing v1)",
                file_name,
                dir.display()
            )
        });
        let rel = format!("{rel_prefix}/{name}");
        if path.is_dir() {
            push_rs_files(&path, &rel, out);
        } else if path.is_file() && rel.ends_with(".rs") {
            out.push((rel, path));
        }
    }
}

/// The FRAMING v1 digest set for `crate_root`: `Cargo.toml` (iff present) +
/// `src/**/*.rs`, sorted ascending by the UTF-8 bytes of the relative path.
fn collect_digest_files(crate_root: &std::path::Path) -> Vec<(String, std::path::PathBuf)> {
    let mut files: Vec<(String, std::path::PathBuf)> = Vec::new();
    let cargo_toml = crate_root.join("Cargo.toml");
    if cargo_toml.is_file() {
        files.push(("Cargo.toml".to_string(), cargo_toml));
    }
    let src = crate_root.join("src");
    if src.is_dir() {
        push_rs_files(&src, "src", &mut files);
    }
    files.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    files
}

/// BLAKE3 hex digest (64 lowercase chars) of `crate_root`'s source under
/// FRAMING v1 — the Rust half of the twin contract with
/// `brutex.wheel_digest.crate_source_digest`.
fn crate_source_digest(crate_root: &std::path::Path) -> String {
    let mut hasher = blake3::Hasher::new();
    for (rel, path) in collect_digest_files(crate_root) {
        let contents = std::fs::read(&path).unwrap_or_else(|err| {
            panic!("source-digest: cannot read {}: {err}", path.display())
        });
        hasher.update(rel.as_bytes());
        hasher.update(&[0u8]);
        hasher.update(&(contents.len() as u64).to_le_bytes());
        hasher.update(&contents);
    }
    hasher.finalize().to_hex().to_string()
}
