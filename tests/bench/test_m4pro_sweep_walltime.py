"""M4-Pro real-trade-walk sweep wall-time bench (DEC-M4PRO-WALLTIME-BENCH-001).

This bench closes a precise GAP that the sibling
``tests/bench/test_full_sweep_extrapolation.py`` leaves open. That bench projects
the 33-billion-trial wall time from the **resident + rayon-parallel batched
mask-AND kernel + blake3** per-trial proxy ONLY — it deliberately EXCLUDES the
per-combo SL/TP/15:20-forced-exit **trade walk**, which the production sweep runs
once per surviving mask and which dominates the real per-combo cost. So the
≤4-day number that bench prints is the *bandwidth-bound mask-AND envelope*, NOT
the full real-sweep wall time (its own module docstring states this).

This file measures the OTHER half: it times the REAL trade-walk
(:meth:`fno_math.ResidentSweepSim.count_trades` — entry signal, SL/TP, 15:20
forced exit, exit precedence) over a representative TF-mix bar window, then
projects wall time for BOTH:

* the **full** 33-billion-trial grid (the COMMON ``_FULL_SWEEP_TRIALS`` reused
  from ``test_full_sweep_extrapolation.py`` — NOT a new constant), and
* the **conflict-pruned** path — the survivors after the upfront conflict prune
  documented in :mod:`brutex.vocabulary.conflicts` (``~2.9M → ~1.5M`` masks,
  i.e. a ``~50%`` reduction; the REAL repo prune, derived from that COMMON
  docstring, never a hardcoded magic number).

Honesty (Golden Rule #1 + #10): an earlier brief referenced ``243.1B`` trials, a
``0.99-day`` projection and a ``6.4%`` prune — **none of those three numbers
exist anywhere in this repo or its history**. This bench therefore uses the REAL
COMMON values: the 33B grid (``_FULL_SWEEP_TRIALS``,
``docs/TRACK1_PERFORMANCE_BUDGET.md`` §5) and the ~50% conflict prune
(``brutex/vocabulary/conflicts.py:5-6``). The measured number printed here on a
shared / non-M4-Pro runner is ILLUSTRATIVE only; the SLA-meaningful number is
produced on the M4 Pro self-hosted runner. The gated timing body runs ONLY when
``BRUTEX_M4PRO_ABS=1`` (the SAME env gate as the sibling benches, set in
``.github/workflows/bench-m4pro.yml``); everywhere else it skips. The pure
projection math is extracted into module-level helpers that a NON-gated
``@pytest.mark.bench`` test exercises on every runner, so the file's logic is
verified without the M4-Pro runner.

This is an HONEST MEASUREMENT, not a perf gate: the gated body asserts only basic
sanity (positive throughput, finite per-trial µs, finite projected days). It does
NOT add a hard wall-time assertion — the trade-walk's per-mask trade COUNT (hence
its cost) depends on the data, so a hard ceiling here would be flaky; the sibling
mask-AND bench keeps the hard 4-day SLA gate.
"""

from __future__ import annotations

import math
import os
import time
from datetime import time as dt_time
from typing import TYPE_CHECKING, Final

import pytest

fno_math = pytest.importorskip("fno_math", reason="resident trade-sim kernel needs the built wheel")

from brutex.calendars.nse import is_forced_exit_bar  # noqa: E402
from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES  # noqa: E402

