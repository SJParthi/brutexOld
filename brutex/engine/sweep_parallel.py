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
  per distinct ``(instrument, tf, proximity, prior_n, fib_anchor, price_source)``;
  contiguous block dispatch to preserve the DEC-SWEEP-RESIDENT-KERNEL-001 reuse win is
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
  a :class:`SweepStallError` so a sweep can never hang forever. The wait budget is
  HEAVY-GRID-AWARE (DEC-SWEEP-STALL-OOM-GUARDS-001): a single legitimately-slow
  ``k=5`` 1-minute tuple (``C(60,5) = 5.46M`` masks × hundreds of thousands of
  bars) runs FAR longer than a flat 300 s with no sub-tuple progress reported, so
  the parent scales the stall deadline by the MAX per-tuple ``mask·bar`` work of
  the in-flight set (a tuple that legitimately needs *N* minutes gets a
  proportional bound), floored at :func:`resolve_stall_seconds` — a genuine hang
  is STILL caught in bounded time (the bound is finite; a wedged worker makes zero
  progress so its work-scaled deadline eventually elapses), a slow-but-progressing
  heavy tuple is not spuriously aborted. See :meth:`ParallelGridDriver._effective_stall_seconds`.
* **A worker OOM is QUARANTINED durably + reversibly, not a resume-livelock**
  (DEC-SWEEP-STALL-OOM-GUARDS-001). A ``k=5`` worker OOM-killed while building
  ``bar_bits``/finalists breaks the whole pool (:class:`BrokenProcessPool`); a
  worker's Python-level :exc:`MemoryError` (cgroup/rlimit — the worker survives)
  arrives through ``future.result()``. Either way the generic ``except Exception``
  would ABORT + re-raise, and — because the checkpoint freezes at the last clean
  prefix — an identically-configured resume would re-dispatch the SAME
  deterministically-OOMing tuple forever (a silent no-progress loop). This driver
  catches BOTH shapes DISTINCTLY: already-FINISHED sibling futures are DRAINED
  (their real results applied, never discarded), the poison tuple(s) are recorded
  in the DURABLE quarantine sidecar (``<root>/.sweep_quarantine.json`` — fsync'd
  BEFORE the raise; deliberately NOT a ``SweepCache.record``, which would
  permanently + invisibly drop them from every identical resume), and the run
  fails LOUDLY with :class:`SweepWorkerPoolBrokenError` naming the tuple(s), the
  sidecar, and the remediation (reduce ``BRUTEX_SWEEP_WORKERS`` / ``k`` / raise
  ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``, then delete the sidecar entries and resume).
  A resume SKIPS quarantined tuples VISIBLY — the DISTINCT
  ``tuples_quarantined_prior`` summary counter, never folded into
  ``skipped_cache`` — and makes progress on the rest; deleting a sidecar entry
  genuinely recomputes that tuple (the ONE reversible recovery lever).
  Fail-loud is chosen over blind pool-rebuild-and-continue on the merits: a broken
  pool from OOM means the machine is under memory pressure, and the per-tuple rank
  budget DIVIDES across workers
  (:func:`brutex.config.hardware_envelope.memory_budget_bytes` sizes
  ``total × fraction / workers`` — the DEC-SWEEP-STREAMING-RANK-001 rank-region
  interaction) — so rebuilding and re-running the next heavy tuple would just
  re-OOM. HONEST limit (named follow-up, NOT claimed): a deterministically-WEDGED
  non-OOM tuple (an infinite loop, not an allocation failure) still aborts via
  :class:`SweepStallError` with a frozen checkpoint, so an identical resume
  re-dispatches IT — the stall path has no quarantine (pre-existing behaviour,
  out of this change's scope).
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
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn, Protocol, cast

import structlog

from brutex.engine.combo_mask import count_combos
from brutex.engine.sweep_mem_budget import MEM_BUDGET_ENV
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
from brutex.observability.logging import configure_process_logging_best_effort
from brutex.orchestrator.audit import BufferedAuditCollector, BufferedAuditRecord

if TYPE_CHECKING:
    from pathlib import Path

    from brutex.data.models import CandleBar

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

#: The BASE (minimum / floor) stall interval in seconds — how long the parent will
#: wait for the NEXT result before firing the stall watchdog on a LIGHT grid (one
#: whose in-flight tuples are cheap). A wedged worker's future never resolves, so —
#: unlike ``fetch_pipeline`` (which only WARNs) — this RAISES so a sweep can never
#: hang forever. On a HEAVY grid the effective bound is SCALED UP from this floor by
#: the in-flight ``mask·bar`` work (:meth:`ParallelGridDriver._effective_stall_seconds`)
#: so a legitimately-slow tuple is never spuriously aborted
#: (DEC-SWEEP-STALL-OOM-GUARDS-001); env-tunable via :data:`STALL_SECONDS_ENV`.
DEFAULT_STALL_SECONDS: Final[float] = 300.0

#: Environment variable overriding the BASE stall floor :data:`DEFAULT_STALL_SECONDS`
#: (a positive float, seconds). Unset/blank keeps the default; an invalid value
#: (non-float / ``<= 0``) falls back to the default and is logged LOUDLY at WARNING
#: (mirrors the :func:`resolve_sweep_workers` invalid-env loudness).
STALL_SECONDS_ENV: Final[str] = "BRUTEX_SWEEP_STALL_SECONDS"

#: The CONSERVATIVE hang-detection floor: the MINIMUM ``mask·bar`` evaluations per
#: second the resident kernel is ASSUMED to sustain. The heavy-grid stall bound is
#: ``work / this`` (seconds), floored at the base stall — so a tuple whose
#: ``C(n_unique, k) × N_bars`` work legitimately needs *N* minutes gets a
#: proportional wait budget instead of the flat 300 s that would abort it near
#: grid completion. HONEST (Golden Rule #10): this is a HEURISTIC floor, NOT a
#: measured throughput — the resident Rust trade-walk is order-of-magnitude
#: ~1e9 ``mask·bar``/s, so ``2.5e8`` gives ~4× headroom (a legit tuple never
#: false-aborts) while keeping the bound FINITE (a genuine hang is still caught in
#: bounded time — a wedged worker makes ZERO progress, so its work-scaled deadline
#: eventually elapses). It sets ONLY the false-abort margin vs the wedge-detection
#: latency; it never changes results. Env-tunable via
#: :data:`STALL_MASK_BARS_PER_SEC_ENV`.
STALL_MIN_MASK_BARS_PER_SEC: Final[float] = 250_000_000.0

#: Environment variable overriding :data:`STALL_MIN_MASK_BARS_PER_SEC` (a positive
#: float). Unset/blank keeps the default; an invalid value (non-float / ``<= 0``)
#: falls back to the default and is logged LOUDLY at WARNING.
STALL_MASK_BARS_PER_SEC_ENV: Final[str] = "BRUTEX_SWEEP_STALL_MASK_BARS_PER_SEC"


def _resolve_positive_float_env(
    env: dict[str, str] | None,
    name: str,
    default: float,
    event: str,
) -> float:
    """Resolve a positive-float env knob — unset/blank → *default*, invalid → LOUD.

    Reads *name* from *env* (default :data:`os.environ`): unset/blank →
    *default*; a valid ``float`` strictly ``> 0`` → that value; an INVALID value
    (non-float / non-finite / ``<= 0``) → *default*, logged at WARNING on *event*
    naming the value via ``repr`` (the :func:`resolve_sweep_workers` invalid-env
    loudness). O(1). Shared by :func:`resolve_stall_seconds` and
    :func:`resolve_stall_min_mask_bars_per_sec` (Golden Rule #11 COMMON).
    """
    import math

    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw)
    except ValueError:
        parsed = float("nan")
    if not math.isfinite(parsed) or parsed <= 0.0:
        _LOG.warning(event, requested_env=repr(raw), fallback=default)
        return default
    return parsed


