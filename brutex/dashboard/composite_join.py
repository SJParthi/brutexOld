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

* NEWEST-run-wins: run selection reuses the ONE oneclick selection core via
  :func:`brutex.dashboard.oneclick_artifacts.newest_run_name` — the same
  ``(mtime_ns, name)``-descending law ``discover_runs`` rides (§11 COMMON,
  never a second selection law), NAME-only so neither the loader nor the
  per-poll freshness probe ever reads/parses a ``manifest.json`` (fix
  round 2, bench-pinned); only THAT run's
  ``results-depth/**/*.aggs.parquet`` files are read.
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
:func:`composite_index_revision` token (the app memoises the index on the
DEDICATED freshness probe below) — disclosed, never claimed O(1).
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import structlog

from brutex.dashboard.lake_revision import ABSENT_TOKEN
from brutex.dashboard.movers_resilient import movers_read_skip_errors
from brutex.dashboard.oneclick_artifacts import load_top100, newest_run_name, runs_root
from brutex.engine.top100_artifact import TOP100_STAMP_FILENAME

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from pathlib import Path

_LOG = structlog.get_logger("brutex.dashboard.composite_join")

__all__ = [
    "COMPOSITE_STATE_ABSENT",
    "COMPOSITE_STATE_ERROR",
    "COMPOSITE_STATE_JOINED",
    "CompositeIndex",
    "composite_index_revision",
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
    """The newest run's ``{run_id → ranking composite}`` read-time index.

    ``tier_by_run_id`` is the ADDITIVE tier join (DEC-RANKING-SPLUS-TIER-001):
    the SAME newest run's ``top100.json`` tier objects (bridge-validated +
    ``status_text``-stamped by :func:`~brutex.dashboard.oneclick_artifacts.load_top100`
    — the ONE reader, never a second parser), keyed by entry ``run_id``.
    Empty on a pre-tier artifact / an unreadable artifact (honest absence —
    the ``/results`` rows then emit NO tier keys).
    """

    by_run_id: Mapping[str, float]
    run_id: str | None
    state: str
    scored_rows: int
    skipped_files: int
    dropped_values: int
    tier_by_run_id: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


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


def _tier_index(results_root: Path, run_id: str) -> dict[str, Mapping[str, object]]:
    """The newest run's ``{run_id → tier object}`` join (DEC-RANKING-SPLUS-TIER-001).

    Rides the ONE display reader (:func:`~brutex.dashboard.oneclick_artifacts.load_top100`
    — stamped ``status_text``, invalid tier objects already DROPPED per row by
    its bridge), so this join can never disagree with the /screener wire. An
    awaiting/corrupt artifact reads an honestly EMPTY index — never a raise
    (``load_top100`` returns a status, it does not throw on lake damage).
    O(artifact) — ≤ 100 entries, one bounded read per memoised index build.
    """
    result = load_top100(runs_root(results_root) / run_id)
    tiers: dict[str, Mapping[str, object]] = {}
    for row in result.rows:
        rid = row.get("run_id")
        tier = row.get("tier")
        if isinstance(rid, str) and rid and isinstance(tier, dict):
            tiers.setdefault(rid, tier)
    return tiers


def load_composite_index(results_root: Path) -> CompositeIndex:
    """Build the composite index from the NEWEST run's aggs sidecars.

    *results_root* is the sweep results root (``<lake>/sweeps``) — the run
    tree is its ``sweep_runs/`` SIBLING via the ONE
    :func:`~brutex.dashboard.oneclick_artifacts.runs_root` seam. See the
    module docstring for the full tolerance law; this function NEVER raises
    on lake damage (state ``error`` is the last-resort degrade).
    """
    import polars as pl

    run_id = newest_run_name(results_root)
    if run_id is None:
        return _empty(COMPOSITE_STATE_ABSENT)
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
        tier_by_run_id=_tier_index(results_root, run_id),
    )


