"""Exponential Moving Average — EMA 20 and EMA 200.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 2 (Indicator Crossover):
    "EMA 20 close, EMA 200 close"

## ONE indicator, ONE source of truth (shared Rust core)

The EMA **recurrence** is the shared Rust ``trading_core`` crate
(``DEC-EMA-RUST-PORT-001``, builds on ``DEC-SHARED-CORE-001`` /
``docs/MIGRATION.md`` #441 + ``DEC-NIFTY-INDICATORS-001``). This module is now a
**thin delegating shim** (mirroring :mod:`brutex.indicators.pivots` /
:mod:`brutex.indicators.fib_levels`): the streaming :class:`EMAState` keeps its
identical public API + checkpoint state, but every arithmetic site —
``alpha = 2/(N+1)``, the SMA seed, and the per-bar step — runs in Rust, so the
backtest EMA equals the (future) live TickVault EMA **by construction**. EMA was
the last shared evaluate-time indicator still computed in Python; porting it
completes the all-Rust shared indicator core.

The math is **bit-identical** to the previous Python recurrence: it runs entirely
in IEEE-754 ``f64`` and the result stays ``float`` (the caller —
:func:`brutex.engine.stage1a._ema_to_paisa` / TickVault — snaps to paisa
separately). IEEE-754 double arithmetic with the same operation order produces
byte-identical values across Python and Rust; the Rust crate's ``ema_golden``
test + this package's golden/parity tests pin it.

## ONE indicator, TWO interfaces (online/offline parity)

This module exposes the SAME math through two interfaces so that
backtest (batch) and live TickVault (streaming) produce
**bit-identical** EMA values at every bar.

### 1. Streaming / live mode (TickVault)

    state = EMAState(period=20)
    for tick_bar in live_feed:
        ema_now = state.update(tick_bar.close)
        if ema_now is not None:
            ... # use ema_now in live strategy

### 2. Batch / backtest mode (Track 1)

    series = compute_ema(closes, period=20)  # list-in -> list-out
    # delegates to the shared Rust trading_core.compute_ema

Both paths exercise the same Rust recurrence. Same input → same output,
regardless of which entry point.

## The math (matches TradingView ta.ema())

```
α = 2 / (N + 1)                          (smoothing factor)
warm-up (bars 0..N-2):    return None
seed bar (bar N-1):       EMA = SMA(values[0..N-1])
subsequent (bar t ≥ N):   EMA_t = α × close_t + (1 − α) × EMA_(t−1)
```

Verified against operator's TradingView screenshot
(NIFTY 14-May-2026 EMA 20 = 23,749.65).

## O(1) per-step

Each `update()` call performs exactly:
- 1 increment (`bars_seen += 1`)
- 1 branch (warm-up vs seed vs post-seed)
- In post-seed: one delegated Rust step (2 multiplies + 1 add)
- L1-resident state: 3 float64s + 1 int = 32 bytes

State serialization (for live → backtest replay): 4 fields, ~40 bytes
JSON. Supports checkpoint/restore for resumable sweeps per
`docs/TRACK1_BACKTEST_SPEC.md` §Phased Build Plan.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import trading_core


def alpha(period: int) -> float:
    """Smoothing factor for an N-period EMA: α = 2 / (N + 1).

    Standard TradingView / AmiBroker convention. Delegated to the shared Rust
    ``trading_core.ema_alpha`` (single source of truth); a ``period < 1`` raises
    ``ValueError`` exactly as before.
    """
    if period < 1:
        raise ValueError(f"EMA period must be >= 1, got {period}")
    alpha_value: float = trading_core.ema_alpha(period)
    return alpha_value


@dataclass
class EMAState:
    """Streaming-mode EMA state object.

    Hold once, call `.update()` per new bar. Works identically in:
    - Live TickVault loop (tick → bar close → update)
    - Backtest replay (historical bar close → update)
    - Resumable sweeps (serialize/restore between checkpoints)

    The arithmetic (alpha, the SMA seed, the per-bar step) is delegated to the
    shared Rust ``trading_core`` crate (``DEC-EMA-RUST-PORT-001``); this object
    keeps only the branch/state-tracking shell + the checkpoint serialization, so
    the public API is byte-identical to the prior pure-Python recurrence.

    Attributes:
        period: EMA period (e.g., 20 or 200).
        alpha: Precomputed 2/(period+1).
        value: Current EMA value, or None during warm-up.
        bars_seen: Count of bars processed so far.
        seed_sum: Running sum of values during warm-up (used to seed at bar N-1).
    """

    period: int
    alpha: float
    value: float | None = None
    bars_seen: int = 0
    seed_sum: float = 0.0

    @classmethod
    def new(cls, period: int) -> EMAState:
        """Construct a fresh EMAState for the given period."""
        return cls(period=period, alpha=alpha(period))

    def update(self, close: float) -> float | None:
        """Process one bar's close. Returns the current EMA value, or None
        during warm-up.

        Args:
            close: The bar's close price (any float-unit; caller's responsibility
                   to keep units consistent).

        Returns:
            Current EMA value, or None if still warming up (bars 0..period-2).
            On bar (period-1), returns the SMA seed.
            On bars ≥ period, returns the recursive EMA value.
        """
        if not math.isfinite(close):
            raise ValueError(f"EMA close must be finite, got {close!r}")
        self.bars_seen += 1

        if self.period == 1:
            # Degenerate case where EMA period 1 collapses to identity.
            self.value = close
            return self.value

        if self.bars_seen < self.period:
            # Warm-up: accumulate for seed
            self.seed_sum += close
            return None

        if self.bars_seen == self.period:
            # Seed bar: EMA = SMA of values[0..period-1] (delegated to Rust).
            # Each ``close`` is finite (guarded above), but a run of finite-but-huge
            # closes can OVERFLOW ``seed_sum`` to ±∞; re-check the computed seed so an
            # overflowed seed raises a clean ``ValueError`` instead of returning ±∞ and
            # poisoning every subsequent bar — mirroring the Rust core's
            # ``EmaState::update`` guard (``rust/trading-core/src/indicators/ema.rs``)
            # AND the Python batch path (``compute_ema`` → ``trading_core.compute_ema``,
            # which delegates to that same Rust ``update``), so streaming == batch ==
            # Rust. Real-magnitude inputs sum far below ``f64::MAX``, so the golden seed
            # is byte-unchanged. The bare ``trading_core.ema_seed`` FFI only guards
            # ``period < 1`` (it is a pure division), so the finiteness re-check lives
            # HERE — the streaming twin of the Rust seed guard.
            self.seed_sum += close
            seed: float = trading_core.ema_seed(self.seed_sum, self.period)
            if not math.isfinite(seed):
                raise ValueError(
                    f"EMA seed overflowed to a non-finite value ({seed}); "
                    "the warm-up close sum is too large"
                )
            self.value = seed
            return self.value

        # Post-seed: recursive update (O(1), delegated to Rust). Value is
        # guaranteed non-None here by the above branches; narrow via local copy
        # so the type checker sees float, not float | None.
        prior = self.value
        if prior is None:  # pragma: no cover — invariant from branches above
            raise AssertionError("EMA state corrupted: post-seed value is None")
        self.value = trading_core.ema_step(self.alpha, close, prior)
        return self.value

    def reset(self) -> None:
        """Reset to fresh state (forget all history). Used at start-of-session
        in some strategies."""
        self.value = None
        self.bars_seen = 0
        self.seed_sum = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for checkpoint persistence."""
        return {
            "period": self.period,
            "alpha": self.alpha,
            "value": self.value,
            "bars_seen": self.bars_seen,
            "seed_sum": self.seed_sum,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EMAState:
        """Restore from checkpoint dict."""
        return cls(**d)


def compute_ema(
    values: Sequence[float],
    period: int,
) -> list[float | None]:
    """Batch interface: compute the full EMA series.

    Delegates to the shared Rust ``trading_core.compute_ema`` (the single source
    of the recurrence), so backtest replay is bit-identical to a live
    ``EMAState.update()`` loop (both run the same Rust math).

    Args:
        values: Sequence of float prices (close values).
        period: EMA period (e.g., 20 or 200).

    Returns:
        List of length len(values), with the first (period − 1) entries
        as None (warm-up) and the rest as float EMA values.

    Raises:
        ValueError: if period < 1, or if any value is non-finite (NaN / ±∞) —
            raised by the Rust core, matching the prior Python contract.
    """
    if period < 1:
        raise ValueError(f"EMA period must be >= 1, got {period}")
    result: list[float | None] = trading_core.compute_ema(list(values), period)
    return result


def latest_ema(values: Sequence[float], period: int) -> float | None:
    """Return only the latest (most recent) EMA value, or None if warm-up
    incomplete.

    Convenience wrapper over :func:`compute_ema` (the shared Rust recurrence).
    """
    if not values:
        return None
    return compute_ema(values, period)[-1]
