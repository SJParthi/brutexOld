"""Rank-region memory budget planning for the persisted sweep (DEC-SWEEP-STREAMING-RANK-001).

The per-tuple Phase-C batch rank retains O(kept) rich state — the
``(mask, ResultMetrics)`` pairs plus :func:`brutex.ranking.rank_combos`' z-term
lists — at roughly ``0.7-1.3 KB`` per kept mask (the judged engine-scale spec's
per-field arithmetic, a PROJECTION, not a benchmark). A tuple keeping tens of
millions of masks therefore exhausts the CLAUDE.md §8 48 GB envelope and dies
with a bare ``MemoryError``. This module is the PURE planning half of the fix:

* :func:`resolve_bounded_rank_mode` / :func:`resolve_mem_budget_bytes` — the two
  operator env knobs (``BRUTEX_SWEEP_BOUNDED_RANK`` ∈ ``{off, auto, on}``,
  default ``auto``; ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``, default 8 GiB).
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

#: Env knob selecting the bounded-rank mode (``off`` / ``auto`` / ``on``).
BOUNDED_RANK_MODE_ENV: Final[str] = "BRUTEX_SWEEP_BOUNDED_RANK"

#: Env knob for the rank-region memory budget in bytes (``> 0``).
MEM_BUDGET_ENV: Final[str] = "BRUTEX_SWEEP_MEM_BUDGET_BYTES"

#: Default rank-region budget: 8 GiB per tuple. A HEURISTIC default for the
#: CLAUDE.md §8 envelope (48 GB box, ~10 sweep cores → several concurrent
#: tuples must coexist with the OS + lake caches), NOT a measured limit; the
#: operator overrides it via ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``. Engaging the
#: bounded path early is result-safe — bounded and batch rank are pinned
#: bit-identical — so a conservative default only ever trades speed, never bytes.
DEFAULT_MEM_BUDGET_BYTES: Final[int] = 8 * 1024**3

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


def resolve_mem_budget_bytes(environ: Mapping[str, str] | None = None) -> int:
    """Resolve the rank-region byte budget from ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``.

    Unset/blank → :data:`DEFAULT_MEM_BUDGET_BYTES` (8 GiB). A non-integer or
    non-positive value raises — a broken budget must never silently disable
    the guard (Golden Rule #1).

    Parameters
    ----------
    environ
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Raises
    ------
    ValueError
        If the value is not a positive integer.
    """
    env = os.environ if environ is None else environ
    raw = env.get(MEM_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_MEM_BUDGET_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{MEM_BUDGET_ENV}={raw!r} is not an integer byte count (> 0 required)"
        ) from None
    if value <= 0:
        raise ValueError(f"{MEM_BUDGET_ENV}={value} must be > 0 bytes")
    return value


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
    projected = n_masks * BATCH_RANK_BYTES_PER_KEPT
    if mode is BoundedRankMode.ON:
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
        return RankMemoryPlan(
            mode=mode,
            budget_bytes=budget_bytes,
            projected_bytes=projected,
            n_masks=n_masks,
            bounded_top_k=top_k,
            refusal=None,
        )
    if projected <= budget_bytes:
        return RankMemoryPlan(
            mode=mode,
            budget_bytes=budget_bytes,
            projected_bytes=projected,
            n_masks=n_masks,
            bounded_top_k=None,
            refusal=None,
        )
    prefix = (
        f"projected batch-rank retention {projected} bytes "
        f"({n_masks} masks x {BATCH_RANK_BYTES_PER_KEPT} B/kept model, no prune credit) "
        f"exceeds {MEM_BUDGET_ENV}={budget_bytes}"
    )
    if mode is BoundedRankMode.OFF:
        refusal = (
            f"{prefix} and {BOUNDED_RANK_MODE_ENV}=off forbids the bounded columnar rank; "
            f"the tuple is refused BEFORE any survivor allocation (graceful skip, "
            f"never a raw MemoryError). " + _remedies(include_top_k=False, include_digest=False)
        )
    elif top_k is None:
        refusal = (
            f"{prefix} and the bounded columnar rank cannot engage with top_k=None "
            f"(rank-all needs the full leaderboard in memory). "
            + _remedies(include_top_k=True, include_digest=False)
        )
    elif not digest_path:
        refusal = (
            f"{prefix} and the bounded columnar rank is refused on the LEGACY "
            f"no-digest path. " + _remedies(include_top_k=False, include_digest=True)
        )
    else:
        return RankMemoryPlan(
            mode=mode,
            budget_bytes=budget_bytes,
            projected_bytes=projected,
            n_masks=n_masks,
            bounded_top_k=top_k,
            refusal=None,
        )
    return RankMemoryPlan(
        mode=mode,
        budget_bytes=budget_bytes,
        projected_bytes=projected,
        n_masks=n_masks,
        bounded_top_k=None,
        refusal=refusal,
    )


__all__ = [
    "BATCH_RANK_BYTES_PER_KEPT",
    "BOUNDED_RANK_MODE_ENV",
    "DEFAULT_MEM_BUDGET_BYTES",
    "MEM_BUDGET_ENV",
    "BoundedRankMode",
    "RankMemoryPlan",
    "plan_rank_memory",
    "resolve_bounded_rank_mode",
    "resolve_mem_budget_bytes",
    "watermark_message",
]
