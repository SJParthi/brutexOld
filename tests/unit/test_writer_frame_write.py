"""Unit gate for the FRAME-DIRECT write siblings (DEC-PREBUILD-FRAME-WRITE-001).

``ParquetWriter.write_frame`` + ``ParquetBarSink.append_frame`` + the Seam-D/E
split privates: every admission guard both directions, WriteResult field
parity fast-vs-legacy on fresh AND no-op, the burn-in/shadow/autorevert
orchestration, the degrade re-entry (the legacy ``write()`` called exactly
once — spied), the never-swallowed ``LakeWriteHalt``/``MemoryError`` law, the
sink-level contracts (empty early-return, ``bars_appended = frame.height``,
ascending-month iteration, catalog parity), and the ``_render_frame_bytes``
schema assert.

Whitebox: exercises writer privates, so per the DEC-P0-017 convention the
file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data import frame_write_gate as gate
from brutex.data.errors import LakeWriteHalt, WriterError
from brutex.data.frame_write_gate import (
    PREBUILD_FRAME_WRITE_ENV,
    DegradeReason,
    FrameWriteStats,
)
from brutex.data.models import CandleBar, CatalogEntry
from brutex.data.parquet_writer import ParquetWriter, WriteResult
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_daily_resample import frame_to_candle_bars

pytest.importorskip("polars")

import polars as pl

_T0 = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)  # 09:15 IST, a regular session
_COORDS = {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "5minute"}


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh gate state + the flag defaulting OFF; tests opt in per case."""
    monkeypatch.delenv(PREBUILD_FRAME_WRITE_ENV, raising=False)
    gate.reset_gate_for_tests()
    gate.reset_observability_memo_for_tests()


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    return tmp_path / "lake"


@pytest.fixture
def writer(lake: Path) -> ParquetWriter:
    return ParquetWriter(lake_root=lake, partition_log_level="debug")


def _frame(
    n: int = 4,
    *,
    start: datetime = _T0,
    step_minutes: int = 5,
    base: float = 100.0,
    oi: list[int | None] | None = None,
    with_oi: bool = False,
) -> pl.DataFrame:
    """A healthy resampler-shaped frame (sorted, unique, positive, ordered)."""
    data: dict[str, Any] = {
        "ts_utc": [start + timedelta(minutes=step_minutes * i) for i in range(n)],
        "open": [base + i for i in range(n)],
        "high": [base + 10 + i for i in range(n)],
        "low": [base - 5 + i for i in range(n)],
        "close": [base + 2 + i for i in range(n)],
        "volume": [10 + i for i in range(n)],
    }
    schema: dict[str, Any] = {
        "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
    }
    if with_oi or oi is not None:
        data["open_interest"] = oi if oi is not None else [500 + i for i in range(n)]
        schema["open_interest"] = pl.Int64
    return pl.DataFrame(data, schema=schema)


def _wf(writer: ParquetWriter, frame: pl.DataFrame, **kwargs: Any) -> WriteResult:
    params: dict[str, Any] = {**_COORDS, "frame": frame, "merge_mode": "latest-wins"}
    params.update(kwargs)
    return writer.write_frame(**params)


def _legacy_write(writer: ParquetWriter, frame: pl.DataFrame, **kwargs: Any) -> WriteResult:
    params: dict[str, Any] = {**_COORDS}
    params.update(kwargs)
    return writer.write(
        bars=frame_to_candle_bars(frame, absent_open_interest=0),
        merge_mode="latest-wins",
        **params,
    )


def _dest(lake: Path) -> Path:
    return lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "5minute" / "2024" / "03.parquet"


def _steady_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-burn-in ON mode: the fast lane SERVES bytes."""
    monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "1")
    gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)


class _LegacySpy:
    """Counts ParquetWriter.write invocations (the degrade re-entry law)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        original = ParquetWriter.write

        def counting(inner_self: ParquetWriter, **kwargs: Any) -> WriteResult:
            self.calls += 1
            return original(inner_self, **kwargs)

        monkeypatch.setattr(ParquetWriter, "write", counting)


# ────────────────────────────────────────────────────────────────────────
# Entry contracts
# ────────────────────────────────────────────────────────────────────────


