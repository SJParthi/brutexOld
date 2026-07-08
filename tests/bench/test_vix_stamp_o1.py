"""O(1) bench for the per-trade India-VIX stamp (DEC-VIX-STAMP-WIRE-001 Stage D).

CLAUDE.md Golden Rule #5: stamping a trade is TWO dict probes
(:meth:`~brutex.engine.reference_series.TradeReferenceStamper.stamp` →
``ReferenceSeries.close_by_ts.get`` at entry + exit) — its latency must be
independent of how many VIX bars the series holds. Two gates:

1. **Cross-size ratio gate** — per-stamp latency at a 100K-instant series vs
   a 1K-instant series, routed through
   :func:`tests.bench._o1_tiers.cache_tier_ceiling` (5× robust on the shared
   GHA runner; the DEC-O1-CACHE-TIER-001 measured cache allowance on the
   M4-Pro strict tier) because the ``N_BIG`` ``{datetime → Decimal}`` dict
   SPILLS the CPU cache into DRAM — a constant memory-tier factor, exactly
   the allowance the throughput canary's dict-lookup gate documents. A
   genuine O(n) regression (e.g. a per-stamp scan over the series) blows the
   ceiling by orders of magnitude.

2. **Converter A/B arm** — :meth:`ResidentTradeSim.simulate_masks` over ONE
   fixed window WITH vs WITHOUT a stamper (the Rust-batch post-stamp seam:
   the hoisted converter adds the two probes per trade at ``_to_trade``).
   The stamped arm must stay within :data:`_AB_CEILING` of the unstamped arm
   — the stamp is a bounded per-trade overhead (round-1 measured ~434 ns per
   trade on the M4-Pro), never a second walk over the window.

De-flake: every tier/arm is timed as the **minimum of N repeats** (the
noise-robust true-cost estimator; upward-only OS jitter never deflates a
minimum).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.stage1a import build_bar_bits
from brutex.strategies.types import Side

from tests.bench._o1_tiers import cache_tier_ceiling

#: Series sizes for the small vs big tiers of the ratio gate.
_N_SMALL = 1_000
_N_BIG = 100_000

#: Stamp calls per timed pass (each call = 2 dict probes).
_N_STAMPS = 20_000

#: Best-of-N repeats — the noise-robust true-cost estimator.
_REPEATS = 5

#: A/B ceiling for the converter arm: the stamp may not more-than-double the
#: whole batch-simulate + convert call. Its MEASURED share is ~10-15% (ledger
#: A/B 1.14x; ~313 ns/trade of two dict probes over a full kernel walk +
#: ClosedTrade construction per trade), so 2.0x is generous headroom while an
#: O(n)-per-trade regression (~6x measured when simulated) still fails loudly.
_AB_CEILING = 2.0

_T0 = datetime(2026, 1, 5, 3, 46, tzinfo=UTC)  # 09:16 IST


def _vix_stamper(n: int) -> TradeReferenceStamper:
    """A stamper over an *n*-instant 1-minute VIX series."""
    level = Decimal("14.25")
    bars = [
        CandleBar(
            timestamp=_T0 + timedelta(minutes=i),
            open=level,
            high=level,
            low=level,
            close=level,
            volume=0,
            open_interest=None,
        )
        for i in range(n)
    ]
    return TradeReferenceStamper.from_vix_bars(bars)


def _per_stamp_ns(stamper: TradeReferenceStamper, n_series: int) -> float:
    """Best-of-N minimum mean ns per ``stamp`` call (always-hit instants).

    The probe instants stride evenly across the WHOLE series key range,
    sampling ``_N_STAMPS`` distinct keys (1 in ``stride`` entries — 20% at
    the big tier, NOT every entry) so the strided sample spans the big
    tier's full hash table and the cross-cache-tier shape is honest; every
    probe HITS — the miss path carries memo bookkeeping that is
    deliberately not part of this gate's hot-path claim.
    """
    stride = max(n_series // _N_STAMPS, 1)
    pairs = [
        (
            _T0 + timedelta(minutes=(i * stride) % n_series),
            _T0 + timedelta(minutes=(i * stride + 1) % n_series),
        )
        for i in range(_N_STAMPS)
    ]
    for entry_ts, exit_ts in pairs[:64]:  # warm-up
        stamper.stamp(entry_ts, exit_ts)
    best = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter_ns()
        for entry_ts, exit_ts in pairs:
            stamper.stamp(entry_ts, exit_ts)
        best = min(best, (time.perf_counter_ns() - t0) / len(pairs))
    assert stamper.miss_count == 0  # every timed probe was a real hit
    return best


def test_per_trade_stamp_cost_is_o1_in_series_size() -> None:
    """Per-stamp latency at a 100K-bar VIX series must hold the ceiling vs 1K."""
    t_small = _per_stamp_ns(_vix_stamper(_N_SMALL), _N_SMALL)
    t_big = _per_stamp_ns(_vix_stamper(_N_BIG), _N_BIG)
    ratio = t_big / max(t_small, 1.0)

    ceiling = cache_tier_ceiling(5.0)
    print(
        f"\nVIX per-trade stamp O(1) bench (DEC-VIX-STAMP-WIRE-001) — best of {_REPEATS}:\n"
        f"  N_small = {_N_SMALL:,} instants | {t_small:.1f} ns/stamp\n"
        f"  N_big   = {_N_BIG:,} instants | {t_big:.1f} ns/stamp\n"
        f"  ratio   = {ratio:.2f}x (ceiling {ceiling}x)\n"
    )
    assert ratio < ceiling, (
        f"per-trade VIX stamp latency ratio {ratio:.2f}x ({_N_BIG} vs {_N_SMALL} "
        f"series instants) exceeds the {ceiling}x CI ceiling — the stamp may "
        f"have regressed away from O(1) dict probes. "
        f"t_small={t_small:.1f}ns, t_big={t_big:.1f}ns."
    )


def _window_bars(n: int) -> list[CandleBar]:
    """An oscillating 1-minute window that produces plenty of TP round-trips."""
    bars: list[CandleBar] = []
    price = 100.0
    for i in range(n):
        close = price + 0.6 if i % 3 else price - 0.3
        hi = max(price, close) + 0.5
        lo = min(price, close) - 0.5
        bars.append(
            CandleBar(
                timestamp=_T0 + timedelta(minutes=i),
                open=Decimal(str(round(price, 2))),
                high=Decimal(str(round(hi, 2))),
                low=Decimal(str(round(lo, 2))),
                close=Decimal(str(round(close, 2))),
                volume=100,
                open_interest=None,
            )
        )
        price = close
    return bars


def _batch_seconds(sim: ResidentTradeSim) -> tuple[float, int]:
    """Best-of-N ``simulate_masks([0])`` wall seconds + the trade count."""
    (warm_trades,) = sim.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
    best = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        sim.simulate_masks([0], direction=Side.BUY, take_profit_paisa=50)
        best = min(best, time.perf_counter() - t0)
    return best, len(warm_trades)


def test_converter_stamp_overhead_is_bounded_ab() -> None:
    """The Rust-batch converter WITH a stamper stays within the A/B ceiling.

    Same window, same masks, same kernel — the ONLY difference is the
    post-stamp at ``_to_trade``. A regression that made the stamp scan the
    series (or re-walk the window) blows the ceiling immediately.
    """
    bars = _window_bars(360)  # one IST session of 1-minute bars
    bar_bits = build_bar_bits(bars)
    stamper = TradeReferenceStamper.from_vix_bars(
        [
            CandleBar(
                timestamp=bar.timestamp,
                open=Decimal("14.25"),
                high=Decimal("14.25"),
                low=Decimal("14.25"),
                close=Decimal("14.25"),
                volume=0,
                open_interest=None,
            )
            for bar in bars
        ]
    )
    plain = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
    stamped = ResidentTradeSim(bars, bar_bits, tf_minutes=1, reference_stamper=stamper)
    # Self-honesty (review NIT): this bench gates the RUST-batch hoisted
    # converter's post-stamp seam. On a wheel-degraded env ResidentTradeSim
    # WARN-falls-back to the Python walker arm — a real A/B, but not the seam
    # this test names — so fail loudly instead of silently measuring the
    # wrong arm (the unit wiring suite hard-asserts rust_active the same way).
    assert plain.rust_active and stamped.rust_active, (
        "trading_core wheel absent/degraded — ResidentTradeSim fell back to the "
        "Python walker, so this bench would gate the fallback seam, not the "
        "hoisted Rust-batch converter it claims; rebuild the wheel "
        "(maturin develop --release) before judging."
    )

    t_plain, n_trades = _batch_seconds(plain)
    t_stamped, n_trades_stamped = _batch_seconds(stamped)
    assert n_trades == n_trades_stamped > 0
    ratio = t_stamped / max(t_plain, 1e-12)
    overhead_ns = (t_stamped - t_plain) * 1e9 / n_trades

    print(
        f"\nVIX converter A/B bench (DEC-VIX-STAMP-WIRE-001) — best of {_REPEATS}:\n"
        f"  unstamped = {t_plain * 1e6:.1f} us | stamped = {t_stamped * 1e6:.1f} us "
        f"({n_trades} trades)\n"
        f"  ratio     = {ratio:.3f}x (ceiling {_AB_CEILING}x) | "
        f"per-trade stamp overhead ~= {overhead_ns:.0f} ns\n"
    )
    assert ratio < _AB_CEILING, (
        f"stamped simulate_masks is {ratio:.2f}x the unstamped call (ceiling "
        f"{_AB_CEILING}x) — the per-trade stamp may have regressed beyond an "
        f"O(1) post-stamp. unstamped={t_plain * 1e6:.1f}us, "
        f"stamped={t_stamped * 1e6:.1f}us over {n_trades} trades."
    )