def resolve_stall_seconds(env: dict[str, str] | None = None) -> float:
    """Resolve the BASE stall floor — env-tunable (DEC-SWEEP-STALL-OOM-GUARDS-001).

    Reads :data:`STALL_SECONDS_ENV`; unset/blank → :data:`DEFAULT_STALL_SECONDS`,
    a valid positive float → that value, an invalid value → the default + a LOUD
    ``sweep.stall_seconds.invalid`` WARNING. O(1).
    """
    return _resolve_positive_float_env(
        env, STALL_SECONDS_ENV, DEFAULT_STALL_SECONDS, "sweep.stall_seconds.invalid"
    )


def resolve_stall_min_mask_bars_per_sec(env: dict[str, str] | None = None) -> float:
    """Resolve the hang-detection throughput floor — env-tunable.

    Reads :data:`STALL_MASK_BARS_PER_SEC_ENV`; unset/blank →
    :data:`STALL_MIN_MASK_BARS_PER_SEC`, a valid positive float → that value, an
    invalid value → the default + a LOUD ``sweep.stall_mask_bars_per_sec.invalid``
    WARNING. O(1).
    """
    return _resolve_positive_float_env(
        env,
        STALL_MASK_BARS_PER_SEC_ENV,
        STALL_MIN_MASK_BARS_PER_SEC,
        "sweep.stall_mask_bars_per_sec.invalid",
    )


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


class SweepWorkerPoolBrokenError(BruteXError):
    """Raised when a worker OOM (or crash) poisons the sweep's worker pool mid-grid.

    Two triggers: an OS-OOM-killed worker breaks the whole pool
    (:class:`BrokenProcessPool` on the next ``future.result()`` / ``pool.submit``),
    or a worker's Python-level :exc:`MemoryError` (allocation failure under a
    cgroup/rlimit — the worker SURVIVES, the pool does not break) arrives through
    ``future.result()``. Because the checkpoint freezes at the last clean prefix,
    an identically-configured resume would re-dispatch the SAME deterministically-
    OOMing tuple forever — a silent no-progress livelock. This driver instead
    records the poison tuple(s) in the DURABLE, REVERSIBLE quarantine sidecar
    (:func:`quarantine_sidecar_path` — deliberately NOT a ``SweepCache.record``,
    which would permanently and invisibly drop the tuples from every identical
    resume) so a resume SKIPS them VISIBLY (the distinct ``tuples_quarantined_prior``
    counter, never folded into ``skipped_cache``), then raises THIS — distinct from
    a generic abort — naming the offending tuple(s), the sidecar path, and the
    remediation: relieve the memory pressure (reduce ``BRUTEX_SWEEP_WORKERS`` — the
    per-tuple rank budget divides across workers — / the grid's ``k``, or raise
    ``BRUTEX_SWEEP_MEM_BUDGET_BYTES``), then DELETE the tuples' sidecar entries and
    resume — the tuples are then genuinely recomputed
    (DEC-SWEEP-STALL-OOM-GUARDS-001).
    """


