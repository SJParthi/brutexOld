"""Verbatim raw-payload ARCHIVE — store the bytes themselves for offline re-verify.

DEC-RAW-PAYLOAD-ARCHIVE-001. This EXTENDS the raw-data integrity layer
(:mod:`brutex.data.raw_integrity` / :mod:`brutex.data.raw_integrity_store`,
DEC-RAW-DATA-INTEGRITY-001). That layer records a content HASH of exactly what
Groww sent per fetched window; this module OPTIONALLY archives the
canonicalised raw payload BYTES themselves (gzip-compressed), keyed by the SAME
``(exchange, segment, groww_symbol, interval, window_start, window_end)`` tuple,
so integrity can be re-verified **offline** later — recompute the hash from the
archived bytes and compare it to the stored integrity record (proving
lake == archived raw == recorded hash) WITHOUT re-fetching from the vendor.

**OFF by default (storage cost).** Archiving every raw payload is heavy (one
compressed file per window across the whole multi-year, multi-instrument pull),
so the archiver is OPT-IN: it is enabled only when an explicit
:class:`RawPayloadArchiveConfig` says so (or the ``BRUTEX_RAW_PAYLOAD_ARCHIVE``
environment flag is truthy). When disabled, :func:`make_archive_hook` returns a
no-op :data:`~brutex.adapters.groww.candles.RawPayloadHook` and NOTHING is
written — the pull is byte-identical to the pre-feature path.

**COMMON (CLAUDE.md §11).** The canonical-byte serialisation + the hash come
from :mod:`brutex.data.raw_integrity` (:func:`~brutex.data.raw_integrity.canonicalize_payload`
+ :func:`~brutex.data.raw_integrity.hash_payload`) — this module NEVER
re-implements them, so the archived bytes are bit-for-bit the same bytes the
hash was taken over (the offline-verify invariant).

**DYNAMIC.** The archive root + the enabled flag are injected
(:class:`RawPayloadArchiveConfig`), so a test / operator can point the archive
anywhere and toggle it without touching call sites.

**Best-effort (never halts/slows the pull).** The archive write rides the SAME
``on_raw_payload`` seam as the hash capture and SWALLOWS every error (a
malformed payload or an ``OSError``), so it can never halt or corrupt a pull —
the archive is additive PROOF, not a gate (mirrors
:func:`~brutex.data.raw_integrity_store.make_capture_hook`).

**Honest complexity (Golden Rule #10).** O(payload) per window write (one
canonicalise + one gzip compress + one file write); O(payload) per offline read
(one decompress + one hash). It does NOT retroactively archive windows pulled
BEFORE the archiver was enabled — only windows captured while enabled exist on
disk; :func:`verify_archived_record` honestly reports a never-archived window as
:attr:`ArchiveVerifyOutcome.MISSING`, never a silent pass.

* :class:`RawPayloadArchiveConfig` — the injectable ``(root, enabled)`` policy.
* :func:`archive_key` / :func:`archive_path` — the deterministic compressed key.
* :func:`archive_payload` — canonicalise + gzip + write one window's bytes.
* :func:`read_archived_canonical_bytes` — read back the verbatim canonical bytes.
* :func:`verify_archived_record` — offline re-verify a record against its archive.
* :func:`make_archive_hook` — the best-effort :data:`RawPayloadHook` (no-op when
  disabled) the live pull wires alongside the hash-capture hook.
"""

from __future__ import annotations

import gzip
import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import structlog

from brutex.data.raw_integrity import (
    RawIntegrityError,
    RawIntegrityRecord,
    canonicalize_payload,
)

#: Structured logger — surfaces a failed archive write / read into ``/logs``.
_LOG = structlog.get_logger("brutex.data.raw_payload_archive")

#: The archive directory under the archive root (NEVER under ``bars/`` /
#: ``options/`` — like the integrity sidecar it is proof ALONGSIDE the data, not
#: lake data, so no candle partition / lake scanner ever sees it).
ARCHIVE_DIRNAME: str = "raw_payload_archive"

#: The per-window archive file extension: compact canonical JSON, gzip-compressed.
ARCHIVE_SUFFIX: str = ".json.gz"

#: The environment flag that opts the archiver IN (default OFF — storage cost).
#: Truthy values (case-insensitive): ``1`` / ``true`` / ``yes`` / ``on``.
ARCHIVE_ENV_FLAG: str = "BRUTEX_RAW_PAYLOAD_ARCHIVE"

#: The set of (lower-cased) environment-flag values that mean "enabled".
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _env_enabled(environ: Mapping[str, str]) -> bool:
    """Return whether *environ* opts the archiver in via :data:`ARCHIVE_ENV_FLAG`."""
    return environ.get(ARCHIVE_ENV_FLAG, "").strip().lower() in _TRUTHY


