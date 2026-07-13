"""ONE alias-aware sweep-slice loader — spot verbatim, futures roll-stitched.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001: every sweep-family bar mouth (``sweep-all``
/ ``sweep`` / ``sweep-verify --recount`` / ``verify-lookahead`` / the cascade's
``load_bars_for_manifest`` / the options-sim replay) previously inlined the
same reconcile + :class:`~brutex.feed.replay.ReplayFeed` block. This module
extracts that block into ONE function, :func:`load_sweep_slice` (CLAUDE.md §11
COMMON), and makes it ALIAS-AWARE:

* a **spot / verbatim-dir symbol** takes EXACTLY the moved block — optional
  catalog reconcile, then one ``ReplayFeed`` read — byte-identical to the
  inline copies it replaces (fixture-parity-pinned);
* the **NIFTY-futures series alias**
  (:data:`brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL`) is resolved via
  :func:`brutex.data.futures_series.resolve_futures_series` into per-contract
  segments and stitched with an **exact IST-date ownership cut** —
  ``bar.timestamp.astimezone(IST).date()``, the SAME conversion
  ``stage1a.push`` keys on, so the loader's cut and the engine's roll cursor
  can never disagree about which contract owns a bar (both derive from the
  ONE ``roll_schedule`` of the same resolution).

The IST-DATE cut (never a ``09:15`` clamp) means muhurat-evening bars and
special-weekend sessions are owned by their own date, and the derived FNO
``1day`` bar (midnight-IST-stamped, living in the UTC month D−1 file) cuts
identically — the Catalog's ``first_ts``/``last_ts`` overlap predicate absorbs
the D−1 drift unchanged.

Duplicates across boundaries are STRUCTURALLY IMPOSSIBLE (disjoint IST-date
ownership) and a boundary monotonicity raise guards the impossible; WITHIN a
contract the catalog's keep-first dedup stands (DEC-LAKE-READ-UNIQUE-001).

Honest envelope (Golden Rule #10): loading is O(files overlapping the window)
per contract — the same envelope as ``Catalog.get_bars`` — and per-bar access
on the returned list stays O(1); no O(1)-total claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

import structlog

from brutex.calendars.nse import IST
from brutex.data.futures_series import (
    SERIES_EXCHANGE,
    SERIES_SEGMENT,
    FuturesSeriesError,
    FuturesSeriesResolution,
    is_futures_series_symbol,
    resolve_futures_series,
)
from brutex.feed.replay import ReplayFeed

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable
    from pathlib import Path

    from brutex.data.catalog import Catalog
    from brutex.data.catalog_reconcile import ReconcileResult
    from brutex.data.models import CandleBar

__all__ = [
    "load_sweep_slice",
    "require_series_coordinates",
]

_LOG = structlog.get_logger("brutex.feed.series_load")


def _now_utc() -> datetime:
    """The wall-clock seam for the 1day live-edge rule (monkeypatchable in tests)."""
    return datetime.now(tz=UTC)


def require_series_coordinates(symbol: str, *, exchange: str, segment: str) -> None:
    """Refuse a foreign launch ``(exchange, segment)`` for the futures alias.

    The ONE coordinate pin (review round-7 finding 3, hoisted to a shared
    helper by review round-8 finding 9): the alias resolves against the
    pinned ``bars/NSE/FNO`` tree while the per-contract reads use the
    caller's coordinates verbatim — an override-admitted foreign coordinate
    (``--allow-out-of-scope`` past the scope gate's NSE/FNO refusal) would
    read every contract under the FOREIGN tree, find zero bars, and raise a
    misleading "EMPTY — re-pull" (or, on a box with no pinned tree at all,
    the resolver's "no NSE/FNO lake tree — run the pipeline pull")
    remediation. Every alias mouth calls this BEFORE
    :func:`~brutex.data.futures_series.resolve_futures_series` so the
    refusal names the REAL problem (the coordinate) ahead of any resolution
    or lake I/O; :func:`load_sweep_slice` re-checks as defence in depth.

    Raises
    ------
    FuturesSeriesError
        When ``(exchange, segment)`` differs from the pinned pair.
    """
    if (exchange, segment) != (SERIES_EXCHANGE, SERIES_SEGMENT):
        # {exchange!r}/{segment!r}: operator-typed CLI input echoed to stderr —
        # repr-quote so an embedded \n/ESC can never forge a '[brutex]' line
        # (review round-8 finding 2; the #907/#919 repr-naming convention).
        raise FuturesSeriesError(
            f"the NIFTY-futures series alias {symbol!r} lives at the pinned "
            f"{SERIES_EXCHANGE}/{SERIES_SEGMENT} lake coordinates, but this launch "
            f"reads {exchange!r}/{segment!r} — the coordinate is wrong, not the "
            f"stored data; re-launch with --exchange {SERIES_EXCHANGE} --segment "
            f"{SERIES_SEGMENT} (DEC-FUTURES-ADDRESSING-PIN-001)"
        )


def _expected_bar_time_in_window(*, interval: str, seg_start: datetime, seg_end: datetime) -> bool:
    """True iff ``[seg_start, seg_end]`` can contain ANY stored bar timestamp.

    Judged per IST date against the REAL NSE session calendar
    (:func:`brutex.calendars.nse.get_session_checked` — muhurat + special
    weekend sessions included): an intraday interval labels its bars on the
    resampler's ACTUAL fixed-arithmetic grid — ``09:15 IST + k·tf`` on the
    bar's IST date (DEC-RESAMPLE-ANCHOR-002; DEC-SERIES-LOAD-GRID-HONEST-001
    corrected this helper's model, which anchored the grid at the SESSION
    OPEN: on any session whose open offset from 09:15 is not a TF multiple —
    every Muhurat evening for non-divisor TFs (a 30m grid on an 18:00 open
    stores 17:45/18:15/18:45, never 18:00/18:30), the 2025 13:45 open @4m —
    the predicted labels did not exist on disk, so a healthy lake drew the
    loud false 'EMPTY — re-pull' refusal (Fragment A) while the mirror
    sliver holding the real off-open label was skipped (Fragment B); the
    grids coincide on every regular/special-weekend 09:15-open session, so
    those are byte-identical — parity-pinned). A grid label ``t`` is
    expectable iff ``k ≥ 0``, ``seg_start ≤ t ≤ seg_end``, and the bucket
    OVERLAPS the session (``t < close`` and ``t + tf > open``), while the
    derived FNO ``1day`` bar is stamped at its session date's midnight IST —
    but that bar SUMMARISES the whole session and is written by the
    POST-session pipeline prebuild, so an in-window midnight stamp
    (``seg_start <= stamp <= seg_end`` — the READ window serves exactly that
    bar on a healthy lake) counts as an expectable instant only when the bar
    can already EXIST: the window reaches the day's session CLOSE
    (``seg_end >= close``) OR the close has already PASSED on the wall clock
    (``close <= now`` — deep history, where the prebuild ran long ago and an
    empty read IS a data defect; review round-8 finding 8: the round-6 rule
    skipped a genuinely missing HISTORICAL 1day bar the read window would
    have served, silently shifting ``data_snapshot_hash`` vs a healthy
    lake). Only the TRUE live edge — a window ending before a close that is
    still in the future (the morning-after-expiry pre-open sliver of a
    bare-date ``--end`` — ``[00:00 → 05:30 IST]`` on the successor
    contract's FIRST owned day) — demands a bar the prebuild structurally
    cannot have derived yet, where the "re-pull / run the prebuild"
    remediation would be impossible until the session completes (review
    round-6 finding 3 — the 1day residual of the round-1/round-3 boundary
    class). A window that holds NO such instant (a
    pre-open / post-close boundary sliver, a weekend/holiday run, a
    sub-bucket fragment of a coarse TF — a 4hour day carries ONLY the 09:15 +
    13:15 labels, so a 13:30→midnight expiry-day fragment overlaps the
    session yet cannot hold a bar) genuinely cannot hold bars, so an empty
    read there is NOT a data defect (review round-1 findings 2/16/22 +
    round-3 finding 7 — the false "re-pull" refusal on a legitimate
    roll-boundary window). On a session whose open is NOT on the 09:15 grid
    the first in-session bucket's label PRECEDES the open (the stored 17:45
    @30m / 17:15 @60m / 17:59 @2m Muhurat labels; 13:43 @4m for the 2025
    13:45 open — the B-S1 table) — such a label IS expectable when in-window
    (the Fragment-B mirror flips skip→read: strictly LESS refusal on healthy
    data, never more).

    HONEST abstain: an out-of-coverage date (2027+ weekday) raises
    ``HolidayDataUnavailableError`` — this helper then returns ``True`` so the
    caller KEEPS its loud empty-partition refusal rather than silently
    skipping a window the calendar cannot vouch for; likewise an *interval*
    that is not a known lake TF label (its grid cannot be modelled). O(days in
    the window intersection), bounded by one contract's ownership span.
    """
    from brutex.calendars.nse import HolidayDataUnavailableError, get_session_checked
    from brutex.data.timeframes import trading_minutes_for_lake_interval

    if seg_end < seg_start:  # a degenerate intersection holds no instants
        return False
    step: timedelta | None = None
    if interval != "1day":
        try:
            step = timedelta(minutes=trading_minutes_for_lake_interval(interval))
        except ValueError:
            return True  # unknown TF label → cannot model its grid → keep the loud refusal
    # The 1day live-edge clock (review round-8 finding 8): read once per call;
    # consulted only to distinguish deep history (close already passed → the
    # prebuild owes the bar) from the true live edge. Never part of run
    # identity — it only decides skip-vs-refuse for EMPTY reads, and a
    # successful load never reaches this helper.
    now = _now_utc()
    day = seg_start.astimezone(IST).date()
    last_day = seg_end.astimezone(IST).date()
    while day <= last_day:
        try:
            session = get_session_checked(day)
        except HolidayDataUnavailableError:
            return True  # cannot prove bar-less → keep the loud refusal
        if session is not None:
            if step is None:  # the derived FNO 1day bar — midnight-IST stamp
                stamp = datetime.combine(day, time.min, tzinfo=IST)
                close_dt = datetime.combine(day, session.close_time_ist, tzinfo=IST)
                # Expectable when the window holds the stamp (the READ window
                # serves exactly that bar on a healthy lake — read and
                # refusal must agree, round-8 finding 8) AND the bar can
                # already exist: the window reaches the session close the
                # bar summarises (``stamp < close`` so ``seg_end >= close``
                # implies ``stamp <= seg_end``), OR the close has already
                # passed on the wall clock (deep history — the post-session
                # prebuild ran long ago, so an empty read is a REAL gap).
                # Only the true live edge (close still in the future) skips
                # (round-6 finding 3 — the morning-after-expiry sliver).
                if seg_start <= stamp <= seg_end and (seg_end >= close_dt or close_dt <= now):
                    return True
            else:
                open_dt = datetime.combine(day, session.open_time_ist, tzinfo=IST)
                close_dt = datetime.combine(day, session.close_time_ist, tzinfo=IST)
                # Bar labels live on the resampler's ACTUAL fixed grid —
                # ``09:15 IST + k·tf`` on the bar's IST date, k ≥ 0
                # (DEC-RESAMPLE-ANCHOR-002 arithmetic; the pre-
                # DEC-SERIES-LOAD-GRID-HONEST-001 model anchored at the
                # SESSION open, which diverges on any open whose offset from
                # 09:15 is not a TF multiple — every Muhurat evening on a
                # non-divisor TF). Expectable iff a grid label t satisfies
                # seg_start ≤ t ≤ seg_end AND the bucket [t, t+tf) OVERLAPS
                # the session (t < close and t + tf > open — the resampler
                # only ever labels buckets that hold in-session bars). Find
                # the SMALLEST such t in closed form:
                anchor = datetime.combine(day, time(9, 15), tzinfo=IST)
                # (a) the first grid label whose bucket overlaps the session:
                #     smallest t = anchor + k·step with t > open − step,
                #     via floor-division on timedeltas (exact, no float
                #     drift; floor(y/step) + 1 is the strictly-greater grid
                #     rank for any y).
                k_overlap = (open_dt - step - anchor) // step + 1
                # (b) the first grid label at/after the fragment start:
                #     ceil((x − anchor)/step) as −floor((anchor − x)/step).
                x = max(seg_start, anchor)
                k_window = -((anchor - x) // step)
                k = max(k_overlap, k_window, 0)  # k ≥ 0: labels start at 09:15
                first_label = anchor + k * step
                if first_label <= seg_end and first_label < close_dt:
                    return True
        day += timedelta(days=1)
    return False


def _reconcile(
    *,
    cat: Catalog,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    on_reconcile: Callable[[ReconcileResult], None] | None,
    stat_cache: bool,
) -> None:
    """Reconcile ONE ``(symbol, interval)`` coordinate from the lake into *cat*.

    The same lazy-import + call the inline mouths used
    (DEC-CATALOG-RECONCILE-WIRE-001); *on_reconcile* is the optional caller
    hook (the cascade's L1 heal rides it — DEC-LAKE-HEAL-001); *stat_cache*
    is the DEC-SWEEP-RECONCILE-STATCACHE-001 warm-launch fast path
    (the launcher passes True; the heal/census callers keep False).
    """
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

    result = reconcile_catalog_from_lake(
        lake_root=lake_root,
        catalog=cat,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        stat_cache=stat_cache,
    )
    if on_reconcile is not None:
        on_reconcile(result)


def _read_symbol(
    *,
    cat: Catalog,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list[CandleBar]:
    """One ``ReplayFeed`` read for one verbatim lake dir — the moved inline block."""
    feed = ReplayFeed(
        catalog=cat,
        lake_root=lake_root,
        exchange=exchange,
        segment=segment,
        symbols=[symbol],
        interval=interval,
        start=start,
        end=end,
    )
    return [event.bar for event in feed]


def load_sweep_slice(
    *,
    cat: Catalog,
    lake_root: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    reconcile: bool = True,
    resolution: FuturesSeriesResolution | None = None,
    on_reconcile: Callable[[ReconcileResult], None] | None = None,
    stat_cache: bool = False,
) -> list[CandleBar]:
    """Load ONE sweep ``(symbol, interval)`` slice — spot verbatim, alias stitched.

    Parameters
    ----------
    cat
        An open :class:`~brutex.data.catalog.Catalog` (caller-owned).
    lake_root
        The lake root directory.
    exchange, segment
        The launch coordinates. A verbatim symbol reads them as-is; the alias
        REQUIRES the pinned ``NSE``/``FNO`` pair and refuses any other up
        front (review round-7 finding 3): the scope gate already refuses a
        foreign coordinate, but its CLI-only ``--allow-out-of-scope``
        research override can admit one — the resolver would then resolve
        against the pinned ``bars/NSE/FNO`` tree while every contract READ
        went to the foreign tree, found zero bars, and mis-remediated as
        "EMPTY — re-pull" (the wrong problem named).
    symbol
        A verbatim lake dir symbol, or the NIFTY-futures series alias
        (:func:`~brutex.data.futures_series.is_futures_series_symbol`).
    interval
        The lake interval label.
    start, end
        Inclusive tz-aware replay window (the :class:`ReplayFeed` contract).
    reconcile
        When True (default), reconcile the catalog from the lake for each
        physical dir before reading (``cmd_sweep`` passes False — its
        no-reconcile law is preserved).
    resolution
        A pre-computed series resolution (``sweep-all`` resolves ONCE up
        front and passes it down per interval); ``None`` resolves here.
    on_reconcile
        Optional hook fired with each :class:`ReconcileResult` (the cascade's
        L1 heal seam). Only fired when *reconcile* is True.
    stat_cache
        DEC-SWEEP-RECONCILE-STATCACHE-001 pass-through to
        :func:`~brutex.data.catalog_reconcile.reconcile_catalog_from_lake`: a
        WARM (byte-unchanged) partition skips the redundant full-bytes sha256
        on a stat-only ``(mtime_ns, size, ino)`` token match — the loaded
        bars, hence ``data_snapshot_hash`` / run identity, are byte-identical
        (``get_bars`` re-reads the file content). The sweep-all LAUNCHER
        passes True (it discards findings + heals nothing); the cascade
        replay / heal / census callers keep the default False so the L1
        classification is never suppressed where a healer consumes it.

    Returns
    -------
    list[CandleBar]
        The chronologically ordered slice (possibly empty for a spot symbol
        with no bars — the caller's preflight owns that refusal).

    Raises
    ------
    FuturesSeriesError
        Futures alias only: a launch coordinate other than the pinned
        ``NSE``/``FNO`` (named as a COORDINATE error, never a data one),
        resolution refusals (interior hole etc.), a
        per-``(contract, interval)`` ZERO-BAR owned window whose in-window
        span holds at least one expectable session instant (dir exists but
        the requested interval is empty — distinct message from a missing
        contract; a bar-less boundary sliver — pre-open/post-close edges,
        a holiday/weekend run after an expiry, a degenerate intersection, a
        sub-bucket coarse-TF fragment holding no grid label, a LIVE-EDGE
        ``1day`` fragment ending before its only owned session's CLOSE
        while that close is still in the wall-clock future (the bar the
        post-session prebuild cannot yet have derived — a DEEP-HISTORY
        pre-close fragment whose midnight stamp the read window serves
        REFUSES instead, review round-8 finding 8) — is SKIPPED with
        one ``series_load.boundary_segment_no_session`` WARN instead of a
        false refusal), or a boundary monotonicity break (never silent).
    """
    if not is_futures_series_symbol(symbol):
        # Spot / verbatim-dir path: EXACTLY the inline optional-reconcile +
        # ReplayFeed block the sweep mouths carried — moved, not rewritten.
        if reconcile:
            _reconcile(
                cat=cat,
                lake_root=lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                on_reconcile=on_reconcile,
                stat_cache=stat_cache,
            )
        return _read_symbol(
            cat=cat,
            lake_root=lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
        )

    # Coordinate pin (review round-7 finding 3): refuse a foreign coordinate
    # up front, before any resolution or lake I/O — the ONE shared helper the
    # CLI mouths ALSO call ahead of resolve_futures_series (review round-8
    # finding 9), re-checked here as defence in depth.
    require_series_coordinates(symbol, exchange=exchange, segment=segment)

    res = (
        resolution
        if resolution is not None
        else resolve_futures_series(
            lake_root,
            start=start.astimezone(IST).date(),
            end=end.astimezone(IST).date(),
        )
    )
    bars: list[CandleBar] = []
    for seg in res.segments:
        if reconcile:
            _reconcile(
                cat=cat,
                lake_root=lake_root,
                exchange=exchange,
                segment=segment,
                symbol=seg.contract_symbol,
                interval=interval,
                on_reconcile=on_reconcile,
                stat_cache=stat_cache,
            )
        # Feed-bound to the segment window (UTC of IST 00:00 own_start →
        # IST 23:59:59.999999 own_end), intersected with the operator window.
        # Bounds are normalised to UTC — the lake stores UTC timestamps and
        # the catalog's window filter compares dtype-strictly.
        seg_start = max(start, datetime.combine(seg.own_start, time.min, tzinfo=IST)).astimezone(
            UTC
        )
        seg_end = min(end, datetime.combine(seg.own_end, time.max, tzinfo=IST)).astimezone(UTC)
        if seg_end < seg_start:  # a degenerate intersection cannot own bars
            seg_bars: list[CandleBar] = []
        else:
            # …then the EXACT authoritative IST-date ownership cut — the SAME
            # ``astimezone(IST).date()`` conversion ``stage1a.push`` keys on.
            seg_bars = [
                bar
                for bar in _read_symbol(
                    cat=cat,
                    lake_root=lake_root,
                    exchange=exchange,
                    segment=segment,
                    symbol=seg.contract_symbol,
                    interval=interval,
                    start=seg_start,
                    end=seg_end,
                )
                if seg.own_start <= bar.timestamp.astimezone(IST).date() <= seg.own_end
            ]
        if not seg_bars:
            if not _expected_bar_time_in_window(
                interval=interval, seg_start=seg_start, seg_end=seg_end
            ):
                # The in-window slice of this segment's owned span holds NO
                # expectable bar instant (a pre-open/post-close boundary
                # sliver, a holiday/weekend run right after a roll, a
                # degenerate intersection, a sub-bucket coarse-TF fragment
                # straddling no grid label, or the TRUE 1day live edge — a
                # day-only `--end` the morning after an expiry whose session
                # close is still in the future, the derived bar the
                # post-session prebuild cannot yet have written): an empty
                # read is EXPECTED, not a data defect. Skip the boundary
                # segment with ONE WARN instead of a false "re-pull"
                # whole-sweep refusal (review round-1 findings 2/16/22 +
                # round-3 finding 7 + round-6 finding 3); the loud refusal
                # below stands for any span the calendar says SHOULD hold
                # bars — including a DEEP-HISTORY 1day span whose midnight
                # stamp the read window serves (review round-8 finding 8:
                # the session close has already passed, so the prebuild owes
                # the bar and a silent skip would shift data_snapshot_hash).
                _LOG.warning(
                    "series_load.boundary_segment_no_session",
                    contract=seg.contract_symbol,
                    interval=interval,
                    window_start=seg_start.astimezone(IST).isoformat(),
                    window_end=seg_end.astimezone(IST).isoformat(),
                )
                continue
            hint = (
                ""
                if reconcile
                else " (this mouth reads without a catalog reconcile — run "
                "'brutex sweep-all' or reconcile the catalog first if the "
                "partition exists on disk)"
            )
            raise FuturesSeriesError(
                f"stored partition {seg.contract_symbol!r} / {interval!r} is EMPTY "
                f"over its owned window [{seg.own_start.isoformat()} → "
                f"{seg.own_end.isoformat()}] although the window holds at least "
                f"one expectable bar instant on the resampler's fixed 09:15-IST "
                f"bucket grid — stored data is MISSING: re-pull the contract, "
                f"then run the pipeline prebuild (dashboard ▶ Pull · full "
                f"pipeline; DEC-SERIES-LOAD-GRID-HONEST-001 — a re-pull/prebuild "
                f"never MOVES grid labels, so this refusal only fires for a "
                f"genuine gap, never for an off-open Muhurat label){hint}"
            )
        if bars and seg_bars[0].timestamp <= bars[-1].timestamp:
            raise FuturesSeriesError(
                f"boundary monotonicity broken: first bar of {seg.contract_symbol!r} "
                f"({seg_bars[0].timestamp.isoformat()}) is not after the previous "
                f"segment's last bar ({bars[-1].timestamp.isoformat()}) — the lake "
                "violates disjoint IST-date ownership (a genuine bug, never silent)"
            )
        bars.extend(seg_bars)
    return bars
