"""Write-side uniqueness invariant tests (DEC-LAKE-WRITE-UNIQUE-001).

A vendor payload can physically carry duplicate same-timestamp rows (measured:
135 duplicate 1-minute rows in one ADANIENT January window, reproduced across
two pulls). Before the fix the FRESH render stored the batch verbatim
(``sorted()`` preserves duplicates), the no-op merge path returned an
already-poisoned file's bytes forever, and ``_read_existing_bars`` collapsed
stored duplicates silently LAST-wins. This suite pins the whole invariant:

* **In-batch dedup observability** on the fresh AND merge paths — the fresh
  branch collapses via ``_dedup_batch`` (DEC-FRESH-WRITE-BATCH-DEDUP-001, the
  concurrent main fix this suite builds on), the merge branch via its
  timestamp-keyed dict; EITHER collapse now emits one aggregated
  ``lake.vendor_duplicate_rows`` WARNING per write (never silent), and a
  differing keep-first pair records a :class:`BarConflict`;
* **Merge self-heal** — a physically-poisoned stored file forces a deduped
  rewrite even when the merge adds nothing (``lake.partition.dedup_healed``),
  keep-FIRST winner proven;
* **Keep-first read collapse** — ``lake.partition.duplicate_collapsed`` logged
  on BOTH decode paths (columnar + the per-row overflow fallback);
* **P9 tripwire** — ``_assert_strictly_increasing`` raises a loud
  :class:`WriterError` naming the partition (a genuine invariant assert);
* **Idempotence** — a duplicated batch re-run is byte-identical (Rule #7).

Whitebox: exercises the private ``_assert_strictly_increasing`` /
``_stored_physical_rows`` helpers directly (the DEC-P0-017 convention), so the
file silences ``reportPrivateUsage``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.errors import WriterError
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")


_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _ts(minute: int) -> datetime:
    """A UTC 1-minute instant inside the June-2024 partition."""
    return datetime(2024, 6, 3, 4, minute, tzinfo=UTC)


def _bar(minute: int, *, close: str = "100.00", volume: int = 10) -> CandleBar:
    """A flat bar at :func:`_ts`\\ (*minute*) whose OHLC all equal *close*."""
    c = Decimal(close)
    return CandleBar(
        timestamp=_ts(minute),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


def _dest(tmp_path: Path) -> Path:
    return build_partition_path(
        lake_root=tmp_path,
        exchange=_COORDS["exchange"],
        segment=_COORDS["segment"],
        symbol=_COORDS["symbol"],
        interval=_COORDS["interval"],
        year=2024,
        month=6,
    )


def _poison_partition(dest: Path, rows: Sequence[tuple[datetime, float, int]]) -> None:
    """Write a RAW pyarrow partition (simulating the operator's poisoned lake).

    Each row is ``(timestamp, price, volume)`` with OHLC all equal to *price* —
    duplicate timestamps are written VERBATIM, exactly the way the pre-fix fresh
    render persisted a duplicated vendor batch.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [price for _, price, _ in rows]
    table = pa.table(
        {
            "timestamp": pa.array([ts for ts, _, _ in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([vol for _, _, vol in rows], type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


def _stored_frame(dest: Path):  # type: ignore[no-untyped-def]
    import polars as pl

    return pl.read_parquet(dest).sort("timestamp")


# ── FRESH path in-batch dedup ──────────────────────────────────────────────


class TestFreshWriteDedup:
    def test_identical_dups_store_unique_timestamps_and_warn(self, tmp_path: Path) -> None:
        """FAILS on unfixed main: the fresh render stored the duplicate row verbatim."""
        writer = ParquetWriter(lake_root=tmp_path)
        with capture_logs() as logs:
            result = writer.write(bars=[_bar(0), _bar(0), _bar(1)], **_COORDS)
        assert result.row_count == 2
        assert result.bars_added == 2  # the DEDUPED count — a dup is never counted twice
        assert result.conflicts == ()  # identical values → no conflict recorded
        stored = _stored_frame(result.path)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1)]
        warns = _events(logs, "lake.vendor_duplicate_rows")
        assert len(warns) == 1, "exactly ONE aggregated WARNING per write"
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["symbol"] == "NIFTY"
        assert warns[0]["interval"] == "1minute"
        assert warns[0]["dropped"] == 1
        assert warns[0]["window_start"] == _ts(0).isoformat()
        assert warns[0]["window_end"] == _ts(1).isoformat()

    def test_differing_dup_records_conflict_and_first_in_batch_wins(self, tmp_path: Path) -> None:
        """FAILS on unfixed main: both rows were stored; no conflict was recorded."""
        writer = ParquetWriter(lake_root=tmp_path)
        first = _bar(0, close="100.00")
        restated = _bar(0, close="999.00")
        with capture_logs() as logs:
            result = writer.write(bars=[first, restated, _bar(1)], **_COORDS)
        assert result.row_count == 2
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict.timestamp == _ts(0)
        assert conflict.stored.close == Decimal("100.00")  # first-in-batch won
        assert conflict.vendor.close == Decimal("999.00")  # the discarded re-statement
        stored = _stored_frame(result.path)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1)]
        assert stored["close"].to_list()[0] == 100.0
        warns = _events(logs, "lake.vendor_duplicate_rows")
        assert len(warns) == 1
        assert warns[0]["dropped"] == 1

    def test_clean_batch_emits_no_duplicate_warning(self, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path)
        with capture_logs() as logs:
            result = writer.write(bars=[_bar(0), _bar(1)], **_COORDS)
        assert result.row_count == 2
        assert _events(logs, "lake.vendor_duplicate_rows") == []

    def test_latest_wins_in_batch_dup_takes_later_and_warns(self, tmp_path: Path) -> None:
        """The latest-wins (derived 1day) mode mirrors the merge restate rule for an
        in-batch duplicate — the LATER occurrence wins, no conflict recorded
        (DEC-FRESH-WRITE-BATCH-DEDUP-001) — and the collapse is still COUNTED in
        the aggregated WARNING (the observability this PR adds): defensive only,
        the 1day deriver emits unique buckets by construction.
        """
        writer = ParquetWriter(lake_root=tmp_path)
        with capture_logs() as logs:
            result = writer.write(
                bars=[_bar(0, close="100.00"), _bar(0, close="999.00")],
                merge_mode="latest-wins",
                **_COORDS,
            )
        assert result.row_count == 1
        assert result.conflicts == ()
        assert _stored_frame(result.path)["close"].to_list() == [999.0]
        warns = _events(logs, "lake.vendor_duplicate_rows")
        assert len(warns) == 1
        assert warns[0]["dropped"] == 1


# ── MERGE path in-batch dedup ──────────────────────────────────────────────


class TestMergeWriteBatchDedup:
    def test_merge_batch_with_dups_dedups_and_carries_conflicts(self, tmp_path: Path) -> None:
        """FAILS on unfixed main: the in-batch conflict was never surfaced on the result."""
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(bars=[_bar(5)], **_COORDS)  # existing partition → merge path
        with capture_logs() as logs:
            result = writer.write(
                bars=[_bar(0, close="100.00"), _bar(0, close="999.00"), _bar(1)],
                **_COORDS,
            )
        assert result.row_count == 3  # ts0 + ts1 + the pre-stored ts5
        assert result.bars_added == 2
        assert len(result.conflicts) == 1
        assert result.conflicts[0].stored.close == Decimal("100.00")
        stored = _stored_frame(result.path)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1), _ts(5)]
        assert stored["close"].to_list()[0] == 100.0  # first-in-batch won
        warns = _events(logs, "lake.vendor_duplicate_rows")
        assert len(warns) == 1
        assert warns[0]["dropped"] == 1


