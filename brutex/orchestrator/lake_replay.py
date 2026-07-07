"""Audit→Parquet lake replay engine + byte-equality oracle (P-20).

DEC-AUDIT-LAKE-REPLAY-ORACLE-001 — the CONSUMER half of the replay oracle
pair (the producer is :mod:`brutex.data.audit_capture`):

* :func:`replay_lake_from_audit` streams a ``verify_chain``-verified audit
  log, selects the ``LAKE_APPEND_WRITTEN`` events, sorts them by
  ``(capture run, capture_seq)`` — ``capture_seq`` is the true lake-apply
  order WITHIN one run (the producer's ONE lock spans the durable append and
  the stamp), and the per-run ``capture_run`` nonce segments the runs of a
  resume-appended production chain, whose per-process seqs would otherwise
  collide — decodes each event's captured bar tuples, and re-drives the REAL
  :class:`~brutex.data.sink.ParquetBarSink` →
  :class:`~brutex.data.parquet_writer.ParquetWriter` stack into a fresh
  replay root — ZERO re-implementation of partition grouping / tick-rounding
  / dedup-merge / render anywhere. Every replayed partition's ``raw_hash``
  is checked against the captured per-event anchor, so a divergence is
  localized to the exact append (strictly stronger than a final-state diff).
* :func:`assert_lake_byte_equal` then compares the replayed partitions
  byte-for-byte against the original lake; a divergence names the partition,
  both sizes, both sha256s, the FIRST divergent byte offset, and a three-way
  verdict against the last audited ``raw_hash`` that separates "the live
  lake mutated AFTER the audited write" from "capture/writer
  nondeterminism".
* :func:`check_capture_anchors` verifies an ANCHOR-mode log (no ``bars``
  payloads) against the live lake with no reconstruction claim: for each
  partition the LAST anchor (in ``(run, capture_seq)`` apply order) must
  equal the sha256 of the live file bytes — tamper-evident lake
  verification.

**Honest envelope (Golden Rule #10).** Byte equality is a SAME-ENVIRONMENT
guarantee (Parquet bytes are deterministic per environment, not warranted
across polars/zstd version bumps — P-19's own contract); replay + original
run in one process in CI. Capture-mode replay reproduces the INGEST-BOUNDARY
bytes: on a Greeks-enrichment-rewritten lake the on-disk compare of those
partitions honestly FAILS and the anchor cross-check says why (lake moved
after the audited write). Prebuild-derived TFs are excluded by design — the
oracle's contract is the vendor-ingest write boundary, the only place
external bytes enter the system.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from brutex.orchestrator.audit import AuditEntry, AuditError, iter_entries, verify_chain

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_LOG = structlog.get_logger("brutex.orchestrator.lake_replay")


class ReplayCaptureMissing(AuditError):
    """An anchor-mode event (no ``bars`` payload) was fed to reconstruction.

    An anchor-mode log carries per-append ``raw_hash`` anchors but NOT the
    input bars, so the lake cannot be rebuilt from it — use
    :func:`check_capture_anchors` (verification, not reconstruction).
    """


class ReplayMismatchError(AuditError):
    """THE loud divergence failure — replayed bytes disagree with the record.

    Raised on a per-event anchor mismatch (naming the ``capture_seq`` and the
    partition), on a tampered payload (an empty ``bars`` array the producer
    never emits, a junk ``capture_seq`` / ``capture_run`` / ``partitions``
    shape, a duplicate ``(capture_run, capture_seq)`` pair), on a full
    byte-compare divergence (naming the partition, both sizes, both sha256s,
    the FIRST divergent byte offset, and the three-way verdict), and — in
    strict mode — on a live ``*.parquet`` never named by any capture event
    (the "write bypassed audit" class).
    """


@dataclass(frozen=True, slots=True)
class LakeReplayReport:
    """Outcome of one replay / anchor-verification pass.

    Attributes
    ----------
    appends_replayed
        Number of ``LAKE_APPEND_WRITTEN`` events processed (replayed by
        :func:`replay_lake_from_audit`; examined by
        :func:`check_capture_anchors`).
    partitions
        The audited partition paths, relative to the lake root (posix).
    anchor_matches
        Per-event (replay) / per-partition (anchor check) ``raw_hash``
        equalities observed.
    anchor_mismatches
        Human-readable mismatch descriptions — always empty from
        :func:`replay_lake_from_audit` (the first mismatch RAISES there);
        :func:`check_capture_anchors` COLLECTS them instead so an operator
        sweep reports every drifted partition in one pass.
    partition_anchors
        Per-partition LAST audited ``raw_hash`` (by ``capture_seq``) — the
        three-way cross-check input :func:`assert_lake_byte_equal` consumes.
    """

    appends_replayed: int
    partitions: frozenset[str]
    anchor_matches: int
    anchor_mismatches: tuple[str, ...]
    partition_anchors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _CaptureEvent:
    """One parsed ``LAKE_APPEND_WRITTEN`` event (validated payload projection)."""

    capture_run: str
    capture_seq: int
    exchange: str
    segment: str
    symbol: str
    interval: str
    merge_mode: str
    partitions: tuple[tuple[str, str], ...]  # (rel, raw_hash) in sink write order
    bars: list[list[object]] | None


def _require_str(payload: dict[str, Any], key: str, *, seq: int) -> str:
    """Extract a required string payload field — loud on a tampered shape."""
    value = payload.get(key)
    if not isinstance(value, str):
        raise ReplayMismatchError(
            f"LAKE_APPEND_WRITTEN entry seq={seq} carries no valid {key!r}: {value!r}"
        )
    return value


def _parse_event(entry: AuditEntry) -> _CaptureEvent:
    """Validate + project one audit entry into a :class:`_CaptureEvent`.

    The chain hash proves the line was not TAMPERED after commit; this
    validation additionally refuses a hand-crafted / foreign payload whose
    SHAPE cannot drive a faithful replay (Golden Rule #1 — never reconstruct
    from junk).
    """
    payload = entry.payload
    seq = payload.get("capture_seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise ReplayMismatchError(
            f"LAKE_APPEND_WRITTEN entry seq={entry.seq} carries no valid capture_seq: {seq!r}"
        )
    raw_partitions = payload.get("partitions")
    if not isinstance(raw_partitions, list):
        raise ReplayMismatchError(
            f"LAKE_APPEND_WRITTEN capture_seq={seq} carries no valid partitions "
            f"array: {raw_partitions!r}"
        )
    anchors: list[tuple[str, str]] = []
    for item in raw_partitions:
        if not isinstance(item, dict):
            raise ReplayMismatchError(
                f"LAKE_APPEND_WRITTEN capture_seq={seq} has a malformed partition anchor: {item!r}"
            )
        rel = item.get("rel")
        raw_hash = item.get("raw_hash")
        if not isinstance(rel, str) or not isinstance(raw_hash, str):
            raise ReplayMismatchError(
                f"LAKE_APPEND_WRITTEN capture_seq={seq} has a malformed partition anchor: {item!r}"
            )
        anchors.append((rel, raw_hash))
    raw_bars = payload.get("bars")
    bars: list[list[object]] | None
    if raw_bars is None:
        bars = None
    elif isinstance(raw_bars, list):
        bars = []
        for row in raw_bars:
            if not isinstance(row, list):
                raise ReplayMismatchError(
                    f"LAKE_APPEND_WRITTEN capture_seq={seq} has a malformed bars row: {row!r}"
                )
            bars.append(row)
    else:
        raise ReplayMismatchError(
            f"LAKE_APPEND_WRITTEN capture_seq={seq} has a malformed bars payload: {raw_bars!r}"
        )
    return _CaptureEvent(
        capture_run=_require_str(payload, "capture_run", seq=seq),
        capture_seq=seq,
        exchange=_require_str(payload, "exchange", seq=seq),
        segment=_require_str(payload, "segment", seq=seq),
        symbol=_require_str(payload, "symbol", seq=seq),
        interval=_require_str(payload, "interval", seq=seq),
        merge_mode=_require_str(payload, "merge_mode", seq=seq),
        partitions=tuple(anchors),
        bars=bars,
    )


def _load_capture_events(audit_path: Path, *, verify: bool) -> list[_CaptureEvent]:
    """Stream the log, verify the chain first (default), and sort into apply order.

    ``verify=True`` (the default) runs the existing
    :func:`~brutex.orchestrator.audit.verify_chain` FIRST — a tampered chain
    dies as :class:`~brutex.orchestrator.audit.AuditChainCorruption` before
    any reconstruction (Golden Rule #8). A torn (uncommitted) tail is skipped
    by ``iter_entries``' existing discriminator and surfaces later as a byte
    mismatch — never silently absorbed. The documented ``verify=False``
    bypass exists for diagnostic replays of a knowingly-broken chain.

    Foreign actions (the spine's coarse ``INGEST_*`` lines, the L3 ``add``
    rows) are ignored. The apply order is ``(run, capture_seq)``:

    * WITHIN one run, chain-line order across pairs varies run-to-run
      (pair completion order), so the sort by ``capture_seq`` — the true
      lake-apply order under the producer's one lock — makes replay
      order-insensitive;
    * ACROSS runs, ``capture_seq`` restarts at 0 per process while the
      production chain is resume-appended across pulls, so events are FIRST
      grouped by their ``capture_run`` nonce in chain first-appearance order
      (runs on one chain are strictly sequential — the CLI's cross-process
      single-flight pull gate admits one pull per lake at a time), which IS
      the run start order. A duplicate ``(capture_run, capture_seq)`` pair is
      impossible from the producer (one monotonic stamp per sink), so its
      presence is refused loudly instead of silently mis-ordering.
    """
    if verify:
        verify_chain(audit_path)
    events = [
        _parse_event(entry)
        for entry in iter_entries(audit_path)
        if entry.action == "LAKE_APPEND_WRITTEN"
    ]
    run_order: dict[str, int] = {}
    seen: set[tuple[str, int]] = set()
    for event in events:
        run_order.setdefault(event.capture_run, len(run_order))
        key = (event.capture_run, event.capture_seq)
        if key in seen:
            raise ReplayMismatchError(
                f"duplicate LAKE_APPEND_WRITTEN capture_seq={event.capture_seq} within "
                f"capture run {event.capture_run!r} — the producer stamps each run's "
                "seq exactly once, so this log is tampered (or two runs share a nonce)"
            )
        seen.add(key)
    events.sort(key=lambda event: (run_order[event.capture_run], event.capture_seq))
    return events


def check_capture_anchors(
    audit_path: Path,
    *,
    lake_root: Path,
    verify: bool = True,
) -> LakeReplayReport:
    """Verify an anchor-mode log against the LIVE lake — no reconstruction.

    For each audited partition, the LAST ``LAKE_APPEND_WRITTEN`` anchor (in
    the ``(run, capture_seq)`` apply order — multi-pull resumed chains pick
    the truly-last run's anchor) must equal the sha256 of the live file
    bytes. Mismatches
    (including a missing live file) are COLLECTED into the report's
    ``anchor_mismatches``, never raised — the operator-sweep mode that names
    every drifted partition in one pass. Works on anchor AND capture logs
    (the anchors ride both).

    Parameters
    ----------
    audit_path
        The hash-chained audit ``.jsonl``.
    lake_root
        The LIVE lake root the anchors were captured against.
    verify
        Run :func:`~brutex.orchestrator.audit.verify_chain` first (default).
    """
    events = _load_capture_events(audit_path, verify=verify)
    last_anchor: dict[str, str] = {}
    for event in events:
        for rel, raw_hash in event.partitions:
            last_anchor[rel] = raw_hash
    matches = 0
    mismatches: list[str] = []
    for rel in sorted(last_anchor):
        expected = last_anchor[rel]
        live_path = lake_root / rel
        if not live_path.is_file():
            mismatches.append(f"{rel}: audited raw_hash {expected} but the live file is MISSING")
            continue
        actual = hashlib.sha256(live_path.read_bytes()).hexdigest()
        if actual == expected:
            matches += 1
        else:
            mismatches.append(f"{rel}: audited raw_hash {expected} != live sha256 {actual}")
    _LOG.info(
        "lake_replay.anchors_checked",
        audit_path=str(audit_path),
        events=len(events),
        partitions=len(last_anchor),
        matches=matches,
        mismatches=len(mismatches),
    )
    return LakeReplayReport(
        appends_replayed=len(events),
        partitions=frozenset(last_anchor),
        anchor_matches=matches,
        anchor_mismatches=tuple(mismatches),
        partition_anchors=dict(last_anchor),
    )


def replay_lake_from_audit(
    audit_path: Path,
    *,
    replay_root: Path,
    verify: bool = True,
) -> LakeReplayReport:
    """Rebuild a lake from a CAPTURE-mode audit log through the REAL writers.

    Streams the verified log, sorts the ``LAKE_APPEND_WRITTEN`` events into
    ``(run, capture_seq)`` apply order, decodes each event's bars
    (:func:`~brutex.data.audit_capture.decode_bars`), and drives a fresh REAL
    :class:`~brutex.data.sink.ParquetBarSink` (no catalog) over
    :class:`~brutex.data.parquet_writer.ParquetWriter` rooted at
    *replay_root* — the sink re-derives partition grouping through the same
    ``partition_for_timestamp`` and the writer re-runs tick-rounding /
    dedup / merge / render, so NOTHING is re-implemented. Every replayed
    partition ``raw_hash`` is asserted equal to the captured per-event anchor
    (same sorted-month order the sink emits).

    Parameters
    ----------
    audit_path
        The hash-chained CAPTURE-mode audit ``.jsonl``.
    replay_root
        A fresh root to rebuild into (never the live lake).
    verify
        Run :func:`~brutex.orchestrator.audit.verify_chain` first (default).

    Raises
    ------
    ReplayCaptureMissing
        On a ``bars``-less event — an anchor-mode log cannot reconstruct;
        use :func:`check_capture_anchors`.
    ReplayMismatchError
        On the FIRST per-event anchor mismatch (naming ``capture_seq``,
        partition, expected and actual hashes — divergence localized to the
        exact append), or on a tampered payload (an empty ``bars`` array is
        impossible from the producer — empty appends buffer nothing — so its
        presence means tampering; this also pre-guards the writer's own
        empty-batch ``WriterError``).
    AuditChainCorruption
        From ``verify_chain`` on a tampered chain (before any reconstruction).
    """
    from brutex.data.audit_capture import decode_bars
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    events = _load_capture_events(audit_path, verify=verify)
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=replay_root))
    partitions: set[str] = set()
    partition_anchors: dict[str, str] = {}
    anchor_matches = 0
    for event in events:
        if event.bars is None:
            raise ReplayCaptureMissing(
                f"LAKE_APPEND_WRITTEN capture_seq={event.capture_seq} carries no "
                "'bars' payload — an anchor-mode log cannot reconstruct the lake; "
                "use check_capture_anchors instead"
            )
        if not event.bars:
            raise ReplayMismatchError(
                f"LAKE_APPEND_WRITTEN capture_seq={event.capture_seq} carries an "
                "EMPTY 'bars' array — the producer never emits one (empty appends "
                "buffer nothing), so this payload is tampered"
            )
        bars = decode_bars(event.bars)
        result = sink.append(
            exchange=event.exchange,
            segment=event.segment,
            symbol=event.symbol,
            interval=event.interval,
            bars=bars,
            merge_mode=event.merge_mode,
        )
        if len(result.write_results) != len(event.partitions):
            raise ReplayMismatchError(
                f"LAKE_APPEND_WRITTEN capture_seq={event.capture_seq} replayed into "
                f"{len(result.write_results)} partition(s) but the capture recorded "
                f"{len(event.partitions)}"
            )
        for write_result, (expected_rel, expected_hash) in zip(
            result.write_results, event.partitions, strict=True
        ):
            actual_rel = write_result.path.relative_to(replay_root).as_posix()
            if actual_rel != expected_rel:
                raise ReplayMismatchError(
                    f"LAKE_APPEND_WRITTEN capture_seq={event.capture_seq} replayed "
                    f"into partition {actual_rel!r} but the capture recorded "
                    f"{expected_rel!r}"
                )
            partitions.add(actual_rel)
            partition_anchors[actual_rel] = expected_hash
            if write_result.raw_hash != expected_hash:
                raise ReplayMismatchError(
                    f"per-event anchor mismatch at capture_seq={event.capture_seq} "
                    f"partition {actual_rel}: captured raw_hash {expected_hash} != "
                    f"replayed raw_hash {write_result.raw_hash}"
                )
            anchor_matches += 1
    _LOG.info(
        "lake_replay.replayed",
        audit_path=str(audit_path),
        replay_root=str(replay_root),
        appends_replayed=len(events),
        partitions=len(partitions),
        anchor_matches=anchor_matches,
    )
    return LakeReplayReport(
        appends_replayed=len(events),
        partitions=frozenset(partitions),
        anchor_matches=anchor_matches,
        anchor_mismatches=(),
        partition_anchors=partition_anchors,
    )


def _first_divergent_offset(original: bytes, replayed: bytes) -> int:
    """Return the first byte offset where the two buffers disagree.

    When one buffer is a strict prefix of the other, the divergence offset is
    the shorter length (the first missing byte).
    """
    for offset, (original_byte, replayed_byte) in enumerate(zip(original, replayed, strict=False)):
        if original_byte != replayed_byte:
            return offset
    return min(len(original), len(replayed))


def _cross_check_verdict(
    *,
    anchor: str | None,
    original_sha: str,
    replayed_sha: str,
) -> str:
    """The three-way verdict separating post-write mutation from nondeterminism.

    Compares BOTH sides against the last audited ``raw_hash`` for the
    partition: replayed == anchor ⇒ the replay faithfully reproduced the
    audited write, so the LIVE lake moved AFTER it; original == anchor ⇒ the
    live lake still holds the audited bytes, so the REPLAY diverged
    (capture/writer nondeterminism); neither ⇒ both sides drifted.
    """
    if anchor is None:
        return "no audited raw_hash available for a three-way cross-check"
    if replayed_sha == anchor:
        return (
            "replayed bytes MATCH the audited raw_hash — the live lake was "
            "mutated AFTER the audited write"
        )
    if original_sha == anchor:
        return (
            "live bytes MATCH the audited raw_hash — the REPLAY diverged from "
            "the audited write (capture/writer nondeterminism)"
        )
    return "NEITHER side matches the audited raw_hash — both diverged from the audited write"


def assert_lake_byte_equal(
    *,
    original_root: Path,
    replay_root: Path,
    report: LakeReplayReport,
    strict: bool = True,
) -> None:
    """Assert byte equality of every audited partition — THE P-20 oracle.

    Compares ``read_bytes()`` of each partition in ``report.partitions``
    under *original_root* vs *replay_root*. A divergence raises
    :class:`ReplayMismatchError` naming the partition, both sizes, both
    sha256s, the FIRST divergent byte offset, and the three-way verdict
    against the last audited ``raw_hash`` (see :func:`_cross_check_verdict`).

    Parameters
    ----------
    original_root, replay_root
        The live lake and the replayed lake roots.
    report
        The :class:`LakeReplayReport` from :func:`replay_lake_from_audit`
        (its ``partitions`` scope + ``partition_anchors`` cross-check input).
    strict
        ``True`` (default) additionally walks ``original_root / "bars"`` —
        the :class:`~brutex.data.sink.ParquetBarSink` tree the capture sink
        wraps, the ONLY tree a captured write can land in — for ``*.parquet``
        files never named by any capture event and raises: the "write
        bypassed audit" class. Deliberately NOT the whole lake root: every
        real pull also writes non-bar parquet sidecars OUTSIDE ``bars/``
        (``window_ledger/ledger.parquet`` + deltas, ``.quarantine/**`` heal
        copies) that structurally never route through the capture sink —
        walking them would false-flag every genuine production capture and
        force ``strict=False``, losing the bypass-detection class entirely.
        ``False`` scopes the compare to audited datasets only — the honest
        mode for a lake whose ``bars/`` tree also holds prebuild / heal /
        enrichment output.

    Raises
    ------
    ReplayMismatchError
        On any divergence (missing file, byte mismatch, or — strict mode —
        an un-audited live partition).
    """
    for rel in sorted(report.partitions):
        original_path = original_root / rel
        replay_path = replay_root / rel
        if not original_path.is_file():
            raise ReplayMismatchError(
                f"audited partition {rel} is MISSING from the original lake at {original_root}"
            )
        if not replay_path.is_file():
            raise ReplayMismatchError(
                f"audited partition {rel} is MISSING from the replayed lake at {replay_root}"
            )
        original_bytes = original_path.read_bytes()
        replayed_bytes = replay_path.read_bytes()
        if original_bytes == replayed_bytes:
            continue
        original_sha = hashlib.sha256(original_bytes).hexdigest()
        replayed_sha = hashlib.sha256(replayed_bytes).hexdigest()
        offset = _first_divergent_offset(original_bytes, replayed_bytes)
        verdict = _cross_check_verdict(
            anchor=report.partition_anchors.get(rel),
            original_sha=original_sha,
            replayed_sha=replayed_sha,
        )
        raise ReplayMismatchError(
            f"lake byte divergence at partition {rel}: original "
            f"{len(original_bytes)} bytes (sha256 {original_sha}) != replayed "
            f"{len(replayed_bytes)} bytes (sha256 {replayed_sha}); first divergent "
            f"byte offset {offset}; {verdict}"
        )
    if strict:
        # Scope the bypass walk to the bars/ tree the capture sink wraps —
        # the window-ledger + quarantine parquet sidecars outside it never
        # route through a BarSink and would false-flag every real pull.
        unaudited = sorted(
            path.relative_to(original_root).as_posix()
            for path in (original_root / "bars").rglob("*.parquet")
            if path.relative_to(original_root).as_posix() not in report.partitions
        )
        if unaudited:
            raise ReplayMismatchError(
                f"{len(unaudited)} live parquet partition(s) under bars/ were never "
                f"named by any capture event (a write bypassed audit, or the lake "
                f"holds un-audited derived output — use strict=False to scope to "
                f"audited datasets): {unaudited[:10]!r}"
            )
    _LOG.info(
        "lake_replay.byte_equal",
        original_root=str(original_root),
        replay_root=str(replay_root),
        partitions=len(report.partitions),
        strict=strict,
    )


__all__ = [
    "LakeReplayReport",
    "ReplayCaptureMissing",
    "ReplayMismatchError",
    "assert_lake_byte_equal",
    "check_capture_anchors",
    "replay_lake_from_audit",
]
