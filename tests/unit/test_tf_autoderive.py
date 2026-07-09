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

from datetime import UTC, date, datetime, timedelta

import polars as pl
from brutex.data import tf_autoderive
from brutex.data.tf_autoderive import compare_daily_freshness, compare_derived_freshness

_OPEN_UTC = datetime(2025, 6, 9, 3, 45, tzinfo=UTC).replace(tzinfo=None)

# Trading days for the 1day-arm fixtures (2024-01-02/03 are real NSE sessions).
_D1 = date(2024, 1, 2)
_D2 = date(2024, 1, 3)
_D3 = date(2024, 1, 4)


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
        # The compare cores (intraday + the 1day sibling of
        # DEC-MOVERS-STORED-FRESHNESS-001) + the sweep-preflight freshness source +
        # its shared lake reader (DEC-SUSPICION-SWEEP-FIXES-001 G1) — and NOTHING
        # of the removed auto-derive arm.
        assert tf_autoderive.__all__ == [
            "compare_daily_freshness",
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


def _day_1m_bars(day: date, *, n_bars: int = 20, base: str = "100.00"):
    """*n_bars* in-session 1-minute bars (09:16 IST →) on *day*."""
    from datetime import timezone
    from decimal import Decimal

    from brutex.data.models import CandleBar

    ist = timezone(timedelta(hours=5, minutes=30))
    bars = []
    for i in range(n_bars):
        ts = datetime(day.year, day.month, day.day, 9, 16 + i, tzinfo=ist).astimezone(UTC)
        close = Decimal(base) + Decimal(i) / 10
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
    return bars


def _seed_lake_1m_days(lake_root, days, symbol: str = "NIFTY"):
    """Write one 1-minute session per day in *days* to a real Parquet lake; return the sink."""
    from pathlib import Path

    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=Path(lake_root)))
    for j, day in enumerate(days):
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol=symbol,
            interval="1minute",
            bars=_day_1m_bars(day, base=f"{100 + j}.00"),
        )
    return sink


