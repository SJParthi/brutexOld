"""DEC-DERIVED-1DAY-FRESHNESS-001 — the 1day freshness arm + the OI-aware compare.

The E2-H5(b) confirmed defect (derived-TF audit, DEC-DERIVED-TF-AUDIT-001):
``lake_derived_freshness_verdicts`` abstained on ``1day`` BY DESIGN, so a
corrupt/drifted derived ``1day`` partition — the PDH/PDL anchor source for
/movers, /db and the pre-open reports — was checked NOWHERE (the gate read
all-fresh with NO 1day key while a 77777.77 close served everywhere). The
gate now keys ``(symbol, "1day")`` via the segment-routed VALUE arm
``compare_daily_freshness`` whenever both a stored 1day and a 1m base exist;
the intraday compare additionally became OI-AWARE (a drifted / fabricated
``open_interest`` reads stale — pre-fix the compare projected OHLCV-6 only).

``test_drifted_1day_reads_stale`` and
``test_fno_intraday_oi_drift_only_reads_stale`` fail at the merge base (the
fix-reversion evidence): pre-fix, the 1day pair was never keyed and the
OI-only drift read "fresh".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.data import tf_autoderive
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_IST).astimezone(UTC)


def _bar(ts: datetime, px: float, *, vol: int = 100, oi: int | None = None) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(px)),
        high=Decimal(str(px + 0.5)),
        low=Decimal(str(px - 0.5)),
        close=Decimal(str(px + 0.1)),
        volume=vol,
        open_interest=oi,
    )


def _session(
    day: tuple[int, int, int],
    *,
    n: int = 15,
    oi: int | Callable[[int], int | None] | None = None,
) -> list[CandleBar]:
    y, mo, d = day
    bars: list[CandleBar] = []
    for i in range(n):
        mins = 9 * 60 + 15 + i
        this_oi: int | None = oi(i) if callable(oi) else oi
        bars.append(
            _bar(_ist(y, mo, d, mins // 60, mins % 60), 100.0 + i * 0.05, vol=100 + i, oi=this_oi)
        )
    return bars


def _seed(lake: Path, *, segment: str, symbol: str, bars: list[CandleBar]) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    sink.append(exchange="NSE", segment=segment, symbol=symbol, interval="1minute", bars=bars)


def _materialize(lake: Path, *, segment: str, symbol: str, kind: PrebuildKind) -> None:
    materialize_lake_timeframes(
        lake_root=lake,
        exchange="NSE",
        segment=segment,
        symbol=symbol,
        kind=kind,
        tfs=("5m",),
        include_daily=True,
        daily_before=date(2024, 6, 14),  # both fixture sessions complete
        incremental=False,
    )


def _verdicts(
    lake: Path, *, segment: str, symbol: str, intervals: list[str]
) -> dict[tuple[str, str], str]:
    return tf_autoderive.lake_derived_freshness_verdicts(
        lake, exchange="NSE", segment=segment, symbols=[symbol], intervals=intervals
    )


def _partition(lake: Path, segment: str, symbol: str, interval: str) -> Path:
    return lake / "bars" / "NSE" / segment / symbol / interval / "2024" / "06.parquet"


class TestDailyArm:
    """The (symbol, '1day') VALUE arm of lake_derived_freshness_verdicts."""

    def test_fresh_derived_1day_reads_fresh(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _seed(lake, segment="INDEX", symbol="NIFTY", bars=_session((2024, 6, 12)))
        _materialize(lake, segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT)
        assert _verdicts(lake, segment="INDEX", symbol="NIFTY", intervals=["1day"]) == {
            ("NIFTY", "1day"): "fresh"
        }

    def test_drifted_1day_reads_stale(self, tmp_path: Path) -> None:
        """The E2-H5(b) shape: whole-bar-consistent drift of the stored 1day
        (close → 77777.77) now reads ('SYM','1day'): 'stale' — pre-fix the
        gate returned NO 1day key at all (this test fails at merge base)."""
        lake = tmp_path / "lake"
        _seed(lake, segment="INDEX", symbol="NIFTY", bars=_session((2024, 6, 12)))
        _materialize(lake, segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT)
        p1d = _partition(lake, "INDEX", "NIFTY", "1day")
        df = pl.read_parquet(p1d)
        shift = 77777.77 - float(df.get_column("close")[0])
        df.with_columns(
            [(pl.col(c) + shift).alias(c) for c in ("open", "high", "low", "close")]
        ).write_parquet(p1d)
        assert _verdicts(lake, segment="INDEX", symbol="NIFTY", intervals=["1day"]) == {
            ("NIFTY", "1day"): "stale"
        }

    def test_fno_1day_oi_drift_only_reads_stale(self, tmp_path: Path) -> None:
        """F&O daily law routing: an OI-only drift on the stored FNO 1day
        (OHLCV untouched) reads stale — the compare includes open_interest."""
        lake = tmp_path / "lake"
        sym = "NSE-NIFTY-27Jun24-FUT"
        _seed(lake, segment="FNO", symbol=sym, bars=_session((2024, 6, 12), oi=lambda i: 1000 + i))
        _materialize(lake, segment="FNO", symbol=sym, kind=PrebuildKind.FUTURES)
        p1d = _partition(lake, "FNO", sym, "1day")
        df = pl.read_parquet(p1d)
        assert df.get_column("open_interest").to_list() == [1014]  # last-of-day OI
        df.with_columns(pl.lit(999_999).cast(pl.Int64).alias("open_interest")).write_parquet(p1d)
        assert _verdicts(lake, segment="FNO", symbol=sym, intervals=["1day"]) == {
            (sym, "1day"): "stale"
        }

    def test_prebuild_lag_stays_out_of_the_gate(self, tmp_path: Path) -> None:
        """Intersecting-dates law: a 1m session the stored 1day does not yet
        cover (the 1day lagging a 1m backfill — prebuild lag) is NOT a stale
        verdict — recency-by-absence is the prebuild's job, and a
        clock-entangled recency check would refusal-loop on a legitimately
        deferred forming session."""
        lake = tmp_path / "lake"
        _seed(lake, segment="INDEX", symbol="NIFTY", bars=_session((2024, 6, 12)))
        _materialize(lake, segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT)
        # A LATER session lands in the 1m base; the stored 1day still covers
        # only the first session.
        _seed(lake, segment="INDEX", symbol="NIFTY", bars=_session((2024, 6, 13)))
        assert _verdicts(lake, segment="INDEX", symbol="NIFTY", intervals=["1day"]) == {
            ("NIFTY", "1day"): "fresh"
        }

    def test_orphan_stored_day_warns_observe_only(self, tmp_path: Path) -> None:
        """A stored 1day row whose 1m base vanished (an orphan) is an
        observe-only derived_1day_orphan_rows WARN — never a stale verdict
        (latest-wins cannot delete an orphan; a stale verdict would be an
        un-remediable refusal loop)."""
        lake = tmp_path / "lake"
        _seed(
            lake,
            segment="INDEX",
            symbol="NIFTY",
            bars=_session((2024, 6, 12)) + _session((2024, 6, 13)),
        )
        _materialize(lake, segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT)
        # Remove the FIRST session's 1m rows (both sessions share the month
        # file): the stored 1day's 06-12 row becomes an orphan.
        p1m = _partition(lake, "INDEX", "NIFTY", "1minute")
        df = pl.read_parquet(p1m)
        cutoff = _ist(2024, 6, 13, 0, 0)
        df.filter(pl.col("timestamp") >= cutoff).write_parquet(p1m)
        with capture_logs() as logs:
            verdicts = _verdicts(lake, segment="INDEX", symbol="NIFTY", intervals=["1day"])
        assert verdicts == {("NIFTY", "1day"): "fresh"}
        (event,) = [e for e in logs if e["event"] == "derived_1day_orphan_rows"]
        assert event["log_level"] == "warning"
        assert event["orphan_dates"] == ["2024-06-12"]
        assert event["orphan_count"] == 1

    def test_1day_and_intraday_keyed_together(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _seed(lake, segment="INDEX", symbol="NIFTY", bars=_session((2024, 6, 12)))
        _materialize(lake, segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT)
        assert _verdicts(lake, segment="INDEX", symbol="NIFTY", intervals=["5minute", "1day"]) == {
            ("NIFTY", "5minute"): "fresh",
            ("NIFTY", "1day"): "fresh",
        }


class TestOiAwareIntradayCompare:
    """The intraday compare is OI-aware (pre-fix it projected OHLCV-6 only)."""

    def test_fno_intraday_oi_drift_only_reads_stale(self, tmp_path: Path) -> None:
        """An OI-only drift on the stored FNO 5minute (OHLCV untouched) now
        reads stale — kills the OI-blind mutant (fails at merge base)."""
        lake = tmp_path / "lake"
        sym = "NSE-NIFTY-27Jun24-FUT"
        _seed(lake, segment="FNO", symbol=sym, bars=_session((2024, 6, 12), oi=lambda i: 1000 + i))
        _materialize(lake, segment="FNO", symbol=sym, kind=PrebuildKind.FUTURES)
        assert _verdicts(lake, segment="FNO", symbol=sym, intervals=["5minute"]) == {
            (sym, "5minute"): "fresh"
        }
        p5 = _partition(lake, "FNO", sym, "5minute")
        pl.read_parquet(p5).with_columns(
            pl.lit(999_999).cast(pl.Int64).alias("open_interest")
        ).write_parquet(p5)
        assert _verdicts(lake, segment="FNO", symbol=sym, intervals=["5minute"]) == {
            (sym, "5minute"): "stale"
        }

    def test_legacy_fabricated_zero_oi_transient_stale_then_fresh(self, tmp_path: Path) -> None:
        """The disclosed ≤1-nightly transient: a legacy pre-DEC-DERIVED-OI-
        NULL-HONEST-001 partition (null OI fabricated as 0) reads stale under
        the OI-aware compare — and the next full re-derive (which the #1791
        writer_convention stamp bump forces on that SAME nightly) rewrites
        the honest nulls and reads fresh again."""
        lake = tmp_path / "lake"
        sym = "NSE-NIFTY-27Jun24-FUT"
        oi_pattern: dict[int, int | None] = {i: (1000 + i if i < 5 else None) for i in range(15)}
        _seed(
            lake,
            segment="FNO",
            symbol=sym,
            bars=_session((2024, 6, 12), oi=lambda i: oi_pattern[i]),
        )
        _materialize(lake, segment="FNO", symbol=sym, kind=PrebuildKind.FUTURES)
        assert _verdicts(lake, segment="FNO", symbol=sym, intervals=["5minute"]) == {
            (sym, "5minute"): "fresh"
        }
        # Rewrite the derived 5m as the LEGACY fabricated-0 bytes.
        p5 = _partition(lake, "FNO", sym, "5minute")
        pl.read_parquet(p5).with_columns(
            pl.col("open_interest").fill_null(0).alias("open_interest")
        ).write_parquet(p5)
        assert _verdicts(lake, segment="FNO", symbol=sym, intervals=["5minute"]) == {
            (sym, "5minute"): "stale"
        }
        # The re-derive (latest-wins) restores the honest nulls → fresh.
        _materialize(lake, segment="FNO", symbol=sym, kind=PrebuildKind.FUTURES)
        assert _verdicts(lake, segment="FNO", symbol=sym, intervals=["5minute"]) == {
            (sym, "5minute"): "fresh"
        }


class TestCompareDailyFreshnessUnits:
    """Direct-unit arms of the pure compare (edge branches)."""

    def _frames(self) -> tuple[pl.DataFrame, pl.DataFrame]:
        df_1m = pl.DataFrame(
            {
                "ts_utc": [
                    datetime(2024, 6, 12, 3, 45, tzinfo=UTC).replace(tzinfo=None)
                    + timedelta(minutes=i)
                    for i in range(15)
                ],
                "open": [100.0 + i * 0.05 for i in range(15)],
                "high": [100.5 + i * 0.05 for i in range(15)],
                "low": [99.5 + i * 0.05 for i in range(15)],
                "close": [100.1 + i * 0.05 for i in range(15)],
                "volume": [100 + i for i in range(15)],
            }
        )
        from brutex.data.spot_daily_resample import resample_1m_to_daily

        bars = resample_1m_to_daily(df_1m, before=date(2024, 6, 13))
        stored = pl.DataFrame(
            {
                "ts_utc": [b.timestamp for b in bars],
                "open": [float(b.open) for b in bars],
                "high": [float(b.high) for b in bars],
                "low": [float(b.low) for b in bars],
                "close": [float(b.close) for b in bars],
                "volume": [b.volume for b in bars],
            }
        )
        return df_1m, stored

    def test_empty_stored_frame_is_fresh(self) -> None:
        df_1m, stored = self._frames()
        assert (
            tf_autoderive.compare_daily_freshness(df_1m, stored.head(0), segment="INDEX") == "fresh"
        )

    def test_stored_without_oi_column_compares_ohlcv_only(self) -> None:
        """A hand-built stored frame WITHOUT the OI column degrades to the
        OHLCV compare on that partition (naive stamps treated as UTC)."""
        df_1m, stored = self._frames()
        assert tf_autoderive.compare_daily_freshness(df_1m, stored, segment="INDEX") == "fresh"
        drifted = stored.with_columns((pl.col("close") + 1.0).alias("close"))
        assert tf_autoderive.compare_daily_freshness(df_1m, drifted, segment="INDEX") == "stale"

    def test_drifted_stamp_reads_stale(self) -> None:
        """A wrong midnight-IST stamp (the wrong-bucket class) reads stale."""
        df_1m, stored = self._frames()
        shifted = stored.with_columns(pl.col("ts_utc") + timedelta(minutes=555))
        # The shifted stamp maps to the SAME IST date (18:30 UTC + 9h15 is
        # still 06-13 IST? — no: keep it same-date by shifting minutes only),
        # so it intersects and the instant mismatch is caught.
        assert tf_autoderive.compare_daily_freshness(df_1m, shifted, segment="INDEX") == "stale"


class TestSweepPreflightIntegration:
    """The fail-closed preflight fold picks the 1day arm up with zero changes."""

    def test_1day_drift_refuses_the_sweep_preflight(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_preflight import preflight_derived_freshness

        lake = tmp_path / "lake"
        _seed(lake, segment="INDEX", symbol="NIFTY", bars=_session((2024, 6, 12)))
        _materialize(lake, segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT)
        p1d = _partition(lake, "INDEX", "NIFTY", "1day")
        df = pl.read_parquet(p1d)
        df.with_columns((pl.col("close") + 500.0).alias("close")).write_parquet(p1d)
        report = preflight_derived_freshness(
            _verdicts(lake, segment="INDEX", symbol="NIFTY", intervals=["1minute", "1day"])
        )
        assert report.ok is False
        assert report.stale == (("NIFTY", "1day"),)
        assert "prebuild" in report.remediation()


class TestOutOfRangeStoredStamp:
    """R4-M1 (review round 1): an out-of-datetime-range STORED 1day stamp must
    yield the typed fail-closed "stale" verdict — never an uncaught
    OverflowError/ValueError/PanicException escaping the sweep-preflight gate
    (the #1594 rounds-12–18 isolation-escape class; the COMMON
    ``datetime_out_of_range_errors()`` set is the guard)."""

    def _df_1m(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "ts_utc": [
                    datetime(2024, 6, 12, 3, 45, tzinfo=UTC) + timedelta(minutes=i)
                    for i in range(5)
                ],
                "open": [100.0] * 5,
                "high": [100.5] * 5,
                "low": [99.5] * 5,
                "close": [100.1] * 5,
                "volume": [10] * 5,
            }
        ).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))

    def _stored(self, ts: datetime) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "ts_utc": [ts],
                "open": [100.0],
                "high": [100.5],
                "low": [99.5],
                "close": [100.1],
                "volume": [10],
            }
        ).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))

    def test_year_9999_evening_stamp_overflows_astimezone_reads_stale(self) -> None:
        # +05:30 pushes past datetime.max inside _ist_date_of.
        stored = self._stored(datetime(9999, 12, 31, 20, 0, tzinfo=UTC))
        assert (
            tf_autoderive.compare_daily_freshness(self._df_1m(), stored, segment="INDEX") == "stale"
        )

    def test_year_9999_last_date_overflows_the_before_derivation_reads_stale(self) -> None:
        # Survives _ist_date_of (9999-12-31 IST) but `last + timedelta(days=1)`
        # overflows date.max.
        stored = self._stored(datetime(9999, 12, 31, 0, 0, tzinfo=UTC))
        assert (
            tf_autoderive.compare_daily_freshness(self._df_1m(), stored, segment="INDEX") == "stale"
        )

    def test_beyond_datetime_max_i64_stamp_panic_reads_stale(self) -> None:
        # A hand-planted 2**62-microsecond stamp (~146,000 AD): polars
        # .to_list() PANICS (pyo3 PanicException, a BaseException) — the
        # third member of the COMMON set.
        stored = pl.DataFrame(
            {
                "ts_utc": pl.Series([2**62], dtype=pl.Int64),
                "open": [100.0],
                "high": [100.5],
                "low": [99.5],
                "close": [100.1],
                "volume": [10],
            }
        ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us", "UTC")))
        assert (
            tf_autoderive.compare_daily_freshness(self._df_1m(), stored, segment="INDEX") == "stale"
        )


class TestOutOfRangeBaseStamp:
    """Refuter round 1 (seat 1, MAJOR): the R4-M1 guard covered only the
    STORED-1day reduction — a corrupt out-of-range 1-MINUTE BASE stamp (a
    far-past negative i64, the #1594 hand-planted class) reached the daily
    law's re-derivation OUTSIDE the try and escaped as an uncaught
    PanicException through ``lake_derived_freshness_verdicts`` at the
    sweep-all gate (the intraday twin — polars-only — degrades gracefully on
    the SAME base). The re-derivation now rides the ValueError-EXCLUDING
    ``datetime_out_of_range_escape_errors()`` set (the consumption law: the
    law call spans arbitrary code where a ValueError can be a genuine bug —
    e.g. the R5 negative-volume-wrap class, which must keep propagating)."""

    def _valid_stored_1day(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "ts_utc": [datetime(2024, 6, 11, 18, 30, tzinfo=UTC)],
                "open": [100.0],
                "high": [100.5],
                "low": [99.5],
                "close": [100.1],
                "volume": [10],
            }
        ).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))

    def _corrupt_base(self) -> pl.DataFrame:
        """A clean session frame + ONE far-past -(2**62)-µs hand-planted row."""
        clean = pl.DataFrame(
            {
                "ts_utc": pl.Series(
                    [
                        int(datetime(2024, 6, 12, 3, 45 + i, tzinfo=UTC).timestamp() * 1_000_000)
                        for i in range(5)
                    ]
                    + [-(2**62)],
                    dtype=pl.Int64,
                ),
                "open": [100.0] * 6,
                "high": [100.5] * 6,
                "low": [99.5] * 6,
                "close": [100.1] * 6,
                "volume": [10] * 6,
            }
        ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us", "UTC")))
        return clean.sort("ts_utc")

    def test_corrupt_base_direct_compare_reads_stale(self) -> None:
        """Repro B2: pre-fix an ESCAPED PanicException; now the typed verdict."""
        assert (
            tf_autoderive.compare_daily_freshness(
                self._corrupt_base(), self._valid_stored_1day(), segment="INDEX"
            )
            == "stale"
        )

    def test_corrupt_base_fno_law_reads_stale(self) -> None:
        """The FNO-routed law arm takes the same guard."""
        stored = self._valid_stored_1day().with_columns(
            pl.lit(1004).cast(pl.Int64).alias("open_interest")
        )
        base = self._corrupt_base().with_columns(pl.lit(1000).cast(pl.Int64).alias("open_interest"))
        assert tf_autoderive.compare_daily_freshness(base, stored, segment="FNO") == "stale"

    def test_corrupt_base_on_disk_gate_shape_reads_stale(self, tmp_path: Path) -> None:
        """The refuter's /tmp/repro_gate.py shape: an on-disk lake whose
        1-minute BASE month file is hand-poisoned with the out-of-range row;
        the exact cli sweep-preflight call (lake_derived_freshness_verdicts)
        must return the typed verdict, never a raw Rust-panic traceback."""
        lake = tmp_path / "lake"
        _seed(lake, segment="INDEX", symbol="NIFTY", bars=_session((2024, 6, 12)))
        _materialize(lake, segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT)
        p1m = _partition(lake, "INDEX", "NIFTY", "1minute")
        df = pl.read_parquet(p1m)
        poison = df.head(1).with_columns(
            pl.lit(-(2**62)).cast(pl.Int64).cast(pl.Datetime("us", "UTC")).alias("timestamp")
        )
        pl.concat([poison, df]).write_parquet(p1m)
        verdicts = _verdicts(lake, segment="INDEX", symbol="NIFTY", intervals=["5minute", "1day"])
        # Fail-closed: the corrupt base refuses the launch on BOTH arms — the
        # intraday twin was already graceful (polars-only compare), the daily
        # arm now matches it instead of panicking.
        assert verdicts == {
            ("NIFTY", "5minute"): "stale",
            ("NIFTY", "1day"): "stale",
        }


class TestCorruptStoredValue:
    """Refuter round 2 (seat 1, MAJOR): the two out-of-range-STAMP guards vet
    STAMPS only — a corrupt/hand-planted stored 1day VALUE (a null OHLCV/
    volume → ``float(None)``/``int(None)`` TypeError; a NaN-f64 volume/OI →
    ``int(NaN)`` ValueError) previously ESCAPED the compare loop's bare
    conversions as an uncaught raise through the exact ``cli.py``
    sweep-preflight call (``lake_derived_freshness_verdicts``), while the
    polars-only intraday twin read "stale" gracefully (``.equals``) on the
    identical corruption — the #1594 isolation-escape class. The per-row
    stored-key build now fail-closes to the typed "stale" verdict + ONE
    ``derived_1day_corrupt_value`` WARN."""

    def _df_1m(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "ts_utc": [
                    datetime(2024, 6, 12, 3, 45, tzinfo=UTC) + timedelta(minutes=i)
                    for i in range(5)
                ],
                "open": [100.0] * 5,
                "high": [100.5] * 5,
                "low": [99.5] * 5,
                "close": [100.1] * 5,
                "volume": [10] * 5,
            }
        ).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))

    def _stored(
        self,
        *,
        close: float | None = 100.1,
        volume_f64: float | None = None,
        oi_f64: float | None = None,
    ) -> pl.DataFrame:
        """A stored 1day row on the INTERSECTING midnight-IST 2024-06-12 date.

        *volume_f64* / *oi_f64* build the column as Float64 (the NaN-capable
        hand-planted dtype); the default volume is the honest Int64.
        """
        frame = pl.DataFrame(
            {
                "ts_utc": [datetime(2024, 6, 11, 18, 30, tzinfo=UTC)],
                "open": [100.0],
                "high": [100.5],
                "low": [99.5],
                "close": pl.Series([close], dtype=pl.Float64),
                "volume": (
                    pl.Series([volume_f64], dtype=pl.Float64)
                    if volume_f64 is not None
                    else pl.Series([10], dtype=pl.Int64)
                ),
            }
        ).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))
        if oi_f64 is not None:
            frame = frame.with_columns(pl.Series("open_interest", [oi_f64], dtype=pl.Float64))
        return frame

    def test_null_close_reads_stale_with_one_warn(self) -> None:
        """The refuter's on-disk repro shape: pre-fix an ESCAPED
        ``TypeError: float() argument ... not 'NoneType'`` at float(close)."""
        with capture_logs() as logs:
            verdict = tf_autoderive.compare_daily_freshness(
                self._df_1m(), self._stored(close=None), segment="INDEX", source="NIFTY/1day"
            )
        assert verdict == "stale"
        (event,) = [e for e in logs if e["event"] == "derived_1day_corrupt_value"]
        assert event["log_level"] == "warning"
        assert event["source"] == "NIFTY/1day"
        assert event["ist_date"] == "2024-06-12"

    def test_null_volume_reads_stale(self) -> None:
        """Pre-fix: ESCAPED ``TypeError: int() ... not 'NoneType'``."""
        stored = self._stored().with_columns(pl.Series("volume", [None], dtype=pl.Int64))
        assert tf_autoderive.compare_daily_freshness(self._df_1m(), stored, segment="INDEX") == (
            "stale"
        )

    def test_nan_f64_volume_reads_stale(self) -> None:
        """Pre-fix: ESCAPED ``ValueError: cannot convert float NaN to integer``."""
        stored = self._stored(volume_f64=float("nan"))
        assert tf_autoderive.compare_daily_freshness(self._df_1m(), stored, segment="INDEX") == (
            "stale"
        )

    def test_nan_f64_oi_reads_stale(self) -> None:
        """A NaN OI in a hand-planted Float64 OI column: pre-fix the same
        ``int(NaN)`` ValueError escape through the OI leg of the key build."""
        stored = self._stored(oi_f64=float("nan"))
        assert tf_autoderive.compare_daily_freshness(self._df_1m(), stored, segment="INDEX") == (
            "stale"
        )

    def test_null_close_on_disk_gate_shape_reads_stale(self, tmp_path: Path) -> None:
        """The refuter's /tmp/repro_null_gate.py shape verbatim: a real lake
        whose STORED 1day partition is hand-poisoned with ``close=null`` on
        the intersecting date (1-minute base left CLEAN); the exact cli
        sweep-preflight call must return the typed verdict — never an
        uncaught TypeError traceback. The untouched intraday partition stays
        fresh (only the corrupt arm refuses)."""
        lake = tmp_path / "lake"
        _seed(lake, segment="INDEX", symbol="NIFTY", bars=_session((2024, 6, 12)))
        _materialize(lake, segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT)
        p1d = _partition(lake, "INDEX", "NIFTY", "1day")
        df = pl.read_parquet(p1d)
        pl.read_parquet(p1d).with_columns(
            pl.lit(None).cast(pl.Float64).alias("close")
        ).write_parquet(p1d)
        assert df.height == 1  # the poisoned row IS the intersecting date
        assert _verdicts(lake, segment="INDEX", symbol="NIFTY", intervals=["5minute", "1day"]) == {
            ("NIFTY", "5minute"): "fresh",
            ("NIFTY", "1day"): "stale",
        }