def composite_index_revision(results_root: Path) -> str | None:
    """Return the composite index's freshness token for *results_root*, or ``None``.

    DEC-RESULTS-CASCADE-VERDICT-JOIN-001 fix round 1 (refuter-confirmed MAJOR):
    the aggs sidecars this module reads live under the ``sweep_runs/`` SIBLING
    tree, which ``lake_revision.sweep_results_revision`` (sweeps dir-mtime
    digest + audit stat) NEVER stats — and ``cmd_sweep_all``'s results-depth
    pass writes them AFTER the sweep's last store flush / audit append, so a
    composite index cached on that generation alone could freeze an
    EMPTY/PARTIAL index (a poll landing mid-depth-pass) for the WHOLE
    inter-sweep interval. This dedicated probe covers exactly what
    :func:`load_composite_index` reads:

    * the NEWEST run's identity via the SAME oneclick selection core
      (:func:`~brutex.dashboard.oneclick_artifacts.newest_run_name` — the
      ``(mtime_ns, name)``-descending law ``discover_runs`` rides; §11
      COMMON — never a second selection law), NAME-only since fix round 2
      (refuter-confirmed MINOR): the pre-fix ``discover_runs(limit=1)`` hop
      also read + json-parsed the newest run's ``manifest.json`` per poll —
      pure waste the probe never consumed, and undisclosed; and
    * a bounded directory-mtime digest of that run's ``results-depth/``
      subtree down to the ``<instrument>/<tf>/`` dirs — every aggs sidecar
      lands via the atomic ``trade_store`` tempfile + ``os.replace`` INTO its
      ``<tf>/`` dir, which moves that dir's mtime, so any aggs write moves
      the token (the ``lake_revision.sweeps_revision`` digest shape).

    Returns
    -------
    str | None
        * a digest token when a newest run exists (a still-absent depth tree
          contributes the stable ``"<run_id>|absent"`` component — its
          first-ever creation moves the token exactly once);
        * :data:`~brutex.dashboard.lake_revision.ABSENT_TOKEN` when no run
          directory exists at all (``newest_run_name`` folds an unreadable
          runs root into the same ``None`` the LOADER sees, so probe and
          loader can never disagree about absence);
        * ``None`` — "unknown, no signal" — when the depth-tree stat/scan
          fails with a non-missing-file :class:`OSError` (logged); the caller
          folds it as a probe OUTAGE (the cached payload holds — bounded
          staleness, never a fake flip).

    Honest cost (Golden Rule #10): O(run dirs) ``scandir``/``stat`` for the
    name-only newest-run selection — ZERO file reads, ZERO JSON parses (fix
    round 2; the pre-fix hop paid one manifest.json read + parse per poll) —
    plus O(depth dirs) ``scandir``/``stat`` — bounded by instruments ×
    timeframes, NEVER by the sidecar file count — paid per ``/results`` poll
    (the ``sweeps_revision`` envelope, disclosed; the zero-manifest-I/O law
    is bench-pinned in ``test_cascade_join_o1.py`` beside the cascade
    one-stat pin).
    """
    run_id = newest_run_name(results_root)
    if run_id is None:
        return ABSENT_TOKEN
    # DEC-RANKING-SPLUS-TIER-001: the index now ALSO reads the newest run's
    # top100.json (the tier join), which write_top100 lands into the run dir
    # AFTER the depth-tree aggs writes — the depth-dir digest alone would
    # freeze a tier-less index cached by a poll landing in that window (the
    # fix-round-1 composite class). ONE stat of the top100 STAMP (written
    # LAST — the token moves only once the artifact pair is complete) folds
    # into every returned token; a stat outage is a probe outage (None).
    stamp_path = runs_root(results_root) / run_id / TOP100_STAMP_FILENAME
    try:
        st_stamp = os.stat(stamp_path)
    except (FileNotFoundError, NotADirectoryError):
        stamp_part = f"top100:{ABSENT_TOKEN}"
    except OSError as exc:
        _LOG.warning(
            "composite_join.revision_stat_failed",
            path=str(stamp_path),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    else:
        stamp_part = f"top100:{st_stamp.st_mtime_ns}:{st_stamp.st_size}"
    depth_dir = runs_root(results_root) / run_id / _RESULTS_DEPTH_DIRNAME
    try:
        st_depth = os.stat(depth_dir)
    except (FileNotFoundError, NotADirectoryError):
        # The depth pass has not created the tree yet — a stable PER-RUN
        # absent component: its first creation moves the token exactly once.
        return f"{run_id}|{ABSENT_TOKEN}|{stamp_part}"
    except OSError as exc:
        _LOG.warning(
            "composite_join.revision_stat_failed",
            path=str(depth_dir),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    parts: list[str] = [run_id, f".:{st_depth.st_mtime_ns}", stamp_part]
    try:
        with os.scandir(depth_dir) as level1:
            for d1 in sorted(
                (entry for entry in level1 if entry.is_dir(follow_symlinks=False)),
                key=lambda entry: entry.name,
            ):
                parts.append(f"{d1.name}:{d1.stat(follow_symlinks=False).st_mtime_ns}")
                with os.scandir(d1.path) as level2:
                    for d2 in sorted(
                        (entry for entry in level2 if entry.is_dir(follow_symlinks=False)),
                        key=lambda entry: entry.name,
                    ):
                        parts.append(
                            f"{d1.name}/{d2.name}:{d2.stat(follow_symlinks=False).st_mtime_ns}"
                        )
    except OSError as exc:
        # A dir vanishing mid-scan (a concurrent prune) or a permission fault:
        # no signal — never a spurious invalidation, never a masked error.
        _LOG.warning(
            "composite_join.revision_scan_failed",
            path=str(depth_dir),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
