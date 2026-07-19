"""Post-pull movers warm — precompute the default movers snapshots after a clean pull.

DEC-PULL-MOVERS-WARM-001 — the gated phase-4 binding for
:func:`brutex.dashboard.pipeline_pull.run_full_pipeline`. After a fully-successful fetch
(spot + metadata + historical all passed their gates), warm the DEFAULT movers snapshots
(``1day`` · the LATEST available day · all-exchange · spot/futures/options) into the
:class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` at the current write
generation, so the operator's first ``/movers`` open is an O(1) cache hit instead of a
cold lake scan.

## No illusion (Golden Rule #1)

The warm targets the **latest day that actually has data** (``latest_movers_date`` — the
MAX spot IST date over the **1-minute base** ∪ the derived ``1day`` partitions;
DEC-MOVERS-DAILY-FRESHNESS-001 repointed it at the 1-minute source of truth so a
post-prebuild 1-minute append moves the default date), NEVER "today": the just-finished
historical pull fills the lake only to the PREVIOUS trading day, so warming today's
coordinate would cache EMPTY snapshots (`compute_spot_candidates` reads the exact
requested date — no fallback). If the lake has no spot data yet in EITHER source
(`latest_movers_date` is ``None``), nothing is warmed and ``0`` is returned — an honest
"warmed nothing", never a fabricated snapshot.

## Best-effort (the pull's success is never compromised)

The warm is a pure read-only accelerator over the just-filled lake. A warmer that raises
(a transient lake read race) is logged and counted as ``0`` for that asset — it NEVER
propagates to fail the already-successful pull (Correctness of the pull > the warm's
latency benefit). The cached value (even an empty candidate set) is still valid for the
generation, so the next ``/movers`` open is correct regardless.

## Bounded per-job SLOWNESS (DEC-MOVERS-WARM-UNBLOCK-001)

Best-effort covered a job that *raises*, but a job that is merely SLOW was unbounded:
the 2026-07-04/05 forensics (pull_id ``22fc7739f719``) measured 170-256 s PER warm key
over the 39-key grid (~1.7-2.6 h total; one BSE key with ZERO candidates cost 169.4 s of
pure lake-walk) because each job re-walked the whole options-exploded lake. Each job now
runs under the COMMON :func:`~brutex.adapters.groww.deadline.run_with_deadline` freeze
guard (the #910/#999 guard — REUSED, no copy; CLAUDE.md §11 COMMON), default
:data:`DEFAULT_WARM_JOB_DEADLINE_S` (300 s), env :data:`WARM_JOB_DEADLINE_ENV`. A
timed-out job is counted ``failed`` + logged ``movers.warm.job_timeout`` and the warm
CONTINUES with the next coordinate — the pull is never failed by a slow warm.

HONEST envelope (Golden Rule #10, the #999 pattern): the guard runs the job in a daemon
worker thread — an abandoned (timed-out) worker CANNOT be killed; it dies with the
process or when its blocking read finally returns. If it eventually completes, its
``get_or_compute`` result is the CORRECT value for that generation, so a late completion
can only populate the cache with the same data the route would compute — never a wrong
snapshot.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Final

import structlog

from brutex.adapters.groww.deadline import DeadlineExceeded, run_with_deadline
from brutex.calendars.nse import IST_TZ_NAME
from brutex.config.hardware_envelope import apply_duckdb_thread_bound
from brutex.observability.progress_beat import emit_progress_beat

__all__ = [
    "DEFAULT_WARM_JOB_DEADLINE_S",
    "WARM_EXCHANGES",
    "WARM_JOB_DEADLINE_ENV",
    "WARM_OPTIONS_TIERS",
    "WARM_OPTIONS_TRACKS",
    "MoversWarmResult",
    "futures_warm_grid",
    "latest_movers_date",
    "latest_movers_minute",
    "options_warm_grid",
    "resolve_warm_job_deadline_s",
    "spot_warm_grid",
    "warm_coordinate_label",
    "warm_movers_snapshots",
]

_LOG = structlog.get_logger(__name__)

#: Default per-warm-job wall-clock deadline (seconds). Sized from the 2026-07-04/05
#: forensics: a HEALTHY warmed coordinate completes in seconds (28 s for the WHOLE
#: 39-key grid pre-materialization), while the pathological full-lake-walk jobs ran
#: 170-256 s each — 300 s passes a slow-but-live job and cuts an effectively-wedged
#: one, keeping the worst-case whole-grid warm bounded at ~39 × 300 s in the
#: background (DEC-MOVERS-WARM-UNBLOCK-001; the movers GATE no longer waits on it).
DEFAULT_WARM_JOB_DEADLINE_S: Final[float] = 300.0

#: Env override for the per-warm-job deadline (seconds).
WARM_JOB_DEADLINE_ENV: Final[str] = "BRUTEX_MOVERS_WARM_JOB_DEADLINE_S"


def resolve_warm_job_deadline_s(raw: str | None = None) -> float:
    """Resolve the per-warm-job deadline: env override, else the audited default.

    Reads :data:`WARM_JOB_DEADLINE_ENV` (``BRUTEX_MOVERS_WARM_JOB_DEADLINE_S``);
    falls back to :data:`DEFAULT_WARM_JOB_DEADLINE_S` when unset, blank,
    non-numeric, non-positive, OR non-finite (``nan``/``inf`` parse as floats but
    would DEFEAT the bound — an ``inf`` deadline never expires, re-introducing the
    unbounded warm this fix removes). A bad value can therefore NEVER disable the
    bound (fail-safe to the audited default, Golden Rule #1). Mirrors the COMMON
    validation shape of
    :func:`~brutex.adapters.groww.write_deadline.resolve_write_deadline_seconds`
    (CLAUDE.md §11 COMMON shape). O(1).

    Parameters
    ----------
    raw
        The deadline value (``None`` reads :data:`WARM_JOB_DEADLINE_ENV`); a test
        may pass one directly.
    """
    value = os.environ.get(WARM_JOB_DEADLINE_ENV) if raw is None else raw
    if value is None or not value.strip():
        return DEFAULT_WARM_JOB_DEADLINE_S
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_WARM_JOB_DEADLINE_S
    if not math.isfinite(parsed) or parsed <= 0.0:
        return DEFAULT_WARM_JOB_DEADLINE_S
    return parsed


#: The spot segments the movers default snapshot reads (INDEX + CASH 1day partitions).
_SPOT_SEGMENTS: tuple[str, ...] = ("INDEX", "CASH")
_DAILY_INTERVAL = "1day"
#: The fetched granularity the latest-minute resolver reads (the ONE granularity
#: Groww serves — DEC-1MINUTE-ONLY-DERIVE-1DAY-001; the derived TFs never carry a
#: finer minute than their 1-minute base).
_MINUTE_INTERVAL = "1minute"
#: The writer's canonical zero-padded month-partition name parts
#: (`<yyyy>/<mm>.parquet` — the lake layout law), ASCII-anchored so a
#: Unicode-digit dir name can never parse (the #1614 round-3 lesson).
_CANONICAL_YEAR_RE: Final = re.compile(r"\d{4}", re.ASCII)
_CANONICAL_MONTH_RE: Final = re.compile(r"\d{2}", re.ASCII)

#: The exchange scopes the post-pull warmer pre-warms (DEC-MOVERS-WARM-GRID-001): the
#: all-exchange population (``None``) PLUS the two per-exchange scopes the operator can
#: pick, so a per-exchange ``/movers`` open after a pull is an O(1) cache hit not a cold
#: scan. Mirrors the ``exchange in ("NSE", "BSE")`` normalisation in the ``/movers`` route.
WARM_EXCHANGES: tuple[str | None, ...] = (None, "NSE", "BSE")

#: The serial-expiry tiers the warmer pre-warms for futures + options (the Near/Next/Far
#: dropdown — DEC-MOVERS-FUTURES-TIER-001 / DEC-MOVERS-OPTIONS-EXPIRY-001). Lower-cased to
#: match the canonical tokens the cache key uses.
WARM_OPTIONS_TIERS: tuple[str, ...] = ("near", "next", "far")

#: The cadence tracks the warmer pre-warms for options (the All/Monthly/Weekly selector —
#: DEC-MOVERS-OPTIONS-TRACK-001). Lower-cased to match the canonical cache-key tokens.
WARM_OPTIONS_TRACKS: tuple[str, ...] = ("all", "weekly", "monthly")


def spot_warm_grid() -> list[dict[str, str | None]]:
    """The spot warm grid — one coordinate per exchange scope (DEC-MOVERS-WARM-GRID-001).

    Returns one ``{"exchange": ...}`` dict per :data:`WARM_EXCHANGES` scope (all-exchange
    + NSE + BSE), so a spot ``/movers`` open at any exchange after a pull is an O(1) cache
    hit. Pure — the caller turns each coordinate into a warm job that reuses the EXACT
    ``candidate_key`` + ``compute_spot_candidates`` the route uses (CLAUDE.md §11 COMMON).
    """
    return [{"exchange": exch} for exch in WARM_EXCHANGES]


def futures_warm_grid() -> list[dict[str, str | None]]:
    """The futures warm grid — exchange × Near/Next/Far tier (DEC-MOVERS-WARM-GRID-001).

    Returns one ``{"exchange": ..., "expiry_tier": ...}`` dict per
    :data:`WARM_EXCHANGES` × :data:`WARM_OPTIONS_TIERS` combination (3 × 3 = 9), so any
    Futures tier at any exchange after a pull is an O(1) cache hit. Pure — the caller
    builds the warm jobs reusing the EXACT ``candidate_key`` + ``compute_futures_candidates``.
    """
    return [
        {"exchange": exch, "expiry_tier": tier}
        for exch in WARM_EXCHANGES
        for tier in WARM_OPTIONS_TIERS
    ]


def options_warm_grid() -> list[dict[str, str | None]]:
    """The options warm grid — exchange × tier × cadence track (DEC-MOVERS-WARM-GRID-001).

    Returns one ``{"exchange": ..., "expiry_tier": ..., "expiry_track": ...}`` dict per
    :data:`WARM_EXCHANGES` × :data:`WARM_OPTIONS_TIERS` × :data:`WARM_OPTIONS_TRACKS`
    combination (3 × 3 × 3 = 27), so any Options Near/Next/Far × All/Monthly/Weekly at any
    exchange after a pull is an O(1) cache hit (the operator's "movers is slow" fix). Pure
    — the caller builds the warm jobs reusing the EXACT ``candidate_key`` +
    ``compute_options_candidates`` the route uses (CLAUDE.md §11 COMMON — no drift).
    """
    return [
        {"exchange": exch, "expiry_tier": tier, "expiry_track": track}
        for exch in WARM_EXCHANGES
        for tier in WARM_OPTIONS_TIERS
        for track in WARM_OPTIONS_TRACKS
    ]


def _max_ist_date_over_globs(globs: list[str], *, event: str) -> str | None:
    """``MAX(IST date)`` over the parquet *globs*, or ``None`` (empty/unreadable).

    The ONE guarded DuckDB reduction the two :func:`latest_movers_date` sources
    share (CLAUDE.md §11 COMMON — the 1-day scan and the 1-minute latest-month
    scan differ only in WHICH files they bind). Opens its own short-lived
    connection (self-contained, like the ``compute_*_candidates`` readers).

    GAP #4 (resilience): a single corrupt / unreadable Parquet under a glob makes
    DuckDB raise a `duckdb.Error` (an `InvalidInputException` — "No magic bytes …")
    or an `OSError` fault; this read backs the DEFAULT `/movers` poll, so an
    unguarded raise would 500 the whole page on every open. Treat an unreadable
    lake the same as an empty one — return ``None`` so the route degrades to an
    honest empty/today payload instead of crashing (Golden Rule #1 / #10).
    NARROW: a code bug still propagates. *event* names the WARN per source.
    """
    import duckdb

    if not globs:
        return None
    try:
        with duckdb.connect() as conn:
            # DEC-THREAD-BUDGET-BOUNDS-001 — bound this ephemeral connection's
            # thread pool (DuckDB defaults to ALL logical cores per connection).
            # Honest tradeoff: an ISOLATED run on an idle box now scans at
            # physical//2 — accepted, because the warm fires right after a
            # pull (prebuild/writer activity) and the bound reads are small
            # (the tiny spot 1day partitions / ONE month of spot 1-minute).
            apply_duckdb_thread_bound(conn)
            row = conn.execute(
                # The tz literal interpolates the ONE canonical IST_TZ_NAME
                # root (DEC-TIMESTAMP-IST-SINGLESOURCE-001; SQL byte-identical).
                f"SELECT MAX((\"timestamp\" AT TIME ZONE '{IST_TZ_NAME}')::DATE) "  # noqa: S608  # nosec B608 - static hardcoded tz constant; glob list is parameter-bound
                "FROM read_parquet(?, union_by_name=true)",
                [globs],
            ).fetchone()
    except (duckdb.Error, OSError) as exc:
        _LOG.warning(event, error=str(exc))
        return None
    return str(row[0]) if row is not None and row[0] is not None else None


def _latest_spot_month(lake_root: Path) -> tuple[int, int] | None:
    """The lexicographically-latest ``(year, month)`` spot 1-minute partition, or ``None``.

    A NAMES-ONLY filesystem enumeration of the spot ``1minute`` month files
    (``…/1minute/<yyyy>/<mm>.parquet`` — the lake layout law): the latest
    1-minute bar necessarily lives in the max ``(year, month)`` UTC partition,
    so the DuckDB date reduction afterwards binds ONLY that one month's files
    (the :func:`latest_movers_minute` month-prune idiom) instead of the whole
    multi-year 1-minute lake. Non-canonical names (non-ASCII-digit year/month
    dirs — the #1614 Unicode-digit lesson) are skipped, never crashed on.
    Honest cost (Golden Rule #10): O(spot 1-minute files) directory-entry
    names per call — no parquet is opened here; the route memoises the caller
    per write generation.
    """
    latest: tuple[int, int] | None = None
    bars = lake_root / "bars"
    if not bars.is_dir():
        return None
    for seg in _SPOT_SEGMENTS:
        for path in bars.glob(f"*/{seg}/*/{_MINUTE_INTERVAL}/*/*.parquet"):
            year_text, month_text = path.parent.name, path.stem
            # Only the writer's CANONICAL zero-padded `<yyyy>/<mm>.parquet`
            # stem parses (the #1558 round-5/8 law; ASCII-anchored per the
            # #1614 round-3 Unicode-digit lesson): a junk `2024/6.parquet`
            # would bind a `06.parquet` glob that misses itself — skip it
            # here so the month key and the glob always agree.
            if not (
                _CANONICAL_YEAR_RE.fullmatch(year_text)
                and _CANONICAL_MONTH_RE.fullmatch(month_text)
            ):
                continue
            key = (int(year_text), int(month_text))
            if latest is None or key > latest:
                latest = key
    return latest


def latest_movers_date(lake_root: Path) -> str | None:
    """Return the IST date (ISO ``YYYY-MM-DD``) of the most recent spot bar, or ``None``.

    DEC-MOVERS-DAILY-FRESHNESS-001: the latest day the movers default snapshot
    can show WITH DATA — resolved from the **1-minute base** (the ONLY fetched
    granularity, the source of truth — DEC-1MINUTE-ONLY-DERIVE-1DAY-001) so a
    post-prebuild 1-minute append MOVES the default date to the new day instead
    of freezing it at the last prebuilt ``1day`` bar (the pre-fix read scanned
    ONLY the derived ``1day`` partitions, which the pipeline's final prebuild
    alone rewrites — a stale default day beside fresh 1-minute data). The
    derived ``1day`` scan is KEPT as the second source and the answer is the
    MAX of the two, so a 1day-only / legacy lake keeps its "latest AVAILABLE
    day, never an empty today" honesty (``None`` only when BOTH sources are
    empty — the caller then warms nothing rather than an empty "today"
    snapshot).

    The 1-minute source is month-pruned (:func:`_latest_spot_month` — a
    names-only enumeration finds the max ``(year, month)`` partition, then ONE
    DuckDB ``MAX`` binds only that month's spot files, the
    :func:`latest_movers_minute` idiom); the ``1day`` source stays bounded to
    the intrinsically tiny daily partitions. Each source is guarded
    independently (corrupt/unreadable ⇒ that source reads ``None`` + one WARN,
    never a 500 — :func:`_max_ist_date_over_globs`).

    Parameters
    ----------
    lake_root
        Top-level lake directory (parent of ``bars/``).

    Returns
    -------
    str | None
        The latest spot IST date as ``YYYY-MM-DD`` over the 1-minute base ∪
        the derived ``1day`` partitions, or ``None`` if both are absent.
    """
    # Only query the segments that actually have at least one file — read_parquet raises
    # "No files found" on an empty glob, which we treat as "no data" (None), not an error.
    daily_globs = [
        (lake_root / "bars" / "*" / seg / "*" / _DAILY_INTERVAL).as_posix() + "/**/*.parquet"
        for seg in _SPOT_SEGMENTS
        if (lake_root / "bars").is_dir()
        and any((lake_root / "bars").glob(f"*/{seg}/*/{_DAILY_INTERVAL}/**/*.parquet"))
    ]
    daily_latest = _max_ist_date_over_globs(daily_globs, event="movers.latest_date.unreadable_lake")
    minute_latest: str | None = None
    latest_month = _latest_spot_month(lake_root)
    if latest_month is not None:
        year, month = latest_month
        month_glob = f"{_MINUTE_INTERVAL}/{year:04d}/{month:02d}.parquet"
        minute_globs = [
            (lake_root / "bars" / "*" / seg / "*" / month_glob).as_posix()
            for seg in _SPOT_SEGMENTS
            if any((lake_root / "bars").glob(f"*/{seg}/*/{month_glob}"))
        ]
        minute_latest = _max_ist_date_over_globs(
            minute_globs, event="movers.latest_minute_date.unreadable_lake"
        )
    # ISO dates compare lexicographically in calendar order — MAX of the two
    # sources is the honest "latest available day" whichever holds it.
    candidates = [d for d in (daily_latest, minute_latest) if d is not None]
    return max(candidates) if candidates else None


def latest_movers_minute(lake_root: Path, day: date_cls) -> str | None:
    """Return the IST ``"HH:MM"`` of the most recent spot 1-minute bar on *day*, or ``None``.

    The DISPLAY-side sibling of :func:`latest_movers_date`
    (DEC-MOVERS-ASOF-LATEST-DEFAULT-001): ONE bounded DuckDB
    ``MAX(minute-of-day)`` over the spot ``INDEX`` + ``CASH`` ``1minute``
    partitions, converting the max IST bar to its ``HH:MM`` — the latest minute
    the default as-of snapshot resolves to. It backs the DEFAULT ``/movers``
    display echo ONLY (the *cut* stays whole-day, `cutoff_min=None`), so its
    value is honest by construction: it is the SAME ``MAX`` minute the whole-day
    read's last kept bar carries (``echo == cut`` in value).

    Guarded EXACTLY like :func:`latest_movers_date`
    (:func:`~brutex.config.hardware_envelope.apply_duckdb_thread_bound` on the
    ephemeral connection; ``(duckdb.Error, OSError) → None``) — it feeds the
    default poll, so it must NEVER raise/500. ``None`` (no 1m bar for *day*, an
    empty/absent spot lake, OR a corrupt Parquet) → the caller shows a plain
    "latest" label and still cuts whole-day.

    Honest envelope (Golden Rule #10): the read is MONTH-PRUNED — because *day*
    fully determines its ``<yyyy>/<mm>`` partition (lake layout
    ``…/1minute/<yyyy>/<mm>.parquet``), the glob targets ONLY that one month's
    spot ``1minute`` files (skipping F&O AND every other month), so DuckDB reads
    the timestamp column of ~one-month-of-spot-1m rather than the whole
    multi-year 1-minute lake — the amplification the sibling
    :func:`latest_movers_date` avoids by construction. The caller also memoises
    it per write generation (a repeat DEFAULT poll is an O(1) handoff). Still
    O(spot-1m-files-in-day's-month), so it is called on the threadpool worker,
    never the event loop.

    Parameters
    ----------
    lake_root
        Top-level lake directory (parent of ``bars/``).
    day
        The IST calendar day to resolve the latest 1-minute bar for.

    Returns
    -------
    str | None
        The latest 1-minute bar's IST ``"HH:MM"`` on *day*, or ``None``.
    """
    import duckdb

    # MONTH-PRUNE: *day* fully determines its `<yyyy>/<mm>` partition (lake
    # layout `…/<interval>/<yyyy>/<mm>.parquet`), so glob ONLY that month's spot
    # 1-minute files instead of `1minute/**/*.parquet` — one month (~750 files)
    # rather than the whole multi-year lake. The WHERE date filter still selects
    # `day`'s bars out of the month partition.
    month_glob = f"{_MINUTE_INTERVAL}/{day.year:04d}/{day.month:02d}.parquet"
    present = [
        (lake_root / "bars" / "*" / seg / "*" / month_glob).as_posix()
        for seg in _SPOT_SEGMENTS
        if (lake_root / "bars").is_dir()
        and any((lake_root / "bars").glob(f"*/{seg}/*/{month_glob}"))
    ]
    if not present:
        return None
    try:
        with duckdb.connect() as conn:
            # DEC-THREAD-BUDGET-BOUNDS-001 — bound this ephemeral connection's
            # thread pool (mirror `latest_movers_date`).
            apply_duckdb_thread_bound(conn)
            row = conn.execute(
                # The tz literals interpolate the ONE canonical IST_TZ_NAME
                # root (DEC-TIMESTAMP-IST-SINGLESOURCE-001; SQL byte-identical).
                "SELECT MAX("  # noqa: S608  # nosec B608 - static hardcoded tz constant; glob list + day are parameter-bound
                f"  EXTRACT(HOUR FROM (\"timestamp\" AT TIME ZONE '{IST_TZ_NAME}')) * 60 "
                f"  + EXTRACT(MINUTE FROM (\"timestamp\" AT TIME ZONE '{IST_TZ_NAME}'))"
                ") "
                "FROM read_parquet(?, union_by_name=true) "
                f"WHERE (\"timestamp\" AT TIME ZONE '{IST_TZ_NAME}')::DATE = ?",
                [present, day],
            ).fetchone()
    except (duckdb.Error, OSError) as exc:
        _LOG.warning("movers.latest_minute.unreadable_lake", error=str(exc))
        return None
    if row is None or row[0] is None:
        return None
    from brutex.dashboard.movers_snapshot import minute_to_hhmm

    return minute_to_hhmm(int(row[0]))


@dataclass(frozen=True, slots=True)
class MoversWarmResult:
    """The honest tally of one segment's movers-warm run (DEC-MOVERS-WARM-PROGRESS-001).

    Attributes
    ----------
    warmed
        Jobs that warmed a NON-empty snapshot (≥ 1 candidate).
    failed
        Jobs that RAISED (each logged + swallowed — best-effort); a failed
        coordinate is NOT hidden inside a bare warmed-count anymore, so the
        pipeline completion note can name it (Golden Rule #1).
    total
        Total jobs run (``warmed + failed + empty-but-clean``).
    """

    warmed: int
    failed: int
    total: int


def warm_coordinate_label(coord: Mapping[str, str | None]) -> str:
    """One human-readable warm-grid coordinate key for the progress logs.

    Renders the grid dict (``exchange`` / ``expiry_tier`` / ``expiry_track``)
    as e.g. ``"all"`` / ``"NSE|near"`` / ``"BSE|far|monthly"`` — the all-exchange
    scope (``None``) reads ``"all"``. Pure + deterministic (fixed field order),
    so a log line names the exact coordinate a slow/failed warm belongs to.
    """
    parts = [coord.get("exchange") or "all"]
    for field in ("expiry_tier", "expiry_track"):
        value = coord.get(field)
        if value is not None:
            parts.append(value)
    return "|".join(parts)


def warm_movers_snapshots(
    jobs: Sequence[Callable[[], int]],
    *,
    segment: str = "",
    labels: Sequence[str] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    job_deadline_s: float | None = None,
) -> MoversWarmResult:
    """Run each warm *job* (compute+cache one default snapshot); return the honest tally.

    Each job computes one asset's default snapshot via ``MoversSnapshotCache.get_or_compute``
    (so it is stored at the current generation) and returns that snapshot's candidate count.
    A job that produces ≥ 1 candidate counts toward ``warmed``; a job that warms an
    empty snapshot counts ``0`` (honest). A job that RAISES (a transient lake-read race) is
    logged, swallowed and counted in ``failed`` — the warm is best-effort and NEVER fails the
    already-successful pull that triggered it (the cached value, if any, is still valid for
    the generation). A job that is merely SLOW is bounded too (DEC-MOVERS-WARM-UNBLOCK-001):
    each job runs under the COMMON ``run_with_deadline`` freeze guard (*job_deadline_s*), and
    a timeout is counted ``failed`` + logged ``movers.warm.job_timeout`` — never an unbounded
    stall of the whole warm.

    Per-coordinate PROGRESS (DEC-MOVERS-WARM-PROGRESS-001 — the forensic fix for
    the silent 9+ minutes between ``pipeline.movers_warm.started`` and ``…done``
    that read as a hang on the operator's 2026-07-04 run): every coordinate
    emits one ``movers.warm.progress`` log line (``key i/N`` + segment + per-job
    and cumulative elapsed) — a BOUNDED cadence by construction (the warm grid
    is ≤ 39 coordinates per DEC-MOVERS-WARM-GRID-001) — plus the existing
    :func:`~brutex.observability.progress_beat.emit_progress_beat` per
    coordinate (the #1082 stall-watchdog seam), plus the OPTIONAL *on_progress*
    seam so a caller (the prebuild gate's honest note,
    DEC-PREBUILD-GATE-HONEST-NOTE-001) can surface ``i/N`` live. *on_progress*
    is best-effort: an observer that raises is logged and never breaks the warm.

    Parameters
    ----------
    jobs
        One zero-arg callable per default asset snapshot; each computes+caches its snapshot
        and returns its candidate count (``len(snapshots)`` / ``len(movers)``).
    segment
        The segment name for the progress logs (``"spot"`` / ``"futures"`` /
        ``"options"``); empty for legacy/anonymous callers.
    labels
        Optional per-job coordinate keys (same length/order as *jobs*, e.g. from
        :func:`warm_coordinate_label`); a missing/short list falls back to the
        1-based index — never a crash on a length mismatch.
    on_progress
        Optional ``(done_so_far, total)`` observer invoked BEFORE each job runs
        (``done_so_far`` counts completed jobs, so the first call is ``(0, N)``)
        and once more after the last job completes (``(N, N)``).
    job_deadline_s
        The per-job wall-clock deadline in seconds (DEC-MOVERS-WARM-UNBLOCK-001).
        ``None`` (the default) resolves :func:`resolve_warm_job_deadline_s` (env
        :data:`WARM_JOB_DEADLINE_ENV`, default :data:`DEFAULT_WARM_JOB_DEADLINE_S`
        = 300 s). Each job runs under the COMMON
        :func:`~brutex.adapters.groww.deadline.run_with_deadline` freeze guard; a
        job that blows the deadline is counted ``failed``, logged
        ``movers.warm.job_timeout``, and the warm CONTINUES. HONEST envelope
        (Golden Rule #10, same as #999): the abandoned worker is a daemon thread
        that CANNOT be killed — it dies with the process, and a late completion
        only caches the correct generation-keyed value.

    Returns
    -------
    MoversWarmResult
        The ``warmed`` / ``failed`` / ``total`` tally.
    """
    deadline = resolve_warm_job_deadline_s() if job_deadline_s is None else job_deadline_s
    total = len(jobs)
    warmed = 0
    failed = 0
    started = time.monotonic()

    def _observe(done: int) -> None:
        # Best-effort observer: a raising observer is a caller bug worth a log
        # line, never a reason to abort the (already best-effort) warm.
        if on_progress is None:
            return
        try:
            on_progress(done, total)
        except Exception as exc:  # pylint: disable=broad-except - best-effort observer; logged, never re-raised
            _LOG.warning("movers.warm.progress_observer_failed", error=str(exc))

    for index, job in enumerate(jobs, start=1):
        # Liveness beat per warmed coordinate (DEC-PIPELINE-STALL-HEARTBEAT-001): the
        # movers warm runs minutes after the last fetch heartbeat — keep the pull
        # stall watchdog honestly fed. No-op outside a dashboard pull; O(1).
        emit_progress_beat()
        _observe(index - 1)
        label = labels[index - 1] if labels is not None and index <= len(labels) else str(index)
        # Job-level start event (DEC-MOVERS-WARM-UNBLOCK-001 observability): the
        # 2026-07-04 incident's per-coordinate 170-256 s stalls were invisible
        # between coordinate-DONE lines — the start line + the done line bound
        # each job's elapsed from the log alone. BOUNDED cadence by construction
        # (≤ 39 coordinates per DEC-MOVERS-WARM-GRID-001).
        _LOG.info(
            "movers.warm.job_start",
            segment=segment,
            key=label,
            index=index,
            total=total,
            deadline_s=deadline,
        )
        job_started = time.monotonic()
        try:
            # The COMMON freeze guard (#910/#999 — REUSED, no copy): bounds a job
            # that is SLOW-but-never-raises (the unbounded-walk incident class).
            candidates = run_with_deadline(job, deadline)
        except DeadlineExceeded:
            failed += 1
            _LOG.warning(
                "movers.warm.job_timeout",
                segment=segment,
                key=label,
                index=index,
                total=total,
                deadline_s=deadline,
                elapsed_s=round(time.monotonic() - job_started, 3),
            )
            continue
        except Exception as exc:  # pylint: disable=broad-except - best-effort warm; logged, never re-raised
            failed += 1
            _LOG.warning(
                "movers.warm.job_failed",
                segment=segment,
                key=label,
                index=index,
                total=total,
                error=str(exc),
            )
            continue
        if candidates > 0:
            warmed += 1
        _LOG.info(
            "movers.warm.progress",
            segment=segment,
            key=label,
            index=index,
            total=total,
            candidates=candidates,
            job_elapsed_s=round(time.monotonic() - job_started, 3),
            elapsed_s=round(time.monotonic() - started, 3),
        )
    _observe(total)
    result = MoversWarmResult(warmed=warmed, failed=failed, total=total)
    _LOG.info(
        "movers.warm.segment_done",
        segment=segment,
        warmed=result.warmed,
        failed=result.failed,
        total=result.total,
        elapsed_s=round(time.monotonic() - started, 3),
    )
    return result
