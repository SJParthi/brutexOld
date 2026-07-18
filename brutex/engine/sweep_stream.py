"""Columnar survivor retention + bounded top-K rank (DEC-SWEEP-STREAMING-RANK-001).

The batch rank path retains every kept mask's full 39-field
:class:`~brutex.metrics.compute.ResultMetrics` (≈0.7-1.3 KB each, the judged
spec's projection) until :func:`brutex.ranking.rank_combos` has scored the whole
tuple — O(kept) *rich* memory that exhausts the CLAUDE.md §8 48 GB envelope
past a few tens of millions of kept masks. This module is the bounded
alternative the memory-budget plan (:mod:`brutex.engine.sweep_mem_budget`) can
engage:

* :class:`SurvivorColumns` — appended once per kept mask WHILE its
  ``ResultMetrics`` is alive (the object is dropped right after), retaining
  ONLY the fields ranking consumes, columnar: the u128 mask (two u64 limbs),
  the gate verdict (evaluated inline via the SAME
  :func:`brutex.ranking.passes_gates`), the raw Sharpe (+ ``None`` bit — the
  sidecar + overfit pool need ``None`` distinguished from ``0.0``, NEVER
  NaN-encoded), the 7 composite z-term inputs (the SAME
  :func:`brutex.ranking.composite_term_inputs` float64s the batch rank uses),
  and — when the overfit gate is active — ``total_trades`` + ``skewness`` +
  ``kurtosis`` (+ ``None`` bits), the verified
  :func:`~brutex.ranking.overfit.deflated_sharpe_for` inputs (spec V8).
* :func:`rank_survivor_columns` — the bounded rank: the SAME shared internals
  as :func:`~brutex.ranking.rank_combos` (gate bits recorded via
  ``passes_gates``; the TWO populations preserved exactly — overfit N/V over
  ALL kept candidates, z-scores over the post-gate post-overfit survivors,
  spec V7), then top-K selection via :func:`heapq.nsmallest` on the strict
  total-order key ``(-composite, mask)`` — documented equivalent to
  ``sorted(...)[:k]``, hence BIT-IDENTICAL to the batch sort-then-slice.

Honest envelope (Golden Rule #10): retained memory is **O(kept) columnar**, NOT
O(1) — the payload arithmetic is 120.25 B/kept without overfit columns
(16 mask + 8 sharpe + 7×8 terms + 2 bits + the LANE-W1 40 B of selection-axis
columns: 3×8 int net_pnl/losing/expectancy + 2×8 float win_rate/dd —
DEC-RANK-QUOTA-PERSIST-1000-001, the parallel-heap twin's inputs) and
144.5 B/kept with the overfit columns (+8 trades + 2×8 skew/kurt + 2 bits),
and :meth:`SurvivorColumns.owned_nbytes`
reports the EXACT logical payload (``array`` over-allocation slack + CPython
object headers are excluded — the bench gate pins the per-entry slope on this
figure, so the ~10× reduction vs the ≈1 KB/kept batch path is a payload-level
claim, not an allocator RSS claim). The top-K heap is O(survivors · log K)
time, O(K) extra space; the z/composite pass is O(survivors) — nothing here is
O(1) total. A true O(top_k) rank is impossible without changing the
z-population semantics (a spec change, explicitly rejected).
"""

from __future__ import annotations

import heapq
from array import array
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from brutex.ranking import (
    N_COMPOSITE_TERMS,
    OverfitGateConfig,
    composite_scores,
    composite_term_inputs,
    dsr_z_contributions,
    ensure_unique_survivor_masks,
    overfit_gate_positions,
    passes_gates,
)
from brutex.ranking.multi_select import SelectionInputs, UnionSelection

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from brutex.metrics.compute import ResultMetrics

#: The injectable union-selection binding (the burn-in-checked
#: :func:`brutex.engine.rust_rank_select.union_selection` in production;
#: ``None`` keeps the legacy composite-only top-K byte-identical).
type UnionSelectionFn = Callable[..., UnionSelection]