@dataclass(frozen=True, slots=True)
class RawPayloadArchiveConfig:
    """The injectable archive policy — WHERE to archive and WHETHER to.

    Attributes
    ----------
    root
        The archive root directory; the per-window files live under
        ``<root>/raw_payload_archive/`` (:data:`ARCHIVE_DIRNAME`). Typically the
        lake root, so the archive sits beside (NEVER inside) ``bars/``.
    enabled
        ``True`` to actually write archives; ``False`` (the DEFAULT — storage
        cost) makes :func:`make_archive_hook` a no-op.
    """

    root: Path
    enabled: bool = False

    @classmethod
    def from_env(
        cls, root: Path, *, environ: Mapping[str, str] | None = None
    ) -> RawPayloadArchiveConfig:
        """Build a config whose ``enabled`` reads :data:`ARCHIVE_ENV_FLAG`.

        *environ* is injectable for deterministic tests (``None`` →
        :data:`os.environ`). The archiver stays OFF unless the flag is explicitly
        truthy, so production defaults to no archive (Golden Rule #10 honest
        storage envelope).
        """
        env = environ if environ is not None else os.environ
        return cls(root=root, enabled=_env_enabled(env))


def archive_dir(config: RawPayloadArchiveConfig) -> Path:
    """Return the archive directory (``<config.root>/raw_payload_archive``)."""
    return config.root / ARCHIVE_DIRNAME


def archive_key(
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    window_start: str,
    window_end: str,
) -> str:
    """Return the DETERMINISTIC archive key (hex) for one window's coordinates.

    The key is :func:`hashlib.sha256` over the six coordinate strings joined by a
    NUL byte (a separator that can never appear inside a coordinate), so two
    windows differing in ANY coordinate map to different files and the SAME
    coordinates always map to the SAME file (idempotent re-archive overwrites in
    place). The hex digest is filesystem-safe regardless of the symbol's
    characters (e.g. an option symbol's ``/`` or an underlying's ``&``), which a
    raw-coordinate path could not guarantee. O(coordinates) — a fixed six-field
    hash, independent of payload size.
    """
    joined = "\x00".join((exchange, segment, groww_symbol, interval, window_start, window_end))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def archive_path(
    config: RawPayloadArchiveConfig,
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    window_start: str,
    window_end: str,
) -> Path:
    """Return the per-window archive file path for the given coordinates.

    ``<config.root>/raw_payload_archive/<archive_key><ARCHIVE_SUFFIX>`` — the
    deterministic compressed-key path (:func:`archive_key` +
    :data:`ARCHIVE_SUFFIX`).
    """
    key = archive_key(
        exchange=exchange,
        segment=segment,
        groww_symbol=groww_symbol,
        interval=interval,
        window_start=window_start,
        window_end=window_end,
    )
    return archive_dir(config) / f"{key}{ARCHIVE_SUFFIX}"


def archive_payload(
    config: RawPayloadArchiveConfig,
    payload: Mapping[str, object],
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    window_start: str,
    window_end: str,
) -> Path:
    """Canonicalise + gzip-compress + write *payload*'s verbatim bytes (one window).

    Writes the COMMON :func:`~brutex.data.raw_integrity.canonicalize_payload`
    bytes (the EXACT bytes the integrity hash is taken over —
    :func:`~brutex.data.raw_integrity.hash_payload`) gzip-compressed to
    :func:`archive_path`, so an offline read can recompute the same hash. The
    write is DETERMINISTIC: gzip ``mtime=0`` so the compressed bytes depend only
    on the canonical payload (a golden-stable file). Creates the archive
    directory if absent and overwrites an existing same-key file in place
    (idempotent re-archive). O(payload) — one canonicalise + one compress + one
    write.

    Returns
    -------
    Path
        The archive file written.

    Raises
    ------
    RawIntegrityError
        Propagated from :func:`canonicalize_payload` for a malformed payload (the
        best-effort hook in :func:`make_archive_hook` catches it).
    OSError
        On a write failure (disk full / permission) — also caught by the hook.
    """
    canonical = canonicalize_payload(payload)
    directory = archive_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    dest = archive_path(
        config,
        exchange=exchange,
        segment=segment,
        groww_symbol=groww_symbol,
        interval=interval,
        window_start=window_start,
        window_end=window_end,
    )
    # mtime=0 → the gzip header carries no wall-clock, so identical canonical
    # bytes always produce identical compressed bytes (deterministic golden).
    compressed = gzip.compress(canonical, mtime=0)
    dest.write_bytes(compressed)
    return dest


