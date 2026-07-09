"""Unit tests for the movers stored-read freshness guard (DEC-MOVERS-STORED-FRESHNESS-001).

The pure seams, driven with in-memory frames / bars (no route, no app):

* :func:`~brutex.dashboard.movers_stored_read.stored_day_frame_is_stale` — the
  exported PURE predicate (the screened-bar-reader composition seam): fresh /
  value-drift / height-drift / ts-grid-drift verdicts, the ``tf_minutes == 1``
  short-circuit (spy-proven to never touch the comparator), and a REAL-prebuild
  frame-level fresh verdict.
* :func:`_daily_stored_is_stale` — the daily VALUE-plane compare: equal /
  per-field drift / the deliberate timestamp exemption / OI irrelevance.
* :func:`_derive_daily_bars` — the canonical daily derive + the ``date.max``
  saturation abstain.
* :func:`_resample_frame_to_bars` / :func:`_frame_session_value_paisa` — the
  byte-identical extraction parity vs the wrapped public readers.
* :func:`warn_stored_stale_fallback` — cap / dedupe / suppressed arithmetic
  (the mirror of the ``movers.prev_baseline_noncontiguous`` warner tests).

Skipped when polars / pyarrow are absent (the lake deps).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.dashboard.movers_snapshot import (
    _STORED_STALE_WARN_CAP,
    _daily_stored_is_stale,
    _derive_daily_bars,
    _frame_session_value_paisa,
    _NoncontiguousWarnState,
    _read_cut_1m_frame,
    _read_session_bars,
    _resample_frame_to_bars,
    spot_session_value_paisa,
    warn_stored_stale_fallback,
)
from brutex.dashboard.movers_stored_read import stored_day_frame_is_stale
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_DAY = date(2024, 6, 13)


def _bar(*, hour: int, minute: int, close: str, volume: int, day: date = _DAY) -> CandleBar:
    """A 1-minute bar at ``HH:MM`` IST on *day* (IST = UTC + 05:30, no DST)."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c - Decimal("1"),
        high=c + Decimal("2"),
        low=c - Decimal("3"),
        close=c,
        volume=volume,
        open_interest=None,
    )


def _frame_1m(bars: list[CandleBar]) -> pl.DataFrame:
    """An in-memory raw-1m frame in the resampler's canonical shape."""
    return pl.DataFrame(
        {
            "ts_utc": [b.timestamp for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [b.volume for b in bars],
        }
    ).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))


def _session(base: int, *, day: date = _DAY, count: int = 10) -> list[CandleBar]:
    """*count* 1-minute bars 09:15… IST with varying closes/volumes."""
    return [
        _bar(hour=9, minute=15 + i, close=str(base + i * 10), volume=100 + i, day=day)
        for i in range(count)
    ]


def _daily_bar(
    *,
    open_: str = "20004",
    high: str = "20097",
    low: str = "20002",
    close: str = "20095",
    volume: int = 1045,
    oi: int | None = None,
    hour: int = 3,
) -> CandleBar:
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, hour, 45, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=oi,
    )


# ── stored_day_frame_is_stale (U1) ────────────────────────────────────────


