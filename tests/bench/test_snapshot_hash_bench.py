"""Bench — the full-content snapshot hash cost pin (DEC-SNAPSHOT-HASH-FULL-001).

The HONEST complexity gate: ``data_snapshot_hash`` is O(total loaded bars)
per launch — deliberately NOT an O(1)-total claim (Golden Rule #10; the
docstring says so). What must stay flat is the **amortized per-bar** cost:
hashing 20× the bars must cost ~20× the wall time, i.e. the per-bar ratio
``(t_big/N_big) / (t_small/N_small)`` stays ≤ the tiered ceiling. A
super-linear regression (e.g. an accidental list-accumulating preimage that
reallocs quadratically) blows the ratio and fails here.

Median-of-5 timing + a robust shared-CI ceiling via the COMMON
:func:`tests.bench._o1_tiers.ratio_ceiling` (strict 1.1× on the M4-Pro tier).
"""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.stage3_wire import data_snapshot_hash

from tests.bench._o1_tiers import ratio_ceiling

_IST = timezone(timedelta(hours=5, minutes=30))

_N_SMALL = 2_000
_N_BIG = 40_000


def _bars(n: int) -> list[CandleBar]:
    base = datetime(2026, 1, 5, 9, 16, tzinfo=_IST).astimezone(UTC)
    return [
        CandleBar(
            timestamp=base + timedelta(minutes=i),
            open=Decimal("100.00"),
            high=Decimal("105.00"),
            low=Decimal("95.00"),
            close=Decimal("100.00") + Decimal(i % 400) / 100,
            volume=1000 + i,
            open_interest=None if i % 3 == 0 else i,
        )
        for i in range(n)
    ]


def _median_seconds(bars: list[CandleBar], repeats: int = 5) -> float:
    slices = {("NIFTY", "1minute"): bars}
    samples: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        data_snapshot_hash(
            exchange="NSE",
            segment="INDEX",
            start="2026-01-01",
            end="2026-12-31",
            slices=slices,
        )
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def test_per_bar_amortized_hash_cost_is_flat() -> None:
    small = _bars(_N_SMALL)
    big = _bars(_N_BIG)
    per_bar_small = _median_seconds(small) / _N_SMALL
    per_bar_big = _median_seconds(big) / _N_BIG
    ratio = per_bar_big / per_bar_small
    ceiling = ratio_ceiling(3.0)
    assert ratio < ceiling, (
        f"per-bar snapshot-hash cost grew {ratio:.2f}x from N={_N_SMALL} to "
        f"N={_N_BIG} (ceiling {ceiling}x) — the O(N)-total/O(1)-per-bar "
        "envelope regressed"
    )
