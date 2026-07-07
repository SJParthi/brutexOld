"""Multi-core grid-sweep driver — Phase 1 (DEC-MULTICORE-SWEEP-PHASE1-001).

The FIRST safe, tested slice of multi-core parallelism for the brute-force grid
sweep. Originally shipped behind a DEFAULT-OFF worker-count flag; since
ADR-0016 (DEC-MULTICORE-SWEEP-AUTODEFAULT-001 — the deferred default-raise of
MIGRATION.md #1080) an unset ``BRUTEX_SWEEP_WORKERS`` resolves to an AUTO
worker count (``max(1, min(cpu_count - 2, 8))``, small grids stay sequential;
``BRUTEX_SWEEP_WORKERS=1`` is the explicit sequential opt-out) — see
:func:`resolve_sweep_workers`. The per-tuple engine path is UNCHANGED; the
sequential==parallel byte-identity goldens remain the correctness net.

## Why processes, not threads (the GIL, honestly)

The dominant per-tuple cost is **CPU-bound and GIL-holding** — ``build_bar_bits``
(a Python bar walk), the per-mask metrics tail (pure Python:
``metrics_from_trades`` = equity fold + ``compute_metrics``), and — since
DEC-SWEEP-RUST-TRADESIM-WIRE-001 — the resident Rust trade-walk
(:class:`~brutex.engine.rust_trade_sim.ResidentTradeSim`), whose PyO3 batch call
runs WITHOUT releasing the GIL. A ``ThreadPoolExecutor`` would serialise on the
GIL → **~1×** on compute. So parallelism here is a
:class:`concurrent.futures.ProcessPoolExecutor` of ``workers - 1`` EVAL workers
(each in its own address space, own kernel LRU, own per-tuple resident
trade-sim). Threads are USELESS here; processes are MANDATORY.

## Honest speedup envelope (Golden Rule #10)

On the CLAUDE.md §8 reference hardware — **Apple M4 Pro, 14-core (10P+4E),
48 GB RAM** — the realistic ceiling on a compute-heavy grid is **~6–8×**, NOT
10×: the single serialising writer-actor's per-tuple ``fsync``, the reorder
overhead, and inter-process result marshalling eat ~20–40%. A grid with fewer
tuples than cores, or one dominated by a single huge-``N_bars`` instrument, sees
far less — **no number of cores speeds up ONE tuple's latency**: its trade walk
is the resident Rust kernel, but ``_worker_init`` below pins
``RAYON_NUM_THREADS=1`` in every worker (bit-for-bit determinism + no
``workers × num_cpus`` thread oversubscription), so the kernel's rayon
parallel arm applies ONLY to single-process runs — under this pool the per-tuple
speedup is the SERIAL-kernel ratio, and the per-mask metrics tail stays pure
Python either way (the Phase-A honest envelope: ~2.2–2.8× end-to-end per tuple,
Amdahl-capped by that tail). Parallelism is strictly ACROSS independent tuples.
No "millions in minutes."

## The shape (a mirror of ``adapters/groww/fetch_pipeline.py``)

```
  parent thread  ──►  WRITER-ACTOR (this driver, ONE thread)
   (the driver)        • enumerates iter_sweep_tuples(grid) in canonical order
                       • checkpoint resume-gate + coarse SweepCache gate (parent-only)
                       • dispatches (index, tuple, bars) to the pool
                       • drains results through a reorder MIN-HEAP keyed on `index`
                       • applies ALL ordered state in enumeration order:
                           – BOTH audit chains (ingest SWEEP_* + supervisor TUPLE_*)
                           – checkpoint.record via a CONTIGUOUS-CLEAN-PREFIX pointer
                           – SweepCache.record + progress.advance
   ProcessPoolExecutor of `workers-1` EVAL WORKERS (own address space):
     run_one_tuple(...) → run_persisted_sweep body (UNCHANGED) →
       build_bar_bits → ResidentTradeSim.simulate_masks (chunked; serial kernel
       under the RAYON_NUM_THREADS=1 pin) → metrics_from_trades → rank_combos →
       per-tuple Parquet + completion sentinel; audit lines collected as PLAIN
       DATA (BufferedAuditCollector)
```

## Bounded memory under head-of-line blocking (Golden Rule #5)

The reorder heap holds a completed tuple's ``_Outcome`` (its ``SweepRunSummary`` +
buffered audit) until the contiguous-clean ``next_apply`` prefix reaches it. If the
HEAD tuple is legitimately slow while its siblings complete, those sibling outcomes
would pile into the heap — O(T − next_apply), unbounded on a multi-million-tuple
sweep. So DISPATCH is COUPLED to APPLY-progress: no new tuple is dispatched while
the heap is at its cap (:data:`_HEAP_CAP_MULTIPLIER` × pool_size). The heap is thus
GENUINELY BOUNDED to O(pool_size) — the honest tradeoff is that a blocked head
throttles throughput (bounded memory > max throughput). The STALL clock measures
time-since-last-COMPLETION (bumped on ANY future resolving), NOT time-since-last-
APPLY, so a slow-but-progressing head with completing siblings never spuriously
raises :class:`SweepStallError` — only a genuine no-completion hang does.

## Scope (Golden Rule #3) — digest path ONLY

This driver is scoped to the **production digest path** (a tuple digest is always
present → ``bypass_dedup`` is always ``True`` in ``run_persisted_sweep``, so no
cross-worker dedup race exists on the hot path — the completion sentinel is the
sole redo authority). ``workers > 1`` is **hard-refused** on the legacy no-digest
path (where cross-worker survivor filtering would diverge). Because
:func:`~brutex.engine.sweep_orchestrator.run_one_tuple` ALWAYS passes a digest,
the production path is the digest path; the refusal is a belt-and-braces guard on
the flag, not a real production limitation.

## What is DEFERRED to follow-up PRs (honest, not shipped here)

* **Block-partition kernel locality** — each worker rebuilds its resident kernel
  per distinct ``(instrument, tf, proximity, prior_n, fib_anchor)``; contiguous
  block dispatch to preserve the DEC-SWEEP-RESIDENT-KERNEL-001 reuse win is
  future work. This slice dispatches per-tuple (correct, just not locality-tuned).
* **Resident-kernel reuse across the pool / a persistent worker kernel** — a
  follow-up.
* ~~Raising the default worker count above 1~~ — DONE per ADR-0016
  (DEC-MULTICORE-SWEEP-AUTODEFAULT-001): an unset ``BRUTEX_SWEEP_WORKERS`` now
  resolves to the AUTO count ``max(1, min(cpu_count - 2, 8))`` with a
  small-grid sequential fallback; ``BRUTEX_SWEEP_WORKERS=1`` stays the explicit
  sequential opt-out. See :func:`resolve_sweep_workers`.
* **The stall watchdog RAISES rather than warns** — a wedged worker's future never
  completes, so unlike ``fetch_pipeline`` (which only WARNs) this driver surfaces
  a :class:`SweepStallError` so a sweep can never hang forever.
"""

from __future__ import annotations

import contextlib
import functools
import heapq
import multiprocessing
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED as _FIRST_COMPLETED
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures import wait as _futures_wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, cast

import structlog