_U64_MASK: Final[int] = (1 << 64) - 1

#: Exclusive upper bound of the storable mask space (two u64 limbs = u128).
_MASK_LIMIT: Final[int] = 1 << 128


def _append_bit(bits: bytearray, index: int, value: bool) -> None:
    """Append one bit at *index* (must be the next free position) — O(1)."""
    byte = index >> 3
    if byte == len(bits):
        bits.append(0)
    if value:
        bits[byte] |= 1 << (index & 7)


def _get_bit(bits: bytearray, index: int) -> bool:
    """Read one previously-appended bit — O(1)."""
    return bool((bits[index >> 3] >> (index & 7)) & 1)


class SurvivorColumns:
    """Columnar retention of exactly the fields the leaderboard rank consumes.

    Append-only, kept order (== the batch path's ``pairs`` order, so every
    downstream population is order-identical to :func:`~brutex.ranking.rank_combos`'s).
    Each column is a compact ``array``/bit-set; the source ``ResultMetrics`` is
    dropped by the caller right after :meth:`append` returns.

    Parameters
    ----------
    with_overfit
        ``True`` retains the three extra Deflated-Sharpe input columns
        (``total_trades`` / ``skewness`` / ``kurtosis``) the opt-in overfit
        gate needs (spec V8). MUST match the tuple's ``overfit`` config —
        :func:`rank_survivor_columns` refuses a mismatch loudly.
    """

    __slots__ = (
        "_dd_pct",
        "_expectancy",
        "_gate_bits",
        "_kurt",
        "_kurt_none",
        "_losing",
        "_mask_hi",
        "_mask_lo",
        "_n",
        "_net_pnl",
        "_sharpe",
        "_sharpe_none",
        "_skew",
        "_skew_none",
        "_terms",
        "_trades",
        "_win_rate",
        "_with_overfit",
    )

    def __init__(self, *, with_overfit: bool) -> None:
        self._with_overfit = with_overfit
        self._n = 0
        self._mask_hi = array("Q")
        self._mask_lo = array("Q")
        self._gate_bits = bytearray()
        self._sharpe = array("d")
        self._sharpe_none = bytearray()
        self._terms = tuple(array("d") for _ in range(N_COMPOSITE_TERMS))
        # Overfit columns exist (empty) even when disabled — uniform attribute
        # types keep the accessors branch-free; only ``append`` gates on the flag.
        self._trades = array("q")
        self._skew = array("d")
        self._skew_none = bytearray()
        self._kurt = array("d")
        self._kurt_none = bytearray()
        # LANE-W1 selection-axis columns (DEC-RANK-QUOTA-PERSIST-1000-001 —
        # the sweep_stream parallel-heap twin's inputs): always retained
        # (+40 B/kept — 3×i64 + 2×f64), all non-null on ResultMetrics.
        self._net_pnl = array("q")
        self._win_rate = array("d")
        self._dd_pct = array("d")
        self._losing = array("q")
        self._expectancy = array("q")

    @property
    def n_kept(self) -> int:
        """How many kept masks have been appended (the tuple's ``backtested`` count)."""
        return self._n

    @property
    def with_overfit(self) -> bool:
        """Whether the Deflated-Sharpe input columns are being retained."""
        return self._with_overfit

    @staticmethod
    def _append_optional(
        values: array[float], none_bits: bytearray, index: int, value: float | None
    ) -> None:
        """Append a ``float | None`` as (value column, ``None`` bit) — never NaN-encoded.

        ``None`` stores a ``0.0`` PLACEHOLDER with its ``None`` bit set (the
        accessor restores ``None`` exactly); a real value — including a NaN/inf
        the downstream consumers treat semantically differently from ``None`` —
        stores its exact float64 bits.
        """
        if value is None:
            _append_bit(none_bits, index, True)
            values.append(0.0)
        else:
            _append_bit(none_bits, index, False)
            values.append(value)

    def append(self, mask: int, metrics: ResultMetrics) -> None:
        """Retain one kept mask's ranking columns; the caller then drops *metrics*.

        O(1) amortised. The gate verdict + the 7 composite term inputs are
        computed HERE, while the full metrics object is alive, via the SAME
        shared :func:`~brutex.ranking.passes_gates` /
        :func:`~brutex.ranking.composite_term_inputs` the batch rank uses — the
        stored float64s are therefore bit-identical to what
        :func:`~brutex.ranking.rank_combos` would derive later.

        Raises
        ------
        ValueError
            If *mask* is outside ``[0, 2**128)`` (the two-u64-limb store).
        """
        if not 0 <= mask < _MASK_LIMIT:
            raise ValueError(f"SurvivorColumns.append: mask {mask!r} outside [0, 2**128)")
        index = self._n
        self._mask_hi.append(mask >> 64)
        self._mask_lo.append(mask & _U64_MASK)
        _append_bit(self._gate_bits, index, passes_gates(metrics))
        self._append_optional(self._sharpe, self._sharpe_none, index, metrics.sharpe_ratio)
        for column, value in zip(self._terms, composite_term_inputs(metrics), strict=True):
            column.append(value)
        if self._with_overfit:
            self._trades.append(metrics.total_trades)
            self._append_optional(self._skew, self._skew_none, index, metrics.skewness)
            self._append_optional(self._kurt, self._kurt_none, index, metrics.kurtosis)
        self._net_pnl.append(metrics.net_pnl_paisa)
        self._win_rate.append(metrics.win_rate)
        self._dd_pct.append(metrics.max_drawdown_pct)
        self._losing.append(metrics.losing_trades)
        self._expectancy.append(metrics.expectancy_paisa)
        self._n = index + 1

    def mask_at(self, index: int) -> int:
        """The u128 mask at *index* (exact two-limb round-trip) — O(1)."""
        return (self._mask_hi[index] << 64) | self._mask_lo[index]

    def gate_passed(self, index: int) -> bool:
        """The recorded :func:`~brutex.ranking.passes_gates` verdict — O(1)."""
        return _get_bit(self._gate_bits, index)

    def gate_survivor_positions(self) -> list[int]:
        """Positions of the gate-passing kept masks, ascending kept order — O(kept)."""
        return [index for index in range(self._n) if _get_bit(self._gate_bits, index)]

    def sharpe_at(self, index: int) -> float | None:
        """The raw ``sharpe_ratio`` (``None`` restored from its bit) — O(1)."""
        if _get_bit(self._sharpe_none, index):
            return None
        return self._sharpe[index]

    def sharpe_values(self) -> list[float | None]:
        """EVERY kept mask's raw Sharpe in kept order — the sidecar/overfit pool.

        O(kept). This is the full backtested population (one entry per kept
        mask, ``None`` preserved) — exactly the list the batch path feeds
        ``tuple_trial_stats_from_sharpes`` + the overfit ``N``/``V`` pool.
        """
        return [self.sharpe_at(index) for index in range(self._n)]

    def term_column(self, term: int, positions: Sequence[int]) -> list[float]:
        """One composite z-term column restricted to *positions* — O(len(positions))."""
        column = self._terms[term]
        return [column[position] for position in positions]

    def trades_at(self, index: int) -> int:
        """``total_trades`` at *index* (overfit column) — O(1)."""
        return self._trades[index]

    def skew_at(self, index: int) -> float | None:
        """``skewness`` at *index* (overfit column; ``None`` restored) — O(1)."""
        if _get_bit(self._skew_none, index):
            return None
        return self._skew[index]

    def kurt_at(self, index: int) -> float | None:
        """``kurtosis`` at *index* (overfit column; ``None`` restored) — O(1)."""
        if _get_bit(self._kurt_none, index):
            return None
        return self._kurt[index]

    def net_pnl_at(self, index: int) -> int:
        """``net_pnl_paisa`` at *index* (selection-axis column) — O(1)."""
        return self._net_pnl[index]

    def win_rate_at(self, index: int) -> float:
        """``win_rate`` at *index* (selection-axis column) — O(1)."""
        return self._win_rate[index]

    def dd_pct_at(self, index: int) -> float:
        """``max_drawdown_pct`` at *index* (selection-axis column) — O(1)."""
        return self._dd_pct[index]

    def losing_at(self, index: int) -> int:
        """``losing_trades`` at *index* (selection-axis column) — O(1)."""
        return self._losing[index]

    def expectancy_at(self, index: int) -> int:
        """``expectancy_paisa`` at *index* (selection-axis column) — O(1)."""
        return self._expectancy[index]

    def owned_nbytes(self) -> int:
        """EXACT logical payload bytes of every owned column — the watermark figure.

        ``Σ len(array) × itemsize + Σ len(bit-set)`` — O(#columns) = O(1) per
        call, independent of ``n_kept``. HONEST scope (Golden Rule #10): this
        is the columns' logical payload, not process RSS — ``array``
        over-allocation slack and CPython object headers are excluded (both are
        small constants here; the ``MemoryError`` global-stop backstop is
        unchanged for anything the model misses).
        """
        arrays: tuple[array[int] | array[float], ...] = (
            self._mask_hi,
            self._mask_lo,
            self._sharpe,
            *self._terms,
            self._trades,
            self._skew,
            self._kurt,
            self._net_pnl,
            self._win_rate,
            self._dd_pct,
            self._losing,
            self._expectancy,
        )
        payload = sum(len(column) * column.itemsize for column in arrays)
        bits = (
            len(self._gate_bits)
            + len(self._sharpe_none)
            + len(self._skew_none)
            + len(self._kurt_none)
        )
        return payload + bits


