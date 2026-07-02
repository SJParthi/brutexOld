"""Writer-path tests for the Greeks overlay (DEC-GREEKS-ENGINE-PR2B-001, plan §15.3).

The "single biggest correctness trap" coverage: the 17-column render branch,
the schema-tolerant read-back (legacy 7-column partitions NEVER ``KeyError``
and round-trip byte-identically), the overlay merge semantics on BOTH merge
modes (a Greek difference never trips keep-first; ``latest-wins`` heals), the
type-preserving 2dp snap, and the corrupt-value quarantine.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.errors import WriterError
from brutex.data.models import (
    GREEKS_OVERLAY_FIELDS,
    CandleBar,
    FnoGreeksBar,
)
from brutex.data.parquet_writer import ParquetWriter, _overlay_equal, _tick_round

pl = pytest.importorskip("polars", reason="ParquetWriter needs polars")

_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)
_SYMBOL = "NSE-NIFTY-25Jan24-21500-CE"


def _plain(ts: datetime = _TS, close: str = "200") -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(close),
        high=Decimal(close) + 5,
        low=Decimal(close) - 5,
        close=Decimal(close),
        volume=10,
        open_interest=100,
    )


def _enriched(
    ts: datetime = _TS,
    close: str = "200",
    *,
    iv: float | None = 0.125,
    pid: int | None = 1_050_000,
) -> FnoGreeksBar:
    return FnoGreeksBar(
        timestamp=ts,
        open=Decimal(close),
        high=Decimal(close) + 5,
        low=Decimal(close) - 5,
        close=Decimal(close),
        volume=10,
        open_interest=100,
        iv=iv,
        delta=None if iv is None else 0.53,
        gamma=None if iv is None else 0.00087,
        theta=None if iv is None else -8.68,
        vega=None if iv is None else 14.31,
        rho=None if iv is None else 3.2,
        spot_at_bar=None if iv is None else Decimal("21500.00"),
        t_years_used=None if iv is None else 0.0281,
        rate_used=None if iv is None else 0.065,
        greeks_provenance_id=pid,
    )


def _writer(tmp_path: Path) -> ParquetWriter:
    return ParquetWriter(lake_root=tmp_path)


def _write(
    writer: ParquetWriter, bars: list[CandleBar], *, merge_mode: str = "keep-first"
) -> object:
    return writer.write(
        exchange="NSE",
        segment="FNO",
        symbol=_SYMBOL,
        interval="1minute",
        bars=bars,
        merge_mode=merge_mode,
    )


def _partition(tmp_path: Path) -> Path:
    return tmp_path / "bars" / "NSE" / "FNO" / _SYMBOL / "1minute" / "2024" / "01.parquet"


class TestTickRoundPreservesType:
    def test_plain_bar_stays_plain(self) -> None:
        out = _tick_round(_plain(close="200.005"))
        assert type(out) is CandleBar
        assert out.close == Decimal("200.01")

    def test_enriched_bar_stays_enriched_and_snaps_spot(self) -> None:
        bar = FnoGreeksBar(
            timestamp=_TS,
            open=Decimal("200.004"),
            high=Decimal("205"),
            low=Decimal("195"),
            close=Decimal("200.004"),
            volume=1,
            open_interest=None,
            iv=0.125,
            spot_at_bar=Decimal("21500.005"),
            greeks_provenance_id=1,
        )
        out = _tick_round(bar)
        assert type(out) is FnoGreeksBar
        assert out.close == Decimal("200.00")
        assert out.spot_at_bar == Decimal("21500.01")
        assert out.iv == 0.125  # FULL precision — never snapped

    def test_enriched_none_spot_passes_through(self) -> None:
        out = _tick_round(_enriched(iv=None, pid=7))
        assert isinstance(out, FnoGreeksBar)
        assert out.spot_at_bar is None and out.greeks_provenance_id == 7


class TestOverlayEqual:
    def test_two_plain_bars_always_overlay_equal(self) -> None:
        assert _overlay_equal(_plain(), _plain(close="999"))

    def test_plain_vs_enriched_differ(self) -> None:
        assert not _overlay_equal(_plain(), _enriched())

    def test_identical_enriched_equal(self) -> None:
        assert _overlay_equal(_enriched(), _enriched())

    def test_differing_greek_detected(self) -> None:
        assert not _overlay_equal(_enriched(iv=0.125), _enriched(iv=0.126))


class TestRenderAndReadBack:
    def test_plain_batch_renders_seven_columns(self, tmp_path: Path) -> None:
        _write(_writer(tmp_path), [_plain()])
        df = pl.read_parquet(_partition(tmp_path))
        assert df.columns == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_enriched_batch_renders_seventeen_columns(self, tmp_path: Path) -> None:
        _write(_writer(tmp_path), [_enriched()], merge_mode="latest-wins")
        df = pl.read_parquet(_partition(tmp_path))
        assert df.columns == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
            *GREEKS_OVERLAY_FIELDS,
        ]
        row = next(df.iter_rows(named=True))
        assert row["iv"] == 0.125
        assert row["spot_at_bar"] == 21500.0
        assert row["greeks_provenance_id"] == 1_050_000

    def test_enriched_merge_roundtrip_is_byte_identical(self, tmp_path: Path) -> None:
        """Re-writing the SAME enriched bars is a no-op (Golden Rule #7)."""
        writer = _writer(tmp_path)
        _write(writer, [_enriched()], merge_mode="latest-wins")
        h1 = hashlib.sha256(_partition(tmp_path).read_bytes()).hexdigest()
        result = _write(writer, [_enriched()], merge_mode="latest-wins")
        h2 = hashlib.sha256(_partition(tmp_path).read_bytes()).hexdigest()
        assert h1 == h2
        assert result.bars_added == 0  # type: ignore[attr-defined]

    def test_legacy_partition_merge_roundtrip_untouched(self, tmp_path: Path) -> None:
        """A 7-column partition merged with identical plain bars stays byte-identical."""
        writer = _writer(tmp_path)
        _write(writer, [_plain()])
        h1 = hashlib.sha256(_partition(tmp_path).read_bytes()).hexdigest()
        _write(writer, [_plain()])
        assert hashlib.sha256(_partition(tmp_path).read_bytes()).hexdigest() == h1
        assert pl.read_parquet(_partition(tmp_path)).width == 7

    def test_partial_overlay_columns_read_tolerantly(self, tmp_path: Path) -> None:
        """A partition with only SOME overlay columns reads the rest as None."""
        dest = _partition(tmp_path)
        dest.parent.mkdir(parents=True)
        pl.DataFrame(
            {
                "timestamp": [_TS],
                "open": [200.0],
                "high": [205.0],
                "low": [195.0],
                "close": [200.0],
                "volume": [10],
                "open_interest": [100],
                "iv": [0.125],  # ONLY one overlay column present
            },
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "open_interest": pl.Int64,
                "iv": pl.Float64,
            },
        ).write_parquet(dest)
        bars = ParquetWriter._read_existing_bars(dest, dest.read_bytes())
        bar = bars[_TS]
        assert isinstance(bar, FnoGreeksBar)
        assert bar.iv == 0.125
        assert bar.delta is None and bar.greeks_provenance_id is None

    def test_nan_greek_in_stored_partition_quarantines(self, tmp_path: Path) -> None:
        dest = _partition(tmp_path)
        dest.parent.mkdir(parents=True)
        pl.DataFrame(
            {
                "timestamp": [_TS],
                "open": [200.0],
                "high": [205.0],
                "low": [195.0],
                "close": [200.0],
                "volume": [10],
                "open_interest": [100],
                "iv": [float("nan")],
            },
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "open_interest": pl.Int64,
                "iv": pl.Float64,
            },
        ).write_parquet(dest)
        with pytest.raises(WriterError, match="corrupt existing partition"):
            ParquetWriter._read_existing_bars(dest, dest.read_bytes())


