"""Raw-data integrity VERIFY path — re-fetch a window + compare its hash.

DEC-RAW-DATA-INTEGRITY-001. The capture side
(:func:`~brutex.data.raw_integrity_store.make_capture_hook`) records a hash of
EXACTLY what the vendor sent for each fetched window. This module is the verify
counterpart: given a recorded :class:`~brutex.data.raw_integrity.RawIntegrityRecord`,
RE-FETCH the byte-identical window from Groww and re-hash the response — a match
proves the window's raw vendor data has not drifted / been tampered with since
capture; a mismatch flags it.

The PURE hash+compare core lives in
:func:`~brutex.data.raw_integrity.verify_record` (no network, fully unit-tested);
this module is the THIN re-fetch layer over the injected
:class:`~brutex.adapters.groww.client.GrowwClientProtocol` (the same single
network surface the pull uses), so the only Groww-specific knowledge — re-issuing
``historical_candles`` with the recorded coordinates — lives in one place.

* :func:`verify_one` — re-fetch + verify ONE recorded window (a
  :class:`~brutex.data.raw_integrity.VerifyResult`).
* :func:`verify_lake_records` — load every sidecar record for a lake root and
  verify each, returning a :class:`VerifyRun` summary (the ``brutex
  verify-integrity`` core; a CLI is a thin wrapper over this).

**Honest envelope (Golden Rule #10).** A clean verify proves the recorded hash
still matches a fresh re-fetch — i.e. the vendor still serves the same raw bytes
AND the recorded proof is intact. It cannot attest a window that was never
captured (pre-feature data — :attr:`VerifyRun.missing`), nor distinguish a
legitimate vendor RESTATEMENT from tampering (both surface as a mismatch — the
operator investigates). The re-fetch is network-bound, O(records) total — a
diagnostic / audit tool, never a hot path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from brutex.adapters.groww.client import GrowwClientProtocol
from brutex.data.raw_integrity import RawIntegrityRecord, VerifyResult, verify_record
from brutex.data.raw_integrity_store import load_records

#: Structured logger — surfaces verify mismatches into ``/logs``.
_LOG = structlog.get_logger("brutex.adapters.groww.integrity_verify")


def verify_one(client: GrowwClientProtocol, record: RawIntegrityRecord) -> VerifyResult:
    """Re-fetch *record*'s window from Groww + verify its hash (one window).

    Re-issues :meth:`GrowwClientProtocol.historical_candles` with the EXACT
    coordinates the record stored (``exchange`` / ``segment`` / ``groww_symbol`` /
    ``interval`` / ``window_start`` / ``window_end`` — the bounds are the same ISO
    strings the original call used), then delegates to the pure
    :func:`~brutex.data.raw_integrity.verify_record`. A
    :class:`~brutex.data.raw_integrity.RawIntegrityError` on a malformed re-fetched
    payload propagates (an inconclusive verification surfaces, never a silent
    pass). The vendor call may raise its own
    :class:`~brutex.adapters.groww.errors.GrowwError` — that propagates to the
    caller (the CLI wrapper decides how to report a re-fetch failure).
    """
    payload = client.historical_candles(
        exchange=record.exchange,
        segment=record.segment,
        groww_symbol=record.groww_symbol,
        interval=record.interval,
        start=record.window_start,
        end=record.window_end,
    )
    return verify_record(record, payload)


@dataclass(frozen=True, slots=True)
class VerifyRun:
    """Summary of verifying every recorded window for a lake root.

    Attributes
    ----------
    total
        Number of recorded windows examined.
    matched
        Windows whose re-fetched hash equalled the recorded hash (no drift).
    mismatched
        :class:`~brutex.data.raw_integrity.VerifyResult` for every window whose
        re-fetched hash DIFFERED — the drift / tamper / restatement candidates.
    """

    total: int
    matched: int
    mismatched: tuple[VerifyResult, ...] = field(default_factory=tuple)

    @property
    def all_matched(self) -> bool:
        """``True`` iff every examined window verified clean (no mismatches)."""
        return not self.mismatched


def verify_lake_records(client: GrowwClientProtocol, lake_root: Path) -> VerifyRun:
    """Verify EVERY sidecar-recorded window for *lake_root* — the audit entry point.

    Loads the integrity sidecar (:func:`~brutex.data.raw_integrity_store.load_records`),
    re-fetches + verifies each recorded window (:func:`verify_one`), and returns a
    :class:`VerifyRun` summary. A mismatch is logged at WARNING (naming the key +
    both hashes) so a drift is visible in ``/logs`` as well as the return value.
    An empty sidecar yields a zero :class:`VerifyRun` (``total=0``, ``all_matched``
    trivially true — there is nothing to disprove). O(records) network re-fetches;
    a diagnostic tool, never a hot path.
    """
    records = load_records(lake_root)
    matched = 0
    mismatched: list[VerifyResult] = []
    for record in records.values():
        result = verify_one(client, record)
        if result.matches:
            matched += 1
        else:
            mismatched.append(result)
            _LOG.warning(
                "raw_integrity.verify_mismatch",
                key=list(result.key),
                recorded_hash=result.recorded_hash,
                actual_hash=result.actual_hash,
            )
    return VerifyRun(total=len(records), matched=matched, mismatched=tuple(mismatched))


__all__ = ["VerifyRun", "verify_lake_records", "verify_one"]
