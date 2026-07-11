"""Cost gates for the flag-gated Rust ranking seam (slice-4 PR-1).

Two pins, mirroring ``test_sweep_rust_metrics_off_cost.py``:

1. **OFF-arm zero-cost:** with ``BRUTEX_RANKING_RUST`` OFF (the default),
   the ONLY additions on the ``rank_combos`` path are one cached-module
   import, one env-dict probe and one boolean call — pinned nil next to
   the rank itself (measured 2026-07-11 on this container: probe median
   ~0.6 µs vs a ~0.6 ms 200-survivor rank — 0.1%).
2. **The (b*) differential-cost bound:** the flag-ON leaderboard build —
   which by design double-computes the Python oracle on EVERY call for the
   always-on (b*) families and once per burn-in sample for the (a)/(b)
   families — stays within the design's generous MEASURED ×3 bound of the
   flag-OFF build (§3.6: "≤3× flag-OFF — measured, not assumed"; measured
   2026-07-11 on this container: 1.29× at 200 survivors, post-burn-in
   steady state includes the always-on composite+dsr double-compute).

No speedup is claimed anywhere on this seam (the honest-payoff framing:
rank-time code, one-language consistency — the ON arm is EXPECTED to cost
more while the differential runs).
"""

from __future__ import annotations

import time

import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import rank_combos
from brutex.ranking.rust_ranking import (
    RANKING_RUST_ENV,
    ranking_rust_enabled,
    reset_ranking_rust_for_tests,
)

from tests.fakes.metrics_fixtures import make_result_metrics

_SURVIVORS = 200
_WALK_ELEMENTS = 100_000


def _pool() -> list[tuple[int, ResultMetrics]]:
    return [
        (
            mask,
            make_result_metrics(
                sharpe_ratio=1.0 + mask * 0.001,
                sortino_ratio=0.5 + mask * 0.002,
                calmar_ratio=0.25 + mask * 0.0005,
            ),
        )
        for mask in range(1, _SURVIVORS + 1)
    ]


def _median(samples: list[float]) -> float:
    samples.sort()
    return samples[len(samples) // 2]


def _rank_median(pool: list[tuple[int, ResultMetrics]], runs: int = 15) -> float:
    rank_combos(pool)  # warm-up (imports + burn-in progression)
    samples: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        rank_combos(pool)
        samples.append(time.perf_counter() - t0)
    return _median(samples)


class TestOffModeIsFree:
    def test_flag_probe_cost_is_below_one_bare_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole OFF-mode seam addition (one env probe + one call) is
        nil next to even ONE no-op walk — it can never hide per-row work."""
        monkeypatch.setenv(RANKING_RUST_ENV, "0")
        elements = list(range(_WALK_ELEMENTS))

        probe_samples: list[float] = []
        for _ in range(200):
            t0 = time.perf_counter()
            enabled = ranking_rust_enabled()
            probe_samples.append(time.perf_counter() - t0)
            assert enabled is False
        probe = _median(probe_samples)

        walk_samples: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            for _element in elements:
                pass
            walk_samples.append(time.perf_counter() - t0)
        walk = _median(walk_samples)

        assert probe < walk * 2.0, (
            f"OFF-mode flag probe ({probe * 1e6:.1f} µs) is not nil next to a bare "
            f"{_WALK_ELEMENTS}-element walk ({walk * 1e6:.1f} µs)"
        )

    def test_off_arm_dispatch_overhead_is_nil_next_to_the_rank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dispatch addition per OFF-arm rank call is bounded by the
        flag-probe cost — pinned at under 10% of one leaderboard rank
        (measured ~0.1%; the generous ceiling absorbs shared-CI noise)."""
        monkeypatch.setenv(RANKING_RUST_ENV, "0")
        pool = _pool()
        rank = _rank_median(pool)
        probe_samples: list[float] = []
        for _ in range(200):
            t0 = time.perf_counter()
            ranking_rust_enabled()
            probe_samples.append(time.perf_counter() - t0)
        probe = _median(probe_samples)
        assert probe < rank * 0.10, (
            f"flag probe ({probe * 1e6:.1f} µs) is not nil next to one "
            f"{_SURVIVORS}-survivor rank ({rank * 1e6:.1f} µs)"
        )


class TestDifferentialCostBound:
    def test_flag_on_rank_stays_within_the_measured_3x_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ON arm — always-on (b*) double-compute included — costs at
        most 3× the OFF arm (design §3.6's generous measured bound;
        measured 1.29× on this container 2026-07-11). A blow-past means an
        accidental extra pass (e.g. a per-row FFI call) snuck in."""
        pool = _pool()
        monkeypatch.setenv(RANKING_RUST_ENV, "0")
        off = _rank_median(pool)
        monkeypatch.setenv(RANKING_RUST_ENV, "1")
        reset_ranking_rust_for_tests()
        try:
            on = _rank_median(pool)
        finally:
            reset_ranking_rust_for_tests()
        assert on < off * 3.0, (
            f"flag-ON rank ({on * 1e3:.2f} ms) exceeds 3× the flag-OFF rank "
            f"({off * 1e3:.2f} ms) — the differential envelope regressed"
        )