@dataclass(frozen=True, slots=True)
class BoundedFinalist:
    """One bounded-rank finalist: the mask + its streamed composite + DSR.

    The bounded twin of a :class:`~brutex.ranking.RankedCombo` MINUS the full
    ``metrics`` (which the runner recomputes O(top_k) via the same sim path —
    the established finalist re-simulation precedent, spec V16).

    The LANE-W1 selection-provenance fields (DEC-RANK-PARETO-PERSIST-001 /
    DEC-RANK-QUOTA-PERSIST-1000-001) are ADDITIVE + defaulted ``None`` — the
    legacy composite-only path (no ``union_selection_fn``) never stamps them.
    """

    mask: int
    composite_score: float
    deflated_sharpe: float | None
    selection_basis: str | None = None
    pareto_member: bool | None = None


@dataclass(frozen=True, slots=True)
class BoundedRankResult:
    """The bounded rank's output: ordered finalists + the full leaderboard size.

    Attributes
    ----------
    finalists
        The selected survivors in the EXACT batch persist order: the composite
        top-K prefix (composite descending, mask ascending tie-break —
        ``== rank_combos(pairs)[:top_k]`` projected to
        ``(mask, composite, dsr)``, bit-for-bit) followed — union path only —
        by the union-only extras in the same ``(-composite, mask)`` order.
    n_ranked
        The post-gate post-overfit survivor count BEFORE top-K truncation (the
        tuple's ``ranked`` tally — batch's ``len(rank_combos(pairs))``).
    truncated
        Union path: gate-passing ranked survivors NOT selected by any basis
        (the ``gated_pass_truncated`` counter's numerator). Legacy path: 0
        (additive default — the legacy tally lives with the caller).
    frontier_dropped
        Union path: frontier members dropped by the
        :data:`~brutex.ranking.multi_select.PARETO_FRONTIER_CAP` bound.
        Legacy path: 0.
    """

    finalists: tuple[BoundedFinalist, ...]
    n_ranked: int
    truncated: int = 0
    frontier_dropped: int = 0


