"""Exponential Moving Average — EMA 20 and EMA 200.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 2 (Indicator Crossover):
    "EMA 20 close, EMA 200 close"

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
    # internally instantiates an EMAState and feeds it in order

Both paths exercise the same `_step()` helper for the recursive update.
Same input → same output, regardless of which entry point.

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
- In post-seed: 2 multiplications + 1 addition = 3 ops total
- L1-resident state: 3 float64s + 1 int = 32 bytes

State serialization (for live → backtest replay): 4 fields, ~40 bytes
JSON. Supports checkpoint/restore for resumable sweeps per
`docs/TRACK1_BACKTEST_SPEC.md` §Phased Build Plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


def alpha(period: int) -> float:
    """Smoothing factor for an N-period EMA: α = 2 / (N + 1).

    Standard TradingView / AmiBroker convention.
    """
    if period < 1:
        raise ValueError(f"EMA period must be >= 1, got {period}")
    return 2.0 / (period + 1)


@dataclass
class EMAState:
    """Streaming-mode EMA state object.

    Hold once, call `.update()` per new bar. Works identically in:
    - Live TickVault loop (tick → bar close → update)
    - Backtest replay (historical bar close → update)
    - Resumable sweeps (serialize/restore between checkpoints)

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
            # Seed bar: EMA = SMA of values[0..period-1]
            self.seed_sum += close
            self.value = self.seed_sum / self.period
            return self.value

        # Post-seed: recursive update (O(1)). Value is guaranteed
        # non-None here by the above branches; narrow via local copy
        # so the type checker sees float, not float | None.
        prior = self.value
        if prior is None:  # pragma: no cover — invariant from branches above
            raise AssertionError("EMA state corrupted: post-seed value is None")
        self.value = self.alpha * close + (1.0 - self.alpha) * prior
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

    Wraps `EMAState` so backtest replay produces bit-identical output to
    a live `EMAState.update()` loop.

    Args:
        values: Sequence of float prices (close values).
        period: EMA period (e.g., 20 or 200).

    Returns:
        List of length len(values), with the first (period − 1) entries
        as None (warm-up) and the rest as float EMA values.
    """
    if period < 1:
        raise ValueError(f"EMA period must be >= 1, got {period}")
    state = EMAState.new(period)
    return [state.update(v) for v in values]


def latest_ema(values: Sequence[float], period: int) -> float | None:
    """Return only the latest (most recent) EMA value, or None if warm-up
    incomplete.

    Convenience wrapper; uses the streaming EMAState under the hood.
    """
    if not values:
        return None
    state = EMAState.new(period)
    last: float | None = None
    for v in values:
        last = state.update(v)
    return last
