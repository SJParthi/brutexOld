"""Pure-Python twin of the Rust crate-source BLAKE3 digest framing.

DEC-RUST-SOURCE-DIGEST-TWIN-001 (``docs/MIGRATION.md`` #1258): each PyO3
kernel crate's ``build.rs`` bakes a BLAKE3 digest of its own source into the
compiled wheel (``rust/source_digest_core.rs`` — the ONE Rust framing core,
``include!``-ed by both ``rust/trading-core`` and ``rust/fno-math``; exposed
as ``SOURCE_DIGEST`` / the PyO3 ``__source_digest__``). This module is the
byte-for-byte Python twin of that framing: ``brutex doctor``'s
``wheel_source_digest`` stages recompute the digest over the repo checkout's
crate tree with THIS code and compare it against the installed wheel's baked
value — a mismatch means the wheel was built from DIFFERENT source than the
checkout (the same-version, symbol-compatible behavioural-drift class the
wheel-freshness stage names as its residual gap).

FRAMING v1 (must stay byte-identical to ``rust/source_digest_core.rs``; the
shared golden vector at ``tests/golden/fixtures/source_digest_expected.txt``
pins the parity — a drift on EITHER side fails that side's golden test):

* **Digest set** — ``Cargo.toml`` at the crate root (iff present) plus every
  regular file whose relative path ends in ``.rs`` under ``src/``, recursive,
  any depth. Everything else is excluded by construction (``target/``,
  ``tests/``, ``fuzz/``, ``python/``, ``benches/``, ``Cargo.lock``,
  ``build.rs``, …). Symlinks are followed (both sides use is-dir / is-file
  predicates that follow links). HONEST residual (Golden Rule #10): the
  excluded ``python/`` shim IS shipped wheel behaviour — maturin's mixed
  layout packages ``rust/<crate>/python/<pkg>/__init__.py`` into the wheel —
  so a wheel stale ONLY in the shim escapes this digest (and the freshness
  stages, which fingerprint the compiled module, not shim content); folding
  the shim into a future FRAMING v2 is the named follow-up.
* **Ordering** — entries sorted ascending by the UTF-8 BYTES of the relative
  path (``/``-separated, no leading ``./``). UTF-8 byte order equals code
  point order, so this matches Rust's byte-wise ``String`` sort exactly; the
  explicit ``encode("utf-8")`` sort key makes the equivalence mechanical.
* **Per-file frame** (unambiguous — the ``0x00`` byte terminates the path,
  the u64-le length prefix terminates the contents, so no concatenation of
  frames can collide)::

      path-bytes ‖ 0x00 ‖ u64-le(content length in bytes) ‖ raw content bytes

* **Digest** — BLAKE3 (single-tree, no key/context) over the concatenated
  frames, rendered as 64 lowercase hex chars. An empty digest set hashes the
  empty stream (the well-known BLAKE3 empty digest).
* **Non-UTF-8 file names** inside the digest set are REJECTED loudly
  (:class:`SourceDigestError` here; a build panic on the Rust side) — never
  skipped, so the two sides can never disagree about the digest set.

The hash primitive is the ``blake3`` PyPI package — already a BruteX BASE
dependency (``pyproject.toml``, DEC-SWEEP-REALTIME-GUARANTEE-001), so no new
dependency and no reliance on the very wheel under test.
"""

from __future__ import annotations

from pathlib import Path

import blake3

__all__ = ["SourceDigestError", "collect_digest_files", "crate_source_digest"]


class SourceDigestError(Exception):
    """A crate tree violates FRAMING v1 (e.g. a non-UTF-8 file name).

    Raised loudly instead of skipping the offending entry: silently dropping
    a file would let the Python twin and the Rust core disagree about the
    digest set, which is exactly the drift this module exists to catch.
    """


def _encode_rel_path(rel: str, directory: Path) -> bytes:
    """UTF-8-encode a relative path, rejecting non-UTF-8 (surrogate) names.

    On POSIX, Python decodes raw filesystem bytes with ``surrogateescape``, so
    a non-UTF-8 file name arrives as a ``str`` carrying lone surrogates that
    ``encode("utf-8")`` refuses — the exact set of names Rust's
    ``OsStr::to_str`` returns ``None`` for (and the build panics on).
    """
    try:
        return rel.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceDigestError(
            f"non-UTF-8 file name {rel!r} under {directory} — rejected (framing v1)"
        ) from exc


def _push_rs_files(directory: Path, rel_prefix: str, out: list[tuple[str, Path]]) -> None:
    """Recursively append ``(relative-path, path)`` for every ``*.rs`` file.

    The twin of ``push_rs_files`` in ``rust/source_digest_core.rs`` —
    unsorted here; :func:`collect_digest_files` sorts the final set.
    """
    for entry in directory.iterdir():
        rel = f"{rel_prefix}/{entry.name}"
        _encode_rel_path(rel, directory)
        if entry.is_dir():
            _push_rs_files(entry, rel, out)
        elif entry.is_file() and rel.endswith(".rs"):
            out.append((rel, entry))


def collect_digest_files(crate_root: Path) -> list[tuple[str, Path]]:
    """The FRAMING v1 digest set for ``crate_root``, sorted.

    ``Cargo.toml`` (iff present) plus ``src/**/*.rs``, sorted ascending by
    the UTF-8 bytes of the relative path — the twin of
    ``collect_digest_files`` in ``rust/source_digest_core.rs``.

    Parameters
    ----------
    crate_root
        The crate directory (the one holding ``Cargo.toml`` + ``src/``).

    Returns
    -------
    list[tuple[str, Path]]
        ``(relative-path, absolute-path)`` pairs in digest order.
    """
    files: list[tuple[str, Path]] = []
    cargo_toml = crate_root / "Cargo.toml"
    if cargo_toml.is_file():
        files.append(("Cargo.toml", cargo_toml))
    src = crate_root / "src"
    if src.is_dir():
        _push_rs_files(src, "src", files)
    files.sort(key=lambda item: item[0].encode("utf-8"))
    return files


def crate_source_digest(crate_root: Path) -> str:
    """BLAKE3 hex digest (64 lowercase chars) of a crate tree under FRAMING v1.

    The Python half of the twin contract with
    ``rust/source_digest_core.rs::crate_source_digest`` — byte-identical
    framing, pinned by the shared golden vector.

    Parameters
    ----------
    crate_root
        The crate directory to digest.

    Returns
    -------
    str
        64 lowercase hex chars.

    Raises
    ------
    SourceDigestError
        A file name inside the digest set is not valid UTF-8.
    OSError
        A digested file or directory cannot be read.
    """
    from brutex.data.guarded_read import (
        read_regular_bytes,
    )

    hasher = blake3.blake3()
    for rel, path in collect_digest_files(crate_root):
        contents = read_regular_bytes(path)
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(len(contents).to_bytes(8, "little"))
        hasher.update(contents)
    return str(hasher.hexdigest())
