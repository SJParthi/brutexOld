"""Unit tests for brutex.data.audit_replay — the P-20 replay harness.

Covers the encoding round-trip, the action filter, and every failure
branch of :func:`assert_replay_byte_equals_original` so the harness
qualifies for the strict per-module 100% line+branch gate.

The happy-path roundtrip + L3-consistency paths are exercised by
``tests/integration/test_replay_audit_log.py``; this file pins the
edge + failure paths.

Decision: DEC-P20-REPLAY-HARNESS-001 (``docs/MIGRATION.md`` entry #580).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.audit_replay import (
    PARQUET_WRITE_ACTION,
    ReplayedWrite,
    _bar_from_payload,
    _bar_to_payload,
    assert_replay_byte_equals_original,
    partition_subject,
    replay_parquet_writes,
    write_and_audit,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, WriteResult
from brutex.orchestrator.audit import AuditError, AuditWriter

_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _bar(minute: int, *, month: int = 1, oi: int | None = None) -> CandleBar:
    px = Decimal(22000 + minute)
    return CandleBar(
        timestamp=datetime(2024, month, 15, 9, minute, tzinfo=UTC),
        open=px,
        high=px + Decimal(10),
        low=px - Decimal(5),
        close=px + Decimal(3),
        volume=1000 + minute,
        open_interest=oi,
    )


# ────────────────────────────────────────────────────────────────────────
# Encoding round-trip
# ────────────────────────────────────────────────────────────────────────


class TestBarEncoding:
    def test_roundtrip_preserves_bar_exactly_with_none_oi(self) -> None:
        bar = _bar(15)
        assert _bar_from_payload(_bar_to_payload(bar)) == bar

    def test_roundtrip_preserves_open_interest(self) -> None:
        bar = _bar(16, oi=4242)
        restored = _bar_from_payload(_bar_to_payload(bar))
        assert restored == bar
        assert restored.open_interest == 4242

    def test_payload_is_json_safe_strings(self) -> None:
        payload = _bar_to_payload(_bar(15))
        # Prices are exact Decimal strings (no float round-trip).
        assert payload["o"] == "22015"
        assert isinstance(payload["ts"], str)


class TestPartitionSubject:
    def test_subject_mirrors_partition_layout(self) -> None:
        subject = partition_subject(year=2024, month=3, **_COORDS)
        assert subject == "NSE/INDEX/NIFTY/1minute/2024/03"


# ────────────────────────────────────────────────────────────────────────
# replay_parquet_writes — action filter
# ────────────────────────────────────────────────────────────────────────


class TestReplayActionFilter:
    def test_non_matching_actions_are_skipped(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "run.jsonl"
        writer = ParquetWriter(lake_root=tmp_path / "orig")
        with AuditWriter.open(audit_path) as audit:
            # An interleaved non-PARQUET_WRITE action must be ignored.
            audit.append(action="RUN_START", subject="run:x", payload={})
            write_and_audit(writer=writer, audit=audit, bars=[_bar(15)], **_COORDS)
            audit.append(action="RUN_COMPLETE", subject="run:x", payload={})

        replayed = replay_parquet_writes(audit_path=audit_path, lake_root=tmp_path / "rep")
        assert len(replayed) == 1
        assert replayed[0].subject == "NSE/INDEX/NIFTY/1minute/2024/01"


# ────────────────────────────────────────────────────────────────────────
# assert_replay_byte_equals_original — failure branches
# ────────────────────────────────────────────────────────────────────────


class TestReplayFailureBranches:
    def test_hash_mismatch_raises(self, tmp_path: Path) -> None:
        """A PARQUET_WRITE entry whose recorded raw_hash is WRONG must be
        caught: the replayed file's real hash won't match the audited one."""
        audit_path = tmp_path / "run.jsonl"
        original = tmp_path / "orig"
        writer = ParquetWriter(lake_root=original)
        # Write the real file so the original lake exists + matches bytes…
        result = writer.write(bars=[_bar(15)], **_COORDS)
        with AuditWriter.open(audit_path) as audit:
            # …but record a BOGUS raw_hash in the payload.
            audit.append(
                action=PARQUET_WRITE_ACTION,
                subject=partition_subject(year=2024, month=1, **_COORDS),
                payload={
                    **_COORDS,
                    "year": 2024,
                    "month": 1,
                    "raw_hash": "0" * 64,  # deliberately wrong
                    "row_count": result.row_count,
                    "bars": [_bar_to_payload(_bar(15))],
                },
            )

        with pytest.raises(AuditError, match="raw_hash differs"):
            assert_replay_byte_equals_original(
                audit_path=audit_path,
                original_lake_root=original,
                replay_lake_root=tmp_path / "rep",
            )

    def test_partition_set_mismatch_raises(self, tmp_path: Path) -> None:
        """An EXTRA parquet file in the original lake (not in the audit log)
        makes the partition-file SET differ → raise."""
        audit_path = tmp_path / "run.jsonl"
        original = tmp_path / "orig"
        writer = ParquetWriter(lake_root=original)
        with AuditWriter.open(audit_path) as audit:
            write_and_audit(writer=writer, audit=audit, bars=[_bar(15)], **_COORDS)

        # Plant an extra partition file the audit log never recorded.
        rogue = original / "bars" / "NSE" / "INDEX" / "ROGUE" / "1minute" / "2024" / "01.parquet"
        rogue.parent.mkdir(parents=True, exist_ok=True)
        rogue.write_bytes(b"rogue")

        with pytest.raises(AuditError, match="file SET differs"):
            assert_replay_byte_equals_original(
                audit_path=audit_path,
                original_lake_root=original,
                replay_lake_root=tmp_path / "rep",
            )

    def test_clean_replay_returns_writes(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "run.jsonl"
        original = tmp_path / "orig"
        writer = ParquetWriter(lake_root=original)
        with AuditWriter.open(audit_path) as audit:
            write_and_audit(writer=writer, audit=audit, bars=[_bar(15)], **_COORDS)

        replayed = assert_replay_byte_equals_original(
            audit_path=audit_path,
            original_lake_root=original,
            replay_lake_root=tmp_path / "rep",
        )
        assert [rw.subject for rw in replayed] == ["NSE/INDEX/NIFTY/1minute/2024/01"]


class TestReplayedWrite:
    def test_hash_matches_property(self) -> None:
        result = WriteResult(
            path=Path("x.parquet"),
            row_count=1,
            raw_hash="a" * 64,
            first_ts=datetime(2024, 1, 15, tzinfo=UTC),
            last_ts=datetime(2024, 1, 15, tzinfo=UTC),
            bytes_written=10,
        )
        assert ReplayedWrite(subject="s", result=result, expected_raw_hash="a" * 64).hash_matches
        assert not ReplayedWrite(
            subject="s", result=result, expected_raw_hash="b" * 64
        ).hash_matches
