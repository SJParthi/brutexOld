"""Parallel per-symbol timeframe prebuild across CPU cores (DEC-HARDWARE-ENVELOPE-001).

The pipeline prebuild (:mod:`brutex.dashboard.pipeline_prebuild`) resamples the
stored 1-minute lake into the 19 derived intraday timeframes + the derived
``1day`` for EVERY pulled symbol (spot underlyings + their FUT/CE/PE contracts).
That per-symbol work is CPU-bound (a polars resample + a Parquet write) and was
run strictly single-threaded, so a full-universe prebuild used ONE core while
the other 13 on the operator's M4 Pro (CLAUDE.md §8) sat idle — the biggest
single-threaded win in the pull pipeline.

## Why THREADS (not processes)

The heavy per-symbol work is **off-GIL**: the polars resample releases the GIL,
and the Parquet writer releases the GIL during the write
(``parquet_writer.py``). So a :class:`~concurrent.futures.ThreadPoolExecutor`
genuinely scales here — and it avoids every process-pool hazard: no pickling of
the materialise closure / the raised exceptions, and
:func:`~brutex.observability.progress_beat.emit_progress_beat` (the pull-id-keyed
in-process seam, DEC-PULL-BEAT-PERRUN-001 — each pool TASK re-binds the driver's
captured ``pull_id`` so its beats route to the run's channel) keeps feeding the
pull stall watchdog for free (a ProcessPool child's beat is a no-op → a false
600 s "hung" kill). The pool is sized by
:func:`~brutex.config.hardware_envelope.cpu_pool_workers`.

## Safe per symbol (Golden Rule #7)

Different symbols write DIFFERENT lake partitions
(``bars/<exch>/<segment>/<symbol>/…``), and the durable writer takes a
per-partition cross-process ``flock`` keyed on ``sha256(dest)``
(:mod:`brutex.data.partition_lock`, DEC-LAKE-PARTITION-FLOCK-001), so there is
NO cross-symbol write contention and the parallel lake output is
**byte-identical** to a sequential run — proven by the byte-identity golden.
The per-symbol quarantine
(:func:`~brutex.dashboard.gated_prebuild.per_symbol_data_errors`) runs inside
each worker; a pull-wide global-stop (``LakeWriteHalt``) re-raises on the parent
via ``future.result()`` and propagates to the route guard (the halt alert
fires). The per-symbol tallies are folded PARENT-SIDE from the returned results
(single-threaded fold — no shared-counter race).

## polars thread pool (honest note, Golden Rule #10)

A THREAD pool shares ONE process-wide polars/rayon pool, so N worker threads do
NOT spawn ``N × cores`` polars threads (unlike a process pool) — the shared pool
is already bounded to the core count. We therefore deliberately do NOT pin
``POLARS_MAX_THREADS`` per worker (that would SERIALISE the shared resample
pool). :func:`~brutex.config.hardware_envelope.library_thread_budget` is exposed
for the process-pool / entrypoint scenario, not applied here.

## Kill switch

``BRUTEX_PREBUILD_WORKERS=1`` forces the sequential in-process path
(byte-identical to the pre-parallel behaviour); a tiny prebuild (``<= 1``
target) also stays in-process. An unset value auto-detects.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import structlog

from brutex.config.hardware_envelope import cpu_pool_workers
from brutex.observability.correlation import current_pull_id, pull_id_scope

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import date, datetime
    from pathlib import Path

    from brutex.data.timeframe_materialize import MaterializeResult, PrebuildKind

_LOG = structlog.get_logger(__name__)

#: Env var forcing / pinning the prebuild worker count. ``1`` = the sequential
#: in-process kill switch (byte-identical to the pre-parallel path); unset =
#: auto-detect via :func:`~brutex.config.hardware_envelope.cpu_pool_workers`.
PREBUILD_WORKERS_ENV: str = "BRUTEX_PREBUILD_WORKERS"

#: The sequential worker count — the ``BRUTEX_PREBUILD_WORKERS=1`` opt-out AND
#: the tiny-prebuild fallback (``1`` runs the in-process map, never a pool).
SEQUENTIAL_WORKERS: int = 1


@dataclass(frozen=True, slots=True)
class PrebuildWorkItem:
    """A single prebuild unit — one lake partition's derive (DEC-HARDWARE-ENVELOPE-001).

    Reconstructs the
    :func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes` call
    from DATA alone, so a worker (thread) needs no captured parent closure.

    Attributes
    ----------
    lake_root
        The lake root (``bars/<exchange>/<segment>/<symbol>/…`` lives under it).
    exchange, segment, symbol
        The partition's lake coordinates.
    kind
        The :class:`~brutex.data.timeframe_materialize.PrebuildKind` selecting
        the resampler pair (SPOT vs OI-aware F&O).
    include_daily
        Whether to also derive the ``1day`` partition.
    daily_before
        The forming-session cutoff (an IST date), resolved ONCE per run by the
        orchestrator and passed EXPLICITLY so every worker uses the SAME cutoff
        (byte-identity across a midnight-IST boundary; ``None`` only for a
        non-daily run).
    incremental
        The incremental-skip toggle (``None`` → the
        ``BRUTEX_PREBUILD_INCREMENTAL`` env default; ``False`` on the heal
        path).
    dense_symbol
        ``True`` for a SPOT INDEX underlying (the #1031 density gate) — selects
        the observe-only ``daily_open.substituted`` per-session WARNs AND the
        data-grounded daily-cutoff arm (DEC-DAILY-CUTOFF-GROUNDED-001);
        ``False`` for contracts + non-index spot.
    daily_cutoff_now
        The run's PINNED IST instant (DEC-DAILY-CUTOFF-GROUNDED-001), resolved
        ONCE per run by the orchestrator when ``daily_before`` is ``None`` —
        every worker resolves the SAME session-close-aware clock cutoff from
        it (M1 byte-identity across a midnight-IST boundary) and the grounded
        per-symbol verdict stays a pure function of ``(instant, own frame)``
        — order/concurrency-independent. APPENDED defaulted field
        (back-compat: an existing construction site behaves as before).
    """

    lake_root: Path
    exchange: str
    segment: str
    symbol: str
    kind: PrebuildKind
    include_daily: bool
    daily_before: date | None
    incremental: bool | None
    dense_symbol: bool = False
    daily_cutoff_now: datetime | None = None


@dataclass(frozen=True, slots=True)
class PrebuildWorkResult:
    """The outcome of ONE :class:`PrebuildWorkItem`.

    Exactly one of :attr:`result` / :attr:`error` is set:

    * :attr:`result` is the
      :class:`~brutex.data.timeframe_materialize.MaterializeResult` on success
      (per-TF bar counts + the ``unchanged`` incremental flag +
      ``opens_substituted``); ``None`` when the symbol was quarantined.
    * :attr:`error` is ``"<ErrorType>: <msg>"`` when the materialise raised a
      per-symbol DATA fault
      (:func:`~brutex.dashboard.gated_prebuild.per_symbol_data_errors`) — the
      symbol is quarantined + named, EXACTLY as the sequential spine does;
      ``None`` on success. A pull-wide global-stop (``LakeWriteHalt`` / a genuine
      bug) is NOT captured here — it re-raises on the parent and tears the pool
      down (the route guard fires the CRITICAL halt alert).
    """

    symbol: str
    result: MaterializeResult | None
    error: str | None


def materialize_work_item(item: PrebuildWorkItem) -> PrebuildWorkResult:
    """Materialise ONE work item; quarantine a per-symbol data fault.

    The worker (thread) entry point (also the in-process sequential body). Calls
    :func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes` with
    the item's coordinates and catches the COMMON per-symbol data-fault set
    (:func:`~brutex.dashboard.gated_prebuild.per_symbol_data_errors`) into a
    :attr:`PrebuildWorkResult.error` marker; a pull-wide global-stop
    (``LakeWriteHalt`` / a genuine bug) is NOT caught, so it propagates (the
    parent re-raises it via ``future.result()`` and the route guard fires the
    halt alert — mirroring the sequential spine). ``materialize_lake_timeframes``
    is resolved via its module at call time so tests can monkeypatch it.
    """
    from brutex.dashboard.gated_prebuild import per_symbol_data_errors
    from brutex.data import timeframe_materialize

    try:
        result = timeframe_materialize.materialize_lake_timeframes(
            lake_root=item.lake_root,
            exchange=item.exchange,
            segment=item.segment,
            symbol=item.symbol,
            kind=item.kind,
            include_daily=item.include_daily,
            daily_before=item.daily_before,
            daily_cutoff_now=item.daily_cutoff_now,
            incremental=item.incremental,
            dense_symbol=item.dense_symbol,
        )
    except per_symbol_data_errors() as exc:
        return PrebuildWorkResult(
            symbol=item.symbol,
            result=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    return PrebuildWorkResult(symbol=item.symbol, result=result, error=None)


def resolve_prebuild_workers(
    env: Mapping[str, str] | None = None,
    *,
    target_count: int | None = None,
    workers_fn: Callable[[], int] | None = None,
) -> int:
    """Resolve the prebuild worker count — env kill switch → auto → tiny-fallback.

    A valid ``>= 1`` :data:`PREBUILD_WORKERS_ENV` wins verbatim (``=1`` is the
    explicit sequential opt-out); an INVALID value (non-integer / ``< 1``) falls
    back to the auto count and is logged LOUDLY at WARNING; unset → the auto
    count (:func:`~brutex.config.hardware_envelope.cpu_pool_workers`, sized off
    the P-cores with 2 reserved → 8 on the reference M4 Pro). A known
    *target_count* ``<= 1`` demotes to :data:`SEQUENTIAL_WORKERS` (pool overhead
    is not worth one symbol) — but an EXPLICIT env override is never demoted.
    Emits ONE ``prebuild.workers.resolved`` event. O(1).

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`).
    target_count
        The number of work items when known (``None`` = unknown → no
        tiny-fallback).
    workers_fn
        The auto worker-count probe (defaults to
        :func:`~brutex.config.hardware_envelope.cpu_pool_workers`) — injected in
        tests.
    """
    source = os.environ if env is None else env
    auto_fn: Callable[[], int] = (
        (lambda: cpu_pool_workers(env)) if workers_fn is None else workers_fn
    )
    raw = source.get(PREBUILD_WORKERS_ENV)

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

    if explicit is not None:
        resolved = explicit
        reason = "env_explicit"
    elif target_count is not None and target_count <= 1:
        resolved = SEQUENTIAL_WORKERS
        reason = "tiny_sequential"
    elif invalid:
        resolved = auto_fn()
        reason = "env_invalid_auto"
    else:
        resolved = auto_fn()
        reason = "auto"

    log = _LOG.warning if invalid else _LOG.info
    log(
        "prebuild.workers.resolved",
        requested_env=repr(raw),
        env_invalid=invalid,
        target_count=target_count,
        resolved=resolved,
        reason=reason,
    )
    return resolved


def run_work_items(
    items: Sequence[PrebuildWorkItem],
    *,
    workers: int,
    materialize_fn: Callable[[PrebuildWorkItem], PrebuildWorkResult] | None = None,
) -> list[PrebuildWorkResult]:
    """Materialise every work item — in-process (``workers <= 1``) or across a thread pool.

    Returns the results in INPUT order (regardless of completion order) so the
    caller can rebuild the phase reports deterministically. Byte-identical lake
    output either way (different partitions, per-partition ``flock``). Per-symbol
    faults are quarantined inside :func:`materialize_work_item`; a pull-wide
    global-stop (``LakeWriteHalt``) or a genuine bug RE-RAISES on the parent via
    ``future.result()`` — the ``with`` block drains the running workers then the
    error propagates (the route guard routes a halt to the CRITICAL alert).

    Parameters
    ----------
    items
        The work items (already phase-ordered + coord-deduped by the caller).
    workers
        The worker budget. ``<= 1`` OR ``len(items) <= 1`` runs the sequential
        in-process map (the kill switch / tiny fallback); otherwise a
        :class:`~concurrent.futures.ThreadPoolExecutor` of *workers* threads.
    materialize_fn
        The per-item materialiser (defaults to :func:`materialize_work_item`) —
        injected in tests so BOTH paths are exercised without a real lake.

    Returns
    -------
    list[PrebuildWorkResult]
        One result per item, in input order.
    """
    do_one = materialize_work_item if materialize_fn is None else materialize_fn
    if workers <= SEQUENTIAL_WORKERS or len(items) <= 1:
        return [do_one(item) for item in items]

    # Run correlation for the pool workers (DEC-PULL-BEAT-PERRUN-001): capture the
    # DRIVER thread's ambient pull_id BEFORE the pool, and re-bind it PER TASK (never
    # per thread — pool threads are REUSED across runs; ``bound_contextvars`` restores
    # on task exit so no id can leak into a later run's task). The workers' seam beats
    # (timeframe_materialize / the derive partition lock-wait) then attribute to the
    # run's keyed channel; a captured "" (no ambient scope) binds "" → the legacy
    # channel → byte-identical behaviour.
    pull_id = current_pull_id()

    def _bound_do_one(item: PrebuildWorkItem) -> PrebuildWorkResult:
        with pull_id_scope(pull_id):
            return do_one(item)

    _LOG.info("prebuild.parallel.start", items=len(items), workers=workers)
    results: list[PrebuildWorkResult | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prebuild") as pool:
        futures = {pool.submit(_bound_do_one, item): idx for idx, item in enumerate(items)}
        for future in as_completed(futures):
            idx = futures[future]
            # A per-symbol fault is already a PrebuildWorkResult(error=...); a
            # per-symbol EXCEPTION (LakeWriteHalt / a genuine bug) re-raises HERE
            # → out of the `with` → the route guard (halt → CRITICAL alert, else
            # an honest crashed=<Type>).
            results[idx] = future.result()
    # Every slot is filled — ``as_completed`` yields exactly one result per
    # future, each written to its own index — so this is a pure type-narrowing
    # cast (no runtime branch, keeping 100% branch coverage).
    return cast("list[PrebuildWorkResult]", results)


__all__ = [
    "PREBUILD_WORKERS_ENV",
    "SEQUENTIAL_WORKERS",
    "PrebuildWorkItem",
    "PrebuildWorkResult",
    "materialize_work_item",
    "resolve_prebuild_workers",
    "run_work_items",
]
