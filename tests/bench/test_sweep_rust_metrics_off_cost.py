"""OFF-mode cost gate for the flag-gated Rust sweep-metrics tail.

DEC-SWEEP-RUST-METRICS-001's hard constraint (mirrors
``test_sweep_lookahead_off_cost.py``): with ``BRUTEX_SWEEP_RUST_METRICS``
OFF (the default), ``metrics_from_trades`` must be byte-identical AND
cost-unchanged — the ONLY addition on the OFF path is one env-dict probe.
Two complementary proofs (the structural third — OFF never touches the
kernel machinery — is a unit test with a bombed seam in
``test_engine_sweep_driver.py``):

1. **Flag-probe cost is nil:** the per-call ``sweep_rust_metrics_enabled()``
   read must cost less than ONE bare no-op walk over a large bar window —
   it can therefore never hide per-bar work.
2. **A/B wall-clock:** the OFF-mode ``metrics_from_trades`` median stays
   within a generous noise ceiling of the DIRECT
   ``build_equity_curve`` + ``compute_metrics`` tail it wraps (the same big
   work ± one env read).
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
    samples.sort()
    return samples[len(samples) // 2]


class TestOffModeIsFree:
    def test_flag_probe_cost_is_below_one_bare_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole OFF-mode seam addition (one env probe) is nil next to
        even ONE no-op per-bar walk — so it cannot hide a per-bar cost."""
        monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
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
        """OFF-arm seam cost ≈ the direct equity+metrics tail it wraps."""
        monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
        bars = _bars(_TAIL_BARS)

        def _direct() -> None:
            equity = build_equity_curve(bars, [], start_equity_paisa=10_000_000, tf_minutes=1)
            compute_metrics([], equity, total_bars=len(bars))

        def _seam() -> None:
            metrics_from_trades(bars, [], start_equity_paisa=10_000_000, tf_minutes=1)

        _direct()
        _seam()  # warm-up both
        direct_samples: list[float] = []
        seam_samples: list[float] = []
        for _ in range(15):
            t0 = time.perf_counter()
            _direct()
            direct_samples.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            _seam()
            seam_samples.append(time.perf_counter() - t0)
        direct = _median(direct_samples)
        seam = _median(seam_samples)
        # Identical big work ± one env probe; 1.5× absorbs shared-CI timer
        # noise while still catching any accidental extra per-bar pass.
        assert seam < direct * 1.5, (
            f"OFF-mode metrics_from_trades ({seam * 1e3:.2f} ms) is not "
            f"cost-identical to the direct tail ({direct * 1e3:.2f} ms)"
        )
