"""Audit-log → Parquet replay integration tests (invariant P-20) — CAPTURE-grade.

Per ``docs/TRACK1_VERIFICATION_PYRAMID.md`` rows G2 (Reproducibility)
+ G7 (Audit chain), this is the cross-layer correctness anchor:

> ``replay(audit_log) → Parquet`` byte-equals the original Parquet
> (invariant P-20).

DEC-AUDIT-LAKE-REPLAY-ORACLE-001 upgrades the DEC-INGEST-AUDIT-WIRING-001
re-execution flavour (replaying by re-RUNNING the audited inputs against the
same deterministic source) to the CAPTURE-mode oracle: the pull's
:class:`~brutex.data.audit_capture.AuditCaptureSink` records every non-empty
``sink.append`` — full 7-field bar tuples + per-partition
:class:`~brutex.data.parquet_writer.WriteResult` ``raw_hash`` anchors — onto
the SAME hash-chained audit trail the spine already writes
(``INGEST_RUN_START`` → per-pair → ``INGEST_RUN_COMPLETE``), drained on the
controller thread at the real ``run_groww_spot_ingest`` ``capture=`` seams.
:func:`~brutex.orchestrator.lake_replay.replay_lake_from_audit` then rebuilds
a FRESH lake from the log ALONE through the REAL
:class:`~brutex.data.sink.ParquetBarSink` →
:class:`~brutex.data.parquet_writer.ParquetWriter` stack, and
:func:`~brutex.orchestrator.lake_replay.assert_lake_byte_equal` asserts FULL
file-bytes equality against the original lake — with byte-mutation TEETH
(XOR one live byte → a loud
:class:`~brutex.orchestrator.lake_replay.ReplayMismatchError` naming the
first divergent offset + the three-way verdict).

The L3 half (DEC-L3-REPLAY-ORACLE-001, MIGRATION.md #241): the capture
sink's :class:`~brutex.data.audit_capture.DedupJournal` emits one
``action="add"`` row per key FIRST admitted into the live
:class:`~brutex.data.lmdb_membership.LmdbMembership`-backed
:class:`~brutex.data.option_dedup.OptionDedupIndex`; the UNMODIFIED
:func:`brutex.orchestrator.replay.assert_dedup_consistent` oracle compares
the live LMDB key set against the chain's replayed ``add`` subjects —
silence iff consistent, with divergence teeth both ways
(drop a dedup key → ``missing_from_live``; an extra live key →
``extra_in_live``).

HONEST envelope (Golden Rule #10): byte equality is a SAME-ENVIRONMENT,
CAPTURE-mode guarantee (replay + original run in one process; Parquet bytes
are deterministic per environment per P-19's own contract, never pinned
across polars/zstd bumps).

``test_resume_from_kill_roundtrip`` (DEC-AUDIT-TORN-TAIL-001) is unchanged.

Decisions: DEC-PHASE1-INTEGRATION-SKELETONS-001 (MIGRATION entry #251);
DEC-AUDIT-TORN-TAIL-001; DEC-L3-REPLAY-ORACLE-001 (entry #241);
DEC-INGEST-AUDIT-WIRING-001 (the de-skeleton);
DEC-AUDIT-LAKE-REPLAY-ORACLE-001 (the capture-mode upgrade).
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest
from brutex.adapters.groww.run_ingest import run_groww_spot_ingest
from brutex.adapters.synthetic import SyntheticMarketAdapter, build_synthetic_index
from brutex.data.audit_capture import (
    DEDUP_ADD_ACTION,
    LAKE_APPEND_ACTION,
    AuditCaptureSink,
    CaptureMode,
    DedupJournal,
)
from brutex.data.lmdb_membership import LmdbMembership
from brutex.data.option_dedup import OptionDedupIndex
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Instrument
from brutex.orchestrator.audit import (
    AuditEntry,
    AuditError,
    AuditWriter,
    read_entries,
    verify_chain,
)
from brutex.orchestrator.lake_replay import (
    LakeReplayReport,
    ReplayMismatchError,
    assert_lake_byte_equal,
    check_capture_anchors,
    replay_lake_from_audit,
)
from brutex.orchestrator.parallel import SerialRunParallel
from brutex.orchestrator.replay import assert_dedup_consistent, replay_audit_to_diff

#: One regular trading day (2021-01-04, a Monday) — a single 1-minute vendor
#: window, a single month partition, 375 in-session bars.
_START = date(2021, 1, 4)
_END = date(2021, 1, 5)

#: A month-STRADDLING range: sessions 2021-01-29 (Fri) + 2021-02-01 (Mon), so
#: every pair's single append splits across the Jan + Feb month partitions.
_STRADDLE_START = date(2021, 1, 29)
_STRADDLE_END = date(2021, 2, 2)

_BARS_PER_DAY_1M = 375


def _index_instrument(symbol: str) -> Instrument:
    return next(i for i in build_synthetic_index().universe if i.symbol_name == symbol)


def _capture_sink(lake_root: Path, *, journal: DedupJournal | None = None) -> AuditCaptureSink:
    """A CAPTURE-mode :class:`AuditCaptureSink` over a REAL Parquet sink."""
    return AuditCaptureSink(
        ParquetBarSink(writer=ParquetWriter(lake_root=lake_root)),
        lake_root=lake_root,
        mode=CaptureMode.CAPTURE,
        journal=journal,
    )


def _captured_pull(
    capture: AuditCaptureSink,
    audit_path: Path,
    *,
    symbols: list[str],
    intervals: list[str],
    start: date,
    end: date,
) -> None:
    """One fixture pull: the REAL spine + REAL writers + REAL audit chain.

    The capture sink IS the spine's sink (so every lake append routes through
    it) AND is passed as ``capture=`` so its buffered records drain onto the
    hash chain at the production controller-thread seams.
    """
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
        report = run_groww_spot_ingest(
            SyntheticMarketAdapter(),
            capture,
            instruments=[_index_instrument(s) for s in symbols],
            intervals=intervals,
            start=start,
            end=end,
            audit=audit,
            capture=capture,
            run_parallel=SerialRunParallel(),
        )
    assert report.failed_pairs == 0


def _bars_tree(lake_root: Path) -> dict[str, bytes]:
    """Map each ``bars/**/*.parquet`` file's lake-relative path → its bytes."""
    root = lake_root / "bars"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.parquet"))}