def read_archived_canonical_bytes(
    config: RawPayloadArchiveConfig,
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    window_start: str,
    window_end: str,
) -> bytes | None:
    """Read back the VERBATIM canonical bytes for one archived window.

    Decompresses the per-window archive file (:func:`archive_path`) and returns
    the exact :func:`~brutex.data.raw_integrity.canonicalize_payload` bytes that
    were archived, or ``None`` when the window was never archived (a missing file
    — the honest "no archive on disk" state, e.g. a window pulled before the
    archiver was enabled). O(payload) — one read + one decompress.

    Raises
    ------
    OSError
        On a read failure of an EXISTING file (e.g. a corrupt gzip member raises
        the gzip ``BadGzipFile``, an ``OSError`` subclass) — surfaced to the
        caller (a verify reports it inconclusive, never a silent pass).
    """
    dest = archive_path(
        config,
        exchange=exchange,
        segment=segment,
        groww_symbol=groww_symbol,
        interval=interval,
        window_start=window_start,
        window_end=window_end,
    )
    if not dest.is_file():
        return None
    return gzip.decompress(dest.read_bytes())


class ArchiveVerifyOutcome(Enum):
    """The verdict of offline-verifying a record against its archived bytes.

    Attributes
    ----------
    MATCH
        The archive exists and its recomputed hash EQUALS the record's
        ``payload_hash`` — provable lake == archived raw == recorded hash.
    MISMATCH
        The archive exists but its recomputed hash DIFFERS from the record's —
        the archive / record drift or tamper candidate.
    MISSING
        No archive file exists for the record's coordinates (never archived — the
        honest "no offline proof on disk" state, not a failure).
    """

    MATCH = "match"
    MISMATCH = "mismatch"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class ArchiveVerifyResult:
    """Outcome of :func:`verify_archived_record` for one window.

    Attributes
    ----------
    outcome
        The :class:`ArchiveVerifyOutcome` verdict.
    recorded_hash
        The hash stored in the :class:`~brutex.data.raw_integrity.RawIntegrityRecord`.
    archived_hash
        The hash recomputed from the archived bytes — ``None`` when the archive
        is :attr:`~ArchiveVerifyOutcome.MISSING`.
    key
        The record's integrity key (the coordinates verified).
    """

    outcome: ArchiveVerifyOutcome
    recorded_hash: str
    archived_hash: str | None
    key: tuple[str, str, str, str, str, str]

    @property
    def matches(self) -> bool:
        """``True`` iff the offline verdict is :attr:`~ArchiveVerifyOutcome.MATCH`."""
        return self.outcome is ArchiveVerifyOutcome.MATCH


def verify_archived_record(
    config: RawPayloadArchiveConfig, record: RawIntegrityRecord
) -> ArchiveVerifyResult:
    """Offline-verify *record* against its archived payload bytes (no vendor call).

    Reads the verbatim archived canonical bytes for *record*'s coordinates
    (:func:`read_archived_canonical_bytes`), recomputes the hash over EXACTLY
    those bytes with the SAME algorithm the record used
    (:func:`hashlib.blake2b`, matching
    :func:`~brutex.data.raw_integrity.hash_payload`), and compares it to
    :attr:`record.payload_hash`. The whole proof is OFFLINE — it re-verifies that
    the lake's recorded hash still matches the archived raw bytes WITHOUT
    re-fetching from Groww (the point of the archive). A
    :attr:`~ArchiveVerifyOutcome.MISSING` archive is reported honestly (the
    record was captured but its payload never archived — e.g. archiving was off
    at pull time), never a silent pass. O(payload) — one decompress + one hash.

    Raises
    ------
    OSError
        Propagated from :func:`read_archived_canonical_bytes` on a corrupt /
        unreadable EXISTING archive file (an inconclusive verification, never a
        silent pass).
    """
    canonical = read_archived_canonical_bytes(
        config,
        exchange=record.exchange,
        segment=record.segment,
        groww_symbol=record.groww_symbol,
        interval=record.interval,
        window_start=record.window_start,
        window_end=record.window_end,
    )
    if canonical is None:
        return ArchiveVerifyResult(
            outcome=ArchiveVerifyOutcome.MISSING,
            recorded_hash=record.payload_hash,
            archived_hash=None,
            key=record.key,
        )
    # Hash the verbatim archived bytes the SAME way hash_payload hashes the
    # canonical bytes (blake2b digest_size=32) — the offline-verify equivalence.
    archived_hash = hashlib.blake2b(canonical, digest_size=32).hexdigest()
    outcome = (
        ArchiveVerifyOutcome.MATCH
        if archived_hash == record.payload_hash
        else ArchiveVerifyOutcome.MISMATCH
    )
    return ArchiveVerifyResult(
        outcome=outcome,
        recorded_hash=record.payload_hash,
        archived_hash=archived_hash,
        key=record.key,
    )


