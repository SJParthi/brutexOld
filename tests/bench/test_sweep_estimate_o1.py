"""O(1) bench pins for the sweep estimator (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001, R29).

HONEST claim (Golden Rule #10 / BUILD-SPEC §10.2): :func:`estimate_selection`
is **O(axes) pure arithmetic** — one ``math.comb`` per distinct ``k`` plus a
handful of axis-width products — and **O(1) w.r.t. lake depth, stored-winner
count and bar counts** (it takes no lake path at all; the calibration read is
ONE bounded ≤20-entry JSON sidecar). These gates exist so a future regression
(e.g. an accidental results-tree scan inside the estimate path) fails CI
mechanically — R29's named worst case is an ``out_root`` holding thousands of
prior winner files.

Ceilings route through :func:`tests.bench._o1_tiers.ratio_ceiling` (robust on
shared CI, strict 1.1× on the M4-Pro tier) — never a bare constant. Measured
2026-07-07 on the shared sandbox (Python 3.12.3): selection-range ratio 1.45×
(constant O(axes) arithmetic — 1.2M× more trials for ≤1.5× the wall),
stored-winners ratio 0.97×, recorded-history ratio 0.98× — all far under the
shared 5× ceiling.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from brutex.data.timeframes import LAKE_INTERVAL_LABELS
from brutex.engine.sweep_calibration import (
    RunActuals,
    load_calibration,
    record_run_calibration,
)
from brutex.engine.sweep_estimate import (
    SweepSelection,
    estimate_selection,
    selection_from_raw,
)
from structlog.testing import capture_logs

from tests.bench._o1_tiers import ratio_ceiling

if TYPE_CHECKING:
    from collections.abc import Callable

_START = date(2020, 1, 1)
_END = date(2026, 7, 3)
_REPS = 50
_FP = "cores=bench;ram=1"


def _tiny_selection() -> SweepSelection:
    """1 symbol × 1 TF × k=1 × 1×1 SL/TP — the smallest sensible selection."""
    return selection_from_raw(
        symbols=["NIFTY"],
        intervals=["1minute"],
        directions=["BUY"],
        k_values=[1],
        stop_losses=[None],
        take_profits=[None],
        fib_anchors=["prev_day"],
        top_k=20,
        start=_START,
        end=_END,
    )


def _big_selection() -> SweepSelection:
    """3 symbols × 21 TFs × k≤5 × the 16-cell O-g SL/TP grid (BUILD-SPEC §10.2a)."""
    return selection_from_raw(
        symbols=["NIFTY", "BANKNIFTY", "SENSEX"],
        intervals=list(LAKE_INTERVAL_LABELS.values()),
        directions=["BUY", "SELL"],
        k_values=[1, 2, 3, 4, 5],
        stop_losses=[None, 5, 100, 200],
        take_profits=[None, 10, 250, 500],
        fib_anchors=["prev_day"],
        top_k=20,
        start=_START,
        end=_END,
    )


def _best_ns(fn: Callable[[], object], reps: int = _REPS) -> float:
    """Best-of-3 mean nanoseconds per call (warm first; logs captured, not printed)."""
    with capture_logs():
        fn()  # warm
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter_ns()
            for _ in range(reps):
                fn()
            best = min(best, (time.perf_counter_ns() - start) / reps)
    return best


def _actuals(*, trials: int = 200_000, wall: float = 100.0) -> RunActuals:
    """One qualifying run for seeding calibration rings."""
    return RunActuals(
        run_identity="bench",
        wall_seconds=wall,
        trials_enumerated=trials,
        tuples_run=2,
        rows_persisted=100,
        result_bytes=98_720,
    )


def test_r29_estimator_flat_vs_selection_and_stored_winners(tmp_path: Path) -> None:
    """R29 / extreme test 15 — flat vs selection value ranges AND vs 10k stored winners.

    (a) A 1×1×k=1 selection vs the 3×21×k≤5×16-cell grid: the estimate cost
    moves only with the AXIS arithmetic, never with the trial count — the big
    grid holds ~1.2 MILLION times more trials (72,676,800 × C(60,4..5) growth
    vs 60) for a small constant extra wall. Measured 2026-07-07: small
    20.5 µs/call, big 29.8 µs/call, ratio 1.45× (the honest O(axes) constant
    — five ``math.comb`` calls + wider dedup lists, never trials-scaled).

    (b) 10,000 fake winner files under ``out_root`` vs an empty ``out_root``:
    the full estimate path (``load_calibration`` + ``estimate_selection``)
    provably never reads the results tree — identical wall (measured
    2026-07-07: 51.8 µs vs 50.2 µs, ratio 0.97×).
    """
    tiny = _tiny_selection()
    big = _big_selection()
    small_ns = _best_ns(lambda: estimate_selection(tiny, calibration=None))
    big_ns = _best_ns(lambda: estimate_selection(big, calibration=None))
    ratio = big_ns / small_ns if small_ns > 0 else 1.0
    ceiling = ratio_ceiling(5.0)
    assert ratio <= ceiling, (
        f"estimate_selection selection-range ratio {ratio:.2f} "
        f"(big={big_ns:.0f}ns small={small_ns:.0f}ns) exceeds the O(axes) ceiling "
        f"{ceiling:.2f} — the estimator scales with something other than the axes"
    )

    empty_root = tmp_path / "empty"
    (empty_root / "calibration").mkdir(parents=True)
    stuffed_root = tmp_path / "stuffed"
    winners = stuffed_root / "NIFTY" / "1minute"
    winners.mkdir(parents=True)
    for i in range(10_000):
        (winners / f"BUY_k2_{i:016x}.parquet").touch()

    def _estimate_path(root: Path) -> None:
        snapshot = load_calibration(root, fingerprint=_FP)
        estimate_selection(big, calibration=snapshot)

    empty_ns = _best_ns(lambda: _estimate_path(empty_root))
    stuffed_ns = _best_ns(lambda: _estimate_path(stuffed_root))
    winners_ratio = stuffed_ns / empty_ns if empty_ns > 0 else 1.0
    assert winners_ratio <= ceiling, (
        f"estimate path stored-winners ratio {winners_ratio:.2f} "
        f"(10k-files={stuffed_ns:.0f}ns empty={empty_ns:.0f}ns) exceeds {ceiling:.2f} — "
        f"the estimator is reading the results tree (R29's named worst case)"
    )


def test_r29_estimator_flat_vs_calibration_ring_size(tmp_path: Path) -> None:
    """R29 — flat vs the RECORDED-RUN history (the ring cap is the bound).

    The ring is CAPPED at :data:`CALIBRATION_RING_CAP` = 20 entries by design:
    a store that has recorded 21 runs and one that has recorded 61 runs hold
    the SAME 20-entry sidecar, so the load+derive cost is flat vs the
    unbounded axis (total runs ever recorded). This pin catches a regression
    that starts accumulating unbounded history. Measured 2026-07-07:
    21-runs 170.3 µs/call vs 61-runs 167.4 µs/call, ratio 0.98× (one
    ≤20-entry JSON read each).
    """
    big = _big_selection()
    few_root = tmp_path / "few"
    many_root = tmp_path / "many"
    with capture_logs():
        base_estimate = estimate_selection(big, calibration=None)
        for i in range(21):
            record_run_calibration(
                few_root,
                estimate=base_estimate,
                actuals=_actuals(trials=200_000 + i, wall=100.0 + i),
                fingerprint=_FP,
            )
        for i in range(61):
            record_run_calibration(
                many_root,
                estimate=base_estimate,
                actuals=_actuals(trials=200_000 + i, wall=100.0 + i),
                fingerprint=_FP,
            )

    def _estimate_path(root: Path) -> None:
        snapshot = load_calibration(root, fingerprint=_FP)
        estimate_selection(big, calibration=snapshot)

    few_ns = _best_ns(lambda: _estimate_path(few_root))
    many_ns = _best_ns(lambda: _estimate_path(many_root))
    ratio = many_ns / few_ns if few_ns > 0 else 1.0
    ceiling = ratio_ceiling(5.0)
    assert ratio <= ceiling, (
        f"estimate path recorded-history ratio {ratio:.2f} "
        f"(61-runs={many_ns:.0f}ns 21-runs={few_ns:.0f}ns) exceeds {ceiling:.2f} — "
        f"the calibration read is no longer bounded by the ring cap"
    )