def _lake_pair_subjects(lake_root: Path) -> frozenset[str]:
    """Project the lake's written partitions to ``SYMBOL:interval`` subjects."""
    root = lake_root / "bars"
    return frozenset(
        f"{p.parent.parent.parent.name}:{p.parent.parent.name}" for p in root.rglob("*.parquet")
    )


def _capture_events(audit_path: Path) -> list[AuditEntry]:
    """The chain's ``LAKE_APPEND_WRITTEN`` entries, in ``capture_seq`` order."""
    events = [e for e in read_entries(audit_path) if e.action == LAKE_APPEND_ACTION]
    events.sort(key=lambda e: int(str(e.payload["capture_seq"])))
    return events


def _add_rows(audit_path: Path) -> list[AuditEntry]:
    """The chain's L3 ``action="add"`` entries, in chain order."""
    return [e for e in read_entries(audit_path) if e.action == DEDUP_ADD_ACTION]


def _assert_anchors_match_live_lake(audit_path: Path, lake_root: Path) -> LakeReplayReport:
    """Every audited ``raw_hash`` anchor equals the sha256 of the REAL lake file."""
    anchor_report = check_capture_anchors(audit_path, lake_root=lake_root)
    assert anchor_report.anchor_mismatches == ()
    assert anchor_report.anchor_matches == len(anchor_report.partitions) > 0
    return anchor_report


