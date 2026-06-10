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
  :class:`LeaderboardKey`, order each leaderboard with EXACTLY the proven
  store discipline (the stable two-pass sort — ``run_id`` ascending first,
  then ``sharpe_ratio`` descending — so ties are deterministic for any
  insertion order, mirroring :meth:`ResultsStore.top_k`), trim to *top_k*.
* :func:`select_stage3_survivors` is the thin disk composition:
  :func:`~brutex.engine.results_report.load_results_tree` (the existing
  deterministic per-tuple merge) → :func:`group_survivors`.

Per ADR-0007 condition 2, the leaderboard ORDER here is the pool-independent
``sharpe_ratio`` (the per-eval objective), NOT the pool-z-scored 8-term
composite — the composite re-ranks the merged pool after refinement, as
always. Direction is NOT part of the leaderboard key: both directions compete
inside one (instrument, TF, mode) leaderboard, exactly as the spec words it.

**Honest envelope (Golden Rule #10):** this is a ONE-SHOT between-stage
selection over the already-trimmed Stage-2 finalists — O(rows log rows) time,
O(rows) space, run once per sweep. It is NOT one of the Rule-#5 O(1) hot paths
and no O(1) claim is made for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.engine.results_report import load_results_tree
from brutex.engine.results_store import ResultRow, ResultsStoreError

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


def group_survivors(
    rows: Iterable[ResultRow],
    *,
    top_k: int = DEFAULT_STAGE3_TOP_K,
) -> dict[LeaderboardKey, tuple[ResultRow, ...]]:
    """Group *rows* per leaderboard and trim each to its top-*top_k* — pure.

    Each leaderboard is ordered ``sharpe_ratio`` descending with ties broken by
    ``run_id`` ascending — the SAME stable two-pass discipline as
    :meth:`~brutex.engine.results_store.ResultsStore.top_k`, so the selection
    is identical for any input order (Golden Rule #4). The returned dict's
    iteration order is the sorted :class:`LeaderboardKey` order (deterministic
    outer-loop order for the refinement runner).

    Parameters
    ----------
    rows
        Stage-2 finalist rows (typically the merged persisted tree).
    top_k
        Per-leaderboard selection depth (``>= 1``); defaults to
        :data:`DEFAULT_STAGE3_TOP_K`. A leaderboard with fewer rows yields
        them all (never an error).

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
    grouped: dict[LeaderboardKey, list[ResultRow]] = {}
    for row in rows:
        key = LeaderboardKey(instrument=row.instrument, tf=row.tf, mode=row.mode)
        grouped.setdefault(key, []).append(row)
    selected: dict[LeaderboardKey, tuple[ResultRow, ...]] = {}
    for key in sorted(grouped, key=lambda k: (k.instrument, k.tf, k.mode)):
        # Stable two-pass sort: run_id asc first (the tie-break), then Sharpe
        # desc — Python's stable sort preserves run_id order inside ties
        # (mirrors ResultsStore.top_k, CLAUDE.md §11 COMMON).
        board = sorted(grouped[key], key=lambda r: r.run_id)
        board.sort(key=lambda r: r.sharpe_ratio, reverse=True)
        selected[key] = tuple(board[:top_k])
    return selected


def select_stage3_survivors(
    root: Path,
    *,
    top_k: int = DEFAULT_STAGE3_TOP_K,
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
    store = load_results_tree(root)
    return group_survivors(store.rows(), top_k=top_k)


__all__ = [
    "DEFAULT_STAGE3_TOP_K",
    "LeaderboardKey",
    "group_survivors",
    "select_stage3_survivors",
]