from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    count_sweep_tuples,
    iter_sweep_tuples,
    make_store_for,
    run_one_tuple,
)
from brutex.engine.sweep_run import (
    SweepComboError,
    SweepRunSummary,
    direction_label,
)
from brutex.engine.sweep_supervisor import (
    SUPERVISOR_ACTION_ABORT,
    SUPERVISOR_ACTION_COMPLETE,
    SUPERVISOR_ACTION_START,
    SUPERVISOR_ACTION_TUPLE_CACHE,
    SUPERVISOR_ACTION_TUPLE_DONE,
    SUPERVISOR_ACTION_TUPLE_FAILED,
    SUPERVISOR_ACTION_TUPLE_NO_BARS,
    SUPERVISOR_ACTION_TUPLE_RESUME,
    SupervisedGridSummary,
    SweepGuards,
    grid_identity,
    tuple_cache_params,
    tuple_label,
)
from brutex.models import BruteXError
from brutex.orchestrator.audit import BufferedAuditCollector, BufferedAuditRecord

if TYPE_CHECKING:
    from pathlib import Path

    from brutex.data.models import CandleBar
    from brutex.engine.reference_series import TradeReferenceStamper

_LOG = structlog.get_logger("brutex.engine.sweep_parallel")

# ────────────────────────────────────────────────────────────────────────
# Config flag — AUTO-default (ADR-0016, DEC-MULTICORE-SWEEP-AUTODEFAULT-001)
# ────────────────────────────────────────────────────────────────────────

#: Environment variable naming the sweep worker count.
SWEEP_WORKERS_ENV: Final[str] = "BRUTEX_SWEEP_WORKERS"

#: The SEQUENTIAL worker count — the explicit ``BRUTEX_SWEEP_WORKERS=1``
#: opt-out AND the small-grid fallback. ``1`` = the EXISTING sequential path,
#: byte-for-byte (``run_supervised_grid`` is called unchanged; this driver is
#: never entered). This is no longer the unconditional DEFAULT: since ADR-0016
#: (DEC-MULTICORE-SWEEP-AUTODEFAULT-001 — the deferred-raise of
#: DEC-MULTICORE-SWEEP-PHASE1-001, MIGRATION.md #1080) an UNSET env resolves
#: to the AUTO count (:func:`auto_sweep_workers`).
SEQUENTIAL_SWEEP_WORKERS: Final[int] = 1

#: The AUTO worker-count CAP. 8 total workers = 1 writer-actor + 7 EVAL
#: processes — inside the CLAUDE.md §8 "10 cores used by sweep, 2 reserved"
#: envelope on the M4 Pro reference machine, and below the point where the
#: single writer-actor's per-tuple ``fsync`` is projected to dominate (the
#: module-docstring Amdahl note). 8-worker THROUGHPUT is NOT benched — the
#: cap is a conservative operator-authorized ceiling, not a measured optimum
#: (ADR-0016 §5).
AUTO_SWEEP_WORKERS_CAP: Final[int] = 8

#: CPUs the AUTO count RESERVES for the rest of the machine (the CLAUDE.md §8
#: "2 reserved" cores — daily apps run concurrently on the reference M4 Pro).
AUTO_SWEEP_WORKERS_RESERVED_CPUS: Final[int] = 2

#: Small-grid factor DEFAULT: a sweep with ``tuple_count < factor × auto``
#: resolves to 1 (sequential) on the AUTO path — pool spawn + result
#: marshalling overhead is not worth paying when there are fewer than ~2
#: tuples per prospective worker (an explicit env override is never demoted).
#: HONEST caveat (ADR-0016 §5): the demotion keys on tuple COUNT, not per-tuple
#: WORK — a few-but-huge-tuple grid is demoted too, so the factor is
#: env-tunable via :data:`SMALL_GRID_FACTOR_ENV` (``0`` disables the demotion).
SMALL_GRID_FACTOR: Final[int] = 2

#: Environment variable overriding :data:`SMALL_GRID_FACTOR` (the operator
#: lever for a compute-heavy SMALL grid — e.g. 15 multi-year tuples on a
#: 14-core box — which the count-keyed heuristic would otherwise demote to one
#: core). ``0`` disables the small-grid demotion entirely; unset/blank keeps
#: the default; an invalid value (non-integer / negative) falls back to the
#: default and is logged LOUDLY at WARNING.
SMALL_GRID_FACTOR_ENV: Final[str] = "BRUTEX_SWEEP_SMALL_GRID_FACTOR"


def small_grid_factor(env: dict[str, str] | None = None) -> int:
    """Resolve the small-grid demotion factor — env-tunable (ADR-0016 §5).

    Reads :data:`SMALL_GRID_FACTOR_ENV` from *env* (default
    :data:`os.environ`): unset/blank → :data:`SMALL_GRID_FACTOR`; a valid
    integer ``>= 0`` → that value verbatim (``0`` DISABLES the small-grid
    demotion — ``tuple_count < 0`` is never true); an INVALID value
    (non-integer / negative) → the default, logged at WARNING on the
    ``sweep.small_grid_factor.invalid`` event naming the value via ``repr``
    (mirrors the :func:`resolve_sweep_workers` invalid-env loudness). O(1).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable for
        tests.

    Returns
    -------
    int
        The demotion factor (``>= 0``).
    """
    source = os.environ if env is None else env
    raw = source.get(SMALL_GRID_FACTOR_ENV)
    if raw is None or not raw.strip():
        return SMALL_GRID_FACTOR
    try:
        parsed = int(raw)
    except ValueError:
        parsed = -1  # sentinel: falls through to the invalid branch below.
    if parsed < 0:
        _LOG.warning(
            "sweep.small_grid_factor.invalid",
            requested_env=repr(raw),
            fallback=SMALL_GRID_FACTOR,
        )
        return SMALL_GRID_FACTOR
    return parsed


def auto_sweep_workers(cpu_count: int | None) -> int:
    """The AUTO sweep worker count for *cpu_count* CPUs — pure + injectable.

    ``max(1, min(cpu_count - 2, 8))`` per ADR-0016
    (DEC-MULTICORE-SWEEP-AUTODEFAULT-001): reserve
    :data:`AUTO_SWEEP_WORKERS_RESERVED_CPUS` cores for the rest of the machine,
    cap at :data:`AUTO_SWEEP_WORKERS_CAP`, and never go below the sequential
    floor of 1. A ``None`` *cpu_count* (``os.cpu_count()`` may return ``None``
    on exotic platforms) is treated as 1 → auto = 1 (sequential — never guess
    parallelism on an undetectable machine). O(1).

    Since DEC-HARDWARE-ENVELOPE-001 this DELEGATES to the COMMON
    :func:`brutex.config.hardware_envelope.cpu_pool_workers` (``reserve=2``,
    ``cap=8``, sized off the logical count with ``prefer_performance=False`` and
    an empty env so no override applies) — the ONE sizing formula (CLAUDE.md §11)
    — with a byte-unchanged result:
    ``min(max(1, n - 2), 8) == max(1, min(n - 2, 8))`` for the cap ``8 >= 1``.

    Parameters
    ----------
    cpu_count
        The detected CPU count, or ``None`` when undetectable.

    Returns
    -------
    int
        The auto worker count (``>= 1``).
    """
    from brutex.config.hardware_envelope import cpu_pool_workers, envelope_from

    logical = cpu_count if cpu_count is not None and cpu_count >= 1 else 1
    return cpu_pool_workers(
        {},
        reserve=AUTO_SWEEP_WORKERS_RESERVED_CPUS,
        cap=AUTO_SWEEP_WORKERS_CAP,
        prefer_performance=False,
        envelope=envelope_from({"logical_cores": logical}),
    )