class TestAuditReplayRoundtrip:
    """``replay(audit_log) → Parquet`` byte-equals the original Parquet (P-20)."""

    def test_single_chunk_roundtrip(self, tmp_path: Path) -> None:
        """One symbol × one interval × one window: replay is byte-identical.

        Includes the byte-mutation TOOTH: XOR one byte of the live partition
        and the oracle fails LOUDLY, naming the first divergent offset and
        the "live lake mutated AFTER the audited write" three-way verdict.
        """
        original = tmp_path / "lake-a"
        replayed = tmp_path / "lake-b"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        _captured_pull(
            _capture_sink(original),
            audit_path,
            symbols=["NIFTY"],
            intervals=["1minute"],
            start=_START,
            end=_END,
        )
        # RUN_START → PAIR_DONE → the drained capture event → RUN_COMPLETE.
        assert verify_chain(audit_path) == 4
        (event,) = _capture_events(audit_path)
        assert event.payload["rows_in"] == _BARS_PER_DAY_1M
        assert len(event.payload["bars"]) == _BARS_PER_DAY_1M

        report = replay_lake_from_audit(audit_path, replay_root=replayed)
        assert report.appends_replayed == 1
        assert report.anchor_matches == 1
        assert report.partitions == frozenset({"bars/NSE/INDEX/NIFTY/1minute/2021/01.parquet"})
        # THE oracle: full byte equality, strict (no un-audited partitions).
        assert_lake_byte_equal(
            original_root=original, replay_root=replayed, report=report, strict=True
        )
        # Independent test-side recompute: tree equality + the raw_hash anchor
        # equals the sha256 of the REAL lake file bytes.
        original_tree = _bars_tree(original)
        assert original_tree  # a real pull wrote real Parquet
        assert _bars_tree(replayed) == original_tree
        (rel,) = report.partitions
        assert (
            report.partition_anchors[rel]
            == hashlib.sha256((original / rel).read_bytes()).hexdigest()
        )
        _assert_anchors_match_live_lake(audit_path, original)

        # TOOTH: XOR one byte of the LIVE partition → loud ReplayMismatchError.
        target = original / rel
        mutated = bytearray(target.read_bytes())
        mutated[13] ^= 0xFF
        target.write_bytes(bytes(mutated))
        with pytest.raises(ReplayMismatchError) as excinfo:
            assert_lake_byte_equal(
                original_root=original, replay_root=replayed, report=report, strict=True
            )
        message = str(excinfo.value)
        assert "first divergent byte offset 13" in message
        assert "mutated AFTER the audited write" in message
        # The anchor verifier flags the same drifted partition (collected, not raised).
        drifted = check_capture_anchors(audit_path, lake_root=original)
        assert len(drifted.anchor_mismatches) == 1
        assert rel in drifted.anchor_mismatches[0]

    def test_multi_chunk_roundtrip(self, tmp_path: Path) -> None:
        """Multi symbol × multi interval × month-straddling partitions — plus a
        second identical re-pull (the all-dedup merge path): byte equality
        holds, the re-pull's events show ``bars_added=0`` per partition with
        unchanged ``raw_hash``, and ZERO new L3 ``add`` rows are emitted.
        """
        original = tmp_path / "lake-a"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        journal = DedupJournal(OptionDedupIndex())
        capture = _capture_sink(original, journal=journal)
        pull = {
            "symbols": ["NIFTY", "SENSEX"],
            "intervals": ["1minute", "1day"],
            "start": _STRADDLE_START,
            "end": _STRADDLE_END,
        }
        _captured_pull(capture, audit_path, **pull)  # type: ignore[arg-type]
        assert verify_chain(audit_path) > 0

        events_first = _capture_events(audit_path)
        adds_first = _add_rows(audit_path)
        # One append per (symbol, interval) pair; each straddles the month
        # boundary → TWO partitions per event (Jan + Feb).
        assert len(events_first) == 4
        assert all(len(e.payload["partitions"]) == 2 for e in events_first)
        # 2 sessions × 375 1m bars + 2 daily bars, per symbol — every bar new.
        expected_adds = 2 * (2 * _BARS_PER_DAY_1M + 2)
        assert len(adds_first) == expected_adds
        assert sum(int(str(e.payload["added_count"])) for e in events_first) == expected_adds

        replayed = tmp_path / "lake-b"
        report = replay_lake_from_audit(audit_path, replay_root=replayed)
        assert report.appends_replayed == 4
        # 2 symbols × 2 intervals × 2 months.
        assert len(report.partitions) == 8
        assert_lake_byte_equal(
            original_root=original, replay_root=replayed, report=report, strict=True
        )
        _assert_anchors_match_live_lake(audit_path, original)
        tree_first = _bars_tree(original)
        assert _bars_tree(replayed) == tree_first

        # ── Second IDENTICAL re-pull through the SAME capture sink + journal ──
        _captured_pull(capture, audit_path, **pull)  # type: ignore[arg-type]
        assert verify_chain(audit_path) > 0
        events_all = _capture_events(audit_path)
        assert len(events_all) == 8
        first_anchor = {
            str(p["rel"]): str(p["raw_hash"]) for e in events_first for p in e.payload["partitions"]
        }
        for event in events_all[4:]:
            # All-dedup: the input batch re-arrives, nothing lands.
            assert event.payload["bars_net_new"] == 0
            assert event.payload["added_count"] == 0
            for partition in event.payload["partitions"]:
                assert partition["bars_added"] == 0
                # Keep-first left the stored bytes untouched → same raw_hash.
                assert partition["raw_hash"] == first_anchor[str(partition["rel"])]
        # Zero NEW add rows (Rule #7 idempotence — the journal admits nothing).
        assert len(_add_rows(audit_path)) == expected_adds
        # The live lake bytes did not move.
        assert _bars_tree(original) == tree_first

        # Replaying the GROWN log (original + re-pull events) stays byte-equal.
        replayed_grown = tmp_path / "lake-c"
        report_grown = replay_lake_from_audit(audit_path, replay_root=replayed_grown)
        assert report_grown.appends_replayed == 8
        assert_lake_byte_equal(
            original_root=original,
            replay_root=replayed_grown,
            report=report_grown,
            strict=True,
        )

    def test_resume_from_kill_roundtrip(self, tmp_path: Path) -> None:
        """A SIGKILL mid-append leaves a torn tail; resume recovers + continues.

        The kill-recovery harness (DEC-AUDIT-TORN-TAIL-001): write a clean
        chain, simulate a crash mid-append by appending raw partial bytes with
        NO trailing newline (exactly what an interrupted ``write()`` leaves),
        then ``open(resume=True)`` — which previously raised ``AuditError`` and
        aborted the unattended restart. After the fix it must skip the torn
        tail, recover the last complete hash-valid entry, continue the chain,
        and leave a fully verifiable log.
        """
        path = tmp_path / "audit" / "run-kill.jsonl"

        # 1. A clean committed prefix.
        with AuditWriter.open(path) as log:
            log.append(action="RUN_START", subject="run:kill", payload={"n": 0})
            log.append(action="INGEST_CHUNK_DONE", subject="NSE-NIFTY", payload={"bars": 375})

        # 2. Simulate the crash: a partial, newline-LESS final line, just as a
        #    SIGKILL between the write() and the durable flush of the next
        #    append would leave on disk.
        with path.open("a", encoding="utf-8") as raw:
            raw.write('{"seq": 2, "prev_hash": "deadbeef", "ts_utc": 1.0, "actor": "bru')

        # 3. Unattended restart. Pre-fix this raised AuditError; post-fix it
        #    recovers from the last complete entry.
        with AuditWriter.open(path, resume=True) as log:
            recovered = log.append(
                action="RUN_COMPLETE", subject="run:kill", payload={"total": 375}
            )

        # 4. The recovered entry continues the chain at seq 2 (the torn line's
        #    seq was uncommitted and is replaced).
        assert recovered.seq == 2

        # 5. The whole log verifies end to end — the torn tail is gone and the
        #    chain is intact across the kill boundary.
        assert verify_chain(path) == 3


