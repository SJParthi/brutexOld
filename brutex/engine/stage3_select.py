"""Stage-3 survivor selection — the top-K query over the persisted leaderboard.

DEC-STAGE3-TOPK-001 (ADR-0007 / DEC-STAGE3-DESIGN-LOCK-001 §5 slice 2),
selection math re-hosted by DEC-RUST-STAGE3-REFINE-001. The Stage-3
refinement consumes "the configured top-K masks **per (instrument, TF, mode)
leaderboard, in leaderboard order**" with a spec default of K = 10,000
(`TRACK1_BACKTEST_SPEC.md` §Stage 3, lines 407-408 — locked by ADR-0007
condition 1; the deterministic outer loop replaced the dropped Optuna layer).

Design (CLAUDE.md §11 COMMON — reuse, never re-implement):

* :func:`group_survivors` is the thin streaming wrapper over the wheel's
  ``SelectSession`` (``rust/brutex-refine/src/select.rs`` — the bounded
  per-key top-K heap of DEC-SWEEP-STREAMING-RANK-002, O(top_k) rows retained
  per key): each row streams through ``push`` and the Python side keeps ONLY
  the currently-kept :class:`ResultRow` objects (``push`` reports the kept /
  evicted verdicts row-by-row), so the bounded-memory law holds end to end —
  pinned by the tracemalloc bench. The emitted selection is byte-identical
  to the retired pure-Python implementation (Sharpe descending, ties broken
  by ``run_id`` ascending, deeper ties by insertion order — the proven store
  discipline mirroring :meth:`ResultsStore.top_k`), pinned by the
  golden-equality suite against the verbatim reference copy.
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
time (one FFI push per row) and O(leaderboards · top_k) space Python-side.
It is NOT one of the Rule-#5 O(1) hot paths and no O(1) claim is made for
it; when the disk composition is used, :func:`load_results_tree` still
materialises the whole tree in its store (an upstream site deliberately not
touched). A wheel-absent/flag-off environment raises the typed
:class:`~brutex.engine.refine_rust.RefineRustUnavailableError` at call time —
no fallback exists (the Python selection math is deleted).
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
from brutex.engine.refine_rust import require_refine_rust
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
class _AxisEntry:
    """One bounded-heap slot of the LANE-W1 multi-axis union arm.

    The retired pure-Python ``_HeapEntry`` bounded-heap discipline (whose
    legacy Sharpe-only arm now lives in the wheel's ``SelectSession`` —
    DEC-RUST-STAGE3-REFINE-001) generalized over a PRE-COMPUTED
    ascending better-first axis key (``better``) instead of the hard-coded
    Sharpe field: ``<`` means "ranks strictly WORSE than" — larger axis key,
    then larger ``run_id`` (asc wins), then later insertion. ``seq`` is
    unique per input row, so the order is a strict total order and eviction
    is deterministic (the same strict-total-order completeness argument as
    the retired ``_HeapEntry`` / the wheel's ``SelectSession`` law).
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
    """Group *rows* per leaderboard and keep each board's top-*top_k*.

    Each leaderboard is ordered ``sharpe_ratio`` descending with ties broken by
    ``run_id`` ascending — the SAME discipline as
    :meth:`~brutex.engine.results_store.ResultsStore.top_k`, so the selection
    is identical for any input order of distinct ``(sharpe, run_id)`` rows
    (Golden Rule #4). The returned dict's iteration order is the sorted
    :class:`LeaderboardKey` order (deterministic outer-loop order for the
    refinement runner) — the wheel emits boards from a sorted map, whose
    UTF-8 byte order equals CPython's code-point ``str`` order.

    RAM is BOUNDED (DEC-SWEEP-STREAMING-RANK-002): the wheel holds at most
    *top_k* ``(sharpe, run_id, seq)`` entries per key and this wrapper keeps
    ONLY the currently-kept :class:`ResultRow` objects (each ``push`` verdict
    adds/evicts one) while *rows* streams through — O(top_k) space per
    leaderboard instead of the pre-#1294 O(candidates)
    accumulate-then-two-pass-sort — and the emitted selection is
    byte-identical to that old path (pinned by the golden-equality suite
    against a verbatim reference copy of the old implementation).

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
    brutex.engine.refine_rust.RefineRustUnavailableError
        When the Rust runtime cannot be engaged (loud, no fallback).
    """
    if top_k < 1:
        raise ResultsStoreError(f"stage3 top_k must be >= 1, got {top_k}")
    if multi_axis:
        return _group_survivors_union(rows, top_k=top_k)
    runtime = require_refine_rust()
    session = runtime.SelectSession(top_k)
    kept: dict[int, ResultRow] = {}
    for seq, row in enumerate(rows):
        was_kept, evicted_seq = session.push(
            row.instrument, row.tf, row.mode, row.sharpe_ratio, row.run_id
        )
        if was_kept:
            kept[seq] = row
        if evicted_seq is not None:
            # The eviction verdict is what keeps Python memory bounded: the
            # displaced row is released the moment the wheel displaces it.
            del kept[evicted_seq]
    selected: dict[LeaderboardKey, tuple[ResultRow, ...]] = {}
    for (instrument, tf, mode), seqs in session.finish():
        selected[LeaderboardKey(instrument=instrument, tf=tf, mode=mode)] = tuple(
            kept[seq] for seq in seqs
        )
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

    The disk-facing composition the Stage-3 refinement runner (slice 4)
    calls: :func:`~brutex.engine.results_report.load_results_tree` merges
    every per-tuple finalist Parquet under *root* deterministically (sorted
    file order, run-identity-collision-safe), then :func:`group_survivors`
    selects.

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