#: The bounded in-flight submission window, as a multiple of the pool size — caps
#: outstanding FUTURES (``pending``) so the fan-out never over-submits (mirrors the
#: ``fetch_pipeline`` back-pressure design). This alone does NOT bound the reorder
#: heap under head-of-line blocking — see :data:`_HEAP_CAP_MULTIPLIER`.
_INFLIGHT_MULTIPLIER: Final[int] = 2

#: The reorder-heap cap, as a multiple of the pool size. Under head-of-line
#: blocking (a legitimately-slow head tuple while siblings complete) the completed
#: siblings' ``_Outcome`` objects accumulate in the heap; without a cap that is
#: O(T − next_apply) — unbounded on a multi-million-tuple sweep. DISPATCH is
#: therefore COUPLED to APPLY-progress: no new tuple is dispatched while
#: ``len(heap) >= pool_size * _HEAP_CAP_MULTIPLIER``, so the heap is GENUINELY
#: bounded (the honest tradeoff: a blocked head throttles throughput, but bounded
#: memory > max throughput). The heap can transiently overshoot the cap by the
#: futures already in flight when dispatch pauses, so the true ceiling is
#: ``pool_size * (_HEAP_CAP_MULTIPLIER + _INFLIGHT_MULTIPLIER)`` — still O(pool_size),
#: independent of T.
_HEAP_CAP_MULTIPLIER: Final[int] = 2

#: How long (seconds) the parent will wait for the NEXT result before firing the
#: stall watchdog, when work is in flight. A wedged worker's future never resolves,
#: so — unlike ``fetch_pipeline`` (which only WARNs) — this RAISES so a sweep can
#: never hang forever.
DEFAULT_STALL_SECONDS: Final[float] = 300.0

#: The minimum worker count the parallel driver accepts (one core is the
#: writer-actor, so a pool needs at least one EVAL worker). ``1`` means "run the
#: sequential path" and is dispatched by the caller, never by this driver.
_MIN_PARALLEL_WORKERS: Final[int] = 2


def resolve_sweep_workers(
    env: dict[str, str] | None = None,
    *,
    tuple_count: int | None = None,
    cpu_count_fn: Callable[[], int | None] | None = None,
) -> int:
    """Resolve the sweep worker count — AUTO-default multicore (ADR-0016).

    DEC-MULTICORE-SWEEP-AUTODEFAULT-001 (the deferred default-raise of
    DEC-MULTICORE-SWEEP-PHASE1-001, MIGRATION.md #1080). Reads
    :data:`SWEEP_WORKERS_ENV` and resolves:

    * **unset / blank** → the AUTO count :func:`auto_sweep_workers`
      (``max(1, min(cpu_count - 2, 8))``; ``os.cpu_count() is None`` → 1).
    * **explicit valid env ``>= 1``** → that value verbatim (an env override
      beats auto; ``=1`` stays the sequential opt-out, never demoted, never
      promoted).
    * **INVALID env (non-integer / ``< 1``)** → the AUTO count. NOTE: this
      consciously REVERSES the prior documented invariant "a bad value can
      never silently switch on parallelism" (a bad value used to fall back to
      the sequential 1). The flip is owned by ADR-0016 §4 (operator-authorized:
      once auto IS the default, degrading a typo to sequential would silently
      throw away the machine — the opposite illusion); it is NOT silent — the
      invalid value is logged LOUDLY at WARNING level on the
      ``sweep.workers.resolved`` event.
    * **small grid** — on the AUTO path only, when *tuple_count* is known and
      ``tuple_count < factor × auto`` (and ``auto > 1``), resolve to 1
      (sequential): pool spawn + marshalling overhead is not worth fewer than
      ~2 tuples per worker. The factor is :func:`small_grid_factor` — the
      :data:`SMALL_GRID_FACTOR` default (2), env-tunable via
      :data:`SMALL_GRID_FACTOR_ENV` (``0`` disables the demotion — the
      operator lever for a few-but-huge-tuple grid, ADR-0016 §5).

    Emits ONE ``sweep.workers.resolved`` structlog event per call
    (WARNING when the env value was invalid, else INFO) carrying
    ``requested_env`` (repr) / ``env_invalid`` (bool — filterable even when
    the small-grid fallback takes the ``reason`` slot) / ``cpu_count`` /
    ``auto`` / ``tuple_count`` / ``resolved`` / ``reason`` — ``reason`` is one
    of ``env_explicit`` / ``env_invalid_auto`` / ``auto`` /
    ``small_grid_sequential`` / ``cpu_count_none``. O(1).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable for
        tests.
    tuple_count
        The sweep's total tuple count when the caller knows it (the CLI
        launcher passes ``count_sweep_tuples(grid)``); ``None`` = unknown →
        the small-grid fallback is skipped.
    cpu_count_fn
        CPU-count probe (defaults to :func:`os.cpu_count`, resolved at CALL
        time so a test monkeypatch of ``os.cpu_count`` is honored) —
        injectable for tests.

    Returns
    -------
    int
        The resolved worker count (``>= 1``). ``1`` means "run sequentially".
    """
    source = os.environ if env is None else env
    raw = source.get(SWEEP_WORKERS_ENV)
    probe = os.cpu_count if cpu_count_fn is None else cpu_count_fn
    cpu = probe()
    auto = auto_sweep_workers(cpu)

    explicit: int | None = None
    invalid = False
    if raw is not None and raw.strip():
        try:
            parsed = int(raw)
        except ValueError:
            invalid = True
        else:
            if parsed < 1:
                invalid = True
            else:
                explicit = parsed

    factor = small_grid_factor(env)  # None → os.environ, same as `source`.
    if explicit is not None:
        resolved = explicit
        reason = "env_explicit"
    elif auto > 1 and tuple_count is not None and tuple_count < factor * auto:
        resolved = SEQUENTIAL_SWEEP_WORKERS
        reason = "small_grid_sequential"
    elif invalid:
        # The ADR-0016 §4 conscious reversal — see the docstring note above.
        resolved = auto
        reason = "env_invalid_auto"
    elif cpu is None:
        resolved = auto  # auto_sweep_workers(None) == 1 — sequential.
        reason = "cpu_count_none"
    else:
        resolved = auto
        reason = "auto"

    log = _LOG.warning if invalid else _LOG.info
    log(
        "sweep.workers.resolved",
        requested_env=repr(raw),
        env_invalid=invalid,
        cpu_count=cpu,
        auto=auto,
        tuple_count=tuple_count,
        resolved=resolved,
        reason=reason,
    )
    return resolved


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class SweepStallError(BruteXError):
    """Raised when the parallel sweep makes no progress for the stall interval.

    A wedged worker's future never resolves, so — to guarantee the sweep can
    NEVER hang forever — the parent's writer-actor raises this when work is in
    flight but no result has arrived for :data:`DEFAULT_STALL_SECONDS`. The pool
    is torn down (``ProcessPoolExecutor.shutdown``) before this propagates.
    """


