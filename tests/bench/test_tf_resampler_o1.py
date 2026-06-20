"""Perf bench for the 1m → 21-TF resampler — O(n) total / O(1) amortised per output bar.

VERIFICATION-HARDENING (no production change). Closes the precision pyramid's perf leg:
resampling a large multi-year synthetic 1-minute dataset to ALL 21 timeframes must scale
LINEARLY in the bar count, i.e. the amortised cost PER INPUT BAR must NOT grow as the
dataset grows. A regression that made the resampler super-linear (e.g. an O(n²) per-bucket
scan) would blow this ratio.

What this proves / does NOT prove (Golden Rule #10, honest envelope)
--------------------------------------------------------------------
* It DOES prove: the cost-per-1m-bar of a full 21-TF resample is roughly CONSTANT between
  a small (~94K-bar, ~1yr) and a large (~470K-bar, ~5yr) dataset — algorithmic O(n) total
  with no per-bar blow-up — gated by the repo's standard ``mean / mean`` ratio ceiling
  routed through :func:`tests.bench._o1_tiers.ratio_ceiling` (robust on shared CI, strict
  1.1× on the M4-Pro tier).
* It DOES prove: the large resample completes within a generous wall-clock bound, so a
  catastrophic slowdown fails CI.
* It does NOT prove: a specific absolute throughput SLA (machine-dependent), nor "O(1)
  total" (the resample is unavoidably O(n) — it reads every bar once). The per-OUTPUT-bar
  work is O(1) amortised; the per-INPUT-bar ratio gate below is the machine-checkable
  proxy for that.

These are NEW tests that must PASS against the EXISTING resampler.
"""

from __future__ import annotations

import time
from datetime import datetime

import polars as pl
from brutex.data.tf_resampler import resample_all_21_tfs
from brutex.data.timeframes import DAILY_TF_MINUTES

from tests.bench._o1_tiers import ratio_ceiling

#: 09:15 IST == 03:45 UTC — the fixed session anchor.
_SESSION_OPEN_UTC = datetime(2024, 1, 2, 3, 45)
_SESSION_MINUTES = DAILY_TF_MINUTES  # 375 1m bars per full trading day

#: Small vs big dataset sizes (whole trading days), ~1yr vs ~5yr of 1m bars.
_DAYS_SMALL = 250  # ~94K 1m bars
_DAYS_BIG = 1_250  # ~469K 1m bars

#: Generous wall-clock ceiling for the big (≈470K-bar) full-21-TF resample. Polars
#: groupby resamples a multi-year 1m series in well under a second on the reference
#: envelope; 30s is a CATASTROPHE gate, not an SLA (Golden Rule #10).
_BIG_WALL_CLOCK_CEILING_S = 30.0


def _make_multiday_1m(n_days: int) -> pl.DataFrame:
    """Build ``n_days`` full 375-minute 1m sessions, each anchored at 09:15 IST.

    Built vectorised (no per-row Python loop) so the FIXTURE cost doesn't dominate the
    measured resample. Distinct rising prices keep the aggregation non-degenerate.
    """
    total = n_days * _SESSION_MINUTES
    # Per-day minute offset (0..374) and a per-day index, combined into a UTC timestamp.
    # Built lazily in one `select` so every column is materialised together (no Expr
    # objects leaking into the DataFrame constructor).
    return (
        pl.select(
            idx=pl.int_range(0, total, dtype=pl.Int64),
        )
        .select(
            (
                pl.lit(_SESSION_OPEN_UTC, dtype=pl.Datetime)
                + pl.duration(
                    days=pl.col("idx") // _SESSION_MINUTES,
                    minutes=pl.col("idx") % _SESSION_MINUTES,
                )
            ).alias("ts_utc"),
            ((pl.col("idx") // _SESSION_MINUTES) * 1000 + (pl.col("idx") % _SESSION_MINUTES))
            .cast(pl.Float64)
            .alias("close"),
            (pl.col("idx") % _SESSION_MINUTES).cast(pl.Int64).alias("_minute"),
        )
        .select(
            pl.col("ts_utc"),
            (pl.col("close") - 0.5).alias("open"),
            (pl.col("close") + 1.0).alias("high"),
            (pl.col("close") - 1.0).alias("low"),
            pl.col("close"),
            (pl.col("_minute") + 1).alias("volume"),
        )
    )


def _time_resample_all_21(df: pl.DataFrame) -> float:
    """Mean nanoseconds PER INPUT 1m bar to resample to all 21 TFs (after a warm-up)."""
    n = df.height
    _ = resample_all_21_tfs(df)  # warm up (import + jit-ish caches)
    t0 = time.perf_counter_ns()
    result = resample_all_21_tfs(df)
    t1 = time.perf_counter_ns()
    assert len(result) == 21  # sanity: all TFs produced
    return (t1 - t0) / n


def test_resample_all_21_cost_per_bar_is_flat_across_dataset_size() -> None:
    """Per-1m-bar cost at ~470K bars must hold the ceiling vs ~94K bars (linear scaling).

    A super-linear regression (e.g. an O(n²) per-bucket scan replacing the groupby)
    makes the per-bar cost grow with the dataset and blows this ratio.
    """
    t_small = _time_resample_all_21(_make_multiday_1m(_DAYS_SMALL))
    t_big = _time_resample_all_21(_make_multiday_1m(_DAYS_BIG))
    ratio = t_big / max(t_small, 1e-9)

    # 5× robust ceiling on shared CI (groupby has fixed per-call overhead that amortises
    # DOWN as n grows, so the ratio is typically < 1; the gate catches a blow-UP),
    # tightened to the strict 1.1× M4-Pro tier (DEC-O1-STRICT-TIER-001).
    assert ratio < ratio_ceiling(5.0), (
        f"per-bar resample cost ratio {ratio:.2f}× ({_DAYS_BIG} vs {_DAYS_SMALL} days) "
        f"exceeds the CI ceiling — the 21-TF resample may have regressed away from O(n) "
        f"total. t_small={t_small:.1f}ns/bar, t_big={t_big:.1f}ns/bar."
    )


def test_large_resample_completes_within_wall_clock_bound() -> None:
    """The ≈470K-bar full-21-TF resample finishes within the catastrophe ceiling."""
    df = _make_multiday_1m(_DAYS_BIG)
    _ = resample_all_21_tfs(df)  # warm up
    t0 = time.perf_counter()
    result = resample_all_21_tfs(df)
    elapsed = time.perf_counter() - t0
    assert len(result) == 21
    # The daily TF must collapse to exactly one bar per trading day (sanity on the result).
    assert result[DAILY_TF_MINUTES].height == _DAYS_BIG
    assert elapsed < _BIG_WALL_CLOCK_CEILING_S, (
        f"full 21-TF resample of {_DAYS_BIG * _SESSION_MINUTES} 1m bars took "
        f"{elapsed:.2f}s, over the {_BIG_WALL_CLOCK_CEILING_S}s catastrophe ceiling."
    )
