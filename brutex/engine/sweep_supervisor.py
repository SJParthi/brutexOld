"""Live guarantee-&-assurance supervisor for the brute-force grid sweep (DEC-SWEEP-WIRE-GA-001).

Phase 3 Wave-2b: the seam that makes the real-time **guarantee + assurance**
layer LIVE. The Wave-1 modules built five standalone G&A bricks
(DEC-SWEEP-REALTIME-GUARANTEE-001, MIGRATION.md #549) and Wave-2a built the grid
:func:`~brutex.engine.sweep_orchestrator.run_sweep_grid` (DEC-SWEEP-ORCHESTRATOR-001,
#554); this module **wires them together** so every grid tuple the sweep runs is:

1. **audit-logged** to a BLAKE3 hash-chain (:class:`~brutex.engine.sweep_audit.SweepAuditWriter`
   — CLAUDE.md Golden Rule #8),
2. **counted** by :class:`~brutex.engine.sweep_progress.SweepProgress` (percent / ETA /
   tuples-per-sec, surfaced on the dashboard ``/api/v1/sweep-progress`` route),
3. **checkpointed** after completion via
   :class:`~brutex.engine.sweep_checkpoint.SweepCheckpointStore` so a killed sweep
   **resumes** from the next tuple instead of recomputing from zero (Golden Rule #7),
4. **dedup-skipped** at the tuple grain via
   :class:`~brutex.engine.sweep_cache.SweepCache` (Bloom→LMDB, O(1)) when the exact
   same tuple identity was already completed, and
5. **heartbeated** to :class:`~brutex.engine.sweep_watchdog.SweepWatchdog` so a
   tuple that wedges past the timeout is detected.

It **composes** the golden-locked engine bricks via the shared
:func:`~brutex.engine.sweep_orchestrator.run_one_tuple` body (the same per-tuple
execution :func:`run_sweep_grid` uses) and re-implements none of them
(CLAUDE.md §11 COMMON). It does **not** edit
:func:`~brutex.engine.sweep_run.run_persisted_sweep` (which keeps its 3-line
per-sweep audit + redo-not-skip ordering).

Honest envelope (Golden Rule #10) — what each layer does and does NOT guarantee
-------------------------------------------------------------------------------
* **Granularity is the grid TUPLE**, the unit the orchestrator loop iterates
  (``(instrument, tf, direction, k, sl, tp, proximity)``). **Mask-level
  (per-u64-mask) dedup is ALREADY provided by the shared
  :class:`~brutex.engine.sweep_run.ComboDedup` inside ``run_persisted_sweep``**
  (Bloom→exact on the 9-field run-identity); this supervisor's
  :class:`SweepCache` adds a COARSER, content-addressed tuple-level fast-skip on
  top of it. The two do not conflict (different key shapes) — the cache lets a
  fully-completed tuple be skipped in true O(1) without re-paying mask
  enumeration. W2-b requirement (4) "dedup-skipped if ``(mask, …)`` is in the
  cache" is satisfied at the **mask** grain by ``ComboDedup`` and at the
  **tuple** grain by this cache; neither is a claim of O(1)-total dedup.
* **Every operation is O(1) per tuple** — one cache probe, one audit append, one
  atomic checkpoint write, one progress mutation, one watchdog beat. The grid as
  a whole is inherently **O(T) tuples × O(Nᵢ) masks** = O(Σ Nᵢ) work plus O(T)
  flushes/fsyncs (one audit ``flush`` + one checkpoint ``fsync`` per tuple).
  Claiming O(1) **total** for a whole sweep is physically impossible and is
  FORBIDDEN. This driver's own memory is O(1) in ``T`` (scalar tallies only; the
  per-tuple detail streams through the audit chain, the Parquet stores, and the
  optional ``on_tuple`` / ``on_progress`` hooks).
* **Watchdog detection is INTER-TUPLE only.** With ``watchdog_poll_seconds`` set,
  a daemon thread calls :meth:`SweepWatchdog.check_once` on a cadence while the
  main thread :meth:`~brutex.engine.sweep_watchdog.SweepWatchdog.beat`\\ s at the
  start of each tuple — so a tuple that runs longer than the timeout is detected.
  A hang **inside** one tuple's mask loop (inside the golden-locked
  ``run_persisted_sweep``) is NOT detectable at sub-tuple grain without modifying
  that runner — a tracked follow-up. With ``watchdog_poll_seconds=None`` the
  heartbeat is recorded (liveness for the snapshot) but **no active detection**
  runs. No illusion (Golden Rule #10).

Crash-safety contract
---------------------
The checkpoint is recorded **after** ``run_one_tuple`` returns (and after the
store flush + sidecar + ``ComboDedup`` mark + completion sentinel inside it). A
``SIGKILL`` mid-tuple therefore leaves that tuple's index un-recorded → it is
re-processed on resume, where the tuple's COMPLETION SENTINEL decides
(DEC-MARK-MANY-SENTINEL-001): a PAIRED sentinel short-circuits the tuple
entirely (O(1) check + one store re-hash — no re-evaluation); an
absent/unpaired sentinel forces a FULL re-evaluation with dedup probes
bypassed, where :func:`~brutex.engine.sweep_orchestrator.load_or_create_store`
+ idempotent appends re-flush the SAME rows byte-identically (redo-not-skip;
never a silent hole, never a double persist, never a SUBSET re-rank). The
prior claim here that ``ComboDedup`` "skips every already-flushed mask" on
resume was FALSE cross-process (the production dedup is in-memory — a new
process starts with no marks) and unsafe same-process (a crash inside the
per-key mark loop leaves a subset; MIGRATION.md #650) — corrected per
DEC-MARK-MANY-SENTINEL-001. The :class:`SweepCache` tuple key is the **full**
tuple+grid identity (incl. ``condition_bits``, ``k``, ``top_k``, ``min_hits``,
``max_combos`` + the three reproducibility hashes) so a re-run with ANY
changed knob computes a different key and is never falsely skipped — the
cache-skip path has no preload-and-reflush safety net, so its key is
intentionally stricter than the per-tuple Parquet path digest.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import blake3
import structlog

from brutex.engine.sweep_cache import SweepCache, combo_digest
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_kernel import ResidentKernelCache
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    count_sweep_tuples,
    iter_sweep_tuples,
    run_one_tuple,
)
from brutex.engine.sweep_progress import SweepProgress, SweepProgressSnapshot
from brutex.engine.sweep_run import SweepComboError, _direction_label
from brutex.engine.sweep_watchdog import SweepWatchdog

if TYPE_CHECKING:
    # Annotation-only — keep the `fastbloom_rs` / `lmdb` PyO3 wheels (pulled by
    # the DedupSink impls' ComboDedup→OptionDedupIndex and AuditWriter→bloom)
    # and the CandleBar import
    # out of this module's runtime import graph, exactly as sweep_orchestrator /
    # sweep_run do, so the strict per-module `--cov=brutex.engine` gate stays
    # clean. All five G&A objects + the dedup + the audit writer are *received*,
    # never constructed here (SweepCache is opened by the CLI launcher).
    from pathlib import Path

    from brutex.data.models import CandleBar
    from brutex.engine.results_store import ResultsStore
    from brutex.engine.sweep_audit import SweepAuditWriter
    from brutex.engine.sweep_run import DedupSink, SweepRunSummary
    from brutex.orchestrator.audit import AuditWriter

_LOG = structlog.get_logger("brutex.engine.sweep_supervisor")

#: Audit action labels — the supervisor's BLAKE3 chain (one per tuple + brackets).
#: RESUME emission is asymmetric between the two drivers (Golden Rule #10): the
#: sequential loop below emits ONE summarising RESUME line per resumed run
#: (DEC-SWEEP-SCALE-OPERABILITY-001), while :mod:`brutex.engine.sweep_parallel` still
#: emits one per-tuple RESUME line — batching it is a flagged follow-up.
_ACTION_START: Final[str] = "SUPERVISOR_START"
_ACTION_TUPLE_DONE: Final[str] = "TUPLE_DONE"
_ACTION_TUPLE_CACHE: Final[str] = "TUPLE_SKIPPED_CACHE"
_ACTION_TUPLE_RESUME: Final[str] = "TUPLE_SKIPPED_RESUME"
_ACTION_TUPLE_NO_BARS: Final[str] = "TUPLE_NO_BARS"
#: A tuple that raised mid-evaluation and was SKIPPED ("Resilient: skip +
#: continue") — its cache/checkpoint are DELIBERATELY not recorded so a resume
#: re-runs it (the sweep_heal "C5" lesson).
_ACTION_TUPLE_FAILED: Final[str] = "TUPLE_FAILED"
_ACTION_COMPLETE: Final[str] = "SUPERVISOR_COMPLETE"
_ACTION_ABORT: Final[str] = "SUPERVISOR_ABORT"

#: PUBLIC aliases of the supervisor audit-action labels, so the parallel driver
#: (:mod:`brutex.engine.sweep_parallel`, DEC-MULTICORE-SWEEP-PHASE1-001) can emit
#: the BYTE-IDENTICAL supervisor chain without importing the private originals —
#: ONE source of truth (CLAUDE.md §11 COMMON), re-exported, never re-defined.
SUPERVISOR_ACTION_START: Final[str] = _ACTION_START
SUPERVISOR_ACTION_TUPLE_DONE: Final[str] = _ACTION_TUPLE_DONE
SUPERVISOR_ACTION_TUPLE_CACHE: Final[str] = _ACTION_TUPLE_CACHE
SUPERVISOR_ACTION_TUPLE_RESUME: Final[str] = _ACTION_TUPLE_RESUME
SUPERVISOR_ACTION_TUPLE_NO_BARS: Final[str] = _ACTION_TUPLE_NO_BARS
SUPERVISOR_ACTION_TUPLE_FAILED: Final[str] = _ACTION_TUPLE_FAILED
SUPERVISOR_ACTION_COMPLETE: Final[str] = _ACTION_COMPLETE
SUPERVISOR_ACTION_ABORT: Final[str] = _ACTION_ABORT

#: Default supervisor worker name for the (single) sweep thread's heartbeat.
DEFAULT_WORKER_NAME: Final[str] = "sweep-worker-0"

#: The idle snapshot the dashboard route serves before any sweep has started.
_IDLE_PROGRESS: Final[dict[str, Any]] = {
    "state": "idle",
    "enumerated": 0,
    "evaluated": 0,
    "survivors": 0,
    "skipped": 0,
    "current": "",
    "detail": "",
    "percent": 0.0,
    "running": False,
    "elapsed_seconds": 0.0,
    "combos_per_sec": 0.0,
    "eta_seconds": None,
    "survivor_rate": 0.0,
    "started_at": 0.0,
    "updated_at": 0.0,
}


@dataclass(frozen=True, slots=True)
class SweepGuards:
    """The five live guarantee-&-assurance objects, bundled for one supervised grid.

    Every field is **received** (constructed + lifecycle-owned by the caller — the
    CLI / dashboard launcher); the supervisor never opens or closes them. This
    keeps the ``fastbloom_rs`` / ``lmdb`` wheels (pulled only by
    :meth:`SweepCache.open <brutex.engine.sweep_cache.SweepCache.open>`) out of
    this module's import graph.

    Attributes
    ----------
    audit
        The BLAKE3 hash-chained supervisor audit log
        (:class:`~brutex.engine.sweep_audit.SweepAuditWriter`), open for append.
    progress
        The O(1) live-progress tracker
        (:class:`~brutex.engine.sweep_progress.SweepProgress`).
    checkpoint
        The crash-safe resume cursor
        (:class:`~brutex.engine.sweep_checkpoint.SweepCheckpointStore`); MUST be
        built with ``sweep_id == grid_identity(grid)`` and
        ``total_combos == count_sweep_tuples(grid)`` (see :func:`run_supervised_grid`).
    cache
        The Bloom→LMDB tuple-level dedup cache
        (:class:`~brutex.engine.sweep_cache.SweepCache`).
    watchdog
        The hung-tuple heartbeat detector
        (:class:`~brutex.engine.sweep_watchdog.SweepWatchdog`).
    worker_name
        The heartbeat worker identity for the (single) sweep thread.
    """

    audit: SweepAuditWriter
    progress: SweepProgress
    checkpoint: SweepCheckpointStore
    cache: SweepCache
    watchdog: SweepWatchdog
    worker_name: str = DEFAULT_WORKER_NAME


@dataclass(frozen=True, slots=True)
class SupervisedGridSummary:
    """The aggregate tally :func:`run_supervised_grid` returns.

    Mirrors :class:`~brutex.engine.sweep_orchestrator.SweepGridSummary` (so the
    run/skip/funnel counts keep the same meaning) and ADDS the two supervisor-only
    skip reasons plus the resolved ``sweep_id``. It is a **separate** frozen
    dataclass, not a subclass (a ``slots=True`` frozen dataclass cannot add slots
    via inheritance cleanly).

    Attributes
    ----------
    sweep_id
        The stable :func:`grid_identity` digest used as the checkpoint key.
    tuples_total
        The full grid size (``count_sweep_tuples(grid)``).
    tuples_run
        Tuples that had bars and were evaluated this run.
    tuples_skipped_no_bars
        Tuples skipped because ``bars_for`` returned no bars.
    skipped_cache
        Tuples short-circuited because their full identity was already in the
        :class:`SweepCache` (a prior completed run).
    skipped_resume
        Tuples skipped because their index was at/below the loaded checkpoint
        high-water mark (resumed from a prior life).
    enumerated, conflict_pruned, skipped_dedup, evaluated, ranked, persisted
        The per-tuple :class:`~brutex.engine.sweep_run.SweepRunSummary` fields,
        summed across every tuple that ran this run.
    tuples_failed
        Tuples whose combo evaluation raised a
        :class:`~brutex.engine.sweep_run.SweepComboError` mid-sweep and were
        SKIPPED ("Resilient: skip + continue"): the supervisor finished the
        remaining tuples but a non-zero count makes the run's verdict NON-ZERO.
        CRITICALLY, a failed tuple's :class:`SweepCache` entry +
        :class:`~brutex.engine.sweep_checkpoint.SweepCheckpointStore` index are
        DELIBERATELY not recorded, so a resume/cache re-runs it (the sweep_heal
        "C5" lesson — a failed tuple must never be permanently skipped). NOT
        counted as run, skipped-no-bars, cache, or resume.
    """

    sweep_id: str
    tuples_total: int
    tuples_run: int
    tuples_skipped_no_bars: int
    skipped_cache: int
    skipped_resume: int
    enumerated: int
    conflict_pruned: int
    skipped_dedup: int
    evaluated: int
    ranked: int
    persisted: int
    tuples_failed: int


def _grid_descriptor(grid: SweepGridConfig) -> dict[str, Any]:
    """Return the canonical JSON-serialisable identity descriptor of *grid*.

    Every field that distinguishes one grid from another — all seven swept axes
    plus the shared context, the reproducibility hashes, and the honest guards —
    so two grids that differ in ANY knob produce different digests. ``None`` legs
    and proximity tolerances are flattened to JSON-native scalars (Golden Rule #4).
    """
    return {
        "instruments": list(grid.instruments),
        "timeframes": [[tf.label, tf.minutes] for tf in grid.timeframes],
        "directions": [d.value for d in grid.directions],
        "k_values": list(grid.k_values),
        "condition_bits": sorted(set(grid.condition_bits)),
        "stop_losses": list(grid.stop_losses),
        "take_profits": list(grid.take_profits),
        "proximities": [
            None
            if p is None
            else [p.pivot_proximity_bps, p.pdh_pdl_proximity_bps, p.fib_proximity_bps]
            for p in grid.proximities
        ],
        # The swept Fib-anchor axis distinguishes one grid from another
        # (DEC-NIFTY-FIB-AXIS-001): a grid that adds/removes an anchor is a
        # different sweep, so its checkpoint must not silently resume the old work.
        "fib_anchors": [a.value for a in grid.fib_anchors],
        "mode": grid.mode,
        "data_snapshot_hash": grid.data_snapshot_hash,
        "indicator_version": grid.indicator_version,
        "code_commit_sha": grid.code_commit_sha,
        "prior_n": grid.prior_n,
        "start_equity_paisa": grid.start_equity_paisa,
        "max_combos": grid.max_combos,
        "top_k": grid.top_k,
        "min_hits": grid.min_hits,
    }


def grid_identity(grid: SweepGridConfig) -> str:
    """Return the stable 64-char BLAKE3 identity of *grid* (the checkpoint key) — O(1).

    Used as the :class:`~brutex.engine.sweep_checkpoint.SweepCheckpointStore`
    ``sweep_id`` so a checkpoint only ever resumes the EXACT same grid: a grid
    that differs in any axis, context field, hash, or guard hashes differently,
    so its checkpoint is correctly ignored (``load`` returns ``None``) and the
    sweep starts fresh rather than silently resuming the wrong work (P2-4 of the
    §9 hostile review). Contains NO timestamp.

    Parameters
    ----------
    grid
        The grid config.

    Returns
    -------
    str
        The 64-char lowercase BLAKE3 hex digest of the grid identity.
    """
    canonical = json.dumps(_grid_descriptor(grid), sort_keys=True, separators=(",", ":"))
    return blake3.blake3(canonical.encode("utf-8")).hexdigest()


def _tuple_cache_params(tuple_: SweepTuple, grid: SweepGridConfig) -> dict[str, Any]:
    """Return the full identity ``params`` dict for a tuple's :class:`SweepCache` key.

    Deliberately the COMPLETE tuple+grid identity — every swept axis AND the
    shared context, the reproducibility hashes, and the honest guards
    (``top_k`` / ``min_hits`` / ``max_combos``). The cache-skip path skips the
    run **entirely** with no preload-and-reflush safety net, so its key must be
    strictly identity-complete: a re-run with a changed ``top_k`` / ``min_hits`` /
    ``condition_bits`` / any knob computes a different key and is never falsely
    skipped (P0-1 of the §9 hostile review). Proximity is flattened to its three
    integer bps values (or ``None``) so equal effective proximity keys identically.

    The three Golden-Rule-#4 reproducibility hashes (``data_snapshot_hash`` /
    ``indicator_version`` / ``code_commit_sha``) are deliberately NOT in this
    dict: they are structural keyword-only parameters of
    :func:`~brutex.engine.sweep_cache.combo_digest` and of the
    :class:`~brutex.engine.sweep_cache.SweepCache` ``seen`` / ``record``
    convenience calls (DEC-COMBO-DIGEST-REPRO-HASHES-001 — the digest injects
    them internally under the SAME legacy keys, byte-identical), so a caller
    can no longer forget them and silently alias dedup entries across data
    snapshots. Every call site passes them from the grid explicitly.
    """
    proximity_bps = (
        None
        if tuple_.proximity is None
        else [
            tuple_.proximity.pivot_proximity_bps,
            tuple_.proximity.pdh_pdl_proximity_bps,
            tuple_.proximity.fib_proximity_bps,
        ]
    )
    return {
        "k": tuple_.k,
        "condition_bits": sorted(set(grid.condition_bits)),
        "tf_minutes": tuple_.tf.minutes,
        "sl_paisa": tuple_.sl_paisa,
        "tp_paisa": tuple_.tp_paisa,
        "proximity_bps": proximity_bps,
        # The swept Fib anchor is part of the tuple identity (DEC-NIFTY-FIB-AXIS-001):
        # two tuples differing ONLY by anchor evaluate DIFFERENT Fib levels, so a
        # cache-skip keyed without it would FALSELY skip the second anchor as
        # already-done (a correctness break — the cache-skip path has no
        # preload-and-reflush safety net; P0-1 of the §9 hostile review).
        "fib_anchor": tuple_.fib_anchor.value,
        "prior_n": grid.prior_n,
        "start_equity_paisa": grid.start_equity_paisa,
        "mode": grid.mode,
        "top_k": grid.top_k,
        "min_hits": grid.min_hits,
        "max_combos": grid.max_combos,
    }


def tuple_cache_key(tuple_: SweepTuple, grid: SweepGridConfig) -> bytes:
    """Return the 32-byte BLAKE3 :class:`SweepCache` key for *tuple_* under *grid* — O(1).

    The content-addressed dedup key for the tuple-level fast-skip. Built from the
    full identity (:func:`_tuple_cache_params`) via
    :func:`~brutex.engine.sweep_cache.combo_digest`, so the same logical tuple
    always maps to the same key and a different tuple never aliases it.

    Parameters
    ----------
    tuple_
        The grid point.
    grid
        The owning grid config (its context participates in the key).

    Returns
    -------
    bytes
        The raw 32-byte BLAKE3 digest.
    """
    return combo_digest(
        tuple_.k,
        tuple_.instrument,
        tuple_.tf.label,
        _direction_label(tuple_.direction),
        _tuple_cache_params(tuple_, grid),
        data_snapshot_hash=grid.data_snapshot_hash,
        indicator_version=grid.indicator_version,
        code_commit_sha=grid.code_commit_sha,
    )


def _tuple_label(tuple_: SweepTuple) -> str:
    """Return a short human label for a tuple (for audit subjects + progress)."""
    return (
        f"{tuple_.instrument}/{tuple_.tf.label}/{_direction_label(tuple_.direction)}"
        f"/k={tuple_.k}/sl={tuple_.sl_paisa}/tp={tuple_.tp_paisa}"
    )


def tuple_label(tuple_: SweepTuple) -> str:
    """PUBLIC re-export of :func:`_tuple_label` for the parallel driver — O(1).

    ONE source of truth (CLAUDE.md §11 COMMON): the parallel driver
    (:mod:`brutex.engine.sweep_parallel`, DEC-MULTICORE-SWEEP-PHASE1-001) uses this
    to emit BYTE-IDENTICAL progress/audit labels without importing the private
    ``_tuple_label``.
    """
    return _tuple_label(tuple_)


def tuple_cache_params(tuple_: SweepTuple, grid: SweepGridConfig) -> dict[str, Any]:
    """PUBLIC re-export of :func:`_tuple_cache_params` for the parallel driver — O(1).

    The full-identity ``params`` dict the :class:`SweepCache` ``seen`` / ``record``
    calls take. Exposed publicly so the parallel driver
    (DEC-MULTICORE-SWEEP-PHASE1-001) gates the cache with the BYTE-IDENTICAL key
    without importing the private ``_tuple_cache_params`` — ONE source of truth.
    """
    return _tuple_cache_params(tuple_, grid)


def _drive_watchdog(
    watchdog: SweepWatchdog,
    stop_event: threading.Event,
    interval: float,
) -> None:
    """Daemon loop body: call :meth:`SweepWatchdog.check_once` every *interval* s.

    Runs on a background thread so a hung tuple (which blocks the main sweep
    thread inside ``run_one_tuple``) is still detected — the main thread cannot
    poll itself. Exits promptly when *stop_event* is set (``Event.wait`` returns
    ``True``); otherwise (``wait`` times out → ``False``) it polls once and loops.

    Parameters
    ----------
    watchdog
        The shared :class:`SweepWatchdog`.
    stop_event
        Set by :func:`run_supervised_grid` in its ``finally`` to stop the loop.
    interval
        Poll cadence in seconds (typically ``timeout / 3``).
    """
    while not stop_event.wait(interval):
        watchdog.check_once()


def run_supervised_grid(
    grid: SweepGridConfig,
    *,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    store_for: Callable[[SweepTuple], ResultsStore],
    dedup: DedupSink,
    ingest_audit: AuditWriter,
    guards: SweepGuards,
    skip_empty: bool = True,
    on_tuple: Callable[[SweepTuple, SweepRunSummary], None] | None = None,
    on_progress: Callable[[SweepProgressSnapshot], None] | None = None,
    watchdog_poll_seconds: float | None = None,
    reuse_bar_bits: bool = True,
    roll_schedule_by_instrument: Mapping[str, tuple[int, ...]] | None = None,
) -> SupervisedGridSummary:
    """Run the grid with the full live guarantee-&-assurance layer wired in.

    Drives the deterministic :func:`~brutex.engine.sweep_orchestrator.iter_sweep_tuples`
    enumeration (strictly sequential — the audit writers are not thread-safe),
    wiring all five G&A modules around the shared
    :func:`~brutex.engine.sweep_orchestrator.run_one_tuple` body. Per tuple, in
    enumeration order with a 0-based ``index``:

    1. ``guards.watchdog.beat`` (heartbeat — the daemon detects a stall here).
    2. If ``index < resume_from`` (the loaded checkpoint high-water + 1): skip as
       **resumed**. The whole already-complete prefix is summarised ONCE before
       the loop — ONE ``TUPLE_SKIPPED_RESUME`` audit line carrying
       ``resumed_count`` + ``resume_from`` and ONE progress publish
       (DEC-SWEEP-SCALE-OPERABILITY-001) — so a restart pays O(1) audit/progress disk
       I/O instead of one fsync'd write per resumed tuple; no store touched,
       no re-record.
    3. Elif ``guards.cache.seen(tuple key)``: skip as **cached** (progress + audit
       + ``checkpoint.record``).
    4. Else ``run_one_tuple``: a :class:`~brutex.engine.sweep_run.SweepComboError`
       → **failed** skip ("Resilient: skip + continue" — progress.advance
       (processed-but-failed) + audit ``TUPLE_FAILED`` naming the offending combo,
       ``tuples_failed`` bumped, but DELIBERATELY **no** ``cache.record`` /
       ``checkpoint.record`` so a resume re-runs it; the sweep_heal "C5" lesson);
       ``None`` → **no-bars** skip (progress + audit + ``checkpoint.record``); a
       summary → accumulate, ``cache.record``, ``checkpoint.record``,
       ``progress.advance``, audit ``TUPLE_DONE``, fire ``on_tuple``.

    A non-zero ``tuples_failed`` makes the run's verdict NON-ZERO; only
    ``SweepComboError`` is caught here — every other exception still propagates to
    the abort handler below. Bracketed by ``progress.start`` + a BLAKE3
    ``SUPERVISOR_START`` line and, on
    clean exit, ``progress.finish`` + ``SUPERVISOR_COMPLETE``. On any
    :class:`Exception`: best-effort ``progress.fail`` + a ``SUPERVISOR_ABORT``
    line, then the original exception is re-raised unchanged (the secondary audit
    / progress writes are suppressed so a disk-full that triggered the abort can
    never mask the root cause — P1-5 of the §9 hostile review). A hard kill
    (``SIGKILL``) or ``KeyboardInterrupt`` leaves the abort line unwritten; the
    per-tuple checkpoint (recorded BEFORE the next tuple) is the resume guarantee,
    not the abort line. The watchdog daemon is always stopped in ``finally``.

    The caller MUST build ``guards.checkpoint`` with
    ``sweep_id == grid_identity(grid)`` and ``total_combos == count_sweep_tuples(grid)``;
    otherwise resume silently no-ops (wrong ``sweep_id`` → ``load`` returns
    ``None``) or a too-small ``total_combos`` raises loudly mid-grid (never silent
    corruption).

    Honest envelope (Golden Rule #10): every per-tuple op is O(1); the grid is
    O(T) tuples × O(Nᵢ) masks; this driver's memory is O(1) in ``T`` (scalar
    tallies only). Watchdog detection is inter-tuple only (see the module
    docstring); mask-level dedup lives in ``dedup`` (a ``DedupSink``), the cache
    is a coarser tuple-level gate.

    Parameters
    ----------
    grid
        The validated grid config.
    bars_for
        ``(instrument, tf_label) -> bars`` loader (empty ⇒ no data).
    store_for
        ``tuple -> ResultsStore`` factory (use
        :func:`~brutex.engine.sweep_orchestrator.make_store_for`).
    dedup
        The shared mask-level :class:`~brutex.engine.sweep_run.DedupSink` —
        a real :class:`~brutex.engine.sweep_run.ComboDedup` on the legacy
        no-digest path, or the launch planner's no-alloc raising
        :class:`~brutex.engine.sweep_launch.DigestPathNoDedup` on the
        production digest path (never consulted there — ``bypass_dedup``).
    ingest_audit
        The per-tuple engine :class:`~brutex.orchestrator.audit.AuditWriter`
        (the ``SWEEP_*`` chain ``run_persisted_sweep`` emits; distinct from the
        supervisor's BLAKE3 chain in ``guards.audit``).
    guards
        The five live G&A objects (:class:`SweepGuards`).
    skip_empty
        When ``True`` (default), a no-bars tuple is skipped + counted; when
        ``False``, ``run_one_tuple`` raises (caught → abort → re-raised).
    on_tuple
        Optional hook fired ``(tuple, summary)`` after each tuple that ran.
    on_progress
        Optional hook fired with the fresh
        :class:`~brutex.engine.sweep_progress.SweepProgressSnapshot` after every
        progress mutation — the streaming seam for a live dashboard publish.
    watchdog_poll_seconds
        When set, a daemon thread polls :meth:`SweepWatchdog.check_once` on this
        cadence (active inter-tuple hang detection); when ``None`` (default), the
        heartbeat is recorded but no active detection runs.
    reuse_bar_bits
        When ``True`` (default), reuse one Stage-1A bar window per distinct
        ``(instrument, tf, proximity, prior_n)`` across its tuples via a bounded
        :class:`~brutex.engine.sweep_kernel.ResidentKernelCache`
        (DEC-SWEEP-RESIDENT-KERNEL-001) — byte-identical results, only the
        redundant rebuild is skipped; ``False`` rebuilds per tuple.
    roll_schedule_by_instrument
        ADR-0012 futures roll schedules keyed by instrument symbol
        (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001) — argument-plumbed straight into
        :func:`~brutex.engine.sweep_orchestrator.run_one_tuple` (identity-safe;
        see :func:`~brutex.engine.sweep_orchestrator.run_sweep_grid`). ``None``
        (the default) is the byte-identical spot path.

    Returns
    -------
    SupervisedGridSummary
        The aggregate tally across the grid (this run's run/skip counts).

    Raises
    ------
    ValueError
        If ``skip_empty=False`` and a tuple has no bars (after an abort line).
    """
    sweep_id = grid_identity(grid)
    total = count_sweep_tuples(grid)
    subject = f"sweep:{sweep_id[:16]}"

    guards.checkpoint.load()
    resume_from = guards.checkpoint.next_index

    guards.progress.start(enumerated=total, detail=subject)
    guards.audit.append(
        action=_ACTION_START,
        subject=subject,
        payload={
            "sweep_id": sweep_id,
            "tuples_total": total,
            "resume_from": resume_from,
            "instruments": list(grid.instruments),
            "timeframes": [tf.label for tf in grid.timeframes],
            "directions": [d.value for d in grid.directions],
            "k_values": list(grid.k_values),
            "mode": grid.mode,
            "data_snapshot_hash": grid.data_snapshot_hash,
            "indicator_version": grid.indicator_version,
            "code_commit_sha": grid.code_commit_sha,
        },
    )

    tuples_run = 0
    tuples_skipped_no_bars = 0
    skipped_cache = 0
    # Batched resume emission (DEC-SWEEP-SCALE-OPERABILITY-001): the already-complete
    # prefix ``[0, resume_from)`` is accounted ONCE up front — one summarising
    # audit line + one progress publish inside the ``try`` below — instead of
    # one flushed audit append + one fsync'd atomic progress write PER resumed
    # tuple (O(resumed) disk I/O per restart). The ``min`` clamp keeps the
    # count honest against an oversized checkpoint cursor (never more than the
    # grid holds); ``skipped_resume`` keeps its exact prior meaning.
    resume_count = min(resume_from, total)
    skipped_resume = resume_count
    tuples_failed = 0
    enumerated = 0
    conflict_pruned = 0
    skipped_dedup = 0
    evaluated = 0
    ranked = 0
    persisted = 0
    # The monotonic checkpoint is a CONTIGUOUS-CLEAN-PREFIX mark: it must only ever
    # advance past tuples that ALL succeeded. Once ANY tuple fails this run we stop
    # recording the checkpoint, freezing it at the last clean index BEFORE the first
    # failure so a resume re-runs the failed tuple (the sweep_heal "C5" lesson +
    # Golden Rules #4/#7 — a failed tuple must never be permanently skipped). Tuples
    # that succeeded AFTER the failure are short-circuited on resume by the CACHE tier
    # (guards.cache.record still runs on success), not re-evaluated, so resume stays cheap.
    had_failure = False

    # Bounded resident-kernel cache (DEC-SWEEP-RESIDENT-KERNEL-001): reuse one
    # Stage-1A bar window per distinct (instrument, tf, proximity, prior_n) across
    # its direction/K/sl/tp tuples; None disables reuse (legacy per-tuple rebuild).
    kernel_cache = ResidentKernelCache(capacity=len(grid.proximities)) if reuse_bar_bits else None

    stop_event = threading.Event()
    wd_thread: threading.Thread | None = None
    if watchdog_poll_seconds is not None:
        wd_thread = threading.Thread(
            target=_drive_watchdog,
            args=(guards.watchdog, stop_event, watchdog_poll_seconds),
            name="sweep-watchdog",
            daemon=True,
        )
        wd_thread.start()

    def _advance(*, current: str, survived: bool, skipped: bool) -> None:
        """Tick progress once + stream the fresh snapshot to *on_progress*."""
        guards.progress.advance(current=current, survived=survived, skipped=skipped)
        if on_progress is not None:
            on_progress(guards.progress.snapshot())

    try:
        if resume_count > 0:
            # ONE in-memory progress batch for the whole resumed prefix (the
            # ``advance`` calls only mutate counters under a lock — zero disk
            # I/O; the single ``on_progress`` publish below is the only write),
            # then ONE summarising hash-chained audit line. The chain stays
            # verifiable — the summary is an ordinary seq/prev_hash/entry_hash
            # entry — and its payload reconstructs the skipped set exactly:
            # the enumeration indices ``[0, resumed_count)`` (Golden Rule #7).
            for _ in range(resume_count):
                guards.progress.advance(survived=False, skipped=True)
            if on_progress is not None:
                on_progress(guards.progress.snapshot())
            guards.audit.append(
                action=_ACTION_TUPLE_RESUME,
                subject=subject,
                payload={"resumed_count": resume_count, "resume_from": resume_from},
            )
            _LOG.info(
                "sweep_supervisor.resume_batch",
                subject=subject,
                resumed_count=resume_count,
                resume_from=resume_from,
            )
        for index, tuple_ in enumerate(iter_sweep_tuples(grid)):
            guards.watchdog.beat(guards.worker_name)

            if index < resume_from:
                # Already-complete resumed prefix: counted + summarised ONCE
                # above (DEC-SWEEP-SCALE-OPERABILITY-001) — zero per-tuple audit /
                # progress / label work here. The generator is still stepped
                # sequentially (unavoidable) but with no disk I/O.
                continue

            label = _tuple_label(tuple_)

            if guards.cache.seen(
                tuple_.k,
                tuple_.instrument,
                tuple_.tf.label,
                _direction_label(tuple_.direction),
                _tuple_cache_params(tuple_, grid),
                data_snapshot_hash=grid.data_snapshot_hash,
                indicator_version=grid.indicator_version,
                code_commit_sha=grid.code_commit_sha,
            ):
                skipped_cache += 1
                if not had_failure:
                    guards.checkpoint.record(index)
                _advance(current=label, survived=False, skipped=True)
                guards.audit.append(
                    action=_ACTION_TUPLE_CACHE,
                    subject=subject,
                    payload={"index": index, "tuple": label},
                )
                continue

            # "Resilient: skip + continue" — a combo that raises mid-evaluation
            # surfaces as a SweepComboError. Skip the WHOLE tuple, count it,
            # audit the fingerprint, advance progress (processed-but-failed), and
            # continue to the remaining tuples. CRITICALLY, the cache + checkpoint
            # are DELIBERATELY NOT recorded for a failed tuple, so a resume/cache
            # re-runs it (the sweep_heal "C5" lesson — a failed tuple must never
            # be permanently skipped). Only SweepComboError is caught; the outer
            # `except Exception` (record-then-reraise ABORT) still handles every
            # genuinely-fatal error.
            try:
                summary = run_one_tuple(
                    grid,
                    tuple_,
                    bars_for=bars_for,
                    store_for=store_for,
                    dedup=dedup,
                    audit=ingest_audit,
                    skip_empty=skip_empty,
                    kernel_cache=kernel_cache,
                    roll_schedule_by_instrument=roll_schedule_by_instrument,
                )
            except SweepComboError as exc:
                tuples_failed += 1
                had_failure = True
                _advance(current=label, survived=False, skipped=False)
                guards.audit.append(
                    action=_ACTION_TUPLE_FAILED,
                    subject=subject,
                    payload={
                        "index": index,
                        "tuple": label,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                _LOG.warning(
                    "sweep_supervisor.tuple_failed",
                    subject=subject,
                    index=index,
                    tuple=label,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if summary is None:
                tuples_skipped_no_bars += 1
                if not had_failure:
                    guards.checkpoint.record(index)
                _advance(current=label, survived=False, skipped=True)
                guards.audit.append(
                    action=_ACTION_TUPLE_NO_BARS,
                    subject=subject,
                    payload={"index": index, "tuple": label},
                )
                continue

            tuples_run += 1
            enumerated += summary.enumerated
            conflict_pruned += summary.conflict_pruned
            skipped_dedup += summary.skipped_dedup
            evaluated += summary.evaluated
            ranked += summary.ranked
            persisted += summary.persisted

            guards.cache.record(
                tuple_.k,
                tuple_.instrument,
                tuple_.tf.label,
                _direction_label(tuple_.direction),
                _tuple_cache_params(tuple_, grid),
                data_snapshot_hash=grid.data_snapshot_hash,
                indicator_version=grid.indicator_version,
                code_commit_sha=grid.code_commit_sha,
            )
            if not had_failure:
                guards.checkpoint.record(index)
            _advance(current=label, survived=summary.persisted > 0, skipped=False)
            guards.audit.append(
                action=_ACTION_TUPLE_DONE,
                subject=subject,
                payload={
                    "index": index,
                    "tuple": label,
                    "enumerated": summary.enumerated,
                    "conflict_pruned": summary.conflict_pruned,
                    "skipped_dedup": summary.skipped_dedup,
                    "evaluated": summary.evaluated,
                    "ranked": summary.ranked,
                    "persisted": summary.persisted,
                    "store_content_hash": summary.store_content_hash,
                },
            )
            if on_tuple is not None:
                on_tuple(tuple_, summary)

        payload = {
            "sweep_id": sweep_id,
            "tuples_total": total,
            "tuples_run": tuples_run,
            "tuples_skipped_no_bars": tuples_skipped_no_bars,
            "skipped_cache": skipped_cache,
            "skipped_resume": skipped_resume,
            "tuples_failed": tuples_failed,
            "enumerated": enumerated,
            "conflict_pruned": conflict_pruned,
            "skipped_dedup": skipped_dedup,
            "evaluated": evaluated,
            "ranked": ranked,
            "persisted": persisted,
        }
        guards.progress.finish(
            detail=(
                f"{tuples_run} run · {skipped_cache} cached · "
                f"{skipped_resume} resumed · {tuples_failed} failed"
            )
        )
        if on_progress is not None:
            on_progress(guards.progress.snapshot())
        guards.audit.append(action=_ACTION_COMPLETE, subject=subject, payload=payload)
        _LOG.info("sweep_supervisor.complete", **payload)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Record-then-re-raise EVERY in-band failure; the bare `raise` preserves
        # it unchanged. The secondary progress/audit writes are suppressed so a
        # disk-full that triggered the abort can never mask the root cause.
        detail = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            guards.progress.fail(detail)
        with contextlib.suppress(Exception):
            guards.audit.append(action=_ACTION_ABORT, subject=subject, payload={"error": detail})
        raise
    finally:
        stop_event.set()
        if wd_thread is not None:
            wd_thread.join(timeout=5.0)

    return SupervisedGridSummary(
        sweep_id=sweep_id,
        tuples_total=total,
        tuples_run=tuples_run,
        tuples_skipped_no_bars=tuples_skipped_no_bars,
        skipped_cache=skipped_cache,
        skipped_resume=skipped_resume,
        enumerated=enumerated,
        conflict_pruned=conflict_pruned,
        skipped_dedup=skipped_dedup,
        evaluated=evaluated,
        ranked=ranked,
        persisted=persisted,
        tuples_failed=tuples_failed,
    )


def publish_progress(path: Path, snapshot: SweepProgressSnapshot) -> None:
    """Atomically write *snapshot* to *path* as JSON — the dashboard file bridge.

    The cross-process seam: a CLI-launched ``brutex sweep-all`` wires this as its
    ``on_progress`` hook so a SEPARATE dashboard process can serve live progress
    at ``/api/v1/sweep-progress`` by reading *path*. The write is atomic
    (tempfile → ``os.replace``, via the proven
    :func:`brutex.engine.sweep_checkpoint._atomic_write`) so the reader never sees
    a half-written file.

    Parameters
    ----------
    path
        Destination JSON file (parents created).
    snapshot
        The progress snapshot to publish.
    """
    from brutex.engine.sweep_checkpoint import _atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(snapshot.to_dict(), sort_keys=True).encode("utf-8")
    _atomic_write(path, content)


def read_progress(path: Path) -> dict[str, Any]:
    """Return the progress dict written by :func:`publish_progress`, or the idle view.

    Best-effort + crash-tolerant: a missing file, an OS read error, or a
    half-written / malformed JSON (a writer mid-``os.replace`` is atomic, but a
    truncated leftover is still defended) all fall back to the idle snapshot so a
    dashboard poll never raises.

    Parameters
    ----------
    path
        The progress JSON file (as written by :func:`publish_progress`).

    Returns
    -------
    dict[str, Any]
        The parsed snapshot dict, or a copy of the idle snapshot.
    """
    try:
        parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_IDLE_PROGRESS)
    return parsed


def idle_progress() -> dict[str, Any]:
    """Return a copy of the idle progress snapshot (the pre-sweep dashboard view)."""
    return dict(_IDLE_PROGRESS)


__all__ = [
    "DEFAULT_WORKER_NAME",
    "SUPERVISOR_ACTION_ABORT",
    "SUPERVISOR_ACTION_COMPLETE",
    "SUPERVISOR_ACTION_START",
    "SUPERVISOR_ACTION_TUPLE_CACHE",
    "SUPERVISOR_ACTION_TUPLE_DONE",
    "SUPERVISOR_ACTION_TUPLE_FAILED",
    "SUPERVISOR_ACTION_TUPLE_NO_BARS",
    "SUPERVISOR_ACTION_TUPLE_RESUME",
    "SupervisedGridSummary",
    "SweepGuards",
    "grid_identity",
    "idle_progress",
    "publish_progress",
    "read_progress",
    "run_supervised_grid",
    "tuple_cache_key",
    "tuple_cache_params",
    "tuple_label",
]
