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
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, TypeVar, cast

import structlog

_LOG = structlog.get_logger("brutex.dashboard.lake_revision")

_T = TypeVar("_T")

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

#: The ``<sweeps>``-relative path of the Stage-4 cascade report
#: (DEC-RESULTS-CASCADE-VERDICT-JOIN-001). MUST equal
#: ``cascade_wire.report_path(<sweeps>)`` — i.e. ``CASCADE_DIRNAME`` /
#: ``REPORT_FILENAME`` — pinned by test, not imported (the
#: :data:`_SWEEPS_DIRNAME` precedent: the engine wire module pulls the
#: metrics/numpy import graph; this probe stays lean stdlib-only).
_CASCADE_REPORT_RELPATH: Final[tuple[str, str]] = ("cascade", "cascade-report.json")


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


def cascade_report_revision(lake_root: Path) -> str | None:
    """Return the Stage-4 cascade report's stat content token, or ``None``.

    DEC-RESULTS-CASCADE-VERDICT-JOIN-001: one ``os.stat`` of
    ``<lake_root>/sweeps/cascade/cascade-report.json``
    (:data:`_CASCADE_REPORT_RELPATH`) folded to ``"<mtime_ns>:<size>"``.
    A DEDICATED file token is needed because the report is atomically
    OVERWRITTEN in place each sweep (``cascade_wire.write_report`` —
    tmp → ``os.replace``): the rewrite moves NO directory mtime that
    :func:`sweeps_revision` digests, so without this probe a ``/results``
    poll could serve verdicts from the PREVIOUS sweep's report forever
    (the ``sweep_results_revision`` audit-stat precedent).

    Inherited :func:`_stat_token` semantics verbatim: a missing file → the
    stable :data:`ABSENT_TOKEN` (its first-ever creation moves the token
    exactly once); ``None`` — "unknown, no signal" — on any other
    :class:`OSError`, logged (``lake_revision.cascade_report_stat_failed``).
    The caller folds ``None`` as a probe OUTAGE (the served payload holds —
    bounded staleness, never a fake flip).
    """
    return _stat_token(
        lake_root.joinpath(_SWEEPS_DIRNAME, *_CASCADE_REPORT_RELPATH),
        event="lake_revision.cascade_report_stat_failed",
    )


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


def registry_revision(registry_path: Path) -> str | None:
    """Return the F&O ``registry.duckdb`` cache's stat content token, or ``None``.

    DEC-O1-GUARANTEE-HARDEN-001 (the H1 registry-pager count cache): the ``/db``
    registry pagers' FILTERED ``COUNT(*)`` totals are paging-invariant, so they
    are memoised per registry CONTENT — and this token is the content signal.
    TWO stat probes folded into one token (O(1), never a table read):

    * the ``registry.duckdb`` file itself — a metadata pull's write session
      moves it on checkpoint/close;
    * its ``registry.duckdb.wal`` sibling — a CONCURRENT writer's rows land in
      the DuckDB write-ahead log BEFORE the main file's checkpoint, so folding
      the WAL's stat closes the mid-write staleness window (an appended WAL
      moves the token even while the main file's stat is still).

    Inherited :func:`_stat_token` semantics per component: a missing piece →
    the stable :data:`ABSENT_TOKEN` (an absent registry keys stably; its
    creation moves the token exactly once), any other :class:`OSError` → a
    structlog WARN (``lake_revision.registry_stat_failed``) + ``None`` =
    "unknown, no signal" — the consumer then computes fresh UNCACHED (the
    lake_revision-family outage posture: never a stale illusion).

    Parameters
    ----------
    registry_path
        The ``registry.duckdb`` path
        (:func:`brutex.dashboard.db_view.registry_path_for`).

    Returns
    -------
    str | None
        ``"<main token>|<wal token>"``, or ``None`` when either probe failed
        with a non-missing-file :class:`OSError`.
    """
    base = _stat_token(registry_path, event="lake_revision.registry_stat_failed")
    if base is None:
        return None
    wal = _stat_token(
        registry_path.with_name(registry_path.name + ".wal"),
        event="lake_revision.registry_stat_failed",
    )
    if wal is None:
        return None
    return f"{base}|{wal}"


#: Default TTL (seconds) for :class:`RevisionProbeMemo` — how long ONE probe
#: result may be re-served before the directory sweep re-runs. 1 s bounds the
#: per-poll O(dirs) probes (DEC-O1-GUARANTEE-HARDEN-001 H2) at a staleness far
#: under the 120 s adaptive-floor norm; the RateBridgeCache 2 s TTL
#: (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001) is the precedent.
DEFAULT_REVISION_PROBE_TTL_S: Final[float] = 1.0

#: The fail-loud env override for :func:`resolve_revision_probe_ttl`.
#: ``0`` disables the memo entirely (byte-identical legacy: every call
#: re-probes); junk raises at the memo's FIRST use, never a silent fallback.
REVISION_PROBE_TTL_ENV: Final[str] = "BRUTEX_REVISION_PROBE_TTL_S"


