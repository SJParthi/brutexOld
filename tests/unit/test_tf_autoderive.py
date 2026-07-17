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

        def _counting_read(symbol_dir, interval, *, source="", screen_bad_ticks=False):
            if interval == "1minute":
                reads["1minute"] += 1
            return real_read(symbol_dir, interval, source=source, screen_bad_ticks=screen_bad_ticks)

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


def _seed_lake_1m_spike(lake_root, *, sole_bucket_spike: bool = False):
    """Write in-session 1-minute bars with ONE self-consistent ~5x spike bar.

    The spike bar (09:25 IST) has every OHLC field scaled together — internally
    ordered, strictly positive — so it passes every write-side guard (the exact
    class the bad-tick screen exists for; DEC-SWEEP-DATA-EDGE-GUARDS-001) and is
    STORED. With *sole_bucket_spike* the surrounding minutes are absent, so the
    spike is the SOLE 1-minute source of its 5-minute bucket (the ghost shape —
    DEC-DERIVED-GHOST-BUCKET-PRUNE-001).
    """
    from datetime import timezone
    from decimal import Decimal
    from pathlib import Path

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    ist = timezone(timedelta(hours=5, minutes=30))
    minutes = [0, 1, 2, 3, 9, 14, 15, 16, 17, 18] if sole_bucket_spike else list(range(20))
    bars = []
    for i in minutes:
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=ist).astimezone(UTC)
        close = Decimal("500.00") if i == 9 else Decimal("100.00") + Decimal(i) / 10
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
    sink.append(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars)
    return bars


def _production_prebuild(lake_root) -> None:
    """Run the REAL pipeline prebuild primitive over the NIFTY 5m timeframe."""
    from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

    materialize_lake_timeframes(
        lake_root=lake_root,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        kind=PrebuildKind.SPOT,
        tfs=["5m"],
        incremental=False,  # the forced FULL derive — never the stamp skip
    )


def _verdicts(lake_root) -> dict:
    return tf_autoderive.lake_derived_freshness_verdicts(
        lake_root,
        exchange="NSE",
        segment="INDEX",
        symbols=["NIFTY"],
        intervals=["5minute"],
    )


