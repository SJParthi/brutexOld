"""O(1)-per-bar benchmark gate for the synthetic generator (CLAUDE.md §2 r5).

Two ratio gates, both routed through the shared
:func:`tests.bench._o1_tiers.ratio_ceiling` tier helper:

1. A single bar's generation cost does NOT depend on how far into history its
   timestamp is — a bar in 2099 costs the same as a bar in 2021 (no scan of
   prior bars).
2. The mean per-bar cost of a large batch matches that of a small batch — the
   generator is O(1) per bar, O(n) total, with no super-linear blow-up.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from brutex.adapters.synthetic.constants import SYMBOL_SPECS
from brutex.adapters.synthetic.generator import generate_bar

from tests.bench._o1_tiers import ratio_ceiling

#: Batch sizes for the small vs big per-bar tiers.
N_SMALL = 2_000
N_BIG = 20_000
#: Warm-up iterations before each timed loop.
_WARMUP = 64
#: A spec with traded volume (exercises the full price + volume path).
_SPEC = next(s for s in SYMBOL_SPECS if s.symbol == "SYNTH")
#: A "near" epoch-anchored start and a "far" (decades later) one.
_NEAR = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
_FAR = datetime(2099, 1, 4, 3, 45, tzinfo=UTC)
_ONE_MIN = timedelta(minutes=1)


def _time_batch(start: datetime, n: int) -> float:
    """Mean ns to generate one bar over *n* consecutive minute timestamps."""
    for i in range(min(_WARMUP, n)):  # warm up
        generate_bar(seed=7, spec=_SPEC, interval="1minute", timestamp=start + i * _ONE_MIN)
    t0 = time.perf_counter_ns()
    for i in range(n):
        generate_bar(seed=7, spec=_SPEC, interval="1minute", timestamp=start + i * _ONE_MIN)
    t1 = time.perf_counter_ns()
    return (t1 - t0) / n


@pytest.mark.bench
def test_generate_bar_is_o1_in_timestamp_position() -> None:
    near = _time_batch(_NEAR, N_SMALL)
    far = _time_batch(_FAR, N_SMALL)
    ratio = far / max(near, 1.0)
    assert ratio < ratio_ceiling(5.0), (
        f"per-bar cost depends on timestamp position: far/near = {ratio:.2f}× "
        f"exceeds the O(1) ceiling — generation must not scan history"
    )


@pytest.mark.bench
def test_generate_bar_is_o1_in_batch_size() -> None:
    small = _time_batch(_NEAR, N_SMALL)
    big = _time_batch(_NEAR, N_BIG)
    ratio = big / max(small, 1.0)
    assert ratio < ratio_ceiling(5.0), (
        f"per-bar cost grows with batch size: big/small = {ratio:.2f}× exceeds the O(1) ceiling"
    )