def resolve_revision_probe_ttl(env: Mapping[str, str] | None = None) -> float:
    """Resolve the revision-probe memo TTL from the environment — fail-loud.

    Reads :data:`REVISION_PROBE_TTL_ENV`; absent/blank →
    :data:`DEFAULT_REVISION_PROBE_TTL_S`. A junk value (non-float, negative,
    NaN, infinite) raises :class:`ValueError` — the house fail-loud env idiom
    (never a silent fallback that would mask an operator typo). ``0`` is the
    documented kill switch: the memo becomes a pass-through and every call
    runs the real probe (byte-identical legacy behaviour).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    float
        The TTL in seconds, ``>= 0`` and finite.

    Raises
    ------
    ValueError
        On a non-float / negative / non-finite override value.
    """
    source = os.environ if env is None else env
    raw = source.get(REVISION_PROBE_TTL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_REVISION_PROBE_TTL_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"invalid {REVISION_PROBE_TTL_ENV}={raw!r}: expected a finite float >= 0"
        ) from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid {REVISION_PROBE_TTL_ENV}={raw!r}: expected a finite float >= 0")
    return value


class RevisionProbeMemo:
    """A tiny TTL memo bounding per-poll revision-probe cost — COMMON, injectable.

    DEC-O1-GUARANTEE-HARDEN-001 (H2): two freshness probes are honest
    O(dirs)-per-poll sweeps — ``oneclick_artifacts.runs_revision_probe`` (an
    UNCAPPED O(run dirs) manifest-stat walk, the F5 ruling) and
    :func:`sweep_results_revision` (an O(sweep dirs) scandir digest) — and both
    re-ran on EVERY request. This memo serves the last probe RESULT for a short
    TTL (:func:`resolve_revision_probe_ttl` — default 1 s, env-overridable,
    ``0`` = disabled/pass-through), so N polls within one TTL pay ONE sweep.

    Honest envelope (Golden Rule #10):

    * staleness is bounded by the TTL — a write landing right after a probe is
      seen at most ``ttl`` seconds late (default 1 s, far under the 120 s
      adaptive-floor norm); a poll AFTER the TTL always re-probes, so there is
      no permanent-staleness state (clock-injected test-pinned).
    * a probe result the *cacheable* predicate rejects (default: ``None`` — the
      probe-outage verdict) is NEVER memoised and evicts any prior entry, so an
      outage/recovery verdict is re-derived per call exactly as before.
    * the probe runs INSIDE the lock (single-flight): concurrent cold pollers
      pay one sweep and the rest wait — on a WEDGED volume that serialises the
      pollers behind one wedged probe instead of wedging each independently
      (the pre-memo behaviour); the memo is a cost bound, not wedge protection.
    * entries are keyed by caller-supplied server-derived keys (a handful per
      app — never client-typed), so the dict is bounded by construction.
    * the TTL is resolved lazily at the FIRST :meth:`get` (junk env fails loud
      there, the documented contract) and latched for the instance's lifetime.

    Parameters
    ----------
    ttl_s
        Explicit TTL override (tests / injection); ``None`` resolves the env
        via :func:`resolve_revision_probe_ttl` at first use.
    clock
        Injectable monotonic clock (tests); production keeps
        :func:`time.monotonic`.
    """

    def __init__(
        self,
        *,
        ttl_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_override = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, object]] = {}
        self._resolved_ttl: float | None = ttl_s

    def _resolve_ttl(self) -> float:
        """Latch the TTL — the injected override, else the fail-loud env resolve."""
        resolved = self._resolved_ttl
        if resolved is None:
            resolved = resolve_revision_probe_ttl()
            self._resolved_ttl = resolved
        return resolved

    def get(
        self,
        key: str,
        probe: Callable[[], _T],
        *,
        cacheable: Callable[[_T], bool] | None = None,
    ) -> _T:
        """Return *key*'s memoised probe result, re-running *probe* past the TTL.

        The caller owns key↔type discipline (one probe result type per key —
        both production keys embed the probed root path). *cacheable* decides
        whether a fresh result may be memoised; ``None`` means
        ``value is not None`` (a ``None`` probe verdict — outage — is never
        held for the TTL). O(1) per call on a hit; one probe per TTL window
        per key otherwise.
        """
        ttl = self._resolve_ttl()
        if ttl <= 0.0:
            return probe()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (self._clock() - entry[0]) < ttl:
                return cast("_T", entry[1])
            value = probe()
            ok = (value is not None) if cacheable is None else cacheable(value)
            if ok:
                self._entries[key] = (self._clock(), value)
            else:
                self._entries.pop(key, None)
            return value

    def reset(self) -> None:
        """Drop every memoised entry (the house cache reset seam)."""
        with self._lock:
            self._entries.clear()


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
    "DEFAULT_REVISION_PROBE_TTL_S",
    "REVISION_PROBE_TTL_ENV",
    "RevisionProbeMemo",
    "cascade_report_revision",
    "lake_revision",
    "rate_bridge_revision",
    "registry_revision",
    "resolve_revision_probe_ttl",
    "sweep_results_revision",
    "sweeps_revision",
]
