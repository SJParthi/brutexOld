"""O(1) benchmark for the walk-forward per-mask fold driver (`sweep_driver`).

DEC-ENGINE-DRIVER-FOLD-O1-001 — completes the engine O(1) bench matrix with the
walk-forward / per-mask driver hot path that the existing sweep benches
(``test_engine_combo_mask_o1`` per-mask AND, ``test_engine_sweep_run_o1`` dedup +
audit) did not cover: the per-mask cost of
:func:`brutex.engine.sweep_driver.walk_forward_sweep`.

The O(1) property this proves (Golden Rule #10 — stated precisely):
**the per-mask walk-forward eval cost is INDEPENDENT of how many masks the sweep
grid contains.** ``walk_forward_sweep`` builds the fold schedule + the
full-stream Stage-1A ``bar_bits`` matrix ONCE (and re-proves the schedule's
no-look-ahead invariant once), then scores each mask across the fixed folds via
the shared :func:`brutex.engine.sweep_driver.metrics_for_mask` core. So
evaluating one mask must cost the same whether the grid holds 1× or 10× as many
masks — the per-mask work is O(folds·bars) in its OWN inputs (the bars + the
fold layout, both held FIXED here), never a function of the grid size. This is a
per-mask O(1)-in-grid-size claim, NOT an O(1)-total-sweep claim (the total is
O(masks · folds · bars) by construction).

Deterministic: a fixed multi-day IST bar stream and the self-contradictory
all-52-bits ``_IMPOSSIBLE_MASK`` (never fires → a stable no-trade fold on every
mask, so each per-mask iteration does identical work) make the timing reflect
only the per-mask machinery, not data-dependent trade counts. The ratio is
routed through the single-source-of-truth :func:`tests.bench._o1_tiers.ratio_ceiling`
(5× on shared CI, the strict 1.1× tier on the M4-Pro runner), mirroring the
sibling engine O(1) benches. A regression that made the per-mask cost grow with
grid size (e.g. re-building ``bar_bits`` or the schedule per mask, or an
accidental O(grid) lookup) would blow the ratio and fail.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.sweep_driver import walk_forward_sweep
from brutex.strategies.types import Side

from tests.bench._o1_tiers import ratio_ceiling

_IST = timezone(timedelta(hours=5, minutes=30))

#: Grid-size dimension under test — the per-mask cost must NOT grow from the
#: small grid to the (10×-larger) grid.
_SMALL = 60
_LARGE = 600

#: A few warm-up masks to prime interpreter caches before the timed loop.
_WARM = 5

#: A self-contradictory mask (all 52 bits required) never fires → a deterministic
#: no-trade fold for EVERY mask, so each per-mask iteration does identical work
#: and the measured time reflects only the per-mask driver machinery.
_IMPOSSIBLE_MASK = (1 << 52) - 1


def _bar(day: int, minute: int) -> CandleBar:
    """Build one IST-session bar (stored UTC) with fixed Decimal prices."""
    ts = datetime(2026, 1, 5, 9, 16 + minute, tzinfo=_IST).astimezone(UTC) + timedelta(days=day)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _stream(n_days: int = 20, bars_per_day: int = 2) -> list[CandleBar]:
    """A deterministic multi-day stream spanning enough days for several folds."""
    return [_bar(d, m) for d in range(n_days) for m in range(bars_per_day)]


#: One shared bar stream + fixed fold layout for every measurement, so the only
#: dimension that changes between _SMALL and _LARGE is the mask-grid size.
_BARS = _stream()


def _sweep(n_masks: int) -> int:
    """Walk-forward ``n_masks`` over the SHARED bars/folds; return result count."""
    return len(
        walk_forward_sweep(
            _BARS,
            [_IMPOSSIBLE_MASK] * n_masks,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=_BARS,
            train_days=5,
            test_days=5,
            step_days=5,
        )
    )


class TestWalkForwardSweepPerMaskIsO1:
    def test_sweep_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        result = benchmark(lambda: _sweep(_WARM))
        assert result == _WARM

    def test_per_mask_ratio_is_o1(self) -> None:
        """Per-mask walk-forward latency is flat at 10× vs 1× grid size (O(1))."""

        def _time(n: int) -> float:
            _sweep(_WARM)  # warm the shared bar_bits / schedule build path
            t0 = time.perf_counter_ns()
            assert _sweep(n) == n
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"walk_forward_sweep per-mask ratio {ratio:.2f}× ({_LARGE} vs {_SMALL} "
            f"masks) exceeds 5× — the per-mask fold-scoring cost may have regressed "
            f"from O(1)-in-grid-size (e.g. a per-mask bar_bits/schedule rebuild). "
            f"t_small={t_small:.0f}ns, t_large={t_large:.0f}ns."
        )
