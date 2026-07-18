"""Property tests for the recency metrics (DEC-METRICS-TRAILING-WINDOW-001).

The window-membership differentials (all-inside ≡ full-history figures;
all-outside ⇒ ``None``), the infinite-half-life plain-net limit, and the
anchor-shift decay monotonicity — each over hypothesis-drawn trade streams.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from brutex.metrics.compute import (
    TRAILING_WINDOW,
    compute_metrics_from_scalars,
    fold_recency,
    combine_recency_from_folds,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

_ANCHOR = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
_START_EQUITY = 1_000_000
_FLAT_DD = (0, 0.0, 0, 0.0)

#: Bounded pnl so any bucket / total sum stays far inside 2**53 — the float
#: additions of int-valued weights-1.0 terms are then EXACT (the inf-limit
#: property's exact-equality basis).
_pnls = st.lists(
    st.integers(min_value=-10_000, max_value=10_000), min_size=1, max_size=30
)


def _trades(pnls: list[int], ages_days: list[int]) -> list[ClosedTrade]:
    return [
        ClosedTrade(
            pnl_paisa=pnl,
            mfe_paisa=max(pnl, 0),
            mae_paisa=max(-pnl, 0),
            holding_bars=1,
            entry_ts=_ANCHOR - timedelta(days=age, minutes=5 + i),
            exit_ts=_ANCHOR - timedelta(days=age, minutes=i),
            direction=Side.BUY,
        )
        for i, (pnl, age) in enumerate(zip(pnls, ages_days, strict=True))
    ]


def _metrics(trades: list[ClosedTrade], **kwargs: object):
    return compute_metrics_from_scalars(
        trades,
        100,
        start_equity_paisa=_START_EQUITY,
        span_years=2.0,
        drawdown=_FLAT_DD,
        **kwargs,  # type: ignore[arg-type]
    )


@given(pnls=_pnls, data=st.data())
@settings(max_examples=80, deadline=None)
def test_all_inside_window_trailing_equals_full_history(pnls: list[int], data: st.DataObject) -> None:
    # Every exit strictly inside the 730-day window ⇒ the windowed fold IS
    # the full fold ⇒ the trailing trio equals the full-history figures.
    ages = data.draw(
        st.lists(st.integers(0, 729), min_size=len(pnls), max_size=len(pnls))
    )
    m = _metrics(_trades(pnls, ages), trailing_anchor=_ANCHOR)
    assert m.trailing_net_pnl_paisa == m.net_pnl_paisa
    assert m.trailing_profit_factor == m.profit_factor
    assert m.trailing_sharpe_ratio == m.sharpe_ratio


@given(pnls=_pnls, data=st.data())
@settings(max_examples=80, deadline=None)
def test_all_outside_window_trailing_is_none(pnls: list[int], data: st.DataObject) -> None:
    ages = data.draw(
        st.lists(st.integers(731, 2000), min_size=len(pnls), max_size=len(pnls))
    )
    m = _metrics(_trades(pnls, ages), trailing_anchor=_ANCHOR)
    assert m.trailing_net_pnl_paisa is None
    assert m.trailing_profit_factor is None
    assert m.trailing_sharpe_ratio is None
    # The decay composite stays concrete — whole-history by design.
    assert m.decay_weighted_net_paisa is not None


@given(pnls=_pnls, data=st.data())
@settings(max_examples=80, deadline=None)
def test_infinite_half_life_is_exactly_the_plain_net(pnls: list[int], data: st.DataObject) -> None:
    # half_life → ∞ ⇒ every weight is exactly 0.5**0.0 == 1.0 and the
    # bounded-int bucket sums add exactly in float (|Σ| far below 2**53), so
    # the limit is EXACT equality — no tolerance needed at ∞ itself (the
    # documented tolerance story applies to large FINITE half-lives, covered
    # below).
    ages = data.draw(
        st.lists(st.integers(0, 2000), min_size=len(pnls), max_size=len(pnls))
    )
    m = _metrics(_trades(pnls, ages), trailing_anchor=_ANCHOR, decay_half_life_months=math.inf)
    assert m.decay_weighted_net_paisa == float(sum(pnls))


@given(pnls=_pnls, data=st.data())
@settings(max_examples=40, deadline=None)
def test_huge_finite_half_life_approaches_the_plain_net(
    pnls: list[int], data: st.DataObject
) -> None:
    # A large FINITE half-life approaches the plain net within a relative
    # tolerance derived from the exponent bound: with ages <= 2000 days
    # (bucket <= 66) and half_life = 1e9, each weight is 0.5**(<= 6.6e-8)
    # in [1 - 5e-8, 1], so the weighted sum sits within 5e-8 of the plain
    # net relative to the ABSOLUTE-value sum (the signed net can cancel).
    ages = data.draw(
        st.lists(st.integers(0, 2000), min_size=len(pnls), max_size=len(pnls))
    )
    m = _metrics(_trades(pnls, ages), trailing_anchor=_ANCHOR, decay_half_life_months=1e9)
    assert m.decay_weighted_net_paisa is not None
    bound = 5e-8 * sum(abs(p) for p in pnls) + 1e-9
    assert abs(m.decay_weighted_net_paisa - sum(pnls)) <= bound


@given(pnls=st.lists(st.integers(1, 10_000), min_size=1, max_size=20), data=st.data())
@settings(max_examples=60, deadline=None)
def test_later_anchor_never_increases_all_positive_decay(
    pnls: list[int], data: st.DataObject
) -> None:
    # Anchor-shift monotonicity: pushing the GLOBAL anchor later only AGES
    # every all-positive trade (bucket keys non-decreasing), so the decay
    # composite is non-increasing. (Sign-mixed streams have no such law —
    # aging a loss RAISES the sum — hence the positive-only strategy.)
    ages = data.draw(st.lists(st.integers(0, 1000), min_size=len(pnls), max_size=len(pnls)))
    shift_days = data.draw(st.integers(1, 500))
    trades = _trades(pnls, ages)
    now = fold_recency(trades, _START_EQUITY, trailing_anchor=_ANCHOR)
    later = fold_recency(
        trades, _START_EQUITY, trailing_anchor=_ANCHOR + timedelta(days=shift_days)
    )
    assert (
        combine_recency_from_folds(later).decay_weighted_net_paisa
        <= combine_recency_from_folds(now).decay_weighted_net_paisa
    )


@given(pnls=_pnls, data=st.data())
@settings(max_examples=60, deadline=None)
def test_bucket_sums_conserve_the_exact_net(pnls: list[int], data: st.DataObject) -> None:
    # The fold is a pure integer partition of the trade stream: bucket sums
    # always total the exact net P&L, whatever the ages.
    ages = data.draw(st.lists(st.integers(0, 3000), min_size=len(pnls), max_size=len(pnls)))
    folds = fold_recency(_trades(pnls, ages), _START_EQUITY, trailing_anchor=_ANCHOR)
    assert sum(pnl for _bucket, pnl in folds.decay_buckets) == sum(pnls)
    keys = [bucket for bucket, _pnl in folds.decay_buckets]
    assert keys == sorted(keys)  # the combine's determinism law


@given(pnls=_pnls, data=st.data())
@settings(max_examples=60, deadline=None)
def test_window_split_net_is_additive(pnls: list[int], data: st.DataObject) -> None:
    # trailing net (inside) + the outside net == the full net — the window
    # partitions the stream by exit instant.
    ages = data.draw(st.lists(st.integers(0, 2000), min_size=len(pnls), max_size=len(pnls)))
    trades = _trades(pnls, ages)
    folds = fold_recency(trades, _START_EQUITY, trailing_anchor=_ANCHOR)
    cutoff = _ANCHOR - TRAILING_WINDOW
    outside_net = sum(t.pnl_paisa for t in trades if t.exit_ts < cutoff)
    inside_net = 0 if folds.trailing is None else folds.trailing.net_pnl_paisa
    assert inside_net + outside_net == sum(pnls)
