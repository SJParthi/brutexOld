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
token with three components:

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
* the **Greeks-overlay marker** component — ``os.stat`` of
  ``<lake_root>/.greeks_backfill/revision.json``
  (DEC-GREEKS-BULK-REENRICH-001 fix round 2; widened to BOTH overlay writers
  in fix round 8). TWO sanctioned production lake WRITERS rewrite option
  1-minute partitions' iv/delta overlay latest-wins WITHOUT ever touching the
  window ledger: the bulk Greeks backfill (``brutex greeks-backfill``) and
  the DEC-CLI-GREEKS-ENRICH-001 enrich pass (standalone ``brutex
  enrich-greeks``, plus the ``brutex fetch`` post-pull tail — which runs
  AFTER the pull's final ledger save, so its overlay writes land after the
  last ledger-token movement). So — exactly the
  CLI-``fetch``/CLI-``sweep-all`` staleness class the first two components
  closed — either run never moved the token and a running dashboard's
  ``/movers`` kept serving the PRE-write iv/delta the run existed to fix,
  until an unrelated pull or a restart. Every wet backfill batch / enrich
  pass that writes bars touches the SHARED marker
  (:func:`brutex.data.greeks_revision.touch_greeks_revision_marker`, atomic
  ``os.replace``), moving this component on the next poll.

:class:`~brutex.dashboard.summary_cache.SummaryCache` polls the composite token
via its ``revision_probe`` seam and bumps its generation when the token changes,
invalidating every generation-keyed cache for free.

The token→generation digest bridge (``revision_generation``) deliberately does
NOT live here — it is homed in :mod:`brutex.dashboard.movers_cache` beside the
cache whose generations it feeds, because a module-level
``db_view → lake_revision`` import closes a real import cycle through this
module's lazy ``window_ledger_store`` import (CodeQL ``py/cyclic-import``
alert 690; see :func:`brutex.dashboard.movers_cache.revision_generation`).

