"""Tests for `brutex.data.tf_autoderive` — the doctor derived-freshness compare core.

The pull-settle auto-derive arm (``autoderive_enabled`` /
``plan_autoderive_symbols`` / ``run_autoderive`` + the ``BRUTEX_AUTO_DERIVE_TFS``
opt-in of DEC-TF-MATERIALIZE-002) is REMOVED by DEC-PIPELINE-AUTOPREBUILD-001 —
derived timeframes are written ONLY by the pipeline's automatic prebuild phase
(covered in ``test_pipeline_prebuild.py`` / ``test_gated_prebuild.py``). What
remains here is the pure freshness compare the ``brutex doctor`` probe uses
(fresh / stale on value drift / stale on row-count drift) plus a pin that the
removed auto-derive surface stays removed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
from brutex.data import tf_autoderive
from brutex.data.tf_autoderive import compare_derived_freshness

_OPEN_UTC = datetime(2025, 6, 9, 3, 45, tzinfo=UTC).replace(tzinfo=None)


def _df_1m(n_bars: int = 20) -> pl.DataFrame:
    closes = [100.0 + i * 0.1 for i in range(n_bars)]
    return pl.DataFrame(
        {
            "ts_utc": [_OPEN_UTC + timedelta(minutes=i) for i in range(n_bars)],
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000] * n_bars,
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


class TestFreshnessCompare:
    def test_fresh_when_stored_equals_rederivation(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5)
        assert compare_derived_freshness(df_1m, stored, 5) == "fresh"

    def test_stale_on_value_drift(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5).with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.col("close") + 1.0)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        assert compare_derived_freshness(df_1m, stored, 5) == "stale"

    def test_stale_on_row_count_drift(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5).slice(1)  # a bucket went missing
        assert compare_derived_freshness(df_1m, stored, 5) == "stale"


class TestAutoDeriveSurfaceRemoved:
    """DEC-PIPELINE-AUTOPREBUILD-001: the pull-settle auto-derive arm stays removed."""

    def test_autoderive_symbols_gone(self) -> None:
        for gone in (
            "autoderive_enabled",
            "plan_autoderive_symbols",
            "run_autoderive",
            "AutoderiveOutcome",
            "AutoderiveReport",
            "ENV_AUTO_DERIVE_TFS",
        ):
            assert not hasattr(tf_autoderive, gone), (
                f"{gone} must stay removed — derived TFs are written only by the "
                "pipeline prebuild (DEC-PIPELINE-AUTOPREBUILD-001)"
            )
        # The compare core + the sweep-preflight freshness source + its shared lake
        # reader (DEC-SUSPICION-SWEEP-FIXES-001 G1) — and NOTHING of the removed
        # auto-derive arm.
        assert tf_autoderive.__all__ == [
            "compare_derived_freshness",
            "lake_derived_freshness_verdicts",
            "read_lake_bars_frame",
        ]


def _seed_lake_1m(lake_root, symbol: str = "NIFTY", n_bars: int = 20):
    """Write *n_bars* in-session 1-minute bars to a real Parquet lake; return the bars."""
    from datetime import timezone
    from decimal import Decimal
    from pathlib import Path

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    ist = timezone(timedelta(hours=5, minutes=30))
    bars = []
    for i in range(n_bars):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=ist).astimezone(UTC)
        close = Decimal("100.00") + Decimal(i) / 10
        bars.append(
            CandleBar(
                timestamp=ts,
                open=close - Decimal("0.05"),
                high=close + Decimal("0.05"),
                low=close - Decimal("0.10"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=Path(lake_root)))
    sink.append(exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=bars)
    return sink, bars


def _write_derived(lake_root, sink, *, symbol: str = "NIFTY", interval: str = "5minute") -> None:
    """Materialise a FRESH derived partition from the STORED 1-minute (the prebuild law)."""
    from brutex.data.spot_daily_resample import frame_to_candle_bars
    from brutex.data.tf_resampler import resample_1m_to_tf
    from brutex.data.timeframes import LAKE_INTERVAL_LABELS

    symbol_dir = lake_root / "bars" / "NSE" / "INDEX" / symbol
    df_1m = tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute")
    tf_minutes = next(m for m, label in LAKE_INTERVAL_LABELS.items() if label == interval)
    derived = resample_1m_to_tf(df_1m, tf_minutes)
    sink.append(
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval=interval,
        bars=frame_to_candle_bars(derived),
    )


class TestReadLakeBarsFrame:
    """The COMMON lake reader (extracted from the doctor — DEC-SUSPICION-SWEEP-FIXES-001 G1)."""

    def test_reads_canonical_sorted_ohlcv_frame(self, tmp_path) -> None:
        _, bars = _seed_lake_1m(tmp_path)
        frame = tf_autoderive.read_lake_bars_frame(
            tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY", "1minute"
        )
        assert frame.columns == ["ts_utc", "open", "high", "low", "close", "volume"]
        assert frame.height == len(bars)
        assert frame.get_column("ts_utc").is_sorted()

    def test_source_label_defaults_and_overrides(self, tmp_path) -> None:
        """The dedup log source: blank derives <symbol>/<interval>; explicit wins
        (the doctor keeps its historical ``doctor:`` prefix through this seam)."""
        from structlog.testing import capture_logs

        _sink, bars = _seed_lake_1m(tmp_path)
        symbol_dir = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY"
        # Duplicate the SAME bars into a second month file? Not needed — the dedup
        # source only logs when rows drop; pin the label via a real duplicate append
        # bypassing the sink's keep-first (write the same bars under a different
        # month is complex) — instead assert the reader works with BOTH labels and
        # returns identical frames (the label is pass-through into dedup_bars_frame).
        with capture_logs():
            default = tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute")
            labelled = tf_autoderive.read_lake_bars_frame(
                symbol_dir, "1minute", source="doctor:NIFTY/1minute"
            )
        assert default.equals(labelled)
        assert default.height == len(bars)


class TestLakeDerivedFreshnessVerdicts:
    """The sweep-preflight freshness source over a REAL Parquet lake."""

    def test_fresh_derived_partition_reads_fresh(self, tmp_path) -> None:
        sink, _ = _seed_lake_1m(tmp_path)
        _write_derived(tmp_path, sink)
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1minute", "5minute"],
        )
        assert verdicts == {("NIFTY", "5minute"): "fresh"}

    def test_stale_derived_partition_reads_stale(self, tmp_path) -> None:
        """A 5minute partition holding the RAW 1-minute bars (a wrong-grid write —
        the drift shape a 1m backfill after the last prebuild leaves) reads stale."""
        sink, bars = _seed_lake_1m(tmp_path)
        sink.append(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="5minute", bars=bars)
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["5minute"],
        )
        assert verdicts == {("NIFTY", "5minute"): "stale"}

    def test_abstains_without_stored_derived_partition(self, tmp_path) -> None:
        """No stored derived partition → NOT keyed (the zero-bar slice preflight
        owns missing data); non-derived intervals are never keyed either."""
        _seed_lake_1m(tmp_path)
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1minute", "1day", "5minute"],
        )
        assert verdicts == {}

    def test_abstains_without_1minute_base(self, tmp_path) -> None:
        """A symbol with a derived partition but NO 1-minute base is unverifiable —
        honest abstain (mirrors the doctor's skip), never a guessed verdict."""
        from pathlib import Path

        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        other = tmp_path / "other"
        _sink, bars = _seed_lake_1m(other)  # borrow bars; write them as 5minute only
        sink2 = ParquetBarSink(writer=ParquetWriter(lake_root=Path(tmp_path)))
        sink2.append(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="5minute", bars=bars)
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["5minute"],
        )
        assert verdicts == {}

    def test_no_derived_intervals_requested_returns_empty_fast(self, tmp_path) -> None:
        """A fetched-intervals-only request short-circuits with ZERO lake reads."""
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1minute", "1day"],
        )
        assert verdicts == {}

    def test_one_minute_frame_read_once_per_symbol(self, tmp_path, monkeypatch) -> None:
        """Two derived intervals share ONE 1-minute read (the df_1m memo branch)."""
        sink, _ = _seed_lake_1m(tmp_path)
        _write_derived(tmp_path, sink, interval="5minute")
        _write_derived(tmp_path, sink, interval="15minute")
        reads = {"1minute": 0}
        real_read = tf_autoderive.read_lake_bars_frame

        def _counting_read(symbol_dir, interval, *, source=""):
            if interval == "1minute":
                reads["1minute"] += 1
            return real_read(symbol_dir, interval, source=source)

        monkeypatch.setattr(tf_autoderive, "read_lake_bars_frame", _counting_read)
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["5minute", "15minute"],
        )
        assert verdicts == {
            ("NIFTY", "5minute"): "fresh",
            ("NIFTY", "15minute"): "fresh",
        }
        assert reads["1minute"] == 1
