"""Vocab-66 wide-grid benches — the SuperTrend implemented-flip measurement kit.

DEC-SUPERTREND-CONDITIONS-001 (crate 0.5.0). With bits 64/65 LIVE every
SuperTrend-warm production bar carries a ``> u64`` ``bar_bits`` word, so the
``>= 64``-bit (u128) paths are the PRODUCTION regime, no longer headroom.
This module ships the INSTRUMENTS the M4-Pro measurement gate (merge
hard-gate (b)) runs — the measurements themselves happen on the self-hosted
runner (``bench-m4pro.yml``, ``BRUTEX_M4PRO_ABS=1``); on shared CI the same
tests run with sanity/linearity assertions only (DEC-P0-020 honest envelope —
absolute ops/s chronic-flakes on shared runners and is PRINTED, never
asserted, there):

* the WIDE Stage-2 count path's throughput (whatever dispatch serves — today
  the two-limb numpy reference; the moment the ``claude/rust-u128-enum-kernel``
  lane lands ``fno_math.ResidentBarsU128`` the SAME bench measures the Rust
  kernel; the ENGAGED path is printed honestly on every run),
* an end-to-end 66-bit sweep-SHAPED grid timing through the REAL sweep spine
  (``run_sweep_grid`` over ST-firing bars with bits 64/65 in the pool; sized
  for CI sanity, scalable via ``BRUTEX_VOCAB66_GRID_DAYS`` for the M4-Pro
  run), and
* the u64-vs-u128 trade-sim kernel ratio on the SAME window
  (``ResidentSweepSim`` vs ``ResidentSweepSimU128`` where the ONLY input
  difference is the ST bit on warm bars — the flip's own kernel cost,
  distinct from ``test_trade_sim_throughput.py``'s generic wide-word arm).

§8 SLA RE-DERIVATION FORMULA (Golden Rule #10 — the 2-4-day figure is
re-derived FROM the M4-Pro run's printed numbers, never from a shared box):

    per_trial_s      = the printed per-(mask·bar) seconds × n_bars   (per combo)
    grid_trials_64   = 33_000_000_000 × Σ_k C(64,k) / Σ_k C(60,k)
                     = 33B × 1.7097 (k = 1..8 aggregate)  ≈ 56.4B
    projected_days   = grid_trials_64 × per_trial_s / 86_400

with the honest per-k growth (printed by the projection test below):
C(64,k)/C(62,k) = 1.0323/1.0661/1.1016/1.1390/1.1783/1.2196/1.2632/1.3091 and
C(64,k)/C(60,k) = 1.0667/1.1390/1.2175/1.3030/1.3960/1.4976/1.6085/1.7299 for
k = 1..8 (default-pool width 62 → 64 at this flip; 60 was the pre-reshape
width the locked 33B envelope was derived at). Whether the re-derived figure
still fits the §8 2-4-day window is the M4-Pro run's verdict to record — this
module prints the inputs, it does not assume the answer.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Final, cast

import pytest

fno_math = pytest.importorskip("fno_math", reason="the resident kernels need the built wheel")

from brutex.data.models import CandleBar  # noqa: E402
from brutex.engine.combo_mask import U64_MASK, ResidentBarBuffer  # noqa: E402

# Reuse the COMMON 33B grid envelope — do NOT redefine it (Golden Rule #11).
from tests.bench.test_full_sweep_extrapolation import (  # noqa: E402
    _FULL_SWEEP_TRIALS,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fno_math import ResidentSweepSim, ResidentSweepSimU128

# Env-var gate — set to "1" on the M4 Pro self-hosted runner (bench-m4pro.yml).
_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"
#: Scale knob for the sweep-shaped grid (days of ST-firing bars). Default 2
#: keeps shared CI in seconds; the M4-Pro run may raise it for a longer,
#: steadier measurement window (the printed per-trial figures are what feed
#: the §8 re-derivation, not the wall total).
_ENV_GRID_DAYS: Final[str] = "BRUTEX_VOCAB66_GRID_DAYS"

#: Loose per-combo regression floor — the test_trade_sim_throughput.py
#: convention (catches a lost release build / catastrophic kernel regression
#: on EVERY runner; absorbs shared-runner jitter).
_REGRESSION_FLOOR_SECONDS_PER_COMBO: Final[float] = 5e-3

#: Wide count window/batch sizing. 20_000 bars × 256 masks = 5.12M mask·bar
#: ops per measured batch (comfortably above noise; the numpy arm is one
#: vectorised pass per mask, the future Rust arm one fused loop).
_WIDE_BARS: Final[int] = 20_000
_WIDE_MASKS: Final[int] = 256

_IST = timezone(timedelta(hours=5, minutes=30))


def _m4pro_abs_active() -> bool:
    return os.environ.get(_ENV_M4PRO_ABS, "").strip() == "1"


def _wide_dispatch_label() -> str:
    """The path the >=64-bit count dispatch serves TODAY — printed, never faked."""
    if hasattr(fno_math, "ResidentBarsU128"):
        return "fno_math.ResidentBarsU128 (Rust u128 kernel — the enum-kernel lane landed)"
    return "numpy two-limb reference (pre-lane law; see test_vocab66_wide_dispatch_pin.py)"


# ── (i) wide Stage-2 count throughput ────────────────────────────────────────


def _make_wide_window(n: int) -> list[int]:
    """Deterministic bar_bits with the production post-flip shape: dense low
    bits + EXACTLY ONE of the ST pair (bit 64/65) per warm bar."""
    seed = 0x9E3779B97F4A7C15
    out: list[int] = []
    for i in range(n):
        low = (seed * (i + 7)) & 0x000F_FFFF
        st_bit = 64 + (i & 1)  # alternate close_above/close_below (never both)
        out.append(low | (1 << st_bit))
    return out


def _make_wide_masks(n: int) -> list[int]:
    """Distinct strategy masks; every 4th carries an ST bit (a wide mask)."""
    out: list[int] = []
    for i in range(n):
        m = ((i * 0x100000001B3) | (1 << (i % 20))) & 0x000F_FFFF
        if i % 4 == 0:
            m |= 1 << (64 + ((i >> 2) & 1))
        out.append(m)
    return out


def _per_mask_seconds_wide(n_bars: int, n_masks: int) -> float:
    """Best-of-N wall time per mask for the DISPATCHED wide count path."""
    buf = ResidentBarBuffer(_make_wide_window(n_bars))
    masks = _make_wide_masks(n_masks)
    buf.count_supermasks(masks)  # warm
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        buf.count_supermasks(masks)
        best = min(best, time.perf_counter() - start)
    return best / n_masks


@pytest.mark.slow
@pytest.mark.bench
def test_wide_count_throughput_measured() -> None:
    """Measure the >=64-bit count path (masks/s + mask·bar/s), engaged path named.

    Shared CI asserts only the loose regression floor; the absolute figures
    are printed for the operator / the M4-Pro measurement record.
    """
    window = _make_wide_window(_WIDE_BARS)
    assert all(b > U64_MASK for b in window)  # teeth: genuinely wide workload
    per_mask = _per_mask_seconds_wide(_WIDE_BARS, _WIDE_MASKS)
    print(
        f"\nVocab-66 wide Stage-2 count path (DEC-SUPERTREND-CONDITIONS-001):\n"
        f"  engaged path        = {_wide_dispatch_label()}\n"
        f"  per-mask ({_WIDE_BARS} bars) = {per_mask * 1e6:.2f} µs/mask\n"
        f"  masks/sec           = {1.0 / per_mask:,.0f}\n"
        f"  mask·bar/sec        = {_WIDE_BARS / per_mask / 1e6:.1f} M\n"
    )
    assert per_mask <= _REGRESSION_FLOOR_SECONDS_PER_COMBO, (
        f"wide per-mask count {per_mask * 1e3:.2f} ms exceeds the "
        f"{_REGRESSION_FLOOR_SECONDS_PER_COMBO * 1e3:.0f} ms floor — the >=64-bit "
        f"count path regressed catastrophically (engaged: {_wide_dispatch_label()})."
    )


@pytest.mark.slow
@pytest.mark.bench
def test_wide_count_per_bar_linearity() -> None:
    """Per-(mask·bar) cost is flat across window sizes on the DISPATCHED path.

    A permissive shared-CI ceiling (the house robust tier; both windows sit in
    the same numpy/Rust arm) — NOT an O(1)-total claim: each count is
    O(N_masks · N_bars) by design; the pin is the amortised per-op cost.
    """
    from tests.bench._o1_tiers import cache_tier_ceiling

    small, large = 5_000, 20_000
    per_op_small = _per_mask_seconds_wide(small, 64) / small
    per_op_large = _per_mask_seconds_wide(large, 64) / large
    ratio = per_op_large / max(per_op_small, 1e-12)
    print(
        f"\nWide count per-op linearity: {per_op_small * 1e9:.3f} ns @ {small} bars "
        f"vs {per_op_large * 1e9:.3f} ns @ {large} bars → ratio {ratio:.2f}× "
        f"(engaged: {_wide_dispatch_label()})\n"
    )
    assert ratio < cache_tier_ceiling(5.0), (
        f"wide count per-(mask·bar) ratio {ratio:.2f}× exceeds the ceiling — "
        f"super-linear regression in the >=64-bit count dispatch."
    )


# ── (ii) end-to-end 66-bit sweep-shaped grid through the real spine ──────────


#: Known-regular NSE trading days (Jan 2024; deliberately excludes the
#: 2024-01-22 special holiday + 2024-01-26 Republic Day + weekends) — the
#: grid-days scale knob clamps to this list so a scaled M4-Pro run can never
#: land bars on a non-session date (which would raise at the forced-exit
#: session resolve).
_GRID_SESSION_DAYS: Final[tuple[int, ...]] = (2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16, 17, 18, 19)


def _st_grid_bars(days: int) -> list[CandleBar]:
    """``days`` real-session days of ST-warming bars (Jan-2024 regular NSE
    trading days, clamped to ``len(_GRID_SESSION_DAYS)``): a flat range-2
    warm-up then a drifting tail so bits fire both ways and trades
    open/close."""
    out: list[CandleBar] = []
    for d in range(min(days, len(_GRID_SESSION_DAYS))):
        base = datetime(2024, 1, _GRID_SESSION_DAYS[d], 9, 15, tzinfo=_IST)
        for m in range(60):
            drift = (m - 30) * 0.1 if m >= 20 else 0.0
            close = 100.0 + drift
            ist = base + timedelta(minutes=m)
            out.append(
                CandleBar(
                    timestamp=ist.astimezone(UTC),
                    open=Decimal(str(round(close, 2))),
                    high=Decimal(str(round(close + 1.0, 2))),
                    low=Decimal(str(round(close - 1.0, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=1_000,
                    open_interest=None,
                )
            )
    return out


@pytest.mark.slow
@pytest.mark.bench
def test_vocab66_sweep_shaped_grid_timed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Time a REAL 66-bit-vocab sweep-shaped grid (bits 64/65 in the pool).

    The full production spine — Stage-1A bar_bits (ST state included, wide
    words from bar 9 on), conflict prune ((64,65) excluded at k=2),
    enumeration, trade sim, ranking, persistence — over a deterministic
    ST-firing window. Sanity assertions only (the walltime-bench precedent:
    a hard wall ceiling here would flake — the printed per-trial figure is
    the measurement product); scale via BRUTEX_VOCAB66_GRID_DAYS on the
    M4-Pro run.
    """
    pytest.importorskip("polars")  # store construction needs the [data] extra
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import (
        SweepGridConfig,
        Timeframe,
        make_store_for,
        run_sweep_grid,
    )
    from brutex.engine.sweep_run import ComboDedup
    from brutex.orchestrator.audit import AuditWriter
    from brutex.strategies.types import Side

    days_raw = os.environ.get(_ENV_GRID_DAYS, "").strip()
    days = int(days_raw) if days_raw.isdigit() and int(days_raw) >= 1 else 2
    bars = _st_grid_bars(days)
    grid = SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1, 2],
        condition_bits=[0, 1, 54, 64, 65],  # the ST pair rides the pool
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="vocab66-bench",
        indicator_version="iv-bench",
        code_commit_sha="cc-bench",
        min_hits=0,
    )
    dedup = ComboDedup(OptionDedupIndex(capacity=100_000, fpr_target=1e-6))
    start = time.perf_counter()
    with AuditWriter.open(tmp_path / "grid.jsonl") as audit:
        summary = run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: bars,
            store_for=make_store_for(tmp_path / "results", grid),
            dedup=dedup,
            audit=audit,
        )
    wall = time.perf_counter() - start
    trials = summary.evaluated * len(bars)
    print(
        f"\nVocab-66 sweep-shaped grid (real spine, {days} day(s) × 60 bars):\n"
        f"  bars                 = {len(bars)}\n"
        f"  enumerated/evaluated = {summary.enumerated}/{summary.evaluated}"
        f"  (conflict-pruned {summary.conflict_pruned} — incl. the (64,65) ST pair)\n"
        f"  persisted            = {summary.persisted}\n"
        f"  wall                 = {wall:.3f} s"
        f"  ({trials / max(wall, 1e-9):,.0f} trial(mask·bar)/s end-to-end)\n"
    )
    # Sanity, never a flaky wall ceiling: the grid ran, nothing failed, and
    # the conflict prune genuinely fired at k=2 (the pool carries both ST
    # bits, so the (64,65) combo is among the pruned pairs — anti-vacuity;
    # the (0,1) EMA pair prunes too, hence >= 1 not == 1).
    assert summary.tuples_failed == 0
    assert summary.evaluated > 0
    assert summary.conflict_pruned >= 1
    assert math.isfinite(wall) and wall > 0.0


