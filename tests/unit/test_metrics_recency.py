"""Unit tests for the recency metrics (DEC-METRICS-TRAILING-WINDOW-001).

The trailing-window trio (``trailing_sharpe_ratio`` /
``trailing_profit_factor`` / ``trailing_net_pnl_paisa``) + the
whole-history decay composite (``decay_weighted_net_paisa``), computed as a
SECOND fold/combine pass — :func:`fold_recency` (pure-integer fold state) →
:func:`combine_recency_from_folds` (the libm weights) — behind the GLOBAL
``trailing_anchor`` kw-only parameter of the public compute entries.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from brutex.metrics.compute import (
    DECAY_BUCKET_DAYS,
    DECAY_HALF_LIFE_MONTHS,
    TRAILING_WINDOW,
    RecencyFolds,
    _fold_trades,
    combine_recency_from_folds,
    compute_metrics,
    compute_metrics_from_scalars,
    fold_recency,
)
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.strategies.types import Side

#: The GLOBAL anchor every test threads (a fixed, tz-aware instant).
_ANCHOR = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
_CUTOFF = _ANCHOR - TRAILING_WINDOW

#: A flat drawdown 4-tuple (the recency pass never reads it).
_FLAT_DD = (0, 0.0, 0, 0.0)
_START_EQUITY = 1_000_000


def make_trade(*, pnl_paisa: int, entry_ts: datetime, exit_ts: datetime) -> ClosedTrade:
    return ClosedTrade(
        pnl_paisa=pnl_paisa,
        mfe_paisa=max(pnl_paisa, 0),
        mae_paisa=max(-pnl_paisa, 0),
        holding_bars=1,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        direction=Side.BUY,
    )


def _trade(exit_ts: datetime, pnl: int) -> ClosedTrade:
    return make_trade(
        pnl_paisa=pnl,
        entry_ts=exit_ts - timedelta(minutes=5),
        exit_ts=exit_ts,
    )


def _metrics(trades: list[ClosedTrade], **kwargs: object):
    return compute_metrics_from_scalars(
        trades,
        100,
        start_equity_paisa=_START_EQUITY,
        span_years=2.0,
        drawdown=_FLAT_DD,
        **kwargs,  # type: ignore[arg-type]
    )


class TestWindowMembership:
    def test_exit_exactly_at_cutoff_is_in(self) -> None:
        # The boundary law: exit_ts >= cutoff — AT the cutoff instant is IN.
        folds = fold_recency([_trade(_CUTOFF, 700)], _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.trailing is not None
        assert folds.trailing.net_pnl_paisa == 700

    def test_exit_one_microsecond_before_cutoff_is_out(self) -> None:
        before = _CUTOFF - timedelta(microseconds=1)
        folds = fold_recency([_trade(before, 700)], _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.trailing is None

    def test_membership_is_by_exit_not_entry(self) -> None:
        # A trade ENTERED before the cutoff but EXITED after it is IN.
        trade = make_trade(
            pnl_paisa=90,
            entry_ts=_CUTOFF - timedelta(days=3),
            exit_ts=_CUTOFF + timedelta(days=1),
        )
        folds = fold_recency([trade], _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.trailing is not None
        assert folds.trailing.net_pnl_paisa == 90

    def test_trailing_fold_equals_fold_trades_of_the_filtered_subset(self) -> None:
        # The C2 mechanical-mirror seam: the trailing member IS a plain
        # _fold_trades over the exit-filtered subsequence.
        inside = [_trade(_ANCHOR - timedelta(days=d), 50 * d) for d in (1, 40, 700)]
        outside = [_trade(_ANCHOR - timedelta(days=d), -30) for d in (731, 900)]
        folds = fold_recency(inside + outside, _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.trailing == _fold_trades(inside, _START_EQUITY)


class TestAnchorArms:
    def test_anchor_none_leaves_every_recency_field_none(self) -> None:
        m = _metrics([_trade(_ANCHOR - timedelta(days=1), 100)])
        assert m.trailing_sharpe_ratio is None
        assert m.trailing_profit_factor is None
        assert m.trailing_net_pnl_paisa is None
        assert m.decay_weighted_net_paisa is None

    def test_zero_trades_leaves_every_recency_field_none_even_with_anchor(self) -> None:
        m = _metrics([], trailing_anchor=_ANCHOR)
        assert m.total_trades == 0
        assert m.trailing_sharpe_ratio is None
        assert m.trailing_profit_factor is None
        assert m.trailing_net_pnl_paisa is None
        assert m.decay_weighted_net_paisa is None

    def test_naive_anchor_is_refused_loudly(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            fold_recency(
                [_trade(_ANCHOR, 1)],
                _START_EQUITY,
                trailing_anchor=datetime(2026, 1, 1),  # noqa: DTZ001 — the refusal under test
            )

    def test_curve_entry_threads_the_anchor_identically(self) -> None:
        trades = [_trade(_ANCHOR - timedelta(days=d), p) for d, p in ((5, 200), (800, -70))]
        equity = EquityCurve(
            equity_paisa=(_START_EQUITY, _START_EQUITY + 130),
            start_equity_paisa=_START_EQUITY,
            span_years=2.0,
        )
        via_curve = compute_metrics(trades, equity, total_bars=100, trailing_anchor=_ANCHOR)
        via_scalars = _metrics(trades, trailing_anchor=_ANCHOR)
        assert via_curve.trailing_net_pnl_paisa == via_scalars.trailing_net_pnl_paisa == 200
        assert via_curve.decay_weighted_net_paisa == via_scalars.decay_weighted_net_paisa


class TestTrailingTrio:
    def test_empty_window_yields_none_trio_but_concrete_decay(self) -> None:
        # Every trade older than the 730-day window: trio None (honest
        # absence), decay still concrete (whole-history by design).
        trades = [_trade(_ANCHOR - timedelta(days=800), 400)]
        m = _metrics(trades, trailing_anchor=_ANCHOR)
        assert m.trailing_sharpe_ratio is None
        assert m.trailing_profit_factor is None
        assert m.trailing_net_pnl_paisa is None
        assert m.decay_weighted_net_paisa is not None

    def test_trailing_values_match_hand_derivation(self) -> None:
        # Window holds +300 / -100 / +200 (old +900 excluded).
        inside = [
            _trade(_ANCHOR - timedelta(days=10), 300),
            _trade(_ANCHOR - timedelta(days=20), -100),
            _trade(_ANCHOR - timedelta(days=30), 200),
        ]
        old = _trade(_ANCHOR - timedelta(days=1000), 900)
        m = _metrics([old, *inside], trailing_anchor=_ANCHOR)
        assert m.trailing_net_pnl_paisa == 400
        assert m.trailing_profit_factor == 500.0 / 100.0
        # The main Sharpe law on the windowed returns (mean / population std).
        returns = [300 / _START_EQUITY, -100 / _START_EQUITY, 200 / _START_EQUITY]
        mean = sum(returns) / 3
        std = math.sqrt(sum((r - mean) ** 2 for r in returns) / 3)
        assert m.trailing_sharpe_ratio == pytest.approx(mean / std, rel=1e-12)
        # And the full-history figures differ (the old +900 counts there).
        assert m.net_pnl_paisa == 1300

    def test_trailing_pf_none_when_no_losers_in_window(self) -> None:
        # The main profit_factor law token-for-token: gross_loss == 0 ⇒ None
        # — even while the FULL history has losers (outside the window).
        trades = [
            _trade(_ANCHOR - timedelta(days=5), 100),
            _trade(_ANCHOR - timedelta(days=6), 50),
            _trade(_ANCHOR - timedelta(days=800), -400),
        ]
        m = _metrics(trades, trailing_anchor=_ANCHOR)
        assert m.trailing_profit_factor is None
        assert m.profit_factor is not None

    def test_trailing_sharpe_none_for_single_trade_window(self) -> None:
        # One windowed trade ⇒ windowed std undefined ⇒ the same None arm
        # as the main Sharpe on a 1-trade result.
        trades = [
            _trade(_ANCHOR - timedelta(days=5), 100),
            _trade(_ANCHOR - timedelta(days=800), 70),
        ]
        m = _metrics(trades, trailing_anchor=_ANCHOR)
        assert m.trailing_sharpe_ratio is None
        assert m.trailing_net_pnl_paisa == 100

    def test_all_inside_window_equals_full_history_figures(self) -> None:
        trades = [_trade(_ANCHOR - timedelta(days=d), p) for d, p in ((1, 80), (2, -30), (3, 60))]
        m = _metrics(trades, trailing_anchor=_ANCHOR)
        assert m.trailing_net_pnl_paisa == m.net_pnl_paisa
        assert m.trailing_profit_factor == m.profit_factor
        assert m.trailing_sharpe_ratio == m.sharpe_ratio


class TestDecayBuckets:
    def test_bucket_key_is_whole_30_day_months_of_age(self) -> None:
        trades = [
            _trade(_ANCHOR - timedelta(days=0), 10),  # bucket 0
            _trade(_ANCHOR - timedelta(days=29), 20),  # bucket 0
            _trade(_ANCHOR - timedelta(days=30), 30),  # bucket 1
            _trade(_ANCHOR - timedelta(days=95), -40),  # bucket 3
        ]
        folds = fold_recency(trades, _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.decay_buckets == ((0, 30), (1, 30), (3, -40))

    def test_bucket_sums_are_exact_integers(self) -> None:
        # Many trades in one bucket accumulate as an exact int sum.
        trades = [_trade(_ANCHOR - timedelta(days=40, minutes=i), 7) for i in range(1, 12)]
        folds = fold_recency(trades, _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.decay_buckets == ((1, 77),)
        assert isinstance(folds.decay_buckets[0][1], int)

    def test_buckets_are_sorted_ascending_regardless_of_trade_order(self) -> None:
        # The combine's float-summation determinism law: sorted keys.
        newest_first = [
            _trade(_ANCHOR - timedelta(days=5), 1),
            _trade(_ANCHOR - timedelta(days=400), 2),
            _trade(_ANCHOR - timedelta(days=65), 3),
        ]
        folds = fold_recency(newest_first, _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.decay_buckets == ((0, 1), (2, 3), (13, 2))

    def test_negative_age_clamps_to_bucket_zero(self) -> None:
        # Defensive: an exit AT/after the anchor has age 0 (never a
        # negative bucket whose weight would exceed 1.0).
        after = _trade(_ANCHOR + timedelta(days=2), 500)
        folds = fold_recency([after], _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.decay_buckets == ((0, 500),)

    def test_fractional_day_age_floors_via_timedelta_days(self) -> None:
        # timedelta.days floors: 29 days 23h → 29 → bucket 0.
        trade = _trade(_ANCHOR - timedelta(days=29, hours=23), 11)
        folds = fold_recency([trade], _START_EQUITY, trailing_anchor=_ANCHOR)
        assert folds.decay_buckets == ((0, 11),)


class TestDecayCombine:
    def test_weight_math_matches_hand_derivation(self) -> None:
        folds = RecencyFolds(trailing=None, decay_buckets=((0, 1000), (12, 1000), (24, -500)))
        rm = combine_recency_from_folds(folds)
        expected = 1000 * 0.5 ** (0 / 12.0) + 1000 * 0.5 ** (12 / 12.0) + (-500) * 0.5 ** (
            24 / 12.0
        )
        assert rm.decay_weighted_net_paisa == expected
        assert rm.decay_weighted_net_paisa == pytest.approx(1000.0 + 500.0 - 125.0)

    def test_half_life_is_configurable(self) -> None:
        folds = RecencyFolds(trailing=None, decay_buckets=((6, 1000),))
        fast = combine_recency_from_folds(folds, decay_half_life_months=6.0)
        slow = combine_recency_from_folds(folds, decay_half_life_months=24.0)
        assert fast.decay_weighted_net_paisa == 1000 * 0.5
        assert slow.decay_weighted_net_paisa == 1000 * 0.5**0.25
        assert fast.decay_weighted_net_paisa < slow.decay_weighted_net_paisa

    def test_infinite_half_life_is_the_plain_net_limit(self) -> None:
        folds = RecencyFolds(trailing=None, decay_buckets=((0, 300), (55, 500), (7, -100)))
        rm = combine_recency_from_folds(folds, decay_half_life_months=math.inf)
        assert rm.decay_weighted_net_paisa == 700.0

    @pytest.mark.parametrize("bad", [0.0, -1.0, math.nan])
    def test_non_positive_or_nan_half_life_is_refused(self, bad: float) -> None:
        folds = RecencyFolds(trailing=None, decay_buckets=((0, 1),))
        with pytest.raises(ValueError, match="decay_half_life_months"):
            combine_recency_from_folds(folds, decay_half_life_months=bad)

    def test_half_life_threads_through_the_public_entry(self) -> None:
        trades = [
            _trade(_ANCHOR - timedelta(days=180), 1000),
            _trade(_ANCHOR - timedelta(days=181), 1000),
        ]
        default = _metrics(trades, trailing_anchor=_ANCHOR)
        custom = _metrics(trades, trailing_anchor=_ANCHOR, decay_half_life_months=6.0)
        assert default.decay_weighted_net_paisa == 2000 * 0.5 ** (6 / DECAY_HALF_LIFE_MONTHS)
        assert custom.decay_weighted_net_paisa == 2000 * 0.5 ** (6 / 6.0)
        assert custom.decay_weighted_net_paisa < default.decay_weighted_net_paisa


class TestConstants:
    def test_frozen_law_constants(self) -> None:
        # 24 months (730 days); 30-day buckets; 12-month half-life default.
        assert TRAILING_WINDOW == timedelta(days=730)
        assert DECAY_BUCKET_DAYS == 30
        assert DECAY_HALF_LIFE_MONTHS == 12.0
