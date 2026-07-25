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
   flag-OFF build (§3.6: "≤3× flag-OFF — measured, not assumed"). The timed
   rank runs WITH the overfit gate (``dsr_min=0.0`` — every survivor kept),
   so BOTH always-on (b*) families genuinely execute in the measured window:
   ``ranking.composite`` AND ``ranking.dsr_z`` (a no-``overfit`` rank never
   reaches the dsr_z kernel — its ``None`` arm is a zero-cost ``[0.0] * n``).
   Measured 2026-07-11 on this container at 200 survivors: 1.5-1.7× with the
   15 timed calls INSIDE the 16-sample (a)/(b) burn-in (the test's own
   shape — one warm-up call, so every timed call still double-computes the
   burn-in families too), 1.1-1.3× post-burn-in steady state.

No speedup is claimed anywhere on this seam (the honest-payoff framing:
rank-time code, one-language consistency — the ON arm is EXPECTED to cost
more while the differential runs).
"""

from __future__ import annotations

import time

import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import rank_combos
from brutex.ranking.overfit import OverfitGateConfig
from brutex.ranking.rust_ranking import (
    RANKING_RUST_ENV,
    ranking_rust_enabled,
    reset_ranking_rust_for_tests,
)

from tests.fakes.metrics_fixtures import make_result_metrics

_SURVIVORS = 200
_WALK_ELEMENTS = 100_000

#: The differential-cost bound's overfit config: ``dsr_min=0.0`` keeps every
#: deflatable survivor, so the timed rank exercises the WHOLE ON-arm pipeline —
#: including the always-on ``ranking.dsr_z`` (b*) double-compute, which a
#: no-``overfit`` rank never reaches (``dsr_by_survivor is None`` → the
#: zero-cost ``[0.0] * n`` arm).
_OVERFIT_CFG = OverfitGateConfig(dsr_min=0.0)


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


def _rank_median(
    pool: list[tuple[int, ResultMetrics]],
    runs: int = 15,
    overfit: OverfitGateConfig | None = None,
) -> float:
    rank_combos(pool, overfit=overfit)  # warm-up (imports + burn-in progression)
    samples: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        rank_combos(pool, overfit=overfit)
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
        """The ON arm — BOTH always-on (b*) double-computes included — costs
        at most 3× the OFF arm (design §3.6's generous measured bound). The
        rank runs WITH the overfit gate so the always-on ``ranking.dsr_z``
        family genuinely executes (a no-``overfit`` rank never reaches it);
        the timed calls additionally sit inside the (a)/(b) 16-sample
        burn-in, so the measurement is the WORST honest shape — every family
        double-computing on every timed call (measured 1.5-1.7× on this
        container 2026-07-11; steady state 1.1-1.3×). A blow-past means an
        accidental extra pass (e.g. a per-row FFI call) snuck in.

        ESTIMATOR (DEC-BENCH-SLOPE-BOUND-001): the two arms are INTERLEAVED and
        the statistic is the median of the PER-PAIR ratios — never two
        independently-measured medians. The prior shape measured arm OFF to
        completion, then arm ON to completion, so a scheduler stall / co-tenant
        burst / GC pause landing in ONE window shifted that arm's whole median
        with nothing to cancel it. Reproduced 2026-07-25 on an emulated
        contended 2-core runner (``taskset -c 0,1`` + 6 busy loops): the
        unpaired shape failed 1-in-8 trials at 3.35× (``off`` read anomalously
        FAST at 4.06 ms vs a 5-7 ms unloaded baseline — the DENOMINATOR was the
        outlier, not the numerator), while the paired estimator over the same
        load peaked at 2.16×. ``min``-of-repeats was measured and REJECTED here:
        worst-of-8 4.76×, because interruption probability scales with duration
        and ``min`` systematically favours the shorter (OFF) arm. The ceiling is
        UNCHANGED at 3.0 — this fixes the estimator, never the teeth: a genuine
        regression inflates EVERY pair, so the median moves with it."""
        pool = _pool()
        monkeypatch.setenv(RANKING_RUST_ENV, "0")
        rank_combos(pool, overfit=_OVERFIT_CFG)  # warm-up OFF
        monkeypatch.setenv(RANKING_RUST_ENV, "1")
        reset_ranking_rust_for_tests()
        rank_combos(pool, overfit=_OVERFIT_CFG)  # warm-up ON (burn-in progression)
        pair_ratios: list[float] = []
        off_samples: list[float] = []
        on_samples: list[float] = []
        try:
            for _ in range(15):
                # ``ranking_rust_enabled()`` is read per call (no global latch),
                # so the arm can be flipped inside the loop.
                monkeypatch.setenv(RANKING_RUST_ENV, "0")
                t0 = time.perf_counter()
                rank_combos(pool, overfit=_OVERFIT_CFG)
                off_i = time.perf_counter() - t0
                monkeypatch.setenv(RANKING_RUST_ENV, "1")
                t0 = time.perf_counter()
                rank_combos(pool, overfit=_OVERFIT_CFG)
                on_i = time.perf_counter() - t0
                off_samples.append(off_i)
                on_samples.append(on_i)
                pair_ratios.append(on_i / off_i)
        finally:
            reset_ranking_rust_for_tests()
        ratio = _median(pair_ratios)
        assert ratio < 3.0, (
            f"flag-ON rank exceeds 3× the flag-OFF rank — the differential envelope "
            f"regressed: paired median {ratio:.2f}× over {len(pair_ratios)} interleaved "
            f"rounds (arm medians ON {_median(on_samples) * 1e3:.2f} ms vs OFF "
            f"{_median(off_samples) * 1e3:.2f} ms)"
        )