@pytest.mark.bench
def test_grid_growth_projection_table() -> None:
    """Print the honest C(60/62/64, k) growth table + the 33B-envelope scaling.

    Pure integer math (runs everywhere, asserts exact values — the table the
    §8 re-derivation and MIGRATION.md DEC-SUPERTREND-CONDITIONS-001 cite).
    """
    rows = []
    tot60 = tot62 = tot64 = 0
    for k in range(1, 9):
        c60, c62, c64 = math.comb(60, k), math.comb(62, k), math.comb(64, k)
        tot60, tot62, tot64 = tot60 + c60, tot62 + c62, tot64 + c64
        rows.append((k, c60, c62, c64, c64 / c62, c64 / c60))
    agg62, agg60 = tot64 / tot62, tot64 / tot60
    print("\nGrid growth (default pool 62 → 64 bits; 60 = the 33B-envelope era):")
    print("  k |        C(60,k) |        C(62,k) |        C(64,k) | 64/62  | 64/60")
    for k, c60, c62, c64, r62, r60 in rows:
        print(f"  {k} | {c60:>14,} | {c62:>14,} | {c64:>14,} | {r62:.4f} | {r60:.4f}")
    print(
        f"  Σ | {tot60:>14,} | {tot62:>14,} | {tot64:>14,} | {agg62:.4f} | {agg60:.4f}\n"
        f"  33B-envelope scaling (k≤8 aggregate): "
        f"{_FULL_SWEEP_TRIALS:,} × {agg60:.4f} ≈ {int(_FULL_SWEEP_TRIALS * agg60):,} trials\n"
    )
    # The exact anchors the ledger cites (integer math — never flaky).
    assert math.comb(64, 4) == 635_376
    assert math.comb(64, 5) == 7_624_512
    assert round(agg62, 4) == 1.3017
    assert round(agg60, 4) == 1.7097