Honest envelope (Golden Rule #10 — stated, not hidden)
------------------------------------------------------
* The ledger component moves ONLY when a writer touches the window ledger. **All
  production pulls do** (the per-window checkpoint of
  DEC-CLI-LEDGER-CHECKPOINT-001 plus the end-of-pull save); the sanctioned
  production writers that do NOT — the bulk Greeks backfill AND the
  DEC-CLI-GREEKS-ENRICH-001 enrich pass (``brutex enrich-greeks`` / the
  ``brutex fetch`` post-ledger tail) — carry the shared Greeks-overlay marker
  component above (fix round 8: the pre-fix wording claimed the backfill was
  "the ONE" such writer, which the merge-absorbed enrich pass falsified — and
  the pass indeed left a running dashboard stale until it gained the marker
  touch). An out-of-band raw
  bar-Parquet write that BYPASSES both the ledger and the marker (e.g.
  hand-copying a partition file
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

#: The Greeks sidecar dir + overlay freshness-marker filename. MUST equal
#: ``brutex.data.greeks_revision.GREEKS_SIDECAR_DIR_NAME`` /
#: ``…REVISION_MARKER_FILE_NAME`` (the canonical home since fix round 8;
#: ``greeks_backfill.CHECKPOINT_DIR_NAME`` re-exports the dir) — pinned by
#: test, not imported: the historical consumer ``greeks_backfill`` pulls the
#: greeks-enrich (polars/trading_core-wheel) import graph, and this probe
#: stays lean stdlib-only (the ``_SWEEPS_DIRNAME`` precedent).
_GREEKS_BACKFILL_DIRNAME: Final[str] = ".greeks_backfill"
_GREEKS_REVISION_FILENAME: Final[str] = "revision.json"

#: The ``<sweeps>``-relative path of the sweep tree's hash-chained audit log.
#: MUST equal ``brutex.dashboard.sweep_results_view._AUDIT_RELPATH`` — pinned by
#: test, not imported (the :data:`_SWEEPS_DIRNAME` precedent: this module stays
#: a lean stdlib-only probe surface).
_SWEEP_AUDIT_RELPATH: Final[tuple[str, str]] = ("audit", "sweep-all.jsonl")

#: The metadata-pull registry cache's filename, a SIBLING of
#: ``instruments.duckdb`` directly under the lake root. MUST equal the literal
#: inside ``brutex.dashboard.db_view.registry_path_for`` (the ONE resolver) —
#: pinned by test, not imported: ``db_view`` pulls the polars/duckdb import
#: graph and this probe surface stays lean stdlib-only (the
#: :data:`_SWEEPS_DIRNAME` precedent).
_REGISTRY_FILENAME: Final[str] = "registry.duckdb"

#: DuckDB's write-ahead-log sibling suffix (``<database>.wal`` — the engine's
#: own naming law). Statted ALONGSIDE the main registry file (hostile-review
#: m1 on DEC-MEMORY-FOOTPRINT-HARDENING-001): a COMMIT lands in the WAL,
#: while the main file's ``mtime``/``size`` move only on CHECKPOINT
#: (connection close / WAL threshold) — probe-verified 2026-07-11: a
#: held-open connection's committed ``upsert_expiries`` moved the ``.wal``
#: stat and left the main file's stat byte-identical.
_REGISTRY_WAL_SUFFIX: Final[str] = ".wal"


def _stat_token(target: Path, *, event: str, include_ino: bool = False) -> str | None:
    """One path's ``"<mtime_ns>:<size>"`` stat token — the shared O(1) probe core.

    Returns :data:`ABSENT_TOKEN` when the path does not exist yet — including
    the ``ENOTDIR`` case (:class:`NotADirectoryError`: a stray regular FILE
    squatting where a sidecar DIRECTORY belongs, e.g. at
    ``<lake>/.greeks_backfill``). That stray-file state is a TOLERATED
    advisory-corruption class elsewhere (the checkpoint store + the wet
    backfill's marker touch both log + continue on it), so a marker under a
    non-directory ancestor genuinely does not exist and "absent" is the
    semantically-correct token — mapping it to ``None`` instead muted the
    ENTIRE composite probe (cross-process invalidation for pulls AND sweeps)
    until manual cleanup (refuter-confirmed, DEC-GREEKS-BULK-REENRICH-001 fix
    round 3). Returns ``None`` (logged under *event*) on any OTHER
    :class:`OSError` — a genuinely-unknown state (permissions, I/O fault).
    ``include_ino=True`` appends ``":<st_ino>"`` — sound for an
    atomic-``os.replace`` writer (every publish swaps the inode), so the token
    moves even on a coarse-mtime filesystem with an equal-size rewrite; the
    default ``False`` keeps every pre-existing token byte-unchanged
    (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001).
    """
    try:
        st = os.stat(target)
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT_TOKEN
    except OSError as exc:
        _LOG.warning(
            event,
            path=str(target),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    if include_ino:
        return f"{st.st_mtime_ns}:{st.st_size}:{st.st_ino}"
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


def rate_bridge_revision(lake_root: Path) -> str | None:
    """Return the CLI rate-bridge file's stat content token, or ``None``.

    DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001. One ``os.stat`` of
    ``<lake_root>/.rate/pull_rate.json`` (the COMMON
    :func:`brutex.data.rate_bridge.rate_bridge_path` — lazily imported, the
    ``_ledger_revision`` precedent, keeping this probe surface lean) folded to
    ``"<mtime_ns>:<size>:<st_ino>"``. The inode is included because the bridge
    publisher is an atomic tmp → ``fsync`` → ``os.replace``: every publish
    swaps the inode, so the token moves even on a coarse-mtime filesystem when
    an equal-size snapshot lands within one mtime granule (the lake is an
    external SSD — CLAUDE.md §8).

    Inherited :func:`_stat_token` semantics verbatim: a missing file → the
    stable :data:`ABSENT_TOKEN` (its creation moves the token exactly once);
    any other :class:`OSError` → a structlog WARN
    (``lake_revision.rate_bridge_stat_failed``) + ``None`` = "unknown, no
    signal — make NO freshness decision".

    HONESTY NOTE (Golden Rule #10): this token exists for the live-vs-cached
    PROVENANCE distinction and to skip a redundant decode of an unchanged file
    — it is NOT wedge protection: ``os.stat`` wedges exactly like
    ``read_bytes`` on a stalled mount, so the cache runs this probe INSIDE its
    deadline guard, never unbounded on a caller.

    Parameters
    ----------
    lake_root
        The lake root (the ``.rate`` sidecar dir lives directly under it).

    Returns
    -------
    str | None
        The stat token, :data:`ABSENT_TOKEN`, or ``None`` on a probe error.
    """
    from brutex.data.rate_bridge import rate_bridge_path

    return _stat_token(
        rate_bridge_path(lake_root),
        event="lake_revision.rate_bridge_stat_failed",
        include_ino=True,
    )


def registry_revision(lake_root: Path) -> str | None:
    """Return the ``registry.duckdb`` content token for *lake_root*, or ``None``.

    DEC-MEMORY-FOOTPRINT-HARDENING-001 — the missing cross-process freshness
    signal for the two options registry-derived caches
    (``_options_calendar_cache`` + ``_options_strikes_cache``, keyed on the
    write generation ONLY): a METADATA pull in ANOTHER process — the nightly
    headless ``run_full_pipeline``, a second dashboard, a CLI registry writer
    — upserts expiries + contracts into ``<lake_root>/registry.duckdb``
    without moving the ledger / sweeps / greeks composite, so an open
    dashboard kept serving the PRE-pull expiry tiers and ATM strike steps
    until an unrelated candle write bumped the generation.
    DEC-STALENESS-CACHE-TRUTH-001 (#1660) closed the same write class for the
    nav-latency caches with ``coverage_revision``; this token is the
    registry-file twin, folded into those caches' KEYS at their ``app.py``
    key-build sites so a committed registry write moves the key on the next
    poll.

    TWO ``os.stat`` probes — the main file ``<lake_root>/registry.duckdb``
    PLUS its DuckDB write-ahead-log sibling ``registry.duckdb.wal``
    (hostile-review m1 on DEC-MEMORY-FOOTPRINT-HARDENING-001) — folded to
    ``"<main mtime_ns>:<size>+<wal mtime_ns>:<size>"`` (the
    :func:`_ledger_revision` base+deltas composition shape). Inherited
    :func:`_stat_token` semantics per component: a never-created registry
    (a CLI-only-pulled lake — the graceful lake-fallback path) → the plain
    stable :data:`ABSENT_TOKEN` (both components absent; its first-ever
    creation moves the token exactly once); an absent ``.wal`` beside a
    present main file contributes the stable ``absent`` component (the
    steady no-writes state — the sentinel never oscillates the token); any
    other :class:`OSError` on either probe → a structlog WARN
    (``lake_revision.registry_stat_failed`` /
    ``…registry_wal_stat_failed``) + ``None`` = "unknown, no signal — make
    NO freshness decision".

    HONEST envelope (Golden Rule #10 — the m1 precision rewording): DuckDB
    COMMITS land in the WAL; the MAIN file's ``mtime``/``size`` move on
    CHECKPOINT (connection close / WAL threshold), so the main stat alone
    sees a metadata pull only because every production registry writer
    closes its connection when done (``RegistryStore.open`` is a context
    manager) — a COMPLETED cross-process pull always moves it. A path
    holding the connection open across commits would have defeated the
    main-file token until checkpoint — no such production path exists
    today, and the ``.wal`` component closes that gap anyway: a held-open
    commit creates/grows the WAL (probe-verified 2026-07-11 — the wal stat
    moved while the main stat did not), and the checkpoint that later
    REMOVES the WAL moves the composite again — correctly, a write
    happened; a read-only open of an existing registry creates NO wal
    (probe-verified), so idle polls never flap the token. Residual: the
    repo-wide same-``(mtime_ns, size)`` in-place-rewrite blind spot applies
    per component as everywhere (a rewrite landing byte-count-equal within
    one mtime granule is invisible — stated, not hidden).

    Parameters
    ----------
    lake_root
        The lake root (``registry.duckdb`` lives directly under it —
        ``db_view.registry_path_for``'s sibling-of-the-master law).

    Returns
    -------
    str | None
        The composite stat token, :data:`ABSENT_TOKEN`, or ``None`` on a
        probe error.
    """
    main = _stat_token(
        lake_root / _REGISTRY_FILENAME,
        event="lake_revision.registry_stat_failed",
    )
    if main is None:
        return None
    wal = _stat_token(
        lake_root / (_REGISTRY_FILENAME + _REGISTRY_WAL_SUFFIX),
        event="lake_revision.registry_wal_stat_failed",
    )
    if wal is None:
        return None
    if main == ABSENT_TOKEN and wal == ABSENT_TOKEN:
        return ABSENT_TOKEN  # never-created registry — the plain stable sentinel.
    return f"{main}+{wal}"


def _greeks_backfill_revision(lake_root: Path) -> str | None:
    """The Greeks-backfill marker component: one ``os.stat`` of the marker file.

    DEC-GREEKS-BULK-REENRICH-001 fix round 2 — the marker
    (``<lake_root>/.greeks_backfill/revision.json``) is touched by every wet
    backfill batch that writes overlay bars, so a CLI ``brutex
    greeks-backfill`` moves the composite token and a running dashboard's
    generation-keyed caches recompute (the backfill never checkpoints the
    window ledger, making this its ONLY cross-process revision signal).
    Returns :data:`ABSENT_TOKEN` when no backfill has ever written (the
    common case) — and equally for a stray regular FILE at
    ``.greeks_backfill`` (``ENOTDIR``; the tolerated advisory-corruption
    state, see :func:`_stat_token`) — ``None`` (logged) on any other
    :class:`OSError`.
    """
    return _stat_token(
        lake_root / _GREEKS_BACKFILL_DIRNAME / _GREEKS_REVISION_FILENAME,
        event="lake_revision.greeks_marker_stat_failed",
    )


def lake_revision(lake_root: Path) -> str | None:
    """Return the composite O(1)/O(dirs) content token for *lake_root*, or ``None``.

    The token is ``"<ledger component>|<sweeps component>|<greeks-backfill
    component>"`` — the window-ledger stat token (every production PULL moves
    it, DEC-CLI-LEDGER-CHECKPOINT-001) joined with the sweeps-tree directory
    digest (every results-store flush moves it,
    DEC-RESULTS-SURFACE-HONESTY-001) and the Greeks-backfill freshness-marker
    stat token (every bar-writing backfill batch moves it,
    DEC-GREEKS-BULK-REENRICH-001 fix round 2) — so a bar pull, a sweep write,
    OR a Greeks backfill from ANY process changes the composite. A component
    that does not exist yet contributes the stable :data:`ABSENT_TOKEN`
    sentinel (its creation moves the composite exactly once).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/`` / ``window_ledger/``
        / ``sweeps/`` / ``.greeks_backfill/``).

    Returns
    -------
    str | None
        The composite token, or ``None`` — "unknown, no signal" — when ANY
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
    greeks = _greeks_backfill_revision(lake_root)
    if greeks is None:
        return None
    return f"{ledger}|{sweeps}|{greeks}"


__all__ = [
    "ABSENT_TOKEN",
    "lake_revision",
    "rate_bridge_revision",
    "registry_revision",
    "sweep_results_revision",
    "sweeps_revision",
]