class TestEntryContracts:
    def test_merge_mode_refusal(self, writer: ParquetWriter) -> None:
        """Fail-closed: keep-first is never re-implemented columnar."""
        with pytest.raises(WriterError, match="latest-wins"):
            _wf(writer, _frame(), merge_mode="keep-first")

    def test_empty_frame_refusal(self, writer: ParquetWriter) -> None:
        """Mirrors write()'s empty-bars refusal (the sink owns the C13 early-return)."""
        with pytest.raises(WriterError, match="empty frame"):
            _wf(writer, _frame(0))

    def test_multi_month_frame_raises_like_legacy(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G6 is a CALLER bug — the same WriterError as legacy, never a degrade."""
        _steady_on(monkeypatch)
        straddle = _frame(2, start=datetime(2024, 3, 31, 23, 55, tzinfo=UTC), step_minutes=10)
        with pytest.raises(WriterError, match="span multiple partitions"):
            _wf(writer, straddle)

    def test_mode_off_delegates_silently(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G0 OFF verdict: legacy delegate, no count, bytes = legacy bytes."""
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "0")
        stats = FrameWriteStats()
        result = _wf(writer, _frame(), stats=stats)
        assert result.file_rewritten is True
        assert stats.fast == 0
        assert stats.degraded == 0
        twin_root = lake.parent / "twin"
        twin = ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        _legacy_write(twin, _frame())
        assert _dest(lake).read_bytes() == _dest(twin_root).read_bytes()

    def test_autoreverted_latch_short_circuits(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _steady_on(monkeypatch)
        gate.autorevert("test_latch")
        spy = _LegacySpy(monkeypatch)
        stats = FrameWriteStats()
        result = _wf(writer, _frame(), stats=stats)
        assert result.file_rewritten is True
        assert spy.calls == 1  # the legacy delegate, exactly once
        assert stats.degraded == 0  # a gate verdict, never a counted degrade


# ────────────────────────────────────────────────────────────────────────
# Admission guards G1–G5 (each both directions)
# ────────────────────────────────────────────────────────────────────────


class TestAdmissionGuards:
    def _reason(self, frame: pl.DataFrame) -> DegradeReason | None:
        return ParquetWriter._frame_admission_reason(frame)

    def test_healthy_frame_admits(self) -> None:
        assert self._reason(_frame()) is None
        assert self._reason(_frame(with_oi=True)) is None
        assert self._reason(_frame(1)) is None  # height-1: the G2 skip branch

    def test_g1_missing_required_column(self) -> None:
        assert self._reason(_frame().drop("close")) is DegradeReason.SCHEMA_MISMATCH

    def test_g1_extra_column(self) -> None:
        bad = _frame().with_columns(pl.lit(1).alias("stray"))
        assert self._reason(bad) is DegradeReason.SCHEMA_MISMATCH

    def test_g1_wrong_ts_dtype(self) -> None:
        naive = _frame().with_columns(pl.col("ts_utc").dt.replace_time_zone(None))
        assert self._reason(naive) is DegradeReason.SCHEMA_MISMATCH

    def test_g1_wrong_price_dtype(self) -> None:
        bad = _frame().with_columns(pl.col("open").cast(pl.Float32))
        assert self._reason(bad) is DegradeReason.SCHEMA_MISMATCH

    def test_g1_non_integer_volume(self) -> None:
        bad = _frame().with_columns(pl.col("volume").cast(pl.Float64))
        assert self._reason(bad) is DegradeReason.SCHEMA_MISMATCH

    def test_g1_non_integer_oi(self) -> None:
        bad = _frame(with_oi=True).with_columns(pl.col("open_interest").cast(pl.Float64))
        assert self._reason(bad) is DegradeReason.SCHEMA_MISMATCH

    def test_g1_null_timestamp(self) -> None:
        bad = _frame(3).with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(None)
            .otherwise(pl.col("ts_utc"))
            .alias("ts_utc")
        )
        assert self._reason(bad) is DegradeReason.SCHEMA_MISMATCH

    def test_g2_duplicate_timestamp(self) -> None:
        f = _frame(3)
        dup = pl.concat([f, f.slice(1, 1)]).sort("ts_utc")
        assert self._reason(dup) is DegradeReason.DUP_OR_UNSORTED_TS

    def test_g2_unsorted_timestamps(self) -> None:
        unsorted = _frame(3).reverse()
        assert self._reason(unsorted) is DegradeReason.DUP_OR_UNSORTED_TS

    def test_g3_null_volume(self) -> None:
        bad = _frame(3).with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(None)
            .otherwise(pl.col("volume"))
            .alias("volume")
        )
        assert self._reason(bad) is DegradeReason.DOMAIN_VIOLATION

    def test_g3_negative_volume(self) -> None:
        bad = _frame(2).with_columns(pl.Series("volume", [-1, 5], dtype=pl.Int64))
        assert self._reason(bad) is DegradeReason.DOMAIN_VIOLATION

    def test_g3_negative_oi(self) -> None:
        bad = _frame(2, oi=[-3, 5])
        assert self._reason(bad) is DegradeReason.DOMAIN_VIOLATION

    def test_g3_null_oi_is_fine(self) -> None:
        assert self._reason(_frame(2, oi=[None, 5])) is None

    def test_g3_null_price(self) -> None:
        bad = _frame(2).with_columns(pl.Series("close", [None, 100.0], dtype=pl.Float64))
        assert self._reason(bad) is DegradeReason.DOMAIN_VIOLATION

    @pytest.mark.parametrize("poison", [float("nan"), float("inf"), float("-inf")])
    def test_g3_non_finite_price(self, poison: float) -> None:
        bad = _frame(2).with_columns(pl.Series("high", [poison, 200.0], dtype=pl.Float64))
        assert self._reason(bad) is DegradeReason.DOMAIN_VIOLATION

    def test_g3_high_below_low(self) -> None:
        bad = _frame(2).with_columns(pl.Series("high", [1.0, 200.0], dtype=pl.Float64))
        assert self._reason(bad) is DegradeReason.DOMAIN_VIOLATION

    def test_g3_open_outside_range(self) -> None:
        bad = _frame(2).with_columns(pl.Series("open", [10_000.0, 101.0], dtype=pl.Float64))
        assert self._reason(bad) is DegradeReason.DOMAIN_VIOLATION

    def test_g3_close_outside_range(self) -> None:
        bad = _frame(2).with_columns(pl.Series("close", [0.01, 102.0], dtype=pl.Float64))
        assert self._reason(bad) is DegradeReason.DOMAIN_VIOLATION

    def test_g4_magnitude_bound(self) -> None:
        bad = _frame(1, base=1e12)
        assert self._reason(bad) is DegradeReason.MAGNITUDE_OVERFLOW
        fine = _frame(1, base=1e11)
        assert self._reason(fine) is None

    def test_g5_snap_overflow_degrades(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The snapped-None branch (production-unreachable behind G4) degrades."""
        _steady_on(monkeypatch)
        monkeypatch.setattr(ParquetWriter, "_snap_frame_prices", staticmethod(lambda frame: None))
        spy = _LegacySpy(monkeypatch)
        stats = FrameWriteStats()
        result = _wf(writer, _frame(), stats=stats)
        assert result.file_rewritten is True
        assert spy.calls == 1
        assert stats.degrade_reasons == {"snap_overflow": 1}

    def test_guard_failure_routes_to_legacy_same_bytes(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A degraded partition's BYTES are the legacy bytes (off-grid source ok)."""
        _steady_on(monkeypatch)
        # A dup-ts frame degrades (G2) — legacy's latest-wins dedup handles it.
        f = _frame(3)
        dup = pl.concat([f, f.slice(1, 1)]).sort("ts_utc")
        stats = FrameWriteStats()
        _wf(writer, dup, stats=stats)
        assert stats.degrade_reasons == {"dup_or_unsorted_ts": 1}
        twin_root = lake.parent / "twin"
        twin = ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        _legacy_write(twin, dup)
        assert _dest(lake).read_bytes() == _dest(twin_root).read_bytes()

    def test_g3_degrade_raises_todays_exact_error(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C8: the poison row raises CandleBar's OWN ValueError via the legacy path."""
        _steady_on(monkeypatch)
        bad = _frame(2).with_columns(pl.Series("volume", [-1, 5], dtype=pl.Int64))
        with pytest.raises(ValueError, match="volume"):
            _wf(writer, bad)


# ────────────────────────────────────────────────────────────────────────
# Fresh lane
# ────────────────────────────────────────────────────────────────────────


class TestFreshLane:
    def test_steady_fresh_bytes_and_result_equal_legacy(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The load-bearing parity: fast fresh ≡ legacy fresh, bytes + fields."""
        _steady_on(monkeypatch)
        spy = _LegacySpy(monkeypatch)
        stats = FrameWriteStats()
        fast = _wf(writer, _frame(with_oi=True), stats=stats)
        assert spy.calls == 0  # the fast lane SERVED — no legacy call
        assert stats.fast == 1

        twin_root = lake.parent / "twin"
        twin = ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        legacy = _legacy_write(twin, _frame(with_oi=True))
        assert _dest(lake).read_bytes() == _dest(twin_root).read_bytes()
        for field in (
            "row_count",
            "raw_hash",
            "first_ts",
            "last_ts",
            "bytes_written",
            "bars_added",
            "conflicts",
            "vendor_dup_rows_dropped",
            "file_rewritten",
        ):
            assert getattr(fast, field) == getattr(legacy, field), field

    def test_steady_fresh_spot_without_oi_column(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent OI column → stored open_interest = 0 (the spot convention, C6)."""
        _steady_on(monkeypatch)
        _wf(writer, _frame())
        stored = pl.read_parquet(_dest(lake))
        assert stored.get_column("open_interest").to_list() == [0, 0, 0, 0]

    def test_steady_fresh_null_oi_maps_to_absent_value(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Present-but-null OI → absent_open_interest (F&O intraday null→0, C6)."""
        _steady_on(monkeypatch)
        _wf(writer, _frame(2, oi=[None, 7]))
        stored = pl.read_parquet(_dest(lake))
        assert stored.get_column("open_interest").to_list() == [0, 7]

    def test_absent_open_interest_none_keeps_nulls(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sibling signature's None arm (not a Tier-1 caller): nulls stay null."""
        _steady_on(monkeypatch)
        _wf(writer, _frame(2, oi=[None, 7]), absent_open_interest=None)
        stored = pl.read_parquet(_dest(lake))
        assert stored.get_column("open_interest").to_list() == [None, 7]

    def test_absent_open_interest_none_without_column(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _steady_on(monkeypatch)
        _wf(writer, _frame(2), absent_open_interest=None)
        stored = pl.read_parquet(_dest(lake))
        assert stored.get_column("open_interest").to_list() == [None, None]

    def test_burn_in_ships_legacy_bytes_and_consumes_sample(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "1")
        gate.reset_gate_for_tests(fresh_remaining=2, merge_remaining=0)
        spy = _LegacySpy(monkeypatch)
        stats = FrameWriteStats()
        result = _wf(writer, _frame(), stats=stats)
        assert spy.calls == 1  # the legacy lane ran and SHIPPED
        assert result.file_rewritten is True
        assert stats.fast == 1  # a verified admission counts
        assert gate._FRESH_REMAINING[0] == 1  # one sample consumed
        assert not gate.autoreverted()
        twin_root = lake.parent / "twin"
        twin = ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        _legacy_write(twin, _frame())
        assert _dest(lake).read_bytes() == _dest(twin_root).read_bytes()

    def test_burn_in_mismatch_autoreverts_and_ships_legacy(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A planted fast-lane fault: legacy bytes ship, the latch sets, ERROR fires."""
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "1")
        gate.reset_gate_for_tests(fresh_remaining=8, merge_remaining=8)
        # Corrupt the FAST lane only: a wrong OI mapping (never touches legacy).
        original = ParquetWriter._fresh_render_frame

        def corrupted(snapped: pl.DataFrame, *, absent_open_interest: int | None) -> pl.DataFrame:
            return original(snapped, absent_open_interest=99)

        monkeypatch.setattr(ParquetWriter, "_fresh_render_frame", staticmethod(corrupted))
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            result = _wf(writer, _frame())
        assert gate.autoreverted()
        errors = [e for e in logs if e["event"] == "prebuild.frame_write.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["lane"] == "fresh"
        assert "raw_hash" in errors[0]["diverged_fields"]
        assert gate._FRESH_REMAINING[0] == 8  # a mismatch never consumes
        # LEGACY bytes shipped — the fast bytes never touched disk.
        assert result.file_rewritten is True
        twin_root = lake.parent / "twin"
        twin = ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        _legacy_write(twin, _frame())
        assert _dest(lake).read_bytes() == _dest(twin_root).read_bytes()
        # Every LATER call short-circuits to legacy (one write per call).
        spy = _LegacySpy(monkeypatch)
        _wf(writer, _frame(base=200.0, start=_T0 + timedelta(days=1)))
        assert spy.calls == 1

    def test_fresh_telemetry_and_dataset_manifest(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The steady fast fresh write emits the SAME telemetry + record_dataset."""
        _steady_on(monkeypatch)
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            _wf(writer, _frame())
        writes = [e for e in logs if e["event"] == "lake.partition.write"]
        assert len(writes) == 1
        assert writes[0]["rows"] == 4
        assert writes[0]["partition_rows"] == 4


# ────────────────────────────────────────────────────────────────────────
# Merge lane (the no-op probe + its degrades)
# ────────────────────────────────────────────────────────────────────────


class TestMergeLane:
    def _seed(self, writer: ParquetWriter, frame: pl.DataFrame | None = None) -> None:
        """Seed the partition through the LEGACY path (flag irrelevant)."""
        _legacy_write(writer, frame if frame is not None else _frame(with_oi=True))

    def test_steady_noop_file_untouched_and_result_equals_legacy(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer)
        before = _dest(lake).read_bytes()
        mtime_before = _dest(lake).stat().st_mtime_ns
        _steady_on(monkeypatch)
        spy = _LegacySpy(monkeypatch)
        stats = FrameWriteStats()
        fast = _wf(writer, _frame(with_oi=True), stats=stats)
        assert spy.calls == 0
        assert stats.fast == 1
        assert fast.file_rewritten is False
        assert fast.bars_added == 0
        assert fast.conflicts == ()
        assert fast.row_count == 4
        assert fast.raw_hash == hashlib.sha256(before).hexdigest()
        assert fast.bytes_written == len(before)
        assert fast.first_ts == _T0
        assert fast.last_ts == _T0 + timedelta(minutes=15)
        assert _dest(lake).read_bytes() == before
        assert _dest(lake).stat().st_mtime_ns == mtime_before  # NO render, NO fsync
        # Field parity vs the legacy no-op verdict on a twin lake.
        twin_root = lake.parent / "twin"
        twin = ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        _legacy_write(twin, _frame(with_oi=True))
        legacy = _legacy_write(twin, _frame(with_oi=True))
        for field in (
            "row_count",
            "raw_hash",
            "first_ts",
            "last_ts",
            "bytes_written",
            "bars_added",
            "conflicts",
            "file_rewritten",
        ):
            assert getattr(fast, field) == getattr(legacy, field), field

    def test_noop_with_stored_extra_rows(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stored ⊇ incoming with equal values ⇒ no-op; stored-only rows KEPT."""
        self._seed(writer, _frame(6, with_oi=True))  # 6 stored rows
        before = _dest(lake).read_bytes()
        _steady_on(monkeypatch)
        fast = _wf(writer, _frame(4, with_oi=True))  # the first 4 only
        assert fast.file_rewritten is False
        assert fast.row_count == 6  # the STORED unique height (union-never-delete)
        assert fast.last_ts == _T0 + timedelta(minutes=25)  # stored bounds
        assert _dest(lake).read_bytes() == before

    def test_merge_delta_new_rows_degrades_and_unions(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer, _frame(3, with_oi=True))
        _steady_on(monkeypatch)
        spy = _LegacySpy(monkeypatch)
        stats = FrameWriteStats()
        result = _wf(writer, _frame(5, with_oi=True), stats=stats)
        assert spy.calls == 1
        assert stats.degrade_reasons == {"merge_delta": 1}
        assert result.bars_added == 2
        assert result.row_count == 5

    def test_merge_delta_restated_value_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer, _frame(3, with_oi=True))
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        changed = _frame(3, with_oi=True).with_columns(
            pl.Series("close", [999.0, 103.0, 104.0], dtype=pl.Float64),
            pl.Series("high", [1_000.0, 111.0, 112.0], dtype=pl.Float64),
        )
        result = _wf(writer, changed, stats=stats)
        assert stats.degrade_reasons == {"merge_delta": 1}
        assert result.file_rewritten is True  # the legacy latest-wins restate
        stored = pl.read_parquet(_dest(lake))
        assert stored.get_column("close").to_list()[0] == 999.0

    def test_merge_delta_oi_null_vs_zero_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """eq_missing law: stored null OI ≠ incoming 0 — a genuine restate."""
        # Seed with absent_open_interest=None so the stored file holds NULL OI.
        writer.write(
            **_COORDS,
            bars=frame_to_candle_bars(_frame(2, oi=[None, None]), absent_open_interest=None),
            merge_mode="latest-wins",
        )
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        # The Tier-1 caller maps null→0, so incoming OI is 0 ≠ stored null.
        _wf(writer, _frame(2, oi=[None, None]), stats=stats)
        assert stats.degrade_reasons == {"merge_delta": 1}
        stored = pl.read_parquet(_dest(lake))
        assert stored.get_column("open_interest").to_list() == [0, 0]

    def test_noop_with_null_oi_both_sides(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """eq_missing law: null == null compares EQUAL (never the null trap)."""
        writer.write(
            **_COORDS,
            bars=frame_to_candle_bars(_frame(2, oi=[None, 7]), absent_open_interest=None),
            merge_mode="latest-wins",
        )
        before = _dest(lake).read_bytes()
        _steady_on(monkeypatch)
        fast = _wf(writer, _frame(2, oi=[None, 7]), absent_open_interest=None)
        assert fast.file_rewritten is False
        assert _dest(lake).read_bytes() == before

    def test_merge_overlay_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-enriched partition degrades — legacy's overlay strip runs."""
        self._seed(writer)
        # Hand-plant an overlay column (writer-rendered files never mix shapes
        # like this — the exotic class the probe must refuse).
        stored = pl.read_parquet(_dest(lake)).with_columns(pl.lit(0.5).alias("iv"))
        stored.write_parquet(_dest(lake))
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        _wf(writer, _frame(with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_overlay": 1}

    def test_merge_stored_schema_extra_column_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stray stored column degrades; LEGACY owns the shape (today: it
        ignores the stray on its no-op — the file stays byte-untouched)."""
        self._seed(writer)
        stored = pl.read_parquet(_dest(lake)).with_columns(pl.lit(1).alias("stray"))
        stored.write_parquet(_dest(lake))
        before = _dest(lake).read_bytes()
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        result = _wf(writer, _frame(with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_stored_schema": 1}
        assert result.file_rewritten is False  # legacy's own no-op verdict
        assert _dest(lake).read_bytes() == before

    def test_merge_stored_null_price_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer, _frame(2, with_oi=True))
        stored = pl.read_parquet(_dest(lake)).with_columns(
            pl.Series("open", [None, 101.0], dtype=pl.Float64)
        )
        stored.write_parquet(_dest(lake))
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        with pytest.raises(Exception):
            _wf(writer, _frame(2, with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_stored_schema": 1}

    def test_merge_stored_naive_timestamp_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-built stored file with a NAIVE ts dtype degrades (schema arm)."""
        self._seed(writer, _frame(2, with_oi=True))
        stored = pl.read_parquet(_dest(lake)).with_columns(
            pl.col("timestamp").dt.replace_time_zone(None)
        )
        stored.write_parquet(_dest(lake))
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        with pytest.raises(Exception):
            _wf(writer, _frame(2, with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_stored_schema": 1}

    def test_merge_stored_float_volume_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer, _frame(2, with_oi=True))
        stored = pl.read_parquet(_dest(lake)).with_columns(pl.col("volume").cast(pl.Float64))
        stored.write_parquet(_dest(lake))
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        _wf(writer, _frame(2, with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_stored_schema": 1}

    def test_merge_stored_float_oi_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer, _frame(2, with_oi=True))
        stored = pl.read_parquet(_dest(lake)).with_columns(pl.col("open_interest").cast(pl.Float64))
        stored.write_parquet(_dest(lake))
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        _wf(writer, _frame(2, with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_stored_schema": 1}

    def test_merge_stored_dups_degrades_and_heals_like_legacy(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dup-poisoned stored partition degrades; legacy's dedup self-heal runs."""
        self._seed(writer, _frame(3, with_oi=True))
        stored = pl.read_parquet(_dest(lake))
        pl.concat([stored, stored.slice(0, 1)]).sort("timestamp").write_parquet(_dest(lake))
        _steady_on(monkeypatch)
        stats = FrameWriteStats()
        result = _wf(writer, _frame(3, with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_stored_dups": 1}
        assert result.file_rewritten is True  # the legacy dedup self-heal rewrote
        healed = pl.read_parquet(_dest(lake))
        assert healed.height == 3

    def test_merge_stored_snap_overflow_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer)
        _steady_on(monkeypatch)
        monkeypatch.setattr(
            ParquetWriter, "_decode_and_snap_frame", staticmethod(lambda dest, b: None)
        )
        stats = FrameWriteStats()
        _wf(writer, _frame(with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_stored_snap": 1}

    def test_merge_footer_probe_failure_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer)
        _steady_on(monkeypatch)
        monkeypatch.setattr(
            ParquetWriter, "_stored_physical_rows", staticmethod(lambda dest, b: None)
        )
        stats = FrameWriteStats()
        _wf(writer, _frame(with_oi=True), stats=stats)
        assert stats.degrade_reasons == {"merge_stored_dups": 1}

    def test_merge_burn_in_noop_consumes_sample(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(writer)
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "1")
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=3)
        spy = _LegacySpy(monkeypatch)
        result = _wf(writer, _frame(with_oi=True))
        assert spy.calls == 1  # the legacy cross-check ran (and no-opped)
        assert result.file_rewritten is False
        assert gate._MERGE_REMAINING[0] == 2
        assert not gate.autoreverted()

    def test_merge_burn_in_mismatch_autoreverts(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fast no-op verdict the legacy lane contradicts latches the process."""
        self._seed(writer)
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "1")
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=3)
        # Plant the fault INSIDE the fast probe: force a bogus no-op verdict
        # for an incoming frame that actually restates a value.
        original_probe = ParquetWriter._merge_noop_probe

        def lying_probe(
            self: ParquetWriter, dest: Path, snapped: pl.DataFrame, **kwargs: Any
        ) -> WriteResult | DegradeReason:
            outcome = original_probe(self, dest, snapped, **kwargs)
            if isinstance(outcome, DegradeReason):
                existing = self._read_existing_bytes(dest)
                return WriteResult(
                    path=dest,
                    row_count=4,
                    raw_hash=hashlib.sha256(existing).hexdigest(),
                    first_ts=_T0,
                    last_ts=_T0 + timedelta(minutes=15),
                    bytes_written=len(existing),
                    bars_added=0,
                    conflicts=(),
                    file_rewritten=False,
                )
            return outcome

        monkeypatch.setattr(ParquetWriter, "_merge_noop_probe", lying_probe)
        changed = _frame(with_oi=True).with_columns(
            pl.Series("close", [900.0, 103.0, 104.0, 105.0], dtype=pl.Float64),
            pl.Series("high", [1_000.0, 111.0, 112.0, 113.0], dtype=pl.Float64),
        )
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            result = _wf(writer, changed)
        assert gate.autoreverted()
        assert any(e["event"] == "prebuild.frame_write.autoreverted" for e in logs)
        # The LEGACY result shipped: the restate genuinely rewrote the file.
        assert result.file_rewritten is True
        stored = pl.read_parquet(_dest(lake))
        assert stored.get_column("close").to_list()[0] == 900.0


# ────────────────────────────────────────────────────────────────────────
# Shadow mode
# ────────────────────────────────────────────────────────────────────────


class TestShadowMode:
    def test_shadow_without_stats_still_ships_legacy(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stats-less shadow write (direct writer callers) counts nothing, ships legacy."""
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "shadow")
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)
        original = ParquetWriter._fresh_render_frame

        def corrupted(snapped: pl.DataFrame, *, absent_open_interest: int | None) -> pl.DataFrame:
            return original(snapped, absent_open_interest=99)

        monkeypatch.setattr(ParquetWriter, "_fresh_render_frame", staticmethod(corrupted))
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            result = _wf(writer, _frame())  # stats=None
        assert result.file_rewritten is True
        assert any(e["event"] == "prebuild.frame_write.shadow_mismatch" for e in logs)
        assert not gate.autoreverted()

    def test_shadow_ships_legacy_and_counts(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "shadow")
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)
        spy = _LegacySpy(monkeypatch)
        stats = FrameWriteStats()
        _wf(writer, _frame(), stats=stats)
        assert spy.calls == 1  # legacy SHIPPED
        assert stats.shadow_checked == 1
        assert stats.shadow_mismatches == 0
        assert stats.fast == 0  # shadow counts under shadow_checked only
        twin_root = lake.parent / "twin"
        twin = ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        _legacy_write(twin, _frame())
        assert _dest(lake).read_bytes() == _dest(twin_root).read_bytes()

    def test_shadow_mismatch_counts_and_errors_without_latching(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "shadow")
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)
        original = ParquetWriter._fresh_render_frame

        def corrupted(snapped: pl.DataFrame, *, absent_open_interest: int | None) -> pl.DataFrame:
            return original(snapped, absent_open_interest=99)

        monkeypatch.setattr(ParquetWriter, "_fresh_render_frame", staticmethod(corrupted))
        from structlog.testing import capture_logs

        stats = FrameWriteStats()
        with capture_logs() as logs:
            _wf(writer, _frame(), stats=stats)
            # A DIFFERENT month partition — the second write must be a fresh
            # admission too (same-month would be a merge_delta degrade).
            _wf(writer, _frame(base=300.0, start=_T0 + timedelta(days=40)), stats=stats)
        errors = [e for e in logs if e["event"] == "prebuild.frame_write.shadow_mismatch"]
        assert len(errors) == 2  # never throttled
        assert stats.shadow_checked == 2
        assert stats.shadow_mismatches == 2
        assert not gate.autoreverted()  # shadow NEVER latches


# ────────────────────────────────────────────────────────────────────────
# The G9 belt + the never-swallowed classes
# ────────────────────────────────────────────────────────────────────────


class TestG9Belt:
    def test_unexpected_fast_lane_error_degrades(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._plant_probe_raise(monkeypatch, ValueError("planted"))
        _steady_on(monkeypatch)
        _legacy_write(writer, _frame(with_oi=True))
        spy = _LegacySpy(monkeypatch)
        stats = FrameWriteStats()
        result = _wf(writer, _frame(with_oi=True), stats=stats)
        assert result.file_rewritten is False  # the legacy no-op served
        assert spy.calls == 1
        assert stats.degrade_reasons == {"fast_lane_error": 1}

    @pytest.mark.parametrize(
        "poison", [LakeWriteHalt("disk_full"), MemoryError("oom")], ids=["halt", "oom"]
    )
    def test_halt_and_oom_never_swallowed(
        self,
        writer: ParquetWriter,
        monkeypatch: pytest.MonkeyPatch,
        poison: BaseException,
    ) -> None:
        """LakeWriteHalt / MemoryError propagate RAW out of the belt (V3 C7)."""
        self._plant_probe_raise(monkeypatch, poison)
        _steady_on(monkeypatch)
        _legacy_write(writer, _frame(with_oi=True))
        with pytest.raises(type(poison)):
            _wf(writer, _frame(with_oi=True))

    @staticmethod
    def _plant_probe_raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
        def raising(
            self: ParquetWriter, dest: Path, snapped: pl.DataFrame, **kwargs: Any
        ) -> WriteResult | DegradeReason:
            raise exc

        monkeypatch.setattr(ParquetWriter, "_merge_noop_probe", raising)


# ────────────────────────────────────────────────────────────────────────
# The render schema assert (Seam D)
# ────────────────────────────────────────────────────────────────────────


class TestRenderFrameBytesAssert:
    def test_wrong_columns_refused(self) -> None:
        df = pl.DataFrame({"timestamp": [], "open": []})
        with pytest.raises(WriterError, match="render schema violation"):
            ParquetWriter._render_frame_bytes(df)

    def test_wrong_dtype_refused(self) -> None:
        df = ParquetWriter._frame_from_bars(
            [
                CandleBar(
                    timestamp=_T0,
                    open=Decimal("1.00"),
                    high=Decimal("2.00"),
                    low=Decimal("0.50"),
                    close=Decimal("1.50"),
                    volume=1,
                    open_interest=0,
                )
            ]
        ).with_columns(pl.col("open").cast(pl.Float32))
        with pytest.raises(WriterError, match="dtype"):
            ParquetWriter._render_frame_bytes(df)

    def test_multi_chunk_frame_renders_like_single_chunk(self) -> None:
        """The G-C rechunk pin at the encoder level: chunk layout never leaks."""
        bars = [
            CandleBar(
                timestamp=_T0 + timedelta(minutes=i),
                open=Decimal("100.05"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.55"),
                volume=i,
                open_interest=i,
            )
            for i in range(6)
        ]
        single = ParquetWriter._frame_from_bars(bars)
        multi = pl.concat(
            [
                ParquetWriter._frame_from_bars(bars[:2]),
                ParquetWriter._frame_from_bars(bars[2:]),
            ],
            rechunk=False,
        )
        assert multi.n_chunks() > 1
        assert ParquetWriter._render_frame_bytes(multi) == ParquetWriter._render_frame_bytes(single)


# ────────────────────────────────────────────────────────────────────────
# Sink sibling: append_frame
# ────────────────────────────────────────────────────────────────────────


class _RecordingCatalog:
    def __init__(self) -> None:
        self.entries: list[CatalogEntry] = []

    def upsert(self, entry: CatalogEntry) -> None:
        self.entries.append(entry)


class TestAppendFrame:
    def test_empty_frame_early_return(self, writer: ParquetWriter) -> None:
        """The C13 law — byte-identical to append([])."""
        sink = ParquetBarSink(writer=writer)
        result = sink.append_frame(**_COORDS, frame=_frame(0))
        assert result.bars_appended == 0
        assert result.partitions_written == 0
        assert result.write_results == ()

    def test_bars_appended_is_input_count(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        result = sink.append_frame(**_COORDS, frame=_frame(4))
        assert result.bars_appended == 4
        assert result.bars_net_new == 4
        assert result.partitions_written == 1

    def test_multi_month_grouping_ascending(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A month-straddling frame becomes one write per month, ascending."""
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        straddle = _frame(4, start=datetime(2024, 3, 31, 23, 50, tzinfo=UTC), step_minutes=5)
        result = sink.append_frame(**_COORDS, frame=straddle)
        assert result.partitions_written == 2
        months = [wr.path.name for wr in result.write_results]
        assert months == ["03.parquet", "04.parquet"]  # ascending — the sink law
        assert result.bars_appended == 4
        assert result.bars_net_new == 4

    def test_ts_dtype_pre_check_degrades_whole_frame(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An untrustworthy ts column routes the WHOLE call through legacy append."""
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        naive = _frame().with_columns(pl.col("ts_utc").dt.replace_time_zone(None))
        stats = FrameWriteStats()
        # frame_to_candle_bars on a NAIVE ts builds bars CandleBar refuses —
        # the exact flag-OFF failure (same class, same site).
        with pytest.raises(ValueError, match="timezone-aware"):
            sink.append_frame(**_COORDS, frame=naive, stats=stats)
        assert stats.degrade_reasons == {"schema_mismatch": 1}

    def test_ts_null_pre_check_degrades_whole_frame(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        bad = _frame(3).with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(None)
            .otherwise(pl.col("ts_utc"))
            .alias("ts_utc")
        )
        stats = FrameWriteStats()
        # frame_to_candle_bars hands CandleBar a None timestamp — TODAY's exact
        # legacy failure class (AttributeError from the tz check), reproduced.
        with pytest.raises(AttributeError):
            sink.append_frame(**_COORDS, frame=bad, stats=stats)
        assert stats.degrade_reasons == {"schema_mismatch": 1}

    def test_catalog_arm_parity(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The catalog upsert arm mirrors append() (parity for future callers)."""
        _steady_on(monkeypatch)
        catalog = _RecordingCatalog()
        sink = ParquetBarSink(writer=writer, catalog=catalog)  # type: ignore[arg-type]
        sink.append_frame(**_COORDS, frame=_frame(4))
        assert len(catalog.entries) == 1
        assert catalog.entries[0].row_count == 4

    def test_append_frame_bytes_equal_legacy_append(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sink-level parity: append_frame ≡ append(frame_to_candle_bars(...))."""
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        sink.append_frame(**_COORDS, frame=_frame(with_oi=True))
        twin_root = lake.parent / "twin"
        twin_sink = ParquetBarSink(
            writer=ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        )
        twin_sink.append(
            **_COORDS,
            bars=frame_to_candle_bars(_frame(with_oi=True), absent_open_interest=0),
            merge_mode="latest-wins",
        )
        assert _dest(lake).read_bytes() == _dest(twin_root).read_bytes()


# ────────────────────────────────────────────────────────────────────────
# Seam E split parity
# ────────────────────────────────────────────────────────────────────────


class TestSeamESplit:
    def test_decode_partition_frame_corrupt_raises_quarantinable(self, tmp_path: Path) -> None:
        bad = tmp_path / "junk.parquet"
        bad.write_bytes(b"not parquet at all")
        with pytest.raises(WriterError, match="corrupt existing partition"):
            ParquetWriter._decode_partition_frame(bad, bad.read_bytes())

    def test_decode_and_snap_returns_snapped_decimals(
        self, writer: ParquetWriter, lake: Path
    ) -> None:
        _legacy_write(writer, _frame(2))
        raw = _dest(lake).read_bytes()
        snapped = ParquetWriter._decode_and_snap_frame(_dest(lake), raw)
        assert snapped is not None
        assert snapped.schema["open"] == pl.Decimal(precision=38, scale=2)
        assert snapped.schema["timestamp"] == pl.Datetime(time_unit="us", time_zone="UTC")

    def test_snap_frame_prices_overflow_returns_none(self) -> None:
        """The REAL G5 except arm: a |x| ≳ 1e19 frame overflows the snap select.

        Production-unreachable BEHIND G4 (the 1e12 bound) — pinned by a
        direct call on the private, the honest way to keep the branch alive.
        """
        big = _frame(1).with_columns(
            *(pl.lit(1e20).alias(c) for c in ("open", "high", "low", "close"))
        )
        assert ParquetWriter._snap_frame_prices(big) is None

    def test_snap_frame_prices_healthy_returns_decimals(self) -> None:
        snapped = ParquetWriter._snap_frame_prices(_frame(2))
        assert snapped is not None
        assert snapped.schema["open"] == pl.Decimal(precision=38, scale=2)

    def test_decode_and_snap_overflow_returns_none(self, tmp_path: Path) -> None:
        """A stored |x| ≳ 1e19 overflows the snap → None (the per-row trigger)."""
        df = pl.DataFrame(
            {
                "timestamp": [_T0],
                "open": [1e20],
                "high": [1e20],
                "low": [1e20],
                "close": [1e20],
                "volume": [1],
                "open_interest": [0],
            },
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "open_interest": pl.Int64,
            },
        )
        dest = tmp_path / "big.parquet"
        df.write_parquet(dest)
        assert ParquetWriter._decode_and_snap_frame(dest, dest.read_bytes()) is None