# ── (iii) u64-vs-u128 trade-sim kernel ratio on the SAME window ──────────────


def _kernel_columns(
    n_bars: int,
) -> tuple[list[int], list[int], list[int], list[int], list[bool], list[int], list[int]]:
    """One deterministic window: the u64 vs u128 arms differ ONLY in bar_bits
    (the wide arm adds one ST bit per warm bar — the exact post-flip shape)."""
    seed = 0x9E3779B97F4A7C15
    base = 1_000_000
    open_p: list[int] = []
    high_p: list[int] = []
    low_p: list[int] = []
    close_p: list[int] = []
    bits_narrow: list[int] = []
    for i in range(n_bars):
        wobble = ((seed * (i + 1)) >> 13) % 200 - 100
        o = base + wobble
        open_p.append(o)
        high_p.append(o + 150)
        low_p.append(o - 150)
        close_p.append(o + (wobble % 30))
        bits_narrow.append((seed * (i + 7)) & 0x000F_FFFF)
    forced = [False] * n_bars
    session_idx = [0] * n_bars
    return open_p, high_p, low_p, close_p, forced, session_idx, bits_narrow


def _per_combo(sim: ResidentSweepSim | ResidentSweepSimU128, masks: Sequence[int]) -> float:
    sl, tp = 8_000, 12_000
    sim.count_trades(list(masks), True, sl, tp)  # warm
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        sim.count_trades(list(masks), True, sl, tp)
        best = min(best, time.perf_counter() - start)
    return best / len(masks)


