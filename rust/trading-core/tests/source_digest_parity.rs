//! Twin-parity golden for the crate-source digest framing
//! (DEC-RUST-SOURCE-DIGEST-TWIN-001, `docs/MIGRATION.md` #1258).
//!
//! Re-includes the ONE shared framing core (`rust/source_digest_core.rs`)
//! and pins it against the language-neutral golden vector: the SAME fixture
//! tree + the SAME pinned hex the Python twin's golden test
//! (`tests/golden/test_wheel_digest_golden.py`) asserts. A framing drift on
//! EITHER side fails that side's golden — parity is enforced with no wheel
//! rebuild in the gate.
//!
//! Fail-before proof (documented per repo rule): during development the
//! Rust side was perturbed (dropping the `0x00` path separator from the
//! frame) and this pin failed with a different digest; restoring the shared
//! framing made it pass. The Python golden test carries the mirrored proof.

include!("../../source_digest_core.rs");

/// The pinned FRAMING v1 digest of `tests/golden/fixtures/source_digest_tree`.
/// MUST equal the literal pinned in `tests/golden/test_wheel_digest_golden.py`
/// and the shared vector file — all three carry the same hex so neither
/// language can silently regenerate the pin after a framing drift.
const PINNED_FIXTURE_DIGEST: &str =
    "7a7a861675f5d3b09b0951a8073bbed7098cbc61ced714f2596a8466e81c4ec9";

fn repo_root() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/rust/trading-core → two levels up.
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn fixture_tree_digest_matches_pinned_vector() {
    let tree = repo_root().join("tests/golden/fixtures/source_digest_tree");
    assert_eq!(crate_source_digest(&tree), PINNED_FIXTURE_DIGEST);
}

#[test]
fn pinned_vector_matches_shared_expected_file() {
    // The shared vector file is what the Python golden also reads; pinning
    // the in-code literal against it guards a stale (or one-sided
    // regenerated) file.
    let expected_file = repo_root().join("tests/golden/fixtures/source_digest_expected.txt");
    let expected = std::fs::read_to_string(&expected_file)
        .unwrap_or_else(|err| panic!("cannot read {}: {err}", expected_file.display()));
    assert_eq!(expected.trim(), PINNED_FIXTURE_DIGEST);
}

#[test]
fn fixture_digest_set_is_exact_and_sorted() {
    // Exclusions by construction: src/notes.txt (not .rs), tests/ + fuzz/
    // (outside src/). Order = UTF-8 byte sort of the relative paths.
    let tree = repo_root().join("tests/golden/fixtures/source_digest_tree");
    let rels: Vec<String> = collect_digest_files(&tree)
        .into_iter()
        .map(|(rel, _)| rel)
        .collect();
    assert_eq!(
        rels,
        vec![
            "Cargo.toml".to_string(),
            "src/aaa.rs".to_string(),
            "src/lib.rs".to_string(),
            "src/nested/deep_mod.rs".to_string(),
            "src/zz_tail.rs".to_string(),
        ]
    );
}

#[test]
fn baked_source_digest_matches_fresh_recompute() {
    // Self-consistency over the REAL crate: the build.rs-baked digest equals
    // a fresh recompute with the shared core over this checkout — proving
    // build.rs and the shared framing agree on a real tree, not just the
    // fixture (cargo rebuilds the lib on any src change, so the two are
    // always in sync at test time).
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    assert_eq!(
        trading_core::SOURCE_DIGEST,
        crate_source_digest(manifest_dir)
    );
}
