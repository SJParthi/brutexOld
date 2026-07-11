"""Read-time ranking-composite index over the newest run's aggs sidecars.

DEC-RESULTS-CASCADE-VERDICT-JOIN-001 (Phase A2 — the /results half of the
deferred HIGH #40): the COMMON ranking composite
(:func:`brutex.ranking.composite_scores`) is PERSISTED per finalist in the
results-depth ``<stem>.aggs.parquet`` sidecars
(``brutex.engine.trade_store`` — columns ``run_id`` Utf8 +
``composite_score`` Float64, verified), but never reached ``/results``: the
``?preset=composite`` axis was wire-disclosed INERT
(``sweep_results_view.PRESET_INERT_AXES``). This module builds the read-time
``{run_id → composite_score}`` index from the NEWEST run's sidecars so the
axis can activate HONESTLY for scored rows; the persist-on-ResultRow half
stays deferred (DEC-CASCADE-ROW-PERSIST-002).

Laws:

* NEWEST-run-wins: run selection reuses the ONE
  :func:`brutex.dashboard.oneclick_artifacts.discover_runs` listing
  (``(mtime_ns, name)`` descending — §11 COMMON, never a second selection
  law); only THAT run's ``results-depth/**/*.aggs.parquet`` files are read.
* Column-projection reads: ONLY ``(run_id, composite_score)`` — never the
  whole sidecar schema.
* Per-file tolerance: an unreadable/corrupt sidecar is SKIPPED + counted
  (the :func:`~brutex.dashboard.movers_resilient.movers_read_skip_errors`
  class — one bad file never blanks the index); a non-finite / non-float
  composite or a junk run_id cell is DROPPED + counted, never clamped into
  a fake score (Golden Rule #1); a duplicate run_id across sidecars keeps
  the deterministic FIRST (sorted file order) value.
* Honest states: ``absent`` = no run directory discovered (a fresh lake /
  cascade-only tree — the caller emits NO meta, byte-identical legacy
  payload); ``joined`` = a newest run exists (``scored_rows`` may be 0 —
  aggs absent/corrupt reads honestly empty); ``error`` = the listing itself
  failed (logged, "—" everywhere).

Honest cost (Golden Rule #10): O(aggs files) column reads ONCE per
leaderboard revision generation (the app memoises the index) — disclosed,
never claimed O(1).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.dashboard.movers_resilient import movers_read_skip_errors
from brutex.dashboard.oneclick_artifacts import discover_runs, runs_root

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from pathlib import Path

_LOG = structlog.get_logger("brutex.dashboard.composite_join")

__all__ = [
    "COMPOSITE_STATE_ABSENT",
    "COMPOSITE_STATE_ERROR",
    "COMPOSITE_STATE_JOINED",
    "CompositeIndex",
    "load_composite_index",
]

#: A newest run was found and its sidecars were read (``scored_rows`` ≥ 0).
COMPOSITE_STATE_JOINED: Final[str] = "joined"

#: No run directory exists yet — the caller emits NO meta (byte-identical
#: legacy payload; the honest "nothing to join" state, never an error).
COMPOSITE_STATE_ABSENT: Final[str] = "absent"

#: The listing/read machinery itself failed (logged) — "—" everywhere.
COMPOSITE_STATE_ERROR: Final[str] = "error"

#: The aggs sidecar suffix under ``<run>/results-depth/<instrument>/<tf>/``
#: (``brutex.engine.trade_store`` writes ``<stem>.aggs.parquet``). Suffix
#: literal pinned by test against a real ``sidecar_paths_for`` build — not
#: imported: trade_store pulls the engine/polars import graph at module
#: scope and this reader only needs the glob shape.
_AGGS_GLOB: Final[str] = "*/*/*.aggs.parquet"

#: The run-relative results-depth subtree (mirror of
#: ``trade_store.RESULTS_DEPTH_DIRNAME`` — same pin law as ``_AGGS_GLOB``).
_RESULTS_DEPTH_DIRNAME: Final[str] = "results-depth"


@dataclass(frozen=True, slots=True)
class CompositeIndex:
    """The newest run's ``{run_id → ranking composite}`` read-time index."""

    by_run_id: Mapping[str, float]
    run_id: str | None
    state: str
    scored_rows: int
    skipped_files: int
    dropped_values: int


def _empty(state: str, run_id: str | None = None) -> CompositeIndex:
    """The honest empty index for *state* (absent / error / 0-row joined)."""
    return CompositeIndex(
        by_run_id={},
        run_id=run_id,
        state=state,
        scored_rows=0,
        skipped_files=0,
        dropped_values=0,
    )


def load_composite_index(results_root: Path) -> CompositeIndex:
    """Build the composite index from the NEWEST run's aggs sidecars.

    *results_root* is the sweep results root (``<lake>/sweeps``) — the run
    tree is its ``sweep_runs/`` SIBLING via the ONE
    :func:`~brutex.dashboard.oneclick_artifacts.runs_root` seam. See the
    module docstring for the full tolerance law; this function NEVER raises
    on lake damage (state ``error`` is the last-resort degrade).
    """
    import polars as pl

    runs = discover_runs(results_root, limit=1)
    if not runs:
        return _empty(COMPOSITE_STATE_ABSENT)
    run_id = runs[0].run_id
    depth_dir = runs_root(results_root) / run_id / _RESULTS_DEPTH_DIRNAME
    try:
        paths = sorted(depth_dir.glob(_AGGS_GLOB))
    except OSError as exc:
        _LOG.warning(
            "composite_join.listing_failed",
            run_id=run_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return _empty(COMPOSITE_STATE_ERROR, run_id)

    by_run_id: dict[str, float] = {}
    skipped_files = 0
    dropped_values = 0
    skip_errors = movers_read_skip_errors()
    for path in paths:
        try:
            frame = pl.read_parquet(path, columns=["run_id", "composite_score"])
        except skip_errors as exc:
            # One corrupt/unreadable sidecar never blanks the index — the
            # movers_resilient per-file skip class (counted + logged).
            skipped_files += 1
            _LOG.warning(
                "composite_join.sidecar_skip",
                path=str(path),
                error_type=type(exc).__name__,
            )
            continue
        for rid, score in frame.iter_rows():
            if not isinstance(rid, str) or not rid:
                dropped_values += 1
                continue
            if not isinstance(score, float) or not math.isfinite(score):
                # A null / non-finite composite is honest no-signal for that
                # row — dropped + counted, never a fabricated 0.
                dropped_values += 1
                continue
            # Deterministic first-wins across sorted sidecar files.
            by_run_id.setdefault(rid, score)
    if skipped_files or dropped_values:
        _LOG.warning(
            "composite_join.partial",
            run_id=run_id,
            skipped_files=skipped_files,
            dropped_values=dropped_values,
            scored_rows=len(by_run_id),
        )
    return CompositeIndex(
        by_run_id=by_run_id,
        run_id=run_id,
        state=COMPOSITE_STATE_JOINED,
        scored_rows=len(by_run_id),
        skipped_files=skipped_files,
        dropped_values=dropped_values,
    )
