"""Async fetch/write-pipeline lifecycle helpers for a Groww spot pull (COMMON).

DEC-CLI-FETCH-ISOLATION-001 (MIGRATION.md) — the single source of truth for
BUILDING + DRAINING the async fetch/process separation pool
(:class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline`) and the coalescing
batched write consumer (:class:`~brutex.data.write_consumer.BatchingWriteConsumer`)
that wrap a Groww spot pull so internal processing (session-filter / lake-write /
``ledger.record``) can NEVER block or slow the fetch lane.

These were originally private helpers inside
:mod:`brutex.dashboard.pull_controller` (the dashboard pull path). The CLI
``brutex fetch`` spot path was MISSING the same isolation — it called
``run_groww_spot_ingest_with_retry`` with ``write_pool=None`` / ``fetch_pipeline=None``,
so the per-window tail (filter + Parquet write + ledger record) ran INLINE on the
fetch worker thread between every two fetches. Per CLAUDE.md §11 COMMON (one source
of truth, no copy-paste), the lifecycle is hoisted HERE so BOTH the dashboard build
site AND the CLI build site share the identical builders + drain + reconcile. The
dashboard re-imports these (its internal call sites are byte-unchanged).

The :class:`~brutex.adapters.rate_common.RateController` (the per-second AIMD ceiling
+ the atomic 500/min governor) is UNTOUCHED by everything here — it stays entirely on
the producer/fetch side (CLAUDE.md §2 Rule 6), so the pull still issues requests at
most at the controller's allowed rate; faster writes only unblock the fetch workers to
fetch UP TO the ceiling, never beyond it (Golden Rule #6).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import structlog

from brutex.adapters.groww.ingest import IngestReport

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from brutex.adapters.groww.fetch_pipeline import FetchPipeline
    from brutex.data.sink import BarSink
    from brutex.data.volume_keepalive import VolumeKeepalive
    from brutex.data.write_consumer import BatchingWriteConsumer

__all__ = [
    "build_fetch_pipeline",
    "build_write_consumer",
    "drain_fetch_pipeline",
    "drain_write_consumer",
    "reconcile_pairs_with_pipeline",
]

_LOG = structlog.get_logger(__name__)


def build_write_consumer(sink: BarSink, *, dry_run: bool) -> BatchingWriteConsumer | None:
    """Build the coalescing batched write consumer when ``BRUTEX_WRITE_BATCH`` is on.

    DEC-WRITE-BATCH-COALESCE-001 — the PROVEN write-bottleneck fix. When the operator opts
    in via ``BRUTEX_WRITE_BATCH`` (DEFAULT OFF), every ``sink.append`` is routed through a
    background :class:`~brutex.data.write_consumer.BatchingWriteConsumer` that COALESCES all
    pending same-series windows into ONE O(month-depth) merge per drain cycle — so the fetch
    lane stops idling on its own write and the achieved rate climbs toward the vendor rate
    (benchmarked ~2-3× on the deep month). It changes ONLY how fetched data is WRITTEN — the
    upstream :class:`~brutex.adapters.rate_common.RateController` (the per-second AIMD ceiling
    + the atomic 500/min governor) is UNTOUCHED, so the pull still issues requests at most at
    the controller's allowed rate (Golden Rule #6 — faster writes unblock workers to fetch UP
    TO the ceiling, never beyond it).

    Returns ``None`` (the byte-identical synchronous write path — no thread, no queue) when:
    the flag is OFF (the default), this is a ``dry_run`` (no real writes), or construction
    fails (guarded — binding the consumer can NEVER crash the pull). The caller OWNS the
    consumer's lifetime and MUST :func:`drain_write_consumer` it in its ``finally`` BEFORE
    saving the window ledger (so every settled window is durable).
    """
    if dry_run:
        return None
    try:
        from brutex.data.write_consumer import (
            BatchingWriteConsumer,
            resolve_write_batch_enabled,
        )

        if not resolve_write_batch_enabled():
            return None
        consumer = BatchingWriteConsumer(sink=sink)
        _LOG.info("pull.write_batch.enabled")
        return consumer
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
        _LOG.exception("pull.write_batch.build_failed")
        return None


def drain_write_consumer(consumer: BatchingWriteConsumer | None) -> None:
    """Drain *consumer* at pull end (flush+join), best-effort.

    Idempotent + guarded — a drain error is logged, never re-raised, so the pull's
    ``finally`` (ledger persist + progress push) always completes. A ``None`` consumer
    (the synchronous default) is a no-op.
    """
    if consumer is None:
        return
    try:
        consumer.drain()
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
        _LOG.exception("pull.write_batch.drain_failed")


def build_fetch_pipeline(
    *,
    dry_run: bool,
    lake_root: Path | None = None,
    on_bars: Callable[[int], None] | None = None,
) -> FetchPipeline | None:
    """Build the FULL async fetch/process separation pool (DEFAULT ON, kill-switchable).

    DEC-GROWW-FETCH-ASYNC-SEPARATION-001 — the operator's core requirement: the fetch
    thread does ONLY fetch + enqueue; a SEPARATE consumer pool does ALL internal work
    (session-filter, dedup/lake-write, ledger.record), so internal processing can NEVER
    block or slow the fetch. The pipeline is ON BY DEFAULT (operator-authorized 2026-06-28):
    each window's tail is handed off to a
    :class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline` consumer; the upstream
    :class:`~brutex.adapters.rate_common.RateController` (the per-second AIMD ceiling + the
    atomic 500/min governor) is UNTOUCHED — it stays entirely on the producer side
    (CLAUDE.md §2 Rule 6). The synchronous byte-identical path is still reachable via the
    ``BRUTEX_GROWW_FETCH_PIPELINE=off`` kill-switch.

    When *lake_root* is given the pipeline is wired with a bounded on-disk SPILL area under it
    (DEC-FETCH-NONBLOCKING-HANDOFF-001) so a SUSTAINED-SLOW (not hung) disk can never
    back-pressure the fetch lane: a full consumer queue spills the window's bars to disk + the
    producer returns immediately, up to a bounded cap (past which it back-pressures, preferring a
    slowdown to an OOM). A disk-full at the spill maps to the existing
    :class:`~brutex.data.errors.LakeWriteHalt` graceful-pull-halt path. When *lake_root* is
    ``None`` (e.g. a build site with no resolved lake root) the pipeline is built WITHOUT a spill
    area — byte-identical to the pre-spill bounded back-pressure behaviour.

    *on_bars* (DEC-PULL-PROGRESS-HONESTY-001) is the optional live-bars progress hook
    threaded verbatim into the :class:`FetchPipeline` — invoked once per background-folded
    window with its durable appended-bar delta so a dashboard bars counter can tick live
    (the async producer's per-pair reports honestly read 0 bars). ``None`` (the default —
    the spot/CLI build sites) is byte-identical to before.

    When *lake_root* is given the pipeline is ALSO wired with a best-effort lake-volume
    KEEPALIVE (:class:`~brutex.data.volume_keepalive.VolumeKeepalive`,
    DEC-VOLUME-KEEPALIVE-001): a 30 s 1-byte write+fsync probe under the lake root whose
    wedge signal PAUSES :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.submit`
    (so the producer never reaches the unbounded spill ``fsync`` on a spun-down / wedged
    volume) and escalates to a :class:`~brutex.data.errors.LakeWriteHalt`
    (``volume_wedged``) after the wedge-halt bound. Construction is guarded by its OWN
    ``try/except`` (:func:`_build_volume_keepalive`) — a keepalive failure degrades to
    ``None`` and the pipeline still builds. HONEST envelope: the synchronous kill-switch
    path (``BRUTEX_GROWW_FETCH_PIPELINE=off``) gets NO keepalive — but it also has NO
    spill, and its inline write tail is already 90 s-bounded
    (DEC-FETCH-HUNG-WRITE-DEADLINE-001), so no unbounded producer hang exists there; the
    keepalive covers the ASYNC pipeline's producer-side spill gap only.

    Returns ``None`` (the byte-identical synchronous path — no thread, no queue) when: the
    kill-switch is set (``BRUTEX_GROWW_FETCH_PIPELINE`` is an explicit falsy token), this is a
    ``dry_run`` (no real writes), or construction fails (guarded — binding the pipeline can
    NEVER crash the pull). The caller OWNS the pipeline's lifetime and MUST
    :func:`drain_fetch_pipeline` it in its ``finally`` BEFORE saving the window ledger (so
    every queued window is processed + ledger-recorded before the flush);
    :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.drain` also stops the
    keepalive daemon on every exit path.
    """
    if dry_run:
        return None
    keepalive: VolumeKeepalive | None = None
    try:
        from brutex.adapters.groww.fetch_pipeline import (
            FetchPipeline,
            resolve_fetch_pipeline_size,
        )
        from brutex.adapters.groww.fetch_spill import FetchSpillArea

        size = resolve_fetch_pipeline_size()
        if size is None:
            return None
        spill_area = None if lake_root is None else FetchSpillArea.under_lake_root(lake_root)
        keepalive = _build_volume_keepalive(lake_root)
        pipeline = FetchPipeline(
            workers=size,
            on_stall=_pipeline_stall_alert,
            spill_area=spill_area,
            keepalive=keepalive,
            on_bars=on_bars,
        )
        _LOG.info(
            "pull.fetch_pipeline.enabled",
            workers=size,
            spill=None if spill_area is None else str(spill_area.spill_dir),
            keepalive=None if keepalive is None else str(keepalive.probe_path),
        )
        return pipeline
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
        _LOG.exception("pull.fetch_pipeline.build_failed")
        if keepalive is not None:
            # The pipeline itself failed to build AFTER the keepalive started — stop the
            # daemon so a failed build never leaks a live ``volume-keepalive`` thread
            # (``stop`` is idempotent + never raises into this handler).
            keepalive.stop()
        return None


def _build_volume_keepalive(lake_root: Path | None) -> VolumeKeepalive | None:
    """Build + start the lake-volume keepalive daemon, best-effort (DEC-VOLUME-KEEPALIVE-001).

    Returns a STARTED :class:`~brutex.data.volume_keepalive.VolumeKeepalive` probing
    ``<lake_root>/.volume-keepalive``, or ``None`` when: *lake_root* is ``None`` (no
    volume to watch — e.g. a build site with no resolved lake root), the
    ``BRUTEX_VOLUME_KEEPALIVE=off`` kill-switch is set, or construction/start fails
    (guarded — a keepalive error is logged ``pull.volume_keepalive.build_failed`` and
    can NEVER block or crash the pull; the fetch pipeline still builds without it).
    A dry-run never reaches here (:func:`build_fetch_pipeline` returns ``None`` first),
    so a dry-run spins no daemon. The :class:`FetchPipeline` OWNS the returned
    keepalive's stop (its ``drain`` stops the daemon on every exit path).
    """
    if lake_root is None:
        return None
    try:
        from brutex.data.volume_keepalive import (
            VolumeKeepalive,
            resolve_volume_keepalive_enabled,
        )

        if not resolve_volume_keepalive_enabled():
            return None
        keepalive = VolumeKeepalive.under_lake_root(lake_root)
        keepalive.start()
        return keepalive
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
        _LOG.exception("pull.volume_keepalive.build_failed")
        return None


def reconcile_pairs_with_pipeline(
    merged_pairs: dict[tuple[str, str], IngestReport],
    pipeline: FetchPipeline | None,
) -> dict[tuple[str, str], IngestReport]:
    """Fold the async pipeline's post-drain per-series totals back into the per-pair reports.

    DEC-GROWW-FETCH-ASYNC-SEPARATION-001 completion-count fix. In async-pipeline mode the
    producer returns a DEFERRED per-series :class:`~brutex.adapters.groww.ingest.IngestReport`
    with ``bars_appended == 0`` (the durable append + ledger.record run on a BACKGROUND consumer,
    not known synchronously). The controller's per-symbol rollup classifies a pair as
    ``completed`` IFF ``bars_appended > 0`` and derives the verdict from those counts — so without
    this reconcile EVERY async series read 0 bars → a false ``completed == 0`` /
    ``known_gap_only`` verdict even though every bar was durably written.

    Called AFTER :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.drain` (so the
    consumer totals are final), this OVERRIDES each deferred report's ``bars_appended`` /
    ``conflicts`` / ``still_failed_windows`` (== ``transient_failures``) with the consumer's real
    per-``(symbol, interval)`` totals, leaving ``windows`` / ``skipped_windows`` /
    ``retried_windows`` (producer-resolved, correct) untouched — so the async-mode reported
    counts + verdict equal the synchronous path's. A ``None`` pipeline (the synchronous default)
    returns *merged_pairs* unchanged. A series with no folded total (every window was a producer
    skip/transient, never reaching a consumer) keeps its producer report.
    """
    if pipeline is None:
        return merged_pairs
    per_series = pipeline.per_series_totals()
    if not per_series:
        return merged_pairs
    reconciled: dict[tuple[str, str], IngestReport] = {}
    for key, report in merged_pairs.items():
        totals = per_series.get(key)
        if totals is None:
            reconciled[key] = report
            continue
        reconciled[key] = replace(
            report,
            bars_appended=totals.bars_appended,
            conflicts=totals.conflicts,
            transient_failures=totals.transient,
            still_failed_windows=totals.transient,
            # DEC-VENDOR-CONFLICT-SURFACE-001: the deferred producer report read 0/None
            # for the vendor-behaviour counters (the consumer did the durable write) —
            # fold the consumer's real per-series totals back in, like bars/conflicts.
            vendor_dup_rows_dropped=totals.vendor_dup_rows_dropped,
            max_close_delta=totals.max_close_delta,
        )
    return reconciled


def _pipeline_stall_alert(idle_seconds: float) -> None:
    """Route a fetch-pipeline stall edge to the pull-health ``pull.stalled`` alert (MINOR-5 fix).

    The :class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline` stall watchdog fires this
    hook when windows are in flight but NO consumer tail completes for the threshold (a wedged
    ``sink.append`` — a stalled external SSD / NFS hiccup). It is routed into the SAME
    :meth:`~brutex.notifications.sweep_alerts.SweepAlerter.pull_degraded` ``pull.stalled`` alert
    channel the :class:`~brutex.observability.pull_health.PullHealthMonitor` stalled rule uses,
    so a stuck consumer reaches the operator (phone) — closing the gap where the watchdog only
    logged. The pipeline's rate-observe-driven stalled rule samples THROUGHPUT; this is the
    complementary CONSUMER-side stall (a wedged write the rate sampler cannot see).

    Best-effort + fully GUARDED (Golden Rule #10): a silent no-op when Telegram is disabled (the
    default) and ANY error is swallowed + logged, so the hook can NEVER block, slow, or crash the
    pull. ``achieved=0.0`` (the pipeline is making no progress); ``allowed`` is unknown here so it
    is reported ``0.0``.
    """
    try:
        from brutex.notifications.sweep_alerts import SweepAlerter

        SweepAlerter().pull_degraded(
            rule="pull.stalled",
            achieved=0.0,
            allowed=0.0,
            detail=(
                "OUR pipeline is the limiter — a fetch-pipeline consumer is wedged in "
                f"sink.append for {idle_seconds:.0f}s (check write latency / disk)"
            ),
        )
    except Exception:  # pylint: disable=broad-exception-caught — best-effort alert, never crash the pull
        _LOG.exception("pull.fetch_pipeline.stall_alert_failed")


def drain_fetch_pipeline(pipeline: FetchPipeline | None) -> None:
    """Drain *pipeline* at pull end (flush+join), re-raising a captured consumer halt.

    Idempotent. A ``None`` pipeline (the synchronous default) is a no-op. A
    :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.drain` that re-raises a
    captured :class:`~brutex.data.errors.LakeWriteHalt` (disk-full in a consumer) is allowed
    to PROPAGATE — that is the graceful pull-halt path (the spine's ``_GLOBAL_STOP_ERRORS``);
    any OTHER drain error is logged + swallowed so the ledger persist still runs.
    """
    if pipeline is None:
        return
    from brutex.data.errors import LakeWriteHalt

    try:
        pipeline.drain()
    except LakeWriteHalt:
        # A consumer hit disk-full / OOM — propagate so the pull halts gracefully (the
        # caller's halt handling + the CRITICAL Telegram alert fire on this).
        raise
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
        _LOG.exception("pull.fetch_pipeline.drain_failed")
