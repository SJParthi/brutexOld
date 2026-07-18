"""Stage-3 survivor selection — the top-K query over the persisted leaderboard.

DEC-STAGE3-TOPK-001 (ADR-0007 / DEC-STAGE3-DESIGN-LOCK-001 §5 slice 2). The
Stage-3 BIPOP-CMA-ES refinement consumes "the configured top-K masks **per
(instrument, TF, mode) leaderboard, in leaderboard order**" with a spec default
of K = 10,000 (`TRACK1_BACKTEST_SPEC.md` §Stage 3, lines 407-408 — locked by
ADR-0007 condition 1; the deterministic outer loop replaced the dropped Optuna
layer). Until this slice that query did not exist: Stage 2 persists per-tuple
finalist Parquets (the ``<root>/<instrument>/<tf>/…`` tree) and the only top-K
selectors served either an in-memory store (:meth:`ResultsStore.top_k`) or the
Stage-4 cascade (``cascade_wire.spec_from_store_rows``).

Design (CLAUDE.md §11 COMMON — reuse, never re-implement):

* :func:`group_survivors` is the **pure** core: group rows by
  :class:`LeaderboardKey`, keep each leaderboard's top-*top_k* in a BOUNDED
  per-key min-heap (DEC-SWEEP-STREAMING-RANK-002 — O(top_k) rows retained per
  key instead of the pre-#1294 accumulate-then-sort O(rows)), then emit the
  kept rows in EXACTLY the proven store discipline order (``sharpe_ratio``
  descending, ties broken by ``run_id`` ascending, deeper ties by insertion
  order — byte-identical to the original stable two-pass sort, mirroring
  :meth:`ResultsStore.top_k`).
* :func:`select_stage3_survivors` is the thin disk composition:
  :func:`~brutex.engine.results_report.load_results_tree` (the existing
  deterministic per-tuple merge) → :func:`group_survivors`.

Per ADR-0007 condition 2, the leaderboard ORDER here is the pool-independent
``sharpe_ratio`` (the per-eval objective), NOT the pool-z-scored 8-term
composite — the composite re-ranks the merged pool after refinement, as
always. Direction is NOT part of the leaderboard key: both directions compete
inside one (instrument, TF, mode) leaderboard, exactly as the spec words it.

**Honest envelope (Golden Rule #10):** this is a ONE-SHOT between-stage
selection over the already-trimmed Stage-2 finalists — O(rows · log top_k)
time and O(leaderboards · top_k) space (the part-2 RAM fix of
DEC-SWEEP-STREAMING-RANK-001, MIGRATION.md #1184/#1187: candidate rows are
no longer ALL accumulated per key before sorting). It is NOT one of the
Rule-#5 O(1) hot paths and no O(1) claim is made for it; when the disk
composition is used, :func:`load_results_tree` still materialises the whole
tree in its store (an upstream site this PR deliberately does NOT touch).
"""

from __future__ import annotations

import functools
import heapq
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.engine.pool_union import (
    POOL_UNION_SOURCE_ORDER,
    axis_better_key,
    round_robin_union,
)
from brutex.engine.results_report import load_results_tree
from brutex.engine.results_store import ResultRow, ResultsStoreError
from brutex.ranking.presets import DEFAULT_PRESET, resolve_preset

#: The spec-default Stage-3 selection depth (`TRACK1_BACKTEST_SPEC.md:407` —
#: "default K = 10,000"; MIGRATION.md row #37 as amended by ADR-0007). Named
#: here as the ONE hard-coded source: the audit behind ADR-0007 found the
#: default documented everywhere but coded nowhere.
DEFAULT_STAGE3_TOP_K: Final[int] = 10_000


@dataclass(frozen=True, slots=True)
class LeaderboardKey:
    """One Stage-3 leaderboard coordinate — ``(instrument, TF, mode)``.

    Direction is deliberately absent (spec §Stage 3): bull and bear masks
    compete inside the same leaderboard and the refinement loop receives
    whichever survive on Sharpe.
    """

    instrument: str
    tf: str
    mode: str


