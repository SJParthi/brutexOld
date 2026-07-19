"""Property tests for the recency metrics (DEC-METRICS-TRAILING-WINDOW-001).

The window-membership differentials (all-inside ≡ full-history figures;
all-outside ⇒ ``None``), the infinite-half-life REFUSAL and the
huge-finite-half-life plain-net limit, and the
anchor-shift decay monotonicity — each over hypothesis-drawn trade streams.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import importlib
import math
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import NoReturn

import pytest
from brutex.metrics.compute import (
    TRAILING_WINDOW,
    combine_recency_from_folds,
    compute_metrics_from_scalars,
    fold_recency,
)
from brutex.metrics.compute_rust import METRICS_RUST_FOLD_ENV
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
_pnls = st.lists(st.integers(min_value=-10_000, max_value=10_000), min_size=1, max_size=30)


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


@contextmanager
def _rust_fold_arm(flag: str) -> Generator[None]:
    """Run a differential body under one BRUTEX_METRICS_RUST_FOLD arm.

    The "1" arm (Stage-C2) widens the seam's burn-in so EVERY metric row
    inside the body is kernel-vs-oracle bit-compared IN the seam, and
    refuses a silent autorevert at exit — turning each armed differential
    into a genuine kernel-vs-oracle proof; wheel-less runs SKIP the armed
    arm loudly (the unit gate enforces kernel presence in CI). The "0" arm
    is the verbatim Python path.
    """
    seam: ModuleType | None = None
    if flag == "1":
        seam = _compute_rust_or_skip()
        seam.reset_self_check_for_tests(remaining=10_000)
        seam.reset_observability_memo_for_tests()
    old = os.environ.get(METRICS_RUST_FOLD_ENV)
    os.environ[METRICS_RUST_FOLD_ENV] = flag
    try:
        yield
        if seam is not None:
            # A burn-in mismatch would have latched the shared autorevert.
            assert not seam.kernel_disabled()
    finally:
        if old is None:
            os.environ.pop(METRICS_RUST_FOLD_ENV, None)
        else:
            os.environ[METRICS_RUST_FOLD_ENV] = old
        if seam is not None:
            seam.reset_self_check_for_tests()
            seam.reset_observability_memo_for_tests()


@pytest.mark.parametrize("flag", ["0", "1"])
@given(pnls=_pnls, data=st.data())
@settings(max_examples=80, deadline=None)
def test_all_inside_window_trailing_equals_full_history(
    flag: str, pnls: list[int], data: st.DataObject
) -> None:
    # Every exit strictly inside the 730-day window ⇒ the windowed fold IS
    # the full fold ⇒ the trailing trio equals the full-history figures.
    ages = data.draw(st.lists(st.integers(0, 729), min_size=len(pnls), max_size=len(pnls)))
    with _rust_fold_arm(flag):
        m = _metrics(_trades(pnls, ages), trailing_anchor=_ANCHOR)
    assert m.trailing_net_pnl_paisa == m.net_pnl_paisa
    assert m.trailing_profit_factor == m.profit_factor
    assert m.trailing_sharpe_ratio == m.sharpe_ratio


@pytest.mark.parametrize("flag", ["0", "1"])
@given(pnls=_pnls, data=st.data())
@settings(max_examples=80, deadline=None)
def test_all_outside_window_trailing_is_none(
    flag: str, pnls: list[int], data: st.DataObject
) -> None:
    ages = data.draw(st.lists(st.integers(731, 2000), min_size=len(pnls), max_size=len(pnls)))
    with _rust_fold_arm(flag):
        m = _metrics(_trades(pnls, ages), trailing_anchor=_ANCHOR)
    assert m.trailing_net_pnl_paisa is None
    assert m.trailing_profit_factor is None
    assert m.trailing_sharpe_ratio is None
    # The decay composite stays concrete — whole-history by design.
    assert m.decay_weighted_net_paisa is not None


@pytest.mark.parametrize("flag", ["0", "1"])
@given(pnls=_pnls, data=st.data())
@settings(max_examples=80, deadline=None)
def test_infinite_half_life_is_refused_on_both_arms(
    flag: str, pnls: list[int], data: st.DataObject
) -> None:
    # CONSCIOUSLY FLIPPED (refuter round 1): ``inf`` was formerly the exact
    # plain-net limit through the public entry; validation symmetry now
    # refuses every non-finite half-life on BOTH fold arms (NaN was already
    # refused via the > 0 check — the silently-degenerate inf was the gap).
    # The near-plain-net semantics live in the huge-FINITE-half-life
    # property below, unchanged.
    ages = data.draw(st.lists(st.integers(0, 2000), min_size=len(pnls), max_size=len(pnls)))
    with _rust_fold_arm(flag), pytest.raises(ValueError, match="finite and > 0"):
        _metrics(_trades(pnls, ages), trailing_anchor=_ANCHOR, decay_half_life_months=math.inf)


@pytest.mark.parametrize("flag", ["0", "1"])
@given(pnls=_pnls, data=st.data())
@settings(max_examples=40, deadline=None)
def test_huge_finite_half_life_approaches_the_plain_net(
    flag: str, pnls: list[int], data: st.DataObject
) -> None:
    # A large FINITE half-life approaches the plain net within a relative
    # tolerance derived from the exponent bound: with ages <= 2000 days
    # (bucket <= 66) and half_life = 1e9, each weight is 0.5**(<= 6.6e-8)
    # in [1 - 5e-8, 1], so the weighted sum sits within 5e-8 of the plain
    # net relative to the ABSOLUTE-value sum (the signed net can cancel).
    ages = data.draw(st.lists(st.integers(0, 2000), min_size=len(pnls), max_size=len(pnls)))
    with _rust_fold_arm(flag):
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


# ─────────────────────────────────────────────────────────────────────────
# Stage-C2 kernel-vs-oracle differentials (DEC-METRICS-TRAILING-WINDOW-001)
# — the Rust recency fold twin against the Python `fold_recency` oracle
# over hostile trade streams. The wire-level differential needs NO env
# flag: `recency_wire_via_kernel` is called directly in the post-burn-in
# steady state with an oracle BOMB, so a served wire is provably
# kernel-computed, then bit-compared against the Python fold's wire
# projection. Skips loudly when the compiled wheel (or the recency symbol)
# is absent — the unit gate's importorskip stanza is the enforcement that
# the kernel path is genuinely exercised in CI.


def _compute_rust_or_skip() -> ModuleType:
    fno_math: ModuleType | None
    try:
        fno_math = importlib.import_module("fno_math")
    except ImportError:  # pragma: no cover - wheel-present envs never hit it
        fno_math = None
    if fno_math is None:  # pragma: no cover - wheel-present envs never hit it
        pytest.skip("fno_math wheel not installed")
    if getattr(fno_math, "metrics_fold_recency", None) is None:  # pragma: no cover
        pytest.skip("stale fno_math wheel — no metrics_fold_recency")
    from brutex.metrics import compute_rust

    return compute_rust


_DAY_US = 86_400_000_000

#: Hostile exit ages in µs: the cutoff straddle (±2 µs around 730 d), the
#: bucket edges (29/30/31 d ± 1 µs), defensively-negative ages (exits
#: AFTER the anchor), and the broad range.
_age_us = st.one_of(
    st.integers(min_value=730 * _DAY_US - 2, max_value=730 * _DAY_US + 2),
    st.sampled_from(
        [
            0,
            1,
            29 * _DAY_US,
            30 * _DAY_US - 1,
            30 * _DAY_US,
            30 * _DAY_US + 1,
            31 * _DAY_US,
        ]
    ),
    st.integers(min_value=-2 * _DAY_US, max_value=0),
    st.integers(min_value=0, max_value=3000 * _DAY_US),
)


def _trades_us(pnls: list[int], ages_us: list[int]) -> list[ClosedTrade]:
    return [
        ClosedTrade(
            pnl_paisa=pnl,
            mfe_paisa=max(pnl, 0),
            mae_paisa=max(-pnl, 0),
            holding_bars=1,
            entry_ts=_ANCHOR - timedelta(microseconds=age) - timedelta(minutes=5),
            exit_ts=_ANCHOR - timedelta(microseconds=age),
            direction=Side.BUY,
        )
        for pnl, age in zip(pnls, ages_us, strict=True)
    ]


@given(pnls=_pnls, data=st.data())
@settings(max_examples=100, deadline=None)
def test_recency_kernel_wire_bit_equals_python_oracle(pnls: list[int], data: st.DataObject) -> None:
    compute_rust = _compute_rust_or_skip()
    from brutex.metrics.compute import _recency_wire

    ages_us = data.draw(st.lists(_age_us, min_size=len(pnls), max_size=len(pnls)))
    trades = _trades_us(pnls, ages_us)
    anchor_us = compute_rust.datetime_to_epoch_us(_ANCHOR)
    cutoff_us = compute_rust.datetime_to_epoch_us(_ANCHOR - TRAILING_WINDOW)
    compute_rust.reset_self_check_for_tests(remaining=0)  # steady state
    try:

        def _bomb() -> NoReturn:
            raise AssertionError("steady state must not evaluate the oracle")

        wire = compute_rust.recency_wire_via_kernel(
            trades,
            _START_EQUITY,
            anchor_us=anchor_us,
            cutoff_us=cutoff_us,
            oracle_wire=_bomb,
        )
        # A degrade here would be silent vacuity — refuse it: the wheel is
        # present (skip-guarded above), so the kernel MUST serve.
        assert wire is not None
        oracle = _recency_wire(fold_recency(trades, _START_EQUITY, trailing_anchor=_ANCHOR))
        assert compute_rust.recency_wire_compare_key(wire) == compute_rust.recency_wire_compare_key(
            oracle
        )
    finally:
        compute_rust.reset_self_check_for_tests()
        compute_rust.reset_observability_memo_for_tests()