def make_archive_hook(
    config: RawPayloadArchiveConfig,
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
) -> Callable[[Mapping[str, object], str, str, str], None]:
    """Return a BEST-EFFORT raw-payload ARCHIVE hook for one instrument's fetch.

    A :data:`~brutex.adapters.groww.candles.RawPayloadHook` the live pull wires
    ALONGSIDE the hash-capture hook (DEC-RAW-DATA-INTEGRITY-001). On each fetched
    window — when :attr:`config.enabled` — it archives the verbatim raw payload
    bytes (:func:`archive_payload`) for the bound
    ``(exchange, segment, groww_symbol)`` plus the per-call ``(interval,
    window_start, window_end)`` the hook receives.

    **DISABLED is a true no-op** (the DEFAULT — storage cost): when
    ``config.enabled`` is ``False`` the returned hook does NOTHING and writes no
    file, so the pull is byte-identical to the pre-feature path.

    **It can NEVER halt or corrupt a pull** (the hard requirement): every error —
    a malformed payload (:class:`~brutex.data.raw_integrity.RawIntegrityError`)
    or an archive write failure (:class:`OSError`) — is caught, logged at
    WARNING, then swallowed, so the candle fetch+store proceeds exactly as if the
    hook were absent.

    O(payload) per window when enabled (one canonicalise + compress + write); a
    constant no-op when disabled.
    """

    def _archive(
        payload: Mapping[str, object], interval: str, window_start: str, window_end: str
    ) -> None:
        if not config.enabled:
            return
        try:
            archive_payload(
                config,
                payload,
                exchange=exchange,
                segment=segment,
                groww_symbol=groww_symbol,
                interval=interval,
                window_start=window_start,
                window_end=window_end,
            )
        except (RawIntegrityError, OSError) as exc:
            # Best-effort: a bad payload / archive write failure must NEVER halt
            # or corrupt the pull (the candle write is the source of truth; the
            # archive is additive proof). Log + swallow so the fetch is unaffected.
            _LOG.warning(
                "raw_payload_archive.write_failed",
                groww_symbol=groww_symbol,
                interval=interval,
                window_start=window_start,
                window_end=window_end,
                error=str(exc),
            )

    return _archive


#: A per-symbol archive-hook factory (DEC-RAW-PAYLOAD-ARCHIVE-001) — the archive
#: twin of :data:`~brutex.data.raw_integrity_store.CaptureHookFactory`. Given the
#: RESOLVED ``(exchange, segment, groww_symbol)`` of ONE instrument's fetch, it
#: returns the :data:`~brutex.adapters.groww.candles.RawPayloadHook` to fire per
#: window for THAT instrument. One live adapter / ingest run pulls MANY symbols
#: through one object, so the live build sites inject this factory (bound to the
#: config) and the fetch paths call it with each resolved symbol.
ArchiveHookFactory = Callable[
    [str, str, str], Callable[[Mapping[str, object], str, str, str], None]
]


def make_archive_hook_factory(config: RawPayloadArchiveConfig) -> ArchiveHookFactory:
    """Return an :data:`ArchiveHookFactory` bound to *config*.

    The factory takes the RESOLVED ``(exchange, segment, groww_symbol)`` of one
    fetch and returns a best-effort :func:`make_archive_hook` for it — so the
    live spot adapter and option ingest, which each pull MANY symbols through ONE
    object, can build a correctly per-symbol-attributed archive hook at the call
    site where the symbol is resolved (mirrors
    :func:`~brutex.data.raw_integrity_store.make_capture_hook_factory`). Every
    returned per-symbol hook is best-effort + a no-op when ``config.enabled`` is
    ``False``, so wiring the factory can never make a pull fail or write when
    disabled.
    """

    def _factory(
        exchange: str, segment: str, groww_symbol: str
    ) -> Callable[[Mapping[str, object], str, str, str], None]:
        return make_archive_hook(
            config, exchange=exchange, segment=segment, groww_symbol=groww_symbol
        )

    return _factory


__all__ = [
    "ARCHIVE_DIRNAME",
    "ARCHIVE_ENV_FLAG",
    "ARCHIVE_SUFFIX",
    "ArchiveHookFactory",
    "ArchiveVerifyOutcome",
    "ArchiveVerifyResult",
    "RawPayloadArchiveConfig",
    "archive_dir",
    "archive_key",
    "archive_path",
    "archive_payload",
    "make_archive_hook",
    "make_archive_hook_factory",
    "read_archived_canonical_bytes",
    "verify_archived_record",
]