class SweepParallelUsageError(BruteXError):
    """Raised when the parallel driver is asked to run in an unsupported mode.

    Specifically: ``workers < 2`` (the caller must dispatch the sequential path
    itself), or a request to run ``workers > 1`` on the legacy no-digest path
    (hard-refused — see the module docstring's scope note).
    """


def refuse_parallel_on_legacy_path(workers: int, *, digest_path: bool) -> None:
    """Hard-refuse ``workers > 1`` on the legacy no-digest sweep path.

    The parallel driver is SOUND only on the PRODUCTION digest path
    (DEC-MULTICORE-SWEEP-PHASE1-001): there ``run_persisted_sweep`` bypasses dedup
    (``bypass_dedup = trial_stats_digest is not None``), so which worker runs which
    tuple is irrelevant to the file set. On the LEGACY no-digest path a worker
    without the shared dedup would rank an UNFILTERED survivor set → bit-divergent
    Parquet — so parallelism there is a correctness defect and is HARD-REFUSED with
    a clear error, never silently downgraded.

    Parameters
    ----------
    workers
        The requested worker count.
    digest_path
        ``True`` iff the caller runs the digest path (a tuple digest is always
        supplied — the production ``run_supervised_grid`` / ``run_parallel_grid``
        path). The legacy ``run_sweep_grid`` no-digest path passes ``False``.

    Raises
    ------
    SweepParallelUsageError
        If ``workers > 1`` and ``digest_path`` is ``False``.
    """
    if workers > 1 and not digest_path:
        raise SweepParallelUsageError(
            f"parallel sweep (workers={workers}) is refused on the LEGACY no-digest "
            "path: cross-worker survivor filtering would diverge (a worker has no "
            "shared dedup). Use workers=1 on this path, or the digest path "
            "(run_supervised_grid / run_parallel_grid)."
        )


# ────────────────────────────────────────────────────────────────────────
# Worker payloads (all picklable — cross the process boundary)
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TupleWorkResult:
    """The picklable result a worker returns for ONE dispatched tuple.

    Exactly one of :attr:`summary` (success or no-bars) / :attr:`failed_error`
    (a ``SweepComboError`` — "Resilient: skip + continue") is meaningful:

    * ``summary`` is a :class:`~brutex.engine.sweep_run.SweepRunSummary` on the
      success path, ``None`` when the tuple had no bars (a no-bars skip), and is
      unused (``None``) when ``failed_error`` is set.
    * ``failed_error`` is the ``str(SweepComboError)`` message on a failed tuple
      (the exception itself is re-materialised on the parent so the audit line +
      ``tuples_failed`` verdict are byte-identical to the sequential path), else
      ``None``.

    :attr:`audit_records` are the buffered ``SWEEP_*`` ingest-audit lines the
    worker collected as plain data — the parent replays them into the real
    hash-chained :class:`~brutex.orchestrator.audit.AuditWriter` in enumeration
    order. :attr:`index` is the canonical enumeration index (the reorder key).
    """

    index: int
    summary: SweepRunSummary | None
    failed_error: str | None
    audit_records: tuple[BufferedAuditRecord, ...]


@dataclass(frozen=True, slots=True)
class _WorkerContext:
    """Immutable per-pool context handed to each worker via the pool initializer.

    Picklable (a frozen dataclass of a ``SweepGridConfig`` + a ``Path`` + an
    optional :class:`~brutex.engine.reference_series.TradeReferenceStamper` —
    itself a frozen slotted dataclass over plain ``datetime → Decimal`` dicts,
    so it crosses the SPAWN boundary verbatim, pinned by test); the worker
    rebuilds its ``store_for`` factory from ``(root, grid)`` via
    :func:`~brutex.engine.sweep_orchestrator.make_store_for`, so the
    un-picklable parent closure never crosses the process boundary.
    ``reference_stamper`` is defaulted LAST (DEC-VIX-STAMP-WIRE-001) so a
    pre-wiring construction stays valid.
    """

    grid: SweepGridConfig
    root: Path
    reference_stamper: TradeReferenceStamper | None = None


#: The per-worker-PROCESS context, set ONCE by :func:`_worker_init`. A module
#: global by necessity — a ``ProcessPoolExecutor`` initializer cannot return a
#: value, and re-sending the (heavy) grid with every task would be wasteful. It
#: is written exactly once per worker process (single-threaded within a worker).
_WORKER_CTX: _WorkerContext | None = None


def _worker_init(ctx: _WorkerContext) -> None:
    """Pool ``initializer``: pin single-threaded numerics + stash the context.

    DETERMINISM PIN (DEC-MULTICORE-SWEEP-PHASE1-001, design §3.3): one Python
    process per core already gives the parallelism; nested BLAS / rayon / polars
    threads would BOTH oversubscribe AND risk an order-DEPENDENT float reduction
    (a reduction whose order changes with the thread count breaks bit-for-bit
    determinism). So every worker forces ``OMP_NUM_THREADS`` /
    ``RAYON_NUM_THREADS`` / ``POLARS_MAX_THREADS`` to ``1``. The RAYON pin now
    also serialises the wired resident trade-sim kernel's rayon arm
    (DEC-SWEEP-RUST-TRADESIM-WIRE-001) — its order-preserving integer
    ``par_iter`` is bit-identical serial-vs-parallel anyway, so the pin costs
    no correctness and prevents ``workers × num_cpus`` thread oversubscription.
    Bit-for-bit seq-vs-parallel equivalence empirically holds for this hot path
    (the golden test proves the end-to-end equivalence, not that these specific
    env pins are the sole cause).
    """
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["RAYON_NUM_THREADS"] = "1"
    os.environ["POLARS_MAX_THREADS"] = "1"
    global _WORKER_CTX  # noqa: PLW0603 — set-once per worker process (see the module note)
    _WORKER_CTX = ctx


def _run_tuple_in_worker(
    index: int,
    tuple_: SweepTuple,
    bars: Sequence[CandleBar],
) -> TupleWorkResult:
    """Run ONE tuple's UNCHANGED per-tuple body inside a worker; return plain data.

    Mirrors ONE iteration of ``run_supervised_grid``'s ``run_one_tuple`` call, but:
    a :class:`~brutex.orchestrator.audit.BufferedAuditCollector` collects the
    audit lines (the worker never opens the real chain), a ``_NoDedup`` proves the
    digest path never consults dedup, and the ``bars`` are passed IN (already in
    parent memory) via a trivial ``bars_for`` so no un-picklable loader crosses
    the boundary. A ``SweepComboError`` is caught → returned as a failed marker
    ("Resilient: skip + continue"); a ``MemoryError`` (OOM = global stop) and any
    OTHER exception propagate → the future breaks → the parent tears the pool down.
    """
    ctx = _WORKER_CTX
    if ctx is None:  # pragma: no cover — the initializer always runs before a task
        raise SweepParallelUsageError("worker context was not initialised")
    collector = BufferedAuditCollector()
    store_for = make_store_for(ctx.root, ctx.grid)
    try:
        summary = run_one_tuple(
            ctx.grid,
            tuple_,
            bars_for=lambda _instrument, _tf: bars,
            store_for=store_for,
            dedup=_NoDedup(),
            audit=collector,
            skip_empty=True,
            # The pool-wide stamper (DEC-VIX-STAMP-WIRE-001): every worker
            # binds the SAME pickled stamper, so parallel == sequential.
            reference_stamper=ctx.reference_stamper,
        )
    except SweepComboError as exc:
        return TupleWorkResult(
            index=index,
            summary=None,
            failed_error=f"{type(exc).__name__}: {exc}",
            audit_records=collector.records,
        )
    return TupleWorkResult(
        index=index,
        summary=summary,
        failed_error=None,
        audit_records=collector.records,
    )


