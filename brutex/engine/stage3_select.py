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

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.engine.refine_rust import require_refine_rust
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


def select_stage3_survivors(
    root: Path,
    *,
    top_k: int = DEFAULT_STAGE3_TOP_K,
    current_snapshot_hash: str | None = None,
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
    return group_survivors(store.rows(), top_k=top_k)


__all__ = [
    "DEFAULT_STAGE3_TOP_K",
    "LeaderboardKey",
    "group_survivors",
    "select_stage3_survivors",
]