class TestAuditReplayConsistency:
    """L3 oracle over a REAL ingest run (DEC-L3-REPLAY-ORACLE-001 + this PR)."""

    def test_live_set_matches_replayed_set(self, tmp_path: Path) -> None:
        """The live LMDB dedup set matches the chain's replayed ``add`` subjects.

        The §3 producer mapping end-to-end: the capture sink's
        :class:`DedupJournal` admits every pulled bar's ``candle_dedup_key``
        into a REAL :class:`LmdbMembership`-backed dedup index and buffers one
        ``action="add"`` row per admission onto the hash chain. The UNMODIFIED
        #241 oracle then compares live vs replayed: silence iff consistent,
        with divergence teeth BOTH ways — and the P-20 replay of the SAME log
        is byte-equal (the two oracles cross-checked over one run).
        """
        lake = tmp_path / "lake"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        with LmdbMembership.open(tmp_path / "dedup-live.lmdb", map_size_mb=64) as membership:
            journal = DedupJournal(OptionDedupIndex(exact=membership))
            _captured_pull(
                _capture_sink(lake, journal=journal),
                audit_path,
                symbols=["NIFTY", "BANKNIFTY"],
                intervals=["1minute"],
                start=_START,
                end=_END,
            )
            assert verify_chain(audit_path) > 0
            live = frozenset(key.hex() for key in membership.iter_keys())
        assert len(live) == 2 * _BARS_PER_DAY_1M

        # Strict oracle: silence iff consistent (default action_filter="add").
        assert_dedup_consistent(audit_path=audit_path, live_subjects=live)
        diff = replay_audit_to_diff(audit_path=audit_path, live_subjects=live)
        assert diff.is_consistent
        assert diff.replayed_count == 2 * _BARS_PER_DAY_1M

        # TOOTH 1: drop one dedup key from the live snapshot → the oracle
        # fails loudly naming the missing side.
        dropped_key = min(live)
        with pytest.raises(AuditError, match="missing_from_live=1"):
            assert_dedup_consistent(audit_path=audit_path, live_subjects=live - {dropped_key})
        # TOOTH 2: an extra live key the chain never recorded → extra_in_live.
        with pytest.raises(AuditError, match="extra_in_live=1"):
            assert_dedup_consistent(audit_path=audit_path, live_subjects=live | {"ff" * 32})

        # Cross-check: the SAME log replays byte-equal (P-20 over the L3 run).
        replayed = tmp_path / "lake-replayed"
        report = replay_lake_from_audit(audit_path, replay_root=replayed)
        assert report.appends_replayed == 2
        assert_lake_byte_equal(original_root=lake, replay_root=replayed, report=report, strict=True)

    def test_unaudited_extra_partition_is_flagged(self, tmp_path: Path) -> None:
        """A lake pair the audit log never recorded trips the oracle (Rule #8:
        every material action must be audited — an extra live subject is a BUG)."""
        lake = tmp_path / "lake"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        _captured_pull(
            _capture_sink(lake),
            audit_path,
            symbols=["NIFTY"],
            intervals=["1minute"],
            start=_START,
            end=_END,
        )
        live = _lake_pair_subjects(lake) | {"GHOST:1minute"}
        with pytest.raises(AuditError, match="extra_in_live"):
            assert_dedup_consistent(
                audit_path=audit_path,
                live_subjects=live,
                action_filter="INGEST_PAIR_DONE",
            )
