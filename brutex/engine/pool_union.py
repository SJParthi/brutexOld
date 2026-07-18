"""Multi-key pool-admission union — the Stage-3 + cascade admission law.

LANE W1 item 3 (DEC-RANK-POOL-UNION-ADMIT-001, operator ruling 2026-07-18):
both downstream pools — the Stage-3 refinement leaderboards
(:func:`brutex.engine.stage3_select.group_survivors`) and the Stage-4 cascade
validation pool (:func:`brutex.engine.cascade_wire.spec_from_store_rows`) —
were admitted by a SINGLE key (Sharpe descending), so a DD-optimal /
high-win-rate / frontier row outside the Sharpe top-K read annotation-only
``not_validated`` forever (the rank-1-forever shape). This module is the ONE
shared union law both seams consume (CLAUDE.md §11 COMMON — one vocabulary,
never two copies).

## The admission law

The pool is the interleaved round-robin dedup-union across the FIVE sources,
in the FIXED :data:`POOL_UNION_SOURCE_ORDER`:

1. ``sharpe`` — Sharpe descending (today's law, always source #1 so the
   single-source reduction is byte-identical to the legacy admission —
   parity-pinned);
2. ``preset`` — the ACTIVE ranking preset via the ONE
   :func:`brutex.ranking.presets.sort_by_preset` law (the DEFAULT
   ``min_dd_first`` at both production seams — neither seam carries a preset
   context; injectable for future preset-threaded callers);
3. ``dd`` — ``max_drawdown_pct`` ascending;
4. ``win_rate`` — ``win_rate`` descending;
5. ``frontier`` — the persisted ε-Pareto members (``pareto_member is True``,
   DEC-RANK-PARETO-PERSIST-001's explicit Stage-3 frontier inclusion).

Round-robin apportionment: per round, each source (in the fixed order)
admits its best not-yet-admitted candidate until the cap is reached or every
source is exhausted — the TOTAL cap is UNCHANGED vs the legacy single-key
admission (pool budget never grows; minority-axis rows displace tail-Sharpe
rows). Every source's internal order rides the store tie law: the axis key,
then ``run_id`` ascending, then input order (a strict total order — the
``ResultsStore.top_k`` two-pass discipline collapsed into one key tuple).

PRECONDITION for the per-source guarantee (review round 1, honesty): every
non-empty source is guaranteed ≥ 1 admission ONLY when
``cap >= the number of non-empty sources``. Below that precondition sources
admit in the fixed source order and the TAIL sources get none — e.g. at
``cap=3`` over five non-empty sources, ``win_rate`` (#4) and ``frontier``
(#5) admit ZERO rows, so the "explicit ε-Pareto frontier inclusion" does
NOT hold there. A starved shape emits one capped
``pool_union.sources_starved`` WARNING naming the starved sources
(warn-once per starved-source set per process). Both production caps —
Stage-3 ``DEFAULT_STAGE3_TOP_K=10_000`` and the cascade's
``DEFAULT_PERSIST_TOP_K=1_000`` — dwarf the five sources, so the
precondition holds everywhere in production and the WARN never fires there.

The EMITTED pool keeps TODAY's ordering law (Sharpe desc, ``run_id`` asc,
input order) over the union membership — the union changes MEMBERSHIP only,
never the downstream ordering discipline.

## Disclosed W1 deviations (design map vs the persisted reality)

* The design map's ``composite DESC`` source is HONESTLY OMITTED:
  ``composite_score`` is NOT persisted on :class:`ResultRow` (it is a
  pool-z-scored leaderboard quantity — the /results composite persist is the
  separately-tracked HIGH #40). The preset source's ``RankRecord`` projection
  therefore carries ``composite_score=None`` (ranks after every scored
  record under the preset law — uniformly inert across a pool of rows) and
  ``max_consecutive_losses=0`` (uniform constant — an inert tie term; the
  real per-row value is a ComboAggregate quantity, not a ResultRow one).
* The frontier source's internal order is the SHARPE law, not the design
  map's ``(-composite, mask)`` — same reason; disclosed in MIGRATION.md
  #1972.

## Honest envelope (Golden Rule #10)

O(sources · n log n) sorts + O(cap) round-robin — a between-stage,
once-per-selection path, never a Rule-#5 hot path; the design map marks
item 3 Rust-optional (pure orchestration; the per-tuple persist-time
kernels live in ``rust/fno-math/src/rank_select.rs``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

from brutex.engine.results_store import ResultRow
from brutex.ranking.presets import DEFAULT_PRESET, RankingPreset, RankRecord, resolve_preset

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence

__all__ = [
    "POOL_UNION_SOURCE_ORDER",
    "axis_better_key",
    "rank_record_of_row",
    "reset_starved_warn_for_tests",
    "round_robin_union",
    "union_select_rows",
]

_LOG = structlog.get_logger("brutex.engine.pool_union")

#: Warn-once memo for the starved-cap shape (one WARNING per distinct
#: starved-source-name set per process — the rust_rank_select
#: ``_OBSERVABILITY_MEMO`` log-volume discipline; capped at the ≤ 2⁵ possible
#: source subsets by construction).
_STARVED_WARNED: Final[set[tuple[str, ...]]] = set()


def reset_starved_warn_for_tests() -> None:
    """Clear the starved-cap warn-once memo (test isolation ONLY)."""
    _STARVED_WARNED.clear()


#: The FIXED admission-source order (module docstring) — ``sharpe`` is
#: deliberately FIRST so the single-source reduction is the legacy law.
POOL_UNION_SOURCE_ORDER: Final[tuple[str, ...]] = (
    "sharpe",
    "preset",
    "dd",
    "win_rate",
    "frontier",
)


def rank_record_of_row(row: ResultRow) -> RankRecord:
    """Project a persisted :class:`ResultRow` onto the preset law's record.

    ``composite_score=None`` + ``max_consecutive_losses=0`` are the disclosed
    uniform stand-ins (module docstring) — aggregate-side quantities a
    persisted row does not carry; uniform across the pool, they are inert
    tie terms under every preset key.
    """
    return RankRecord(
        run_id=row.run_id,
        max_drawdown_pct=row.max_drawdown_pct,
        composite_score=None,
        max_consecutive_losses=0,
        profit_factor=row.profit_factor,
        win_rate=row.win_rate,
        calmar_ratio=row.calmar_ratio,
    )


def axis_better_key(
    source: str, row: ResultRow, *, preset: RankingPreset
) -> tuple[float | int, ...] | None:
    """The ascending better-first AXIS key of *row* under *source*.

    Returns ``None`` when the row does not QUALIFY for the source (only the
    ``frontier`` source filters — a non-member/untagged row carries no
    frontier claim; ``None`` and ``False`` both disqualify, the honest
    pre-W1-store degrade). The caller appends the ``(run_id, input-order)``
    tie tail — the axis key alone need not be total.

    Raises ``ValueError`` on an unknown source token (a poisoned source list
    must never silently sort).
    """
    if source == "sharpe":
        return (-row.sharpe_ratio,)
    if source == "preset":
        key = preset.key(rank_record_of_row(row))
        if preset.reverse:
            # Descending presets carry numeric-only keys — negate into the
            # one ascending better-first convention.
            return tuple(-part for part in key)
        return key
    if source == "dd":
        return (row.max_drawdown_pct,)
    if source == "win_rate":
        return (-row.win_rate,)
    if source == "frontier":
        if row.pareto_member is not True:
            return None
        # Frontier-internal order: the Sharpe law (composite_score is not
        # persisted on ResultRow — the disclosed deviation, module docstring).
        return (-row.sharpe_ratio,)
    raise ValueError(f"pool_union: unknown admission source {source!r}")


def round_robin_union(
    ordered_sources: Sequence[Sequence[int]],
    *,
    cap: int,
    source_names: Sequence[str] | None = None,
) -> set[int]:
    """Interleaved round-robin dedup-union over best-first candidate id lists.

    Per round, each source in order admits its best NOT-YET-ADMITTED
    candidate (a per-source cursor — an already-admitted candidate is
    skipped without consuming the source's turn), until *cap* admissions or
    every source is exhausted. Deterministic; with ONE source this is
    exactly ``set(source[:cap])`` (the legacy top-K prefix — parity-pinned).

    PRECONDITION (review round 1, honesty): every non-empty source admits
    ≥ 1 candidate ONLY when ``cap >= the number of non-empty sources``.
    Below that, sources admit in the fixed input order and the TAIL sources
    admit nothing. A source that admitted ZERO candidates while still
    holding a candidate OUTSIDE the final admitted set is STARVED — the
    starved shape logs one capped ``pool_union.sources_starved`` WARNING
    (warn-once per starved-source set per process; the production caps
    10_000 / 1_000 never trip it). *source_names* labels the sources for
    that WARN (default: positional ``#<index>`` labels).

    Raises ``ValueError`` on ``cap < 1`` (an empty pool budget is a caller
    bug, never a silent empty admission).
    """
    if cap < 1:
        raise ValueError(f"pool_union: cap must be >= 1, got {cap}")
    admitted: set[int] = set()
    cursors = [0] * len(ordered_sources)
    admissions = [0] * len(ordered_sources)
    exhausted = False
    while len(admitted) < cap and not exhausted:
        exhausted = True
        for index, source in enumerate(ordered_sources):
            cursor = cursors[index]
            while cursor < len(source) and source[cursor] in admitted:
                cursor += 1
            cursors[index] = cursor
            if cursor >= len(source):
                continue
            exhausted = False
            admitted.add(source[cursor])
            admissions[index] += 1
            cursors[index] = cursor + 1
            if len(admitted) >= cap:
                break
    non_empty = sum(1 for source in ordered_sources if source)
    if cap < non_empty:
        _warn_starved_once(ordered_sources, admitted, admissions, source_names, cap, non_empty)
    return admitted


def _warn_starved_once(
    ordered_sources: Sequence[Sequence[int]],
    admitted: set[int],
    admissions: Sequence[int],
    source_names: Sequence[str] | None,
    cap: int,
    non_empty: int,
) -> None:
    """Emit the capped ``pool_union.sources_starved`` WARNING (memoised).

    Starved = a non-empty source with ZERO admissions that still holds a
    candidate outside the final admitted set (its inclusion guarantee is
    genuinely broken — a zero-admission source whose every candidate was
    admitted through other sources is covered, not starved). Reached only
    on the ``cap < non_empty`` shape (the caller gates), so the O(n)
    membership sweep never runs on the production caps.
    """
    starved = tuple(
        source_names[index] if source_names is not None else f"#{index}"
        for index, source in enumerate(ordered_sources)
        if source
        and admissions[index] == 0
        and any(candidate not in admitted for candidate in source)
    )
    if not starved or starved in _STARVED_WARNED:
        return
    _STARVED_WARNED.add(starved)
    _LOG.warning(
        "pool_union.sources_starved",
        cap=cap,
        non_empty_sources=non_empty,
        starved_sources=list(starved),
    )


def union_select_rows(
    rows: Sequence[ResultRow],
    *,
    cap: int,
    preset: RankingPreset | None = None,
    sources: Sequence[str] = POOL_UNION_SOURCE_ORDER,
) -> tuple[ResultRow, ...]:
    """Select the multi-key admission union of *rows*, emitted in today's order.

    The pure sort-based composition (the cascade seam's binding and the
    bounded Stage-3 arm's oracle): per source a two-pass-law ordering
    (axis key + ``run_id`` asc + input order), the round-robin union to
    *cap*, then the emitted pool in TODAY's discipline — Sharpe descending,
    ``run_id`` ascending, input order (membership changes; the downstream
    ordering law does not).

    Parameters
    ----------
    rows
        One pool's persisted rows (any order — the selection is
        discovery-order-free by the total-order keys).
    cap
        The pool budget (``>= 1``) — UNCHANGED vs the legacy single-key
        admission. The per-source ≥ 1 admission guarantee needs
        ``cap >= the number of non-empty sources`` (module docstring —
        below that the tail sources are starved, WARNED once).
    preset
        The ACTIVE preset for source #2; ``None`` resolves the DEFAULT
        (``min_dd_first`` — both production seams carry no preset context).
    sources
        The admission sources in order (default
        :data:`POOL_UNION_SOURCE_ORDER`); the ``("sharpe",)`` reduction is
        the legacy law byte-identically (parity-pinned).
    """
    active = resolve_preset(DEFAULT_PRESET) if preset is None else preset
    ordered_sources: list[list[int]] = []
    for source in sources:
        keyed: list[tuple[tuple[float | int, ...], str, int]] = []
        for seq, row in enumerate(rows):
            axis = axis_better_key(source, row, preset=active)
            if axis is None:
                continue
            keyed.append((axis, row.run_id, seq))
        keyed.sort()
        ordered_sources.append([seq for _axis, _run_id, seq in keyed])
    admitted = round_robin_union(ordered_sources, cap=cap, source_names=tuple(sources))
    emitted = sorted(admitted, key=lambda seq: (rows[seq].run_id, seq))
    emitted.sort(key=lambda seq: rows[seq].sharpe_ratio, reverse=True)
    return tuple(rows[seq] for seq in emitted)