class _NoDedup:
    """A dedup stand-in that PROVES the digest path never consults dedup.

    On the production digest path ``run_persisted_sweep`` bypasses dedup entirely
    (``bypass_dedup = trial_stats_digest is not None`` → ``seen`` is never called
    and ``mark_many`` is skipped). Passing this object — whose ``seen`` /
    ``mark_many`` RAISE — turns any accidental consultation into a loud failure
    instead of a silent wrong answer, structurally guaranteeing the invariant the
    whole parallel design rests on.
    """

    def seen(self, run_id_hex: str) -> bool:
        """RAISE — the digest path must never consult ``dedup.seen``.

        Reached only by the legacy-path guard test (which asserts this raise); in
        production the digest path bypasses dedup, so a real call here would be a
        broken invariant, surfaced loudly rather than silently mis-answered.
        """
        raise SweepParallelUsageError(
            "parallel sweep worker consulted dedup.seen on the digest path"
        )

    def mark_many(self, run_id_hexes: object) -> int:
        """RAISE — the digest path must never consult ``dedup.mark_many``.

        Reached only by the legacy-path guard test (which asserts this raise); in
        production the digest path bypasses dedup, so a real call here would be a
        broken invariant, surfaced loudly rather than silently mis-answered.
        """
        raise SweepParallelUsageError(
            "parallel sweep worker consulted dedup.mark_many on the digest path"
        )


# ────────────────────────────────────────────────────────────────────────
# Outcome — one enumeration slot's result, applied in canonical order
# ────────────────────────────────────────────────────────────────────────


@functools.total_ordering
@dataclass(frozen=True, slots=True)
class _Outcome:
    """One enumeration slot's applied-in-order result (the reorder-heap payload).

    Placed into a min-heap keyed on :attr:`index`; the writer-actor pops the
    contiguous ``next_apply`` prefix and applies each in canonical enumeration
    order. ``kind`` is one of ``"resume"`` / ``"cache"`` / ``"no_bars"`` /
    ``"failed"`` / ``"done"`` — mirroring the five branches of the sequential
    ``run_supervised_grid`` loop. The parent computes ``resume`` / ``cache`` slots
    synchronously at dispatch time; ``no_bars`` / ``failed`` / ``done`` arrive from
    a worker via :class:`TupleWorkResult`.

    Ordering is by enumeration :attr:`index` ONLY (the canonical apply order); the
    :func:`functools.total_ordering` decorator derives ``__le__`` / ``__gt__`` /
    ``__ge__`` from :meth:`__lt__` + the dataclass's ``__eq__`` so the type is a
    fully-ordered heap payload (heap semantics unchanged — the heap only ever
    consults ``__lt__``).
    """

    index: int
    tuple_: SweepTuple
    kind: str
    summary: SweepRunSummary | None
    failed_error: str | None
    audit_records: tuple[BufferedAuditRecord, ...]

    def __lt__(self, other: _Outcome) -> bool:
        """Order two outcomes by enumeration ``index`` (the canonical apply order)."""
        return self.index < other.index


# ────────────────────────────────────────────────────────────────────────
# ParallelGridDriver — the single serialising writer-actor
# ────────────────────────────────────────────────────────────────────────


