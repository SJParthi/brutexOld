"""Framing unit + property tests for :mod:`brutex.wheel_digest`.

DEC-RUST-SOURCE-DIGEST-TWIN-001 (``docs/MIGRATION.md`` #1258): the Python
twin of the Rust crate-source digest framing (``rust/source_digest_core.rs``,
FRAMING v1). These tests pin the twin against an INDEPENDENT hand-rolled
framing oracle (raw ``blake3`` over manually concatenated frames — never the
module under test), plus every framing edge: empty tree, nested dirs,
UTF-8-byte ordering, exclusions, empty files, boundary-shift resistance,
non-UTF-8 names, dangling symlinks. The cross-language parity itself is
pinned by the shared golden vector (``tests/golden/test_wheel_digest_golden.py``
+ each crate's ``tests/source_digest_parity.rs``).
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
from pathlib import Path

import blake3
import pytest
from brutex.wheel_digest import SourceDigestError, collect_digest_files, crate_source_digest
from hypothesis import given, settings
from hypothesis import strategies as st

#: BLAKE3 of the empty stream — the digest of an EMPTY digest set (a tree
#: with no Cargo.toml and no src/**/*.rs). Public BLAKE3 test-vector value.
BLAKE3_EMPTY = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"


def _write(tree: Path, rel: str, data: bytes) -> None:
    """Write ``data`` at ``tree/rel``, creating parent directories."""
    target = tree / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _write_non_utf8_named_file(directory: Path, name_bytes: bytes) -> None:
    """Create a file whose NAME is not valid UTF-8, or skip where impossible.

    Linux (CI) accepts arbitrary filename bytes, so the framing's loud
    rejection is exercised there on every run. The §8 reference machine
    (Apple M4 Pro / APFS) REFUSES non-UTF-8 names at ``write`` time (EILSEQ)
    — on such a filesystem the rejection arm is untestable, so the test must
    SKIP with the reason, never error out mid-setup (hostile-review fold,
    DEC-RUST-SOURCE-DIGEST-TWIN-001).
    """
    bad = os.path.join(os.fsdecode(bytes(directory)), os.fsdecode(name_bytes))
    try:
        Path(bad).write_bytes(b"junk")
    except OSError as exc:
        pytest.skip(
            f"filesystem refuses non-UTF-8 file names ({exc!r}) — the framing "
            "rejection arm is exercised on Linux CI"
        )


def _casefold_nfd_dedup(files: dict[str, bytes]) -> dict[str, bytes]:
    """Drop drawn names that collide on a case-/normalization-insensitive FS.

    APFS default volumes (the §8 reference machine) fold case AND normalize
    Unicode, so two hypothesis-drawn names like ``src/A.rs`` / ``src/a.rs``
    would land in ONE on-disk file while the oracle frames two entries. Keyed
    on ``NFD``-normalized ``casefold()`` — a superset of APFS's folding for
    the drawn alphabet — first drawn name wins, deterministic for shrinking.
    """
    unique: dict[str, bytes] = {}
    seen: set[str] = set()
    for rel, data in files.items():
        folded = unicodedata.normalize("NFD", rel).casefold()
        if folded in seen:
            continue
        seen.add(folded)
        unique[rel] = data
    return unique


def _manual_digest(entries: list[tuple[str, bytes]]) -> str:
    """INDEPENDENT framing oracle: raw blake3 over hand-concatenated frames.

    Mirrors the DOCUMENTED framing (path ‖ 0x00 ‖ u64-le length ‖ contents,
    entries pre-sorted by UTF-8 path bytes) without calling the module under
    test, so an implementation bug cannot self-validate.
    """
    buf = b""
    for rel, contents in sorted(entries, key=lambda item: item[0].encode("utf-8")):
        buf += rel.encode("utf-8") + b"\x00" + len(contents).to_bytes(8, "little") + contents
    return str(blake3.blake3(buf).hexdigest())


class TestCollectDigestFiles:
    def test_empty_tree_collects_nothing(self, tmp_path: Path) -> None:
        assert collect_digest_files(tmp_path) == []

    def test_cargo_toml_only(self, tmp_path: Path) -> None:
        _write(tmp_path, "Cargo.toml", b"[package]\n")
        assert [rel for rel, _ in collect_digest_files(tmp_path)] == ["Cargo.toml"]

    def test_src_only_without_cargo_toml(self, tmp_path: Path) -> None:
        # The framing tolerates a manifest-less tree (fixture trees); a real
        # crate build always has Cargo.toml, so production behaviour is
        # identical on both sides.
        _write(tmp_path, "src/lib.rs", b"x")
        assert [rel for rel, _ in collect_digest_files(tmp_path)] == ["src/lib.rs"]

    def test_nested_dirs_recursed_and_sorted_by_utf8_bytes(self, tmp_path: Path) -> None:
        # Created deliberately in NON-sorted order; the final sort must be
        # by UTF-8 bytes of the relative path (== Rust's byte-wise sort).
        _write(tmp_path, "src/zz.rs", b"z")
        _write(tmp_path, "src/nested/deep.rs", b"d")
        _write(tmp_path, "src/aaa.rs", b"a")
        _write(tmp_path, "Cargo.toml", b"[package]\n")
        _write(tmp_path, "src/ab.rs", b"ab")
        rels = [rel for rel, _ in collect_digest_files(tmp_path)]
        assert rels == [
            "Cargo.toml",
            "src/aaa.rs",
            "src/ab.rs",
            "src/nested/deep.rs",
            "src/zz.rs",
        ]

    def test_exclusions_by_construction(self, tmp_path: Path) -> None:
        # Everything outside Cargo.toml + src/**/*.rs is invisible to the
        # framing: tests/, fuzz/, target/, benches/, python/, Cargo.lock,
        # build.rs, and non-.rs files under src/.
        _write(tmp_path, "Cargo.toml", b"[package]\n")
        _write(tmp_path, "src/lib.rs", b"x")
        _write(tmp_path, "src/notes.txt", b"not rust")
        _write(tmp_path, "src/upper.RS", b"case-sensitive suffix")
        _write(tmp_path, "src/backup.rs.bak", b"not a .rs suffix")
        _write(tmp_path, "tests/excluded.rs", b"outside src")
        _write(tmp_path, "fuzz/excluded.rs", b"outside src")
        _write(tmp_path, "target/excluded.rs", b"outside src")
        _write(tmp_path, "benches/excluded.rs", b"outside src")
        _write(tmp_path, "python/pkg/__init__.py", b"outside src")
        _write(tmp_path, "Cargo.lock", b"lock")
        _write(tmp_path, "build.rs", b"fn main() {}")
        assert [rel for rel, _ in collect_digest_files(tmp_path)] == [
            "Cargo.toml",
            "src/lib.rs",
        ]

    def test_bare_dot_rs_name_is_included(self, tmp_path: Path) -> None:
        # Degenerate but well-defined: "src/.rs" ends with ".rs" on BOTH
        # sides (Rust `ends_with` / Python `endswith`), so it is in the set —
        # pinned here so the twin semantics cannot drift on the corner.
        _write(tmp_path, "src/.rs", b"")
        assert [rel for rel, _ in collect_digest_files(tmp_path)] == ["src/.rs"]

    def test_dangling_symlink_under_src_is_skipped(self, tmp_path: Path) -> None:
        # Both sides classify via link-following is-dir / is-file predicates;
        # a dangling symlink is neither → silently outside the digest set.
        _write(tmp_path, "src/lib.rs", b"x")
        (tmp_path / "src" / "gone.rs").symlink_to(tmp_path / "no_such_target.rs")
        assert [rel for rel, _ in collect_digest_files(tmp_path)] == ["src/lib.rs"]

    def test_non_utf8_name_rejected_even_for_non_rs_entry(self, tmp_path: Path) -> None:
        # FRAMING v1 rejects a non-UTF-8 name LOUDLY (never skips): Rust's
        # OsStr::to_str fails on the same set of names, and rejection happens
        # BEFORE suffix classification on both sides — so even a non-.rs bad
        # name rejects, keeping the two sides byte-identical about the set.
        (tmp_path / "src").mkdir()
        _write_non_utf8_named_file(tmp_path / "src", b"bad\xffname.txt")
        with pytest.raises(SourceDigestError, match="non-UTF-8 file name"):
            collect_digest_files(tmp_path)


class TestCrateSourceDigest:
    def test_empty_tree_hashes_empty_stream(self, tmp_path: Path) -> None:
        assert crate_source_digest(tmp_path) == BLAKE3_EMPTY

    def test_matches_independent_manual_framing_oracle(self, tmp_path: Path) -> None:
        entries = [
            ("Cargo.toml", b'[package]\nname = "x"\n'),
            ("src/aaa.rs", b"pub const A: u8 = 1;\n"),
            ("src/lib.rs", b"pub fn f() {}\n"),
            ("src/nested/deep.rs", b""),
        ]
        for rel, data in entries:
            _write(tmp_path, rel, data)
        assert crate_source_digest(tmp_path) == _manual_digest(entries)

    def test_deterministic_across_absolute_locations(self, tmp_path: Path) -> None:
        # Only RELATIVE paths are framed — the same tree at two different
        # absolute roots digests identically (what lets the doctor compare a
        # wheel built elsewhere against this checkout).
        for root in (tmp_path / "one", tmp_path / "two" / "deeper"):
            _write(root, "Cargo.toml", b"[package]\n")
            _write(root, "src/lib.rs", b"pub fn f() {}\n")
        assert crate_source_digest(tmp_path / "one") == crate_source_digest(
            tmp_path / "two" / "deeper"
        )

    def test_single_byte_flip_changes_digest(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/lib.rs", b"pub const X: u8 = 1;\n")
        before = crate_source_digest(tmp_path)
        _write(tmp_path, "src/lib.rs", b"pub const X: u8 = 2;\n")
        assert crate_source_digest(tmp_path) != before

    def test_rename_changes_digest(self, tmp_path: Path) -> None:
        # Same bytes under a different name is DIFFERENT source (the path is
        # part of the frame).
        _write(tmp_path, "src/a.rs", b"same bytes")
        before = crate_source_digest(tmp_path)
        (tmp_path / "src" / "a.rs").rename(tmp_path / "src" / "b.rs")
        assert crate_source_digest(tmp_path) != before

    def test_content_shift_across_file_boundary_changes_digest(self, tmp_path: Path) -> None:
        # {a: "ab", b: "c"} vs {a: "a", b: "bc"} — identical concatenated
        # bytes; the u64-le length prefix keeps the frames unambiguous.
        _write(tmp_path, "src/a.rs", b"ab")
        _write(tmp_path, "src/b.rs", b"c")
        first = crate_source_digest(tmp_path)
        _write(tmp_path, "src/a.rs", b"a")
        _write(tmp_path, "src/b.rs", b"bc")
        assert crate_source_digest(tmp_path) != first

    def test_empty_file_still_framed(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/lib.rs", b"x")
        before = crate_source_digest(tmp_path)
        _write(tmp_path, "src/empty.rs", b"")
        assert crate_source_digest(tmp_path) != before

    def test_excluded_files_never_move_the_digest(self, tmp_path: Path) -> None:
        _write(tmp_path, "Cargo.toml", b"[package]\n")
        _write(tmp_path, "src/lib.rs", b"x")
        before = crate_source_digest(tmp_path)
        _write(tmp_path, "src/notes.txt", b"junk")
        _write(tmp_path, "tests/t.rs", b"junk")
        _write(tmp_path, "fuzz/f.rs", b"junk")
        _write(tmp_path, "target/release/artifact.rs", b"junk")
        _write(tmp_path, "Cargo.lock", b"junk")
        _write(tmp_path, "build.rs", b"junk")
        assert crate_source_digest(tmp_path) == before

    def test_non_utf8_rs_name_raises_source_digest_error(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        _write_non_utf8_named_file(tmp_path / "src", b"bad\xff.rs")
        with pytest.raises(SourceDigestError, match="non-UTF-8 file name"):
            crate_source_digest(tmp_path)

    @settings(max_examples=25, deadline=None)
    @given(
        files=st.dictionaries(
            keys=st.text(
                alphabet=st.characters(categories=("Ll", "Lu", "Nd"), max_codepoint=0x24F),
                min_size=1,
                max_size=8,
            ).map(lambda stem: f"src/{stem}.rs"),
            values=st.binary(max_size=64),
            max_size=6,
        )
    )
    def test_property_twin_equals_manual_oracle(self, files: dict[str, bytes]) -> None:
        """For arbitrary small trees the twin equals the hand-rolled oracle.

        Bounded property (Golden Rule #12 — 7 test types): file names drawn
        from letters/digits (incl. Latin-Extended, exercising multi-byte
        UTF-8 in the sort key), contents arbitrary bytes. Drawn names are
        first deduplicated on their casefolded-NFD form
        (:func:`_casefold_nfd_dedup`) so a case-/normalization-insensitive
        filesystem (APFS default volumes — the §8 reference machine) can
        never collapse two drawn stems into one on-disk file while the
        oracle frames both — the property stays deterministic everywhere.
        """
        unique = _casefold_nfd_dedup(files)
        with tempfile.TemporaryDirectory() as raw:
            tree = Path(raw)
            for rel, data in unique.items():
                _write(tree, rel, data)
            assert crate_source_digest(tree) == _manual_digest(list(unique.items()))


class TestRealCrateTrees:
    def test_real_kernel_crates_digest_cleanly(self) -> None:
        # Smoke over the two REAL crates the doctor watches: 64 lowercase hex
        # and a digest set anchored by Cargo.toml + src/lib.rs.
        repo = Path(__file__).resolve().parents[2]
        for crate in ("rust/trading-core", "rust/fno-math"):
            digest = crate_source_digest(repo / crate)
            assert len(digest) == 64
            assert digest == digest.lower()
            assert set(digest) <= set("0123456789abcdef")
            rels = [rel for rel, _ in collect_digest_files(repo / crate)]
            assert "Cargo.toml" in rels
            assert "src/lib.rs" in rels
