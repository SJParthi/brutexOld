"""Audit-log → Parquet replay integration tests (invariant P-20) — REAL.

Per ``docs/TRACK1_VERIFICATION_PYRAMID.md`` rows G2 (Reproducibility)
+ G7 (Audit chain), this is the cross-layer correctness anchor:

> ``replay(audit_log) → Parquet`` byte-equals the original Parquet
> (invariant P-20).

Formerly a Phase-2 skeleton whose roundtrip tests were ``pytest.skip``-ped
with a STALE "needs Parquet writer" reason — the writer has long existed
(P-19 pins its determinism). DEC-INGEST-AUDIT-WIRING-001 makes them REAL
over a fixture pull:

1. A tiny ingest runs FULLY OFFLINE through the REAL spine
   (:func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest` over the
   :class:`~brutex.adapters.synthetic.SyntheticMarketAdapter`) writing REAL
   Parquet (:class:`~brutex.data.sink.ParquetBarSink` over
   :class:`~brutex.data.parquet_writer.ParquetWriter`) + the hash-chained
   audit trail (the SAME ``INGEST_RUN_START`` → per-pair →
   ``INGEST_RUN_COMPLETE`` events every production pull writes).
2. The REPLAY is driven FROM THE AUDIT LOG: the replay harness reads ONLY
   the verified chain — the ``INGEST_RUN_START`` payload's ``[start, end)``
   range + the ``INGEST_PAIR_DONE`` subjects' ``(symbol, interval)`` pairs —
   and re-runs exactly those audited inputs into a FRESH lake.
3. Byte-equality: the replayed ``bars/`` tree equals the original
   file-for-file, byte-for-byte (P-19 deterministic writer + Golden Rule #7
   idempotence make this exact, not approximate).

HONEST envelope (Golden Rule #10): the audit log records ACTIONS, not bar
payloads (the bars live in the lake — the chain is the tamper-evident record
of WHAT ran), so the replay re-executes the audited inputs against the same
deterministic source; it does not decode bars out of the log. That is the
P-20 projection the pyramid's G2 row wants: the log alone is sufficient to
reproduce the Parquet output bit-for-bit.

``test_resume_from_kill_roundtrip`` (DEC-AUDIT-TORN-TAIL-001) is unchanged.

Decisions: DEC-PHASE1-INTEGRATION-SKELETONS-001 (MIGRATION entry #251);
DEC-AUDIT-TORN-TAIL-001; DEC-L3-REPLAY-ORACLE-001 (entry #241);
DEC-INGEST-AUDIT-WIRING-001 (the de-skeleton).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brutex.adapters.groww.run_ingest import run_groww_spot_ingest
from brutex.adapters.synthetic import SyntheticMarketAdapter, build_synthetic_index
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Instrument
from brutex.orchestrator.audit import (
    AuditWriter,
    read_entries,
    verify_chain,
)
from brutex.orchestrator.replay import assert_dedup_consistent, replay_audit_to_diff

#: One regular trading week (2021-01-04 → 2021-01-08) — a SINGLE 1-minute
#: vendor chunk (the 30-day tier), so the single-chunk case is one window.
_START = date(2021, 1, 4)
_END = date(2021, 1, 9)

#: A >30-day span — the 1-minute tier tiles it into MULTIPLE vendor chunks.
_MULTI_END = date(2021, 2, 20)


def _index_instrument(symbol: str) -> Instrument:
    return next(i for i in build_synthetic_index().universe if i.symbol_name == symbol)


def _sink(lake_root: Path) -> ParquetBarSink:
    return ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


def _run_audited_ingest(
    lake_root: Path,
    audit_path: Path,
    *,
    symbols: list[str],
    intervals: list[str],
    start: date,
    end: date,
) -> None:
    """One fixture pull: the REAL spine + REAL Parquet writer + REAL audit chain."""
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
        run_groww_spot_ingest(
            SyntheticMarketAdapter(),
            _sink(lake_root),
            instruments=[_index_instrument(s) for s in symbols],
            intervals=intervals,
            start=start,
            end=end,
            audit=audit,
        )


def _replay_from_audit(audit_path: Path, lake_root: Path) -> None:
    """Replay the audited ingest FROM THE LOG ALONE into a fresh *lake_root*.

    Reads nothing but the (verified) audit chain: the run's ``[start, end)``
    range off ``INGEST_RUN_START`` and the ``(symbol, interval)`` worklist off
    the ``INGEST_PAIR_DONE`` subjects.
    """
    assert verify_chain(audit_path) > 0  # replay only a proven-untampered chain
    entries = read_entries(audit_path)
    start_entry = next(e for e in entries if e.action == "INGEST_RUN_START")
    start = date.fromisoformat(str(start_entry.payload["start"]))
    end = date.fromisoformat(str(start_entry.payload["end"]))
    pairs = [
        e.subject.rsplit(":", 1) for e in entries if e.action == "INGEST_PAIR_DONE"
    ]
    symbols = sorted({sym for sym, _ in pairs})
    intervals = sorted({interval for _, interval in pairs})
    run_groww_spot_ingest(
        SyntheticMarketAdapter(),
        _sink(lake_root),
        instruments=[_index_instrument(s) for s in symbols],
        intervals=intervals,
        start=start,
        end=end,
    )


def _bars_tree(lake_root: Path) -> dict[str, bytes]:
    """Map each ``bars/**/*.parquet`` file's lake-relative path → its bytes."""
    root = lake_root / "bars"
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.parquet"))
    }