class ParallelGridDriver:
    """Run the grid across a ``ProcessPoolExecutor`` with a single writer-actor.

    The parallel twin of
    :func:`~brutex.engine.sweep_supervisor.run_supervised_grid`, scoped to the
    PRODUCTION digest path (see the module docstring). ``workers - 1`` EVAL
    workers run the UNCHANGED per-tuple body; THIS thread (the writer-actor)
    enumerates the grid in canonical order, gates resume/cache in the parent,
    dispatches with a bounded in-flight window, drains results through a reorder
    min-heap keyed on the enumeration index, and applies ALL ordered state (both
    audit chains, the contiguous-clean-prefix checkpoint, the ``SweepCache``, and
    progress) in enumeration order — so the per-tuple Parquet tree is BYTE-IDENTICAL
    to a sequential run, and both audit chains are CONTENT-IDENTICAL MODULO
    TIMESTAMPS (and, for the supervisor chain, its ``SUPERVISOR_START`` payload's
    added ``workers`` key) AND independently verify, regardless of worker
    interleaving. (``ts_utc`` participates in each entry's chain hash, so the chains
    are not literally byte-for-byte across two runs — the invariant is byte-equal
    results + content-equal-modulo-ts, verifying chains + an identical checkpoint.)

    Not reusable — construct one per grid run and call :meth:`run` once.
    """

    __slots__ = (
        "_bars_for",
        "_grid",
        "_guards",
        "_heap",
        "_ingest_audit",
        "_monotonic",
        "_next_apply",
        "_reference_stamper",
        "_root",
        "_stall_seconds",
        "_subject",
        "_workers",
    )

    def __init__(
        self,
        grid: SweepGridConfig,
        *,
        bars_for: Callable[[str, str], Sequence[CandleBar]],
        root: Path,
        ingest_audit: AuditWriterLike,
        guards: SweepGuards,
        workers: int,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        reference_stamper: TradeReferenceStamper | None = None,
    ) -> None:
        """Build a driver for one grid run.

        Parameters
        ----------
        grid
            The validated grid config.
        bars_for
            ``(instrument, tf_label) -> bars`` loader — called ON THE PARENT
            (bars are marshalled to the worker with the task), so it need not be
            picklable.
        root
            The results root directory (workers rebuild ``store_for`` from it).
        ingest_audit
            The real per-tuple ``SWEEP_*`` chain writer (the parent replays each
            worker's buffered records into it, in enumeration order).
        guards
            The five live G&A objects (the parent owns their lifecycle).
        workers
            The TOTAL worker budget; the pool holds ``workers - 1`` EVAL workers
            (one core is the writer-actor). MUST be ``>= 2`` (else the caller runs
            the sequential path).
        stall_seconds
            Raise :class:`SweepStallError` if no result arrives for this long
            while work is in flight.
        monotonic
            Injected monotonic clock (tests drive the stall watchdog with a fake).
        reference_stamper
            Optional India-VIX stamper (DEC-VIX-STAMP-WIRE-001) carried into
            the picklable :class:`_WorkerContext` so EVERY spawn worker binds
            the same stamper — parallel output stays byte-identical to the
            sequential run (regression-pinned).

        Raises
        ------
        SweepParallelUsageError
            If ``workers < 2``.
        """
        if workers < _MIN_PARALLEL_WORKERS:
            raise SweepParallelUsageError(
                f"ParallelGridDriver requires workers >= {_MIN_PARALLEL_WORKERS} "
                f"(got {workers}); the caller must dispatch the sequential path for "
                "workers == 1"
            )
        self._grid = grid
        self._bars_for = bars_for
        self._root = root
        self._ingest_audit = ingest_audit
        self._guards = guards
        self._workers = workers
        self._stall_seconds = stall_seconds
        self._monotonic = monotonic
        self._reference_stamper = reference_stamper
        self._subject = f"sweep:{grid_identity(grid)[:16]}"
        self._heap: list[_Outcome] = []
        self._next_apply = 0

    def run(self) -> SupervisedGridSummary:
        """Execute the whole grid in parallel; return the aggregate tally.

        Equivalent to :func:`~brutex.engine.sweep_supervisor.run_supervised_grid`
        (Golden Rule #4 / #7 / #8): the per-tuple Parquet tree is BYTE-IDENTICAL,
        both audit chains are CONTENT-IDENTICAL MODULO TIMESTAMPS (plus the
        supervisor ``SUPERVISOR_START`` ``workers`` key) AND verify, and the
        checkpoint state is identical — proven by the sequential-vs-parallel golden
        test (``test_..._modulo_ts``). ``ts_utc`` participates in the chain hash, so
        the chains are not literally byte-for-byte across runs.
        """
        sweep_id = grid_identity(self._grid)
        total = count_sweep_tuples(self._grid)

        self._guards.checkpoint.load()
        resume_from = self._guards.checkpoint.next_index

        self._guards.progress.start(enumerated=total, detail=self._subject)
        self._guards.audit.append(
            action=SUPERVISOR_ACTION_START,
            subject=self._subject,
            payload={
                "sweep_id": sweep_id,
                "tuples_total": total,
                "resume_from": resume_from,
                "workers": self._workers,
                "instruments": list(self._grid.instruments),
                "timeframes": [tf.label for tf in self._grid.timeframes],
                "directions": [d.value for d in self._grid.directions],
                "k_values": list(self._grid.k_values),
                "mode": self._grid.mode,
                "data_snapshot_hash": self._grid.data_snapshot_hash,
                "indicator_version": self._grid.indicator_version,
                "code_commit_sha": self._grid.code_commit_sha,
            },
        )

        acc = _Accumulator()
        pool_size = self._workers - 1
        max_inflight = pool_size * _INFLIGHT_MULTIPLIER
        heap_cap = pool_size * _HEAP_CAP_MULTIPLIER
        ctx = _WorkerContext(
            grid=self._grid, root=self._root, reference_stamper=self._reference_stamper
        )
        try:
            with ProcessPoolExecutor(
                max_workers=pool_size,
                # SPAWN, never FORK (the default on Linux): the parent runs threads
                # (the watchdog daemon) + holds durable-writer + logging locks, and
                # a fork-after-threads inherits those locks in an arbitrary state →
                # a classic child deadlock. Spawn starts each worker from a clean
                # interpreter, so the worker is a pure sequential run of the
                # per-tuple body with no inherited lock state (a REAL production
                # safety fix — proven by the seq-then-parallel golden test, which
                # deadlocks under fork).
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_worker_init,
                initargs=(ctx,),
            ) as pool:
                self._drive(pool, resume_from, max_inflight, heap_cap, acc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Record-then-re-raise — mirror the sequential ABORT contract. The
            # secondary progress/audit writes are suppressed so the halt that
            # caused the abort can never mask the root cause.
            detail = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                self._guards.progress.fail(detail)
            with contextlib.suppress(Exception):
                self._guards.audit.append(
                    action=SUPERVISOR_ACTION_ABORT,
                    subject=self._subject,
                    payload={"error": detail},
                )
            raise

        payload = {
            "sweep_id": sweep_id,
            "tuples_total": total,
            "tuples_run": acc.tuples_run,
            "tuples_skipped_no_bars": acc.tuples_skipped_no_bars,
            "skipped_cache": acc.skipped_cache,
            "skipped_resume": acc.skipped_resume,
            "tuples_failed": acc.tuples_failed,
            "enumerated": acc.enumerated,
            "conflict_pruned": acc.conflict_pruned,
            "skipped_dedup": acc.skipped_dedup,
            "evaluated": acc.evaluated,
            "ranked": acc.ranked,
            "persisted": acc.persisted,
        }
        self._guards.progress.finish(
            detail=(
                f"{acc.tuples_run} run · {acc.skipped_cache} cached · "
                f"{acc.skipped_resume} resumed · {acc.tuples_failed} failed"
            )
        )
        self._guards.audit.append(
            action=SUPERVISOR_ACTION_COMPLETE, subject=self._subject, payload=payload
        )
        _LOG.info("sweep_parallel.complete", **payload)

        return SupervisedGridSummary(
            sweep_id=sweep_id,
            tuples_total=total,
            tuples_run=acc.tuples_run,
            tuples_skipped_no_bars=acc.tuples_skipped_no_bars,
            skipped_cache=acc.skipped_cache,
            skipped_resume=acc.skipped_resume,
            enumerated=acc.enumerated,
            conflict_pruned=acc.conflict_pruned,
            skipped_dedup=acc.skipped_dedup,
            evaluated=acc.evaluated,
            ranked=acc.ranked,
            persisted=acc.persisted,
            tuples_failed=acc.tuples_failed,
        )

    def _drive(
        self,
        pool: ProcessPoolExecutor,
        resume_from: int,
        max_inflight: int,
        heap_cap: int,
        acc: _Accumulator,
    ) -> None:
        """Interleave dispatch + drain, applying outcomes in enumeration order.

        Two bounds keep the driver honest under head-of-line blocking:

        * DISPATCH is gated on BOTH ``len(pending) < max_inflight`` (the outstanding
          FUTURES window) AND ``len(self._heap) < heap_cap`` (the reorder-heap cap),
          so completed-but-not-yet-applied sibling outcomes cannot accumulate
          unboundedly while the head tuple is slow — the heap stays O(pool_size)
          (:data:`_HEAP_CAP_MULTIPLIER`). ``next_apply`` is ALWAYS already in the
          heap or in ``pending`` (dispatch is strictly in enumeration order and
          never skips an index), so pausing dispatch on a full heap can never
          deadlock — the head is drained by step 2 or awaited by step 3.
        * The STALL clock (``last_progress``) is reset on ANY forward progress —
          a future COMPLETION (step 3) OR an APPLY (step 2). Crucially it is reset
          on a future resolving even when NOTHING is applyable yet (a slow head
          blocks the apply prefix while siblings complete), so a slow-but-
          progressing head with completing siblings never spuriously raises
          ``SweepStallError`` — only a genuine no-completion, no-apply hang does.
        """
        pending: dict[Future[TupleWorkResult], SweepTuple] = {}
        last_progress = self._monotonic()
        enumerator = enumerate(iter_sweep_tuples(self._grid))
        exhausted = False

        while not exhausted or pending or self._heap:
            # 1. Dispatch until the in-flight window OR the heap cap is full (or the
            #    grid is done). Coupling dispatch to the heap cap is the memory bound
            #    under head-of-line blocking (Golden Rule #5).
            while not exhausted and len(pending) < max_inflight and len(self._heap) < heap_cap:
                try:
                    index, tuple_ = next(enumerator)
                except StopIteration:
                    exhausted = True
                    break
                self._guards.watchdog.beat(self._guards.worker_name)
                slot = self._gate_or_dispatch(index, tuple_, resume_from, pool, pending)
                if slot is not None:
                    heapq.heappush(self._heap, slot)

            # 2. Apply every contiguous outcome the heap now permits (in order) —
            #    an APPLY is forward progress and resets the stall clock.
            if self._apply_ready_prefix(acc):
                last_progress = self._monotonic()

            # 3. If nothing is applyable yet, wait for the NEXT worker result. A
            #    future COMPLETING is itself forward progress (even when the slow
            #    head keeps the apply prefix blocked), so it resets the stall clock
            #    BEFORE any apply — this is the head-of-line liveness fix.
            if pending and (not self._heap or self._heap[0].index != self._next_apply):
                slot = self._await_next_result(pending, last_progress)
                last_progress = self._monotonic()
                heapq.heappush(self._heap, slot)
                if self._apply_ready_prefix(acc):
                    last_progress = self._monotonic()

    def _gate_or_dispatch(
        self,
        index: int,
        tuple_: SweepTuple,
        resume_from: int,
        pool: ProcessPoolExecutor,
        pending: dict[Future[TupleWorkResult], SweepTuple],
    ) -> _Outcome | None:
        """Resume/cache-gate a tuple in the PARENT, or dispatch it to a worker.

        Returns a synchronous ``_Outcome`` for a resume/cache skip (applied in
        order via the heap), or ``None`` after submitting a worker future (its
        result becomes an outcome when it completes). Mirrors the two parent-only
        gates of the sequential loop.
        """
        if index < resume_from:
            return _skip_outcome(index, tuple_, "resume")
        if self._guards.cache.seen(
            tuple_.k,
            tuple_.instrument,
            tuple_.tf.label,
            direction_label(tuple_.direction),
            tuple_cache_params(tuple_, self._grid),
            data_snapshot_hash=self._grid.data_snapshot_hash,
            indicator_version=self._grid.indicator_version,
            code_commit_sha=self._grid.code_commit_sha,
        ):
            return _skip_outcome(index, tuple_, "cache")
        bars = self._bars_for(tuple_.instrument, tuple_.tf.label)
        future = pool.submit(_run_tuple_in_worker, index, tuple_, bars)
        pending[future] = tuple_
        return None

    def _await_next_result(
        self,
        pending: dict[Future[TupleWorkResult], SweepTuple],
        last_progress: float,
    ) -> _Outcome:
        """Block for the next completed worker future; STALL-guard the wait.

        Raises :class:`SweepStallError` if NO future completes within
        ``stall_seconds`` of the last progress (``last_progress`` — reset by the
        caller on any completion or apply) while work is in flight. A wedged
        worker's future never resolves, so this is the guarantee the sweep cannot
        hang forever; because the clock measures time-since-last-COMPLETION, a
        slow-but-progressing head with completing siblings never trips it (only a
        genuine no-completion hang does).
        """
        deadline = last_progress + self._stall_seconds
        timeout = max(0.0, deadline - self._monotonic())
        done, _ = _futures_wait(pending, timeout=timeout, return_when=_FIRST_COMPLETED)
        if not done:
            # No completion within the stall window while work is in flight → stall.
            raise SweepStallError(
                f"parallel sweep made no progress for {self._stall_seconds:.0f}s "
                f"with {len(pending)} tuple(s) in flight (next_apply={self._next_apply})"
            )
        future = done.pop()
        tuple_ = pending.pop(future)
        result = future.result()  # re-raises a worker's non-combo exception
        return _result_to_outcome(result, tuple_)

    def _apply_ready_prefix(self, acc: _Accumulator) -> bool:
        """Apply every heap outcome whose index == ``next_apply``, contiguously.

        Returns ``True`` if at least one outcome was applied (advancing progress
        for the stall clock). This is the single point where ALL ordered state is
        mutated, in canonical enumeration order.
        """
        applied = False
        while self._heap and self._heap[0].index == self._next_apply:
            outcome = heapq.heappop(self._heap)
            self._apply(outcome, acc)
            self._next_apply += 1
            applied = True
        return applied

    def _apply(self, outcome: _Outcome, acc: _Accumulator) -> None:
        """Apply ONE enumeration slot's ordered state (audit + checkpoint + cache).

        Mirrors, branch-for-branch, the body of the sequential
        ``run_supervised_grid`` loop — but the CHECKPOINT advance is guarded by a
        CONTIGUOUS-CLEAN-PREFIX pointer (``acc.had_failure``): once ANY tuple has
        failed, no further checkpoint is recorded, freezing it at the last clean
        index BEFORE the first failure so a resume re-runs the failed tuple. The
        ``SweepCache.record`` STILL runs on a post-failure success, so those tuples
        are short-circuited on resume by the cache tier, not re-evaluated (the
        exact ``had_failure`` latch semantics of the sequential loop).
        """
        index = outcome.index
        label = tuple_label(outcome.tuple_)

        if outcome.kind == "resume":
            acc.skipped_resume += 1
            self._advance(current=label, survived=False, skipped=True)
            self._guards.audit.append(
                action=SUPERVISOR_ACTION_TUPLE_RESUME,
                subject=self._subject,
                payload={"index": index, "tuple": label},
            )
            return

        if outcome.kind == "cache":
            acc.skipped_cache += 1
            if not acc.had_failure:
                self._guards.checkpoint.record(index)
            self._advance(current=label, survived=False, skipped=True)
            self._guards.audit.append(
                action=SUPERVISOR_ACTION_TUPLE_CACHE,
                subject=self._subject,
                payload={"index": index, "tuple": label},
            )
            return

        # A worker outcome — replay its buffered ingest-audit records into the
        # real chain FIRST (in enumeration order), then apply the supervisor line
        # + checkpoint/cache/progress, mirroring the sequential ordering.
        self._replay_ingest_audit(outcome.audit_records)

        if outcome.kind == "failed":
            acc.tuples_failed += 1
            acc.had_failure = True
            self._advance(current=label, survived=False, skipped=False)
            self._guards.audit.append(
                action=SUPERVISOR_ACTION_TUPLE_FAILED,
                subject=self._subject,
                payload={"index": index, "tuple": label, "error": outcome.failed_error},
            )
            _LOG.warning(
                "sweep_parallel.tuple_failed",
                subject=self._subject,
                index=index,
                tuple=label,
                error=outcome.failed_error,
            )
            return

        if outcome.kind == "no_bars":
            acc.tuples_skipped_no_bars += 1
            if not acc.had_failure:
                self._guards.checkpoint.record(index)
            self._advance(current=label, survived=False, skipped=True)
            self._guards.audit.append(
                action=SUPERVISOR_ACTION_TUPLE_NO_BARS,
                subject=self._subject,
                payload={"index": index, "tuple": label},
            )
            return

        # The remaining branch is a successful ("done") tuple. By construction of
        # ``_result_to_outcome`` a "done" outcome ALWAYS carries a summary (that is
        # exactly the condition that selects the "done" kind), so this ``cast`` is
        # a pure type-narrowing with NO runtime branch — keeping the apply path at
        # 100% branch coverage without an unreachable guard.
        summary = cast("SweepRunSummary", outcome.summary)
        acc.tuples_run += 1
        acc.enumerated += summary.enumerated
        acc.conflict_pruned += summary.conflict_pruned
        acc.skipped_dedup += summary.skipped_dedup
        acc.evaluated += summary.evaluated
        acc.ranked += summary.ranked
        acc.persisted += summary.persisted
        self._guards.cache.record(
            outcome.tuple_.k,
            outcome.tuple_.instrument,
            outcome.tuple_.tf.label,
            direction_label(outcome.tuple_.direction),
            tuple_cache_params(outcome.tuple_, self._grid),
            data_snapshot_hash=self._grid.data_snapshot_hash,
            indicator_version=self._grid.indicator_version,
            code_commit_sha=self._grid.code_commit_sha,
        )
        if not acc.had_failure:
            self._guards.checkpoint.record(index)
        self._advance(current=label, survived=summary.persisted > 0, skipped=False)
        self._guards.audit.append(
            action=SUPERVISOR_ACTION_TUPLE_DONE,
            subject=self._subject,
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

    def _replay_ingest_audit(self, records: tuple[BufferedAuditRecord, ...]) -> None:
        """Append a worker's buffered ``SWEEP_*`` records into the real chain, in order."""
        for record in records:
            self._ingest_audit.append(
                action=record.action, subject=record.subject, payload=record.payload
            )

    def _advance(self, *, current: str, survived: bool, skipped: bool) -> None:
        """Tick progress once (the parent-only progress mutation)."""
        self._guards.progress.advance(current=current, survived=survived, skipped=skipped)


# ────────────────────────────────────────────────────────────────────────
# Small helpers (all pure — the heap/outcome plumbing)
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Accumulator:
    """Mutable running tally + the contiguous-clean-prefix ``had_failure`` latch.

    ``had_failure`` re-implements the sequential loop's checkpoint-freeze semantics
    under out-of-order completion: once ANY tuple fails (in ENUMERATION order —
    the apply loop is ordered), no further checkpoint advance is recorded, so a
    resume re-runs the failed tuple.
    """

    tuples_run: int = 0
    tuples_skipped_no_bars: int = 0
    skipped_cache: int = 0
    skipped_resume: int = 0
    tuples_failed: int = 0
    enumerated: int = 0
    conflict_pruned: int = 0
    skipped_dedup: int = 0
    evaluated: int = 0
    ranked: int = 0
    persisted: int = 0
    had_failure: bool = False


def _skip_outcome(index: int, tuple_: SweepTuple, kind: str) -> _Outcome:
    """Build a synchronous resume/cache skip outcome (no worker involved)."""
    return _Outcome(
        index=index,
        tuple_=tuple_,
        kind=kind,
        summary=None,
        failed_error=None,
        audit_records=(),
    )


def _result_to_outcome(result: TupleWorkResult, tuple_: SweepTuple) -> _Outcome:
    """Map a worker's :class:`TupleWorkResult` + its dispatched tuple to an ``_Outcome``.

    The worker returns only the ``index`` (the reorder key) — the parent still
    holds the ``SweepTuple`` in its pending map, so it is paired back in HERE (not
    re-marshalled across the process boundary). The result's own fields decide
    ``kind``: a ``failed_error`` → ``"failed"``, a ``None`` summary → ``"no_bars"``,
    else ``"done"``.
    """
    if result.failed_error is not None:
        kind = "failed"
    elif result.summary is None:
        kind = "no_bars"
    else:
        kind = "done"
    return _Outcome(
        index=result.index,
        tuple_=tuple_,
        kind=kind,
        summary=result.summary,
        failed_error=result.failed_error,
        audit_records=result.audit_records,
    )


class AuditWriterLike(Protocol):
    """The minimal audit-writer contract the parent replays worker records into.

    Structurally satisfied by the real
    :class:`~brutex.orchestrator.audit.AuditWriter` — the ONE method the driver
    calls (``append``). Kept as a local Protocol (not an import of ``AuditWriter``)
    so the ``fastbloom_rs`` wheel never enters this module's runtime import graph
    (the same per-module coverage-gate discipline the sibling engine modules keep).
    """

    def append(
        self,
        *,
        action: str,
        subject: str,
        payload: dict[str, object] | None = None,
        ts_utc: float | None = None,
    ) -> object:
        """Append one audit line; the return value is ignored by the driver."""


def run_parallel_grid(
    grid: SweepGridConfig,
    *,
    bars_for: Callable[[str, str], Sequence[CandleBar]],
    root: Path,
    ingest_audit: AuditWriterLike,
    guards: SweepGuards,
    workers: int,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    reference_stamper: TradeReferenceStamper | None = None,
) -> SupervisedGridSummary:
    """Run the grid across ``workers - 1`` EVAL processes + a single writer-actor.

    The parallel entry point (DEC-MULTICORE-SWEEP-PHASE1-001) — the twin of
    :func:`~brutex.engine.sweep_supervisor.run_supervised_grid`, scoped to the
    PRODUCTION digest path. HARD-REFUSES ``workers < 2`` (the caller runs the
    sequential path itself for ``workers == 1``). The per-tuple Parquet tree is
    BYTE-IDENTICAL to a sequential run, both audit chains are CONTENT-IDENTICAL
    MODULO TIMESTAMPS (plus the supervisor ``workers`` key) AND verify, and the
    checkpoint is identical.

    Parameters mirror :class:`ParallelGridDriver` (``root`` replaces the
    sequential ``store_for``: workers rebuild ``store_for`` from ``(root, grid)``,
    so no un-picklable parent closure crosses the process boundary — the ONE
    signature difference from ``run_supervised_grid``).

    Raises
    ------
    SweepParallelUsageError
        If ``workers < 2``.
    """
    driver = ParallelGridDriver(
        grid,
        bars_for=bars_for,
        root=root,
        ingest_audit=ingest_audit,
        guards=guards,
        workers=workers,
        stall_seconds=stall_seconds,
        monotonic=monotonic,
        reference_stamper=reference_stamper,
    )
    return driver.run()


__all__ = [
    "AUTO_SWEEP_WORKERS_CAP",
    "AUTO_SWEEP_WORKERS_RESERVED_CPUS",
    "DEFAULT_STALL_SECONDS",
    "SEQUENTIAL_SWEEP_WORKERS",
    "SMALL_GRID_FACTOR",
    "SWEEP_WORKERS_ENV",
    "AuditWriterLike",
    "ParallelGridDriver",
    "SweepParallelUsageError",
    "SweepStallError",
    "TupleWorkResult",
    "auto_sweep_workers",
    "refuse_parallel_on_legacy_path",
    "resolve_sweep_workers",
    "run_parallel_grid",
]
