"""Property: the dual-feed Rust trade-sim kernel ≡ the Python walker, BIT-EXACT.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563) on the DEC-FILL-EXEC-GRID-1M-001
1-minute execution grid: over hypothesis-generated TWO-SESSION dual-stream
windows (contiguous 1m bars over the real NSE sessions 2024-01-02/03,
subsampled to create holes; decision bars either the 1m stream itself
(``tf_minutes == 1``) or 3-minute bucket-START aggregates; windows starting at
15:10 IST cross the forced-exit boundary; the last decision bar of session 1
always carries a firing signal — the session-boundary / last-bar scenario),
the kernel-served :class:`brutex.engine.rust_trade_sim.ResidentTradeSim` must
emit exactly the same ``(trades, exit_indices)`` pairs as (a) the SAME seam
forced onto its pure-Python oracle arm (``fno_math`` BLOCKED via a poisoned
``sys.modules`` entry — ``import fno_math`` raises ImportError, so the seam
constructs on the fallback) and (b) direct
:func:`brutex.engine.trade_sim.simulate_trades` calls — for BOTH compiled
kernel widths: the u64 ``fno_math.ResidentSweepSim`` (bar_bits < 2**60) and
the u128 ``ResidentSweepSimU128`` (>64-bit bar_bits/masks — resident-wide AND
lazily-promoted arms). Trades are compared FIELD-BY-FIELD (pnl_paisa,
mfe_paisa, mae_paisa, holding_bars, entry_ts, exit_ts, direction) and the
exit indices are additionally resolved against the EXECUTION stream (design
§2.6 — 1-minute indices). This is the look-ahead-free, determinism, and
bit-exactness invariant for the dual-feed kernel (Golden Rule #4).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.config.rust_strict import RUST_STRICT_ENV
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import U64_MASK, mask_to_entry_signals
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.trade_sim import (
    STOP_LOSS_REASON_INDEX,
    simulate_trades,
    simulate_trades_with_exit_counts,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

# The decision-bar aggregation law is SHARED with the unit suite (Golden Rule
# #11 COMMON — one bucket-START labelling helper, never a second copy).
from tests.unit.test_engine_rust_trade_sim import _aggregate_decision

pytestmark = pytest.mark.property

_SESSION1 = date(2024, 1, 2)  # regular NSE Tuesday
_SESSION2 = date(2024, 1, 3)  # the next NSE session (Wednesday)
_OPEN_MINUTE = 9 * 60 + 15  # 09:15 IST — the session-open minute
_LAST_MINUTE = 15 * 60 + 29  # 15:29 IST — the session's last stored minute
# Start minutes: 09:15 (deep in-session) / 15:10 (the window quickly crosses
# the 15:20 forced-exit boundary, so forced closes + forced-bar entry gates
# are exercised).
_START_MINUTES = (_OPEN_MINUTE, 15 * 60 + 10)

# Vocabulary positions for bar_bits/masks: the u64 corpus stays inside
# [0, 2**60) (today's production width); the wide corpus straddles the 63/64
# limb boundary and reaches the top u128 bit.
_U64_POSITIONS = (0, 1, 5, 33, 59)
_WIDE_POSITIONS = (0, 1, 59, 63, 64, 100, 127)

# The seven ClosedTrade fields the parity contract compares one by one.
_TRADE_FIELDS = (
    "pnl_paisa",
    "mfe_paisa",
    "mae_paisa",
    "holding_bars",
    "entry_ts",
    "exit_ts",
    "direction",
)

_Case = tuple[
    list[CandleBar],  # decision bars
    list[CandleBar],  # 1m execution bars
    list[int],  # bar_bits (decision-length)
    list[int],  # masks
    Side,
    int | None,  # sl paisa
    int | None,  # tp paisa
    int,  # tf_minutes
]


def _bar_at(
    day: date, minute_of_day: int, low_c: int, high_c: int, open_c: int, close_c: int
) -> CandleBar:
    """A 1-minute bar at IST ``minute_of_day`` on *day* (prices in cents/paisa-per-unit)."""
    hh, mm = divmod(minute_of_day, 60)
    ts = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_c) / Decimal(100),
        high=Decimal(high_c) / Decimal(100),
        low=Decimal(low_c) / Decimal(100),
        close=Decimal(close_c) / Decimal(100),
        volume=0,
        open_interest=None,
    )


@st.composite
def _session_coords(draw: st.DrawFn) -> list[tuple[date, int]]:
    """(day, minute) coordinates over one or two real sessions, holes included."""
    coords: list[tuple[date, int]] = []
    days = [_SESSION1, _SESSION2] if draw(st.booleans()) else [_SESSION1]
    for day in days:
        start = draw(st.sampled_from(_START_MINUTES))
        n = draw(st.integers(min_value=3, max_value=25))
        minutes = list(range(start, min(start + n, _LAST_MINUTE + 1)))
        # Holes: drop a strict subset of the minutes, keeping >= 2 stored bars
        # per session (the subsampled-lake shape the slide law exists for).
        dropped = draw(st.sets(st.sampled_from(minutes), max_size=max(0, len(minutes) - 2)))
        coords.extend((day, m) for m in minutes if m not in dropped)
    return coords


@st.composite
def _dual_stream_case(draw: st.DrawFn, *, wide: bool) -> _Case:
    """One generated dual-stream window + signal vocabulary + trade params.

    ``wide=False`` keeps every bar_bits/mask value strictly below ``2**60``
    (the u64 kernel domain). ``wide=True`` guarantees at least one >64-bit
    value: either the bar_bits widen (the u128 kernel is RESIDENT from
    construction) or a >64-bit NON-firing mask joins the batch over u64-fit
    bar_bits (the LAZY-promotion arm — the whole batch routes through the
    promoted u128 sibling).
    """
    coords = draw(_session_coords())
    exec_bars = [
        _bar_at(
            day,
            minute,
            low_c := draw(st.integers(min_value=9000, max_value=11000)),
            high_c := low_c + draw(st.integers(min_value=0, max_value=1500)),
            draw(st.integers(min_value=low_c, max_value=high_c)),
            draw(st.integers(min_value=low_c, max_value=high_c)),
        )
        for day, minute in coords
    ]
    tf_minutes = draw(st.sampled_from([1, 3]))
    decision = _aggregate_decision(exec_bars, tf_minutes)
    widen_bars = draw(st.booleans()) if wide else False
    # bar_bits positions: only a RESIDENT-wide window may carry >63-bit words
    # (the promotion arm needs u64-fit bar_bits by construction).
    bit_positions = (
        _WIDE_POSITIONS
        if widen_bars
        else tuple(p for p in (_WIDE_POSITIONS if wide else _U64_POSITIONS) if p <= 63)
    )
    fire_mask = sum(
        1 << p for p in draw(st.sets(st.sampled_from(bit_positions), min_size=1, max_size=3))
    )
    if widen_bars:
        fire_mask |= 1 << 100  # the firing mask itself is >64-bit
    masks: list[int] = [fire_mask]
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        positions = draw(st.sets(st.sampled_from(bit_positions), min_size=1, max_size=3))
        masks.append(sum(1 << p for p in positions))
    if wide and not widen_bars:
        # The lazy-promotion driver: a >64-bit mask over the u64 window.
        masks.append((1 << 100) | (1 << 64))
    bits = [
        sum(1 << p for p in draw(st.sets(st.sampled_from(bit_positions), max_size=4)))
        for _ in decision
    ]
    # Force firing signals on a drawn subset of decision bars PLUS the last
    # decision bar of session 1 (the session-boundary / session-last-bar
    # scenario: its fill candidate is the next session → the cross-session
    # DROP, or — when session 2 is absent — the never-fills tail).
    n_dec = len(decision)
    fire_at = draw(
        st.sets(st.integers(min_value=0, max_value=n_dec - 1), min_size=1, max_size=min(4, n_dec))
    )
    session1_last = max(
        (i for i, b in enumerate(decision) if b.timestamp.astimezone(IST).date() == _SESSION1),
        default=None,
    )
    if session1_last is not None:
        fire_at.add(session1_last)
    for i in fire_at:
        bits[i] |= fire_mask
    direction = draw(st.sampled_from([Side.BUY, Side.SELL]))
    sl = draw(st.none() | st.integers(min_value=1, max_value=3000))
    tp = draw(st.none() | st.integers(min_value=1, max_value=3000))
    return decision, exec_bars, bits, masks, direction, sl, tp, tf_minutes


def _python_oracle_sim(
    decision: list[CandleBar], bits: list[int], tf_minutes: int, exec_bars: list[CandleBar]
) -> ResidentTradeSim:
    """Build the seam with ``fno_math`` BLOCKED → the pure-Python oracle arm.

    ``sys.modules["fno_math"] = None`` makes ``import fno_math`` raise
    ImportError inside the constructor (the wheel-absent posture), so the
    returned instance provably serves every batch through
    :class:`brutex.engine.trade_sim.TradeSimWalker`. The inline try/finally
    (not the ``monkeypatch`` fixture) keeps hypothesis' function-scoped
    fixture health check satisfied; ``capture_logs`` swallows the expected
    per-example ``wheel_missing`` WARNING.

    ``BRUTEX_RUST_STRICT`` is pinned off for exactly this construction. The
    blocked wheel here is not a production fall-back -- it is how the test
    MINTS the pure-Python comparand the parity law measures Rust against, so
    DEC-RUST-STRICT-NO-FALLBACK-001's refusal would remove the very reference
    the law needs. The window is the constructor call and nothing else: the
    strict default is restored in the same ``finally`` that restores
    ``sys.modules``, so every other call in this module -- the Rust arm under
    test included -- still runs strict-ON.
    """
    sentinel: object = object()
    prior = sys.modules.get("fno_math", sentinel)
    prior_strict: object = os.environ.get(RUST_STRICT_ENV, sentinel)
    sys.modules["fno_math"] = None  # type: ignore[assignment]
    os.environ[RUST_STRICT_ENV] = "0"
    try:
        with capture_logs():
            sim = ResidentTradeSim(decision, bits, tf_minutes=tf_minutes, exec_bars_1m=exec_bars)
    finally:
        if prior is sentinel:
            sys.modules.pop("fno_math", None)
        else:
            sys.modules["fno_math"] = prior  # type: ignore[assignment]
        if isinstance(prior_strict, str):
            os.environ[RUST_STRICT_ENV] = prior_strict
        else:
            os.environ.pop(RUST_STRICT_ENV, None)
    assert sim.rust_active is False  # provably the oracle arm
    return sim


def _trade_fields(trade: ClosedTrade) -> tuple[object, ...]:
    return tuple(getattr(trade, field) for field in _TRADE_FIELDS)


def _assert_pairs_bit_equal(
    got: list[tuple[list[ClosedTrade], list[int] | None]],
    expected: list[tuple[list[ClosedTrade], list[int] | None]],
    exec_bars: list[CandleBar],
) -> None:
    """Field-by-field ClosedTrade equality + exit-index equality per mask.

    The generated execution streams are strictly increasing (unique minutes),
    so BOTH arms must emit CERTIFIED (non-``None``) exit indices, and each
    index must resolve to its trade's exit bar within the EXECUTION stream
    (design §2.6 — 1-minute indices, never decision indices).
    """
    assert len(got) == len(expected)
    for (g_trades, g_idx), (e_trades, e_idx) in zip(got, expected, strict=True):
        assert [_trade_fields(t) for t in g_trades] == [_trade_fields(t) for t in e_trades]
        assert g_idx is not None
        assert e_idx is not None
        assert g_idx == e_idx
        for trade, idx in zip(g_trades, g_idx, strict=True):
            assert exec_bars[idx].timestamp == trade.exit_ts


def _run_parity_case(case: _Case, *, expect_wide_serving: bool) -> None:
    """The shared parity body: kernel arm ≡ blocked-import oracle arm ≡ simulate_trades."""
    decision, exec_bars, bits, masks, direction, sl, tp, tf_minutes = case
    kernel_sim = ResidentTradeSim(decision, bits, tf_minutes=tf_minutes, exec_bars_1m=exec_bars)
    assert kernel_sim.rust_active is True  # a compiled kernel holds the window
    with capture_logs() as logs:
        got = kernel_sim.simulate_masks_with_exit_indices(
            masks, direction=direction, stop_loss_paisa=sl, take_profit_paisa=tp
        )
    # Anti-vacuity: Rust genuinely served the batch — no oracle fallback fired
    # (a wide batch may PROMOTE — an INFO, not a fallback).
    assert [e for e in logs if e["event"] == "rust_trade_sim.call_fallback"] == []
    if expect_wide_serving:
        # The wide corpus law: at least one >64-bit word crossed the FFI —
        # either the resident window is wide or the batch promoted.
        assert any(b > U64_MASK for b in bits) or any(m > U64_MASK for m in masks)

    oracle_sim = _python_oracle_sim(decision, bits, tf_minutes, exec_bars)
    expected = oracle_sim.simulate_masks_with_exit_indices(
        masks, direction=direction, stop_loss_paisa=sl, take_profit_paisa=tp
    )
    _assert_pairs_bit_equal(got, expected, exec_bars)

    # The seam ALSO equals direct simulate_trades calls (the one dual-feed
    # walker law, mask by mask).
    for mask, (trades, _) in zip(masks, got, strict=True):
        direct = simulate_trades(
            decision,
            mask_to_entry_signals(bits, mask),
            direction=direction,
            tf_minutes=tf_minutes,
            exec_bars_1m=exec_bars,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
        )
        assert [_trade_fields(t) for t in trades] == [_trade_fields(t) for t in direct]

    # Woven exit-reason counter parity (DEC-METRICS-LOSSRATE-FIRSTCLASS-001
    # Stage B, #1974): the kernel's per-mask exit-reason counters equal the
    # Python walker's per reason and sum to the mask's trade count — the ONE
    # guard feeding sl_hit_rate, preserved on the dual-feed 1m grid for BOTH
    # widths (never a silently wrong loss-rate metric). Rust genuinely serves
    # the counted batch (no call_fallback; a wide batch PROMOTES — INFO only),
    # and the Python arm walks the same dual-feed law via
    # simulate_trades_with_exit_counts.
    with capture_logs() as count_logs:
        triples = kernel_sim.simulate_masks_with_exit_counts(
            masks, direction=direction, stop_loss_paisa=sl, take_profit_paisa=tp
        )
    assert [e for e in count_logs if e["event"] == "rust_trade_sim.call_fallback"] == []
    for mask, (trades, _idx, counts) in zip(masks, triples, strict=True):
        _py_trades, _py_idx, py_counts = simulate_trades_with_exit_counts(
            decision,
            mask_to_entry_signals(bits, mask),
            direction=direction,
            tf_minutes=tf_minutes,
            exec_bars_1m=exec_bars,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
        )
        assert counts == py_counts
        assert sum(counts) == len(trades)
        # Under the ONE worst-case law (DEC-FILL-WORST-CASE-ONLY-001) an SL
        # fill is adverse-clamped at/beyond the level, so every stop_loss
        # close is a loss: sl_count <= losers.
        losers = sum(1 for t in trades if t.pnl_paisa < 0)
        assert counts[STOP_LOSS_REASON_INDEX] <= losers


@given(case=_dual_stream_case(wide=False))  # pyright: ignore[reportCallIssue]
@settings(max_examples=25, deadline=None)
def test_u64_kernel_dual_stream_parity(case: _Case) -> None:
    """u64 domain (bar_bits < 2**60): kernel ≡ oracle ≡ simulate_trades, bit-exact."""
    _, _, bits, _, _, _, _, _ = case
    assert all(0 <= b < 1 << 60 for b in bits)  # the generator's u64-domain law
    _run_parity_case(case, expect_wide_serving=False)


@given(case=_dual_stream_case(wide=True))  # pyright: ignore[reportCallIssue]
@settings(max_examples=25, deadline=None)
def test_u128_kernel_dual_stream_parity(case: _Case) -> None:
    """>64-bit domain: the u128 kernel (resident OR lazily promoted) is bit-exact."""
    _run_parity_case(case, expect_wide_serving=True)