def rank_survivor_columns(
    columns: SurvivorColumns,
    *,
    overfit: OverfitGateConfig | None,
    top_k: int,
    union_selection_fn: UnionSelectionFn | None = None,
) -> BoundedRankResult:
    """Rank the columnar survivors and select the top-K — bit-identical to batch.

    Runs the EXACT :func:`~brutex.ranking.rank_combos` pipeline over the
    retained columns via the shared internals (CLAUDE.md §11 COMMON): the gate
    verdicts were recorded at append time by the same ``passes_gates``; the
    overfit gate core (:func:`~brutex.ranking.overfit_gate_positions`) sees the
    same two populations (pool = ALL kept Sharpes, survivors = the gate-passing
    subset — spec V7) with the same ``None`` semantics; the z/composite fold is
    the same :func:`~brutex.ranking.composite_scores` on the same float64s in
    the same kept order; the duplicate-mask guard is the same
    :func:`~brutex.ranking.ensure_unique_survivor_masks`. The ONLY structural
    difference is the selection: a size-``top_k`` :func:`heapq.nsmallest` on
    the strict total-order key ``(-composite, mask)``, which the stdlib
    documents as equivalent to ``sorted(...)[:top_k]`` — under a strict total
    order the two are the SAME list, so bounded == batch bit-for-bit (the
    property centrepiece pins it).

    O(kept) for the gate/pool scans, O(survivors) for the z/composite columns,
    O(survivors · log top_k) for the heap; retained extra space O(survivors).

    Parameters
    ----------
    columns
        The tuple's appended survivor columns.
    overfit
        The tuple's opt-in overfit gate config (must match
        ``columns.with_overfit`` — a mismatch means the DSR input columns were
        never retained and is refused loudly, never silently degraded).
    top_k
        The finalist count to select (``>= 1`` — the memory-budget plan refuses
        ``top_k=None`` before this path can engage).
    union_selection_fn
        LANE-W1 (DEC-RANK-PARETO-PERSIST-001 / DEC-RANK-QUOTA-PERSIST-1000-001):
        the injectable union-selection binding (production =
        :func:`brutex.engine.rust_rank_select.union_selection`). ``None`` (the
        default) keeps the legacy composite-only top-K byte-identical. When
        set, the bounded arm builds the survivors' composite-ordered
        :class:`~brutex.ranking.multi_select.SelectionInputs` and delegates to
        the SAME composer the batch arm calls — bounded == batch row sets +
        tags BY CONSTRUCTION (one law, CLAUDE.md §11 COMMON), at a DISCLOSED
        transient rank-time cost: one O(survivors · log survivors) composite
        argsort + one O(survivors) 7-column materialisation (~56 B/survivor
        transient — retained sweep-time memory is unchanged).

    Raises
    ------
    ValueError
        If ``top_k < 1``, if *overfit* is set but *columns* was built without
        the overfit columns, or if two rank-reaching survivors share a mask
        (the shared duplicate-mask guard, byte-identical message).
    """
    if top_k < 1:
        raise ValueError(f"rank_survivor_columns: top_k must be >= 1, got {top_k}")
    if overfit is not None and not columns.with_overfit:
        raise ValueError(
            "rank_survivor_columns: overfit gate requested but the SurvivorColumns "
            "were built with with_overfit=False (the total_trades/skewness/kurtosis "
            "deflation inputs were not retained)"
        )
    positions = columns.gate_survivor_positions()
    dsr_by_survivor: list[float] | None = None
    if overfit is not None and positions:
        outcome = overfit_gate_positions(
            columns.sharpe_values(),
            [
                (
                    columns.sharpe_at(position),
                    columns.trades_at(position),
                    columns.skew_at(position),
                    columns.kurt_at(position),
                )
                for position in positions
            ],
            overfit,
        )
        if outcome is not None:
            kept_relative, dsr_by_survivor = outcome
            positions = [positions[relative] for relative in kept_relative]
    if not positions:
        return BoundedRankResult(finalists=(), n_ranked=0)

    term_columns = tuple(columns.term_column(term, positions) for term in range(N_COMPOSITE_TERMS))
    dsr_contrib = dsr_z_contributions(dsr_by_survivor, len(positions), overfit)
    composites = composite_scores(term_columns, dsr_contrib)
    masks = [columns.mask_at(position) for position in positions]
    ensure_unique_survivor_masks(masks)
    if union_selection_fn is not None:
        return _union_rank(
            columns,
            positions=positions,
            masks=masks,
            composites=composites,
            dsr_by_survivor=dsr_by_survivor,
            top_k=top_k,
            union_selection_fn=union_selection_fn,
        )
    selected = heapq.nsmallest(
        top_k,
        range(len(positions)),
        key=lambda survivor: (-composites[survivor], masks[survivor]),
    )
    finalists = tuple(
        BoundedFinalist(
            mask=masks[survivor],
            composite_score=composites[survivor],
            deflated_sharpe=None if dsr_by_survivor is None else dsr_by_survivor[survivor],
        )
        for survivor in selected
    )
    return BoundedRankResult(finalists=finalists, n_ranked=len(positions))