#: The supervisor-audit action recording a pool-broken tuple QUARANTINE. A plain
#: local string (not a :mod:`brutex.engine.sweep_supervisor` constant) so the
#: quarantine surface stays contained to this module; the ``AuditWriter.append``
#: contract accepts any action string, so a new vocabulary line needs no schema
#: change (DEC-SWEEP-STALL-OOM-GUARDS-001).
SUPERVISOR_ACTION_TUPLE_POOL_BROKEN: Final[str] = "SUPERVISOR_TUPLE_POOL_BROKEN"

#: The supervisor-audit action recording that a RESUMED run SKIPPED a tuple a PRIOR
#: run's broken pool quarantined (the durable sidecar hit) — counted DISTINCTLY as
#: ``tuples_quarantined_prior``, never folded into ``skipped_cache``, so the
#: completeness hole is always visible (DEC-SWEEP-STALL-OOM-GUARDS-001).
SUPERVISOR_ACTION_TUPLE_QUARANTINED_PRIOR: Final[str] = "SUPERVISOR_TUPLE_QUARANTINED_PRIOR"

#: The durable quarantine sidecar's file name, created under the results ``root``.
#: JSON: ``{"entries": [{"sweep_id":…, "index":…, "tuple":…, "reason":…}, …]}``.
#: The sidecar — NOT the SweepCache, NOT the checkpoint — is the ONLY thing that
#: makes a resume skip a quarantined tuple, so recovery is ONE reversible lever:
#: delete the tuple's entry (or the whole file) after relieving memory pressure and
#: resume — the tuple is then genuinely recomputed (DEC-SWEEP-STALL-OOM-GUARDS-001).
QUARANTINE_SIDECAR_NAME: Final[str] = ".sweep_quarantine.json"


def quarantine_sidecar_path(root: Path) -> Path:
    """The durable quarantine sidecar path for the results *root*. O(1)."""
    return root / QUARANTINE_SIDECAR_NAME


def load_quarantined_tuple_labels(root: Path, sweep_id: str) -> set[str]:
    """Load the quarantined tuple labels recorded for *sweep_id* under *root*.

    Missing file → the empty set (the common case). ANY other unreadable /
    corrupt / non-JSON / wrong-shape state — including the OSError SHAPES a
    hostile filesystem can present (a DIRECTORY at the sidecar path →
    ``IsADirectoryError``, a symlink self-loop → ``ELOOP``, unreadable →
    ``PermissionError``) — → the empty set + ONE LOUD
    ``sweep.quarantine.sidecar_corrupt`` WARNING (advisory sidecar: the worst
    outcome of ignoring a bad file is a re-dispatch of a poison tuple, which
    re-quarantines and REWRITES the sidecar — self-healing, never a wedge, and
    never a crash at the resume load). Entries for OTHER sweep_ids are ignored.
    O(entries).
    """
    import json

    path = quarantine_sidecar_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError as exc:
        # IsADirectoryError / PermissionError / ELOOP / … — the advisory
        # contract: never a wedge, never a crash; LOUD + empty.
        _LOG.warning(
            "sweep.quarantine.sidecar_corrupt",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return set()
    try:
        payload = json.loads(raw)
        entries = payload["entries"]
        labels = {
            str(entry["tuple"])
            for entry in entries
            if isinstance(entry, dict) and entry.get("sweep_id") == sweep_id
        }
    except (ValueError, TypeError, KeyError) as exc:
        _LOG.warning(
            "sweep.quarantine.sidecar_corrupt",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return set()
    return labels


def append_quarantine_entries(
    root: Path, sweep_id: str, entries: Sequence[tuple[int, str]], reason: str
) -> Path:
    """DURABLY append quarantined ``(index, tuple_label)`` *entries* to the sidecar.

    Read-merge-write via a same-directory temp file + ``os.replace`` (atomic on
    POSIX), fsync'd BEFORE the rename — the quarantine record must be durable
    BEFORE the run raises, or a crash between the two could livelock the next
    resume. A corrupt/unreadable existing sidecar is REPLACED (its labels were
    already served as empty by :func:`load_quarantined_tuple_labels` — same
    advisory contract; a symlink self-loop is SELF-HEALED — ``os.replace``
    replaces the link itself). A WRITE failure (an OSError shape — e.g. a
    DIRECTORY squatting at the sidecar path, no permission) is caught: the
    append is SKIPPED with ONE loud
    ``sweep.quarantine.sidecar_corrupt`` WARNING instead of raising — the
    informative :class:`SweepWorkerPoolBrokenError` the caller is about to
    raise must never be MASKED by a filesystem defect (the durable record is
    honestly lost + loud; a re-OOM on the resumed run re-attempts the append).
    Returns the sidecar path. O(existing entries + new entries).
    """
    import json

    path = quarantine_sidecar_path(root)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))["entries"]
        if not isinstance(existing, list):
            existing = []
    except (OSError, ValueError, TypeError, KeyError):
        # FileNotFoundError (the common first-quarantine case) plus any other
        # OSError/corruption shape — start fresh; the write leg below either
        # replaces the bad state or warns loudly.
        existing = []
    for index, label in entries:
        existing.append({"sweep_id": sweep_id, "index": index, "tuple": label, "reason": reason})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"entries": existing}, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        _LOG.warning(
            "sweep.quarantine.sidecar_corrupt",
            path=str(path),
            error=f"{type(exc).__name__}: {exc}",
        )
    return path


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

    Picklable (a frozen dataclass of a ``SweepGridConfig`` + a ``Path``); the
    worker rebuilds its ``store_for`` factory from ``(root, grid)`` via
    :func:`~brutex.engine.sweep_orchestrator.make_store_for`, so the un-picklable
    parent closure never crosses the process boundary.
    """

    grid: SweepGridConfig
    root: Path


#: The per-worker-PROCESS context, set ONCE by :func:`_worker_init`. A module
#: global by necessity — a ``ProcessPoolExecutor`` initializer cannot return a
#: value, and re-sending the (heavy) grid with every task would be wasteful. It
#: is written exactly once per worker process (single-threaded within a worker).
_WORKER_CTX: _WorkerContext | None = None


def _worker_init(ctx: _WorkerContext) -> None:
    """Pool ``initializer``: pin numerics + bound worker logging + stash the context.

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
    # LOG-VOLUME GUARD (DEC-SWEEP-LOG-VOLUME-GUARD-001, R4 fold): a SPAWN
    # worker starts from a CLEAN interpreter — the parent's structlog config
    # is NOT inherited — and the per-trade ``trade_sim.entry``/``exit`` DEBUG
    # events fire INSIDE the worker onto the inherited stderr fd. Bound the
    # worker's own sink exactly like the CLI dispatch bounds the parent
    # (INFO default; ``$BRUTEX_LOG_LEVEL`` propagates to spawn children via
    # the environment). Best-effort by contract: the helper NEVER raises, so
    # a logging-config failure cannot break the worker.
    configure_process_logging_best_effort()
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
    ("Resilient: skip + continue"); a ``MemoryError`` (OOM) or any OTHER exception
    propagates. A worker OOM typically KILLS the worker process, so the pool breaks
    and the parent sees :class:`BrokenProcessPool` on the next ``result()`` — which
    the parent QUARANTINES (DEC-SWEEP-STALL-OOM-GUARDS-001,
    :meth:`ParallelGridDriver._quarantine_broken_pool`) rather than looping on it.
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
    ``run_supervised_grid`` loop — or the parallel-only ``"pool_broken"`` quarantine
    (DEC-SWEEP-STALL-OOM-GUARDS-001). The parent computes ``resume`` / ``cache`` /
    ``pool_broken`` slots synchronously; ``no_bars`` / ``failed`` / ``done`` arrive
    from a worker via :class:`TupleWorkResult`.

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


