"""Dual-arm parity properties for the batch Rust Stage-1A arm.

DEC-STAGE1A-RUST-BATCH-001 — the hypothesis net over `build_bar_bits`:
flag-ON output equals the flag-OFF (frozen Python builder) output
element-for-element, with an ENGAGEMENT SPY proving the ON arm genuinely
crossed the FFI (the vacuous-gate trap — a silently-fallen-back ON arm would
make every equality here prove nothing; Golden Rule #10).

Swept axes: multi-day spans over REAL NSE trading days (the weekly fib
anchors refuse a non-trading `today`, so the corpus must be calendar-true);
``prior_n`` ∈ {1..4}; all three fib anchors incl. cold weeks; proximity
extremes + ``bps 0``; roll schedules (incl. single-day contracts —
consecutive rolls — and roll-on-first-bar layouts); backwards-dated bars
(the day boundary is ``!=``, not ``>``); duplicate timestamps; **non-2dp
Decimal bars** (the HLC3 snap trap — the typical price is computed from the
RAW Decimals then snapped ONCE, so re-deriving it from the individually
snapped OHLC diverges: the 0.004/0.004/0.007 counter-example is pinned);
zero-volume / pre-open-only feeds (VWAP never warms); all-doji feeds;
``apply_1day_mask=True``; flag-ON suffix independence (truncation +
mutation); flag-ON roll equivalence; and the G-6 exception-parity arm
(envelope violations raise IDENTICAL exceptions ON vs OFF).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
import trading_core
from brutex.calendars.nse import is_trading_day
from brutex.data.models import CandleBar
from brutex.engine.rust_stage1a import (
    STAGE1A_RUST_ENV,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)
from brutex.engine.stage1a import build_bar_bits
from brutex.strategies.fib_definitions import FibAnchorKind
from brutex.vocabulary.evaluator import ProximityParams
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): pyright does not model the Hypothesis
# `@st.composite` signature rewrite — the same scoped convention as
# tests/property/test_sweep_suffix_independence.py.

_IST = timezone(timedelta(hours=5, minutes=30))

#: A pool of REAL consecutive NSE trading days (calendar-checked so the
#: rolling-weekly / weekly-static anchors never refuse a non-trading `today`).
_TRADING_DAYS: list[date] = []
_d = date(2024, 1, 1)
while len(_TRADING_DAYS) < 15:
    if _d.weekday() < 5 and is_trading_day(_d):
        _TRADING_DAYS.append(_d)
    _d += timedelta(days=1)

#: Rupee price STRINGS incl. non-2dp Decimals (the HLC3 snap-trap corpus) and
#: sub-tick magnitudes that snap to 0.00 paisa.
_PRICE_TEXT = st.one_of(
    st.just("0.004"),
    st.just("0.007"),
    st.just("99.995"),
    st.integers(min_value=1, max_value=100_000).map(str),
    st.integers(min_value=1, max_value=10_000_000).map(lambda p: f"{p / 1000:.3f}"),
)


def _bar_from(
    d: date, minute_offset: int, o: str, h: str, lo: str, c: str, volume: int
) -> CandleBar:
    """One bar at ``09:15 IST + minute_offset`` on *d* (negative = pre-open)."""
    ts = datetime(d.year, d.month, d.day, 9, 15, tzinfo=_IST) + timedelta(minutes=minute_offset)
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=volume,
        open_interest=None,
    )


def _ordered_ohlc(a: str, b: str) -> tuple[str, str, str, str]:
    """(open, high, low, close) with high/low spanning the drawn pair."""
    open_d, close_d = Decimal(a), Decimal(b)
    high = max(open_d, close_d)
    low = min(open_d, close_d)
    return a, str(high), str(low), b


@st.composite
def _stream(draw: st.DrawFn) -> tuple[list[CandleBar], dict[str, Any]]:
    """Draw (bars, build_bar_bits kwargs) covering the swept axes."""
    # Day layout: indices into the trading-day pool, possibly BACKWARDS and
    # possibly repeating (the day boundary is `!=`).
    day_indices = draw(
        st.lists(st.integers(min_value=0, max_value=9), min_size=1, max_size=4, unique=False)
    )
    all_doji = draw(st.booleans())
    pre_open_only = draw(st.booleans())
    zero_volume = draw(st.booleans())
    bars: list[CandleBar] = []
    for di in day_indices:
        d = _TRADING_DAYS[di]
        n_bars = draw(st.integers(min_value=1, max_value=4))
        # Duplicate timestamps allowed: minutes drawn WITH replacement.
        minutes = draw(
            st.lists(
                st.integers(min_value=-10 if pre_open_only else 0, max_value=5),
                min_size=n_bars,
                max_size=n_bars,
            )
        )
        if pre_open_only:
            minutes = [-10 + (m % 10) for m in minutes]  # keep strictly pre-09:15
        for m in sorted(minutes):
            a = draw(_PRICE_TEXT)
            b = a if all_doji else draw(_PRICE_TEXT)
            o, h, lo, c = _ordered_ohlc(a, b)
            bars.append(_bar_from(d, m, o, h, lo, c, 0 if zero_volume else 100))

    kwargs: dict[str, Any] = {
        "prior_n": draw(st.integers(min_value=1, max_value=4)),
        "fib_anchor": draw(st.sampled_from(list(FibAnchorKind))),
        "apply_1day_mask": draw(st.booleans()),
    }
    bps = draw(st.sampled_from([0, 5, 10, 200, 10_000]))
    kwargs["proximity"] = ProximityParams(
        pivot_proximity_bps=bps, pdh_pdl_proximity_bps=bps, fib_proximity_bps=bps
    )
    if draw(st.booleans()):
        # Futures arm: single-day contracts (a roll on EVERY day boundary —
        # consecutive rolls, incl. the layout where the roll lands on the
        # very first / very last bar of the stream) or one far expiry.
        used = sorted({b.timestamp.astimezone(_IST).date() for b in bars})
        if draw(st.booleans()):
            kwargs["roll_schedule"] = tuple(d.toordinal() for d in used)
        else:
            kwargs["roll_schedule"] = (used[-1].toordinal() + 30,)
    return bars, kwargs


def _dual_arm(bars: list[CandleBar], **kwargs: Any) -> tuple[list[int], list[int]]:
    """(flag-OFF masks, flag-ON masks) with the ON arm engagement-spied."""
    reset_self_check_for_tests(remaining=0)  # steady state: no oracle rebuild
    reset_observability_memo_for_tests()
    real = trading_core.stage1a_bar_bits_batch
    calls = [0]

    def _spy(*args: Any, **kw: Any) -> list[int]:
        calls[0] += 1
        out: list[int] = real(*args, **kw)
        return out

    prior_env = os.environ.get(STAGE1A_RUST_ENV)
    try:
        # Explicit "0" (the kill switch), NOT pop-to-unset: under
        # DEC-RUST-DEFAULT-ON-WAVE-001 an UNSET flag resolves default-ON, so
        # popping would silently route the "OFF" arm to Rust (a vacuous
        # Rust-vs-Rust parity). "0" forces the genuine Python oracle.
        os.environ[STAGE1A_RUST_ENV] = "0"
        off = build_bar_bits(bars, **kwargs)
        trading_core.stage1a_bar_bits_batch = _spy  # type: ignore[attr-defined]
        os.environ[STAGE1A_RUST_ENV] = "1"
        with capture_logs() as on_logs:
            on = build_bar_bits(bars, **kwargs)
    finally:
        trading_core.stage1a_bar_bits_batch = real  # type: ignore[attr-defined]
        if prior_env is None:  # pragma: no cover — the conftest belt presets the flag, so prior_env is always "0"
            os.environ.pop(STAGE1A_RUST_ENV, None)
        else:
            os.environ[STAGE1A_RUST_ENV] = prior_env
        reset_self_check_for_tests()
        reset_observability_memo_for_tests()
    assert calls[0] == 1, "the ON arm silently fell back — a vacuous parity"
    # Review LOW-1 hardening: the call spy proves the FFI was INVOKED; this
    # proves its result was SERVED — a called-then-fell-back arm (a
    # kernel_refused after the call) would compare Python-to-Python.
    fell_back = [e for e in on_logs if e.get("event") == "rust_stage1a.fallback"]
    assert not fell_back, f"the ON arm was called yet FELL BACK — vacuous parity: {fell_back}"
    # Refuter RF1 belt: a pending-burn-in mismatch ships the ORACLE list and
    # logs kernel_autoreverted (not fallback) — assert it too, so the harness
    # stays non-vacuous even if the remaining=0 reset above ever drifts.
    reverted = [e for e in on_logs if e.get("event") == "rust_stage1a.kernel_autoreverted"]
    assert not reverted, f"the ON arm AUTOREVERTED — oracle-substituted parity: {reverted}"
    return off, on


@pytest.fixture(autouse=True)
def _fresh_seam_state() -> Iterator[None]:
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    yield
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()


# ────────────────────────────────────────────────────────────────────────
# The dual-arm equality property
# ────────────────────────────────────────────────────────────────────────


@given(spec=_stream())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_flag_on_equals_flag_off_element_for_element(
    spec: tuple[list[CandleBar], dict[str, Any]],
) -> None:
    bars, kwargs = spec
    off, on = _dual_arm(bars, **kwargs)
    assert on == off
    assert len(on) == len(bars)


def test_hlc3_snap_trap_counter_example_is_parity_pinned() -> None:
    # 0.004 / 0.004 / 0.007: HLC3 = 0.005 → ROUND_HALF_UP → 1 paisa, while
    # the individually snapped H/L/C read 0/0/1 paisa (mean of snapped = 0.33
    # → 0). The prepass MUST compute the typical from the RAW Decimals — a
    # snapped-columns re-derivation would diverge here.
    d = _TRADING_DAYS[0]
    bars = [
        _bar_from(d, 0, "0.004", "0.007", "0.004", "0.007", 100),
        _bar_from(d, 1, "0.004", "0.004", "0.004", "0.004", 100),
    ]
    off, on = _dual_arm(bars)
    assert on == off


def test_single_day_contracts_roll_every_boundary_parity() -> None:
    # Consecutive rolls: every used day its own contract (roll on the first
    # bar of every day after day 1).
    days = _TRADING_DAYS[:3]
    bars = [
        _bar_from(d, m, "100.00", "110.00", "90.00", "105.00", 100) for d in days for m in range(3)
    ]
    schedule = tuple(d.toordinal() for d in days)
    off, on = _dual_arm(bars, roll_schedule=schedule)
    assert on == off


def test_backwards_dated_bars_start_new_days_in_both_arms() -> None:
    # The day boundary is `!=`: A → B → A re-rolls the day each time.
    a, b = _TRADING_DAYS[0], _TRADING_DAYS[1]
    bars = [
        _bar_from(a, 0, "100.00", "101.00", "99.00", "100.50", 100),
        _bar_from(b, 0, "102.00", "103.00", "101.00", "102.50", 100),
        _bar_from(a, 1, "104.00", "105.00", "103.00", "104.50", 100),
    ]
    off, on = _dual_arm(bars)
    assert on == off


# ────────────────────────────────────────────────────────────────────────
# Flag-ON suffix independence + roll equivalence
# ────────────────────────────────────────────────────────────────────────


@given(spec=_stream(), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_flag_on_prefix_truncation_is_suffix_independent(
    spec: tuple[list[CandleBar], dict[str, Any]], data: st.DataObject
) -> None:
    bars, kwargs = spec
    _, full = _dual_arm(bars, **kwargs)
    k = data.draw(st.integers(min_value=1, max_value=len(bars)))
    _, prefix = _dual_arm(bars[:k], **kwargs)
    assert prefix == full[:k]


@given(spec=_stream(), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_flag_on_suffix_mutation_never_moves_earlier_masks(
    spec: tuple[list[CandleBar], dict[str, Any]], data: st.DataObject
) -> None:
    bars, kwargs = spec
    _, full = _dual_arm(bars, **kwargs)
    k = data.draw(st.integers(min_value=1, max_value=len(bars)))
    mutated = list(bars)
    for j in range(k, len(bars)):
        a = data.draw(_PRICE_TEXT)
        b = data.draw(_PRICE_TEXT)
        o, h, lo, c = _ordered_ohlc(a, b)
        mutated[j] = CandleBar(
            timestamp=bars[j].timestamp,
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(lo),
            close=Decimal(c),
            volume=bars[j].volume,
            open_interest=None,
        )
    _, moved = _dual_arm(mutated, **kwargs)
    assert moved[:k] == full[:k]


def test_flag_on_roll_equivalence_post_roll_equals_fresh_build() -> None:
    # The #1702 roll-segmentation law under the flag: the post-roll segment's
    # masks equal a FRESH flag-ON build over that segment alone.
    from tests.golden.test_engine_futures_roll_golden import (
        _HLC_SCRIPT,  # pyright: ignore[reportPrivateUsage]
        _ROLL_SCHEDULE,  # pyright: ignore[reportPrivateUsage]
        _stitched_bars,  # pyright: ignore[reportPrivateUsage]
    )

    bars = _stitched_bars()
    n_pre = len(_HLC_SCRIPT)
    _, full = _dual_arm(bars, roll_schedule=_ROLL_SCHEDULE)
    _, fresh = _dual_arm(bars[n_pre:], roll_schedule=_ROLL_SCHEDULE)
    assert full[n_pre:] == fresh


# ────────────────────────────────────────────────────────────────────────
# G-6 exception parity
# ────────────────────────────────────────────────────────────────────────


def _arm_exception(bars: list[CandleBar], *, flag: bool, **kwargs: Any) -> BaseException:
    reset_self_check_for_tests()
    reset_observability_memo_for_tests()
    prior_env = os.environ.get(STAGE1A_RUST_ENV)
    try:
        if flag:
            os.environ[STAGE1A_RUST_ENV] = "1"
        else:
            # Explicit "0" (kill switch), NOT pop: an UNSET flag resolves
            # default-ON under the wave, so popping would run the "OFF" arm
            # in Rust — a vacuous same-arm exception compare.
            os.environ[STAGE1A_RUST_ENV] = "0"
        with pytest.raises((ValueError, OverflowError)) as excinfo:
            build_bar_bits(bars, **kwargs)
        return excinfo.value
    finally:
        if prior_env is None:  # pragma: no cover — the conftest belt presets the flag, so prior_env is always "0"
            os.environ.pop(STAGE1A_RUST_ENV, None)
        else:
            os.environ[STAGE1A_RUST_ENV] = prior_env


def test_envelope_violating_price_raises_identically_on_and_off() -> None:
    # ₹2·10¹² → 2·10¹⁴ paisa: beyond MAX_BAR_PAISA. The ON arm's boundary
    # refuses → falls back → the ORACLE raises today's exact error.
    d = _TRADING_DAYS[0]
    huge = "2000000000000"
    bars = [
        _bar_from(d, 0, "100.00", "101.00", "99.00", "100.00", 100),
        _bar_from(d, 1, huge, huge, huge, huge, 100),
    ]
    off = _arm_exception(bars, flag=False)
    on = _arm_exception(bars, flag=True)
    assert type(off) is type(on)
    assert str(off) == str(on)


def test_beyond_schedule_bar_raises_identically_on_and_off() -> None:
    d1, d2 = _TRADING_DAYS[0], _TRADING_DAYS[1]
    bars = [
        _bar_from(d1, 0, "100.00", "101.00", "99.00", "100.00", 100),
        _bar_from(d2, 0, "100.00", "101.00", "99.00", "100.00", 100),
    ]
    schedule = (d1.toordinal(),)
    off = _arm_exception(bars, flag=False, roll_schedule=schedule)
    on = _arm_exception(bars, flag=True, roll_schedule=schedule)
    assert type(off) is type(on) is ValueError
    assert str(off) == str(on)


def test_distinct_bps_triple_parity_with_anti_swap_teeth() -> None:
    """RF-γ round-4 NIT + RF-P round-5 C1: every committed dual-arm corpus
    drew ONE bps for all three proximity channels — this pins a DISTINCT
    (pivot, pdh_pdl, fib) triple through the FFI, with anti-swap teeth on
    ALL THREE pairwise axes: on this feed every pairwise tolerance swap
    MOVES the masks, so no cross-wired bps channel can pass the parity
    vacuously (RF-P proved the original two-bar feed left the pdh_pdl↔fib
    axis undistinguished — its swap survived the whole battery).

    Feed law: day-1 OHLC (110/90/100) puts P at 10_000 paisa, PDH at 11_000
    and the bearish retrace-38 at 10_236; day-2 closes 100.20 (20 paisa off
    P — near under 41 or 23 bps, far under 7), 102.16 (20 paisa off
    retrace-38, near only under 41) and 109.70 (30 paisa off PDH — near
    under 41 bps [threshold 44 — the close-based floor 10_970·41 div 10_000], far
    under 23 [25] and 7 [7]).
    """
    d1, d2 = _TRADING_DAYS[0], _TRADING_DAYS[1]
    bars = [
        _bar_from(d1, 0, "100.00", "110.00", "90.00", "100.00", 100),
        _bar_from(d2, 0, "100.10", "100.30", "99.90", "100.20", 100),
        _bar_from(d2, 1, "102.10", "102.30", "101.90", "102.16", 100),
        _bar_from(d2, 2, "109.60", "109.90", "109.40", "109.70", 100),
    ]
    distinct = ProximityParams(
        pivot_proximity_bps=7, pdh_pdl_proximity_bps=23, fib_proximity_bps=41
    )
    off, on = _dual_arm(bars, proximity=distinct)
    assert on == off
    # Anti-swap teeth (flag-OFF oracle only — the parity above carries the
    # FFI): EVERY pairwise tolerance swap changes which near bits fire.
    pivot_fib = ProximityParams(
        pivot_proximity_bps=41, pdh_pdl_proximity_bps=23, fib_proximity_bps=7
    )
    assert build_bar_bits(bars, proximity=pivot_fib) != off, (
        "the feed cannot distinguish the pivot/fib tolerances — vacuous triple"
    )
    pdh_fib = ProximityParams(pivot_proximity_bps=7, pdh_pdl_proximity_bps=41, fib_proximity_bps=23)
    assert build_bar_bits(bars, proximity=pdh_fib) != off, (
        "the feed cannot distinguish the pdh_pdl/fib tolerances — the RF-P C1 axis"
    )
    pivot_pdh = ProximityParams(
        pivot_proximity_bps=23, pdh_pdl_proximity_bps=7, fib_proximity_bps=41
    )
    assert build_bar_bits(bars, proximity=pivot_pdh) != off, (
        "the feed cannot distinguish the pivot/pdh_pdl tolerances — vacuous triple"
    )
