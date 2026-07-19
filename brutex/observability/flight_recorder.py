"""Per-run hash-chained flight recorder (R41/R43, DEC-SWEEP-UNKILLABLE-CORE-001).

Every material sweep-run event (stage start/end, tuple done, tripwire, retry,
quarantine, refusal, checkpoint, milestone, halt, resume) becomes ONE
timestamped, run-id-stamped, hash-chained record in
``<results_root>/runs/<run_id>/flight-recorder.jsonl`` (D-15). The chain core
is the COMMON :class:`brutex.orchestrator.audit.AuditWriter` (SHA-256, 8-key
line, ``resume=True`` torn-tail law — D-2, CLAUDE.md §11 COMMON): NO third
chain implementation is minted here, and the matching verifier is
:func:`brutex.audit_verify.auto_verify` via :func:`verify_recorder_chain`.

Line shape (one :class:`~brutex.orchestrator.audit.AuditEntry` per record)::

    {"seq": 0, "prev_hash": "000…0", "ts_utc": 1789…, "actor": "flight",
     "action": "<KIND>", "subject": "<run_id>", "payload": {…},
     "entry_hash": "<sha256-64hex>"}

The P2→P4 events contract (``{ts_utc, run_id, kind, payload}``) is a
documented SUPERSET mapping of that line — ``kind = action``, ``run_id =
subject`` (D-7) — materialised by :func:`contract_view`.

Fail-soft law (I-3, D-20)
-------------------------
A recorder failure NEVER changes sweep results and NEVER raises into compute:

* the FIRST durable-append failure emits ONE ``flight_recorder.append_failed``
  WARN, latches :attr:`FlightRecorder.degraded`, and counts the drop; every
  later record is silently dropped + counted (no per-record retry spam on a
  dead disk — the latch never clears within a run);
* an :meth:`FlightRecorder.open` failure latches degraded immediately (ONE
  ``flight_recorder.open_failed`` WARN takes the place of the append WARN);
* a per-record payload defect (``NaN``/``Infinity`` → ``AuditPayloadError``,
  a ``|``-bearing field → ``AuditFieldError``) drops ONLY that record with a
  ``flight_recorder.payload_rejected`` WARN and does NOT latch — the disk is
  healthy, so later records still append;
* an unknown ``kind`` is dropped + counted with a
  ``flight_recorder.unknown_kind`` WARN (a deterministic code bug the tests
  catch; per I-3 it must not crash a live run).

TUPLE_DONE decimation (D-9, lossless)
-------------------------------------
On a 100k-tuple grid an fsync per tuple is the dominant recorder cost, so
``TUPLE_DONE`` records may be decimated (``tuple_record_every=N`` records the
1st, N+1st, 2N+1st … call). Decimation is LOSSLESS because every recorded
``TUPLE_DONE`` payload carries the CUMULATIVE ``{done_cum, skipped_cum,
failed_cum}`` counters — the R39 count-reconciliation tripwire reads
``SweepProgress`` counters exactly, never recorder record counts. A decimation
skip returns ``False`` but is NOT a drop (``dropped`` stays untouched). Stage,
milestone and failure records are never decimated.

Honest envelope (Golden Rule #10): :meth:`FlightRecorder.record` is O(1) per
record (bench-pinned flat vs chain length); :func:`iter_run_records` /
:func:`search_timeline` / :func:`verify_recorder_chain` are O(records) —
bounded forensics, never claimed O(1). Timestamps (``ts_utc``) are
observability-only and never part of any run identity (I-2).
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

import structlog

from brutex.data.guarded_read import open_regular_binary
from brutex.engine.run_layout import run_dir
from brutex.orchestrator.audit import (
    AuditEntry,
    AuditFieldError,
    AuditPayloadError,
    AuditWriter,
    iter_entries,
)

_LOG = structlog.get_logger(__name__)

__all__ = [
    "FLIGHT_ACTOR",
    "FLIGHT_RECORDER_FILENAME",
    "MILESTONE_PCTS",
    "RECORD_KINDS",
    "FlightRecorder",
    "contract_view",
    "iter_run_records",
    "recorder_path",
    "search_timeline",
    "verify_recorder_chain",
]

#: The ``actor`` stamped on every flight-recorder chain line (D-7 superset map).
FLIGHT_ACTOR: Final[str] = "flight"

#: The flight-recorder file name inside ``runs/<run_id>/`` (D-15 path law).
FLIGHT_RECORDER_FILENAME: Final[str] = "flight-recorder.jsonl"

#: The closed record-kind vocabulary (design §2). ``record`` drops (fail-soft,
#: counted + WARNed) any kind outside this set — never a silent typo'd stream.
RECORD_KINDS: Final[frozenset[str]] = frozenset(
    {
        "RUN_START",
        "STAGE_START",
        "STAGE_END",
        "TUPLE_DONE",
        "TUPLE_FAILED",
        "TUPLE_SKIPPED",
        "CHECKPOINT",
        "QUARANTINE",
        "REFUSAL",
        "RETRY",
        "TRIPWIRE",
        "MILESTONE",
        "DIFFCHECK_OK",
        "DIFFCHECK_MISMATCH",
        "SUSPECT",
        "HALT",
        "RESUME",
        "MANIFEST_WRITTEN",
        "BUNDLE_WRITTEN",
        "RUN_END",
    }
)

#: The Telegram-milestone percent edges (R41: start/25/50/75/done — start and
#: done ride RUN_START / RUN_END; only these interior edges are MILESTONEs).
MILESTONE_PCTS: Final[tuple[int, ...]] = (25, 50, 75)


def recorder_path(results_root: Path, run_id: str) -> Path:
    """Return the flight-recorder path — ``runs/<run_id>/flight-recorder.jsonl``.

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity (``grid_identity`` BLAKE3 hex, D-1).

    Returns
    -------
    Path
        ``<results_root>/runs/<run_id>/flight-recorder.jsonl`` (D-15).
    """
    return run_dir(results_root, run_id) / FLIGHT_RECORDER_FILENAME


def recorder_has_suspect(results_root: Path, run_id: str) -> bool:
    """Return ``True`` iff the durable flight recorder holds a ``SUSPECT`` record.

    The D-12 never-launder belt (MAJOR round-2 fix): resume reads the sticky
    SUSPECT flag primarily from ``exit-status.json``, but that single fail-soft
    artifact is written after the in-memory latch — a SIGKILL or a torn write
    in the narrow window could leave it ``suspect=false``. The
    hash-chained ``flight-recorder.jsonl`` is appended (across attempts) with a
    durable ``SUSPECT`` record inside every :meth:`RunCoreHandles.mark_suspect`
    fan-out, so folding this into ``prior_suspect`` means a durably-recorded
    SUSPECT can NEVER be laundered back to a clean COMPLETE on resume.

    Fail-soft (I-3): a missing file / unreadable line / malformed JSON returns
    ``False`` for that line and never raises — the exit-status flag remains the
    other (independent) authority, so a read glitch here can only fail to add a
    belt, never fabricate a SUSPECT.

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity (D-1).

    Returns
    -------
    bool
        ``True`` iff at least one recorded line has ``action == "SUSPECT"``.
    """
    path = recorder_path(results_root, run_id)
    try:
        with io.TextIOWrapper(open_regular_binary(path), encoding="utf-8") as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except (ValueError, TypeError):
                    continue
                if isinstance(record, Mapping) and record.get("action") == "SUSPECT":
                    return True
    except OSError:
        return False
    return False


class FlightRecorder:
    """Single-writer, fail-soft, hash-chained per-run event recorder.

    One instance per run per process, guarded by an internal
    :class:`threading.Lock` (the underlying :class:`AuditWriter` is
    single-writer by contract; cross-PROCESS exclusivity is the run core's G6
    flock, not this lock). Construct via :meth:`open`; never raises out of
    :meth:`record` / :meth:`milestone_pct` / :meth:`close` (I-3, D-20).
    """

    def __init__(
        self,
        *,
        writer: AuditWriter | None,
        run_id: str,
        attempt: int,
        tuple_record_every: int,
    ) -> None:
        """Bind an already-opened chain writer (tests / :meth:`open` only).

        Parameters
        ----------
        writer
            The open :class:`AuditWriter`, or ``None`` for a born-degraded
            recorder (the :meth:`open` failure path).
        run_id
            The run identity stamped as every record's ``subject``.
        attempt
            The observability-only attempt ordinal stamped into every payload.
        tuple_record_every
            ``TUPLE_DONE`` decimation stride (``>= 1``; ``1`` = record all).
        """
        self._writer = writer
        self._run_id = run_id
        self._attempt = attempt
        self._tuple_every = tuple_record_every
        self._tuple_seen = 0
        self._degraded = writer is None
        self._dropped = 0
        self._milestones: set[int] = set()
        self._lock = threading.Lock()

    @classmethod
    def open(
        cls,
        results_root: Path,
        *,
        run_id: str,
        attempt: int,
        tuple_record_every: int | None = None,
    ) -> FlightRecorder:
        """Open (or resume) the run's flight recorder — never raises on I/O.

        The underlying chain is ``AuditWriter.open(path, actor="flight",
        resume=<path exists>)`` — an existing file is resumed from its last
        committed entry with the torn-tail truncation law (D-2). An open
        failure (read-only dir, dead disk) returns a BORN-DEGRADED recorder
        (one ``flight_recorder.open_failed`` WARN; every record is then a
        counted silent drop) so binding the recorder can never kill a run
        (I-3).

        Parameters
        ----------
        results_root
            The sweep results root (``runs/<run_id>/`` is created if needed).
        run_id
            The run identity (D-1) — every record's ``subject``.
        attempt
            Observability-only attempt ordinal (``>= 1``), stamped into every
            record payload; NEVER part of any identity (D-1).
        tuple_record_every
            ``TUPLE_DONE`` decimation stride; ``None`` means ``1`` (record
            every tuple). Values ``< 1`` are a launch-time configuration
            defect and raise ``ValueError`` (fail-loud at launch is a
            law-sanctioned refusal, I-3).

        Returns
        -------
        FlightRecorder
            An open (or born-degraded) recorder.

        Raises
        ------
        ValueError
            If *tuple_record_every* is given and ``< 1``.
        """
        every = 1 if tuple_record_every is None else tuple_record_every
        if every < 1:
            raise ValueError(f"tuple_record_every must be >= 1, got {every}")
        path = recorder_path(results_root, run_id)
        writer: AuditWriter | None
        try:
            writer = AuditWriter.open(path, actor=FLIGHT_ACTOR, resume=path.exists())
        except Exception as exc:
            _LOG.warning(
                "flight_recorder.open_failed",
                path=str(path),
                run_id=run_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            writer = None
        return cls(
            writer=writer,
            run_id=run_id,
            attempt=attempt,
            tuple_record_every=every,
        )

    @property
    def degraded(self) -> bool:
        """``True`` once any durable-append (or the open) has failed (D-20 latch)."""
        return self._degraded

    @property
    def dropped(self) -> int:
        """Count of records NOT durably appended (failures — never decimation skips)."""
        return self._dropped

    def record(self, kind: str, payload: Mapping[str, object] | None = None) -> bool:
        """Append one *kind* record; return ``True`` iff durably appended.

        The payload is copied and stamped with ``attempt`` (R43 — the
        recorder, not the caller, is the attempt authority). Fail-soft per the
        module contract: unknown kind / degraded latch / payload defect all
        return ``False`` and never raise (I-3, D-20). A ``TUPLE_DONE`` call
        skipped by decimation returns ``False`` WITHOUT counting a drop —
        the skip is policy, not failure (D-9).

        Parameters
        ----------
        kind
            One of :data:`RECORD_KINDS`.
        payload
            JSON-serialisable record payload (``None`` → ``{}``).

        Returns
        -------
        bool
            ``True`` iff the record is durably on the chain.
        """
        with self._lock:
            return self._record_locked(kind, payload)

    def _record_locked(self, kind: str, payload: Mapping[str, object] | None) -> bool:
        """The :meth:`record` body — caller must hold ``self._lock``."""
        if kind not in RECORD_KINDS:
            self._dropped += 1
            _LOG.warning(
                "flight_recorder.unknown_kind",
                kind=kind,
                run_id=self._run_id,
                dropped=self._dropped,
            )
            return False
        if kind == "TUPLE_DONE":
            self._tuple_seen += 1
            if (self._tuple_seen - 1) % self._tuple_every != 0:
                return False  # decimation skip (D-9) — policy, not a drop
        if self._degraded or self._writer is None:
            self._dropped += 1
            return False  # latched (D-20): silent counted drop, no retry spam
        stamped: dict[str, object] = dict(payload) if payload else {}
        stamped["attempt"] = self._attempt
        try:
            self._writer.append(action=kind, subject=self._run_id, payload=stamped)
        except (AuditPayloadError, AuditFieldError) as exc:
            # Record-specific defect (NaN payload / '|' field): the disk is
            # healthy — drop THIS record loudly, do NOT latch (D-20 scope).
            self._dropped += 1
            _LOG.warning(
                "flight_recorder.payload_rejected",
                kind=kind,
                run_id=self._run_id,
                error=str(exc),
                dropped=self._dropped,
            )
            return False
        except Exception as exc:
            self._degraded = True
            self._dropped += 1
            _LOG.warning(
                "flight_recorder.append_failed",
                kind=kind,
                run_id=self._run_id,
                error=str(exc),
                error_type=type(exc).__name__,
                dropped=self._dropped,
            )
            return False
        return True

    def milestone_pct(self, pct: int, snapshot: Mapping[str, object]) -> bool:
        """Record the 25/50/75% MILESTONE for *pct* exactly once (edge-gated).

        The first call per edge consumes it even when the append fails
        (degraded) — an edge is never retried (D-20's no-spam stance).

        Parameters
        ----------
        pct
            The percent edge; values outside :data:`MILESTONE_PCTS` are
            ignored (``False``).
        snapshot
            Progress snapshot fields folded into the MILESTONE payload
            (e.g. ``tuples_done`` / ``total`` / ``rate_per_s``).

        Returns
        -------
        bool
            ``True`` iff this call durably recorded the edge.
        """
        if pct not in MILESTONE_PCTS:
            return False
        with self._lock:
            if pct in self._milestones:
                return False
            self._milestones.add(pct)
            payload: dict[str, object] = {"pct": pct, **snapshot}
            return self._record_locked("MILESTONE", payload)

    def close(self) -> None:
        """Close the underlying chain writer — idempotent, never raises (I-3)."""
        with self._lock:
            writer = self._writer
            if writer is None:
                return
            self._writer = None
            try:
                writer.close()
            except Exception as exc:
                _LOG.warning(
                    "flight_recorder.close_failed",
                    run_id=self._run_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )


def contract_view(entry: AuditEntry) -> dict[str, object]:
    """Project one chain line onto the P2→P4 events contract shape.

    The contract record ``{ts_utc, run_id, kind, payload}`` is a documented
    SUPERSET mapping of the 8-key :class:`AuditEntry` line — ``kind ==
    action``, ``run_id == subject`` (D-7).

    Parameters
    ----------
    entry
        A parsed flight-recorder chain line.

    Returns
    -------
    dict[str, object]
        Exactly ``{"ts_utc", "run_id", "kind", "payload"}``.
    """
    return {
        "ts_utc": entry.ts_utc,
        "run_id": entry.subject,
        "kind": entry.action,
        "payload": entry.payload,
    }


def iter_run_records(results_root: Path, run_id: str) -> Iterator[dict[str, object]]:
    """Stream the run's records as contract-view dicts — torn-tail tolerant.

    O(1) memory (one record at a time via
    :func:`brutex.orchestrator.audit.iter_entries`); an unterminated torn tail
    is skipped per the chain's committed-line law. A missing recorder file
    (run not started / recorder degraded from the first append) yields an
    EMPTY iterator — the honest degrade for read-only tails, never a 500
    (failure-matrix A11/F9).

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity.

    Yields
    ------
    dict[str, object]
        :func:`contract_view` of each committed record, in chain order.
    """
    path = recorder_path(results_root, run_id)
    if not path.exists():
        return iter(())
    return (contract_view(entry) for entry in iter_entries(path))


def search_timeline(
    results_root: Path,
    run_id: str,
    *,
    kind: str | None = None,
    since: float | None = None,
    until: float | None = None,
    limit: int = 500,
) -> list[dict[str, object]]:
    """Bounded forensic search over the run timeline ("what happened at 02:31?").

    HONEST envelope: O(records) — the file is streamed once and the result is
    bounded to *limit* matches; never claimed O(1) (R41 forensics surface).

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity.
    kind
        Keep only records of this kind (``None`` = all kinds).
    since
        Keep records with ``ts_utc >= since`` (inclusive; ``None`` = no floor).
    until
        Keep records with ``ts_utc <= until`` (inclusive; ``None`` = no cap).
    limit
        Maximum matches returned; ``<= 0`` returns ``[]``.

    Returns
    -------
    list[dict[str, object]]
        Up to *limit* contract-view records in chain order.
    """
    if limit <= 0:
        return []
    path = recorder_path(results_root, run_id)
    if not path.exists():
        return []
    matches: list[dict[str, object]] = []
    for entry in iter_entries(path):
        if kind is not None and entry.action != kind:
            continue
        if since is not None and entry.ts_utc < since:
            continue
        if until is not None and entry.ts_utc > until:
            continue
        matches.append(contract_view(entry))
        if len(matches) >= limit:
            break
    return matches


def verify_recorder_chain(results_root: Path, run_id: str) -> int:
    """Verify the run's flight-recorder hash chain end-to-end (D-2).

    Delegates to the ONE matching verifier,
    :func:`brutex.audit_verify.auto_verify` — the same verifier the R28
    rehearsal chain-break CI gate invokes — never re-implementing hash logic
    (CLAUDE.md §11 COMMON). HONEST: verification is O(records).

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity.

    Returns
    -------
    int
        The number of verified entries (``0`` for an empty file).

    Raises
    ------
    brutex.orchestrator.audit.AuditError
        Missing file / malformed committed line.
    brutex.orchestrator.audit.AuditChainCorruption
        A broken SHA-256 chain.
    """
    # Function-local import: brutex.audit_verify pulls brutex.engine (the
    # compiled-wheel import chain); the recorder itself stays wheel-free at
    # import so read-only tails (P4) never require the kernel wheels.
    from brutex.audit_verify import auto_verify

    return auto_verify(recorder_path(results_root, run_id)).entry_count