class TestMergeOverlaySemantics:
    def test_keep_first_plain_repull_never_strips_greeks(self, tmp_path: Path) -> None:
        """A vendor re-pull (plain bars, same OHLCV) keeps the stored overlay — no conflict."""
        writer = _writer(tmp_path)
        _write(writer, [_enriched()], merge_mode="latest-wins")
        h1 = hashlib.sha256(_partition(tmp_path).read_bytes()).hexdigest()
        result = _write(writer, [_plain()])  # keep-first vendor identity
        assert result.conflicts == ()  # type: ignore[attr-defined]
        assert result.bars_added == 0  # type: ignore[attr-defined]
        assert hashlib.sha256(_partition(tmp_path).read_bytes()).hexdigest() == h1
        df = pl.read_parquet(_partition(tmp_path))
        assert df["iv"].to_list() == [0.125]

    def test_keep_first_ohlcv_conflict_still_conflicts(self, tmp_path: Path) -> None:
        writer = _writer(tmp_path)
        _write(writer, [_enriched()], merge_mode="latest-wins")
        result = _write(writer, [_plain(close="777")])
        assert len(result.conflicts) == 1  # type: ignore[attr-defined]
        # Stored (enriched) bar wins — keep-first.
        assert pl.read_parquet(_partition(tmp_path))["close"].to_list() == [200.0]

    def test_latest_wins_overlay_update_rewrites(self, tmp_path: Path) -> None:
        writer = _writer(tmp_path)
        _write(writer, [_plain()])  # a raw vendor partition
        _write(writer, [_enriched()], merge_mode="latest-wins")  # the enrich pass
        df = pl.read_parquet(_partition(tmp_path))
        assert df["iv"].to_list() == [0.125]
        # A corrected overlay (e.g. a later complete-session spot) overwrites.
        corrected = _enriched(iv=0.2, pid=1_050_001)
        _write(writer, [corrected], merge_mode="latest-wins")
        df2 = pl.read_parquet(_partition(tmp_path))
        assert df2["iv"].to_list() == [0.2]
        assert df2["greeks_provenance_id"].to_list() == [1_050_001]

    def test_new_plain_bar_merges_into_enriched_partition(self, tmp_path: Path) -> None:
        """A later vendor pull adding a NEW minute renders NULL overlay for it."""
        writer = _writer(tmp_path)
        _write(writer, [_enriched()], merge_mode="latest-wins")
        later = _plain(_TS + timedelta(minutes=1), close="201")
        result = _write(writer, [later])
        assert result.bars_added == 1  # type: ignore[attr-defined]
        df = pl.read_parquet(_partition(tmp_path)).sort("timestamp")
        assert df["iv"].to_list() == [0.125, None]
        assert df["greeks_provenance_id"].to_list() == [1_050_000, None]

    def test_per_row_fallback_preserves_overlay(self, tmp_path: Path) -> None:
        """The overflow fallback path reconstructs the overlay too."""
        dest = _partition(tmp_path)
        dest.parent.mkdir(parents=True)
        _write(_writer(tmp_path), [_enriched()], merge_mode="latest-wins")
        df = pl.read_parquet(dest)
        bars = ParquetWriter._reconstruct_bars_per_row(dest, df)
        bar = bars[_TS]
        assert isinstance(bar, FnoGreeksBar)
        assert bar.iv == 0.125
        assert bar.spot_at_bar == Decimal("21500.00")
        assert bar.greeks_provenance_id == 1_050_000

    def test_per_row_fallback_plain_partition_unchanged(self, tmp_path: Path) -> None:
        dest = _partition(tmp_path)
        _write(_writer(tmp_path), [_plain()])
        df = pl.read_parquet(dest)
        bars = ParquetWriter._reconstruct_bars_per_row(dest, df)
        assert type(bars[_TS]) is CandleBar