class TestStoredDayFrameIsStale:
    def test_fresh_re_derive_is_not_stale(self) -> None:
        base = _frame_1m(_session(20005))
        from brutex.data.tf_resampler import resample_1m_to_tf

        stored = resample_1m_to_tf(base, 5).sort("ts_utc")
        assert stored_day_frame_is_stale(stored, base, tf_minutes=5) is False

    def test_value_drift_is_stale(self) -> None:
        base = _frame_1m(_session(20005))
        from brutex.data.tf_resampler import resample_1m_to_tf

        stored = (
            resample_1m_to_tf(base, 5)
            .sort("ts_utc")
            .with_columns((pl.col("close") + 1.0).alias("close"))
        )
        assert stored_day_frame_is_stale(stored, base, tf_minutes=5) is True

    def test_height_drift_is_stale(self) -> None:
        base = _frame_1m(_session(20005))
        from brutex.data.tf_resampler import resample_1m_to_tf

        stored = resample_1m_to_tf(base, 5).sort("ts_utc").head(1)  # a missing bucket
        assert stored_day_frame_is_stale(stored, base, tf_minutes=5) is True

    def test_timestamp_grid_drift_is_stale(self) -> None:
        # Same aggregates on a shifted (legacy epoch-anchored) grid → stale.
        base = _frame_1m(_session(20005))
        from brutex.data.tf_resampler import resample_1m_to_tf

        stored = (
            resample_1m_to_tf(base, 5)
            .sort("ts_utc")
            .with_columns(pl.col("ts_utc").dt.offset_by("1m"))
        )
        assert stored_day_frame_is_stale(stored, base, tf_minutes=5) is True

    def test_tf_minutes_1_short_circuits_without_the_comparator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stored 1minute partition IS the fetched base — trivially fresh;
        # the comparator must never be touched (no validate+clone+equals paid).
        from brutex.data import tf_autoderive

        def _must_not_compare(*args: object, **kwargs: object) -> str:
            raise AssertionError("tf_minutes == 1 must short-circuit, never compare")

        monkeypatch.setattr(tf_autoderive, "compare_derived_freshness", _must_not_compare)
        base = _frame_1m(_session(20005))
        # Even a nonsense stored frame is irrelevant at tf 1.
        stored = base.with_columns((pl.col("close") + 5.0).alias("close"))
        assert stored_day_frame_is_stale(stored, base, tf_minutes=1) is False

    def test_real_prebuild_frame_is_fresh(self, tmp_path: Path) -> None:
        # Frame-level pin against the REAL writer + materialiser output.
        from brutex.dashboard.movers_stored_read import read_stored_day_frame
        from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_session(20005),
        )
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
        )
        stored = read_stored_day_frame(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_DAY,
        )
        base = _read_cut_1m_frame(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            cutoff_min=None,
        )
        assert stored is not None and base is not None
        assert stored_day_frame_is_stale(stored, base, tf_minutes=5) is False


# ── _daily_stored_is_stale (U2) ───────────────────────────────────────────


class TestDailyStoredIsStale:
    def test_equal_values_not_stale(self) -> None:
        assert _daily_stored_is_stale([_daily_bar()], [_daily_bar()]) is False

    @pytest.mark.parametrize(
        "field_kwargs",
        [
            {"open_": "20005"},
            {"high": "20098"},
            {"low": "20001"},
            {"close": "20094"},
            {"volume": 1046},
        ],
    )
    def test_each_field_drift_is_stale(self, field_kwargs: dict[str, object]) -> None:
        drifted = _daily_bar(**field_kwargs)  # type: ignore[arg-type]
        assert _daily_stored_is_stale([_daily_bar()], [drifted]) is True

    def test_timestamp_only_difference_is_exempt(self) -> None:
        # The deliberate exemption: production stamps midnight IST while the
        # legacy/fixture convention stamps 09:15 — a stamping CONVENTION, not a
        # value drift; MoverSnapshot consumes values only.
        stored = _daily_bar(hour=3)
        derived = CandleBar(
            timestamp=datetime(_DAY.year, _DAY.month, _DAY.day - 1, 18, 30, tzinfo=UTC),
            open=stored.open,
            high=stored.high,
            low=stored.low,
            close=stored.close,
            volume=stored.volume,
            open_interest=None,
        )
        assert _daily_stored_is_stale([stored], [derived]) is False

    def test_open_interest_is_irrelevant(self) -> None:
        # Spot daily OI is None on BOTH sides by construction; a fixture OI
        # never flips the verdict (values are what the wire consumes).
        assert _daily_stored_is_stale([_daily_bar(oi=42)], [_daily_bar(oi=None)]) is False

    def test_height_drift_is_stale(self) -> None:
        assert _daily_stored_is_stale([_daily_bar()], []) is True


# ── _derive_daily_bars (U3) ───────────────────────────────────────────────


