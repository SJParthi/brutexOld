"""Design §7.5 bench gates for the 1-minute execution grid (DEC-FILL-EXEC-GRID-1M-001).

The frozen PR-B design (``docs/design/fills/PRB_1M_EXECUTION_GRID_DESIGN.md``
§7.5, teeth restated at §1.5) prescribes THREE bench gates on the dual-feed
trade-sim leg — thresholds are REFUSAL gates, never claims; every measured
number is printed (observed, never asserted-as-SLA):

* **(a) sparse per-mask cost FLAT vs N_1m** — flat-skip (the ``next_arm`` jump
  table) bounds a sparse mask's per-mask cost by
  ``O(#arm-points + Σ in-position minutes + #trades)`` (the table is
  mask-independent — the flat walker visits every ARM POINT, firing or not,
  never only the firing signals), so growing the 1-minute execution stream
  with FLAT, ARMLESS span (a fixed armed region + a growing unarmed tail —
  zero arm points in the growth) must not grow the per-mask cost. Ratio tier
  per ``tests/bench/_o1_tiers.py`` (broken flat-skip walks the tail → ~8×
  here).
* **(b) the A/B tuple-throughput ACCEPTANCE THRESHOLD** — a sparse-mask
  rehearsal batch at tf=5m must land within **≤ 2.0×** the single-feed
  (PR-A-shape) wall-clock per tuple. A >2× degradation means flat-skip is
  broken or a hidden O(N_1m)-per-mask term crept in; the measured ratio is
  printed for the PR body and the 2.0× ceiling REFUSES — it never substitutes
  for the measurement.
* **(c) dense-mask WORST CASE** — an always-in-position mask honestly walks
  ≈N_1m minutes (the SL/TP law); the gate pins that cost LINEAR in N_1m
  (per-1m-bar cost flat across sizes — no superlinear surprise) and STATES
  the dense cost, never hides it.

## Honest envelope (Golden Rule #10) + the (b) baseline residual

The design words gate (b) as "vs the PR-A tree". The literal PR-A tree (the
pre-PR-B commit) cannot be imported into this process — its kernel classes no
longer exist on this branch — so the baseline here is the **PR-A COST SHAPE
emulated on the same binary**: the current kernel at the tf==1 fixed point
over the SAME decision stream (all-armed ``arm_source`` — per-mask cost = one
push per decision bar, exactly the PR-A single-feed walk envelope). Same
binary + same process makes the wall-clock ratio FAIRER than a cross-commit
A/B (no compiler/flag/runner drift); the literal old-tree A/B remains
un-runnable in shared CI and is the honestly-narrowed residual documented in
MIGRATION.md #1979.

Second disclosed observation: growing N_1m by COARSENING tf (fixed armed
count, wider armed stride) measured 2.73× on the build sandbox — a
cache-STRIDE effect (each armed step touches colder lines at a 40-bar stride
than at a 5-bar stride), bounded by a constant memory-tier factor and NOT an
O(N_1m) term (the broken-flat-skip signal on that shape is ~8×). That shape
is not gated here; the production tf=5m interior shape is what gate (b)
bounds at ≤ 2.0×, and gate (a) isolates the flat-span lever itself.

All timed batches stay on the kernel's SERIAL dispatch arm (mask·1m-bar work
< 2^20) so no ratio compares a serial leg against a rayon-parallel one — the
``_assert_serial_arm`` guard makes that deterministic, not incidental.
Timing uses the house best-of-N idiom (min of repeats — the noise-robust
true-cost estimator). Sandbox measurements (2026-07-18, recorded in the gate
docstrings) are OBSERVED numbers, never asserted floors.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

import pytest

fno_math = pytest.importorskip(
    "fno_math", reason="exec-grid trade-sim kernel needs the built wheel"
)

from tests.bench._o1_tiers import cache_tier_ceiling, ratio_ceiling  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only (annotations are lazy)
    from collections.abc import Callable

    from fno_math import ResidentSweepSim

#: Best-of-N repeats per timed batch (house de-flake idiom — min never inflates).
_REPEATS: Final[int] = 5

#: Mirror of the kernel's ``PARALLEL_WORK_THRESHOLD`` (``rust/fno-math/src/
#: trade_sim.rs`` — DEC-RUST-RAYON-SUPERMASK-001): batches at or above this
#: mask·1m-bar work dispatch on the rayon-parallel arm. Every timed batch in
#: this module must stay STRICTLY BELOW it so both sides of every ratio run
#: the SAME (serial) arm.
_PARALLEL_WORK_THRESHOLD: Final[int] = 1 << 20

#: The design's gate-(b) acceptance ceiling (§7.5 (b)): the dual-feed sparse
#: rehearsal batch at tf=5m must land within 2.0× the single-feed-shape
#: wall-clock per tuple. A fixed REFUSAL threshold from the design — not a
#: runner-noise tier (both arms are same-process measurements).
_AB_ACCEPTANCE_CEILING: Final[float] = 2.0

_SEED: Final[int] = 0x9E3779B97F4A7C15
_BASE_PAISA: Final[int] = 1_000_000  # ₹10,000.00


def _assert_serial_arm(n_masks: int, n_1m: int) -> None:
    """Refuse a fixture whose batch would cross onto the parallel dispatch arm."""
    work = n_masks * n_1m
    assert work < _PARALLEL_WORK_THRESHOLD, (
        f"bench fixture work {work:,} mask·1m-bar crosses the kernel's parallel "
        f"threshold {_PARALLEL_WORK_THRESHOLD:,} — a serial-vs-parallel arm switch "
        f"would skew the ratio; shrink masks or bars."
    )


def _make_exec_columns(
    n: int,
) -> tuple[list[int], list[int], list[int], list[int], list[bool], list[int]]:
    """Deterministic 1m execution columns: a gently oscillating single session.

    ``session_idx`` all-0 + ``is_forced`` all-False keeps the window free of
    session-gap / forced exits, so trade lifetimes are controlled purely by
    the SL/TP distances each gate picks (sparse: tight SL → ~1-2 minute
    trades; dense: no SL/TP → one always-in-position trade).
    """
    open_p: list[int] = []
    high_p: list[int] = []
    low_p: list[int] = []
    close_p: list[int] = []
    for i in range(n):
        wobble = ((_SEED * (i + 1)) >> 13) % 200 - 100
        o = _BASE_PAISA + wobble
        open_p.append(o)
        high_p.append(o + 150)
        low_p.append(o - 150)
        close_p.append(o + (wobble % 30))
    return open_p, high_p, low_p, close_p, [False] * n, [0] * n


def _dual_arm_source(n_1m: int, tf_minutes: int, n_decision: int) -> list[int]:
    """The contiguous dual-feed projection: decision bar N fills at 1m bar (N+1)·tf.

    Exactly the ``ExecGridMap.arm_source`` a contiguous aligned pair yields
    (the close-instant law ``exec_idx(N) = (N+1)·tf`` on a holeless stream);
    a decision bar whose fill index falls past the window stays unarmed.
    """
    src = [-1] * n_1m
    for dec in range(n_decision):
        j = (dec + 1) * tf_minutes
        if j < n_1m:
            src[j] = dec
    return src


def _tf1_arm_source(n_bars: int) -> list[int]:
    """The tf==1 fixed point: bar j is the next-1m fill of decision bar j-1.

    The PR-A single-feed COST SHAPE (module docstring): every bar armed, one
    push per decision bar per mask — the pre-PR-B kernel's per-mask envelope.
    """
    return [-1, *range(n_bars - 1)]


def _sparse_bits(n_decision: int, full_pattern: int, k_firing: int) -> list[int]:
    """``bar_bits`` firing ``full_pattern`` on exactly ``k_firing`` evenly spread bars."""
    stride = max(1, n_decision // k_firing)
    return [
        full_pattern if (i % stride == 0 and i // stride < k_firing) else 0
        for i in range(n_decision)
    ]


def _best_of(repeats: int, fn: Callable[[], object]) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def _make_sim(n_1m: int, tf_minutes: int, n_decision: int, bar_bits: list[int]) -> ResidentSweepSim:
    o, h, lo, c, forced, sess = _make_exec_columns(n_1m)
    return fno_math.ResidentSweepSim(  # type: ignore[no-any-return]
        o, h, lo, c, forced, sess, _dual_arm_source(n_1m, tf_minutes, n_decision), bar_bits
    )


# ── gate (a): sparse per-mask cost flat vs N_1m ───────────────────────────────


@pytest.mark.slow
@pytest.mark.bench
def test_sparse_mask_per_mask_cost_flat_vs_n1m() -> None:
    """§7.5 (a): a sparse mask's per-mask cost is FLAT as N_1m grows with flat span.

    Fixed armed region (4,000 decision bars at tf=5 → armed indices within the
    first 20,001 1m bars; 64 firing bars; tight SL so every trade closes in
    ~1-2 minutes) while N_1m grows 20,000 → 160,000 via an UNARMED flat tail —
    exactly the flat-span class ``next_arm`` jumps. Flat-skip intact ⇒ both
    sizes walk the identical armed region (equal touched footprint, so the
    plain :func:`ratio_ceiling` tier applies); broken flat-skip walks the
    140,000-bar tail per mask ⇒ ~8× — loudly over every tier. Deterministic
    tooth: the tail must add ZERO trades (identical per-mask counts).

    Sandbox 2026-07-18 (observed): 36.82 µs/mask @ 20k vs 37.73 µs/mask @
    160k → ratio 1.02×.
    """
    n_decision = 4_000
    tf_minutes = 5
    n_masks = 6
    full_pattern = (1 << n_masks) - 1
    masks = [1 << i for i in range(n_masks)]
    bits = _sparse_bits(n_decision, full_pattern, k_firing=64)
    sl_paisa, tp_paisa = 50, 30_000  # tight SL: trades close on the next 1-2 1m bars

    n_small, n_big = 20_000, 160_000
    _assert_serial_arm(n_masks, n_small)
    _assert_serial_arm(n_masks, n_big)

    sim_small = _make_sim(n_small, tf_minutes, n_decision, bits)
    sim_big = _make_sim(n_big, tf_minutes, n_decision, bits)
    counts_small = sim_small.count_trades(masks, True, sl_paisa, tp_paisa)  # warm + counts
    counts_big = sim_big.count_trades(masks, True, sl_paisa, tp_paisa)
    # Deterministic tooth: the unarmed tail contributes NOTHING — same trades.
    assert counts_small == counts_big, (
        f"the unarmed flat tail changed the trade stream ({counts_small} vs "
        f"{counts_big}) — the fixture no longer isolates flat span."
    )

    per_mask_small = (
        _best_of(_REPEATS, lambda: sim_small.count_trades(masks, True, sl_paisa, tp_paisa))
        / n_masks
    )
    per_mask_big = (
        _best_of(_REPEATS, lambda: sim_big.count_trades(masks, True, sl_paisa, tp_paisa)) / n_masks
    )
    ratio = per_mask_big / max(per_mask_small, 1e-12)
    ceiling = ratio_ceiling(3.0)
    print(
        f"\n§7.5 (a) sparse per-mask flat vs N_1m (DEC-FILL-EXEC-GRID-1M-001) "
        f"— best of {_REPEATS}:\n"
        f"  N_1m = {n_small:7,} | {per_mask_small * 1e6:8.2f} µs/mask "
        f"({counts_small[0]} trades/mask)\n"
        f"  N_1m = {n_big:7,} | {per_mask_big * 1e6:8.2f} µs/mask "
        f"({counts_big[0]} trades/mask)\n"
        f"  ratio = {ratio:.2f}× (ceiling {ceiling:.2f}×; broken flat-skip ≈ "
        f"{n_big / n_small:.0f}×)\n"
    )
    assert ratio <= ceiling, (
        f"sparse per-mask cost grew {ratio:.2f}× when N_1m grew "
        f"{n_big / n_small:.0f}× with pure flat span (ceiling {ceiling:.2f}×) — "
        f"flat-skip is broken or a hidden O(N_1m)-per-mask term crept in "
        f"(design §7.5 (a))."
    )


# ── gate (b): A/B dual-feed vs the PR-A cost shape, ≤ 2.0× per tuple ─────────


@pytest.mark.slow
@pytest.mark.bench
def test_ab_dual_feed_tuple_cost_within_2x_of_single_feed_shape() -> None:
    """§7.5 (b): the tf=5m sparse rehearsal batch lands ≤ 2.0× the PR-A shape.

    Baseline = the PR-A single-feed COST SHAPE on the same binary (module
    docstring): the 8,000-bar decision stream walked as its own execution
    grid (tf==1 fixed point, all-armed). Subject = the dual-feed shape the
    production sweep runs at tf=5m: the SAME 8,000 decision bars armed inside
    a 40,000-bar 1-minute stream. One 24-mask sparse rehearsal batch (64
    firing bars, tight SL) is one tuple's trade-sim leg; construction (the
    map + columns) is once-per-coordinate and amortized across a grid's
    tuples (design §1.5), so the per-tuple wall-clock is the batch walk.

    The ceiling is the design's fixed 2.0× REFUSAL threshold; the measured
    ratio is printed for the PR body (the ceiling never substitutes for the
    measurement). Sandbox 2026-07-18 (observed): baseline 1.574 ms vs
    dual-feed 1.940 ms per tuple → ratio 1.23×.
    """
    n_decision = 8_000
    tf_minutes = 5
    n_1m = n_decision * tf_minutes
    n_bits = 12
    full_pattern = (1 << n_bits) - 1
    # 24 distinct sparse masks (12 single-bit + 12 adjacent two-bit) — every
    # one a subset of the firing pattern, so each fires on the same 64 bars.
    masks = [1 << i for i in range(n_bits)] + [
        (1 << i) | (1 << ((i + 1) % n_bits)) for i in range(n_bits)
    ]
    bits = _sparse_bits(n_decision, full_pattern, k_firing=64)
    sl_paisa, tp_paisa = 50, 30_000

    _assert_serial_arm(len(masks), n_decision)
    _assert_serial_arm(len(masks), n_1m)

    o, h, lo, c, forced, sess = _make_exec_columns(n_decision)
    baseline: ResidentSweepSim = fno_math.ResidentSweepSim(
        o, h, lo, c, forced, sess, _tf1_arm_source(n_decision), bits
    )
    subject = _make_sim(n_1m, tf_minutes, n_decision, bits)

    counts_base = baseline.count_trades(masks, True, sl_paisa, tp_paisa)  # warm + counts
    counts_subj = subject.count_trades(masks, True, sl_paisa, tp_paisa)
    # Deterministic tooth: both arms trade every firing bar (a live, sparse
    # fixture on BOTH shapes — never a vacuous timing of an empty walk).
    assert all(n == 64 for n in counts_base), f"baseline not 64 trades/mask: {counts_base}"
    assert all(n == 64 for n in counts_subj), f"subject not 64 trades/mask: {counts_subj}"

    t_base = _best_of(_REPEATS, lambda: baseline.count_trades(masks, True, sl_paisa, tp_paisa))
    t_subj = _best_of(_REPEATS, lambda: subject.count_trades(masks, True, sl_paisa, tp_paisa))
    ratio = t_subj / max(t_base, 1e-12)
    print(
        f"\n§7.5 (b) A/B tuple throughput at tf=5m (DEC-FILL-EXEC-GRID-1M-001) "
        f"— best of {_REPEATS}, {len(masks)} masks/tuple:\n"
        f"  single-feed shape ({n_decision:,} decision bars) = {t_base * 1e3:8.3f} ms/tuple\n"
        f"  dual-feed         ({n_1m:,} 1m bars)      = {t_subj * 1e3:8.3f} ms/tuple\n"
        f"  ratio = {ratio:.3f}× (acceptance ceiling {_AB_ACCEPTANCE_CEILING:.1f}× — "
        f"design §7.5 (b); measured number recorded, never substituted)\n"
    )
    assert ratio <= _AB_ACCEPTANCE_CEILING, (
        f"dual-feed per-tuple wall-clock is {ratio:.3f}× the single-feed shape — "
        f"over the design's {_AB_ACCEPTANCE_CEILING:.1f}× acceptance threshold "
        f"(§7.5 (b)): flat-skip is broken or a hidden O(N_1m)-per-mask term "
        f"crept into the dual-feed kernel."
    )


# ── gate (c): dense worst case — linear in N_1m, cost stated ─────────────────


@pytest.mark.slow
@pytest.mark.bench
def test_dense_mask_worst_case_cost_linear_in_n1m() -> None:
    """§7.5 (c): the always-in-position mask walks ≈N_1m — LINEAR, never worse.

    Every mask fires on decision bar 0 with no SL/TP, so the position opens at
    1m bar tf and holds to end_of_data — the honest worst case: the SL/TP law
    walks EVERY in-position minute. The gate pins the per-1m-bar cost FLAT
    across sizes (total cost linear in N_1m — no superlinear surprise); the
    dense cost is printed, never hidden. The touched footprint grows with
    N_1m by design, so the :func:`cache_tier_ceiling` tier applies
    (DEC-O1-CACHE-TIER-001). Deterministic tooth: exactly ONE trade per mask
    (the walk provably spans the window).

    Sandbox 2026-07-18 (observed): 8.04 ns/1m-bar @ 20k vs 8.17 ns @ 80k →
    ratio 1.02×; dense per-mask cost 160.8 µs @ 20k / 653.3 µs @ 80k.
    """
    tf_minutes = 5
    n_masks = 8
    full_pattern = (1 << n_masks) - 1
    masks = [1 << i for i in range(n_masks)]

    def dense_per_bar(n_1m: int) -> tuple[float, list[int]]:
        n_decision = n_1m // tf_minutes
        bits = [full_pattern if i == 0 else 0 for i in range(n_decision)]
        sim = _make_sim(n_1m, tf_minutes, n_decision, bits)
        counts = sim.count_trades(masks, True, None, None)  # warm + counts
        total = _best_of(_REPEATS, lambda: sim.count_trades(masks, True, None, None))
        return total / n_masks / n_1m, counts

    n_small, n_big = 20_000, 80_000
    _assert_serial_arm(n_masks, n_small)
    _assert_serial_arm(n_masks, n_big)

    per_bar_small, counts_small = dense_per_bar(n_small)
    per_bar_big, counts_big = dense_per_bar(n_big)
    # Deterministic tooth: one always-in-position trade per mask, both sizes.
    assert all(n == 1 for n in counts_small), f"dense fixture not 1 trade/mask: {counts_small}"
    assert all(n == 1 for n in counts_big), f"dense fixture not 1 trade/mask: {counts_big}"

    ratio = per_bar_big / max(per_bar_small, 1e-12)
    ceiling = cache_tier_ceiling(3.0)
    print(
        f"\n§7.5 (c) dense-mask worst case (DEC-FILL-EXEC-GRID-1M-001) "
        f"— best of {_REPEATS}, always-in-position:\n"
        f"  N_1m = {n_small:6,} | {per_bar_small * 1e9:6.2f} ns/1m-bar "
        f"({per_bar_small * n_small * 1e6:8.2f} µs/mask — the stated dense cost)\n"
        f"  N_1m = {n_big:6,} | {per_bar_big * 1e9:6.2f} ns/1m-bar "
        f"({per_bar_big * n_big * 1e6:8.2f} µs/mask)\n"
        f"  ratio = {ratio:.2f}× (ceiling {ceiling:.2f}×)\n"
    )
    assert ratio <= ceiling, (
        f"dense per-1m-bar cost ratio {ratio:.2f}× exceeds {ceiling:.2f}× — the "
        f"always-in-position walk regressed to SUPERLINEAR in N_1m "
        f"(design §7.5 (c) pins it linear; the ≈N_1m walk itself is the honest, "
        f"stated cost)."
    )