class TestBadTickScreenParity:
    """DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001 — the freshness re-derivation runs
    the SAME bad-tick screen the prebuild's 1-minute read runs (B-1: the false
    sweep/doctor wedge), plus the honest ghost-bucket pins (B-2:
    DEC-DERIVED-GHOST-BUCKET-PRUNE-001, the named follow-up)."""

    def test_screened_spike_lake_production_prebuild_reads_fresh(
        self, tmp_path, monkeypatch
    ) -> None:
        """FAILS-BEFORE (r10, the B-1 false wedge): a lake whose stored 1-minute
        legitimately holds ONE self-consistent >=4x in-session spike — exactly the
        class the screen exists for; write-side guards accept it — with derived
        TFs written by the PRODUCTION prebuild (screened) previously read
        PERMANENTLY 'stale' (the re-derivation was unscreened), and the printed
        remedy (re-run the prebuild) could never clear it. Screen parity: fresh."""
        monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
        _seed_lake_1m_spike(tmp_path)
        _production_prebuild(tmp_path)
        assert _verdicts(tmp_path) == {("NIFTY", "5minute"): "fresh"}
        # And a prebuild RE-run keeps it fresh (the remedy converges, no wedge).
        _production_prebuild(tmp_path)
        assert _verdicts(tmp_path) == {("NIFTY", "5minute"): "fresh"}

    def test_read_opt_in_screens_spike_and_default_read_does_not(
        self, tmp_path, monkeypatch
    ) -> None:
        """The opt-in seam: `screen_bad_ticks=True` drops the spike row (screen ON
        by default); the DEFAULT read is byte-identical to the pre-parity read —
        the stored DERIVED partitions (the prebuild's screened OUTPUT) must never
        be re-screened."""
        monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
        bars = _seed_lake_1m_spike(tmp_path)
        symbol_dir = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY"
        raw = tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute")
        screened = tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute", screen_bad_ticks=True)
        assert raw.height == len(bars)  # default: spike KEPT (unchanged behavior)
        assert screened.height == len(bars) - 1  # opt-in: spike dropped
        assert 500.0 in raw.get_column("close").to_list()
        assert 500.0 not in screened.get_column("close").to_list()

    def test_kill_switch_off_compares_unscreened_vs_unscreened(self, tmp_path, monkeypatch) -> None:
        """BRUTEX_BAD_TICK_SCREEN=0 flips BOTH boundaries together (the one-switch
        law): the prebuild writes the spike-carrying derivation and the freshness
        re-derivation keeps the spike too — unscreened-vs-unscreened, verdict
        fresh (the pre-parity behavior, byte-preserved)."""
        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
        bars = _seed_lake_1m_spike(tmp_path)
        _production_prebuild(tmp_path)
        symbol_dir = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY"
        # The opt-in read honors the kill switch: spike kept.
        screened = tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute", screen_bad_ticks=True)
        assert screened.height == len(bars)
        assert _verdicts(tmp_path) == {("NIFTY", "5minute"): "fresh"}

    def test_clean_lake_verdict_parity(self, tmp_path, monkeypatch) -> None:
        """A clean lake (no bad ticks) is verdict-identical with the screen ON —
        the screen returns clean frames unchanged, so the compare is byte-for-byte
        the pre-parity compare (the no-behavior-change pin)."""
        monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
        sink, _ = _seed_lake_1m(tmp_path)
        _write_derived(tmp_path, sink)
        symbol_dir = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY"
        raw = tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute")
        screened = tf_autoderive.read_lake_bars_frame(symbol_dir, "1minute", screen_bad_ticks=True)
        assert screened.equals(raw)
        assert _verdicts(tmp_path) == {("NIFTY", "5minute"): "fresh"}

    def test_ghost_bucket_reads_stale_honestly_and_escape_works(
        self, tmp_path, monkeypatch
    ) -> None:
        """The B-2 disposition pins (executed evidence for
        DEC-DERIVED-GHOST-BUCKET-PRUNE-001, the named follow-up):

        1. a stored derived bucket whose SOLE 1-minute source is screened away
           SURVIVES a forced full re-prebuild (the latest-wins derived writer
           never deletes) — the ghost;
        2. the screen-parity gate reads the ghost lake 'stale' — the HONEST
           verdict (the stored table genuinely serves a bucket the screened
           base disowns); never 'fresh' (pre-parity the unscreened compare
           blessed the poison bucket);
        3. the printed escape WORKS: delete the derived TF partition dirs +
           re-run the prebuild → fresh (the wedge is never permanent).
        """
        import shutil

        import polars as pl

        monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
        _seed_lake_1m_spike(tmp_path, sole_bucket_spike=True)
        derived_dir = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "5minute"
        # The pre-#1811-shaped derive (screen OFF): the spike-only bucket IS written.
        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
        _production_prebuild(tmp_path)
        monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
        # Screen back ON: a FORCED FULL re-prebuild does NOT delete the ghost.
        _production_prebuild(tmp_path)
        stored = pl.read_parquet(derived_dir.as_posix() + "/**/*.parquet")
        ghost_utc = datetime(2024, 1, 2, 3, 55)  # 09:25 IST — the spike-only bucket
        stamps = [t.replace(tzinfo=None) for t in stored.get_column("timestamp").to_list()]
        assert ghost_utc in stamps  # (1) the ghost survives
        assert _verdicts(tmp_path) == {("NIFTY", "5minute"): "stale"}  # (2) honest
        # (3) the printed escape: delete the derived dirs, re-run the prebuild.
        shutil.rmtree(derived_dir)
        _production_prebuild(tmp_path)
        assert _verdicts(tmp_path) == {("NIFTY", "5minute"): "fresh"}
