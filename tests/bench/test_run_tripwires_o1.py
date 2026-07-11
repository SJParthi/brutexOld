"""O(1) bench — :meth:`TripwireMonitor.on_progress` tick flat vs enumerated total.

The R39 tripwire tick is a fixed set of counter comparisons + two trend
folds — its latency must be independent of how many combos the run
enumerates (1k vs 1M totals here). Ratio routed through the
single-source-of-truth :func:`tests.bench._o1_tiers.ratio_ceiling` (5× on
shared CI; strict tier on the M4-Pro runner); timing via the house
best-of-N-MIN idiom (:func:`tests.bench._abs_baseline_registry.measure_best_of_min`).

HONEST envelope (Golden Rule #10): this pins the per-TICK cost only — the
monitor performs one tick per progress callback, so total tripwire cost is
O(ticks), inherently. ``on_tuple_summary`` is O(keys) in its own small
mapping (a handful of floats), not pinned here.
"""

from __future__ import annotations

from typing import Final

import pytest
from brutex.engine.sweep_progress import SweepProgressSnapshot
from brutex.observability.run_tripwires import TripwireConfig, TripwireEvent, TripwireMonitor

from tests.bench._abs_baseline_registry import measure_best_of_min
from tests.bench._o1_tiers import ratio_ceiling

pytestmark = pytest.mark.bench

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r39_on_progress_tick_flat_vs_enumerated_total": ("R39",),
}

_SMALL_TOTAL: Final[int] = 1_000
_LARGE_TOTAL: Final[int] = 1_000_000
_TICKS_PER_ROUND: Final[int] = 2_000


def _snapshot(total: int) -> SweepProgressSnapshot:
    return SweepProgressSnapshot(
        state="running",
        enumerated=total,
        evaluated=total // 2,
        survivors=total // 10,
        skipped=total // 20,
        current="bench",
        detail="",
        started_at=0.0,
        updated_at=100.0,
    )


def _tick_op(total: int) -> tuple[TripwireMonitor, SweepProgressSnapshot]:
    """A monitor with EVERY watch armed (constant stub probe, known RAM)."""
    monitor = TripwireMonitor(
        config=TripwireConfig(reconcile_interval_s=1e-9),
        enumerated_total=total,
        on_trip=_collect,
        ram_total_bytes=2**40,
        rss_probe=lambda: 1_000_000,
    )
    return monitor, _snapshot(total)


_COLLECTED: list[TripwireEvent] = []


def _collect(event: TripwireEvent) -> None:  # pragma: no cover - fires only on a trip
    _COLLECTED.append(event)


class TestOnProgressTickIsO1:
    def test_r39_on_progress_tick_flat_vs_enumerated_total(self) -> None:
        """Per-tick latency ratio (1M-total / 1k-total) under the tier ceiling."""

        def timed(total: int) -> float:
            monitor, snapshot = _tick_op(total)

            def op() -> None:
                for _ in range(_TICKS_PER_ROUND):
                    monitor.on_progress(snapshot, in_flight=0)

            best_ns, _samples, _spread = measure_best_of_min(
                op, ops_per_round=_TICKS_PER_ROUND, rounds=7, warmup=2
            )
            # Anti-vacuous: the counters were LEGAL — nothing tripped.
            assert monitor.suspect is False
            assert monitor.events == ()
            return best_ns

        small_ns = timed(_SMALL_TOTAL)
        large_ns = timed(_LARGE_TOTAL)
        ratio = large_ns / max(small_ns, 1.0)
        ceiling = ratio_ceiling(5.0)
        assert ratio < ceiling, (
            f"on_progress tick degraded with enumerated total: {small_ns:.0f} ns @ "
            f"{_SMALL_TOTAL} vs {large_ns:.0f} ns @ {_LARGE_TOTAL} (ratio {ratio:.2f} "
            f">= ceiling {ceiling}) — suspected O(total) scan in the reconciliation"
        )
        assert _COLLECTED == []
