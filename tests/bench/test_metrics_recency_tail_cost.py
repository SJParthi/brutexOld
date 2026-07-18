"""Cost gate for the per-combo recency pass (DEC-METRICS-TRAILING-WINDOW-001).

Refuter round 1 (SLA/bench honesty): threading a ``trailing_anchor`` into the
metrics tail runs a SECOND O(trades) fold/combine (``fold_recency`` →
``combine_recency_from_folds``) per combo — a real, disclosed per-mask cost on
the sweep spine (`_iter_mask_metrics` ALWAYS threads the anchor), which no
bench measured. This gate pins that cost's SHAPE: on a fixed 2,000-trade /
2,000-bar workload the with-anchor arm must stay within a GENEROUS 4× of the
anchor-less arm.

MEASURED at introduction (sandbox 2026-07-18, median-of-15, four repeat
process runs at defaults — the Rust metrics kernel arm engaged): plain
2.4–2.9 ms · with-anchor 5.0–5.9 ms · ratio **1.80–2.32×** (median ≈ 2.0×).
The ratio is ~2× rather than a small increment because the BASE tail's
drawdown family rides the default-ON Rust kernel while the recency pass is a
deliberate pure-Python second fold (the #1902 fold-in-Rust/combine-in-Python
split; the flag-gated Rust recency fold twin ships default-OFF) — so the
anchor roughly DOUBLES today's default metrics-tail wall per combo. That is
the honest disclosed cost recorded in MIGRATION.md #1976. The 4× ceiling is
~1.7× noise headroom over the worst observed run — generous enough for
shared-CI timer jitter, tight enough that a superlinear regression (an
O(trades²) window scan at 2k trades) blows it by orders of magnitude.

House shape: direct wall-clock medians (the ``test_sweep_rust_metrics_off_cost``
idiom — same-process A/B on identical inputs, median-of-N), NOT an O(1) ratio
gate (`_o1_tiers` ceilings gate size-growth ratios; this is a fixed-size
arm-vs-arm cost bound, so the generous-multiple pattern applies).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.sweep_driver import metrics_from_trades
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_OPEN_UTC = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)

#: Fixed workload: one trade exiting per bar over N minutes — big enough that
#: the O(trades) folds dominate the tail, small enough for shared CI.
_N = 2_000


def _bars(n: int) -> list[CandleBar]:
    price = Decimal(100)
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
        for i in range(n)
    ]


def _trades(n: int) -> list[ClosedTrade]:
    # One trade exiting AT each bar's own timestamp (build_equity_curve
    # requires every exit_ts to be a bar timestamp).
    return [
        ClosedTrade(
            pnl_paisa=50 if i % 3 else -40,
            mfe_paisa=60,
            mae_paisa=45,
            holding_bars=1,
            entry_ts=_OPEN_UTC + timedelta(minutes=max(i - 1, 0)),
            exit_ts=_OPEN_UTC + timedelta(minutes=i),
            direction=Side.BUY,
        )
        for i in range(n)
    ]


def _median(samples: list[float]) -> float:
    samples.sort()
    return samples[len(samples) // 2]


class TestRecencyPassCost:
    def test_with_anchor_tail_within_generous_multiple_of_plain(self) -> None:
        """The anchor-threaded metrics tail ≤ 4× the anchor-less tail (fixed
        2k-trade workload) — the second O(trades) recency pass is bounded."""
        bars = _bars(_N)
        trades = _trades(_N)
        anchor = bars[-1].timestamp

        def _plain() -> None:
            metrics_from_trades(bars, trades, start_equity_paisa=10_000_000, tf_minutes=1)

        def _with_anchor() -> None:
            metrics_from_trades(
                bars,
                trades,
                start_equity_paisa=10_000_000,
                tf_minutes=1,
                trailing_anchor=anchor,
            )

        _plain()
        _with_anchor()  # warm both arms
        plain_samples: list[float] = []
        anchor_samples: list[float] = []
        for _ in range(15):
            t0 = time.perf_counter()
            _plain()
            plain_samples.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            _with_anchor()
            anchor_samples.append(time.perf_counter() - t0)
        plain = _median(plain_samples)
        with_anchor = _median(anchor_samples)
        assert with_anchor < plain * 4.0, (
            f"anchor-threaded metrics tail ({with_anchor * 1e3:.2f} ms) exceeds 4× the "
            f"plain tail ({plain * 1e3:.2f} ms) on the fixed {_N}-trade workload — the "
            f"recency pass (one extra O(trades) fold + O(buckets) combine) may have "
            f"regressed to a superlinear or per-trade-libm second pass"
        )
