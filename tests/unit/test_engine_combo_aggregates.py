"""Unit tests for brutex.engine.combo_aggregates (DEC-SWEEP-RESULTS-DEPTH-001 WP2).

Covers: nearest-rank percentile edges (n=1 / n=20 / pct bounds), the TF + 1m
excursion folds (floor-mean law, partial-coverage NULL), the minute-path
drawdown wiring, the worst-5-days stress view (< 5 sessions and > 5 sessions),
the VIX win-vs-loss stats + the [p10, p90] band with 0/partial stamps, the
pinned ≥ 0.8 one-regime-wonder rule on both axes (never on a negative net),
the entry-hour + event-day lenses, exit-reason tallies, the consecutive-runs
fold (scratch breaks both), the compute-module win_loss_ratio /
recovery_factor laws, first-class NULL records for zero-trade and
parity-failed combos, record-key parity with ``trade_store.AGG_COLUMNS`` and
a real ``write_tuple_sidecars`` round trip.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.calendars.nse import IST
from brutex.engine.combo_aggregates import (
    REGIME_WONDER_SHARE,
    ComboAggregate,
    ComboAggregateError,
    _float_stats,
    aggregate_combo,
    nearest_rank,
)
from brutex.engine.minute_path import PathStatus, SessionEquityPoint
from brutex.engine.results_store import ResultRow
from brutex.engine.session_lens import SessionLens, TrendRegime
from brutex.engine.trade_capture import CapturedTrade, ComboCapture
from brutex.engine.trade_store import AGG_COLUMNS, sidecar_paths_for, write_tuple_sidecars
from brutex.strategies.types import Side

_RUN = "ab" * 32
_FP = "fe" * 32
_START_EQUITY = 10_000_000  # ₹1,00,000 in paisa — the sweep's own base.
_EMPTY_LENS = SessionLens(trend_by_date={}, event_flags_by_date={})


def _ts(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _trade(
    day: date,
    slot: int,
    pnl: int,
    *,
    mae: int = 10,
    mfe: int = 20,
    reason: str = "signal",
    vix_entry: str | None = None,
    status: PathStatus = PathStatus.NO_1M_DATA,
    mae_1m: int | None = None,
    mfe_1m: int | None = None,
    hour: int = 9,
) -> CapturedTrade:
    entry = _ts(day, hour, 16 + slot * 3)
    return CapturedTrade(
        entry_ts=entry,
        entry_px=Decimal("100.00"),
        exit_ts=entry + timedelta(minutes=1),
        exit_px=Decimal("100.00"),
        direction=Side.BUY,
        lots=None,  # NULL-by-design until P5 (Ruling 18)
        exit_reason=reason,
        pnl_paisa=pnl,
        costs_paisa=None,
        mae_paisa=mae,
        mfe_paisa=mfe,
        mae_1m_paisa=mae_1m,
        mfe_1m_paisa=mfe_1m,
        path_1m_status=status,
        vix_entry=None if vix_entry is None else Decimal(vix_entry),
        vix_exit=None if vix_entry is None else Decimal(vix_entry),
    )


def _capture(
    trades: tuple[CapturedTrade, ...],
    *,
    equity: tuple[SessionEquityPoint, ...] = (),
    equity_status: PathStatus = PathStatus.NO_1M_DATA,
    vix_missing: int = 0,
    parity_ok: bool = True,
    run_id: str = _RUN,
) -> ComboCapture:
    return ComboCapture(
        run_id=run_id,
        trades=trades,
        equity=equity,
        equity_status=equity_status,
        vix_missing_trades=vix_missing,
        parity_ok=parity_ok,
        parity_detail="" if parity_ok else "total_trades=0!=3",
    )


def _row(pnls: list[int], *, run_id: str = _RUN, max_dd_paisa: int = 0) -> ResultRow:
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="1minute",
        direction="bull",
        mode="A",
        mask_bits=0b1,
        net_pnl_paisa=sum(pnls),
        gross_profit_paisa=sum(p for p in pnls if p > 0),
        gross_loss_paisa=-sum(p for p in pnls if p < 0),
        max_drawdown_paisa=max_dd_paisa,
        expectancy_paisa=0,
        total_trades=len(pnls),
        winning_trades=sum(1 for p in pnls if p > 0),
        losing_trades=sum(1 for p in pnls if p < 0),
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("100.00"),
    )


def _aggregate(
    capture: ComboCapture,
    row: ResultRow,
    *,
    lens: SessionLens = _EMPTY_LENS,
    composite_score: float | None = None,
    fingerprint: str = _FP,
) -> ComboAggregate:
    return aggregate_combo(
        capture,
        row,
        lens=lens,
        composite_score=composite_score,
        fingerprint=fingerprint,
        start_equity_paisa=_START_EQUITY,
    )


_DAY = date(2024, 1, 2)


# ── nearest_rank ─────────────────────────────────────────────────────────────


class TestNearestRank:
    def test_n_1_every_pct_is_the_single_value(self) -> None:
        assert nearest_rank([7], 10) == 7
        assert nearest_rank([7], 95) == 7
        assert nearest_rank([7], 100) == 7

    def test_n_20_p95_is_the_19th_smallest(self) -> None:
        values = list(range(1, 21))
        assert nearest_rank(values, 95) == 19  # ceil(0.95·20) = 19 → index 18

    def test_n_20_p10_p90(self) -> None:
        values = list(range(1, 21))
        assert nearest_rank(values, 10) == 2  # ceil(2) = 2 → index 1
        assert nearest_rank(values, 90) == 18

    def test_pct_100_is_the_max(self) -> None:
        assert nearest_rank([3, 5, 9], 100) == 9

    def test_empty_rejected(self) -> None:
        with pytest.raises(ComboAggregateError, match="non-empty"):
            nearest_rank([], 95)

    @pytest.mark.parametrize("pct", [0, 101])
    def test_pct_out_of_range_rejected(self, pct: int) -> None:
        with pytest.raises(ComboAggregateError, match=r"pct must be in \[1, 100\]"):
            nearest_rank([1], pct)


# ── NULL records (zero-trade / parity-failed) ────────────────────────────────


def _assert_null_stats(agg: ComboAggregate) -> None:
    assert agg.mae_avg_paisa is None
    assert agg.mae_worst_paisa is None
    assert agg.mae_p95_paisa is None
    assert agg.mfe_avg_paisa is None
    assert agg.mfe_best_paisa is None
    assert agg.mfe_p95_paisa is None
    assert agg.mae_1m_avg_paisa is None
    assert agg.mfe_1m_avg_paisa is None
    assert agg.trades_with_1m == 0
    assert agg.max_dd_minute_path_paisa is None
    assert agg.max_dd_minute_path_pct is None
    assert agg.worst5_days_json == "[]"
    assert agg.vix_win_min is None
    assert agg.vix_loss_min is None
    assert agg.vix_band_lo is None
    assert agg.vix_band_hi is None
    assert agg.vix_stamped_trades == 0
    assert agg.pnl_by_year_json == "{}"
    assert agg.pnl_by_trend_json == "{}"
    assert agg.one_regime_wonder is False
    assert agg.regime_wonder_axis is None
    assert agg.entry_hour_hist_json == "{}"
    assert agg.event_day_counts_json == "{}"
    assert agg.event_day_pnl_json == "{}"
    assert (
        agg.exits_stop_loss
        == agg.exits_take_profit
        == agg.exits_signal
        == agg.exits_forced_1520
        == agg.exits_end_of_data
        == agg.exits_session_gap
        == 0
    )
    assert agg.max_consecutive_losses == 0
    assert agg.max_consecutive_wins == 0
    assert agg.win_loss_ratio is None
    assert agg.recovery_factor is None


class TestNullRecords:
    def test_zero_trade_combo_is_first_class_null(self) -> None:
        flat_equity = (SessionEquityPoint(_DAY, _START_EQUITY, _START_EQUITY, 0),)
        capture = _capture((), equity=flat_equity, equity_status=PathStatus.OK)
        agg = _aggregate(capture, _row([]), composite_score=1.5)
        assert agg.n_trades == 0
        assert agg.zero_trades is True
        assert agg.parity_ok is True
        assert agg.equity_status == "ok"
        assert agg.composite_score == 1.5  # pass-through, caller-owned
        assert agg.fingerprint == _FP
        _assert_null_stats(agg)

    def test_parity_failed_capture_is_null_with_flags(self) -> None:
        capture = _capture((), parity_ok=False)
        agg = _aggregate(capture, _row([50, -30, 15]))
        assert agg.n_trades == 0
        assert agg.zero_trades is False  # the STORE had trades
        assert agg.parity_ok is False
        assert agg.equity_status == "no_1m_data"
        _assert_null_stats(agg)

    def test_run_id_mismatch_rejected(self) -> None:
        capture = _capture(())
        with pytest.raises(ComboAggregateError, match="run_id"):
            _aggregate(capture, _row([], run_id="cd" * 32))


# ── Excursion folds ──────────────────────────────────────────────────────────


class TestExcursionStats:
    def test_tf_folds_floor_mean_max_p95(self) -> None:
        maes = [5, 10, 16]
        mfes = [7, 8, 100]
        trades = tuple(
            _trade(_DAY, i, 10, mae=maes[i], mfe=mfes[i], reason="signal") for i in range(3)
        )
        agg = _aggregate(_capture(trades), _row([10, 10, 10]))
        assert agg.mae_avg_paisa == (5 + 10 + 16) // 3  # floor mean = 10
        assert agg.mae_worst_paisa == 16
        assert agg.mae_p95_paisa == 16  # ceil(0.95·3) = 3 → the max
        assert agg.mfe_avg_paisa == (7 + 8 + 100) // 3
        assert agg.mfe_best_paisa == 100
        assert agg.mfe_p95_paisa == 100

    def test_1m_stats_when_every_trade_covered(self) -> None:
        trades = tuple(
            _trade(
                _DAY,
                i,
                10,
                status=PathStatus.OK,
                mae_1m=30 + i,
                mfe_1m=60 + i,
            )
            for i in range(3)
        )
        agg = _aggregate(_capture(trades), _row([10, 10, 10]))
        assert agg.trades_with_1m == 3
        assert agg.mae_1m_avg_paisa == (30 + 31 + 32) // 3
        assert agg.mae_1m_worst_paisa == 32
        assert agg.mae_1m_p95_paisa == 32
        assert agg.mfe_1m_avg_paisa == (60 + 61 + 62) // 3
        assert agg.mfe_1m_best_paisa == 62
        assert agg.mfe_1m_p95_paisa == 62

    def test_partial_1m_coverage_yields_null_1m_stats(self) -> None:
        """One PARTIAL trade poisons the 1m aggregate — apples never averaged
        with oranges; coverage stays visible via trades_with_1m."""
        trades = (
            _trade(_DAY, 0, 10, status=PathStatus.OK, mae_1m=30, mfe_1m=60),
            _trade(_DAY, 1, 10, status=PathStatus.PARTIAL, mae_1m=99, mfe_1m=99),
        )
        agg = _aggregate(_capture(trades), _row([10, 10]))
        assert agg.trades_with_1m == 1
        assert agg.mae_1m_avg_paisa is None
        assert agg.mae_1m_worst_paisa is None
        assert agg.mae_1m_p95_paisa is None
        assert agg.mfe_1m_avg_paisa is None
        assert agg.mfe_1m_best_paisa is None
        assert agg.mfe_1m_p95_paisa is None


# ── Minute-path drawdown + worst days ────────────────────────────────────────


class TestDrawdownAndWorstDays:
    def test_minute_path_drawdown_from_equity_series(self) -> None:
        equity = (
            SessionEquityPoint(_DAY, _START_EQUITY + 50, _START_EQUITY - 100, 1),
            SessionEquityPoint(
                _DAY + timedelta(days=1), _START_EQUITY - 200, _START_EQUITY - 350, 1
            ),
        )
        trades = (
            _trade(_DAY, 0, 50),
            _trade(_DAY + timedelta(days=1), 0, -250),
        )
        agg = _aggregate(
            _capture(trades, equity=equity, equity_status=PathStatus.OK), _row([50, -250])
        )
        # Peak = base + 50 (day-1 close); trough = day-2 intraday low.
        assert agg.max_dd_minute_path_paisa == (_START_EQUITY + 50) - (_START_EQUITY - 350)
        assert agg.max_dd_minute_path_pct == 400 / (_START_EQUITY + 50)
        assert agg.equity_status == "ok"

    def test_worst5_keeps_only_the_five_lowest_sessions(self) -> None:
        days = [_DAY + timedelta(days=i) for i in range(7)]
        pnls = [-50, 30, -10, -80, 5, -20, -60]
        trades = tuple(_trade(d, 0, p) for d, p in zip(days, pnls, strict=True))
        agg = _aggregate(_capture(trades), _row(pnls))
        worst = json.loads(agg.worst5_days_json)
        assert worst == [
            [days[3].isoformat(), -80],
            [days[6].isoformat(), -60],
            [days[0].isoformat(), -50],
            [days[5].isoformat(), -20],
            [days[2].isoformat(), -10],
        ]

    def test_worst5_with_fewer_than_five_sessions_and_same_day_sum(self) -> None:
        trades = (
            _trade(_DAY, 0, -30),
            _trade(_DAY, 1, -40),  # same session — sums to -70
            _trade(_DAY + timedelta(days=1), 0, 25),
        )
        agg = _aggregate(_capture(trades), _row([-30, -40, 25]))
        worst = json.loads(agg.worst5_days_json)
        assert worst == [
            [_DAY.isoformat(), -70],
            [(_DAY + timedelta(days=1)).isoformat(), 25],
        ]


# ── VIX stats + band ─────────────────────────────────────────────────────────


class TestVixStats:
    def test_no_stamped_trades_is_honest_null_band(self) -> None:
        trades = (_trade(_DAY, 0, 50), _trade(_DAY, 1, -30))
        agg = _aggregate(_capture(trades, vix_missing=2), _row([50, -30]))
        assert agg.vix_stamped_trades == 0
        assert agg.vix_missing_trades == 2
        assert agg.vix_win_min is None
        assert agg.vix_loss_min is None
        assert agg.vix_band_lo is None
        assert agg.vix_band_hi is None

    def test_win_loss_split_and_band(self) -> None:
        trades = (
            _trade(_DAY, 0, 50, vix_entry="14.5"),
            _trade(_DAY, 1, 60, vix_entry="18.25"),
            _trade(_DAY, 2, -30, vix_entry="22.0"),
            _trade(_DAY, 3, 0, vix_entry="30.0"),  # scratch — neither class
            _trade(_DAY, 4, 40),  # winner, unstamped — excluded
        )
        agg = _aggregate(_capture(trades, vix_missing=1), _row([50, 60, -30, 0, 40]))
        assert agg.vix_stamped_trades == 4
        assert agg.vix_missing_trades == 1
        assert agg.vix_win_min == 14.5
        assert agg.vix_win_avg == (14.5 + 18.25) / 2
        assert agg.vix_win_max == 18.25
        assert agg.vix_loss_min == agg.vix_loss_avg == agg.vix_loss_max == 22.0
        # Two stamped winners: p10 → 1st, p90 → 2nd (nearest-rank).
        assert agg.vix_band_lo == 14.5
        assert agg.vix_band_hi == 18.25

    def test_stamped_losers_only(self) -> None:
        trades = (_trade(_DAY, 0, -30, vix_entry="19.0"), _trade(_DAY, 1, 10))
        agg = _aggregate(_capture(trades), _row([-30, 10]))
        assert agg.vix_loss_min == 19.0
        assert agg.vix_win_min is None
        assert agg.vix_band_lo is None

    def test_repeated_equal_vix_mean_never_exceeds_max(self) -> None:
        """Regression: float summation must not push the stored avg past max.

        Three winners sharing the SAME 2-dp VIX print (a very common real
        pattern): ``sum([6.15]*3)/3 == 6.150000000000001 > 6.15`` pre-fix,
        which persisted ``vix_win_avg`` STRICTLY GREATER than ``vix_win_max``
        — an internally-inconsistent combo card. The clamp pins avg == the
        (identical) min/max exactly.
        """
        trades = tuple(_trade(_DAY, slot, 50, vix_entry="6.15") for slot in range(3))
        agg = _aggregate(_capture(trades), _row([50, 50, 50]))
        assert agg.vix_win_min == agg.vix_win_avg == agg.vix_win_max == 6.15
        assert agg.vix_band_hi is not None
        assert agg.vix_win_max is not None
        assert agg.vix_band_hi <= agg.vix_win_max

    def test_repeated_equal_vix_mean_never_undercuts_min(self) -> None:
        """Regression twin (the n=6 case violates the MIN side of the bracket).

        ``sum([6.15]*6)/6 == 6.1499999999999995 < 6.15`` pre-fix — the same
        invariant defect on the other edge; also pinned on the LOSS class so
        both `_float_stats` call sites are covered.
        """
        trades = tuple(_trade(_DAY, slot, -50, vix_entry="6.15") for slot in range(6))
        agg = _aggregate(_capture(trades), _row([-50] * 6))
        assert agg.vix_loss_min == agg.vix_loss_avg == agg.vix_loss_max == 6.15

    def test_float_stats_clamp_is_ulp_only_never_a_rounding(self) -> None:
        """An interior mean is returned at FULL raw precision (no rounding)."""
        values = [14.5, 18.25]
        assert _float_stats(values) == (14.5, (14.5 + 18.25) / 2, 18.25)
        # Both violation sides of the raw fold are corrected onto the bracket.
        assert _float_stats([6.15] * 3) == (6.15, 6.15, 6.15)
        assert _float_stats([6.15] * 6) == (6.15, 6.15, 6.15)


# ── Regime wonder + lenses ───────────────────────────────────────────────────


def _lens_for(days: dict[date, TrendRegime], flags: dict[date, tuple[str, ...]]) -> SessionLens:
    return SessionLens(trend_by_date=days, event_flags_by_date=flags)


class TestRegimeWonder:
    def test_year_axis_flags_and_wins_precedence(self) -> None:
        d2020 = date(2020, 6, 1)
        d2021 = date(2021, 6, 1)
        lens = _lens_for({d2020: TrendRegime.UP, d2021: TrendRegime.UP}, {})
        trades = (_trade(d2020, 0, 90), _trade(d2021, 0, 10))
        agg = _aggregate(_capture(trades), _row([90, 10]), lens=lens)
        # Year shares 90/10 AND trend share 100% — year wins the pinned order.
        assert agg.one_regime_wonder is True
        assert agg.regime_wonder_axis == "year"
        assert json.loads(agg.pnl_by_year_json) == {"2020": 90, "2021": 10}

    def test_trend_axis_flags_when_years_are_spread(self) -> None:
        d2020 = date(2020, 6, 1)
        d2021 = date(2021, 6, 1)
        lens = _lens_for({d2020: TrendRegime.UP, d2021: TrendRegime.UP}, {})
        trades = (_trade(d2020, 0, 50), _trade(d2021, 0, 50))
        agg = _aggregate(_capture(trades), _row([50, 50]), lens=lens)
        assert agg.one_regime_wonder is True
        assert agg.regime_wonder_axis == "trend"
        assert json.loads(agg.pnl_by_trend_json) == {"up": 100}

    def test_share_boundary_exactly_point_eight_flags(self) -> None:
        assert REGIME_WONDER_SHARE == 0.8
        d2020 = date(2020, 6, 1)
        d2021 = date(2021, 6, 1)
        lens = _lens_for({d2020: TrendRegime.UP, d2021: TrendRegime.DOWN}, {})
        trades = (_trade(d2020, 0, 80), _trade(d2021, 0, 20))
        agg = _aggregate(_capture(trades), _row([80, 20]), lens=lens)
        assert agg.one_regime_wonder is True
        assert agg.regime_wonder_axis == "year"

    def test_share_below_threshold_never_flags(self) -> None:
        d2020 = date(2020, 6, 1)
        d2021 = date(2021, 6, 1)
        lens = _lens_for({d2020: TrendRegime.UP, d2021: TrendRegime.DOWN}, {})
        trades = (_trade(d2020, 0, 79), _trade(d2021, 0, 21))
        agg = _aggregate(_capture(trades), _row([79, 21]), lens=lens)
        assert agg.one_regime_wonder is False
        assert agg.regime_wonder_axis is None

    def test_negative_net_pnl_never_flags(self) -> None:
        trades = (_trade(_DAY, 0, 10), _trade(_DAY, 1, -50))
        agg = _aggregate(_capture(trades), _row([10, -50]))
        assert agg.one_regime_wonder is False
        assert agg.regime_wonder_axis is None

    def test_negative_year_excluded_from_the_positive_denominator(self) -> None:
        d2020 = date(2020, 6, 1)
        d2021 = date(2021, 6, 1)
        trades = (_trade(d2020, 0, 100), _trade(d2021, 0, -40))
        agg = _aggregate(_capture(trades), _row([100, -40]))
        # Positives = {2020: 100} → share 1.0 → year wonder.
        assert agg.one_regime_wonder is True
        assert agg.regime_wonder_axis == "year"

    def test_unknown_trend_bucket_never_claims_regime_knowledge(self) -> None:
        """All profit in the 'unknown' bucket + years spread → NO wonder."""
        d2020 = date(2020, 6, 1)
        d2021 = date(2021, 6, 1)
        trades = (_trade(d2020, 0, 50), _trade(d2021, 0, 50))
        agg = _aggregate(_capture(trades), _row([50, 50]))  # empty lens
        assert json.loads(agg.pnl_by_trend_json) == {"unknown": 100}
        assert agg.one_regime_wonder is False


class TestTimeAndEventLenses:
    def test_entry_hour_histogram(self) -> None:
        trades = (
            _trade(_DAY, 0, 10, hour=9),
            _trade(_DAY, 1, 10, hour=9),
            _trade(_DAY, 2, 10, hour=14),
        )
        agg = _aggregate(_capture(trades), _row([10, 10, 10]))
        assert json.loads(agg.entry_hour_hist_json) == {"9": 2, "14": 1}

    def test_event_day_counts_and_pnl(self) -> None:
        flagged = _DAY
        plain = _DAY + timedelta(days=1)
        lens = _lens_for({}, {flagged: ("expiry", "muhurat")})
        trades = (
            _trade(flagged, 0, 40),
            _trade(flagged, 1, -15),
            _trade(plain, 0, 99),
        )
        agg = _aggregate(_capture(trades), _row([40, -15, 99]), lens=lens)
        assert json.loads(agg.event_day_counts_json) == {"expiry": 2, "muhurat": 2}
        assert json.loads(agg.event_day_pnl_json) == {"expiry": 25, "muhurat": 25}

    def test_pnl_by_trend_mixes_known_and_unknown(self) -> None:
        labelled = _DAY
        unlabelled = _DAY + timedelta(days=1)
        lens = _lens_for({labelled: TrendRegime.DOWN}, {})
        trades = (_trade(labelled, 0, -5), _trade(unlabelled, 0, 30))
        agg = _aggregate(_capture(trades), _row([-5, 30]), lens=lens)
        assert json.loads(agg.pnl_by_trend_json) == {"down": -5, "unknown": 30}


# ── Exit tallies, streaks, ratio laws ────────────────────────────────────────


class TestTalliesAndRatios:
    def test_exit_reason_tallies_cover_all_six(self) -> None:
        # One trade per vocabulary member incl. session_gap
        # (DEC-TRADESIM-OPEN-SESSION-GUARD-001 — the counter is the persistent
        # session-tail-hole signal).
        reasons = [
            "stop_loss",
            "take_profit",
            "signal",
            "forced_1520",
            "end_of_data",
            "session_gap",
        ]
        trades = tuple(_trade(_DAY, i, 10, reason=reason) for i, reason in enumerate(reasons))
        agg = _aggregate(_capture(trades), _row([10] * 6))
        assert agg.exits_stop_loss == 1
        assert agg.exits_take_profit == 1
        assert agg.exits_signal == 1
        assert agg.exits_forced_1520 == 1
        assert agg.exits_end_of_data == 1
        assert agg.exits_session_gap == 1
        # The RECORD carries the real (nonzero) count too — a to_record()
        # hardcoding "exits_session_gap": 0 dies here (refuter round 3 CM3).
        assert agg.to_record()["exits_session_gap"] == 1

    def test_consecutive_runs_scratch_breaks_both(self) -> None:
        pnls = [-10, -10, -10, 0, 20, 20, -5, 20]
        trades = tuple(_trade(_DAY, i, p) for i, p in enumerate(pnls))
        agg = _aggregate(_capture(trades), _row(pnls))
        assert agg.max_consecutive_losses == 3
        assert agg.max_consecutive_wins == 2

    def test_win_loss_ratio_integer_floor_law(self) -> None:
        # gross_profit = 100 over 3 winners → avg_win = 33 (floor);
        # gross_loss = 45 over 2 losers → avg_loss = 22 (floor).
        pnls = [40, 35, 25, -25, -20]
        trades = tuple(_trade(_DAY, i, p) for i, p in enumerate(pnls))
        agg = _aggregate(_capture(trades), _row(pnls))
        assert agg.win_loss_ratio == 33 / 22

    def test_win_loss_ratio_none_without_losers(self) -> None:
        pnls = [40, 35]
        trades = tuple(_trade(_DAY, i, p) for i, p in enumerate(pnls))
        agg = _aggregate(_capture(trades), _row(pnls))
        assert agg.win_loss_ratio is None

    def test_win_loss_ratio_zero_without_winners(self) -> None:
        pnls = [-40, -35]
        trades = tuple(_trade(_DAY, i, p) for i, p in enumerate(pnls))
        agg = _aggregate(_capture(trades), _row(pnls))
        assert agg.win_loss_ratio == 0.0  # avg_win 0 / avg_loss 37

    def test_recovery_factor_from_the_stored_row(self) -> None:
        pnls = [50, -20]
        trades = tuple(_trade(_DAY, i, p) for i, p in enumerate(pnls))
        agg = _aggregate(_capture(trades), _row(pnls, max_dd_paisa=60))
        assert agg.recovery_factor == 30 / 60

    def test_recovery_factor_none_on_zero_stored_drawdown(self) -> None:
        pnls = [50, -20]
        trades = tuple(_trade(_DAY, i, p) for i, p in enumerate(pnls))
        agg = _aggregate(_capture(trades), _row(pnls, max_dd_paisa=0))
        assert agg.recovery_factor is None


# ── Record shape + validation ────────────────────────────────────────────────


def _valid_agg_kwargs() -> dict[str, Any]:
    trades = (_trade(_DAY, 0, 10),)
    agg = _aggregate(_capture(trades), _row([10]))
    return {name: getattr(agg, name) for name, _ in AGG_COLUMNS}


def _agg_with(**overrides: Any) -> ComboAggregate:
    kwargs: dict[str, Any] = {**_valid_agg_kwargs(), **overrides}
    return ComboAggregate(**kwargs)


class TestRecordShape:
    def test_to_record_keys_match_agg_columns_in_order(self) -> None:
        agg = _aggregate(_capture((_trade(_DAY, 0, 10),)), _row([10]))
        assert list(agg.to_record().keys()) == [name for name, _ in AGG_COLUMNS]

    def test_write_tuple_sidecars_accepts_the_record(self, tmp_path: Path) -> None:
        root = tmp_path / "sweeps"
        paths = sidecar_paths_for(
            root / "NIFTY" / "1minute" / "bull_k1_feedfacefeedface.parquet",
            root,
            run_id="grid-0123456789abcdef",
        )
        capture = _capture((_trade(_DAY, 0, 10),))
        agg = _aggregate(capture, _row([10]))
        stamp = write_tuple_sidecars(
            paths, [capture], [agg.to_record()], store_content_hash="12" * 32
        )
        assert stamp.agg_rows == 1

    def test_nonzero_session_gap_round_trips_through_the_sidecar_chain(
        self, tmp_path: Path
    ) -> None:
        """``to_record()`` carries the REAL ``exits_session_gap`` count (refuter round 3 CM3).

        Round-trips a NONZERO count through the REAL chain — ``to_record()`` →
        ``write_tuple_sidecars`` → ``results_depth_pass._load_agg_records`` →
        ``ComboAggregate(**record)`` (the loader constructs the dataclass, so
        the ``__post_init__`` exit-tally sum invariant re-validates on
        read-back). A ``to_record()`` hardcoding ``"exits_session_gap": 0``
        dies twice over: the persisted five-tally sum (1) no longer equals
        ``n_trades`` (3), so the read-back construction raises
        ``ComboAggregateError`` — and even absent the invariant the read-back
        count would read 0 ≠ 2.
        """
        from brutex.engine.results_depth_pass import _load_agg_records

        root = tmp_path / "sweeps"
        paths = sidecar_paths_for(
            root / "NIFTY" / "1minute" / "bull_k1_feedfacefeedface.parquet",
            root,
            run_id="grid-0123456789abcdef",
        )
        trades = (
            _trade(_DAY, 0, 10, reason="session_gap"),
            _trade(_DAY, 1, 10, reason="session_gap"),
            _trade(_DAY, 2, 10, reason="signal"),
        )
        capture = _capture(trades)
        agg = _aggregate(capture, _row([10, 10, 10]))
        assert agg.exits_session_gap == 2
        record = agg.to_record()
        write_tuple_sidecars(paths, [capture], [record], store_content_hash="12" * 32)
        (read_back,) = _load_agg_records(paths)
        assert read_back.exits_session_gap == 2
        assert read_back.exits_signal == 1
        assert read_back.n_trades == 3
        # Belt (after the chain proved itself): the record itself carried the
        # real count — the tally test pins the count-1 shape directly.
        assert record["exits_session_gap"] == 2

    def test_validation_rejects_empty_run_id(self) -> None:
        with pytest.raises(ComboAggregateError, match="run_id"):
            _agg_with(run_id="")

    def test_validation_rejects_empty_fingerprint(self) -> None:
        with pytest.raises(ComboAggregateError, match="fingerprint"):
            _agg_with(fingerprint="")

    def test_validation_rejects_negative_n_trades(self) -> None:
        with pytest.raises(ComboAggregateError, match="n_trades"):
            _agg_with(n_trades=-1, exits_signal=-1)

    def test_validation_rejects_bad_1m_coverage_count(self) -> None:
        with pytest.raises(ComboAggregateError, match="trades_with_1m"):
            _agg_with(trades_with_1m=2)

    def test_validation_rejects_negative_vix_counters(self) -> None:
        with pytest.raises(ComboAggregateError, match="VIX counters"):
            _agg_with(vix_missing_trades=-1)

    def test_validation_rejects_exit_tally_mismatch(self) -> None:
        with pytest.raises(ComboAggregateError, match="exit tallies"):
            _agg_with(exits_stop_loss=5)

    def test_validation_rejects_negative_exit_tally(self) -> None:
        with pytest.raises(ComboAggregateError, match="exit tallies"):
            _agg_with(exits_stop_loss=-1, exits_take_profit=2)

    def test_aggregate_is_frozen(self) -> None:
        agg = _agg_with()
        with pytest.raises(AttributeError):
            agg.n_trades = 5  # type: ignore[misc]

    def test_agg_columns_is_55_with_the_grid_counters_in_pinned_order(self) -> None:
        """The five 1m-grid counters sit AFTER ``exits_session_gap`` and BEFORE
        ``max_consecutive_losses``, in the DEC-FILL-EXEC-GRID-1M-001 order —
        the 50→55 append-in-place law (`trade_store.AGG_COLUMNS`)."""
        names = [name for name, _ in AGG_COLUMNS]
        assert len(names) == 55
        anchor = names.index("exits_session_gap")
        assert names[anchor + 1 : anchor + 6] == [
            "exits_deferred",
            "signals_dropped_collision",
            "fill_slid_minutes",
            "exit_fill_slid_minutes",
            "forced_fill_slid_minutes",
        ]
        assert names[anchor + 6] == "max_consecutive_losses"

    def test_capture_grid_counters_carry_through_to_the_record(self) -> None:
        """`aggregate_combo` copies the walker's per-combo 1m-grid counter
        family off the capture VERBATIM — never re-derived, never zeroed on a
        healthy capture — and ``to_record()`` persists all five."""
        trades = (_trade(_DAY, 0, 10),)
        capture = replace(
            _capture(trades),
            exits_deferred=2,
            signals_dropped_collision=1,
            fill_slid_minutes=3,
            exit_fill_slid_minutes=4,
            forced_fill_slid_minutes=5,
        )
        agg = _aggregate(capture, _row([10]))
        assert (
            agg.exits_deferred,
            agg.signals_dropped_collision,
            agg.fill_slid_minutes,
            agg.exit_fill_slid_minutes,
            agg.forced_fill_slid_minutes,
        ) == (2, 1, 3, 4, 5)
        record = agg.to_record()
        assert record["exits_deferred"] == 2
        assert record["signals_dropped_collision"] == 1
        assert record["fill_slid_minutes"] == 3
        assert record["exit_fill_slid_minutes"] == 4
        assert record["forced_fill_slid_minutes"] == 5

    def test_default_capture_grid_counters_are_zero(self) -> None:
        agg = _aggregate(_capture((_trade(_DAY, 0, 10),)), _row([10]))
        assert (
            agg.exits_deferred,
            agg.signals_dropped_collision,
            agg.fill_slid_minutes,
            agg.exit_fill_slid_minutes,
            agg.forced_fill_slid_minutes,
        ) == (0, 0, 0, 0, 0)

    @pytest.mark.parametrize(
        "field",
        [
            "exits_deferred",
            "signals_dropped_collision",
            "fill_slid_minutes",
            "exit_fill_slid_minutes",
            "forced_fill_slid_minutes",
        ],
    )
    def test_validation_rejects_negative_grid_counter(self, field: str) -> None:
        # The counters are EVENT tallies (deliberately not a partition of
        # n_trades), so the only law is >= 0 — the __post_init__ arm.
        with pytest.raises(ComboAggregateError, match="1m-grid counters"):
            _agg_with(**{field: -1})

    def test_replace_keeps_validation_teeth(self) -> None:
        agg = _agg_with()
        with pytest.raises(ComboAggregateError, match="exit tallies"):
            replace(agg, n_trades=3)
