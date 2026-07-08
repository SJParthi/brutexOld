"""O(1) bench for evaluate-time indicator VALUE lookup — Golden Rule #5 hot path #2.

DEC-O1-BENCH-GAPS-002 (closes follow-up F-2 of DEC-WORKSPACE-VERIFY-002,
``docs/MIGRATION.md`` #609): "indicator value lookup" is named as an O(1) hot
path by CLAUDE.md §2 rule 5, but until this file it had no dedicated isolated
ratio gate. The surface gated here is the real evaluate-time shape: the engine
holds the precomputed per-day levels in a ``dict[date, PivotLevels |
FibLevels]`` and, per evaluated bar, resolves that day's levels then reads one
labelled value (``PivotLevels.get("R2")`` / ``FibLevels.get("62", direction)``,
``brutex/indicators/pivots.py`` / ``fib_levels.py``).

Both lookups must stay latency-independent of how many trading days of levels
are loaded. The gate is the standard ratio ``mean_ns(N_big) /
mean_ns(N_small) ≤ ceiling`` — 5× robust on the shared GHA runner. On the
M4-Pro strict tier the day-map value lookups (pivot / fib) route through
:func:`tests.bench._o1_tiers.cache_tier_ceiling` (the DEC-O1-CACHE-TIER-001
measured 3× cache allowance) because the ``N_BIG`` map (100K days of ``Pivot`` /
``Fib`` level objects) SPILLS the CPU cache into DRAM — the per-lookup cost is
bounded by a CONSTANT memory-tier factor (~2.5–2.7× measured, a cache-cliff not
an O(n) regression), exactly the allowance the throughput canary routes its
1M-elem dict lookups through (DEC-O1-BENCH-GATE-COMPLETE-001). The VWAP
``update`` running-sum path keeps the raw 1.1× via :func:`ratio_ceiling`: its
state is a fixed-size object, so it does NOT cross a cache tier as the session
deepens. A genuine O(n) regression in either still blows well past the ceiling.

Honest envelope (Golden Rule #10): the per-call cost of ``.get`` includes
building a CONSTANT-SIZE label map (11 pivot / 12 fib entries) — that is a
fixed per-call constant, O(1) in the data dimension this gate pins (days
loaded). Setup synthesizes the level values directly; the level ARITHMETIC is
separately golden-tested bit-identically against the shared Rust core
(DEC-SHARED-CORE-001) and is out of scope here.
"""

from __future__ import annotations

import time
from datetime import date

from brutex.indicators.fib_levels import FibDirection, FibLevels
from brutex.indicators.pivots import PivotLevels

from tests.bench._o1_tiers import cache_tier_ceiling, ratio_ceiling

#: Number of trading days of precomputed levels for the small vs big tiers.
N_SMALL = 1_000
N_BIG = 100_000

#: Timed lookups per tier (day/key pattern is identical for both tiers).
ITERATIONS = 200_000

#: Prime stride so the touched days spread across the map.
_STRIDE = 9_973

_BASE_ORDINAL = date(2021, 1, 1).toordinal()


def _make_pivot_days(n: int) -> dict[date, PivotLevels]:
    """Build *n* days of synthetic-but-shaped pivot levels keyed by day."""
    levels: dict[date, PivotLevels] = {}
    for i in range(n):
        day = date.fromordinal(_BASE_ORDINAL + i)
        levels[day] = PivotLevels(
            trading_day=day,
            pdh_paisa=2_050_000 + i,
            pdl_paisa=2_000_000 + i,
            pdc_paisa=2_030_000 + i,
            r5_paisa=2_150_000 + i,
            r4_paisa=2_130_000 + i,
            r3_paisa=2_110_000 + i,
            r2_paisa=2_090_000 + i,
            r1_paisa=2_070_000 + i,
            p_paisa=2_026_667 + i,
            s1_paisa=2_010_000 + i,
            s2_paisa=1_990_000 + i,
            s3_paisa=1_970_000 + i,
            s4_paisa=1_950_000 + i,
            s5_paisa=1_930_000 + i,
        )
    return levels


def _make_fib_days(n: int) -> dict[date, FibLevels]:
    """Build *n* days of synthetic-but-shaped fib levels keyed by day."""
    levels: dict[date, FibLevels] = {}
    for i in range(n):
        day = date.fromordinal(_BASE_ORDINAL + i)
        levels[day] = FibLevels(
            trading_day=day,
            pdh_paisa=2_050_000 + i,
            pdl_paisa=2_000_000 + i,
            range_paisa=50_000,
            retrace_level_at_pdh_paisa=2_050_000 + i,
            retrace_23_paisa=2_038_200 + i,
            retrace_38_paisa=2_030_900 + i,
            retrace_50_paisa=2_025_000 + i,
            retrace_62_paisa=2_019_100 + i,
            retrace_78_paisa=2_010_700 + i,
            retrace_level_at_pdl_paisa=2_000_000 + i,
            ext_up_127_paisa=2_063_600 + i,
            ext_up_162_paisa=2_080_900 + i,
            ext_up_200_paisa=2_100_000 + i,
            ext_up_262_paisa=2_130_900 + i,
            ext_up_424_paisa=2_211_800 + i,
            ext_down_127_paisa=1_986_400 + i,
            ext_down_162_paisa=1_969_100 + i,
            ext_down_200_paisa=1_950_000 + i,
            ext_down_262_paisa=1_919_100 + i,
            ext_down_424_paisa=1_838_200 + i,
        )
    return levels


