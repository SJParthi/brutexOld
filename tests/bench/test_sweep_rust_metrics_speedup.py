"""Split speedup gate for the flag-gated Rust sweep-metrics tail.

DEC-SWEEP-RUST-METRICS-001. The reviewer-mandated DECOMPOSED ratio bench: the
profiled fixture win had two distinct sources, and folding them into one
number would be an illusion (Golden Rule #10) —

1. **Zero-trade masks** (the DOMINANT share of the min_hits=0 profile) are
   served by the O(1) analytic flat-curve shortcut — pure Python, NO Rust.
   Sandbox measured ~1680× vs the OFF-arm O(N_bars) scan; gated at ≥ 20×.
2. **Traded masks** are served by the fused Rust equity-fold + drawdown scan
   plus the curve-less ``compute_metrics_from_scalars`` (the residual
   ``ts_to_index`` dict build + moment/percentile Python stays). Sandbox
   measured ~7.3× on 200 trades over 20k bars; gated at ≥ 1.5× (generous
   shared-CI noise headroom below the measurement).

HONEST ENVELOPE: sandbox ratios ONLY (this is NOT the §8 M4 Pro — no
absolute-throughput claim, and the §8 sweep SLA is NOT claimed met by this
change); a constant-factor win — the per-tuple tail stays O(kept · N_bars).
Both arms are asserted result-EQUAL first, so a diverging fast arm can never
pass as "fast".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.rust_metrics import (
    SWEEP_RUST_METRICS_ENV,
    reset_observability_memo_for_tests,
)
from brutex.engine.sweep_driver import metrics_from_trades
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_OPEN_UTC = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)

_N_BARS = 20_000

#: Conservative shared-CI floors, far below the sandbox measurements
#: (~1680× / ~7.3×) so noise cannot flake the gate while an
#: order-of-magnitude regression still fails loudly.
_ZERO_TRADE_FLOOR = 20.0
_TRADED_FLOOR = 1.5


def _bars() -> list[CandleBar]:
    price = Decimal("100.00")
    return [
        CandleBar(
            timestamp=_OPEN_UTC + timedelta(minutes=i),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0,
            open_interest=None,
        )
        for i in range(_N_BARS)
    ]


def _trades(bars: list[CandleBar], n: int = 200) -> list[ClosedTrade]:
    out: list[ClosedTrade] = []
    for k in range(n):
        i = k * 97 % _N_BARS
        pnl = 50 if k % 3 else -80
        out.append(
            ClosedTrade(
                pnl_paisa=pnl,
                mfe_paisa=max(pnl, 0),
                mae_paisa=max(-pnl, 0),
                holding_bars=1,
                entry_ts=bars[i].timestamp,
                exit_ts=bars[i].timestamp,
                direction=Side.BUY,
            )
        )
    # exit-ordered, as the single-position walker emits them
    return sorted(out, key=lambda t: t.exit_ts)


def _median_seconds(fn: Callable[[], ResultMetrics], rounds: int) -> float:
    samples: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    samples.sort()
    return samples[len(samples) // 2]


def _speedup(
    monkeypatch: pytest.MonkeyPatch, bars: list[CandleBar], trades: list[ClosedTrade]
) -> float:
    def _run() -> ResultMetrics:
        return metrics_from_trades(bars, trades, start_equity_paisa=10_000_000, tf_minutes=1)

    monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
    off_result = _run()  # warm-up + reference
    off = _median_seconds(_run, rounds=9)
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "1")
    reset_observability_memo_for_tests()
    on_result = _run()  # warm-up + equality teeth
    assert on_result == off_result  # a diverging fast arm must never pass as "fast"
    on = _median_seconds(_run, rounds=9)
    return off / on


class TestRustMetricsSpeedupSplit:
    def test_zero_trade_shortcut_speedup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The O(1) flat-curve shortcut vs the OFF-arm O(N_bars) scan."""
        bars = _bars()
        ratio = _speedup(monkeypatch, bars, [])
        assert ratio >= _ZERO_TRADE_FLOOR, (
            f"zero-trade ON/OFF speedup {ratio:.1f}× below the {_ZERO_TRADE_FLOOR}× floor "
            f"(sandbox measured ~1680×)"
        )

    def test_traded_mask_kernel_speedup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fused Rust fold+scan + curve-less metrics vs the Python tail."""
        bars = _bars()
        ratio = _speedup(monkeypatch, bars, _trades(bars))
        assert ratio >= _TRADED_FLOOR, (
            f"traded-mask ON/OFF speedup {ratio:.2f}× below the {_TRADED_FLOOR}× floor "
            f"(sandbox measured ~7.3×)"
        )
