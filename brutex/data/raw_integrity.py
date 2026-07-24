"""Raw-data integrity — a provable hash of EXACTLY what Groww sent.

DEC-RAW-DATA-INTEGRITY-001. The lake stores OHLC bars that are a DETERMINISTIC
function of the raw vendor payload (2-decimal ``ROUND_HALF_UP`` snap +
``high``/``low`` clamp + IST→UTC + non-negative count clamps + skip-empty +
keep-first), but until now nothing recorded WHAT the vendor actually sent — the
:class:`~brutex.data.parquet_writer.ParquetWriter`'s ``raw_hash`` is a
``sha256`` of the STORED Parquet *file* bytes, which proves only that the file
has not changed on disk, NOT that the file derives from the un-tampered vendor
response (the gap this module closes).

This module is the PURE core of the integrity layer (CLAUDE.md §11 COMMON — one
source of truth; the ingest spine wires a thin best-effort capture hook around
it):

* :func:`canonicalize_payload` — serialise a raw Groww candle payload to a
  deterministic, reproducible byte string (the ``candles`` row-of-arrays as
  received, JSON with sorted keys + no whitespace), so the hash is stable across
  processes / Python builds.
* :func:`hash_payload` — :func:`hashlib.blake2b` over the canonical bytes (the
  repo's run-identity hash is ``blake3`` per CLAUDE.md Golden Rule #4; ``blake3``
  is a declared dependency but is not guaranteed importable in every pure-python
  environment, so this PURE module uses the stdlib ``blake2b`` — no native wheel,
  always available, cryptographically strong, deterministic).
* :class:`RawIntegrityRecord` — the immutable sidecar record keyed by
  ``(exchange, segment, groww_symbol, interval, window_start, window_end)`` plus
  the recorded ``payload_hash`` + ``row_count`` + ``captured_at``.
* :func:`build_record` — capture a record from a raw payload + its coordinates
  (the function the ingest hook calls).
* :func:`verify_record` — re-hash a freshly-(re)fetched payload and compare it to
  a recorded :class:`RawIntegrityRecord`, returning a :class:`VerifyResult` —
  the provable "the data has not drifted since capture" check.

**Honest envelope (Golden Rule #10).** This proves that (a) the RECORDED hash
matches the raw payload AT CAPTURE TIME and (b) the lake is a deterministic
function of that exact payload — so any later tampering or drift of a re-fetched
window is caught by a hash mismatch. It does NOT retroactively prove
already-stored lake data: no historical raw payloads exist for windows pulled
before this layer shipped, so :func:`verify_record` can only attest windows
captured from this point forward. It also does not (yet) archive the verbatim
payload for full offline replay — that is a designed FUTURE extension
(``docs/observability/RAW_DATA_INTEGRITY.md``), gated separately because storing
every raw payload is heavy.

Purely additive: it imports nothing from the parser / writer value path and
changes no stored bar. O(payload) to canonicalize + hash one window; O(1) to
compare two recorded hashes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from brutex.data.detectors_rust import raw_integrity_rust_enabled, routed_hash_payload_hex

#: The integrity-hash algorithm identifier stamped on every record so a future
#: migration (e.g. to a native ``blake3`` once the wheel is guaranteed) can
#: distinguish hash families without re-hashing — the record self-describes its
#: algorithm. ``blake2b`` is stdlib (no native dependency), cryptographically
#: strong, and deterministic across processes / Python builds.
HASH_ALGORITHM: str = "blake2b"

#: Digest size (bytes) → a 64-hex-char digest, matching the existing
#: ``sha256`` ``raw_hash`` width so the two coexist legibly in the same record
#: family without a column-width surprise.
_DIGEST_SIZE: int = 32

#: The candle-rows key in a Groww historical-candle payload (the row-of-arrays
#: ``{"candles": [[ts, o, h, l, c, v, oi], ...]}`` — see
#: :mod:`brutex.adapters.groww.parser`). The integrity hash is computed over the
#: CANONICALISED candle rows ONLY (not the whole mapping), so vendor-side
#: envelope noise (a status field, a server timestamp) can never flip the hash of
#: an identical candle series; the candle rows ARE the data we store.
_CANDLES_KEY: str = "candles"


class RawIntegrityError(ValueError):
    """A raw payload cannot be canonicalised (malformed / non-serialisable).

    A subclass of :class:`ValueError` so an ingest hook's narrow ``except
    (RawIntegrityError, ...)`` guard never accidentally swallows a different
    error class. The capture path catches this best-effort so a malformed
    payload can never halt or corrupt a pull (the hash is additive proof, not a
    gate).
    """


def canonicalize_payload(payload: Mapping[str, object]) -> bytes:
    """Serialise a raw Groww candle payload to deterministic, reproducible bytes.

    The hash input is the payload's ``candles`` row-of-arrays (the exact data the
    lake derives from), serialised as compact JSON with **sorted keys** and **no
    whitespace** so two byte-identical candle series always canonicalise to the
    same bytes regardless of dict-ordering or whitespace in the original
    response. A missing ``candles`` key canonicalises an empty list (``[]``) — a
    genuinely-empty vendor window is a real, hashable state, not an error.

    Parameters
    ----------
    payload
        A mapping carrying a ``"candles"`` key (the Groww
        ``get_historical_candles`` response shape). Any non-mapping or a
        non-serialisable ``candles`` value raises :class:`RawIntegrityError`.

    Returns
    -------
    bytes
        The canonical UTF-8 JSON encoding of the candle rows.

    Raises
    ------
    RawIntegrityError
        If *payload* is not a mapping, or its ``candles`` value cannot be
        JSON-serialised (e.g. it contains a non-JSON object).
    """
    if not isinstance(payload, Mapping):
        raise RawIntegrityError(f"raw payload must be a mapping, got {type(payload).__name__}")
    candles = payload.get(_CANDLES_KEY, [])
    try:
        # sort_keys for nested dict stability + the compact separators for
        # whitespace-free, ordering-stable output (default=None → a non-JSON
        # value raises TypeError, mapped to RawIntegrityError below).
        text = json.dumps(candles, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RawIntegrityError(f"raw payload candles are not JSON-serialisable: {exc}") from exc
    return text.encode("utf-8")


def _hash_canonical_python(canonical: bytes) -> str:
    """The verbatim digest law over already-canonical bytes — the LIVE parity oracle.

    Extracted token-for-token from :func:`hash_payload` for the
    DEC-DATA-RUST-INTEGRITY-DETECTORS-001 routing (SLICE T24 PR-1); flag OFF
    (the default) runs exactly this expression, byte-identical to the
    pre-facade law.
    """
    return hashlib.blake2b(canonical, digest_size=_DIGEST_SIZE).hexdigest()


def hash_payload(payload: Mapping[str, object]) -> str:
    """Return the :data:`HASH_ALGORITHM` hex digest of *payload*'s canonical bytes.

    Determinism (CLAUDE.md §2 Rule 7): the SAME candle series always yields the
    SAME digest; a one-byte change anywhere in any candle row yields a DIFFERENT
    digest (tamper detection). O(payload) — one canonicalise + one hash pass.

    Routing (DEC-DATA-RUST-INTEGRITY-DETECTORS-001, SLICE T24 PR-1): the
    DIGEST leg is served by :func:`_hash_canonical_python` — the verbatim
    ``hashlib.blake2b`` expression, the LIVE parity oracle — via the
    default-ON ``BRUTEX_LAKE_RUST_RAW_INTEGRITY`` facade
    (:mod:`brutex.data.detectors_rust`, an in-crate RFC 7693 blake2b). The
    CANONICALISATION stays Python on BOTH arms (``json.dumps`` is CPython's
    ``float.__repr__`` byte law — raw vendor candles carry float prices;
    never re-derived in Rust, the stats-shard ruling). Flag OFF (the
    default) returns the oracle's digest directly — the crate is never
    imported, byte-identical to the pre-facade law.
    """
    canonical = canonicalize_payload(payload)
    if not raw_integrity_rust_enabled():
        return _hash_canonical_python(canonical)
    return routed_hash_payload_hex(canonical, python_law=lambda: _hash_canonical_python(canonical))


def _row_count(payload: Mapping[str, object]) -> int:
    """Best-effort count of raw candle rows in *payload* (0 for absent/non-list).

    This is the RAW row count (before the empty-bar / session filter), recorded
    alongside the hash as a cheap cross-check. A non-list ``candles`` value is
    reported as 0 — the hash (over the canonical bytes) is the authoritative
    signal; the count is an informational companion only.
    """
    candles = payload.get(_CANDLES_KEY)
    if isinstance(candles, (list, tuple)):
        return len(candles)
    return 0


@dataclass(frozen=True, slots=True)
class RawIntegrityRecord:
    """An immutable per-window proof of EXACTLY what the vendor sent.

    Keyed by the fetch coordinates so a stored window can be looked up + verified
    later. The :attr:`payload_hash` is the cryptographic content hash of the raw
    vendor candle rows at capture time (:func:`hash_payload`); a later re-fetch
    that produces a different hash proves the window has drifted / been tampered
    with (:func:`verify_record`).

    Attributes
    ----------
    exchange, segment, groww_symbol, interval
        The vendor fetch coordinates (e.g. ``"NSE"`` / ``"CASH"`` /
        ``"NSE-NIFTY"`` / ``"1minute"``).
    window_start, window_end
        The window bounds AS SENT to the vendor (the exact ``start`` / ``end``
        strings :meth:`~brutex.adapters.groww.client.GrowwClient.historical_candles`
        was called with), so the verify path can re-issue the byte-identical call.
    payload_hash
        The :data:`HASH_ALGORITHM` hex digest of the canonical raw candle rows.
    row_count
        The raw candle-row count at capture (informational cross-check).
    algorithm
        The hash family identifier (:data:`HASH_ALGORITHM`) — self-describing so a
        future hash migration can coexist.
    captured_at
        UTC timestamp the record was built (provenance; never part of the hash).
    issued_symbol
        The symbol form ACTUALLY SENT to the vendor when it differs from
        :attr:`groww_symbol` (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001): a ``%26``
        fallback recovery fetches with the ENCODED form while the record stays
        keyed under the plain symbol, so without this field the verify path would
        re-issue the PLAIN form and flag a guaranteed false ``verify_mismatch``.
        ``None`` (the default, and every pre-field legacy record) means the issued
        form IS the key symbol — see :attr:`fetch_symbol`. NEVER part of
        :attr:`key` (latest-wins per plain-symbol coordinates is preserved).
    """

    exchange: str
    segment: str
    groww_symbol: str
    interval: str
    window_start: str
    window_end: str
    payload_hash: str
    row_count: int
    algorithm: str = HASH_ALGORITHM
    captured_at: datetime | None = None
    issued_symbol: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        """The integrity key — the 6-tuple of fetch coordinates (O(1))."""
        return (
            self.exchange,
            self.segment,
            self.groww_symbol,
            self.interval,
            self.window_start,
            self.window_end,
        )

    @property
    def fetch_symbol(self) -> str:
        """The symbol form to RE-ISSUE the byte-identical vendor call with (O(1)).

        The :attr:`issued_symbol` when the capture recorded a divergent form (the
        ``%26`` fallback recovery), else the key :attr:`groww_symbol` — so a legacy
        record (no field) verifies exactly as before
        (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 verify-path correction).
        """
        return self.issued_symbol or self.groww_symbol


def build_record(
    payload: Mapping[str, object],
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    window_start: str,
    window_end: str,
    now: datetime | None = None,
    issued_symbol: str | None = None,
) -> RawIntegrityRecord:
    """Capture a :class:`RawIntegrityRecord` from a raw payload + its coordinates.

    The function the ingest hook calls at the point the raw vendor response is in
    hand (BEFORE any parse / transform), so the recorded hash attests EXACTLY
    what the vendor sent. *now* is injectable for deterministic tests (``None`` →
    :func:`datetime.now` in UTC). *issued_symbol* is the symbol form the fetch
    was ACTUALLY issued with (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001) —
    NORMALISED to ``None`` when it equals *groww_symbol*, so the common
    plain-form record (and its sidecar JSON line) stays byte-identical to the
    pre-field format; only a genuinely divergent form (the ``%26`` fallback) is
    recorded.

    Raises
    ------
    RawIntegrityError
        Propagated from :func:`canonicalize_payload` for a malformed payload —
        the caller (best-effort capture hook) catches it so a bad payload can
        never halt a pull.
    """
    return RawIntegrityRecord(
        exchange=exchange,
        segment=segment,
        groww_symbol=groww_symbol,
        interval=interval,
        window_start=window_start,
        window_end=window_end,
        payload_hash=hash_payload(payload),
        row_count=_row_count(payload),
        algorithm=HASH_ALGORITHM,
        captured_at=now if now is not None else datetime.now(UTC),
        issued_symbol=(issued_symbol if issued_symbol != groww_symbol else None),
    )


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of comparing a recorded hash to a re-fetched payload's hash.

    Attributes
    ----------
    matches
        ``True`` iff the recorded hash equals the freshly-computed hash — the
        provable "no drift since capture" verdict.
    recorded_hash
        The hash stored in the :class:`RawIntegrityRecord`.
    actual_hash
        The hash of the payload presented to :func:`verify_record`.
    key
        The record's integrity key (the coordinates verified).
    """

    matches: bool
    recorded_hash: str
    actual_hash: str
    key: tuple[str, str, str, str, str, str]


def verify_record(record: RawIntegrityRecord, payload: Mapping[str, object]) -> VerifyResult:
    """Verify a re-fetched *payload* against a recorded :class:`RawIntegrityRecord`.

    Re-hashes *payload* (the same canonicalisation as capture) and compares it to
    :attr:`record.payload_hash`. A match proves the window's raw vendor data has
    not drifted since the record was captured; a mismatch flags tampering /
    vendor restatement / a wrong window. Pure + O(payload) — the re-fetch (live
    SDK call or archived-payload replay) is the caller's thin layer; this is the
    deterministic hash+compare core.

    Raises
    ------
    RawIntegrityError
        Propagated from :func:`canonicalize_payload` if *payload* is malformed
        (the verify caller surfaces it as an inconclusive verification, never a
        silent pass).
    """
    actual = hash_payload(payload)
    return VerifyResult(
        matches=actual == record.payload_hash,
        recorded_hash=record.payload_hash,
        actual_hash=actual,
        key=record.key,
    )


__all__ = [
    "HASH_ALGORITHM",
    "RawIntegrityError",
    "RawIntegrityRecord",
    "VerifyResult",
    "build_record",
    "canonicalize_payload",
    "hash_payload",
    "verify_record",
]
