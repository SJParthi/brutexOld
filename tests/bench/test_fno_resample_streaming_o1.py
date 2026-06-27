"""O(1)-streaming bench for the F&O resample (DEC-FNO-TF-DERIVE-OI-001).

The per-contract resample is a SINGLE pass over that contract's sorted 1-minute
bars with O(1) memory per open bucket (one running open/high/low/close/volume/
last-OI accumulator), so the AMORTISED per-bar cost is FLAT as the bar count
scales 1K → 1M. This gate measures ``mean_ns_per_bar`` at the two tiers and
asserts the ratio holds the ceiling — a regression that made the per-bar cost
scale with the total (e.g. a quadratic re-scan) blows it.

Honest envelope (Golden Rule #10): this is explicitly NOT an O(1)-TOTAL claim —
total work is O(bars) per contract (linear). The gate proves the PER-BAR cost is
constant (the streaming/linear-pass property), which is what CLAUDE.md §5's hot-op
O(1) discipline asks for; the absolute total scales with the input as stated.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import polars as pl
from brutex.data.tf_resampler import resample_1m_to_tf

from tests.bench._o1_tiers import ratio_ceiling

#: Bar-count tiers (kept inside one synthetic multi-year span so the 5m grid has
#: many buckets at the big tier — a real per-bar amortised measure).
N_SMALL = 1_000
N_BIG = 1_000_000

# A fixed anchor; bars step one minute, so N_BIG spans well over a year of
# wall-clock minutes — fine for a synthetic per-bar throughput measure (the
# session-anchor math is pure column arithmetic, never an O(bars) re-scan).
_ANCHOR_UTC = datetime(2020, 1, 1, 3, 45, tzinfo=UTC)


def _make_fno_1m(n: int) -> pl.DataFrame:
    """Build *n* synthetic F&O 1-minute bars (with an ``open_interest`` column)."""
    return pl.DataFrame(
        {
            "ts_utc": [_ANCHOR_UTC + timedelta(minutes=i) for i in range(n)],
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [10] * n,
            "open_interest": [1000 + (i % 50) for i in range(n)],
        },
        schema={
            "ts_utc": pl.Datetime(time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "open_interest": pl.Int64,
        },
    )


def _time_per_bar(df: pl.DataFrame) -> float:
    """Mean ns per source bar to resample the whole frame to a 5m grid."""
    n = df.height
    _ = resample_1m_to_tf(df.head(min(256, n)), 5)  # warm up the polars engine
    t0 = time.perf_counter_ns()
    out = resample_1m_to_tf(df, 5)
    t1 = time.perf_counter_ns()
    assert out.height > 0
    return (t1 - t0) / n


def test_resample_per_bar_cost_is_flat_1k_to_1m() -> None:
    """Per-bar resample cost at 1M bars holds the ceiling vs 1K bars (linear pass)."""
    t_small = _time_per_bar(_make_fno_1m(N_SMALL))
    t_big = _time_per_bar(_make_fno_1m(N_BIG))
    ratio = t_big / max(t_small, 1.0)

    # 5× robust shared-CI ceiling, tightened to the strict 1.1× M4-Pro tier
    # (DEC-O1-STRICT-TIER-001). A quadratic re-scan regression blows this.
    assert ratio < ratio_ceiling(5.0), (
        f"F&O resample per-bar latency ratio {ratio:.2f}× ({N_BIG} vs {N_SMALL} "
        f"bars) exceeds the CI ceiling — the resample may have regressed away "
        f"from a single linear pass. t_small={t_small:.2f}ns, t_big={t_big:.2f}ns."
    )
