"""Cost gate for the per-combo recency pass (DEC-METRICS-TRAILING-WINDOW-001).

Refuter round 1 (SLA/bench honesty): threading a ``trailing_anchor`` into the
metrics tail runs a SECOND O(trades) fold/combine (``fold_recency`` →
``combine_recency_from_folds``) per combo — a real, disclosed per-mask cost on
the sweep spine (`_iter_mask_metrics` ALWAYS threads the anchor), which no
bench measured. This file pins that cost's SHAPE on a fixed 2,000-trade /
2,000-bar workload.

Refuter round 2 (GATING honesty): the wall-clock RATIO assertion (with-anchor
≤ 4× plain) is gated to the quiet M4-Pro tier (``BRUTEX_M4PRO_ABS=1`` — the
``test_feed_rust_bench`` idiom). The round-1 docstring's "generous-multiple"
justification was FALSE for these ASYMMETRIC arms: the two arms run DIFFERENT
code (Rust-kernel base tail vs base tail + pure-Python second fold), so under
shared-CI contention they DECORRELATE instead of shifting together — the
round-2 refuter REPRODUCED the 4× ceiling breaching on 20-25% of runs under
simulated 2-core contention (worst observed ratio 11.26×), and this bench sits
in a REQUIRED ci-ok job. The generous-multiple pattern is only safe for
same-code A/B arms whose noise is common-mode; an absolute-ish asymmetric
wall-clock bound belongs on the quiet tier (the house
``test_instrument_bulk_upsert``/``test_feed_rust_bench`` precedent).

MEASURED uncontended (sandbox 2026-07-18, median-of-15, repeat process runs at
defaults — the Rust metrics kernel arm engaged): plain 2.4–2.9 ms ·
with-anchor 5.0–5.9 ms · ratio **1.76–2.32×** (median ≈ 2.0×). The ratio is
~2× rather than a small increment because the BASE tail's drawdown family
rides the default-ON Rust kernel while the recency pass is a deliberate
pure-Python second fold (the #1902 fold-in-Rust/combine-in-Python split; the
flag-gated Rust recency fold twin ships default-OFF) — so the anchor roughly
DOUBLES today's default metrics-tail wall per combo. That is the honest
disclosed cost recorded in MIGRATION.md #1976. The 4× ceiling keeps ~1.7×
headroom over the worst UNCONTENDED run — on the quiet tier it still catches
a superlinear regression (an O(trades²) window scan at 2k trades) by orders
of magnitude.

Shared CI is NOT vacuous: the ungated smoke arm below exercises BOTH code
paths every push and asserts a CORRECTNESS law (the anchor arm's non-recency
fields are bit-identical to the plain arm's, its recency fields concrete,
both arms deterministic) — a timing-free assert that cannot flake.
"""

from __future__ import annotations

import dataclasses
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_driver import metrics_from_trades
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_OPEN_UTC = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)

#: Fixed workload: one trade exiting per bar over N minutes — big enough that
#: the O(trades) folds dominate the tail, small enough for shared CI.
_N = 2_000

#: The four recency fields the anchor arm adds (DEC-METRICS-TRAILING-WINDOW-001).
_RECENCY_FIELDS = (
    "trailing_sharpe_ratio",
    "trailing_profit_factor",
    "trailing_net_pnl_paisa",
    "decay_weighted_net_paisa",
)


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
    def test_both_arms_complete_and_agree_smoke(self) -> None:
        """Ungated shared-CI smoke: BOTH tail arms run on the full workload and
        obey the composition law — the anchor arm equals the plain arm on every
        non-recency field (the recency pass is a ``dataclasses.replace`` AFTER
        the main combine, so the base metrics are bit-identical), its four
        recency fields are CONCRETE (every trade exits inside the 24-month
        window of the bar-range-end anchor), and each arm is deterministic.
        A correctness assert, never a timing assert — this file cannot go
        vacuous on shared CI while the ratio gate rides the M4-Pro tier."""
        bars = _bars(_N)
        trades = _trades(_N)
        anchor = bars[-1].timestamp

        plain = metrics_from_trades(
            bars, trades, start_equity_paisa=10_000_000, tf_minutes=1, exec_bars_1m=bars
        )
        with_anchor = metrics_from_trades(
            bars,
            trades,
            start_equity_paisa=10_000_000,
            tf_minutes=1,
            exec_bars_1m=bars,
            trailing_anchor=anchor,
        )
        # Determinism: repeat calls reproduce each arm bit-for-bit.
        assert (
            metrics_from_trades(
                bars, trades, start_equity_paisa=10_000_000, tf_minutes=1, exec_bars_1m=bars
            )
            == plain
        )
        assert (
            metrics_from_trades(
                bars,
                trades,
                start_equity_paisa=10_000_000,
                tf_minutes=1,
                exec_bars_1m=bars,
                trailing_anchor=anchor,
            )
            == with_anchor
        )
        # The plain arm is the honest legacy shape: recency fields all None.
        for field in _RECENCY_FIELDS:
            assert getattr(plain, field) is None, field
            assert getattr(with_anchor, field) is not None, field
        # Composition law: nulling the recency four recovers the plain arm
        # EXACTLY (the second pass never perturbs the main combine).
        nulled = dataclasses.replace(
            with_anchor,
            trailing_sharpe_ratio=None,
            trailing_profit_factor=None,
            trailing_net_pnl_paisa=None,
            decay_weighted_net_paisa=None,
        )
        assert nulled == plain

    @pytest.mark.skipif(
        os.environ.get("BRUTEX_M4PRO_ABS", "") != "1",
        reason=(
            "asymmetric-arm wall-clock ratio gated to the quiet M4-Pro tier "
            "(BRUTEX_M4PRO_ABS=1) — reproduced flaking 20-25% under shared-CI "
            "contention (max 11.26x vs the 4x ceiling)"
        ),
    )
    def test_with_anchor_tail_within_generous_multiple_of_plain(self) -> None:
        """The anchor-threaded metrics tail ≤ 4× the anchor-less tail (fixed
        2k-trade workload) — the second O(trades) recency pass is bounded.
        Quiet-tier only (see the module docstring's round-2 gating record)."""
        bars = _bars(_N)
        trades = _trades(_N)
        anchor = bars[-1].timestamp

        def _plain() -> None:
            metrics_from_trades(
                bars, trades, start_equity_paisa=10_000_000, tf_minutes=1, exec_bars_1m=bars
            )

        def _with_anchor() -> None:
            metrics_from_trades(
                bars,
                trades,
                start_equity_paisa=10_000_000,
                tf_minutes=1,
                exec_bars_1m=bars,
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
