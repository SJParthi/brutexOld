"""Audit-log → Parquet replay integration tests (invariant **P-20**).

Per ``docs/TRACK1_VERIFICATION_PYRAMID.md`` rows G2 (Reproducibility)
+ G7 (Audit chain), this is the cross-layer correctness anchor:

> ``replay(audit_log) → Parquet`` byte-equals the original Parquet
> (invariant P-20).

The harness under test is :mod:`brutex.data.audit_replay`
(DEC-P20-REPLAY-HARNESS-001, MIGRATION entry #580): a tiny ingest job
writes Parquet output + appends every action to the hash-chained audit
log via :func:`~brutex.data.audit_replay.write_and_audit`; the audit log
is then replayed via
:func:`~brutex.data.audit_replay.assert_replay_byte_equals_original`
into a FRESH lake root and the bytes are asserted identical.

The L3 dedup oracle in
:func:`brutex.orchestrator.replay.assert_dedup_consistent`
(DEC-L3-REPLAY-ORACLE-001, MIGRATION entry #241) validates the
**dedup-subject** projection; the consistency test below wraps a real
ingest run through it.

Previously these were ``pytest.skip`` skeletons ("Phase 2 pending — needs
Parquet writer"); the deterministic writer now exists
(:class:`brutex.data.parquet_writer.ParquetWriter`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.audit_replay import (
    PARQUET_WRITE_ACTION,
    assert_replay_byte_equals_original,
    partition_subject,
    replay_parquet_writes,
    replay_summary_json,
    write_and_audit,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.orchestrator.audit import AuditError, AuditWriter, verify_chain
from brutex.orchestrator.replay import assert_dedup_consistent

# ────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────

#: Partition coordinates reused across the suite — NIFTY index spot, 1m.
_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _bar(minute: int, *, day: int = 15, month: int = 1, base: int = 22000) -> CandleBar:
    """Build one valid OHLCV bar at 2024-{month}-{day} 09:{minute} UTC.

    Prices vary with *minute* so distinct bars carry distinct content
    (a flat series would still pass byte-equality but wouldn't prove the
    payload preserves per-bar values).
    """
    px = Decimal(base + minute)
    return CandleBar(
        timestamp=datetime(2024, month, day, 9, minute, tzinfo=UTC),
        open=px,
        high=px + Decimal(10),
        low=px - Decimal(5),
        close=px + Decimal(3),
        volume=1000 + minute,
        open_interest=None,
    )


def _ingest(
    audit_path: Path,
    lake_root: Path,
    chunks: list[list[CandleBar]],
    *,
    resume_after: int | None = None,
) -> list[str]:
    """Run a tiny ingest job: write each chunk + audit it.

    Returns the list of partition subjects written (the "live set").

    If *resume_after* is set, the audit writer is CLOSED after that many
    chunks and re-opened with ``resume=True`` — modelling a crash/kill
    mid-run so the replay must rebuild from the resumed chain.
    """
    writer = ParquetWriter(lake_root=lake_root)
    subjects: list[str] = []

    audit = AuditWriter.open(audit_path)
    try:
        for idx, bars in enumerate(chunks):
            if resume_after is not None and idx == resume_after:
                audit.close()
                audit = AuditWriter.open(audit_path, resume=True)
            result = write_and_audit(writer=writer, audit=audit, bars=bars, **_COORDS)
            year, month = result.first_ts.year, result.first_ts.month
            subjects.append(partition_subject(year=year, month=month, **_COORDS))
    finally:
        audit.close()
    return subjects


# ────────────────────────────────────────────────────────────────────────
# P-20 roundtrip
# ────────────────────────────────────────────────────────────────────────


class TestAuditReplayRoundtrip:
    """``replay(audit_log) → Parquet`` byte-equals the original Parquet (P-20)."""

    def test_single_chunk_roundtrip(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "audit" / "run.jsonl"
        original = tmp_path / "lake_original"
        replay = tmp_path / "lake_replay"

        _ingest(audit_path, original, [[_bar(15), _bar(16), _bar(17)]])

        replayed = assert_replay_byte_equals_original(
            audit_path=audit_path,
            original_lake_root=original,
            replay_lake_root=replay,
        )
        assert len(replayed) == 1
        assert replayed[0].hash_matches
        # The hash-chain is intact (replay relies on an un-tampered log).
        assert verify_chain(audit_path) >= 1

    def test_multi_chunk_roundtrip(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "audit" / "run.jsonl"
        original = tmp_path / "lake_original"
        replay = tmp_path / "lake_replay"

        # Chunk 1 + 2 target the SAME month partition (merge-on-write);
        # chunk 3 targets a DIFFERENT month — proves both the merge path
        # and the multi-partition path replay byte-identically.
        _ingest(
            audit_path,
            original,
            [
                [_bar(15), _bar(16)],
                [_bar(17), _bar(18)],
                [_bar(15, month=2), _bar(16, month=2)],
            ],
        )

        replayed = assert_replay_byte_equals_original(
            audit_path=audit_path,
            original_lake_root=original,
            replay_lake_root=replay,
        )
        assert len(replayed) == 3
        assert all(rw.hash_matches for rw in replayed)
        # Two distinct partition files exist (Jan merged + Feb).
        assert len(list(original.rglob("*.parquet"))) == 2

    def test_resume_from_kill_roundtrip(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "audit" / "run.jsonl"
        original = tmp_path / "lake_original"
        replay = tmp_path / "lake_replay"

        # Three chunks; the audit writer is killed + resumed after the
        # first, so the chain spans an open→close→reopen boundary.
        _ingest(
            audit_path,
            original,
            [
                [_bar(15), _bar(16)],
                [_bar(17), _bar(18)],
                [_bar(19), _bar(20)],
            ],
            resume_after=1,
        )

        # The resumed chain must still verify AND replay byte-identically.
        assert verify_chain(audit_path) == 3
        replayed = assert_replay_byte_equals_original(
            audit_path=audit_path,
            original_lake_root=original,
            replay_lake_root=replay,
        )
        assert len(replayed) == 3
        assert all(rw.hash_matches for rw in replayed)

    def test_tampered_replay_is_detected(self, tmp_path: Path) -> None:
        """A divergent original lake makes the byte-equality assertion fail.

        Guards the failure path of P-20: if the lake the audit log claims
        to reproduce is NOT what replay produces, the harness raises
        rather than silently passing.
        """
        audit_path = tmp_path / "audit" / "run.jsonl"
        original = tmp_path / "lake_original"
        replay = tmp_path / "lake_replay"

        _ingest(audit_path, original, [[_bar(15), _bar(16)]])

        # Corrupt the stored partition AFTER ingest so replay != original.
        stored = next(original.rglob("*.parquet"))
        stored.write_bytes(b"not a parquet file")

        with pytest.raises(AuditError, match="byte content differs"):
            assert_replay_byte_equals_original(
                audit_path=audit_path,
                original_lake_root=original,
                replay_lake_root=replay,
            )

    def test_replay_summary_is_deterministic(self, tmp_path: Path) -> None:
        """The replay summary JSON is stable + reports per-partition hashes."""
        audit_path = tmp_path / "audit" / "run.jsonl"
        original = tmp_path / "lake_original"
        replay = tmp_path / "lake_replay"

        _ingest(audit_path, original, [[_bar(15)], [_bar(15, month=2)]])
        replayed = replay_parquet_writes(audit_path=audit_path, lake_root=replay)

        summary = replay_summary_json(replayed)
        assert summary == replay_summary_json(list(reversed(replayed)))  # sort-stable
        assert PARQUET_WRITE_ACTION  # action constant is the audit filter
        assert '"hash_matches":true' in summary


# ────────────────────────────────────────────────────────────────────────
# L3 dedup-subject consistency (DEC-L3-REPLAY-ORACLE-001)
# ────────────────────────────────────────────────────────────────────────


class TestAuditReplayConsistency:
    """L3 oracle: replay matches the live LMDB dedup set (P-20 + DEC-L3-REPLAY-ORACLE-001)."""

    def test_live_set_matches_replayed_set(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "audit" / "run.jsonl"
        original = tmp_path / "lake_original"

        # The "live set" is the set of partition subjects the ingest job
        # actually wrote; the audit log is its tamper-evident record.
        subjects = _ingest(
            audit_path,
            original,
            [[_bar(15)], [_bar(15, month=2)], [_bar(16, month=2)]],
        )
        live = frozenset(subjects)

        # The L3 oracle projects the audit log's PARQUET_WRITE subjects and
        # asserts they equal the live set — silence means consistent.
        assert_dedup_consistent(
            audit_path=audit_path,
            live_subjects=live,
            action_filter=PARQUET_WRITE_ACTION,
        )

    def test_missing_live_subject_is_inconsistent(self, tmp_path: Path) -> None:
        """If the live set drops a partition the audit log recorded, the
        oracle flags it (dedup-cache loss / bypassed delete)."""
        audit_path = tmp_path / "audit" / "run.jsonl"
        original = tmp_path / "lake_original"

        _ingest(audit_path, original, [[_bar(15)], [_bar(15, month=2)]])

        with pytest.raises(AuditError, match="inconsistent"):
            assert_dedup_consistent(
                audit_path=audit_path,
                live_subjects=frozenset(),  # live lost everything
                action_filter=PARQUET_WRITE_ACTION,
            )
