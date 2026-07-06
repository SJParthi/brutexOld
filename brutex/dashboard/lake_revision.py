"""Cross-process lake freshness token — O(1)/O(dirs) ``stat`` probes, no tree walk.

DEC-HARDENING-LEFTOVERS-001 (+ the sweeps component,
DEC-RESULTS-SURFACE-HONESTY-001). The dashboard's freshness signal is the
in-process :class:`~brutex.dashboard.summary_cache.SummaryCache` generation
counter, bumped by :meth:`~brutex.dashboard.summary_cache.SummaryCache.mark_dirty`
at the dashboard's OWN write sites (the background pull job, materialize, sweep).
A CLI ``brutex fetch`` / ``brutex sweep-all`` writing into the SAME
``BRUTEX_LAKE_ROOT`` runs in a **separate process** — it never touches that int,
so every generation-keyed cache (``/db`` totals + dataset discovery, the F&O/spot
cascades, ``/coverage/windows`` counts, ``/movers``, coverage/completeness/
mapping, the ``/results`` sweep leaderboard) served pre-CLI-write data until a
dashboard restart.

This module supplies the missing cross-process signal as ONE composite content
token with two components:

* the **window-ledger** component — ``os.stat`` of the durable window-ledger
  Parquet (``<lake_root>/window_ledger/ledger.parquet``). Every production pull
  — dashboard AND CLI — checkpoints that ledger per window
  (DEC-CLI-LEDGER-CHECKPOINT-001), so a bar pull from ANY process moves it.
* the **sweeps-tree** component (:func:`sweeps_revision`) — a bounded
  directory-mtime digest over ``<lake_root>/sweeps`` (the per-tuple results
  tree ``<sweeps>/<instrument>/<tf>/*.parquet``). The #1069 probe statted ONLY
  the window ledger, so an out-of-band ``ResultsStore`` write (a CLI
  ``sweep-all``) never bumped the generation and the ``/results`` leaderboard
  stayed stale — AND inconsistent across cache keys (a never-viewed filter
  showed fresh rows while a viewed one served the pre-write cache). Every
  results-store flush is a tempfile + ``os.replace`` into its
  ``<instrument>/<tf>/`` directory, which moves that directory's mtime — the
  digest sees it on the next poll.

:class:`~brutex.dashboard.summary_cache.SummaryCache` polls the composite token
via its ``revision_probe`` seam and bumps its generation when the token changes,
invalidating every generation-keyed cache for free.

Honest envelope (Golden Rule #10 — stated, not hidden)
------------------------------------------------------
* The ledger component moves ONLY when a writer touches the window ledger. **All
  production pulls do** (the per-window checkpoint of
  DEC-CLI-LEDGER-CHECKPOINT-001 plus the end-of-pull save). An out-of-band raw
  bar-Parquet write that BYPASSES the ledger (e.g. hand-copying a partition file
  into ``bars/``) is **NOT detected** — explicitly out of scope; detecting it
  would need an O(files) tree walk per probe (not fabricated as O(1)) or the
  write-time lake manifest already named as future work (#867/#1025).
* The sweeps component is **O(directories), NOT O(files)** — one ``scandir`` of
  the sweeps root plus one per instrument directory, statting only the
  two-level ``<instrument>/<tf>/`` dirs (bounded by instruments × timeframes —
  dozens, never the store count). The honest cost of catching nested writes: a
  root-only ``stat`` misses a store landing in an EXISTING ``<tf>/`` dir
  (directory mtimes only move for DIRECT children), so the root stat alone
  would be exactly the staleness bug this fixes.
* The sweeps component keys on directory **mtimes** — it sees any create /
  ``os.replace`` / unlink in those dirs (every repo writer is a tempfile
  followed by ``os.replace``). An exotic in-place file rewrite that never
  renames would NOT move its parent dir mtime and is not detected (no repo
  writer does this).
* Each probe is O(1)-per-directory ``stat`` calls — safe on every generation
  read; a probe error maps to ``None`` = "no signal" (never a spurious
  invalidation, never a masked error).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

import structlog

_LOG = structlog.get_logger("brutex.dashboard.lake_revision")

#: Stable sentinel component for a lake whose window ledger / sweeps tree does
#: not exist yet (a fresh/empty lake, or one populated before the ledger
#: existed). Stable so a missing component never oscillates the token: the FIRST
#: write that creates it moves ``absent`` → a real stat token exactly once.
ABSENT_TOKEN: Final[str] = "absent"  # noqa: S105  # nosec B105 — a freshness sentinel, not a secret

#: The per-tuple results tree's sub-directory of the lake root. MUST equal
#: ``brutex.dashboard.sweep_results_view.SWEEPS_DIRNAME`` — pinned by test, not
#: imported: that module pulls the engine/results-store (Rust-wheel) import
#: graph, and this probe must stay a lean stdlib-only surface the SummaryCache
#: can call on every generation read (the ``trial_stats._TUPLE_DIGEST_LEN``
#: precedent).
_SWEEPS_DIRNAME: Final[str] = "sweeps"

#: The ``<sweeps>``-relative path of the sweep tree's hash-chained audit log.
#: MUST equal ``brutex.dashboard.sweep_results_view._AUDIT_RELPATH`` — pinned by
#: test, not imported (the :data:`_SWEEPS_DIRNAME` precedent: this module stays
#: a lean stdlib-only probe surface).
_SWEEP_AUDIT_RELPATH: Final[tuple[str, str]] = ("audit", "sweep-all.jsonl")


def revision_generation(token: str) -> int:
    """Map a content token onto a ``MoversSnapshotCache`` generation int — one digest bridge.

    DEC-LEDGER-DELTA-O-PAGE-001: the reused
    :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` keys freshness on
    an ``int`` *generation*, while the lake content signals are opaque string
    tokens (a ledger stamp token, a revision composite). This bridge digests the
    token to the first 8 bytes of its SHA-256 — a 64-bit int, so ANY token
    change moves the generation. HONEST envelope (Golden Rule #10): two distinct
    tokens collide with probability ~2\\ :sup:`-64`; a collision would serve ONE
    stale entry for that colliding pair and self-heals on the next token move —
    documented, not hidden. Pure, O(len(token)).
    """
    return int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")


def _stat_token(target: Path, *, event: str) -> str | None:
    """One path's ``"<mtime_ns>:<size>"`` stat token — the shared O(1) probe core.

    Returns :data:`ABSENT_TOKEN` when the path does not exist yet, ``None``
    (logged under *event*) on any other :class:`OSError`.
    """
    try:
        st = os.stat(target)
    except FileNotFoundError:
        return ABSENT_TOKEN
    except OSError as exc:
        _LOG.warning(
            event,
            path=str(target),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


def _ledger_revision(lake_root: Path) -> str | None:
    """The window-ledger component: the base Parquet stat + the deltas-dir stat.

    Resolved via the COMMON :func:`brutex.data.window_ledger_store.ledger_path` /
    :func:`~brutex.data.window_ledger_store.deltas_dir` — the ONE place those
    paths are defined (CLAUDE.md §11). Since DEC-LEDGER-DELTA-FLUSH-001 the
    mid-pull flush no longer rewrites ``ledger.parquet`` — it APPENDS a delta
    file under ``window_ledger/deltas/`` — so the base stat alone would FREEZE
    the cross-process token for the whole pull. The deltas DIRECTORY's mtime
    moves on every direct-child create/delete (each delta append + the pull-end
    subsume-prune), so folding its stat keeps the token honest at the same O(1)
    cost (two ``stat`` calls). Returns :data:`ABSENT_TOKEN`-composed tokens for
    missing pieces, ``None`` (logged) when either stat fails with a
    non-missing-file :class:`OSError`.
    """
    from brutex.data.window_ledger_store import deltas_dir, ledger_path

    base = _stat_token(ledger_path(lake_root), event="lake_revision.stat_failed")
    if base is None:
        return None
    deltas = _stat_token(deltas_dir(lake_root), event="lake_revision.deltas_stat_failed")
    if deltas is None:
        return None
    return f"{base}+{deltas}"


def sweeps_revision(lake_root: Path) -> str | None:
    """Return the sweeps-tree component token for *lake_root*, or ``None``.

    A bounded directory-mtime digest over ``<lake_root>/sweeps`` (the per-tuple
    results tree — :data:`_SWEEPS_DIRNAME`, test-pinned to
    ``sweep_results_view.SWEEPS_DIRNAME``): the sweeps root's own mtime plus
    the name + mtime of every first-level
    directory and every second-level ``<instrument>/<tf>/`` directory, folded
    into one sha256 hex token. Every results-store flush is a tempfile +
    ``os.replace`` into its ``<tf>/`` dir, so a store written by ANY process —
    the dashboard's sweep job or a CLI ``brutex sweep-all`` — moves that dir's
    mtime and therefore the token (DEC-RESULTS-SURFACE-HONESTY-001).

    Cost + envelope: O(directories) ``scandir``/``stat`` — bounded by
    instruments × timeframes, NEVER the store/file count (see the module
    docstring's honest-envelope notes).

    Returns
    -------
    str | None
        * a digest token when the sweeps tree exists;
        * :data:`ABSENT_TOKEN` (a stable sentinel) when it does not exist yet;
        * ``None`` — "unknown, no signal" — on any OTHER :class:`OSError`,
          logged as a structlog warning.
    """
    sweeps_root = lake_root / _SWEEPS_DIRNAME
    try:
        st_root = os.stat(sweeps_root)
    except FileNotFoundError:
        return ABSENT_TOKEN
    except OSError as exc:
        _LOG.warning(
            "lake_revision.sweeps_stat_failed",
            sweeps_root=str(sweeps_root),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    parts: list[str] = [f".:{st_root.st_mtime_ns}"]
    try:
        with os.scandir(sweeps_root) as level1:
            first_level = sorted(
                (entry for entry in level1 if entry.is_dir(follow_symlinks=False)),
                key=lambda entry: entry.name,
            )
            for d1 in first_level:
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
            "lake_revision.sweeps_scan_failed",
            sweeps_root=str(sweeps_root),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def sweep_results_revision(lake_root: Path) -> str | None:
    """Return the ``/results`` content token for *lake_root*, or ``None``.

    DEC-LEDGER-DELTA-O-PAGE-001: the composite is the :func:`sweeps_revision`
    directory-mtime digest PLUS the audit chain's ``(mtime_ns:size)`` stat
    token (``<sweeps>/audit/sweep-all.jsonl`` — :data:`_SWEEP_AUDIT_RELPATH`,
    test-pinned to ``sweep_results_view._AUDIT_RELPATH``).
    :func:`sweeps_revision` alone digests DIRECTORY mtimes, and an append to
    the audit chain (a ``SWEEP_START`` — the era / mixed-era honesty source
    the leaderboard scan reads) moves NO directory mtime, so the file stat
    closes that invalidation gap. Every other ``/results``-visible write moves
    a directory mtime already: a results-store flush (dashboard sweep OR a CLI
    ``brutex sweep-all``) is a tempfile + ``os.replace`` into ``<tf>/``, and a
    store delete moves its parent dir.

    Cost + envelope: one O(dirs) scandir digest plus ONE ``os.stat`` — paid per
    poll (honest, never claimed O(1)-total). An absent chain contributes the
    stable :data:`ABSENT_TOKEN` (its first-ever creation moves the composite
    exactly once). ``None`` = "unknown, no signal" (either probe failed with a
    non-missing-file :class:`OSError`, logged inside the component) — the
    caller must compute fresh and cache NOTHING (never a stale serve, never a
    poisoned key — Golden Rule #1).
    """
    dirs = sweeps_revision(lake_root)
    if dirs is None:
        return None
    chain = _stat_token(
        lake_root.joinpath(_SWEEPS_DIRNAME, *_SWEEP_AUDIT_RELPATH),
        event="lake_revision.sweep_audit_stat_failed",
    )
    if chain is None:
        return None
    return f"{dirs}|{chain}"


def lake_revision(lake_root: Path) -> str | None:
    """Return the composite O(1)/O(dirs) content token for *lake_root*, or ``None``.

    The token is ``"<ledger component>|<sweeps component>"`` — the window-ledger
    stat token (every production PULL moves it,
    DEC-CLI-LEDGER-CHECKPOINT-001) joined with the sweeps-tree directory digest
    (every results-store flush moves it, DEC-RESULTS-SURFACE-HONESTY-001) — so a
    bar pull OR a sweep write from ANY process changes the composite. A
    component that does not exist yet contributes the stable
    :data:`ABSENT_TOKEN` sentinel (its creation moves the composite exactly
    once).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/`` / ``window_ledger/``
        / ``sweeps/``).

    Returns
    -------
    str | None
        The composite token, or ``None`` — "unknown, no signal" — when EITHER
        component probe failed with a non-missing-file :class:`OSError`
        (logged inside the component). ``None`` tells the caller to make NO
        freshness decision (never a spurious invalidation, never a masked
        error).
    """
    ledger = _ledger_revision(lake_root)
    if ledger is None:
        return None
    sweeps = sweeps_revision(lake_root)
    if sweeps is None:
        return None
    return f"{ledger}|{sweeps}"


__all__ = [
    "ABSENT_TOKEN",
    "lake_revision",
    "revision_generation",
    "sweep_results_revision",
    "sweeps_revision",
]