class TestDeriveDailyBars:
    def test_normal_derive_emits_the_selected_day(self) -> None:
        bars = _derive_daily_bars(_frame_1m(_session(20005)), _DAY)
        assert bars is not None and len(bars) == 1
        (bar,) = bars
        # The canonical resample_1m_to_daily law: element selections + int sum.
        assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
            Decimal("20004"),
            Decimal("20097"),
            Decimal("20002"),
            Decimal("20095"),
            1045,
        )
        assert bar.open_interest is None

    def test_date_max_saturates_to_none(self) -> None:
        # A degenerate ?date=9999-12-31 must abstain, never OverflowError
        # (the DEC-CLI-GREEKS-ENRICH-001 date-saturation precedent).
        assert _derive_daily_bars(_frame_1m(_session(20005)), date.max) is None


# ── extraction byte-parity (U4) ───────────────────────────────────────────


class TestExtractionParity:
    def test_resample_frame_to_bars_equals_read_session_bars(self, tmp_path: Path) -> None:
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_session(20005),
        )
        via_wrapper = _read_session_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            minutes=5,
            cutoff_min=9 * 60 + 19,
        )
        frame = _read_cut_1m_frame(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            cutoff_min=9 * 60 + 19,
        )
        assert frame is not None
        assert _resample_frame_to_bars(frame, 5) == via_wrapper
        assert via_wrapper  # a real session, not a vacuous [] == []

    def test_frame_session_value_paisa_equals_public_reader(self, tmp_path: Path) -> None:
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_session(20005),
        )
        via_public = spot_session_value_paisa(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            cutoff_min=None,
        )
        frame = _read_cut_1m_frame(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            cutoff_min=None,
        )
        assert frame is not None
        assert via_public is not None
        assert _frame_session_value_paisa(frame) == via_public


# ── warn_stored_stale_fallback (U5) ───────────────────────────────────────


def _warn(state: _NoncontiguousWarnState, symbol: str, interval: str = "5minute") -> None:
    warn_stored_stale_fallback(
        state,
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval=interval,
        snapshot_day=_DAY,
    )


class TestWarnStoredStaleFallback:
    def test_emits_named_fields(self) -> None:
        from structlog.testing import capture_logs

        state = _NoncontiguousWarnState()
        with capture_logs() as logs:
            _warn(state, "NIFTY")
        (event,) = logs
        assert event["event"] == "movers.stored_stale_fallback"
        assert event["exchange"] == "NSE"
        assert event["segment"] == "INDEX"
        assert event["symbol"] == "NIFTY"
        assert event["interval"] == "5minute"
        assert event["snapshot_day"] == _DAY.isoformat()
        assert state.emitted == 1

    def test_dedupes_per_symbol_interval(self) -> None:
        from structlog.testing import capture_logs

        state = _NoncontiguousWarnState()
        with capture_logs() as logs:
            _warn(state, "NIFTY")
            _warn(state, "NIFTY")  # the dup — dropped
            _warn(state, "NIFTY", interval="1day")  # a DIFFERENT interval — kept
        assert [e["interval"] for e in logs] == ["5minute", "1day"]
        assert state.emitted == 2
        assert state.suppressed == 0

    def test_cap_then_one_suppressed_summary_then_counting(self) -> None:
        from structlog.testing import capture_logs

        state = _NoncontiguousWarnState()
        with capture_logs() as logs:
            for i in range(_STORED_STALE_WARN_CAP + 3):
                _warn(state, f"SYM{i:03d}")
        warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        suppressed = [e for e in logs if e["event"] == "movers.stored_stale_fallback_suppressed"]
        assert len(warns) == _STORED_STALE_WARN_CAP
        assert len(suppressed) == 1
        assert suppressed[0]["cap"] == _STORED_STALE_WARN_CAP
        assert state.emitted == _STORED_STALE_WARN_CAP
        assert state.suppressed == 3

    def test_budget_is_independent_of_the_contiguity_warner(self) -> None:
        # Two SEPARATE per-build instances — the stored-stale budget can never
        # eat into the prev-baseline budget (and vice versa).
        stale_state = _NoncontiguousWarnState()
        contiguity_state = _NoncontiguousWarnState()
        for i in range(_STORED_STALE_WARN_CAP):
            _warn(stale_state, f"SYM{i:03d}")
        assert stale_state.emitted == _STORED_STALE_WARN_CAP
        assert contiguity_state.emitted == 0