@functools.total_ordering
@dataclass(frozen=True, slots=True)
class _HeapEntry:
    """One bounded-heap slot — ``<`` means "ranks strictly WORSE than".

    ``heapq`` is a MIN-heap, so with this ordering the heap root is always
    the WORST kept entry — the one a strictly-better candidate evicts. The
    selection total order is ``sharpe_ratio`` descending, then ``run_id``
    ascending, then insertion order (``seq``) ascending — exactly the order
    the original stable two-pass sort produced (run_id asc first, then a
    stable Sharpe-desc pass preserves run_id order inside Sharpe ties and
    insertion order inside full ``(sharpe, run_id)`` ties). "Worse" is the
    reverse: SMALLER Sharpe, then LARGER ``run_id`` (``run_id`` is a hex
    string — it cannot be negated into a tuple key, hence this wrapper with
    an inverted comparison), then LARGER ``seq``. ``seq`` is unique per input
    row, so the order is a STRICT total order and eviction is deterministic:
    among equal-lowest-Sharpe entries the LARGEST ``run_id`` sits at the root
    and is evicted first (``run_id`` asc is preferred), byte-identical to the
    pre-#1294 accumulate-then-sort trim (Golden Rule #4).

    ``sharpe`` is carried at FULL precision, never rounded (the
    DEC-MONEY-2DP-001 boundary: money only — statistical metrics keep every
    bit or strategy ranking breaks). ``NaN`` cannot occur: ``ResultRow``
    rejects non-finite metrics at construction, so ``!=``/``<`` here are
    total on the actual domain.

    The ordering protocol is deliberately COMPLETE (CodeQL
    ``py/incomplete-ordering``): :func:`functools.total_ordering` derives
    ``__le__`` / ``__gt__`` / ``__ge__`` from :meth:`__lt__` + the
    dataclass ``__eq__`` — the same discipline as ``_Outcome`` in
    :mod:`brutex.engine.sweep_parallel` (CLAUDE.md §11 COMMON, one derivation
    site — never hand-rolled twins). Its derivations (``a >= b ≡ not a < b``;
    ``a > b ≡ not a < b and a != b``; ``a <= b ≡ a < b or a == b``) coincide
    with the total-order identities (``a <= b ≡ not b < a``;
    ``a > b ≡ b < a``) ONLY because the order is strict and TOTAL: ``seq``
    is unique per input row, so incomparability (neither ``<`` holds) occurs
    exactly on field-equal twins, where the dataclass ``__eq__`` is ``True``
    — the two formulations agree on every pair in the domain. Heap semantics
    are UNCHANGED: ``heapq`` only ever consults ``__lt__``, whose body is
    byte-identical to the pre-completion definition.
    """

    sharpe: float
    run_id: str
    seq: int
    row: ResultRow

    def __lt__(self, other: _HeapEntry) -> bool:
        """Return ``True`` iff ``self`` ranks strictly worse than *other*."""
        if self.sharpe != other.sharpe:
            return self.sharpe < other.sharpe  # smaller Sharpe is worse
        if self.run_id != other.run_id:
            return self.run_id > other.run_id  # larger run_id is worse (asc wins)
        return self.seq > other.seq  # later insertion is worse (stable-sort tie)


@functools.total_ordering
@dataclass(frozen=True, slots=True)
class _AxisEntry:
    """One bounded-heap slot of the LANE-W1 multi-axis union arm.

    The :class:`_HeapEntry` discipline generalized over a PRE-COMPUTED
    ascending better-first axis key (``better``) instead of the hard-coded
    Sharpe field: ``<`` means "ranks strictly WORSE than" — larger axis key,
    then larger ``run_id`` (asc wins), then later insertion. ``seq`` is
    unique per input row, so the order is a strict total order and eviction
    is deterministic (the same completeness argument as :class:`_HeapEntry`).
    """

    better: tuple[float | int, ...]
    run_id: str
    seq: int
    row: ResultRow

    def __lt__(self, other: _AxisEntry) -> bool:
        """Return ``True`` iff ``self`` ranks strictly worse than *other*."""
        if self.better != other.better:
            return self.better > other.better  # larger better-first key is worse
        if self.run_id != other.run_id:
            return self.run_id > other.run_id  # larger run_id is worse (asc wins)
        return self.seq > other.seq  # later insertion is worse (stable-sort tie)


