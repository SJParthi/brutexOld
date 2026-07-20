// build.rs — bake the crate-source BLAKE3 digest into the compiled artifact
// (DEC-RUST-SOURCE-DIGEST-TWIN-001, docs/MIGRATION.md #1258).
//
// The digest (FRAMING v1 — see rust/source_digest_core.rs, the ONE shared
// framing all kernel crates and the Python twin `brutex/wheel_digest.py`
// mirror) covers `Cargo.toml` + `src/**/*.rs`, so ANY source change moves the
// baked value. The crate re-exposes it as
// `brutex_observability::SOURCE_DIGEST` and the PyO3 module exports
// `SOURCE_DIGEST` / `__source_digest__` (the brutex-feed shape,
// DEC-RUST-OBSERVABILITY-001).

include!("../source_digest_core.rs");

fn main() {
    let crate_root = std::path::PathBuf::from(
        std::env::var("CARGO_MANIFEST_DIR").expect("cargo always sets CARGO_MANIFEST_DIR"),
    );
    // Re-run on any digested input change: the manifest + the whole src tree
    // (a directory path makes cargo watch it recursively — catching file
    // adds/removes too) + the shared framing core itself.
    println!("cargo:rerun-if-changed=Cargo.toml");
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=../source_digest_core.rs");
    println!(
        "cargo:rustc-env=BRUTEX_SOURCE_DIGEST={}",
        crate_source_digest(&crate_root)
    );
}
