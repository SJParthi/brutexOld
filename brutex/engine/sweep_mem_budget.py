"""Rank-region memory budget planning for the persisted sweep (DEC-SWEEP-STREAMING-RANK-001).

The per-tuple Phase-C batch rank retains O(kept) rich state — the
``(mask, ResultMetrics)`` pairs plus :func:`brutex.ranking.rank_combos`' z-term
lists — at roughly ``0.7-1.3 KB`` per kept mask (the judged engine-scale spec's
per-field arithmetic, a PROJECTION, not a benchmark). A tuple keeping tens of
millions of masks therefore exhausts the CLAUDE.md §8 48 GB envelope and dies
with a bare ``MemoryError``. This module is the PURE planning half of the fix:

* :func:`resolve_bounded_rank_mode` / :func:`resolve_mem_budget_bytes` — the two
  operator env knobs (``BRUTEX_SWEEP_BOUNDED_RANK`` ∈ ``{off, auto, on}``,
  default ``auto``; ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``, default RAM-derived
  per DEC-RAM-BUDGETS-001 — ``total RAM × 1/6`` when detected, DIVIDED by the
  live EVAL-worker count on the parallel path
  (DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001 — the pool initializer exports the
  INTERNAL :data:`BUDGET_WORKERS_ENV`, read by :func:`resolve_budget_workers`;
  the explicit override stays per-worker UNDIVIDED, the #1352 contract; the
  8 GiB floor when RAM is undetectable, undivided).
* :func:`plan_rank_memory` — ONE deterministic pre-flight decision per tuple,
  taken from the config alone (``n_masks`` is ``C(n, k)``, known before any
  allocation): below budget the plan keeps today's batch path byte-identical;
  past budget mode ``auto`` ENGAGES the bounded columnar rank
  (:mod:`brutex.engine.sweep_stream`) while mode ``off`` returns a REFUSAL
  verdict the runner raises as the graceful, tuple-skippable
  :class:`~brutex.engine.sweep_run.SweepMemoryBudgetError` — strictly dominating
  today's raw ``MemoryError`` crash.
* :func:`watermark_message` — the mid-flight message for the EXACT owned-bytes
  watermark the bounded accumulator enforces per append.

The verdict/message split keeps this module import-cycle-free: the typed error
class lives beside its :class:`~brutex.engine.sweep_run.SweepComboError` parent
in :mod:`brutex.engine.sweep_run` (which imports THIS module), so nothing here
imports the runner.

Honest envelope (Golden Rule #10): the pre-flight projection is a MODEL —
``n_masks × BATCH_RANK_BYTES_PER_KEPT`` deliberately takes NO credit for
conflict/dedup/min-hits pruning (conservative, never an under-estimate by
construction of the kept ⊆ enumerated bound), and the 1024 B/kept constant is
the spec's projected mid-range, not a measured allocator figure. The bounded
path this plan can engage is O(kept) columnar memory (~80-105 B/kept payload —
see :class:`~brutex.engine.sweep_stream.SurvivorColumns`), NOT O(1); a genuine
``MemoryError`` remains a bare global stop (DEC-LAKE-WRITE-HALT-001) — the plan
narrows the window in which one can occur, it cannot abolish it. Every function
here is O(1); nothing touches the hot per-mask loop.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final

from brutex.config.hardware_envelope import memory_budget_bytes

# DEC-RUST-SWEEP-OPS-001 (slice 1b): the default-OFF Rust routing facade. The
# `plan_rank_memory` decision table gates on `sweep_ops_rust_enabled()` FIRST
# so the flag-OFF default path is byte-identical; `_rank_plan_decision_python`
# below is the live oracle. The mode-`on` config raises + the refusal message
# rendering + both env resolvers stay Python.
from brutex.engine.sweep_ops_rust import routed_rank_plan, sweep_ops_rust_enabled

#: Env knob selecting the bounded-rank mode (``off`` / ``auto`` / ``on``).
BOUNDED_RANK_MODE_ENV: Final[str] = "BRUTEX_SWEEP_BOUNDED_RANK"

#: Env knob for the rank-region memory budget in bytes (``> 0``).
MEM_BUDGET_ENV: Final[str] = "BRUTEX_SWEEP_MEM_BUDGET_BYTES"

#: INTERNAL divisor seam (DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001): the live
#: EVAL-worker count the RAM-derived DEFAULT budget divides by, exported into
#: each pool worker's environment by ``sweep_parallel._worker_init`` from the
#: pool context — NOT an operator knob. Before this wiring each of the pool's
#: N eval workers independently admitted the FULL ``total × 1/6`` (aggregate
#: ~1.167× total RAM at the AUTO cap's 7 eval workers — the C4/W7 root). The
#: explicit :data:`MEM_BUDGET_ENV` override is a PER-WORKER figure and stays
#: UNDIVIDED (the #1352 contract); unset → 1 (the sequential/CLI single-process
#: path, byte-identical to the pre-wire default).
BUDGET_WORKERS_ENV: Final[str] = "BRUTEX_SWEEP_BUDGET_WORKERS"

#: The rank-region budget FLOOR: 8 GiB per tuple — the original hand-picked
#: HEURISTIC for the CLAUDE.md §8 envelope (48 GB box, ~10 sweep cores →
#: several concurrent tuples must coexist with the OS + lake caches), NOT a
#: measured limit. Since DEC-RAM-BUDGETS-001 the DEFAULT budget is derived
#: from the machine's detected total RAM (:data:`MEM_BUDGET_FRACTION`); this
#: constant is applied EXACTLY when RAM detection fails (the honest
#: undetected sentinel — pre-derivation behavior, bit-for-bit). The operator
#: override stays ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``. Engaging the bounded
#: path early is result-safe — bounded and batch rank are pinned
#: bit-identical — so a conservative default only ever trades speed, never bytes.
DEFAULT_MEM_BUDGET_BYTES: Final[int] = 8 * 1024**3

#: The detected-RAM share the default rank-region budget claims
#: (DEC-RAM-BUDGETS-001): ``1/6`` gives EXACT parity with the old 8 GiB
#: constant on the CLAUDE.md §8 48 GiB reference box (regression-pinned in
#: ``tests/unit/test_hardware_envelope.py``); a smaller machine derives a
#: smaller budget (portability), a larger one gains headroom. A PORTABILITY
#: choice, NOT a measured figure (Golden Rule #10).
MEM_BUDGET_FRACTION: Final[float] = 1.0 / 6.0

#: The batch-path retention MODEL: bytes per kept mask across the
#: ``(mask, ResultMetrics)`` pair + `rank_combos`' z-term lists. The judged
#: spec's per-field arithmetic projects ~0.7-1.3 KB/kept on CPython 3.12; 1024
#: is the documented mid-range PROJECTION (Golden Rule #10 — a model, not a
#: benchmark; the ``MemoryError`` backstop is unchanged if it under-estimates).
BATCH_RANK_BYTES_PER_KEPT: Final[int] = 1024


class BoundedRankMode(Enum):
    """The three ``BRUTEX_SWEEP_BOUNDED_RANK`` modes (default ``AUTO``).

    * ``OFF`` — never rank via the bounded columnar path; a tuple whose batch
      projection exceeds the budget is REFUSED gracefully (typed error,
      tuple-skip) instead of crashing with a raw ``MemoryError``.
    * ``AUTO`` — byte-identical batch path below budget; past budget the
      bounded path ENGAGES (loudly logged) when the tuple is eligible
      (digest path + finite ``top_k``), else the tuple is refused gracefully.
    * ``ON`` — always rank via the bounded path (operator-forced; the
      dual-mode bit-identity arm of the test suite). Requires the digest path
      and a finite ``top_k`` at validation time.
    """

    OFF = "off"
    AUTO = "auto"
    ON = "on"


def resolve_bounded_rank_mode(environ: Mapping[str, str] | None = None) -> BoundedRankMode:
    """Resolve the bounded-rank mode from ``BRUTEX_SWEEP_BOUNDED_RANK``.

    Unset/blank → :attr:`BoundedRankMode.AUTO` (the default). The value is
    case-insensitive and whitespace-trimmed. Anything else raises — an operator
    typo must fail LOUD at sweep start, never silently fall back to a mode the
    operator did not choose (Golden Rule #1).

    Parameters
    ----------
    environ
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Raises
    ------
    ValueError
        If the value is not one of ``off`` / ``auto`` / ``on``.
    """
    env = os.environ if environ is None else environ
    raw = env.get(BOUNDED_RANK_MODE_ENV, "").strip().lower()
    if not raw:
        return BoundedRankMode.AUTO
    try:
        return BoundedRankMode(raw)
    except ValueError:
        raise ValueError(
            f"{BOUNDED_RANK_MODE_ENV}={raw!r} is not a bounded-rank mode; "
            f"expected one of 'off', 'auto', 'on' (default: 'auto')"
        ) from None


def resolve_budget_workers(environ: Mapping[str, str] | None = None) -> int:
    """Resolve the live EVAL-worker divisor from :data:`BUDGET_WORKERS_ENV`.

    The INTERNAL seam of DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001: the parallel
    pool initializer (``sweep_parallel._worker_init``) exports the pool's
    eval-worker count into each worker's environment, so the per-worker
    RAM-derived default budget genuinely divides across the pool. Unset/blank
    → ``1`` (the sequential / single-process path, byte-identical to the
    pre-wire default). Garbage or a value ``< 1`` raises — a broken divisor
    must never silently mis-size admission (Golden Rule #1; the
    :data:`MEM_BUDGET_ENV` fail-loud posture).

    Parameters
    ----------
    environ
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Raises
    ------
    ValueError
        If the value is set and not an integer ``>= 1``.
    """
    env = os.environ if environ is None else environ
    raw = env.get(BUDGET_WORKERS_ENV, "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{BUDGET_WORKERS_ENV}={raw!r} is not an integer worker count (>= 1 required); "
            f"this is the INTERNAL pool-initializer seam, not an operator knob"
        ) from None
    if value < 1:
        raise ValueError(f"{BUDGET_WORKERS_ENV}={value} must be >= 1")
    return value


def resolve_mem_budget_bytes(
    environ: Mapping[str, str] | None = None, *, workers: int | None = None
) -> int:
    """Resolve the rank-region byte budget from ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``.

    Delegates to the COMMON :func:`brutex.config.hardware_envelope.memory_budget_bytes`
    (DEC-RAM-BUDGETS-001). Resolution order: a set, non-blank env value is the
    validated explicit override (byte-unchanged #1352 contract — a non-integer
    or non-positive value raises, a broken budget must never silently disable
    the guard, Golden Rule #1; the override is a PER-WORKER figure and is
    NEVER divided); unset/blank → the RAM-derived default —
    ``detected total RAM`` × :data:`MEM_BUDGET_FRACTION` **÷ the live
    eval-worker count** (DEC-SWEEP-MEM-BUDGET-WORKERS-WIRE-001 — *workers*
    when given, else :func:`resolve_budget_workers` from the environment; the
    pool initializer exports it, so N parallel eval workers now share ONE
    budget fraction instead of EACH admitting ``total × 1/6``) when detection
    succeeds, else :data:`DEFAULT_MEM_BUDGET_BYTES` (the 8 GiB floor) EXACTLY
    — the undetected sentinel keeps the pre-derivation constant bit-for-bit,
    UNdivided (the :func:`memory_budget_bytes` floor law, stated honestly).
    RAM detection is cached once per process, so the resolved default is
    stable for the whole run even though the production runner re-resolves
    per tuple (no mid-run flapping).

    Parameters
    ----------
    environ
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.
    workers
        Explicit eval-worker divisor (``>= 1``); ``None`` resolves it from
        :data:`BUDGET_WORKERS_ENV` via :func:`resolve_budget_workers`.

    Raises
    ------
    ValueError
        If the env value is set and not a positive integer, or the divisor
        (env or explicit) is not an integer ``>= 1``.
    """
    return memory_budget_bytes(
        fraction=MEM_BUDGET_FRACTION,
        floor_bytes=DEFAULT_MEM_BUDGET_BYTES,
        workers=resolve_budget_workers(environ) if workers is None else workers,
        env=environ,
        env_var=MEM_BUDGET_ENV,
    )


@dataclass(frozen=True, slots=True)
class RankMemoryPlan:
    """One tuple's pre-flight rank-memory verdict (pure data — the runner acts on it).

    Attributes
    ----------
    mode
        The resolved :class:`BoundedRankMode`.
    budget_bytes
        The resolved budget the projection was compared against.
    projected_bytes
        ``n_masks × BATCH_RANK_BYTES_PER_KEPT`` — the conservative batch-path
        retention MODEL (no prune credit; a projection, not a measurement).
    n_masks
        The tuple's enumerated mask count ``C(n, k)`` the projection used.
    bounded_top_k
        Non-``None`` iff the BOUNDED columnar rank is engaged for this tuple —
        then it is the validated finite ``top_k`` the bounded selection needs.
        ``None`` = today's batch path.
    refusal
        Non-``None`` iff the tuple must be REFUSED gracefully: the fully
        rendered reason + remedies message the runner wraps in
        :class:`~brutex.engine.sweep_run.SweepMemoryBudgetError`. Mutually
        exclusive with ``bounded_top_k``.
    """

    mode: BoundedRankMode
    budget_bytes: int
    projected_bytes: int
    n_masks: int
    bounded_top_k: int | None
    refusal: str | None

    @property
    def bounded(self) -> bool:
        """``True`` iff the bounded columnar rank is engaged for this tuple."""
        return self.bounded_top_k is not None

    def require_bounded_top_k(self) -> int:
        """Return the engaged bounded ``top_k`` — raises if the plan is not bounded.

        A typed accessor so the runner's bounded branch gets a plain ``int``
        without a re-narrowing branch of its own; calling it on a non-bounded
        plan is a caller bug and fails LOUD.
        """
        if self.bounded_top_k is None:
            raise ValueError(
                "RankMemoryPlan.require_bounded_top_k: plan did not engage the bounded rank"
            )
        return self.bounded_top_k


def _remedies(*, include_top_k: bool, include_digest: bool, include_mode: bool = True) -> str:
    """Render the remedy tail every refusal message carries (knobs by NAME).

    ``include_mode=False`` drops the "engage the bounded rank" clause for the
    MID-FLIGHT watermark message — the watermark only ever fires when the
    bounded rank is ALREADY engaged, so advising the operator to engage it
    would be a dishonest remedy (Golden Rule #10).
    """
    parts = [
        f"raise {MEM_BUDGET_ENV} (current budget applies per tuple)",
        "raise min_hits / lower k to shrink the kept population",
    ]
    if include_mode:
        parts.insert(
            1, f"set {BOUNDED_RANK_MODE_ENV}=auto or =on to engage the bounded columnar rank"
        )
    if include_top_k:
        parts.insert(
            1, "set a finite top_k (the bounded rank cannot retain an unbounded leaderboard)"
        )
    if include_digest:
        parts.insert(
            1,
            "run via the production digest path (the bounded rank is refused on the legacy no-digest path)",
        )
    return "remedies: " + "; ".join(parts)


def watermark_message(*, owned_bytes: int, budget_bytes: int, n_kept: int) -> str:
    """Render the mid-flight watermark breach message (EXACT owned bytes).

    Unlike the pre-flight projection this figure is exact — the bounded
    accumulator owns its arrays, so ``owned_bytes`` is its real payload size
    when it crossed the budget.
    """
    return (
        f"bounded-rank memory watermark breached mid-flight: the survivor columns own "
        f"{owned_bytes} bytes after {n_kept} kept masks, exceeding "
        f"{MEM_BUDGET_ENV}={budget_bytes}; the tuple is skipped for a deterministic redo. "
        + _remedies(include_top_k=False, include_digest=False, include_mode=False)
    )


def plan_rank_memory(
    *,
    n_masks: int,
    top_k: int | None,
    digest_path: bool,
    mode: BoundedRankMode,
    budget_bytes: int,
) -> RankMemoryPlan:
    """Take ONE deterministic pre-flight rank-memory decision for a tuple.

    Pure in its inputs (config + resolved knobs only — no data dependence), so
    a given grid config always takes the same path on every machine and every
    resume (Golden Rules #4/#7; result bytes are identical either way — the
    bounded rank is pinned bit-identical to the batch rank).

    Decision table (``projected = n_masks × BATCH_RANK_BYTES_PER_KEPT``):

    ========  ============  ==============================================
    mode      projection    verdict
    ========  ============  ==============================================
    ``on``    (any)         bounded (digest path + finite ``top_k`` REQUIRED
                            — a violation raises ``ValueError``: an explicit
                            operator forcing with an impossible shape is a
                            config error, not a tuple condition)
    ``off``   ≤ budget      batch (byte-identical default)
    ``off``   > budget      REFUSAL (graceful typed error, never a raw
                            ``MemoryError`` crash)
    ``auto``  ≤ budget      batch (byte-identical default)
    ``auto``  > budget      bounded when eligible (digest + finite ``top_k``),
                            else REFUSAL naming the missing prerequisite
    ========  ============  ==============================================

    Parameters
    ----------
    n_masks
        The tuple's enumerated mask count ``C(n, k)``.
    top_k
        The tuple's ``top_k`` (``None`` = rank-all — ineligible for bounding).
    digest_path
        ``True`` on the production sentinel-authoritative digest path.
    mode, budget_bytes
        The resolved operator knobs.

    Raises
    ------
    ValueError
        Mode ``on`` without the digest path or without a finite ``top_k``.
    """
    if mode is BoundedRankMode.ON:
        # The two config raises fire BEFORE the routed decision (they are
        # config errors, not decision outcomes — pure hoist, no observable
        # reorder: the projection arithmetic has no side effect).
        if not digest_path:
            raise ValueError(
                f"{BOUNDED_RANK_MODE_ENV}=on is refused on the LEGACY no-digest sweep path: "
                f"the bounded rank is a production digest-path feature (the legacy probe "
                f"semantics keep the eager batch rank byte-verbatim)"
            )
        if top_k is None:
            raise ValueError(
                f"{BOUNDED_RANK_MODE_ENV}=on requires a finite top_k: the bounded rank "
                f"selects top-K via a size-K heap and cannot retain an unbounded leaderboard "
                f"(set SweepRunConfig.top_k, or use mode 'auto'/'off')"
            )
    if sweep_ops_rust_enabled():
        projected, verdict = routed_rank_plan(
            n_masks,
            budget_bytes,
            mode.value,
            top_k is not None,
            digest_path,
            python_law=lambda: _rank_plan_decision_python(
                n_masks, budget_bytes, mode.value, top_k is not None, digest_path
            ),
        )
    else:
        projected, verdict = _rank_plan_decision_python(
            n_masks, budget_bytes, mode.value, top_k is not None, digest_path
        )
    if verdict in {"batch", "bounded"}:
        return RankMemoryPlan(
            mode=mode,
            budget_bytes=budget_bytes,
            projected_bytes=projected,
            n_masks=n_masks,
            bounded_top_k=top_k if verdict == "bounded" else None,
            refusal=None,
        )
    prefix = (
        f"projected batch-rank retention {projected} bytes "
        f"({n_masks} masks x {BATCH_RANK_BYTES_PER_KEPT} B/kept model, no prune credit) "
        f"exceeds {MEM_BUDGET_ENV}={budget_bytes}"
    )
    if verdict == "refuse_off":
        refusal = (
            f"{prefix} and {BOUNDED_RANK_MODE_ENV}=off forbids the bounded columnar rank; "
            f"the tuple is refused BEFORE any survivor allocation (graceful skip, "
            f"never a raw MemoryError). " + _remedies(include_top_k=False, include_digest=False)
        )
    elif verdict == "refuse_no_top_k":
        refusal = (
            f"{prefix} and the bounded columnar rank cannot engage with top_k=None "
            f"(rank-all needs the full leaderboard in memory). "
            + _remedies(include_top_k=True, include_digest=False)
        )
    else:  # "refuse_no_digest" — the decision law's only remaining verdict
        refusal = (
            f"{prefix} and the bounded columnar rank is refused on the LEGACY "
            f"no-digest path. " + _remedies(include_top_k=False, include_digest=True)
        )
    return RankMemoryPlan(
        mode=mode,
        budget_bytes=budget_bytes,
        projected_bytes=projected,
        n_masks=n_masks,
        bounded_top_k=None,
        refusal=refusal,
    )


def _rank_plan_decision_python(
    n_masks: int,
    budget_bytes: int,
    mode_token: str,
    has_top_k: bool,
    digest_path: bool,
) -> tuple[int, str]:
    """The verbatim pre-flight decision table — the live parity oracle
    (DEC-RUST-SWEEP-OPS-001; retained until the final slice's default flip).

    Returns ``(projected_bytes, verdict)`` with verdict one of ``"batch"`` /
    ``"bounded"`` / ``"refuse_off"`` / ``"refuse_no_top_k"`` /
    ``"refuse_no_digest"`` — the exact branch order of the pre-conversion
    :func:`plan_rank_memory` body (mode ``on``'s config raises fire in the
    caller BEFORE this decision, so its arm is unconditionally bounded).
    """
    projected = n_masks * BATCH_RANK_BYTES_PER_KEPT
    if mode_token == BoundedRankMode.ON.value:
        return projected, "bounded"
    if projected <= budget_bytes:
        return projected, "batch"
    if mode_token == BoundedRankMode.OFF.value:
        return projected, "refuse_off"
    if not has_top_k:
        return projected, "refuse_no_top_k"
    if not digest_path:
        return projected, "refuse_no_digest"
    return projected, "bounded"


__all__ = [
    "BATCH_RANK_BYTES_PER_KEPT",
    "BOUNDED_RANK_MODE_ENV",
    "BUDGET_WORKERS_ENV",
    "DEFAULT_MEM_BUDGET_BYTES",
    "MEM_BUDGET_ENV",
    "MEM_BUDGET_FRACTION",
    "BoundedRankMode",
    "RankMemoryPlan",
    "plan_rank_memory",
    "resolve_bounded_rank_mode",
    "resolve_budget_workers",
    "resolve_mem_budget_bytes",
    "watermark_message",
]
