"""Persist the :class:`~brutex.data.fetch_coverage.FetchCoverage` ledger beside the lake.

The skip-already-pulled ledger (DEC-FETCH-COVERAGE-001) is only quota-frugal
ACROSS process restarts if it SURVIVES one — otherwise every fresh ``make run``
re-pulls every window and re-burns the Dhan **5 r/s / 100k-day** budget
(CLAUDE.md §6). This module is that durable backing: a tiny, deterministic JSON
sidecar at ``<lake_root>/fetch_coverage.json`` that round-trips the ledger via
:meth:`FetchCoverage.to_records` / :meth:`FetchCoverage.from_records`
(DEC-FETCH-COVERAGE-WIRING-001).

Design (priority law — Correctness > Reliability > … > Performance):

* **Correctness.** The on-disk records are :meth:`to_records` output (a sorted,
  deterministic list of ``[market, security_id, interval, start, end]``), so the
  file is **byte-reproducible** for the same ledger (CLAUDE.md §2 Rule 7) and a
  load→save→load is a fixed point.
* **Reliability — never crash a pull.** :func:`load_fetch_coverage` degrades to
  an **empty** ledger (a structured WARNING, never an exception) on a missing,
  unreadable, malformed, or wrong-shaped file — re-pulling from scratch is always
  safe (the idempotent sink no-ops stored bars, CLAUDE.md §2 Rule 7), so a
  corrupt sidecar costs quota, never data.
* **Reliability — crash-safe write.** :func:`save_fetch_coverage` writes a
  sibling ``*.tmp`` then :func:`os.replace` (atomic on POSIX within one
  filesystem), so a crash mid-write can never leave a half-written ledger that
  would later be read as truth.

The O(1) membership / mark stays in :class:`FetchCoverage`; only the one-shot
load (run start) and save (run end) are O(N) over the recorded windows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

from brutex.data.fetch_coverage import FetchCoverage

#: Structured logger — surfaces a corrupt/absent sidecar (load) into ``/logs``
#: so a silent re-pull is never a mystery.
_LOG = structlog.get_logger("brutex.data.fetch_coverage_store")

#: The ledger sidecar filename, written directly under the lake root.
COVERAGE_FILENAME: str = "fetch_coverage.json"


def coverage_path(lake_root: Path) -> Path:
    """Return the ledger sidecar path for *lake_root* (``<lake_root>/fetch_coverage.json``)."""
    return lake_root / COVERAGE_FILENAME


def load_fetch_coverage(lake_root: Path) -> FetchCoverage:
    """Load the persisted ledger for *lake_root*, or an EMPTY one if unavailable.

    Reads :func:`coverage_path` and restores via
    :meth:`FetchCoverage.from_records`. **Never raises** (reliability — a re-pull
    must not be blocked by a bad sidecar): a missing file is a clean empty
    ledger; a malformed / wrong-shaped / unreadable file logs a WARNING and also
    degrades to empty (re-pulling is safe — the idempotent sink no-ops stored
    bars, so the only cost is quota, never data).

    Parameters
    ----------
    lake_root
        The lake directory whose sidecar to read.

    Returns
    -------
    FetchCoverage
        The restored ledger, or an empty one when no usable file exists.
    """
    path = coverage_path(lake_root)
    if not path.is_file():
        return FetchCoverage()
    try:
        raw = path.read_text(encoding="utf-8")
        records = json.loads(raw)
        # Coerce each row to the exact 5-string tuple from_records expects; a
        # wrong arity / non-string element raises here and is caught below.
        coerced = [
            (str(market), str(security_id), str(interval), str(start), str(end))
            for market, security_id, interval, start, end in records
        ]
        return FetchCoverage.from_records(coerced)
    except (OSError, ValueError, TypeError) as exc:
        # ValueError covers json.JSONDecodeError + bad-arity unpack + bad ISO
        # date in from_records; TypeError covers a non-iterable / non-record JSON
        # (e.g. an object or a bare number). Degrade to empty, but make it VISIBLE.
        _LOG.warning("fetch_coverage.load_failed", path=str(path), error=str(exc))
        return FetchCoverage()


def save_fetch_coverage(coverage: FetchCoverage, lake_root: Path) -> None:
    """Persist *coverage* to *lake_root*'s sidecar atomically (crash-safe).

    Serialises :meth:`FetchCoverage.to_records` (sorted, deterministic) to JSON,
    writes a sibling ``*.tmp`` file, then :func:`os.replace` onto the final path
    — so a reader never observes a partial write (reliability). The lake root is
    created if absent. O(N) over the recorded windows (called once per run).

    Parameters
    ----------
    coverage
        The ledger to persist (its accumulated completed windows).
    lake_root
        The lake directory to write the sidecar under.
    """
    lake_root.mkdir(parents=True, exist_ok=True)
    path = coverage_path(lake_root)
    tmp = path.with_name(f"{COVERAGE_FILENAME}.tmp")
    # to_records() is already sorted → deterministic, byte-reproducible JSON.
    tmp.write_text(json.dumps(coverage.to_records()), encoding="utf-8")
    os.replace(tmp, path)


def begin_pull_coverage(
    lake_root: Path, *, dry_run: bool, coverage: FetchCoverage | None
) -> FetchCoverage | None:
    """Resolve the ledger to thread into a pull (load the persisted one for a live run).

    The single place the dry-run rule lives (CLAUDE.md §11 COMMON), shared by the
    spot + options pull cores:

    * a **live** pull (*dry_run* ``False``) with no caller-supplied *coverage*
      LOADS the persisted ledger (:func:`load_fetch_coverage`) so windows already
      in the lake are skipped across process restarts (quota-frugal re-runs);
    * an explicit *coverage* is honoured unchanged (tests seed one);
    * a **dry-run** pull threads *coverage* unchanged (default ``None`` → no skip)
      and never touches disk — its null sink DISCARDS bars, so recording "this
      window is done" would be a lie that later skips a real fetch.
    """
    if dry_run:
        return coverage
    if coverage is not None:
        return coverage
    return load_fetch_coverage(lake_root)


def end_pull_coverage(coverage: FetchCoverage | None, lake_root: Path, *, dry_run: bool) -> None:
    """Persist *coverage* after a live pull — a no-op for dry-run or an absent ledger.

    The mirror of :func:`begin_pull_coverage`: only a **live** run (*dry_run*
    ``False``) with a real ledger writes the sidecar (:func:`save_fetch_coverage`),
    so the windows just completed are skipped on the next pull. A dry-run (null
    sink) never persists.
    """
    if not dry_run and coverage is not None:
        save_fetch_coverage(coverage, lake_root)


__all__ = [
    "COVERAGE_FILENAME",
    "begin_pull_coverage",
    "coverage_path",
    "end_pull_coverage",
    "load_fetch_coverage",
    "save_fetch_coverage",
]
