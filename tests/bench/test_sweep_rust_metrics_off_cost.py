"""OFF-mode cost gate for the flag-gated Rust sweep-metrics tail.

DEC-SWEEP-RUST-METRICS-001's hard constraint (mirrors
``test_sweep_lookahead_off_cost.py``): with ``BRUTEX_SWEEP_RUST_METRICS``
OFF (the explicit ``"0"`` kill switch — since ADR-0017 /
DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001 the DEFAULT is the kernel arm, so the
Python arm is pinned by ``"0"``), ``metrics_from_trades`` must be
byte-identical AND cost-unchanged — the ONLY addition on the OFF path is
one env-dict probe.
Two complementary proofs (the structural third — OFF never touches the
kernel machinery — is a unit test with a bombed seam in
``test_engine_sweep_driver.py``):

1. **Flag-probe cost is nil:** the per-call ``sweep_rust_metrics_enabled()``
   read must cost less than ONE bare no-op walk over a large bar window —
   it can therefore never hide per-bar work.
2. **A/B wall-clock:** the OFF-mode ``metrics_from_trades`` PAIRED median
   stays within a generous noise ceiling of the DIRECT
   ``build_equity_curve`` + ``compute_metrics`` tail it wraps (the same big
   work ± one env read). The statistic is the median of the per-pair ratios
   over interleaved samples, never two independently estimated medians —
   see the ESTIMATOR paragraph on the test itself
   (DEC-BENCH-SLOPE-BOUND-002, MIGRATION.md #2251 — the estimator fix
   DEC-BENCH-SLOPE-BOUND-001 #2250 introduced, applied to this third
   sibling its residual list did not name).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.rust_metrics import SWEEP_RUST_METRICS_ENV, sweep_rust_metrics_enabled
from brutex.engine.sweep_driver import build_equity_curve, metrics_from_trades
from brutex.metrics.compute import compute_metrics

_OPEN_UTC = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)

_LARGE = 100_000
_TAIL_BARS = 20_000


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


def _median(samples: list[float]) -> float:
    # `sorted` (not `list.sort`): the caller's sample lists are re-read for the
    # failure message, so an in-place sort would silently reorder them under it.
    return sorted(samples)[len(samples) // 2]


class TestOffModeIsFree:
    def test_flag_probe_cost_is_below_one_bare_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole OFF-mode seam addition (one env probe) is nil next to
        even ONE no-op per-bar walk — so it cannot hide a per-bar cost."""
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        bars = _bars(_LARGE)

        probe_samples: list[float] = []
        for _ in range(200):
            t0 = time.perf_counter()
            enabled = sweep_rust_metrics_enabled()
            probe_samples.append(time.perf_counter() - t0)
            assert enabled is False
        probe = _median(probe_samples)

        walk_samples: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            for _bar in bars:
                pass
            walk_samples.append(time.perf_counter() - t0)
        walk = _median(walk_samples)

        assert probe < walk * 2.0, (
            f"OFF-mode flag probe ({probe * 1e6:.1f} µs) is not nil next to a bare "
            f"{_LARGE}-bar walk ({walk * 1e6:.1f} µs)"
        )

    def test_off_metrics_from_trades_matches_the_direct_tail_cost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OFF-arm seam cost ≈ the direct equity+metrics tail it wraps.

        FIDELITY: there is no confound here. Read
        ``brutex/engine/sweep_driver.py`` — with the flag OFF
        ``metrics_from_trades`` performs exactly ``build_equity_curve(...)``
        then ``compute_metrics(..., total_bars=len(exec_bars_1m))``, and this
        test passes ``exec_bars_1m=bars``, so ``total_bars == len(bars)`` on
        BOTH arms. The only seam additions are one
        ``sweep_rust_metrics_enabled()`` env probe and the ``None``-valued
        ``sl_exit_count`` expression. The arms already do identical big work,
        so the ceiling stays 1.5× — widening it to absorb noise would be
        weakening real teeth (Golden Rule #10).

        ESTIMATOR (DEC-BENCH-SLOPE-BOUND-002, MIGRATION.md #2251): the
        statistic is the median of the PER-PAIR ratios over interleaved
        samples, never two independently estimated medians. Independent
        medians can draw from different iterations, so a scheduler stall or
        co-tenant burst landing in ONE arm's window shifts that median with
        nothing to cancel it — and in both contended-runner cases reproduced
        for the sibling ``test_exec_rust_off_cost.py`` the DENOMINATOR was the
        outlier, inflating the ratio into a false FAIL. Pairing cancels a
        stall against its own immediate neighbour. The ceiling is UNCHANGED:
        a genuine regression inflates EVERY pair, so the median moves with it.
        """
        monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, "0")
        bars = _bars(_TAIL_BARS)

        def _direct() -> None:
            equity = build_equity_curve(bars, [], start_equity_paisa=10_000_000, tf_minutes=1)
            compute_metrics([], equity, total_bars=len(bars))

        def _seam() -> None:
            metrics_from_trades(
                bars, [], start_equity_paisa=10_000_000, tf_minutes=1, exec_bars_1m=bars
            )

        _direct()
        _seam()  # warm-up both
        pair_ratios: list[float] = []
        direct_samples: list[float] = []
        seam_samples: list[float] = []
        for _ in range(15):
            t0 = time.perf_counter()
            _direct()
            direct_i = time.perf_counter() - t0
            t0 = time.perf_counter()
            _seam()
            seam_i = time.perf_counter() - t0
            direct_samples.append(direct_i)
            seam_samples.append(seam_i)
            pair_ratios.append(seam_i / direct_i)
        # Identical big work ± one env probe; 1.5× absorbs shared-CI timer
        # noise. HONEST SENSITIVITY (measured, not asserted — an injected
        # extra per-bar integer pass over 100% of bars reads 1.302× and over
        # 50% reads 1.155×, i.e. BOTH pass): this gate catches a pass costing
        # roughly >=50% of the equity+metrics tail, NOT any accidental extra
        # per-bar pass. The earlier wording claimed the latter and was
        # measurably optimistic. The deterministic teeth for a small extra
        # pass are the seam's own parity/golden suites, not this bench. The
        # statistic is the PER-PAIR median (see the ESTIMATOR paragraph) — a
        # stall inside one arm's window is cancelled by its own paired
        # neighbour, never by an unrelated iteration's sample.
        ratio = _median(pair_ratios)
        assert ratio < 1.5, (
            f"OFF-mode metrics_from_trades is not cost-identical to the direct "
            f"tail: paired median {ratio:.2f}× over {len(pair_ratios)} interleaved "
            f"rounds (arm medians seam {_median(seam_samples) * 1e3:.2f} ms vs "
            f"direct {_median(direct_samples) * 1e3:.2f} ms)"
        )
