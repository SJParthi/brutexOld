"""Twin-parity GOLDEN for the crate-source digest framing (Python side).

DEC-RUST-SOURCE-DIGEST-TWIN-001 (``docs/MIGRATION.md`` #1258): the Python
twin (:mod:`brutex.wheel_digest`) and the Rust core
(``rust/source_digest_core.rs``) are pinned to ONE language-neutral golden
vector — the fixture tree at ``tests/golden/fixtures/source_digest_tree`` and
the pinned hex below. Each crate's ``tests/source_digest_parity.rs`` asserts
the SAME literal over the SAME tree, so a framing drift on EITHER side fails
that side's golden with no wheel rebuild in the gate.

Fail-before proof (repo bug-fix regression rule, adapted for a parity gate):
during development the Python side was perturbed — dropping the ``0x00``
path separator from the frame — and this pin failed with digest
``836ef51f43a793d44822944bd29e0c3520bf726cca123c7d6cf5dff76204d6be`` != the
pinned vector; restoring the shared framing made it pass.
The Rust parity test carries the mirrored proof (same perturbation class on
the Rust side breaks its pin). On ``origin/main`` neither the module nor the
framing exists at all, so this whole suite fails collection there — the
new-behaviour half of fail-before/pass-after.
"""

from __future__ import annotations

from pathlib import Path

from brutex.wheel_digest import collect_digest_files, crate_source_digest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_TREE = REPO_ROOT / "tests" / "golden" / "fixtures" / "source_digest_tree"
EXPECTED_FILE = REPO_ROOT / "tests" / "golden" / "fixtures" / "source_digest_expected.txt"

#: The pinned FRAMING v1 digest of the fixture tree. MUST equal the literal
#: pinned in each crate's ``tests/source_digest_parity.rs``
#: (``PINNED_FIXTURE_DIGEST``) and the shared vector file — all three carry
#: the same hex so neither language can silently regenerate the pin after a
#: framing drift.
PINNED_FIXTURE_DIGEST = "7a7a861675f5d3b09b0951a8073bbed7098cbc61ced714f2596a8466e81c4ec9"


def test_fixture_tree_digest_matches_pinned_vector() -> None:
    assert crate_source_digest(FIXTURE_TREE) == PINNED_FIXTURE_DIGEST


def test_pinned_vector_matches_shared_expected_file() -> None:
    # The shared vector file is what the Rust goldens also read; pinning the
    # in-code literal against it guards a stale (or one-sided regenerated)
    # file.
    assert EXPECTED_FILE.read_text(encoding="utf-8").strip() == PINNED_FIXTURE_DIGEST


def test_fixture_digest_set_is_exact_and_sorted() -> None:
    # Exclusions by construction: src/notes.txt (not .rs), tests/ + fuzz/
    # (outside src/). Order = UTF-8 byte sort of the relative paths. Mirrors
    # `fixture_digest_set_is_exact_and_sorted` in source_digest_parity.rs.
    assert [rel for rel, _ in collect_digest_files(FIXTURE_TREE)] == [
        "Cargo.toml",
        "src/aaa.rs",
        "src/lib.rs",
        "src/nested/deep_mod.rs",
        "src/zz_tail.rs",
    ]
