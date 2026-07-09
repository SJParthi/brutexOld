"""Apriori / anti-monotone level-wise PRUNED sweep candidate generator.

DEC-SWEEP-APRIORI-TUPLE-001 — a drop-in, **default-OFF** replacement for the
brute-force :func:`brutex.engine.combo_mask.iter_masks_k_of_bits` enumeration
of the sweep's target level ``k``. Enabled per-process by the tri-state env flag
``BRUTEX_SWEEP_APRIORI`` (``1/true/yes/on`` — anything else, incl. unset, is
OFF), it replaces "enumerate every C(n, k) combo then let Stage-1B drop the
sparse ones" with the classic Apriori join + subset-prune (Agrawal & Srikant
1994): build the frequent frontier ``F_1 … F_{k-1}`` level by level (each level
hit-filtered against ``min_hits``), then emit only the level-``k`` candidates
whose every ``(k-1)``-subset is frequent.

## Why this is bit-identical to the brute-force path (the merge gate)

A mask ``M`` (a set of condition-bit indices) HITS a bar iff
``(bar_bits & M) == M`` — the SUPERSET relation, which is **anti-monotone**:
adding a required bit can only ever REMOVE hits, so ``M ⊆ M'`` ⇒
``hits(M') ≤ hits(M)``. Stage-1B keeps a combo iff ``hits(combo) ≥ min_hits``.

* If a ``k``-combo ``M`` passes Stage-1B (``hits(M) ≥ min_hits``) then, by
  anti-monotonicity, EVERY subset of ``M`` — in particular every
  ``(k-1)``-subset — also has ``hits ≥ min_hits`` and is therefore frequent.
  So ``M`` is guaranteed to survive Apriori's subset-prune: **Apriori never
  drops a combo that Stage-1B would have kept.**
* Conversely, if some ``(k-1)``-subset of ``M`` is INfrequent, then
  ``hits(M) ≤ hits(that subset) < min_hits``, so ``M`` fails Stage-1B anyway.
  **Apriori only drops combos that Stage-1B would have dropped.**

Hence the KEPT set (Apriori candidates ∩ ``hits ≥ min_hits``) equals the
brute-force kept set (all conflict-free combos ∩ ``hits ≥ min_hits``), exactly.
The downstream ranking (:func:`brutex.ranking.rank_combos`) derives its trial
count ``N`` and its z-score pool from that kept set alone — never from the
enumerated count — so the persisted leaderboard is **byte-identical** whether
the flag is ON or OFF (proven end-to-end by the parity golden). Only the honest
tallies differ: ``enumerated`` counts the (smaller) Apriori candidate set and
``conflict_pruned`` is ``0`` (the candidates are conflict-free by construction).

## Emission ORDER — a deliberate, documented deviation

The candidates are emitted in the SAME order
:func:`~brutex.engine.combo_mask.iter_masks_k_of_bits` yields them —
**lexicographic on the ascending bit-position tuple** — NOT "ascending by
integer mask" (the two differ whenever the chosen bits interleave, e.g. bits
``(0, 3)`` → mask ``9`` sorts BEFORE ``(1, 2)`` → mask ``6`` lexicographically
but AFTER it numerically). Matching the brute-force enumeration order exactly
makes the Apriori kept-list a literal SUBSEQUENCE of the brute-force kept-list,
so the z-score pool (whose mean is a left-fold sum, ULP-order-sensitive — see
``brutex.ranking.composite_term_inputs``) is summed in the identical order and
the leaderboard is bit-identical, not merely set-equal. (The locked design's
"sort ascending by int mask" wording assumed contiguous bits where the two
orders coincide; lex order is the correct invariant for arbitrary bit sets and
is what the parity gate requires.)

## Frontier storage + the <64-bit guard

Each intermediate frequent frontier's membership authority is a **sorted numpy
``uint64`` array** (8 bytes/mask, subset lookup via ``np.searchsorted``) — never
a Python ``set`` (per-element object overhead). Because a single ``uint64`` limb
holds the mask, the generator REQUIRES every chosen condition bit ``< 64``; with
the flag ON a ``>= 64`` bit raises a clear ``ValueError`` (unset the flag for a
u128 vocabulary). Today's vocabulary is 60 bits, so production is unaffected.

## The time-based breaker (honest, MODELED)

Apriori is a search accelerator, not a bound: a degenerate cell (tiny
``min_hits`` on a dense vocabulary) can still emit an enormous candidate set. A
wall-clock breaker refuses such a cell LOUDLY rather than let it silently run
for days: it raises :class:`brutex.engine.sweep_run.SweepAprioriBudgetError`
(a :class:`~brutex.engine.sweep_run.SweepComboError` subclass, so the grid
drivers' existing ``TUPLE_FAILED`` path catches it — counted, non-zero exit,
deterministic redo) when the projected SERIAL backtest work
``len(candidates) × n_bars / walk_rate`` exceeds ``cell_budget_s``.

The default ``walk_rate`` (:data:`DEFAULT_WALK_RATE`) and budget
(:data:`DEFAULT_CELL_BUDGET_S`) are **MODELED / container-measured /
UNVERIFIED-on-M4** figures (Golden Rule #10) — a self-calibratable placeholder
(the B1 self-calibration follow-up), not a measured M4-Pro throughput. Both are
env-tunable (``BRUTEX_SWEEP_APRIORI_CELL_BUDGET_S`` /
``BRUTEX_SWEEP_APRIORI_WALK_RATE``); an invalid value falls back to the default
LOUDLY, a non-positive / non-finite value is refused.

## Honest complexity envelope (Golden Rule #10)

NOT free and NOT ``O(1)``: building the frontier costs one ``count_hits`` batch
per level over that level's candidates, the join is
``O(Σ_j |bucket|² · subset-checks)``, and the final emit list is
``O(|candidates|)``. Memory is driven by the FRONTIER size (bounded by the
breaker), NOT by ``n_bars`` — the generator never holds the bar window (it calls
the injected ``count_hits``), so its peak is flat versus bar count.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Sequence
from typing import Final

import numpy as np
import numpy.typing as npt
import structlog

from brutex.engine.combo_mask import count_supermasks
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.vocabulary.conflicts import has_conflict

__all__ = [
    "CELL_BUDGET_ENV",
    "DEFAULT_CELL_BUDGET_S",
    "DEFAULT_WALK_RATE",
    "SWEEP_APRIORI_ENV",
    "WALK_RATE_ENV",
    "apriori_enabled",
    "generate_apriori_candidates",
    "resolve_cell_budget_s",
    "resolve_walk_rate",
]

_LOG = structlog.get_logger(__name__)

#: The tri-state env flag enabling the Apriori generator (default OFF). Truthy
#: literals (``1/true/yes/on``, case-insensitive) enable it; anything else —
#: unset, empty, ``0/false/no/off`` or garbage — leaves the byte-identical
#: brute-force enumeration in place (fail-safe default, the sibling
#: DEC-SWEEP-LOOKAHEAD-GUARD-001 posture).
SWEEP_APRIORI_ENV: Final[str] = "BRUTEX_SWEEP_APRIORI"

#: Env var overriding :data:`DEFAULT_CELL_BUDGET_S` (a positive float, seconds).
CELL_BUDGET_ENV: Final[str] = "BRUTEX_SWEEP_APRIORI_CELL_BUDGET_S"

#: Env var overriding :data:`DEFAULT_WALK_RATE` (a positive float, mask·bars/s).
WALK_RATE_ENV: Final[str] = "BRUTEX_SWEEP_APRIORI_WALK_RATE"

#: Default per-cell wall-clock budget for the projected SERIAL backtest of the
#: emitted candidates (seconds). MODELED / container-measured / UNVERIFIED-on-M4
#: (Golden Rule #10) — 2 h is a generous default headroom, not a measured SLA;
#: env-tunable via :data:`CELL_BUDGET_ENV`, self-calibratable (B1 follow-up).
DEFAULT_CELL_BUDGET_S: Final[float] = 7200.0

#: Default modeled trade-sim throughput used to project serial backtest seconds
#: from ``len(candidates) × n_bars`` (mask·bars per second). MODELED /
#: container-measured / UNVERIFIED-on-M4 (Golden Rule #10); env-tunable via
#: :data:`WALK_RATE_ENV`.
DEFAULT_WALK_RATE: Final[float] = 1.61e8

#: The single ``uint64`` limb width — the maximum (exclusive) condition-bit index
#: the sorted-array frontier can represent.
_U64_BITS: Final[int] = 64

#: Stride between incremental breaker checks during candidate accumulation, so a
#: degenerate cell trips BEFORE a giant list is fully materialised.
_BUDGET_CHECK_STRIDE: Final[int] = 65_536

#: The pair level. Conflict-pruning is applied ONLY when building level-2
#: candidates; every higher level inherits conflict-freeness by subset-prune over
#: the conflict-free lower frontier (see :func:`_join_and_prune`).
_PAIR_LEVEL: Final[int] = 2


def apriori_enabled() -> bool:
    """Return ``True`` iff ``BRUTEX_SWEEP_APRIORI`` is set to a truthy literal.

    Truthy literals are ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive,
    surrounding whitespace ignored). Anything else — unset, empty, an explicit
    falsy literal, or garbage — is OFF: an unrecognised value must never
    silently enable the pruned path (fail-safe default). Classification rides
    the COMMON :func:`brutex.engine.sweep_lookahead.env_flag_tristate`.

    Returns
    -------
    bool
        Whether the Apriori pruned candidate generator is enabled.
    """
    return env_flag_tristate(os.environ.get(SWEEP_APRIORI_ENV, "")) is True


def _resolve_positive_float_env(
    name: str,
    default: float,
    event: str,
    env: dict[str, str] | None,
) -> float:
    """Resolve a positive-float env knob — unset/blank → *default*, invalid → LOUD.

    Mirrors the numeric-env idiom of
    ``brutex.engine.sweep_parallel._resolve_positive_float_env`` (kept local so
    this pure module stays free of the heavy parallel-sweep import graph). Reads
    *name* from *env* (default :data:`os.environ`): unset/blank → *default*; a
    finite ``float`` strictly ``> 0`` → that value; an INVALID value (non-float,
    non-finite, or ``<= 0``) → *default*, logged at WARNING on *event* naming the
    raw value via ``repr``. O(1).

    Parameters
    ----------
    name
        The environment variable name.
    default
        The value returned when the var is unset/blank or invalid.
    event
        The structlog event name emitted at WARNING for an invalid value.
    env
        The mapping to read (``None`` → :data:`os.environ`).

    Returns
    -------
    float
        The resolved positive float.
    """
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw)
    except ValueError:
        parsed = math.nan
    if not math.isfinite(parsed) or parsed <= 0.0:
        _LOG.warning(event, requested_env=repr(raw), fallback=default)
        return default
    return parsed


def resolve_cell_budget_s(env: dict[str, str] | None = None) -> float:
    """Resolve the per-cell wall-clock budget — env-tunable, invalid → LOUD default.

    Reads :data:`CELL_BUDGET_ENV`; unset/blank → :data:`DEFAULT_CELL_BUDGET_S`, a
    valid positive float → that value, an invalid value → the default plus a LOUD
    ``sweep.apriori.cell_budget_s.invalid`` WARNING. O(1).

    Parameters
    ----------
    env
        The environment mapping to read (``None`` → :data:`os.environ`).

    Returns
    -------
    float
        The resolved budget in seconds.
    """
    return _resolve_positive_float_env(
        CELL_BUDGET_ENV, DEFAULT_CELL_BUDGET_S, "sweep.apriori.cell_budget_s.invalid", env
    )


def resolve_walk_rate(env: dict[str, str] | None = None) -> float:
    """Resolve the modeled trade-sim throughput — env-tunable, invalid → LOUD default.

    Reads :data:`WALK_RATE_ENV`; unset/blank → :data:`DEFAULT_WALK_RATE`, a valid
    positive float → that value, an invalid value → the default plus a LOUD
    ``sweep.apriori.walk_rate.invalid`` WARNING. O(1).

    Parameters
    ----------
    env
        The environment mapping to read (``None`` → :data:`os.environ`).

    Returns
    -------
    float
        The resolved throughput in mask·bars per second.
    """
    return _resolve_positive_float_env(
        WALK_RATE_ENV, DEFAULT_WALK_RATE, "sweep.apriori.walk_rate.invalid", env
    )


def _mask_of(positions: Sequence[int]) -> int:
    """Pack an ascending bit-position tuple into its integer mask. O(k)."""
    mask = 0
    for p in positions:
        mask |= 1 << p
    return mask


def _in_sorted(arr: npt.NDArray[np.uint64], value: int) -> bool:
    """Return whether ``value`` is present in the ascending ``uint64`` array.

    Membership via ``np.searchsorted`` — ``O(log n)`` per query, no Python set.
    """
    idx = int(np.searchsorted(arr, np.uint64(value)))
    return idx < arr.size and int(arr[idx]) == value


def _all_subsets_frequent(
    candidate: tuple[int, ...], frontier_masks: npt.NDArray[np.uint64]
) -> bool:
    """Return ``True`` iff every ``(len-1)``-subset mask of *candidate* is frequent.

    Drops each position in turn, packs the remaining ascending positions into a
    mask, and checks membership in the sorted frequent-frontier array. This is
    Apriori's subset-prune: a candidate survives only if ALL its immediate
    subsets are frequent (anti-monotone downward closure).
    """
    for drop in range(len(candidate)):
        subset = candidate[:drop] + candidate[drop + 1 :]
        if not _in_sorted(frontier_masks, _mask_of(subset)):
            return False
    return True


def _join_and_prune(
    frontier_tuples: list[tuple[int, ...]],
    frontier_masks: npt.NDArray[np.uint64],
    *,
    drop_conflicts: bool,
    on_count: Callable[[int], None],
) -> list[tuple[int, ...]]:
    """Classic Apriori join of the ``(j-1)``-frontier into pruned ``j``-candidates.

    *frontier_tuples* is the frequent ``(j-1)``-frontier as **lex-sorted**
    ascending bit-position tuples; *frontier_masks* is the same set as a sorted
    ``uint64`` array (the subset-membership authority). Buckets the frontier by
    its first ``j-2`` positions (the shared prefix) and, within each bucket,
    joins every ordered pair ``(A, B)`` with ``A[-1] < B[-1]`` into
    ``prefix + (A[-1], B[-1])`` — the classic
    ``F_{j-1} ⋈ F_{j-1}`` self-join. Each candidate is kept iff ALL its
    ``(j-1)``-subsets are frequent (:func:`_all_subsets_frequent`).

    When *drop_conflicts* is set (the ``j == 2`` level only) a candidate that
    contains a :data:`~brutex.vocabulary.conflicts.CONFLICT_PAIRS` pair is
    dropped. For ``j >= 3`` this is UNNECESSARY: a conflict pair inside a
    ``j``-candidate would also sit inside at least one of its ``(j-1)``-subsets
    (drop any element outside the pair — possible since ``j >= 3``), and that
    subset could not be in a conflict-free ``F_{j-1}`` frontier, so subset-prune
    already excludes it. Conflict-freeness is thus INDUCTIVE from ``F_2`` upward
    (proven in the module's parity contract + a dedicated property test).

    *on_count* is called with the running candidate count every
    :data:`_BUDGET_CHECK_STRIDE` appends so the wall-clock breaker can trip
    BEFORE a giant list is materialised.

    Returns the pruned candidates as lex-sorted ascending position tuples.
    """
    buckets: dict[tuple[int, ...], list[int]] = {}
    for tpl in frontier_tuples:
        buckets.setdefault(tpl[:-1], []).append(tpl[-1])

    candidates: list[tuple[int, ...]] = []
    for prefix, lasts in buckets.items():
        m = len(lasts)
        for i in range(m):
            for jj in range(i + 1, m):
                candidate = (*prefix, lasts[i], lasts[jj])
                if not _all_subsets_frequent(candidate, frontier_masks):
                    continue
                if drop_conflicts and has_conflict(_mask_of(candidate)):
                    continue
                candidates.append(candidate)
                if len(candidates) % _BUDGET_CHECK_STRIDE == 0:
                    on_count(len(candidates))
    candidates.sort()
    return candidates


def _raise_budget(
    cell_label: str, k: int, count: int, n_bars: int, projected_s: float, budget_s: float
) -> None:
    """Raise the wall-clock breaker as a ``SweepComboError`` subclass (LOUD).

    Imports :class:`brutex.engine.sweep_run.SweepAprioriBudgetError` LAZILY at the
    raise site — the ONLY reference this pure module makes to the heavy
    ``sweep_run`` orchestrator — so there is no import cycle (``sweep_run``
    imports this generator lazily too) and the module top stays light.
    """
    from brutex.engine.sweep_run import SweepAprioriBudgetError

    raise SweepAprioriBudgetError(
        f"apriori cell {cell_label!r}: k={k} would emit {count} candidate(s) over "
        f"{n_bars} bars — projected serial backtest {projected_s:,.0f}s exceeds the "
        f"per-cell budget {budget_s:,.0f}s. Remediation: raise "
        f"{CELL_BUDGET_ENV}, tighten min_hits, reduce k, or unset "
        f"{SWEEP_APRIORI_ENV} to use the brute-force path."
    )


def generate_apriori_candidates(
    bar_bits: Sequence[int],
    *,
    condition_bits: Sequence[int],
    k: int,
    min_hits: int,
    count_hits: Callable[[Sequence[int]], list[int]] | None = None,
    n_bars: int | None = None,
    root_mod: tuple[int, int] = (0, 1),
    cell_budget_s: float | None = None,
    walk_rate: float | None = None,
    cell_label: str = "",
) -> list[int]:
    """Emit the Apriori-pruned level-``k`` candidate masks for one sweep cell.

    A drop-in replacement for
    ``list(iter_masks_k_of_bits(condition_bits, k))`` that returns only the
    candidates whose every ``(k-1)``-subset is frequent (fires on ``>= min_hits``
    bars), in the IDENTICAL lexicographic-by-position order (a subsequence of the
    brute-force enumeration). The KEPT set after the caller's Stage-1B
    (``hits >= min_hits``) filter is provably identical to the brute-force kept
    set (module docstring), so the persisted leaderboard is byte-identical.

    Parameters
    ----------
    bar_bits
        Per-bar packed condition bitsets for this cell. Used only to build the
        DEFAULT hit counter (when *count_hits* is ``None``) and to default
        *n_bars*; never retained (memory is flat vs bar count).
    condition_bits
        The condition bit positions the sweep chooses from (deduped, each
        ``< 64`` when the flag is on).
    k
        The target combo size, ``0 <= k <= len(set(condition_bits))``.
    min_hits
        The Stage-1B frequency threshold — a combo is frequent iff it fires on
        at least this many bars. ``<= 0`` makes every combo frequent (subset
        pruning never fires; the whole conflict-free ``C_k`` is emitted).
    count_hits
        The SHARED superset-hit counter injected by the caller (the resident
        kernel's counter, or ``count_supermasks``) — ``Callable`` mapping a mask
        sequence to per-mask hit counts. ``None`` builds the default
        ``lambda masks: count_supermasks(bar_bits, masks)``.
    n_bars
        The bar-window length feeding the wall-clock breaker's work estimate.
        ``None`` → ``len(bar_bits)``.
    root_mod
        A ``(w, W)`` sharding hook. The default ``(0, 1)`` owns the WHOLE
        level-``k`` set. When ``W > 1`` the RETURNED candidates are filtered to
        those owned by shard ``w`` — a mask is owned iff the index of its lowest
        set bit ``% W == w`` (mask ``0`` at ``k == 0`` is owned by shard 0). The
        frontiers ``F_1 … F_{k-1}`` are always computed WHOLE; the union over
        ``w in [0, W)`` reproduces the whole set and the shards are pairwise
        disjoint.
    cell_budget_s
        Per-cell wall-clock budget (seconds). ``None`` → :func:`resolve_cell_budget_s`.
    walk_rate
        Modeled trade-sim throughput (mask·bars/s). ``None`` → :func:`resolve_walk_rate`.
    cell_label
        A human label for the cell, echoed in the breaker error message.

    Returns
    -------
    list[int]
        The pruned, shard-owned level-``k`` masks in lexicographic-by-position
        order.

    Raises
    ------
    ValueError
        A condition bit is negative or ``>= 64`` (the u128-vocab guard), ``k``
        is outside ``[0, len(set(condition_bits))]``, ``root_mod`` is malformed,
        ``n_bars`` is negative, or an explicit *cell_budget_s* / *walk_rate* is
        non-positive / non-finite.
    brutex.engine.sweep_run.SweepAprioriBudgetError
        The projected serial backtest of the emitted candidates exceeds
        *cell_budget_s* (a :class:`~brutex.engine.sweep_run.SweepComboError`
        subclass — the grid drivers skip the tuple, count it, and exit non-zero).
    """
    unique_bits = sorted(set(condition_bits))
    for bit in unique_bits:
        if bit < 0 or bit >= _U64_BITS:
            raise ValueError(
                f"generate_apriori_candidates: condition bit {bit} out of range "
                f"[0, {_U64_BITS}). Apriori frontier requires <64-bit masks; unset "
                f"{SWEEP_APRIORI_ENV} for u128 vocabularies."
            )
    n = len(unique_bits)
    if not 0 <= k <= n:
        raise ValueError(
            f"generate_apriori_candidates: invalid k={k} for {n} unique bits (require 0 <= k <= n)"
        )
    w, big_w = root_mod
    if big_w < 1 or not 0 <= w < big_w:
        raise ValueError(
            f"generate_apriori_candidates: root_mod {root_mod!r} invalid "
            f"(require W >= 1 and 0 <= w < W)"
        )
    resolved_n_bars = len(bar_bits) if n_bars is None else n_bars
    if resolved_n_bars < 0:
        raise ValueError(f"generate_apriori_candidates: n_bars must be >= 0, got {resolved_n_bars}")
    budget_s = resolve_cell_budget_s() if cell_budget_s is None else cell_budget_s
    rate = resolve_walk_rate() if walk_rate is None else walk_rate
    if not math.isfinite(budget_s) or budget_s <= 0.0:
        raise ValueError(f"generate_apriori_candidates: cell_budget_s must be > 0, got {budget_s}")
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError(f"generate_apriori_candidates: walk_rate must be > 0, got {rate}")

    def _default_counter(masks: Sequence[int]) -> list[int]:
        return count_supermasks(bar_bits, masks)

    counter = _default_counter if count_hits is None else count_hits

    def _budget_check(count: int) -> None:
        projected = (count * resolved_n_bars) / rate
        if projected > budget_s:
            _raise_budget(cell_label, k, count, resolved_n_bars, projected, budget_s)

    # ── k == 0: the single empty mask (owned by shard 0 only) ──
    if k == 0:
        return [0] if w == 0 else []

    singleton_bits = unique_bits  # ascending == lex order for length-1 tuples
    singleton_masks = [1 << bit for bit in singleton_bits]

    # ── k == 1: every conflict-free singleton (singletons never conflict), NOT
    #    hit-filtered — the caller's Stage-1B drops the sparse ones. ──
    if k == 1:
        owned = [
            mask
            for bit, mask in zip(singleton_bits, singleton_masks, strict=True)
            if bit % big_w == w
        ]
        _budget_check(len(owned))
        return owned

    # ── k >= 2: build the frequent singleton frontier F_1 ──
    counts = counter(singleton_masks)
    frontier_tuples: list[tuple[int, ...]] = [
        (bit,) for bit, count in zip(singleton_bits, counts, strict=True) if count >= min_hits
    ]
    frontier_masks: npt.NDArray[np.uint64] = np.array(
        [1 << bit for (bit,) in frontier_tuples], dtype=np.uint64
    )

    # Build the intermediate frequent frontiers F_2 … F_{k-1} (each hit-filtered).
    for j in range(2, k):
        candidates = _join_and_prune(
            frontier_tuples,
            frontier_masks,
            drop_conflicts=(j == _PAIR_LEVEL),
            on_count=_budget_check,
        )
        cand_masks = [_mask_of(tpl) for tpl in candidates]
        counts = counter(cand_masks)
        frequent = [
            (tpl, mask)
            for tpl, mask, count in zip(candidates, cand_masks, counts, strict=True)
            if count >= min_hits
        ]
        frontier_tuples = [tpl for tpl, _ in frequent]
        frontier_masks = np.array(sorted(mask for _, mask in frequent), dtype=np.uint64)
        _budget_check(len(frontier_tuples))

    # Final level k: build C_k (NOT hit-filtered — the caller's Stage-1B does that),
    # apply the shard-ownership filter, and emit in lex-by-position order.
    final_candidates = _join_and_prune(
        frontier_tuples, frontier_masks, drop_conflicts=(k == _PAIR_LEVEL), on_count=_budget_check
    )
    owned_masks = [_mask_of(tpl) for tpl in final_candidates if tpl[0] % big_w == w]
    _budget_check(len(owned_masks))
    return owned_masks