def _day_pattern(n: int) -> list[date]:
    """A spread, size-independent pattern of days to look up."""
    return [date.fromordinal(_BASE_ORDINAL + (i * _STRIDE) % n) for i in range(ITERATIONS)]


def _time_pivot_lookup(levels: dict[date, PivotLevels]) -> float:
    """Mean ns per ``levels[day].get("R2")`` over the spread day pattern."""
    days = _day_pattern(len(levels))
    for day in days[:2_000]:  # warm up
        _ = levels[day].get("R2")
    t0 = time.perf_counter_ns()
    for day in days:
        _ = levels[day].get("R2")
    t1 = time.perf_counter_ns()
    return (t1 - t0) / ITERATIONS


def _time_fib_lookup(levels: dict[date, FibLevels]) -> float:
    """Mean ns per ``levels[day].get("62", BULLISH)`` over the day pattern."""
    days = _day_pattern(len(levels))
    direction = FibDirection.BULLISH
    for day in days[:2_000]:  # warm up
        _ = levels[day].get("62", direction)
    t0 = time.perf_counter_ns()
    for day in days:
        _ = levels[day].get("62", direction)
    t1 = time.perf_counter_ns()
    return (t1 - t0) / ITERATIONS


def test_pivot_value_lookup_is_o1_in_days_loaded() -> None:
    """Pivot lookup latency at 100K days must hold the ceiling vs 1K days.

    A regression that made the per-bar level resolution scale with the
    loaded history (e.g. replacing the day map with a list scan) blows
    this ratio.
    """
    t_small = _time_pivot_lookup(_make_pivot_days(N_SMALL))
    t_big = _time_pivot_lookup(_make_pivot_days(N_BIG))
    ratio = t_big / max(t_small, 1.0)

    assert ratio < cache_tier_ceiling(5.0), (
        f"pivot value-lookup latency ratio {ratio:.2f}× ({N_BIG} vs {N_SMALL} "
        f"days) exceeds the CI ceiling — the indicator lookup may have "
        f"regressed away from O(1). t_small={t_small:.1f}ns, t_big={t_big:.1f}ns."
    )


def test_fib_value_lookup_is_o1_in_days_loaded() -> None:
    """Fib lookup latency at 100K days must hold the same ceiling vs 1K days."""
    t_small = _time_fib_lookup(_make_fib_days(N_SMALL))
    t_big = _time_fib_lookup(_make_fib_days(N_BIG))
    ratio = t_big / max(t_small, 1.0)

    assert ratio < cache_tier_ceiling(5.0), (
        f"fib value-lookup latency ratio {ratio:.2f}× ({N_BIG} vs {N_SMALL} "
        f"days) exceeds the CI ceiling — the indicator lookup may have "
        f"regressed away from O(1). t_small={t_small:.1f}ns, t_big={t_big:.1f}ns."
    )


def _time_vwap_update(prefilled_bars: int) -> float:
    """Mean ns per ``trading_core.VwapState.update`` AFTER the session already
    accumulated ``prefilled_bars`` bars — proving the running-sum cost is O(1)
    in the number of bars already seen (NOT a re-sum of session history, PR-2
    risk #2). Times the PRODUCTION Rust pyclass ``stage1a`` drives — the single
    VWAP source since DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 deleted the
    hand-synced Python twin this bench previously timed."""
    from datetime import date as _date
    from typing import Any

    import trading_core

    state: Any = trading_core.VwapState()
    day_ordinal = _date(2024, 1, 2).toordinal()
    base_tod = 9 * 3600 + 16 * 60
    # Prefill the session so cum_pv/cum_vol carry `prefilled_bars` of history.
    for k in range(prefilled_bars):
        state.update(
            2_000_000 + (k % 1000),
            100 + (k % 50),
            False,  # is_pre_open
            1,  # session_key
            day_ordinal,
            base_tod + k,
        )
    iters = 200_000
    tod = base_tod + prefilled_bars
    for k in range(2_000):  # warm up
        state.update(2_000_000, 100, False, 1, day_ordinal, tod + k)
    t0 = time.perf_counter_ns()
    for k in range(iters):
        state.update(2_000_000, 100, False, 1, day_ordinal, tod + 2_000 + k)
    t1 = time.perf_counter_ns()
    return (t1 - t0) / iters


def test_vwap_update_is_o1_in_session_bars() -> None:
    """VWAP ``update`` latency with a 100K-bar-deep session must hold the ceiling
    vs a 1K-bar-deep one — the running cum(pv)/cum(vol) is O(1) per bar (two adds
    + one divide, in Rust), never a re-sum of the session (PR-2 —
    DEC-NIFTY-INDICATORS-001).
    """
    t_small = _time_vwap_update(N_SMALL)
    t_big = _time_vwap_update(N_BIG)
    ratio = t_big / max(t_small, 1.0)

    assert ratio < ratio_ceiling(5.0), (
        f"VWAP update latency ratio {ratio:.2f}× ({N_BIG} vs {N_SMALL} session "
        f"bars) exceeds the CI ceiling — VWAP may have regressed away from O(1) "
        f"per bar. t_small={t_small:.1f}ns, t_big={t_big:.1f}ns."
    )