def group_survivors(
    rows: Iterable[ResultRow],
    *,
    top_k: int = DEFAULT_STAGE3_TOP_K,
    multi_axis: bool = True,
) -> dict[LeaderboardKey, tuple[ResultRow, ...]]:
    """Group *rows* per leaderboard and keep each board's top-*top_k* — pure.

    Each leaderboard is ordered ``sharpe_ratio`` descending with ties broken by
    ``run_id`` ascending — the SAME discipline as
    :meth:`~brutex.engine.results_store.ResultsStore.top_k`, so the selection
    is identical for any input order of distinct ``(sharpe, run_id)`` rows
    (Golden Rule #4). The returned dict's iteration order is the sorted
    :class:`LeaderboardKey` order (deterministic outer-loop order for the
    refinement runner).

    RAM is BOUNDED (DEC-SWEEP-STREAMING-RANK-002): each key holds at most
    *top_k* rows in a min-heap while *rows* streams through — O(top_k) space
    per leaderboard instead of the pre-#1294 O(candidates) accumulate-then-
    two-pass-sort — and the emitted selection is byte-identical to that old
    path (pinned by the golden-equality suite against a verbatim reference
    copy of the old implementation).

    Parameters
    ----------
    rows
        Stage-2 finalist rows (typically the merged persisted tree). May be
        a lazy iterable — it is consumed exactly once, in one pass.
    top_k
        Per-leaderboard selection depth (``>= 1``); defaults to
        :data:`DEFAULT_STAGE3_TOP_K`. A leaderboard with fewer rows yields
        them all (never an error).
    multi_axis
        LANE W1 (DEC-RANK-POOL-UNION-ADMIT-001): ``True`` (the default)
        admits each board's rows by the interleaved round-robin union of the
        FIVE :data:`~brutex.engine.pool_union.POOL_UNION_SOURCE_ORDER`
        sources (Sharpe · the DEFAULT preset law · DD asc · win-rate desc ·
        the persisted ε-Pareto frontier — frontier inclusion GUARANTEED only
        under the ``top_k >= non-empty sources`` precondition, which the
        production ``DEFAULT_STAGE3_TOP_K=10_000`` always satisfies; below
        it the tail sources are starved + WARNED once — the
        :mod:`~brutex.engine.pool_union` module docstring, review round 1),
        capped at the SAME *top_k* budget with the emitted board still in
        TODAY's Sharpe-law order (membership changes; ordering discipline
        does not; a board with ≤ *top_k* rows is byte-identical either way).
        ``False`` is the legacy single-key (Sharpe) admission — the
        byte-identity arm the golden-equality suite pins. RAM stays bounded:
        the union arm holds at most ``len(POOL_UNION_SOURCE_ORDER) × top_k``
        entries per board (5× the legacy bound — disclosed).

    Returns
    -------
    dict[LeaderboardKey, tuple[ResultRow, ...]]
        Per-leaderboard survivors in leaderboard order; empty for no rows.

    Raises
    ------
    ResultsStoreError
        If ``top_k < 1`` (mirrors the store's ``top_k`` guard).
    """
    if top_k < 1:
        raise ResultsStoreError(f"stage3 top_k must be >= 1, got {top_k}")
    if multi_axis:
        return _group_survivors_union(rows, top_k=top_k)
    heaps: dict[LeaderboardKey, list[_HeapEntry]] = {}
    for seq, row in enumerate(rows):
        key = LeaderboardKey(instrument=row.instrument, tf=row.tf, mode=row.mode)
        board = heaps.setdefault(key, [])
        entry = _HeapEntry(sharpe=row.sharpe_ratio, run_id=row.run_id, seq=seq, row=row)
        if len(board) < top_k:
            heapq.heappush(board, entry)
        elif board[0] < entry:
            # The kept-worst root ranks strictly worse than the candidate —
            # evict it (among equal-lowest-Sharpe roots, the LARGEST run_id
            # is at the root and goes first; see _HeapEntry).
            heapq.heapreplace(board, entry)
    selected: dict[LeaderboardKey, tuple[ResultRow, ...]] = {}
    for key in sorted(heaps, key=lambda k: (k.instrument, k.tf, k.mode)):
        # Emit in the ORIGINAL two-pass discipline, restricted to the kept
        # set: run_id asc first (seq restores the insertion order the old
        # stable sort preserved inside duplicate run_ids), then a stable
        # Sharpe-desc pass (mirrors ResultsStore.top_k, CLAUDE.md §11 COMMON).
        entries = sorted(heaps[key], key=lambda e: (e.run_id, e.seq))
        entries.sort(key=lambda e: e.sharpe, reverse=True)
        selected[key] = tuple(entry.row for entry in entries)
    return selected