@pytest.mark.slow
@pytest.mark.bench
def test_trade_sim_u64_vs_u128_same_window_ratio() -> None:
    """The flip's own kernel cost: ResidentSweepSim vs ResidentSweepSimU128 on
    the SAME window where the ONLY delta is the ST bit on warm bars.

    Distinct from ``test_trade_sim_throughput.py``'s generic wide arm (which
    scatters bits across the whole high limb): this is the production
    post-flip shape. The ratio is printed (informational — never asserted
    equal); each arm holds the loose regression floor.
    """
    n_bars, n_masks = 20_000, 64
    open_p, high_p, low_p, close_p, forced, session_idx, bits_narrow = _kernel_columns(n_bars)
    # The post-flip twin: warm bars (>= 9) gain exactly one ST bit.
    bits_wide = [b | (1 << (64 + (i & 1))) if i >= 9 else b for i, b in enumerate(bits_narrow)]
    masks = [((i * 0x100000001B3) | (1 << (i % 20))) & 0x000F_FFFF for i in range(n_masks)]
    # The contiguous tf==1 fixed-point arm_source (DEC-FILL-EXEC-GRID-1M-001,
    # #1989): 1m bar j is the next-1m fill bar of decision bar j-1 (bar 0
    # unarmed) — the exec-grid 8-arg constructor's 7th positional argument.
    arm_source = [-1, *range(n_bars - 1)]

    sim_u64 = cast(
        "ResidentSweepSim",
        fno_math.ResidentSweepSim(
            open_p, high_p, low_p, close_p, forced, session_idx, arm_source, bits_narrow
        ),
    )
    sim_u128 = cast(
        "ResidentSweepSimU128",
        fno_math.ResidentSweepSimU128(
            open_p, high_p, low_p, close_p, forced, session_idx, arm_source, bits_wide
        ),
    )
    per_u64 = _per_combo(sim_u64, masks)
    per_u128 = _per_combo(sim_u128, masks)
    print(
        f"\nTrade-sim u64 vs u128 on the SAME window (post-flip ST shape, "
        f"{n_bars} bars × {n_masks} masks):\n"
        f"  u64  per-combo = {per_u64 * 1e6:.2f} µs  ({1.0 / per_u64:,.0f} combos/s; "
        f"{n_bars / per_u64 / 1e6:.1f} M mask·bar/s)\n"
        f"  u128 per-combo = {per_u128 * 1e6:.2f} µs  ({1.0 / per_u128:,.0f} combos/s; "
        f"{n_bars / per_u128 / 1e6:.1f} M mask·bar/s)\n"
        f"  u128/u64 ratio = {per_u128 / per_u64:.2f}×  (measured, informational — "
        f"the u128 kernel is the production regime post-flip)\n"
    )
    assert per_u64 <= _REGRESSION_FLOOR_SECONDS_PER_COMBO
    assert per_u128 <= _REGRESSION_FLOOR_SECONDS_PER_COMBO, (
        f"u128 per-combo {per_u128 * 1e3:.2f} ms exceeds the "
        f"{_REGRESSION_FLOOR_SECONDS_PER_COMBO * 1e3:.0f} ms floor — the wide kernel "
        f"(now the production regime) regressed or lost its release build."
    )
