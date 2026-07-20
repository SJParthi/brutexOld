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
the flag ON a ``>= 64`` bit raises
:class:`brutex.engine.sweep_run.SweepAprioriUnsupportedError` — a
:class:`~brutex.engine.sweep_run.SweepComboError` subclass, so the grid
GRACEFULLY skips that tuple (TUPLE_FAILED) rather than crashing on a raw
``ValueError`` if the flag is ever left ON over a future u128 vocabulary (unset
the flag to use the byte-identical brute-force path, which has no bit-width
limit). Today's ACTIVE vocabulary is 62 bits, all ``< 64``, so production is
unaffected.

## The deterministic mask·bar breaker (pure integer, MODELED limit)

Apriori is a search accelerator, not a bound: a degenerate cell (tiny
``min_hits`` on a dense vocabulary) can still emit an enormous candidate set. A
breaker refuses such a cell LOUDLY rather than let it silently run for days: it
raises :class:`brutex.engine.sweep_run.SweepAprioriBudgetError`
(a :class:`~brutex.engine.sweep_run.SweepComboError` subclass, so the grid
drivers' existing ``TUPLE_FAILED`` path catches it — counted, non-zero exit,
deterministic redo) when the PURE-INTEGER work estimate
``len(candidates) * n_bars`` **strictly exceeds** ``max_mask_bars`` (a mask·bar
count; STRICT ``>`` — an exactly-at-limit cell is ALLOWED, never trips).