def _group_survivors_union(
    rows: Iterable[ResultRow], *, top_k: int
) -> dict[LeaderboardKey, tuple[ResultRow, ...]]:
    """The LANE-W1 multi-axis union arm of :func:`group_survivors`.

    Streams *rows* ONCE into per-board, per-source bounded heaps (each the
    :class:`_AxisEntry` discipline at capacity *top_k* — O(sources · top_k)
    space per board), then per board round-robins the source orderings to
    the SAME *top_k* cap (:func:`~brutex.engine.pool_union.round_robin_union`
    — the ONE shared law the cascade seam also consumes) and emits the union
    membership in TODAY's Sharpe-law discipline.
    """
    preset = resolve_preset(DEFAULT_PRESET)
    n_sources = len(POOL_UNION_SOURCE_ORDER)
    heaps: dict[LeaderboardKey, list[list[_AxisEntry]]] = {}
    for seq, row in enumerate(rows):
        key = LeaderboardKey(instrument=row.instrument, tf=row.tf, mode=row.mode)
        boards = heaps.setdefault(key, [[] for _ in range(n_sources)])
        for index, source in enumerate(POOL_UNION_SOURCE_ORDER):
            axis = axis_better_key(source, row, preset=preset)
            if axis is None:
                continue  # the row does not qualify for this source (frontier)
            entry = _AxisEntry(better=axis, run_id=row.run_id, seq=seq, row=row)
            board = boards[index]
            if len(board) < top_k:
                heapq.heappush(board, entry)
            elif board[0] < entry:
                heapq.heapreplace(board, entry)
    selected: dict[LeaderboardKey, tuple[ResultRow, ...]] = {}
    for key in sorted(heaps, key=lambda k: (k.instrument, k.tf, k.mode)):
        # The seq→row map is rebuilt from the KEPT heap entries only — the
        # retained space stays O(sources · top_k) per board, never O(rows).
        row_by_seq: dict[int, ResultRow] = {}
        ordered_sources: list[list[int]] = []
        for board in heaps[key]:
            entries = sorted(board, key=lambda e: (e.better, e.run_id, e.seq))
            for entry in entries:
                row_by_seq[entry.seq] = entry.row
            ordered_sources.append([entry.seq for entry in entries])
        admitted = round_robin_union(
            ordered_sources, cap=top_k, source_names=POOL_UNION_SOURCE_ORDER
        )
        emitted = sorted(admitted, key=lambda seq: (row_by_seq[seq].run_id, seq))
        emitted.sort(key=lambda seq: row_by_seq[seq].sharpe_ratio, reverse=True)
        selected[key] = tuple(row_by_seq[seq] for seq in emitted)
    return selected


def select_stage3_survivors(
    root: Path,
    *,
    top_k: int = DEFAULT_STAGE3_TOP_K,
    current_snapshot_hash: str | None = None,
    multi_axis: bool = True,
) -> dict[LeaderboardKey, tuple[ResultRow, ...]]:
    """Load the persisted Stage-2 results tree and select each leaderboard's top-K.

    The disk-facing composition the Stage-3 refinement runner (slice 4) will
    call: :func:`~brutex.engine.results_report.load_results_tree` merges every
    per-tuple finalist Parquet under *root* deterministically (sorted file
    order, run-identity-collision-safe), then :func:`group_survivors` selects.

    Parameters
    ----------
    root
        The grid results root (the ``<root>/<instrument>/<tf>/…`` tree the
        sweep orchestrator persists).
    top_k
        Per-leaderboard selection depth (defaults to
        :data:`DEFAULT_STAGE3_TOP_K`).
    current_snapshot_hash
        Optional CURRENT lake snapshot context — threaded to the loader's
        snapshot-mismatch quarantine (DEC-LAKE-READ-UNIQUE-001) so a
        poisoned-era tree cannot seed the Stage-3 refinement. ``None`` (the
        default) is byte-identical to legacy behaviour.
    multi_axis
        Threaded to :func:`group_survivors` (LANE W1,
        DEC-RANK-POOL-UNION-ADMIT-001) — ``True`` (the default) is the
        multi-key union admission; ``False`` the legacy Sharpe-only law.

    Returns
    -------
    dict[LeaderboardKey, tuple[ResultRow, ...]]
        Per-leaderboard survivors in deterministic leaderboard order; an
        existing-but-empty tree yields an empty dict.

    Raises
    ------
    ResultsStoreError
        If *root* does not exist, ``polars`` is missing, a cross-file
        run-identity conflict is detected (all from the loader), or
        ``top_k < 1``.
    """
    # Validate top_k BEFORE the (potentially large) tree load — fail fast.
    if top_k < 1:
        raise ResultsStoreError(f"stage3 top_k must be >= 1, got {top_k}")
    store = load_results_tree(root, current_snapshot_hash=current_snapshot_hash)
    return group_survivors(store.rows(), top_k=top_k, multi_axis=multi_axis)


__all__ = [
    "DEFAULT_STAGE3_TOP_K",
    "LeaderboardKey",
    "group_survivors",
    "select_stage3_survivors",
]