# ── merge self-heal of an already-poisoned partition ───────────────────────


class TestPoisonedPartitionHeal:
    def test_poisoned_file_heals_on_merge_touch_keep_first_wins(self, tmp_path: Path) -> None:
        """FAILS on unfixed main: the poisoned rows survived the merge (last-wins, silent)."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        # First physical ts0 row carries 100.0 — the keep-FIRST winner to prove.
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11), (_ts(1), 101.0, 12)])
        with capture_logs() as logs:
            result = writer.write(bars=[_bar(2, close="102.00")], **_COORDS)
        assert result.row_count == 3
        assert result.bars_added == 1
        stored = _stored_frame(dest)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1), _ts(2)]
        assert stored["close"].to_list() == [100.0, 101.0, 102.0]  # FIRST row's values survive
        healed = _events(logs, "lake.partition.dedup_healed")
        assert len(healed) == 1
        assert healed[0]["log_level"] == "warning"
        assert healed[0]["kept"] == 2
        assert healed[0]["dropped"] == 1
        collapsed = _events(logs, "lake.partition.duplicate_collapsed")
        assert len(collapsed) == 1
        assert collapsed[0]["physical_rows"] == 3
        assert collapsed[0]["unique_timestamps"] == 2
        assert collapsed[0]["collapsed"] == 1

    def test_noop_merge_on_poisoned_closed_month_still_heals(self, tmp_path: Path) -> None:
        """FAILS on unfixed main: added==0 returned the poisoned bytes forever."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 100.0, 10), (_ts(1), 101.0, 12)])
        # The incoming bar is ALREADY stored identically → the merge adds nothing.
        with capture_logs() as logs:
            result = writer.write(bars=[_bar(1, close="101.00", volume=12)], **_COORDS)
        assert result.bars_added == 0
        assert result.row_count == 2
        stored = _stored_frame(dest)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1)]  # healed on disk
        assert len(_events(logs, "lake.partition.dedup_healed")) == 1

    def test_latest_wins_heal_still_keeps_first_physical_row(self, tmp_path: Path) -> None:
        """ACCEPTED-BY-DESIGN divergence pin (hostile-review ask, DEC-LAKE-WRITE-UNIQUE-001):
        the self-heal READ collapse is deliberately MODE-INDEPENDENT keep-FIRST — a
        poisoned stored file healed under ``latest-wins`` keeps the FIRST physical
        occurrence, even though the fresh latest-wins in-batch rule (#1137) would
        keep the LATER one for the same payload. Confined to latest-wins (only the
        derived-1day path uses it, where a PHYSICAL duplicate daily timestamp is
        implausible); the production keep-first spine is unaffected. This pin makes
        the documented edge a regression guard, not an accident.
        """
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        # Same-poisoned payload as the fresh latest-wins test: first row 100.0,
        # later duplicate 999.0 — the heal must keep 100.0 (FIRST physical row).
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 999.0, 11), (_ts(1), 101.0, 12)])
        with capture_logs() as logs:
            result = writer.write(
                bars=[_bar(2, close="102.00")], merge_mode="latest-wins", **_COORDS
            )
        assert result.row_count == 3
        stored = _stored_frame(dest)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1), _ts(2)]
        assert stored["close"].to_list() == [100.0, 101.0, 102.0]  # FIRST wins, mode-independent
        assert len(_events(logs, "lake.partition.dedup_healed")) == 1

    def test_healed_file_re_touch_is_a_byte_identical_noop(self, tmp_path: Path) -> None:
        """After the heal, the same touch is a pure no-op — idempotent (Rule #7)."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 100.0, 10)])
        healed = writer.write(bars=[_bar(0, close="100.00", volume=10)], **_COORDS)
        healed_bytes = dest.read_bytes()
        with capture_logs() as logs:
            again = writer.write(bars=[_bar(0, close="100.00", volume=10)], **_COORDS)
        assert dest.read_bytes() == healed_bytes
        assert again.raw_hash == healed.raw_hash == hashlib.sha256(healed_bytes).hexdigest()
        assert again.bars_added == 0
        assert _events(logs, "lake.partition.dedup_healed") == []

    def test_probe_failure_skips_heal_never_misheals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Honest envelope: a failed footer probe SKIPS the heal (poison persists,
        logged) rather than guessing — the next successful probe heals it."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 100.0, 10)])
        monkeypatch.setattr(writer, "_stored_physical_rows", lambda _dest, _existing_bytes: None)
        with capture_logs() as logs:
            result = writer.write(bars=[_bar(0, close="100.00", volume=10)], **_COORDS)
        assert result.bars_added == 0
        assert _events(logs, "lake.partition.dedup_healed") == []
        # The file is untouched (still physically poisoned) — never mis-healed.
        assert ParquetWriter._stored_physical_rows(dest, dest.read_bytes()) == 2

    def test_poisoned_file_on_per_row_fallback_path_still_heals(self, tmp_path: Path) -> None:
        """A poisoned file whose price overflows the columnar snap heals via the
        per-row fallback — the keep-first collapse + log live on BOTH decode paths."""
        writer = ParquetWriter(lake_root=tmp_path)
        dest = _dest(tmp_path)
        huge = 1e22  # overflows the snap's Decimal(precision=38) → per-row fallback
        _poison_partition(dest, [(_ts(0), huge, 10), (_ts(0), huge, 10), (_ts(1), 101.0, 12)])
        with capture_logs() as logs:
            result = writer.write(bars=[_bar(2, close="102.00")], **_COORDS)
        assert result.row_count == 3
        stored = _stored_frame(dest)
        assert stored["timestamp"].to_list() == [_ts(0), _ts(1), _ts(2)]
        assert stored["open"].to_list() == [huge, 101.0, 102.0]
        assert len(_events(logs, "lake.partition.duplicate_collapsed")) == 1
        assert len(_events(logs, "lake.partition.dedup_healed")) == 1


# ── P9 tripwire ────────────────────────────────────────────────────────────


class TestStrictlyIncreasingTripwire:
    def test_passes_on_strictly_increasing(self) -> None:
        ParquetWriter._assert_strictly_increasing(Path("p.parquet"), [_bar(0), _bar(1)])

    def test_raises_loud_on_duplicate_naming_partition(self) -> None:
        with pytest.raises(WriterError, match=r"poisoned\.parquet"):
            ParquetWriter._assert_strictly_increasing(Path("poisoned.parquet"), [_bar(0), _bar(0)])

    def test_raises_loud_on_decreasing(self) -> None:
        with pytest.raises(WriterError, match="strictly increasing"):
            ParquetWriter._assert_strictly_increasing(Path("p.parquet"), [_bar(1), _bar(0)])


# ── footer row-count probe ─────────────────────────────────────────────────


class TestStoredPhysicalRows:
    def test_counts_footer_rows(self, tmp_path: Path) -> None:
        dest = _dest(tmp_path)
        _poison_partition(dest, [(_ts(0), 100.0, 10), (_ts(0), 100.0, 10), (_ts(1), 101.0, 1)])
        assert ParquetWriter._stored_physical_rows(dest, dest.read_bytes()) == 3

    def test_garbage_bytes_probe_returns_none_and_logs(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            probed = ParquetWriter._stored_physical_rows(_dest(tmp_path), b"not a parquet footer")
        assert probed is None
        failures = _events(logs, "lake.partition.rowcount_probe_failed")
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"


# ── idempotent re-run (Rule #7) ────────────────────────────────────────────


class TestIdempotentRerun:
    def test_duplicated_batch_rerun_is_byte_identical(self, tmp_path: Path) -> None:
        """FAILS on unfixed main: the first (fresh) write stored 3 rows, the second
        (merge) deduped to 2 — the two runs were NOT byte-identical."""
        writer = ParquetWriter(lake_root=tmp_path)
        batch = [_bar(0), _bar(0), _bar(1)]
        first = writer.write(bars=list(batch), **_COORDS)
        first_bytes = first.path.read_bytes()
        second = writer.write(bars=list(batch), **_COORDS)
        assert second.path.read_bytes() == first_bytes
        assert second.raw_hash == first.raw_hash
        assert second.row_count == first.row_count == 2
        assert second.bars_added == 0