@dataclass(frozen=True, slots=True)
class _Dispatched:
    """A dispatched-but-not-yet-completed tuple's parent-side record.

    Stored per in-flight future in the driver's ``pending`` map: the enumeration
    ``index`` (paired back on completion instead of re-marshalling the tuple across
    the process boundary), the ``SweepTuple`` itself, and its ``mask·bar`` work
    estimate (``C(n_unique, k) × N_bars``) — the heavy-grid stall bound is scaled by
    the MAX ``work`` across the in-flight set (DEC-SWEEP-STALL-OOM-GUARDS-001), and
    the whole set is quarantined by index on a broken pool.
    """

    index: int
    tuple_: SweepTuple
    work: float


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
        "_n_unique",
        "_next_apply",
        "_quarantined",
        "_root",
        "_stall_min_mask_bars_per_sec",
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
        stall_seconds: float | None = None,
        stall_min_mask_bars_per_sec: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
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
            The BASE stall floor — raise :class:`SweepStallError` if no result
            arrives for this long (or the heavier work-scaled bound) while work is
            in flight. ``None`` (the default) resolves it from the environment via
            :func:`resolve_stall_seconds` (:data:`STALL_SECONDS_ENV`); a test passes
            an explicit value to drive the watchdog deterministically.
        stall_min_mask_bars_per_sec
            The conservative hang-detection throughput floor used to SCALE the stall
            bound by the in-flight ``mask·bar`` work (heavy-grid awareness,
            DEC-SWEEP-STALL-OOM-GUARDS-001). ``None`` resolves it from the
            environment via :func:`resolve_stall_min_mask_bars_per_sec`.
        monotonic
            Injected monotonic clock (tests drive the stall watchdog with a fake).

        Raises
        ------
        SweepParallelUsageError
            If ``workers < 2``, or an explicit ``stall_seconds`` /
            ``stall_min_mask_bars_per_sec`` is not a positive finite float (a
            direct-constructor ``0`` would divide-by-zero the work-scaled bound;
            the env resolvers already guarantee positive-finite).
        """
        import math

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
        self._stall_seconds = resolve_stall_seconds() if stall_seconds is None else stall_seconds
        self._stall_min_mask_bars_per_sec = (
            resolve_stall_min_mask_bars_per_sec()
            if stall_min_mask_bars_per_sec is None
            else stall_min_mask_bars_per_sec
        )
        for name, value in (
            ("stall_seconds", self._stall_seconds),
            ("stall_min_mask_bars_per_sec", self._stall_min_mask_bars_per_sec),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise SweepParallelUsageError(
                    f"ParallelGridDriver requires a positive finite {name} (got {value!r})"
                )
        #: ``C(n_unique, k)`` over these unique condition bits sizes each tuple's
        #: mask count — the heavy-grid work estimate (DEC-SWEEP-STALL-OOM-GUARDS-001).
        self._n_unique = len(set(grid.condition_bits))
        self._monotonic = monotonic
        self._subject = f"sweep:{grid_identity(grid)[:16]}"
        self._heap: list[_Outcome] = []
        self._next_apply = 0
        #: Labels a PRIOR run's broken pool quarantined (loaded from the durable
        #: sidecar in :meth:`run`) — gated FIRST in :meth:`_gate_or_dispatch` so a
        #: resume classifies them VISIBLY as ``quarantined_prior``, regardless of
        #: the checkpoint/cache state (DEC-SWEEP-STALL-OOM-GUARDS-001).
        self._quarantined: frozenset[str] = frozenset()

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

        # Load the durable quarantine sidecar BEFORE dispatch: a tuple a PRIOR
        # run's broken pool quarantined is skipped VISIBLY (quarantined_prior),
        # never re-dispatched into the same OOM (DEC-SWEEP-STALL-OOM-GUARDS-001).
        self._quarantined = frozenset(load_quarantined_tuple_labels(self._root, sweep_id))

        acc = _Accumulator()
        pool_size = self._workers - 1
        max_inflight = pool_size * _INFLIGHT_MULTIPLIER
        heap_cap = pool_size * _HEAP_CAP_MULTIPLIER
        ctx = _WorkerContext(grid=self._grid, root=self._root)
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
            "tuples_quarantined_prior": acc.tuples_quarantined_prior,
            "enumerated": acc.enumerated,
            "conflict_pruned": acc.conflict_pruned,
            "skipped_dedup": acc.skipped_dedup,
            "evaluated": acc.evaluated,
            "ranked": acc.ranked,
            "persisted": acc.persisted,
        }
        quarantine_note = (
            f" · {acc.tuples_quarantined_prior} QUARANTINED-prior "
            f"(see {quarantine_sidecar_path(self._root)})"
            if acc.tuples_quarantined_prior
            else ""
        )
        self._guards.progress.finish(
            detail=(
                f"{acc.tuples_run} run · {acc.skipped_cache} cached · "
                f"{acc.skipped_resume} resumed · {acc.tuples_failed} failed"
                f"{quarantine_note}"
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
            tuples_quarantined_prior=acc.tuples_quarantined_prior,
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
          The wait budget is HEAVY-GRID-AWARE (:meth:`_await_next_result` scales it
          by the in-flight ``mask·bar`` work), so a legitimately-slow k=5 tuple is
          not aborted while a genuine hang still is (DEC-SWEEP-STALL-OOM-GUARDS-001).

        A worker OOM (or crash) breaks the pool; :class:`BrokenProcessPool` from
        either ``pool.submit`` or a ``future.result()`` is caught here and routed to
        :meth:`_quarantine_broken_pool`, which drains the already-finished futures'
        REAL results, durably sidecar-quarantines the genuinely-broken in-flight
        tuples, then raises :class:`SweepWorkerPoolBrokenError` — so a resume never
        re-dispatches the same OOMing tuple (no livelock). A worker's surviving
        Python-level :exc:`MemoryError` takes the single-tuple twin path inside
        :meth:`_await_next_result` (:meth:`_quarantine_memory_error`).
        """
        pending: dict[Future[TupleWorkResult], _Dispatched] = {}
        last_progress = self._monotonic()
        enumerator = enumerate(iter_sweep_tuples(self._grid))
        exhausted = False

        try:
            while not exhausted or pending or self._heap:
                # 1. Dispatch until the in-flight window OR the heap cap is full (or
                #    the grid is done). Coupling dispatch to the heap cap is the
                #    memory bound under head-of-line blocking (Golden Rule #5).
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
                #    head keeps the apply prefix blocked), so it resets the stall
                #    clock BEFORE any apply — this is the head-of-line liveness fix.
                if pending and (not self._heap or self._heap[0].index != self._next_apply):
                    slot = self._await_next_result(pending, acc, last_progress)
                    last_progress = self._monotonic()
                    heapq.heappush(self._heap, slot)
                    if self._apply_ready_prefix(acc):
                        last_progress = self._monotonic()
        except BrokenProcessPool as exc:
            # A worker OOM/crash broke the pool: quarantine the in-flight tuples so a
            # resume skips them (no re-OOM livelock), then re-raise a distinct error.
            self._quarantine_broken_pool(pending, acc, exc)

    def _gate_or_dispatch(
        self,
        index: int,
        tuple_: SweepTuple,
        resume_from: int,
        pool: ProcessPoolExecutor,
        pending: dict[Future[TupleWorkResult], _Dispatched],
    ) -> _Outcome | None:
        """Resume/cache-gate a tuple in the PARENT, or dispatch it to a worker.

        Returns a synchronous ``_Outcome`` for a quarantined-prior/resume/cache skip
        (applied in order via the heap), or ``None`` after submitting a worker
        future (its result becomes an outcome when it completes). Mirrors the two
        parent-only gates of the sequential loop, PLUS the quarantine gate FIRST —
        a tuple a prior run's broken pool quarantined must classify VISIBLY as
        ``quarantined_prior`` (never blend into resume/cache skips), whatever the
        checkpoint/cache state. A dispatched tuple's ``_Dispatched`` record carries
        its ``mask·bar`` work estimate so :meth:`_await_next_result` can size the
        heavy-grid stall bound (DEC-SWEEP-STALL-OOM-GUARDS-001).
        """
        if self._quarantined and tuple_label(tuple_) in self._quarantined:
            return _skip_outcome(index, tuple_, "quarantined_prior")
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
        pending[future] = _Dispatched(
            index=index, tuple_=tuple_, work=self._tuple_work(tuple_, len(bars))
        )
        return None

    def _tuple_work(self, tuple_: SweepTuple, n_bars: int) -> float:
        """The tuple's ``mask·bar`` work estimate = ``C(n_unique, k) × N_bars``.

        The exact per-tuple work the trade sim performs (each of the ``C(n_unique,
        k)`` enumerated masks walks the ``N_bars`` bars). Used ONLY to size the
        heavy-grid stall bound (:meth:`_effective_stall_seconds`); never in results.
        O(1) — ``count_combos`` is a single ``math.comb``.
        """
        return float(count_combos(tuple_.k, self._n_unique)) * float(n_bars)

    def _effective_stall_seconds(self, max_inflight_work: float) -> float:
        """The heavy-grid-aware stall bound for the current in-flight set.

        ``max(base_stall, max_inflight_work / min_mask_bars_per_sec)`` — a light
        grid keeps the base floor (byte-identical to the pre-fix flat bound), a
        heavy k=5 tuple gets a bound proportional to its legitimate compute time so
        it is never spuriously aborted near grid completion. The bound is FINITE, so
        a genuine no-progress hang is still caught in bounded time — HONEST caveat
        (Golden Rule #10): the bound SCALES with ``C(n_unique, k) × N_bars``, so a
        wedged VERY heavy tuple is caught ~(1 / throughput-floor-headroom) × its
        legitimate runtime, which can be long (hours for a k=5 1-minute tuple) —
        the deliberate correctness > fast-detection trade
        (DEC-SWEEP-STALL-OOM-GUARDS-001). O(1).
        """
        work_bound = max_inflight_work / self._stall_min_mask_bars_per_sec
        return max(self._stall_seconds, work_bound)

    def _await_next_result(
        self,
        pending: dict[Future[TupleWorkResult], _Dispatched],
        acc: _Accumulator,
        last_progress: float,
    ) -> _Outcome:
        """Block for the next completed worker future; STALL-guard the wait.

        Raises :class:`SweepStallError` if NO future completes within the effective
        stall bound of the last progress (``last_progress`` — reset by the caller on
        any completion or apply) while work is in flight. The bound is
        HEAVY-GRID-AWARE: :meth:`_effective_stall_seconds` scales the base floor by
        the MAX ``mask·bar`` work of the in-flight tuples, so a legitimately-slow
        heavy head is not aborted (its trade walk simply needs longer than the flat
        300 s), while a wedged worker's never-resolving future still trips the
        (finite) bound. Because the clock measures time-since-last-COMPLETION, a
        slow-but-progressing head with completing siblings never trips it.

        A worker's Python-level :exc:`MemoryError` (allocation failure with the
        worker SURVIVING — the pool does NOT break, so the ``BrokenProcessPool``
        path never fires) is caught HERE and routed through the SAME durable
        quarantine as a broken pool, but for THIS tuple ONLY (the siblings are
        innocent and will be re-run on resume) — closing the identical-resume
        re-dispatch livelock for the cgroup/rlimit OOM shape too
        (DEC-SWEEP-STALL-OOM-GUARDS-001).
        """
        max_work = max(item.work for item in pending.values())
        effective_stall = self._effective_stall_seconds(max_work)
        deadline = last_progress + effective_stall
        timeout = max(0.0, deadline - self._monotonic())
        done, _ = _futures_wait(pending, timeout=timeout, return_when=_FIRST_COMPLETED)
        if not done:
            # No completion within the stall window while work is in flight → stall.
            raise SweepStallError(
                f"parallel sweep made no progress for {effective_stall:.0f}s "
                f"(base {self._stall_seconds:.0f}s, heaviest in-flight work "
                f"{max_work:.3g} mask·bar) with {len(pending)} tuple(s) in flight "
                f"(next_apply={self._next_apply})"
            )
        future = done.pop()
        # ``result()`` FIRST (before removing from ``pending``): a broken pool raises
        # :class:`BrokenProcessPool` here, and the future must still be in ``pending``
        # so :meth:`_quarantine_broken_pool` quarantines it (DEC-SWEEP-STALL-OOM-GUARDS-001).
        try:
            result = future.result()  # re-raises a worker's non-combo exception
        except MemoryError as exc:
            dispatched = pending.pop(future)
            self._quarantine_memory_error(dispatched, acc, exc)
        dispatched = pending.pop(future)
        return _result_to_outcome(result, dispatched.tuple_)

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

        if outcome.kind == "quarantined_prior":
            # A tuple a PRIOR run's broken pool quarantined (the durable sidecar).
            # Skip it VISIBLY: a DISTINCT counter (never folded into skipped_cache)
            # + a distinct audit action + one WARNING per tuple — the completeness
            # hole can never blend into a "successful" run. Treated as a STANDING
            # failure for the checkpoint (had_failure latch — data is permanently
            # missing until the operator deletes the sidecar entry), so the frozen
            # checkpoint keeps the recovery path live: delete the entry → the very
            # next resume re-dispatches the tuple (nothing else marks it done).
            acc.tuples_quarantined_prior += 1
            acc.had_failure = True
            self._advance(current=label, survived=False, skipped=True)
            self._guards.audit.append(
                action=SUPERVISOR_ACTION_TUPLE_QUARANTINED_PRIOR,
                subject=self._subject,
                payload={
                    "index": index,
                    "tuple": label,
                    "sidecar": str(quarantine_sidecar_path(self._root)),
                },
            )
            _LOG.warning(
                "sweep_parallel.tuple_quarantined_prior",
                subject=self._subject,
                index=index,
                tuple=label,
                sidecar=str(quarantine_sidecar_path(self._root)),
            )
            return

        if outcome.kind == "pool_broken":
            # A worker OOM/crash poisoned the pool while this tuple was in flight.
            # QUARANTINE — durable sidecar ONLY (written by the caller BEFORE these
            # outcomes are applied): deliberately NO SweepCache.record (that would
            # permanently + invisibly drop the tuple from every identical resume)
            # and NO checkpoint advance (had_failure latch, the sequential
            # failed-tuple semantics) — so the ONE reversible recovery lever is
            # "delete the sidecar entry and resume". The loss is LOUD: a distinct
            # audit action + ERROR log + counter, and the run itself fails loudly
            # via SweepWorkerPoolBrokenError naming tuple(s) + sidecar + remediation.
            acc.tuples_pool_broken += 1
            acc.had_failure = True
            self._advance(current=label, survived=False, skipped=False)
            self._guards.audit.append(
                action=SUPERVISOR_ACTION_TUPLE_POOL_BROKEN,
                subject=self._subject,
                payload={
                    "index": index,
                    "tuple": label,
                    "reason": outcome.failed_error,
                    "sidecar": str(quarantine_sidecar_path(self._root)),
                },
            )
            _LOG.error(
                "sweep_parallel.tuple_pool_broken",
                subject=self._subject,
                index=index,
                tuple=label,
                reason=outcome.failed_error,
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

    def _quarantine_broken_pool(
        self,
        pending: dict[Future[TupleWorkResult], _Dispatched],
        acc: _Accumulator,
        exc: BrokenProcessPool,
    ) -> NoReturn:
        """DURABLY quarantine the in-flight tuples on a broken pool, then fail loud.

        A worker OOM/crash breaks the pool. Already-FINISHED futures still carry
        REAL results — those are DRAINED and applied as genuine outcomes first
        (their compute is never discarded); every genuinely-broken future's tuple
        (the executor sets :class:`BrokenProcessPool` on unfinished futures; the
        parent cannot know WHICH caused the OOM) is quarantined: its identity is
        appended to the DURABLE sidecar (:func:`append_quarantine_entries` —
        fsync'd BEFORE anything else, so even a crash right here cannot lose the
        record) and it becomes a ``"pool_broken"`` outcome. All outcomes are
        pushed into the reorder heap at their enumeration indices and the
        contiguous prefix is applied — the union of ``self._heap`` and ``pending``
        covers every index in ``[next_apply, dispatched_so_far)`` (dispatch is
        strictly in enumeration order and never skips), so the prefix applies with
        no gaps. :meth:`_apply` deliberately does NOT ``SweepCache.record`` a
        quarantined tuple and does NOT advance the checkpoint past it — the
        SIDECAR alone makes a resume skip it (VISIBLY, as
        ``tuples_quarantined_prior``), so deleting the sidecar entry genuinely
        recomputes the tuple (the ONE reversible recovery lever).

        Then raises :class:`SweepWorkerPoolBrokenError` naming the offending
        tuple(s) + the sidecar + the remediation. This runs INSIDE ``run()``'s
        try-block, so the durable writes land BEFORE the abort line is recorded
        and the error re-raised (DEC-SWEEP-STALL-OOM-GUARDS-001).
        """
        reason = f"{type(exc).__name__}: {exc}"
        drained: list[_Outcome] = []
        broken: list[_Dispatched] = []
        for future, item in pending.items():
            outcome: _Outcome | None = None
            if future.done():
                try:
                    result = future.result(timeout=0)
                except Exception:  # a broken/raising done future → quarantine
                    outcome = None
                else:
                    outcome = _result_to_outcome(result, item.tuple_)
            if outcome is None:
                broken.append(item)
            else:
                drained.append(outcome)
        pending.clear()
        broken.sort(key=lambda item: item.index)

        # DURABLE FIRST: the sidecar write must land before any other state moves.
        sidecar = append_quarantine_entries(
            self._root,
            grid_identity(self._grid),
            [(item.index, tuple_label(item.tuple_)) for item in broken],
            reason,
        )
        for done_outcome in drained:
            heapq.heappush(self._heap, done_outcome)
        for item in broken:
            heapq.heappush(
                self._heap,
                _Outcome(
                    index=item.index,
                    tuple_=item.tuple_,
                    kind="pool_broken",
                    summary=None,
                    failed_error=reason,
                    audit_records=(),
                ),
            )
        self._apply_ready_prefix(acc)
        labels = ", ".join(tuple_label(item.tuple_) for item in broken)
        raise SweepWorkerPoolBrokenError(
            f"parallel sweep worker pool BROKE ({reason}) — a worker was likely "
            f"OOM-killed. Quarantined {len(broken)} in-flight tuple(s) in the durable "
            f"sidecar {sidecar}: [{labels}]. An identical-config resume SKIPS them "
            f"(reported as tuples_quarantined_prior — they are NOT silently "
            f"recomputed). Remediation: relieve the memory pressure — reduce "
            f"{SWEEP_WORKERS_ENV} (fewer workers raises each worker's per-tuple "
            f"memory budget AND cuts total pressure), reduce the grid's k, or raise "
            f"{MEM_BUDGET_ENV} — then DELETE the tuples' entries from the sidecar "
            f"(or the whole file) and resume: the tuples are then genuinely "
            f"recomputed."
        )

    def _quarantine_memory_error(
        self, dispatched: _Dispatched, acc: _Accumulator, exc: MemoryError
    ) -> NoReturn:
        """DURABLY quarantine ONE tuple whose worker raised :exc:`MemoryError`.

        The Python-level OOM shape (cgroup/rlimit allocation failure — the worker
        SURVIVES, the pool does NOT break): only THIS tuple is poisoned, so only it
        is quarantined — its innocent in-flight siblings are left un-recorded and
        will be re-run on resume. Sidecar write FIRST (durable before the raise),
        then the loud state (counter + had_failure latch + distinct audit action +
        ERROR log — mirroring the :meth:`_apply` ``pool_broken`` branch without the
        heap, because the run raises immediately and a blocked apply prefix must
        not be able to swallow the record), then
        :class:`SweepWorkerPoolBrokenError` (DEC-SWEEP-STALL-OOM-GUARDS-001).
        """
        reason = f"MemoryError: {exc}"
        label = tuple_label(dispatched.tuple_)
        sidecar = append_quarantine_entries(
            self._root, grid_identity(self._grid), [(dispatched.index, label)], reason
        )
        acc.tuples_pool_broken += 1
        acc.had_failure = True
        self._guards.audit.append(
            action=SUPERVISOR_ACTION_TUPLE_POOL_BROKEN,
            subject=self._subject,
            payload={
                "index": dispatched.index,
                "tuple": label,
                "reason": reason,
                "sidecar": str(sidecar),
            },
        )
        _LOG.error(
            "sweep_parallel.tuple_pool_broken",
            subject=self._subject,
            index=dispatched.index,
            tuple=label,
            reason=reason,
        )
        raise SweepWorkerPoolBrokenError(
            f"parallel sweep worker raised {reason} on tuple [{label}] (the worker "
            f"survived — a cgroup/rlimit allocation failure, not an OS OOM-kill). "
            f"Quarantined it in the durable sidecar {sidecar}; an identical-config "
            f"resume SKIPS it (reported as tuples_quarantined_prior). Remediation: "
            f"relieve the memory pressure — reduce {SWEEP_WORKERS_ENV}, reduce the "
            f"grid's k, or raise {MEM_BUDGET_ENV} — then DELETE the tuple's entry "
            f"from the sidecar and resume: the tuple is then genuinely recomputed."
        ) from exc

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
    #: Tuples THIS run quarantined because the worker pool broke / a worker raised
    #: MemoryError while they were in flight — recorded in the DURABLE sidecar
    #: (:func:`quarantine_sidecar_path`), deliberately NOT cache-recorded and NOT
    #: checkpoint-advanced, so the sidecar is the ONE reversible skip lever
    #: (DEC-SWEEP-STALL-OOM-GUARDS-001).
    tuples_pool_broken: int = 0
    #: Tuples a PRIOR run quarantined that THIS run therefore skipped (the sidecar
    #: gate) — a DISTINCT counter, never folded into ``skipped_cache``, surfaced in
    #: the summary so the completeness hole is always visible.
    tuples_quarantined_prior: int = 0
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
    stall_seconds: float | None = None,
    stall_min_mask_bars_per_sec: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
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
    signature difference from ``run_supervised_grid``). ``stall_seconds`` /
    ``stall_min_mask_bars_per_sec`` default to ``None`` → resolved from the
    environment (the heavy-grid-aware stall bound, DEC-SWEEP-STALL-OOM-GUARDS-001).

    Raises
    ------
    SweepParallelUsageError
        If ``workers < 2``.
    SweepWorkerPoolBrokenError
        If a worker OOM/crash breaks the pool (the in-flight tuples are durably
        quarantined first so a resume never re-dispatches them).
    """
    driver = ParallelGridDriver(
        grid,
        bars_for=bars_for,
        root=root,
        ingest_audit=ingest_audit,
        guards=guards,
        workers=workers,
        stall_seconds=stall_seconds,
        stall_min_mask_bars_per_sec=stall_min_mask_bars_per_sec,
        monotonic=monotonic,
    )
    return driver.run()


__all__ = [
    "AUTO_SWEEP_WORKERS_CAP",
    "AUTO_SWEEP_WORKERS_RESERVED_CPUS",
    "DEFAULT_STALL_SECONDS",
    "QUARANTINE_SIDECAR_NAME",
    "SEQUENTIAL_SWEEP_WORKERS",
    "SMALL_GRID_FACTOR",
    "STALL_MASK_BARS_PER_SEC_ENV",
    "STALL_MIN_MASK_BARS_PER_SEC",
    "STALL_SECONDS_ENV",
    "SUPERVISOR_ACTION_TUPLE_POOL_BROKEN",
    "SUPERVISOR_ACTION_TUPLE_QUARANTINED_PRIOR",
    "SWEEP_WORKERS_ENV",
    "AuditWriterLike",
    "ParallelGridDriver",
    "SweepParallelUsageError",
    "SweepStallError",
    "SweepWorkerPoolBrokenError",
    "TupleWorkResult",
    "append_quarantine_entries",
    "auto_sweep_workers",
    "load_quarantined_tuple_labels",
    "quarantine_sidecar_path",
    "refuse_parallel_on_legacy_path",
    "resolve_stall_min_mask_bars_per_sec",
    "resolve_stall_seconds",
    "resolve_sweep_workers",
    "run_parallel_grid",
]
