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
