//! Twin-parity golden for the crate-source digest framing
//! (DEC-RUST-SOURCE-DIGEST-TWIN-001, `docs/MIGRATION.md` #1258) — the
//! brutex-calendar instance of the house law every PyO3 kernel crate
//! carries.
//!
//! Re-includes the ONE shared framing core (`rust/source_digest_core.rs`)
//! and pins it against the language-neutral golden vector: the SAME fixture
//! tree + the SAME pinned hex the Python twin's golden test
//! (`tests/golden/test_wheel_digest_golden.py`) asserts. A framing drift on
//! EITHER side fails that side's golden — parity is enforced with no wheel
//! rebuild in the gate.

include!("../../source_digest_core.rs");

/// The pinned FRAMING v1 digest of `tests/golden/fixtures/source_digest_tree`.
/// MUST equal the literal pinned in `tests/golden/test_wheel_digest_golden.py`
/// and the sibling crates' parity tests.
const PINNED_FIXTURE_DIGEST: &str =
    "7a7a861675f5d3b09b0951a8073bbed7098cbc61ced714f2596a8466e81c4ec9";

fn repo_root() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/rust/brutex-calendar → two levels up.
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn fixture_tree_digest_matches_pinned_vector() {
    let tree = repo_root().join("tests/golden/fixtures/source_digest_tree");
    assert_eq!(crate_source_digest(&tree), PINNED_FIXTURE_DIGEST);
}

#[test]
fn pinned_vector_matches_shared_expected_file() {
    let expected_file = repo_root().join("tests/golden/fixtures/source_digest_expected.txt");
    let expected = std::fs::read_to_string(&expected_file)
        .unwrap_or_else(|err| panic!("cannot read {}: {err}", expected_file.display()));
    assert_eq!(expected.trim(), PINNED_FIXTURE_DIGEST);
}

#[test]
fn baked_source_digest_matches_fresh_recompute() {
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    assert_eq!(
        brutex_calendar::SOURCE_DIGEST,
        crate_source_digest(manifest_dir)
    );
}