def _union_rank(
    columns: SurvivorColumns,
    *,
    positions: list[int],
    masks: list[int],
    composites: list[float],
    dsr_by_survivor: list[float] | None,
    top_k: int,
    union_selection_fn: UnionSelectionFn,
) -> BoundedRankResult:
    """The LANE-W1 union arm of :func:`rank_survivor_columns`.

    Sorts the survivors into the batch composite order ``(-composite, mask)``
    (a full argsort — O(survivors · log survivors) transient, disclosed in the
    caller's docstring), materialises the composite-ordered
    :class:`~brutex.ranking.multi_select.SelectionInputs` from the retained
    axis columns, and delegates to the injected union composer — so the
    bounded arm's persisted row SET, ORDER and TAGS are the batch arm's by
    construction (the same pure composer over the same columns).
    """
    composite_order = sorted(
        range(len(positions)), key=lambda survivor: (-composites[survivor], masks[survivor])
    )
    inputs = SelectionInputs(
        masks=tuple(masks[survivor] for survivor in composite_order),
        composites=tuple(composites[survivor] for survivor in composite_order),
        net_pnl_paisa=tuple(
            columns.net_pnl_at(positions[survivor]) for survivor in composite_order
        ),
        win_rate=tuple(columns.win_rate_at(positions[survivor]) for survivor in composite_order),
        max_dd_pct=tuple(columns.dd_pct_at(positions[survivor]) for survivor in composite_order),
        losing_trades=tuple(columns.losing_at(positions[survivor]) for survivor in composite_order),
        expectancy_paisa=tuple(
            columns.expectancy_at(positions[survivor]) for survivor in composite_order
        ),
    )
    selection = union_selection_fn(inputs, top_k=top_k)
    finalists = tuple(
        BoundedFinalist(
            mask=masks[composite_order[pool_index]],
            composite_score=composites[composite_order[pool_index]],
            deflated_sharpe=(
                None if dsr_by_survivor is None else dsr_by_survivor[composite_order[pool_index]]
            ),
            selection_basis=selection.basis_by_index[pool_index],
            pareto_member=pool_index in selection.frontier,
        )
        for pool_index in selection.order
    )
    return BoundedRankResult(
        finalists=finalists,
        n_ranked=len(positions),
        truncated=selection.truncated,
        frontier_dropped=selection.frontier_dropped,
    )


__all__ = [
    "BoundedFinalist",
    "BoundedRankResult",
    "SurvivorColumns",
    "UnionSelectionFn",
    "rank_survivor_columns",
]