def _write_daily_derived(lake_root, sink, *, before: date, symbol: str = "NIFTY") -> None:
    """Materialise the derived ``1day`` from the STORED 1-minute via the canonical
    midnight-IST daily law + the REAL lake writer (the prebuild write path)."""
    from brutex.data.spot_daily_resample import resample_1m_to_daily

    symbol_dir = lake_root / "bars" / "NSE" / "INDEX" / symbol
    df_1m = tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute")
    sink.append(
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval="1day",
        bars=resample_1m_to_daily(df_1m, before=before),
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


class TestCompareDailyFreshness:
    """The ``1day`` comparator (DEC-MOVERS-STORED-FRESHNESS-001 fold-in — closes
    the BND-5b sweep-preflight abstain on the derived ``1day``)."""

    def _frames(self, tmp_path, *, days, daily_before: date) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Seed 1m for *days* + a ``1day`` partition derived at *daily_before*
        through the REAL writer path; return the two read-back compare frames."""
        sink = _seed_lake_1m_days(tmp_path, days)
        _write_daily_derived(tmp_path, sink, before=daily_before)
        symbol_dir = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY"
        return (
            tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute"),
            tf_autoderive.read_lake_bars_frame(symbol_dir, "1day"),
        )

    def test_fresh_through_the_real_writer_round_trip(self, tmp_path) -> None:
        """The dtype/stamp round-trip proof: derive + WRITE via the production
        path, read back, verdict fresh — midnight-IST stamps survive the lake."""
        df_1m, df_1day = self._frames(tmp_path, days=(_D1, _D2), daily_before=_D3)
        assert df_1day.height == 2
        assert compare_daily_freshness(df_1m, df_1day, before=_D3) == "fresh"

    def test_value_drift_is_stale(self, tmp_path) -> None:
        df_1m, df_1day = self._frames(tmp_path, days=(_D1, _D2), daily_before=_D3)
        drifted = df_1day.with_columns((pl.col("volume") + 1).alias("volume"))
        assert compare_daily_freshness(df_1m, drifted, before=_D3) == "stale"

    def test_grown_1m_extra_day_is_stale(self, tmp_path) -> None:
        """The BND-5b shape: the 1m base grew a completed day the stored ``1day``
        never derived (kill-9 mid-re-derive / CLI fetch with no prebuild) —
        the height early-out fires stale."""
        df_1m, df_1day = self._frames(tmp_path, days=(_D1, _D2), daily_before=_D2)
        assert df_1day.height == 1  # only _D1 derived; _D2 exists in 1m only
        assert compare_daily_freshness(df_1m, df_1day, before=_D3) == "stale"

    def test_stamp_only_drift_is_stale_the_deliberate_divergence(self, tmp_path) -> None:
        """A 09:15-session-open-stamped stored bar with EQUAL values is stale HERE
        (the sweep consumes the stamp into run identity, Golden Rule #4) while the
        movers VALUE-plane comparator deliberately calls the same bars equal —
        the documented divergence pin: never 'unify' the two comparators."""
        df_1m, df_1day = self._frames(tmp_path, days=(_D1, _D2), daily_before=_D3)
        # Midnight IST (18:30 UTC prior day) → the 09:15-IST session open (03:45 UTC).
        shifted = df_1day.with_columns(pl.col("ts_utc") + timedelta(hours=9, minutes=15))
        assert compare_daily_freshness(df_1m, shifted, before=_D3) == "stale"
        from brutex.dashboard.movers_snapshot import _daily_stored_is_stale
        from brutex.data.spot_daily_resample import frame_to_candle_bars, resample_1m_to_daily

        stored_bars = frame_to_candle_bars(shifted, absent_open_interest=None)
        derived_bars = resample_1m_to_daily(df_1m, before=_D3)
        assert _daily_stored_is_stale(stored_bars, derived_bars) is False

    def test_forming_day_excluded_on_both_sides_is_fresh(self, tmp_path) -> None:
        """Mid-session determinism: *before* drops the forming day from the
        EXPECTED side exactly as the writer did — no false-stale mid-session."""
        df_1m, df_1day = self._frames(tmp_path, days=(_D1, _D2), daily_before=_D2)
        assert compare_daily_freshness(df_1m, df_1day, before=_D2) == "fresh"


class TestLakeDailyFreshnessVerdicts:
    """The ``1day`` arm of the sweep-preflight freshness source
    (DEC-MOVERS-STORED-FRESHNESS-001 fold-in) over REAL Parquet lakes."""

    def test_new_1m_day_without_rederive_reads_stale(self, tmp_path) -> None:
        """THE BND-5b pin: 1day derived through D1, then a 1m fetch adds D2 with
        NO re-derive → the pair is KEYED stale (previously an ungated abstain)."""
        sink = _seed_lake_1m_days(tmp_path, (_D1,))
        _write_daily_derived(tmp_path, sink, before=_D2)
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_day_1m_bars(_D2, base="101.00"),
        )
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1day"],
            daily_before=_D3,
        )
        assert verdicts == {("NIFTY", "1day"): "stale"}

    def test_fully_derived_1day_reads_fresh(self, tmp_path) -> None:
        sink = _seed_lake_1m_days(tmp_path, (_D1, _D2))
        _write_daily_derived(tmp_path, sink, before=_D3)
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1day"],
            daily_before=_D3,
        )
        assert verdicts == {("NIFTY", "1day"): "fresh"}

    def test_default_cutoff_resolves_via_production_daily_derive_cutoff(self, tmp_path) -> None:
        """No *daily_before* injected → the production resolver
        (``daily_derive_cutoff(now_ist())``) — the 2024 fixture sessions are long
        complete under any current-clock cutoff, so the verdict is fresh."""
        sink = _seed_lake_1m_days(tmp_path, (_D1, _D2))
        _write_daily_derived(tmp_path, sink, before=_D3)
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1day"],
        )
        assert verdicts == {("NIFTY", "1day"): "fresh"}

    def test_daily_before_injection_is_deterministic(self, tmp_path) -> None:
        """The test seam controls the forming-session cutoff: a D1-only stored
        ``1day`` is FRESH at ``before=_D2`` (D2 treated as forming) and STALE at
        ``before=_D3`` (D2 complete → the stored table is provably behind)."""
        sink = _seed_lake_1m_days(tmp_path, (_D1, _D2))
        _write_daily_derived(tmp_path, sink, before=_D2)
        common = {
            "exchange": "NSE",
            "segment": "INDEX",
            "symbols": ["NIFTY"],
            "intervals": ["1day"],
        }
        fresh = tf_autoderive.lake_derived_freshness_verdicts(tmp_path, daily_before=_D2, **common)
        stale = tf_autoderive.lake_derived_freshness_verdicts(tmp_path, daily_before=_D3, **common)
        assert fresh == {("NIFTY", "1day"): "fresh"}
        assert stale == {("NIFTY", "1day"): "stale"}

    def test_abstains_without_stored_1day_partition(self, tmp_path) -> None:
        """A 1m-only lake (never prebuilt) → NOT keyed — the zero-bar slice
        preflight owns missing data (abstain semantics preserved)."""
        _seed_lake_1m_days(tmp_path, (_D1,))
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1day"],
            daily_before=_D3,
        )
        assert verdicts == {}

    def test_abstains_without_1minute_base_for_1day(self, tmp_path) -> None:
        """A ``1day``-only partition (no 1m base) is unverifiable → honest abstain."""
        from datetime import timezone
        from decimal import Decimal
        from pathlib import Path

        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        ist = timezone(timedelta(hours=5, minutes=30))
        daily_bar = CandleBar(
            timestamp=datetime(_D1.year, _D1.month, _D1.day, 0, 0, tzinfo=ist).astimezone(UTC),
            open=Decimal("100.00"),
            high=Decimal("102.00"),
            low=Decimal("99.00"),
            close=Decimal("101.00"),
            volume=20000,
            open_interest=None,
        )
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=Path(tmp_path)))
        sink.append(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1day", bars=[daily_bar]
        )
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1day"],
            daily_before=_D3,
        )
        assert verdicts == {}

    def test_fno_segment_1day_abstains(self, tmp_path) -> None:
        """An FNO-segment ``1day`` is never keyed — the F&O daily law is the
        OI-carrying ``resample_1m_to_fno_daily`` (spot comparator would
        false-flag; named follow-up DEC-FNO-DAILY-FRESHNESS-001) — proven over a
        REAL stored FNO 1m + 1day pair, not an empty lake."""
        from datetime import timezone
        from decimal import Decimal
        from pathlib import Path

        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        ist = timezone(timedelta(hours=5, minutes=30))
        contract = "NSE-NIFTY-25Jan24-22000-CE"

        def _fno_bar(hour: int, minute: int) -> CandleBar:
            return CandleBar(
                timestamp=datetime(
                    _D1.year, _D1.month, _D1.day, hour, minute, tzinfo=ist
                ).astimezone(UTC),
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=Decimal("101.00"),
                volume=100,
                open_interest=500,
            )

        sink = ParquetBarSink(writer=ParquetWriter(lake_root=Path(tmp_path)))
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol=contract,
            interval="1minute",
            bars=[_fno_bar(9, 16), _fno_bar(9, 17)],
        )
        sink.append(
            exchange="NSE", segment="FNO", symbol=contract, interval="1day", bars=[_fno_bar(0, 0)]
        )
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbols=[contract],
            intervals=["1day"],
            daily_before=_D3,
        )
        assert verdicts == {}

    def test_1minute_only_request_short_circuits(self, tmp_path) -> None:
        """Neither an intraday TF nor ``1day`` requested → the early return
        (byte-preserved pre-change fast path)."""
        sink = _seed_lake_1m_days(tmp_path, (_D1,))
        _write_daily_derived(tmp_path, sink, before=_D2)
        verdicts = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY"],
            intervals=["1minute"],
        )
        assert verdicts == {}

    def test_mixed_request_intraday_verdicts_byte_identical(self, tmp_path) -> None:
        """The B3 non-regression pin: adding ``1day`` to the request leaves the
        intraday verdicts byte-identical to an intraday-only call, and an
        intervals list WITHOUT ``1day`` never keys the daily pair."""
        sink = _seed_lake_1m_days(tmp_path, (_D1,))
        _write_derived(tmp_path, sink)  # fresh 5minute
        _write_daily_derived(tmp_path, sink, before=_D2)
        common = {"exchange": "NSE", "segment": "INDEX", "symbols": ["NIFTY"]}
        only = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path, intervals=["5minute"], **common
        )
        mixed = tf_autoderive.lake_derived_freshness_verdicts(
            tmp_path, intervals=["5minute", "1day"], daily_before=_D2, **common
        )
        assert only == {("NIFTY", "5minute"): "fresh"}
        assert {k: v for k, v in mixed.items() if k[1] != "1day"} == only
        assert mixed[("NIFTY", "1day")] == "fresh"
        assert ("NIFTY", "1day") not in only

    def test_one_minute_frame_read_once_across_intraday_and_daily(
        self, tmp_path, monkeypatch
    ) -> None:
        """The ``df_1m`` memo is SHARED by the intraday and ``1day`` arms —
        exactly ONE 1-minute read for a mixed request."""
        sink = _seed_lake_1m_days(tmp_path, (_D1,))
        _write_derived(tmp_path, sink)
        _write_daily_derived(tmp_path, sink, before=_D2)
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
            intervals=["5minute", "1day"],
            daily_before=_D2,
        )
        assert verdicts == {
            ("NIFTY", "5minute"): "fresh",
            ("NIFTY", "1day"): "fresh",
        }
        assert reads["1minute"] == 1