The trip DECISION is a pure function of the SETTLED RAW per-level candidate
counts. The Apriori circuit-breaker check |C_j|·n_bars > MAX_MASK_BARS (strict >,
env BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS default 1_159_200_000_000,
MODELED/UNVERIFIED-on-M4 per Golden Rule #10) is evaluated on the WHOLE RAW
per-level candidate count at the k==1 target level, every intermediate level
2..k−1, and the final level k; for k≥2 the level-1 singleton count |C₁| =
n_unique ≤ 62 is DELIBERATELY NOT budget-checked (trivially bounded, provably
non-runaway). Each AUTHORITATIVE check runs the instant that level's raw
candidate list is complete — the singleton set when ``k == 1`` IS the target
level, the raw join output ``|C_j|`` at each intermediate level ``j in 2..k-1``
BEFORE hit-filtering, and the WHOLE raw ``|C_k|`` at the final level BEFORE any
``root_mod`` shard filtering. The breaker thus guards the candidate ENUMERATION
cost (the ``|C_j|`` masks that must be hit-counted), NOT the post-hit-filter
frequent frontier ``|F_j|`` (a smaller, different quantity). Because every check
is on the whole pre-shard ``|C_j|``, the trip is SHARD-INVARIANT at every checked
level: every ``root_mod`` shard of a cell trips identically on the whole-cell
budget.

The incremental :data:`_BUDGET_CHECK_STRIDE` check is a pure DECISION-PRESERVING
memory optimization layered ON TOP: it fires the SAME authoritative check on the
running ``|C_j|`` every ``_BUDGET_CHECK_STRIDE`` appends so a giant list is never
fully materialised, and since ``candidates`` only ever appends, a strided fire is
always a strictly-EARLIER raise of the identical authoritative decision. The trip
is therefore STRIDE-INVARIANT: whether a cell trips is independent of
:data:`_BUDGET_CHECK_STRIDE`, only how many candidates are materialised before the
raise depends on it (pinned by ``test_budget_check_stride_invariance``).

There is NO float division, NO ``time.time()``, NO seconds / rate anywhere in
the trip path: the breaker is a single integer comparison on settled raw counts,
so the SAME inputs + config produce the SAME trip — including the SAME failure
set — **bit-identically Python↔Rust** (Golden Rule #7 idempotence). The
wall-clock runaway backstop is the SEPARATE, EXISTING #1608 parallel-sweep stall
watchdog (non-identity-bearing, crash-equivalent / deterministic-redo); this
deterministic breaker never touches it.

The default limit :data:`DEFAULT_MAX_MASK_BARS` = ``1_159_200_000_000`` mask·bar
is a **MODELED / container-measured / UNVERIFIED-on-M4** figure (Golden Rule
#10): ≈ 2 h serial backtest × ``1.61e8`` mask·bar/s (≈ 6.2 ns per mask·bar), a
self-calibratable placeholder (the B1 follow-up), NOT a measured M4-Pro
throughput. It is env-tunable via the ONE knob
``BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS`` (a positive integer); an unset / blank
value uses the default, an invalid (non-integer / non-positive) value falls back
to the default LOUDLY.

What the breaker bounds — and does NOT (honest, Golden Rule #10): it caps the
mask·bar PRODUCT of the emitted candidates × the bar window and trips on THAT.
It does **not** directly bound the generator's OWN join CPU, nor the transient
candidate-LIST memory below — it is a proxy that trips before a candidate set
large enough to make the backtest infeasible is fully materialised (the
incremental :data:`_BUDGET_CHECK_STRIDE` check compares the RUNNING count times
``n_bars``). The generator's own join CPU is bounded in practice only by
``n = len(unique bits) <= 62`` (the active vocabulary width).

## The parity-completion caveat (Golden Rule #10)

ON-vs-OFF leaderboard parity is **bit-identical for cells that BOTH paths run to
completion**; a cell that trips the (default-OFF, deterministic mask·bar) breaker
is a LOUD ``TUPLE_FAILED`` (operator-visible, counted, non-zero exit), never a
silent divergence. Because the trip is a pure-integer function of (candidate
count, ``n_bars``, ``max_mask_bars``), the failure set itself is reproducible
run-to-run AND Python↔Rust.

## Honest complexity + memory envelope (Golden Rule #10)

NOT free and NOT ``O(1)``: building the frontier costs one ``count_hits`` batch
per level over that level's candidates, the join is
``O(Σ_j |bucket|² · subset-checks)``, and the final emit list is
``O(|candidates|)``.

Memory: the persistent per-level frequent-frontier authority is the sorted
``uint64`` numpy array at **8 bytes/mask**. But the DOMINANT peak TRANSIENT at
level ``k`` is the Python ``list[tuple[int, ...]]`` of candidates the join
materialises — each an interned-``int`` tuple costing on the order of **~160
bytes/candidate** (small-tuple + element overhead, an order-of-magnitude figure,
not a measured constant), so peak ``≈ O(160 B) · |candidates_k|``, bounded by
``min(C(n, k), the breaker's implied candidate cap)``. This peak is driven by the
FRONTIER / candidate-list size, NOT by ``n_bars`` — the generator never holds the
bar window (it calls the injected ``count_hits``), so its peak is flat versus bar
count.

## Per-level survivor census (counts only, ZERO identity impact)

For every Apriori level it processes the generator emits ONE
``sweep.apriori.census`` structlog event (:func:`_emit_census`) carrying the
settled RAW candidate count ``|C_j|`` and — for the internal frequent frontiers
``F_1 … F_{k-1}`` — the post-``min_hits`` frequent count ``|F_j|`` (the TARGET
level ``k`` is NOT hit-filtered here, so its ``frequent`` is OMITTED; the
caller's Stage-1B is the authority). A real-lake Apriori run therefore produces
the EMPIRICAL per-level survivor curve, replacing the modeled numbers. This is
pure observability — counts only, gated on the Apriori path (fires only when the
generator runs): it never touches the returned candidates, the run identity, or
the persisted store bytes.

## The Apriori-aware ``max_combos`` pre-flight (guardrail semantics)

The sweep's up-front pre-flight refuses any ``k`` whose raw ``C(n_unique, k)``
mask count exceeds ``max_combos``. That is correct for the brute-force path but
with the DEFAULT cap would REFUSE a full-vocab ``k >= 4`` grid
(``C(62, 4) = 557_845 > 100_000`` over the 62 active bits) BEFORE the Apriori
generator ever runs — even
though, with the flag ON, the deterministic ``MAX_MASK_BARS`` breaker is the real
guardrail on emitted work, not raw ``C(n, k)``. :func:`effective_max_combos`
therefore replaces **only the DEFAULT** ``max_combos`` guard value: with Apriori
enabled AND the value the built-in default it AUTO-RAISES to admit every
requested ``k`` (emitting one ``sweep.apriori.max_combos_raised`` event). An
EXPLICIT operator-set ``--max-combos`` always WINS (returned unchanged, never
raised — even if the guard then refuses a too-large ``k``), and with Apriori
disabled the value is likewise UNCHANGED — the brute-force path's pre-flight is
byte-identical (the guard LOGIC is never touched; this helper only ever FEEDS the
guards a value).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Final

import numpy as np
import numpy.typing as npt
import structlog

from brutex.engine.combo_mask import count_combos, count_supermasks
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.vocabulary.conflicts import has_conflict

__all__ = [
    "DEFAULT_MAX_MASK_BARS",
    "MAX_MASK_BARS_ENV",
    "SWEEP_APRIORI_ENV",
    "apriori_enabled",
    "effective_max_combos",
    "generate_apriori_candidates",
    "resolve_max_mask_bars",
]

_LOG = structlog.get_logger(__name__)

#: The tri-state env flag enabling the Apriori generator (default OFF). Truthy
#: literals (``1/true/yes/on``, case-insensitive) enable it; anything else —
#: unset, empty, ``0/false/no/off`` or garbage — leaves the byte-identical
#: brute-force enumeration in place (fail-safe default, the sibling
#: DEC-SWEEP-LOOKAHEAD-GUARD-001 posture).
SWEEP_APRIORI_ENV: Final[str] = "BRUTEX_SWEEP_APRIORI"

#: The ONE env var overriding :data:`DEFAULT_MAX_MASK_BARS` (a positive INTEGER —
#: the ``len(candidates) * n_bars`` mask·bar work cap the breaker trips on).
MAX_MASK_BARS_ENV: Final[str] = "BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS"

#: Default per-cell mask·bar work cap: the breaker trips when
#: ``len(candidates) * n_bars`` STRICTLY exceeds this integer. ``1_159_200_000_000``
#: is a MODELED / container-measured / UNVERIFIED-on-M4 figure (Golden Rule #10) —
#: ≈ 2 h serial backtest × ``1.61e8`` mask·bar/s (≈ 6.2 ns per mask·bar), a
#: self-calibratable placeholder (the B1 follow-up), NOT a measured M4-Pro
#: throughput. A PURE INTEGER so the trip is deterministic + bit-identical
#: Python↔Rust (Golden Rule #7); env-tunable via :data:`MAX_MASK_BARS_ENV`.
DEFAULT_MAX_MASK_BARS: Final[int] = 1_159_200_000_000

#: The single ``uint64`` limb width — the maximum (exclusive) condition-bit index
#: the sorted-array frontier can represent.
_U64_BITS: Final[int] = 64

#: Stride between incremental breaker checks during candidate accumulation — a
#: pure DECISION-PRESERVING MEMORY optimization, so a degenerate cell trips BEFORE
#: a giant list is fully materialised. The trip DECISION is now a pure function of
#: the SETTLED RAW per-level candidate counts (the authoritative post-join
#: ``_budget_check(len(candidates))`` runs on the SAME raw ``|C_j|`` this strided
#: check accumulates, at EVERY level), and ``candidates`` only ever appends, so by
#: monotonicity a strided fire is always a strictly-EARLIER raise of the identical
#: authoritative decision — the trip is STRIDE-INVARIANT (whether a cell trips is
#: independent of this value; only HOW MANY candidates are materialised before the
#: raise depends on it). This constant is therefore GENUINELY result-equivalent to
#: mutate (a ``±1`` mutation cannot change any cell's raise/return verdict — proven
#: by ``test_budget_check_stride_invariance``, not papered over:
#: DEC-SWEEP-APRIORI-TUPLE-001 stride-invariance fix). Hence the ``no mutate``.
_BUDGET_CHECK_STRIDE: Final[int] = 65_536  # pragma: no mutate

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
    # Read with NO string default, then short-circuit on the unset (``None``)
    # case, so ``env_flag_tristate`` is never handed a fabricated default: an
    # UNSET var maps straight to ``False`` here. (A ``get(name, "")`` form left a
    # genuinely-equivalent mutant — ``env_flag_tristate`` maps ``""`` and every
    # non-truthy string alike to ``None`` → ``is True`` False — so the default's
    # value was unobservable; this shape has no such default to mutate.)
    raw = os.environ.get(SWEEP_APRIORI_ENV)
    return raw is not None and env_flag_tristate(raw) is True


def effective_max_combos(
    *,
    apriori_enabled: bool,
    k_values: Sequence[int],
    n_unique: int,
    requested_max_combos: int,
    requested_is_explicit: bool,
) -> int:
    """Resolve the ``max_combos`` pre-flight cap, Apriori-AWARE (guardrail semantics).

    DEC-SWEEP-APRIORI-TUPLE-001 guardrail: the sweep refuses up-front any ``k``
    whose raw ``C(n_unique, k)`` mask count exceeds ``max_combos`` (the guards in
    ``SweepGridConfig.__post_init__`` + ``run_persisted_sweep``). That raw-count
    pre-flight is correct for the brute-force path, but with the DEFAULT cap it
    would REFUSE a full-vocab ``k >= 4`` grid (``C(62, 4) = 557_845 > 100_000``
    over the 62 active bits) BEFORE the Apriori generator ever runs — even
    though, with Apriori ON, the
    deterministic ``MAX_MASK_BARS`` breaker (:func:`resolve_max_mask_bars`) is the
    real guardrail on emitted work, not raw ``C(n, k)``.

    So the Apriori bypass replaces **only the DEFAULT** ``max_combos`` guard
    value — an EXPLICIT operator-set ``--max-combos`` always WINS and is NEVER
    auto-raised (even when that means the guard later refuses a too-large ``k``:
    the operator's explicit limit is honored). The three cases:

    * *apriori_enabled* ``False`` → *requested_max_combos* returned **unchanged**
      (the brute-force pre-flight is byte-identical — the guard logic is never
      touched; this only ever FEEDS the guards a value).
    * *apriori_enabled* ``True`` AND *requested_is_explicit* ``True`` (operator
      passed ``--max-combos``) → *requested_max_combos* returned **unchanged**
      (OPERATOR WINS), no log.
    * *apriori_enabled* ``True`` AND *requested_is_explicit* ``False`` (the value
      is the built-in DEFAULT) → AUTO-RAISED to
      ``max(requested, max_k C(n_unique, k))`` so every requested ``k`` is
      admitted while the breaker bounds the actual candidate·bar work; a raise
      emits ONE counts-only ``sweep.apriori.max_combos_raised`` structlog (an
      already-sufficient default logs nothing). An out-of-range ``k``
      (``k > n_unique`` or ``k < 0``) contributes NOTHING to the raise — it is
      filtered out (``count_combos`` would raise for it) and DEFERRED to the
      downstream ``SweepGridConfig`` validation, which refuses it with a clean
      ``EXIT_USER_ERROR`` (its ``ValueError`` caught at the CLI build sites, no
      traceback; never a raw ``ValueError`` here, never an auto-raise).

    Parameters
    ----------
    apriori_enabled
        Whether the Apriori generator is enabled for this process (typically
        :func:`apriori_enabled`). ``False`` → *requested_max_combos* unchanged.
    k_values
        The resolved target combo sizes for the grid.
    n_unique
        The number of UNIQUE condition bits (``len(set(condition_bits))``).
    requested_max_combos
        The operator-requested cap, or the resolved built-in default when the
        operator did not pass one.
    requested_is_explicit
        ``True`` iff the operator explicitly passed ``--max-combos`` (so it must
        WIN); ``False`` iff *requested_max_combos* is the built-in DEFAULT (the
        only value the Apriori bypass may raise).

    Returns
    -------
    int
        The effective ``max_combos`` to feed the grid + run pre-flight guards.
    """
    if not apriori_enabled:
        return requested_max_combos
    if requested_is_explicit:
        # OPERATOR WINS: an explicit --max-combos is honored verbatim, never
        # auto-raised — even if the guard then refuses a too-large k.
        return requested_max_combos
    # DEFAULT cap only: raise to the largest raw C(n, k) any requested k enumerates
    # so Apriori's breaker (not raw C(n,k)) becomes the guardrail. An out-of-range
    # k (k > n_unique or k < 0) is FILTERED here — ``count_combos`` raises for it,
    # and it is refused cleanly downstream by ``SweepGridConfig`` regardless of
    # ``max_combos``, so we never crash with a raw ``ValueError`` (flag-OFF hygiene
    # parity: ALL k out of range → generator empty → ``needed = 0`` → the requested
    # cap is returned unchanged, deferring to that downstream config validation).
    needed = max((count_combos(k, n_unique) for k in k_values if 0 <= k <= n_unique), default=0)
    effective = max(requested_max_combos, needed)
    if effective > requested_max_combos:
        _LOG.info(
            "sweep.apriori.max_combos_raised",
            requested=requested_max_combos,
            effective=effective,
            reason="apriori_breaker_guardrail",
        )
    return effective


def _resolve_positive_int_env(
    name: str,
    default: int,
    event: str,
    env: dict[str, str] | None,
) -> int:
    """Resolve a positive-INTEGER env knob — unset/blank → *default*, invalid → LOUD.

    The integer sibling of the numeric-env idiom used across the sweep. Kept a
    LOCAL copy (a DELIBERATE, reviewer-blessed deviation from Golden Rule #11 —
    COMMON) so this pure, light-import generator stays free of the heavy
    parallel-sweep import graph. Reads *name* from *env* (default
    :data:`os.environ`): unset/blank → *default*; an ``int`` strictly ``> 0`` →
    that value; an INVALID value (non-integer OR ``<= 0``) → *default*, logged at
    WARNING on *event* naming the raw value via ``repr``. Pure integer, no float,
    no wall clock — O(1).

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
    int
        The resolved positive integer.
    """
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except ValueError:
        parsed = 0  # non-integer → the sentinel trips the non-positive guard below
    if parsed <= 0:
        _LOG.warning(event, requested_env=repr(raw), fallback=default)
        return default
    return parsed


def resolve_max_mask_bars(env: dict[str, str] | None = None) -> int:
    """Resolve the per-cell mask·bar work cap — env-tunable, invalid → LOUD default.

    Reads :data:`MAX_MASK_BARS_ENV`; unset/blank → :data:`DEFAULT_MAX_MASK_BARS`, a
    valid positive integer → that value, an invalid value → the default plus a LOUD
    ``sweep.apriori.max_mask_bars.invalid`` WARNING. Pure integer — O(1).

    Parameters
    ----------
    env
        The environment mapping to read (``None`` → :data:`os.environ`).

    Returns
    -------
    int
        The resolved mask·bar limit.
    """
    return _resolve_positive_int_env(
        MAX_MASK_BARS_ENV, DEFAULT_MAX_MASK_BARS, "sweep.apriori.max_mask_bars.invalid", env
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
    :data:`_BUDGET_CHECK_STRIDE` appends so the deterministic incremental
    (mask·bar) breaker can trip before a giant candidate list is materialised.

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
                # Tuple concatenation (not ``(*prefix, …)`` unpacking): byte-for-byte
                # the same tuple, but avoids a ``*``→``/`` mutation that would be a
                # bare SyntaxError (``(/prefix, …)``) — an uncompilable mutant mutmut
                # cannot run and so mis-reports as SURVIVED (no test can execute a
                # syntax error). ``prefix`` is always a tuple, so ``+`` appends the
                # two new positions identically. (RUF005 would "fix" this back to
                # the ``*``-unpacking form that re-introduces that syntax-error
                # mutant, so it is deliberately suppressed here.)
                candidate = prefix + (lasts[i], lasts[jj])  # noqa: RUF005
                if not _all_subsets_frequent(candidate, frontier_masks):
                    continue
                if drop_conflicts and has_conflict(_mask_of(candidate)):
                    continue
                candidates.append(candidate)
                if len(candidates) % _BUDGET_CHECK_STRIDE == 0:
                    on_count(len(candidates))
    candidates.sort()
    return candidates


def _raise_budget(cell_label: str, k: int, count: int, n_bars: int, max_mask_bars: int) -> None:
    """Raise the deterministic mask·bar breaker as a ``SweepComboError`` subclass (LOUD).

    Imports :class:`brutex.engine.sweep_run.SweepAprioriBudgetError` LAZILY at the
    raise site — the ONLY reference this pure module makes to the heavy
    ``sweep_run`` orchestrator — so there is no import cycle (``sweep_run``
    imports this generator lazily too) and the module top stays light. The message
    NAMES the cell + the level ``k`` (a k=2 tuple reads ``k=2``, a k=5 tuple reads
    ``k=5``), the candidate count, the pure-integer ``count x n_bars`` mask·bar
    product and the limit it exceeds, and the remedies.
    """
    from brutex.engine.sweep_run import SweepAprioriBudgetError

    raise SweepAprioriBudgetError(
        f"apriori cell {cell_label!r}: k={k} would emit {count} candidate(s) over "
        f"{n_bars} bars — {count} x {n_bars} = {count * n_bars} mask·bar exceeds the "
        f"per-cell limit {max_mask_bars} mask·bar. Remediation: raise "
        f"{MAX_MASK_BARS_ENV}, tighten min_hits, reduce k, or unset "
        f"{SWEEP_APRIORI_ENV} to use the brute-force path."
    )


def _raise_unsupported_bit(bit: int) -> None:
    """Raise the ``>= 64``-bit u128-vocab refusal as a ``SweepComboError`` subclass.

    Imports :class:`brutex.engine.sweep_run.SweepAprioriUnsupportedError` LAZILY at
    the raise site (the sibling of :func:`_raise_budget`) so this pure module makes
    NO top-level reference to the heavy ``sweep_run`` orchestrator — no import
    cycle, light module top. Subclassing ``SweepComboError`` means the flag ON over
    a future ``>= 64``-bit vocabulary GRACEFULLY skips the tuple (TUPLE_FAILED)
    rather than crashing the whole grid with a raw ``ValueError``.
    """
    from brutex.engine.sweep_run import SweepAprioriUnsupportedError

    raise SweepAprioriUnsupportedError(
        f"generate_apriori_candidates: Apriori frontier requires <64-bit masks "
        f"(vocab bit {bit} >= {_U64_BITS}); unset {SWEEP_APRIORI_ENV} for u128 "
        f"vocabularies."
    )


def _emit_census(
    *,
    cell_label: str,
    k: int,
    level: int,
    candidates: int,
    min_hits: int,
    n_bars: int,
    frequent: int | None = None,
) -> None:
    """Emit the counts-only per-level Apriori survivor census (structlog, no identity).

    DEC-SWEEP-APRIORI-TUPLE-001 census: fires ONE ``sweep.apriori.census`` event
    for every Apriori level the generator processes, carrying the settled RAW
    candidate count ``|C_level|`` and — for the internal frequent frontiers
    (levels ``1 .. k-1``) — the post-``min_hits`` frequent count ``|F_level|``.
    The TARGET level ``k`` is NOT hit-filtered here (the caller's Stage-1B is the
    authority), so its ``frequent`` is OMITTED. A real-lake Apriori run therefore
    emits the EMPIRICAL per-level survivor curve, replacing the modeled numbers.

    Pure observability — counts only. It does NOT touch the returned candidates,
    the run identity, or the persisted store bytes; it only fires on the Apriori
    path (when the generator runs). O(1) per level.
    """
    fields: dict[str, object] = {
        "cell": cell_label,
        "target_k": k,
        "level": level,
        "candidates": candidates,
        "min_hits": min_hits,
        "n_bars": n_bars,
    }
    if frequent is not None:
        fields["frequent"] = frequent
    _LOG.info("sweep.apriori.census", **fields)


def generate_apriori_candidates(
    bar_bits: Sequence[int],
    *,
    condition_bits: Sequence[int],
    k: int,
    min_hits: int,
    count_hits: Callable[[Sequence[int]], list[int]] | None = None,
    n_bars: int | None = None,
    root_mod: tuple[int, int] = (0, 1),
    max_mask_bars: int | None = None,
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
        The bar-window length feeding the breaker's ``count * n_bars`` mask·bar
        work estimate. ``None`` → ``len(bar_bits)``.
    root_mod
        A ``(w, W)`` sharding hook. The default ``(0, 1)`` owns the WHOLE
        level-``k`` set. When ``W > 1`` the RETURNED candidates are filtered to
        those owned by shard ``w`` — a mask is owned iff the index of its lowest
        set bit ``% W == w`` (mask ``0`` at ``k == 0`` is owned by shard 0). The
        frontiers ``F_1 … F_{k-1}`` are always computed WHOLE; the union over
        ``w in [0, W)`` reproduces the whole set and the shards are pairwise
        disjoint.
    max_mask_bars
        The per-cell mask·bar work cap (a positive integer). The breaker trips when
        ``len(candidates) * n_bars`` STRICTLY exceeds it (deterministic, pure
        integer, bit-identical Python↔Rust). ``None`` → :func:`resolve_max_mask_bars`.
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
        A condition bit is negative, ``k`` is outside
        ``[0, len(set(condition_bits))]``, ``root_mod`` is malformed, ``n_bars``
        is negative, or an explicit *max_mask_bars* is ``<= 0`` (all
        caller-contract violations).
    brutex.engine.sweep_run.SweepAprioriUnsupportedError
        A condition bit is ``>= 64`` (the u128-vocab guard) — a
        :class:`~brutex.engine.sweep_run.SweepComboError` subclass, so the flag
        ON over a future ``>= 64``-bit vocabulary GRACEFULLY skips the tuple
        (TUPLE_FAILED) rather than crashing the grid. Remedy: unset the flag.
    brutex.engine.sweep_run.SweepAprioriBudgetError
        The mask·bar product ``len(candidates) * n_bars`` strictly exceeds
        *max_mask_bars* (a :class:`~brutex.engine.sweep_run.SweepComboError`
        subclass — the grid drivers skip the tuple, count it, and exit non-zero).
    """
    unique_bits = sorted(set(condition_bits))
    for bit in unique_bits:
        if bit < 0:
            raise ValueError(
                f"generate_apriori_candidates: condition bit {bit} is negative "
                f"(out of range [0, {_U64_BITS}))"
            )
        if bit >= _U64_BITS:
            # A ``>= 64``-bit vocabulary (u128) — a GRACEFUL per-tuple refusal
            # (SweepComboError subclass → TUPLE_FAILED), never a raw ValueError
            # that would crash the whole grid with the flag left ON.
            _raise_unsupported_bit(bit)
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
    limit = resolve_max_mask_bars() if max_mask_bars is None else max_mask_bars
    if limit <= 0:
        raise ValueError(f"generate_apriori_candidates: max_mask_bars must be > 0, got {limit}")

    def _default_counter(masks: Sequence[int]) -> list[int]:
        return count_supermasks(bar_bits, masks)

    counter = _default_counter if count_hits is None else count_hits

    def _budget_check(count: int) -> None:
        # Pure-integer trip: STRICT ``>`` — an exactly-at-limit cell is ALLOWED.
        if count * resolved_n_bars > limit:
            _raise_budget(cell_label, k, count, resolved_n_bars, limit)

    # ── k == 0: the single empty mask (owned by shard 0 only) ──
    if k == 0:
        return [0] if w == 0 else []

    singleton_bits = unique_bits  # ascending == lex order for length-1 tuples

    # ── k == 1: every conflict-free singleton (singletons never conflict), NOT
    #    hit-filtered — the caller's Stage-1B drops the sparse ones. Built by a
    #    direct comprehension over the bits (NOT a ``zip(bits, masks, strict=…)``:
    #    that zipped two internally-constructed always-equal-length lists, so the
    #    ``strict`` flag could never fire and a ``strict=False`` mutant was
    #    genuinely equivalent). ──
    if k == 1:
        # AUTHORITATIVE on the WHOLE raw |C_1| (pre-shard) — stride + shard invariant.
        _budget_check(len(singleton_bits))
        # k == 1 IS the target level: |C_1| emitted, frequent OMITTED (Stage-1B decides).
        _emit_census(
            cell_label=cell_label,
            k=k,
            level=1,
            candidates=len(singleton_bits),
            min_hits=min_hits,
            n_bars=resolved_n_bars,
        )
        return [1 << bit for bit in singleton_bits if bit % big_w == w]

    # ── k >= 2: build the frequent singleton frontier F_1 ──
    singleton_masks = [1 << bit for bit in singleton_bits]
    counts = counter(singleton_masks)
    frontier_tuples: list[tuple[int, ...]] = [
        (bit,) for bit, count in zip(singleton_bits, counts, strict=True) if count >= min_hits
    ]
    frontier_masks: npt.NDArray[np.uint64] = np.array(
        [1 << bit for (bit,) in frontier_tuples], dtype=np.uint64
    )
    # Level-1 census: raw |C_1| singletons + the post-min_hits frequent frontier |F_1|.
    _emit_census(
        cell_label=cell_label,
        k=k,
        level=1,
        candidates=len(singleton_bits),
        frequent=len(frontier_tuples),
        min_hits=min_hits,
        n_bars=resolved_n_bars,
    )

    # Build the intermediate frequent frontiers F_2 … F_{k-1} (each hit-filtered).
    for j in range(2, k):
        candidates = _join_and_prune(
            frontier_tuples,
            frontier_masks,
            drop_conflicts=(j == _PAIR_LEVEL),
            on_count=_budget_check,
        )
        # AUTHORITATIVE breaker check on the SETTLED RAW |C_j| — the SAME quantity
        # the strided ``on_count`` check accumulates, BEFORE hit-filtering. The
        # breaker guards the candidate ENUMERATION cost (the |C_j| masks that must
        # be hit-counted), NOT the post-filter frontier |F_j|; checking |F_j| here
        # instead made the trip DECISION depend on _BUDGET_CHECK_STRIDE (a
        # degenerate level with |C_j| over budget but |F_j| under budget tripped at
        # small stride yet passed at large stride) — breaking Python↔Rust
        # failure-set bit-identity (Golden Rule #7).
        _budget_check(len(candidates))
        cand_masks = [_mask_of(tpl) for tpl in candidates]
        counts = counter(cand_masks)
        frequent = [
            (tpl, mask)
            for tpl, mask, count in zip(candidates, cand_masks, counts, strict=True)
            if count >= min_hits
        ]
        frontier_tuples = [tpl for tpl, _ in frequent]
        frontier_masks = np.array(sorted(mask for _, mask in frequent), dtype=np.uint64)
        # Intermediate-level census: raw |C_j| join output + frequent frontier |F_j|.
        _emit_census(
            cell_label=cell_label,
            k=k,
            level=j,
            candidates=len(candidates),
            frequent=len(frequent),
            min_hits=min_hits,
            n_bars=resolved_n_bars,
        )

    # Final level k: build C_k (NOT hit-filtered — the caller's Stage-1B does that),
    # check the breaker on the WHOLE settled raw |C_k| BEFORE any shard-ownership
    # filtering (so the trip is shard-INVARIANT + whole-cell: every root_mod shard
    # trips identically on the whole-cell budget), then emit the shard-owned masks
    # in lex-by-position order.
    final_candidates = _join_and_prune(
        frontier_tuples, frontier_masks, drop_conflicts=(k == _PAIR_LEVEL), on_count=_budget_check
    )
    _budget_check(len(final_candidates))
    # Target-level census: raw |C_k| (pre-shard); frequent OMITTED (Stage-1B decides).
    _emit_census(
        cell_label=cell_label,
        k=k,
        level=k,
        candidates=len(final_candidates),
        min_hits=min_hits,
        n_bars=resolved_n_bars,
    )
    return [_mask_of(tpl) for tpl in final_candidates if tpl[0] % big_w == w]
