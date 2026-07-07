"""Property: the Rust trade-sim seam is byte-identical to the Python oracle.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). Over hypothesis-generated bar
feeds (valid OHLC, varied start times so some windows cross the 15:20 forced
exit), masks, SL/TP distances, directions and fill policies, the resident Rust
kernel :class:`brutex.engine.rust_trade_sim.ResidentTradeSim` must emit exactly
the same :class:`~brutex.metrics.types.ClosedTrade` lists as
:func:`brutex.engine.trade_sim.simulate_trades` (Golden Rule #4). This is the
look-ahead-free, determinism, and bit-exactness invariant for the kernel.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.trade_sim import FillPolicy, LevelFill, SameBarTie, simulate_trades
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

pytestmark = pytest.mark.property

_DAY = date(2024, 1, 2)
_DAY2 = date(2024, 1, 3)  # the next NSE session — two-session feeds (R2-OC-2)
# Start IST times: 09:15 (no forced bars within ~1h) + 15:16 (windows quickly
# cross the 15:20 forced exit), so the forced-exit branch is exercised too.
_START_TIMES = [(9, 15), (15, 16)]


@st.composite
def _feed_and_params(
    draw: st.DrawFn,
) -> tuple[list[CandleBar], list[int], Side, int | None, int | None, FillPolicy]:
    """Generate (bars, masks, direction, sl_paisa, tp_paisa, policy).

    Bars at index ``>= split`` roll to the NEXT session (day-2, restarting at
    09:15 IST), so the corpus spans trading-day boundaries and the kernel's
    cross-session pending-entry drop (R2-OC-2) is parity-checked too;
    ``split in {0, n}`` degenerates to the original single-session feed.
    """
    n = draw(st.integers(min_value=1, max_value=30))
    start_h, start_m = draw(st.sampled_from(_START_TIMES))
    split = draw(st.integers(min_value=0, max_value=n))
    bars: list[CandleBar] = []
    for i in range(n):
        low = draw(st.integers(min_value=9000, max_value=11000))
        high = low + draw(st.integers(min_value=0, max_value=2000))
        o = draw(st.integers(min_value=low, max_value=high))
        c = draw(st.integers(min_value=low, max_value=high))
        # IST wall-clock marching by one minute; stored UTC (bars are UTC).
        # Bars from ``split`` onward step to day-2, restarting at 09:15 IST.
        if i < split:
            day = _DAY
            minute_total = start_h * 60 + start_m + i
        else:
            day = _DAY2
            minute_total = 9 * 60 + 15 + (i - split)
        hh, mm = divmod(minute_total, 60)
        ts = datetime(day.year, day.month, day.day, hh % 24, mm, tzinfo=IST).astimezone(UTC)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(o) / Decimal(100),
                high=Decimal(high) / Decimal(100),
                low=Decimal(low) / Decimal(100),
                close=Decimal(c) / Decimal(100),
                volume=0,
                open_interest=None,
            )
        )
    direction = draw(st.sampled_from([Side.BUY, Side.SELL]))
    sl = draw(st.none() | st.integers(min_value=1, max_value=4000))
    tp = draw(st.none() | st.integers(min_value=1, max_value=4000))
    policy = FillPolicy(
        stop_loss=draw(st.sampled_from([LevelFill.AT_LEVEL, LevelFill.NEXT_OPEN])),
        take_profit=draw(st.sampled_from([LevelFill.AT_LEVEL, LevelFill.NEXT_OPEN])),
        same_bar=draw(st.sampled_from([SameBarTie.SL_FIRST, SameBarTie.TP_FIRST])),
    )
    # Masks drawn from a small bit space so they actually fire on the feed.
    masks = draw(
        st.lists(st.integers(min_value=0, max_value=63), min_size=1, max_size=6, unique=True)
    )
    return bars, masks, direction, sl, tp, policy


@given(case=_feed_and_params())
@settings(max_examples=200, deadline=None)
def test_rust_seam_equals_python_oracle(
    case: tuple[list[CandleBar], list[int], Side, int | None, int | None, FillPolicy],
) -> None:
    bars, masks, direction, sl, tp, policy = case
    bar_bits = build_bar_bits(bars)
    sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
    got = sim.simulate_masks(
        masks, direction=direction, stop_loss_paisa=sl, take_profit_paisa=tp, policy=policy
    )
    expected = [
        simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, m),
            direction=direction,
            tf_minutes=1,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
            policy=policy,
        )
        for m in masks
    ]
    assert got == expected


# ── u128 domain parity (DEC-TRADE-SIM-U128-KERNEL-001) ────────────────────────
#
# The hostile >64-bit corpus: masks STRADDLING bit 63/64, all-zero-hi (u64-fit)
# masks riding the WIDE path, the all-ones _U128_MASK, the top bit 127, and
# random combinations — over bar_bits that are either widened (the u128 kernel
# is resident) or u64-fit (the wide batch PROMOTES the u128 sibling). Every
# example asserts byte-identity against the pure-Python simulate_trades oracle
# AND that Rust genuinely served the batch (no call-fallback events — the
# anti-vacuity teeth: an oracle-served run would trivially "match" itself).

# Bit positions spanning both limbs: low bits that can fire on real Stage-1A
# bar_bits, the 63/64 straddle pair, a mid-high bit, and the top bit 127.
_WIDE_POSITIONS: list[int] = [0, 1, 62, 63, 64, 65, 100, 127]
_U128_ALL_ONES = (1 << 128) - 1


@st.composite
def _wide_feed_and_params(
    draw: st.DrawFn,
) -> tuple[list[CandleBar], list[int], list[int], Side, int | None, int | None, FillPolicy, bool]:
    """Generate (bars, bar_bits, masks, direction, sl, tp, policy, widen_bars).

    ``widen_bars`` selects the resident-kernel width: True ORs drawn
    >64-bit positions into the Stage-1A bar_bits (the u128 kernel serves from
    construction); False keeps bar_bits u64-fit so a wide mask exercises the
    LAZY promotion path instead. Masks always include at least one >64-bit
    value plus the special corpus (straddler, all-zero-hi, all-ones).
    """
    n = draw(st.integers(min_value=2, max_value=20))
    start_h, start_m = draw(st.sampled_from(_START_TIMES))
    bars: list[CandleBar] = []
    for i in range(n):
        low = draw(st.integers(min_value=9000, max_value=11000))
        high = low + draw(st.integers(min_value=0, max_value=2000))
        o = draw(st.integers(min_value=low, max_value=high))
        c = draw(st.integers(min_value=low, max_value=high))
        minute_total = start_h * 60 + start_m + i
        hh, mm = divmod(minute_total, 60)
        ts = datetime(_DAY.year, _DAY.month, _DAY.day, hh % 24, mm, tzinfo=IST).astimezone(UTC)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(o) / Decimal(100),
                high=Decimal(high) / Decimal(100),
                low=Decimal(low) / Decimal(100),
                close=Decimal(c) / Decimal(100),
                volume=0,
                open_interest=None,
            )
        )
    widen_bars = draw(st.booleans())
    bar_bits = build_bar_bits(bars)
    if widen_bars:
        for i in range(n):
            extra = draw(st.sets(st.sampled_from(_WIDE_POSITIONS), min_size=0, max_size=4))
            for pos in extra:
                bar_bits[i] |= 1 << pos
    # Random masks from the position pool (some fire, some never), plus the
    # required specials: a 63/64 straddler, an all-zero-hi (u64-fit) mask that
    # rides the SAME batch, and the all-ones u128 word.
    masks: list[int] = [
        (1 << 63) | (1 << 64),  # straddles the limb boundary
        (1 << 63) | 1,  # all-zero hi — u64-fit, served on the wide path
        _U128_ALL_ONES,  # all-ones
    ]
    n_random = draw(st.integers(min_value=1, max_value=4))
    for _ in range(n_random):
        positions = draw(st.sets(st.sampled_from(_WIDE_POSITIONS), min_size=1, max_size=5))
        masks.append(sum(1 << pos for pos in positions))
    direction = draw(st.sampled_from([Side.BUY, Side.SELL]))
    sl = draw(st.none() | st.integers(min_value=1, max_value=4000))
    tp = draw(st.none() | st.integers(min_value=1, max_value=4000))
    policy = FillPolicy(
        stop_loss=draw(st.sampled_from([LevelFill.AT_LEVEL, LevelFill.NEXT_OPEN])),
        take_profit=draw(st.sampled_from([LevelFill.AT_LEVEL, LevelFill.NEXT_OPEN])),
        same_bar=draw(st.sampled_from([SameBarTie.SL_FIRST, SameBarTie.TP_FIRST])),
    )
    return bars, bar_bits, masks, direction, sl, tp, policy, widen_bars


# The pre-existing `@given(case=_feed_and_params())` above carries the SAME
# hypothesis-composite reportCallIssue finding (the local-baseline class);
# this new instance is ignore-tagged so the pyright error count stays at
# the pre-change baseline (0 new diagnostics).
@given(case=_wide_feed_and_params())  # pyright: ignore[reportCallIssue]
@settings(max_examples=150, deadline=None)
def test_rust_u128_seam_equals_python_oracle(
    case: tuple[
        list[CandleBar], list[int], list[int], Side, int | None, int | None, FillPolicy, bool
    ],
) -> None:
    """>64-bit masks/bar_bits: the Rust u128 path is bit-identical to the oracle."""
    bars, bar_bits, masks, direction, sl, tp, policy, _widen_bars = case
    sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
    assert sim.rust_active is True  # a compiled kernel holds the window
    with capture_logs() as logs:
        got = sim.simulate_masks(
            masks, direction=direction, stop_loss_paisa=sl, take_profit_paisa=tp, policy=policy
        )
    # Anti-vacuity: Rust served the batch — no oracle fallback fired.
    assert [e for e in logs if e["event"] == "rust_trade_sim.call_fallback"] == []
    expected = [
        simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, m),
            direction=direction,
            tf_minutes=1,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
            policy=policy,
        )
        for m in masks
    ]
    assert got == expected