# Reuse the COMMON 33B grid envelope — do NOT redefine it (Golden Rule #11).
# Intentional friend import: the 33B figure lives in exactly ONE place
# (`test_full_sweep_extrapolation._FULL_SWEEP_TRIALS`); reusing it keeps the two
# wall-time benches in lockstep, so the private-name access is deliberate.
from tests.bench.test_full_sweep_extrapolation import (  # noqa: E402
    _FULL_SWEEP_TRIALS,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:  # pragma: no cover - typing only (annotations are lazy)
    # `fno_math` above is a runtime variable (importorskip result); pyright forbids
    # it in a type expression. Import the class name for annotations only.
    from fno_math import ResidentSweepSim

# Env-var gate — set to "1" on the M4 Pro self-hosted runner (bench-m4pro.yml).
# The SAME gate the sibling absolute/SLA benches use; no new `m4pro` marker.
_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"

# Conflict-prune survivor fraction — DERIVED from the COMMON
# `brutex/vocabulary/conflicts.py:5-6` docstring ("drops mask count from ~2.9M to
# ~1.5M survivors"), which is the SINGLE source of truth for the prune. We encode
# the two documented mask counts (not a magic fraction) so this stays traceable;
# 1.5/2.9 ≈ 0.517 survivors ⇒ ~48% pruned. NOT the non-existent "6.4%".
_CONFLICT_MASKS_BEFORE_PRUNE: Final[int] = 2_900_000
_CONFLICT_MASKS_AFTER_PRUNE: Final[int] = 1_500_000

# Number of session bars in one NSE day at the 1-day timeframe = the full session
# span (375 minutes = 09:15-15:30 IST), per `brutex/data/timeframes.py`. Used to
# derive a representative per-TF bar count below.
_SESSION_MINUTES: Final[int] = DAILY_TF_MINUTES  # 375

# Representative number of trading sessions to size the bench window against (a
# fraction of a real ~250-session year — chosen so the per-TF-mean window
# (`_representative_n_bars`), batched with `_BATCH_MASKS`, crosses the kernel's
# 2^20 parallel threshold while the bench stays fast). At 75 sessions the mean
# per-TF count is ~4.6K bars ⇒ 256 × ~4.6K ≈ 1.17M mask·bar > 2^20.
_REPRESENTATIVE_SESSIONS: Final[int] = 75

# Masks per measured batch. Sized together with the bar count so
# `n_masks × n_bars > 2^20` (PARALLEL_WORK_THRESHOLD) — the production rayon arm.
_BATCH_MASKS: Final[int] = 256


def _m4pro_abs_active() -> bool:
    """Return ``True`` iff ``BRUTEX_M4PRO_ABS=1`` is set (the M4 Pro runner)."""
    return os.environ.get(_ENV_M4PRO_ABS, "").strip() == "1"


def conflict_prune_fraction() -> float:
    """Fraction of masks REMOVED by the upfront conflict prune (the real ~50%).

    Derived from the COMMON `brutex/vocabulary/conflicts.py` docstring
    (``~2.9M → ~1.5M`` survivors). Returns the pruned-away fraction in ``[0, 1)``;
    ``1 - fraction`` is the survivor fraction the pruned-path projection uses.
    """
    return 1.0 - _CONFLICT_MASKS_AFTER_PRUNE / _CONFLICT_MASKS_BEFORE_PRUNE


def pruned_trials(full_trials: float, prune_fraction: float) -> float:
    """Surviving trial count after pruning ``prune_fraction`` of the full grid.

    ``prune_fraction`` is the fraction REMOVED (``0`` ⇒ no prune ⇒ the full grid).
    """
    return full_trials * (1.0 - prune_fraction)


def project_walltime(per_trial_seconds: float, n_trials: float) -> dict[str, float]:
    """Project wall time for ``n_trials`` at ``per_trial_seconds`` each.

    Returns a dict with ``seconds`` / ``hours`` / ``days`` — the pure arithmetic a
    wall-time projection needs, with NO timing or environment dependency (so a
    non-gated unit test covers it on every runner).
    """
    seconds = per_trial_seconds * n_trials
    return {
        "seconds": seconds,
        "hours": seconds / 3_600.0,
        "days": seconds / 86_400.0,
    }


def _representative_n_bars() -> int:
    """Representative per-TF session-bar count, averaged across the 21 TFs.

    A single NSE session is ``_SESSION_MINUTES`` (375 min, 09:15-15:30 IST) wide
    (`brutex/data/timeframes.py`). At a timeframe of ``m`` minutes there are
    ``ceil(375 / m)`` bars per session, so over ``_REPRESENTATIVE_SESSIONS``
    sessions a TF holds ``ceil(375 / m) × sessions`` bars. Averaging that count
    across all 21 ``ALL_TF_MINUTES`` gives ONE representative window size that
    spans the whole TF mix (1m dense → 1d sparse) without hardcoding a magic
    number — the count is DERIVED from `timeframes.ALL_TF_MINUTES`.

    The returned size is large enough that ``_BATCH_MASKS × n_bars`` crosses the
    kernel's ``PARALLEL_WORK_THRESHOLD`` (2^20), so the measurement exercises the
    production rayon-parallel arm (asserted in the gated test).
    """
    per_tf_bars = [
        math.ceil(_SESSION_MINUTES / m) * _REPRESENTATIVE_SESSIONS for m in ALL_TF_MINUTES
    ]
    return max(1, round(sum(per_tf_bars) / len(per_tf_bars)))


def _make_sim(n_bars: int) -> ResidentSweepSim:
    """Deterministic resident sim mirroring ``test_trade_sim_throughput._make_sim``.

    A gently oscillating int64-paisa OHLC window with sparse ``bar_bits`` so masks
    fire intermittently, plus a 15:20-IST forced-exit flag on the session-close
    bars (via :func:`brutex.calendars.nse.is_forced_exit_bar`, the SAME seam
    :class:`brutex.engine.rust_trade_sim.ResidentTradeSim` uses), so the kernel's
    forced-exit branch is exercised — representative of the production trade walk.
    """
    seed = 0x9E3779B97F4A7C15
    base = 1_000_000  # ₹10,000.00 in paisa
    open_p: list[int] = []
    high_p: list[int] = []
    low_p: list[int] = []
    close_p: list[int] = []
    bar_bits: list[int] = []
    is_forced: list[bool] = []
    session_idx: list[int] = []
    # The synthetic window is 1-minute bars walking forward from 09:15 IST and
    # wrapping at the 375-minute session, so the per-bar forced-exit flag matches
    # the real 15:20 cutoff for a 1-minute timeframe. Each 375-minute wrap is a
    # NEW session (`session_idx = i // 375`), mirroring the production per-bar
    # IST-date ordinals so the kernel's cross-session pending-entry gate
    # (R2-OC-2) is exercised at its real per-day cadence.
    for i in range(n_bars):
        wobble = ((seed * (i + 1)) >> 13) % 200 - 100
        o = base + wobble
        open_p.append(o)
        high_p.append(o + 150)
        low_p.append(o - 150)
        close_p.append(o + (wobble % 30))
        bar_bits.append((seed * (i + 7)) & 0x000F_FFFF)
        minute_of_session = i % _SESSION_MINUTES
        total_minutes = 9 * 60 + 15 + minute_of_session  # 09:15 IST + offset
        bar_time = dt_time(hour=total_minutes // 60, minute=total_minutes % 60)
        is_forced.append(is_forced_exit_bar(bar_time, 1))
        session_idx.append(i // _SESSION_MINUTES)
    # Dual-feed exec-grid ctor (DEC-FILL-EXEC-GRID-1M-001, #1989): this 1-minute
    # window IS its own execution grid — the tf==1 fixed point where decision
    # bar j-1 fills on 1m bar j (`arm_source[j] = j-1`, bar 0 unarmed), exactly
    # the `ExecGridMap.arm_source` a contiguous decision==exec stream yields at
    # tf_minutes == 1. Mirrors `test_trade_sim_throughput._make_sim` /
    # `test_exec_grid_dual_feed_bench._tf1_arm_source`. `bar_bits` stays the
    # per-decision (== per-1m at tf=1) firing bits already built above.
    arm_source: list[int] = [-1, *range(n_bars - 1)]
    # `fno_math` is the importorskip runtime variable, so the constructor's return
    # is `Any` to mypy; capture it in a precisely-typed local before returning
    # (the no-`# type: ignore` pattern of DEC-M4PRO-BENCH-SURFACE-001 #979).
    sim: ResidentSweepSim = fno_math.ResidentSweepSim(
        open_p, high_p, low_p, close_p, is_forced, session_idx, arm_source, bar_bits
    )
    return sim


def _make_masks(n: int) -> list[int]:
    return [((i * 0x100000001B3) | (1 << (i % 20))) & 0x000F_FFFF for i in range(n)]


def _per_trial_seconds(n_bars: int, n_masks: int) -> float:
    """Best-of-N wall time per trial for the REAL ``count_trades`` trade walk.

    One ``count_trades`` call walks ``n_masks`` masks over the resident window,
    each producing a per-mask round-trip trade COUNT (entry / SL / TP / 15:20
    forced exit). One "trial" = one mask walked over the window, so per-trial cost
    = batch wall time / ``n_masks``. Best-of-N rejects upward-only OS noise.
    """
    sim = _make_sim(n_bars)
    masks = _make_masks(n_masks)
    sl, tp = 8_000, 12_000  # ₹80 / ₹120 stop/target — trades close, bounded count
    sim.count_trades(masks, True, sl, tp)  # warm allocator + caches
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        sim.count_trades(masks, True, sl, tp)
        best = min(best, time.perf_counter() - start)
    return best / n_masks


@pytest.mark.slow
@pytest.mark.bench
def test_m4pro_real_trade_walk_walltime() -> None:
    """Time the REAL trade-walk + project full + pruned wall time (M4-Pro gated).

    Skips unless ``BRUTEX_M4PRO_ABS=1`` (the M4 Pro self-hosted runner). On every
    other runner the measured number is illustrative-only, so the heavy timing is
    skipped; the pure projection helpers are still covered by
    :func:`test_walltime_projection_helpers` below. The gated body asserts only
    basic sanity — this is an honest MEASUREMENT, not a hard perf gate.
    """
    if not _m4pro_abs_active():
        pytest.skip(
            f"{_ENV_M4PRO_ABS}=1 not set — the real-trade-walk wall-time bench runs "
            "only on the M4 Pro self-hosted runner. The pure projection helpers are "
            "covered on every runner by test_walltime_projection_helpers."
        )

    n_bars = _representative_n_bars()
    n_masks = _BATCH_MASKS
    # Representative of the production sweep: cross the rayon-parallel arm.
    assert n_masks * n_bars > (1 << 20), (
        f"bench window {n_masks} masks × {n_bars} bars = {n_masks * n_bars} ≤ 2^20 — "
        "would run the serial arm, not the production rayon-parallel path."
    )

    per_trial = _per_trial_seconds(n_bars, n_masks)
    per_trial_us = per_trial * 1e6
    trials_per_sec = 1.0 / per_trial

    prune_fraction = conflict_prune_fraction()
    full = project_walltime(per_trial, float(_FULL_SWEEP_TRIALS))
    pruned_n = pruned_trials(float(_FULL_SWEEP_TRIALS), prune_fraction)
    pruned = project_walltime(per_trial, pruned_n)

    print(
        f"\nM4-Pro real-trade-walk sweep wall-time (DEC-M4PRO-WALLTIME-BENCH-001):\n"
        f"  path                  = ResidentSweepSim.count_trades (SL/TP/15:20 forced exit)\n"
        f"  representative window = {n_bars} bars × {n_masks} masks "
        f"({n_masks * n_bars:,} mask·bar > 2^20 parallel arm)\n"
        f"  per-trial (trade-walk)= {per_trial_us:.3f} µs/trial\n"
        f"  measured throughput   = {trials_per_sec:,.0f} trials/sec\n"
        f"  --- projected wall time ---\n"
        f"  FULL grid  ({_FULL_SWEEP_TRIALS:,} trials):\n"
        f"      = {full['seconds']:,.0f} s = {full['hours']:,.1f} h = {full['days']:,.2f} DAYS\n"
        f"  PRUNED path (~{prune_fraction * 100:.0f}% conflict-pruned, "
        f"{pruned_n:,.0f} trials):\n"
        f"      = {pruned['seconds']:,.0f} s = {pruned['hours']:,.1f} h = "
        f"{pruned['days']:,.2f} DAYS\n"
        f"  NOTE: this is the DOMINANT trade-walk cost that "
        f"test_full_sweep_extrapolation's day-number EXCLUDES.\n"
    )

    # HONEST sanity only — not a hard wall-time gate (trade count is data-dependent).
    assert trials_per_sec > 0.0
    assert math.isfinite(per_trial_us) and per_trial_us > 0.0
    assert math.isfinite(full["days"]) and full["days"] > 0.0
    assert math.isfinite(pruned["days"]) and pruned["days"] > 0.0
    assert pruned["days"] < full["days"]  # pruning can only reduce wall time


@pytest.mark.bench
def test_walltime_projection_helpers() -> None:
    """Cover the pure projection math on EVERY runner (no M4-Pro gate).

    Exercises :func:`project_walltime`, :func:`pruned_trials`,
    :func:`conflict_prune_fraction`, :func:`_representative_n_bars`, and the
    :func:`_m4pro_abs_active` decoder — so the file's logic is fully verified
    without the M4-Pro runner (Golden Rule #10 worst-case hardening).
    """
    # project_walltime: 1 µs/trial × 86_400e6 trials = 86_400 s = 1 day.
    proj = project_walltime(1e-6, 86_400e6)
    assert proj["seconds"] == pytest.approx(86_400.0)
    assert proj["hours"] == pytest.approx(24.0)
    assert proj["days"] == pytest.approx(1.0)
    # Zero trials ⇒ zero wall time across all units.
    zero = project_walltime(1e-6, 0.0)
    assert zero["seconds"] == 0.0 and zero["hours"] == 0.0 and zero["days"] == 0.0

    # pruned_trials: a 0% prune keeps the full grid; a 100% prune empties it.
    assert pruned_trials(100.0, 0.0) == pytest.approx(100.0)
    assert pruned_trials(100.0, 1.0) == pytest.approx(0.0)
    assert pruned_trials(100.0, 0.5) == pytest.approx(50.0)

    # conflict_prune_fraction: the real ~50% (1.5M/2.9M survivors ⇒ ~48% pruned),
    # bounded strictly inside (0, 1) and matching the documented mask counts.
    frac = conflict_prune_fraction()
    assert 0.0 < frac < 1.0
    assert frac == pytest.approx(1.0 - _CONFLICT_MASKS_AFTER_PRUNE / _CONFLICT_MASKS_BEFORE_PRUNE)
    assert 0.4 < frac < 0.6  # the documented "~50%" prune, NOT the non-existent 6.4%
    # The pruned grid has strictly fewer trials than the full grid.
    assert pruned_trials(float(_FULL_SWEEP_TRIALS), frac) < float(_FULL_SWEEP_TRIALS)

    # _representative_n_bars: positive and large enough to cross the parallel arm
    # when batched with _BATCH_MASKS (the gated test relies on this).
    n_bars = _representative_n_bars()
    assert n_bars > 0
    assert _BATCH_MASKS * n_bars > (1 << 20)

    # The reused full-grid envelope is the COMMON 33B constant, not a local copy.
    assert _FULL_SWEEP_TRIALS == 33_000_000_000


@pytest.mark.bench
def test_m4pro_gate_decoder() -> None:
    """The env-gate decoder runs on EVERY runner (no skip) — verifies the logic.

    Independent of whether the gate fires, so the M4-Pro-only branch in
    :func:`test_m4pro_real_trade_walk_walltime` is never an untested dark corner
    (Golden Rule #10).
    """
    saved = os.environ.get(_ENV_M4PRO_ABS)
    try:
        for value, expected in [
            ("1", True),
            (" 1 ", True),
            ("", False),
            ("0", False),
            ("true", False),
        ]:
            os.environ[_ENV_M4PRO_ABS] = value
            assert _m4pro_abs_active() is expected
        os.environ.pop(_ENV_M4PRO_ABS, None)
        assert _m4pro_abs_active() is False
    finally:
        if saved is None:
            os.environ.pop(_ENV_M4PRO_ABS, None)
        else:
            os.environ[_ENV_M4PRO_ABS] = saved