def _lake_pair_subjects(lake_root: Path) -> frozenset[str]:
    """Project the lake's written partitions to ``SYMBOL:interval`` subjects."""
    root = lake_root / "bars"
    return frozenset(
        f"{p.parent.parent.parent.name}:{p.parent.parent.name}"
        for p in root.rglob("*.parquet")
    )


class TestAuditReplayRoundtrip:
    """``replay(audit_log) → Parquet`` byte-equals the original Parquet (P-20)."""

    def test_single_chunk_roundtrip(self, tmp_path: Path) -> None:
        """One symbol × one interval × one vendor chunk: replay is byte-identical."""
        original = tmp_path / "lake-a"
        replayed = tmp_path / "lake-b"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        _run_audited_ingest(
            original,
            audit_path,
            symbols=["NIFTY"],
            intervals=["1minute"],
            start=_START,
            end=_END,
        )
        # The chain verifies end to end: RUN_START → PAIR_DONE → RUN_COMPLETE.
        assert verify_chain(audit_path) == 3
        _replay_from_audit(audit_path, replayed)
        original_tree = _bars_tree(original)
        assert original_tree  # a real pull wrote real Parquet
        assert _bars_tree(replayed) == original_tree  # byte-for-byte (P-20)

    def test_multi_chunk_roundtrip(self, tmp_path: Path) -> None:
        """Multi symbol × multi interval × multi chunk: replay is byte-identical."""
        original = tmp_path / "lake-a"
        replayed = tmp_path / "lake-b"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        _run_audited_ingest(
            original,
            audit_path,
            symbols=["NIFTY", "SENSEX"],
            intervals=["1minute", "1day"],
            start=_START,
            end=_MULTI_END,  # > one 30-day 1-minute chunk → multiple windows
        )
        # 2 symbols × 2 intervals: RUN_START + 4 × PAIR_DONE + RUN_COMPLETE.
        assert verify_chain(audit_path) == 6
        entries = read_entries(audit_path)
        assert {e.subject for e in entries if e.action == "INGEST_PAIR_DONE"} == {
            "NIFTY:1minute",
            "NIFTY:1day",
            "SENSEX:1minute",
            "SENSEX:1day",
        }
        _replay_from_audit(audit_path, replayed)
        original_tree = _bars_tree(original)
        # The multi-chunk span really spans partitions (Jan + Feb months).
        assert len(original_tree) >= 4
        assert _bars_tree(replayed) == original_tree  # byte-for-byte (P-20)

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
    """L3 oracle over a REAL ingest run (P-20 + DEC-L3-REPLAY-ORACLE-001)."""

    def test_live_set_matches_replayed_set(self, tmp_path: Path) -> None:
        """The lake's live partition set matches the audit log's replayed subjects.

        Wraps :func:`brutex.orchestrator.replay.assert_dedup_consistent` around a
        real ingest run: the LIVE state = the ``SYMBOL:interval`` pairs actually
        written to the lake; the REPLAYED state = the chain's ``INGEST_PAIR_DONE``
        subjects. Consistent → silence; a partition written past the audit log
        (or an audited pair missing from the lake) → a loud ``AuditError``.
        """
        lake = tmp_path / "lake"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        _run_audited_ingest(
            lake,
            audit_path,
            symbols=["NIFTY", "BANKNIFTY"],
            intervals=["1minute"],
            start=_START,
            end=_END,
        )
        live = _lake_pair_subjects(lake)
        assert live == {"NIFTY:1minute", "BANKNIFTY:1minute"}
        # Strict oracle: silence iff consistent.
        assert_dedup_consistent(
            audit_path=audit_path,
            live_subjects=live,
            action_filter="INGEST_PAIR_DONE",
        )
        # And the diff view agrees, with an exact replayed count.
        diff = replay_audit_to_diff(
            audit_path=audit_path,
            live_subjects=live,
            action_filter="INGEST_PAIR_DONE",
        )
        assert diff.is_consistent
        assert diff.replayed_count == 2

    def test_unaudited_extra_partition_is_flagged(self, tmp_path: Path) -> None:
        """A lake pair the audit log never recorded trips the oracle (Rule #8:
        every material action must be audited — an extra live subject is a BUG)."""
        import pytest
        from brutex.orchestrator.audit import AuditError

        lake = tmp_path / "lake"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        _run_audited_ingest(
            lake,
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
