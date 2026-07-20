"""India-VIX reference-stamp wiring — lake → stamper + trade-VIX aggregation.

DEC-VIX-STAMP-WIRE-001: wake the dormant per-trade India-VIX stamping
(DEC-VIX-TRADE-STAMP-001 — the ``vix_at_entry`` / ``vix_at_exit`` fields on
:class:`~brutex.metrics.types.ClosedTrade` and the ``reference_stamper`` seam
on :func:`~brutex.engine.trade_sim.simulate_trades`) by supplying the two
missing production pieces:

* :func:`build_vix_stamper` / :func:`vix_stamper_provider` — build ONE
  :class:`~brutex.engine.reference_series.TradeReferenceStamper` per run from
  the lake's ``NSE / INDEX / INDIAVIX`` series via the ONE COMMON slice-read
  law (:func:`~brutex.engine.lake_slice.load_lake_slice`: catalog-reconcile →
  L1 heal → window-bounded feed → keep-FIRST dedup). The build degrades
  HONESTLY to ``None`` (observe-only WARN, the run proceeds unstamped) when
  the lake carries no VIX data — a missing series never fabricates a stamp
  and never fails a sweep (Golden Rule #1). HONEST (round-2 review, the
  #935/#1014 dormant-surface class): production's once-per-run property is
  owned by the launchers' SINGLE EAGER :func:`build_vix_stamper` call
  (``cmd_sweep_all`` / the sweep-verify recount build once and thread the
  one object everywhere; the cascade replay builds NOTHING — its candidate
  re-simulations are discarded streams, round-6) — the lazy cached
  :func:`vix_stamper_provider` is a LIBRARY-level convenience with NO
  production consumer today (see its docstring).
* :func:`aggregate_trade_vix` — the ONE pure fold from a combo's stamped
  trades to its ``(vix_mean, vix_min, vix_max)`` summary trio.

## Which intervals feed the stamper (and why)

The stamper's series is the union of the VIX **1-minute** partition and the
derived **1-day** partition:

* every intraday-TF trade timestamp is a session-anchored bucket-open MINUTE
  (DEC-RESAMPLE-ANCHOR-002), so the 1-minute series serves all 20 intraday
  timeframes with one read;
* a ``1day``-swept trade's timestamps are **midnight-IST** daily anchors
  (``18:30`` UTC of the prior calendar day — never a session minute), which
  only the ``1day`` partition carries.

The two partitions can never collide on a timestamp (a trading-session minute
never falls at midnight IST), so the union is exact — the keep-FIRST gate in
:meth:`~brutex.engine.reference_series.ReferenceSeries.from_bars` stays a pure
duplicate guard.

## Scope law (CLAUDE.md §2 Rule 3)

India VIX is **REFERENCE ONLY**: the stamp is a downstream filter surface,
NEVER part of the condition vocabulary, NEVER a rankable field, NEVER in
the BLAKE3 run identity. A missing VIX bar stamps ``None`` — never a
fabricated, interpolated, or carried-forward level.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from brutex.data.models import CandleBar
    from brutex.engine.reference_series import TradeReferenceStamper
    from brutex.metrics.types import ClosedTrade

_LOG = structlog.get_logger(__name__)

#: The India-VIX lake partition coordinates. The symbol is the BARE lake
#: symbol-directory name (``bars/NSE/INDEX/INDIAVIX/…``) — the reference-only
#: ``UnderlyingSpec(exchange=NSE, underlying_symbol="INDIAVIX")`` of
#: :data:`brutex.universe.REFERENCE_ONLY_INDICES` (pinned by test), NOT the
#: vendor's prefixed ``NSE-INDIAVIX`` groww_symbol.
VIX_LAKE_SYMBOL: Final[str] = "INDIAVIX"
#: India VIX is an NSE volatility index — always ``NSE``, regardless of which
#: exchange the swept instrument trades on (SENSEX sweeps still stamp NSE VIX).
VIX_LAKE_EXCHANGE: Final[str] = "NSE"
#: Indices ride the neutral ``INDEX`` lake segment (CLAUDE.md §6).
VIX_LAKE_SEGMENT: Final[str] = "INDEX"
#: The two VIX partitions the stamper unions (see the module docstring).
VIX_STAMPER_INTERVALS: Final[tuple[str, str]] = ("1minute", "1day")

#: Cap on the per-day timestamps sampled into the one ``vix_daily_stale`` WARN.
_STALE_SAMPLE_CAP: Final[int] = 5


def aggregate_trade_vix(
    trades: Sequence[ClosedTrade],
) -> tuple[float | None, float | None, float | None]:
    """Fold a combo's stamped trades into its ``(vix_mean, vix_min, vix_max)`` trio.

    The ONE pure aggregation (CLAUDE.md §11 COMMON) every consumer of the
    per-combo VIX summary uses: pools EVERY non-``None`` stamp — both
    ``vix_at_entry`` AND ``vix_at_exit`` of every trade — and returns the
    pool's mean / min / max as full-precision floats. VIX is a volatility
    index level, not money: the values are NEVER rounded (CLAUDE.md §4 —
    rounding a metric breaks downstream analysis), and the mean uses
    :func:`math.fsum` so the fold is order-robust and deterministic.

    HONEST envelope (Golden Rule #1): a ``None`` stamp (no VIX bar at that
    instant) contributes NOTHING — it is skipped, never imputed. An empty
    *trades* sequence or an all-``None`` stamp set returns
    ``(None, None, None)`` — the honest "unknown" trio, never a fabricated
    ``0.0``. The mean is CLAMPED into ``[min, max]``: the true mean always
    lies there, but the computed float can land outside on a degenerate pool
    — on the primary path only the final DIVISION rounds, so the clamp snaps
    a ≤ 1-ulp artefact (e.g. three identical ``0.0108`` stamps divide to
    ``0.010799999999999999``); the clamp restores the mathematical order law
    ``min <= mean <= max`` and only ever snaps onto an ACTUALLY-ATTAINED
    level — never a fabricated one.

    OVERFLOW envelope (round-5 panel): each level is individually
    Float64-finite (the ``from_bars`` screens guarantee that), but the RUNNING
    SUM of a corrupt-grade pool can exceed the Float64 range — ``math.fsum``
    then RAISES ``OverflowError`` ("intermediate overflow in fsum"), and the
    call site (the finalist projection) sits OUTSIDE the grid tuple-fence, so
    an uncaught raise would abort a whole launch with a traceback that never
    names VIX. The fold therefore falls back to the pre-scaled sum
    ``fsum(x / n for x in levels)``: each term's magnitude is ``≤ max|x| / n``
    so every exact partial sum is bounded by ``max|x|`` — structurally
    overflow-free. On that fallback each term rounds individually, so the
    clamp guards the fold's accumulated rounding (a few ulp, not the primary
    path's ≤ 1) — still only ever snapping onto an attained level. Real VIX
    is ~9-90; this arm is reachable only from corrupt stored closes that are
    nonetheless Float64-representable (they round-trip the lake), which is
    exactly why it must degrade gracefully instead of aborting the sweep.

    Parameters
    ----------
    trades
        The combo's closed trades (stamped or not).

    Returns
    -------
    tuple[float | None, float | None, float | None]
        ``(mean, min, max)`` over every non-``None`` stamp, or
        ``(None, None, None)`` when no stamp exists. O(trades).
    """
    levels: list[float] = []
    for trade in trades:
        for stamp in (trade.vix_at_entry, trade.vix_at_exit):
            if stamp is not None:
                levels.append(float(stamp))
    if not levels:
        return (None, None, None)
    low, high = min(levels), max(levels)
    n = len(levels)
    try:
        mean = math.fsum(levels) / n
    except OverflowError:
        # Corrupt-grade magnitudes: every level is Float64-finite but the
        # exact running sum exceeds the Float64 range and fsum RAISES. The
        # pre-scaled fold is structurally overflow-free (each term ≤ max/n in
        # magnitude, every partial sum bounded by max) — see the docstring's
        # OVERFLOW envelope. Never an abort, never a fabricated value.
        mean = math.fsum(level / n for level in levels)
    # fsum is exactly rounded, but the final division (or the fallback's
    # per-term rounding) can land outside [low, high] on a degenerate pool —
    # clamp back (see the docstring).
    return (min(max(mean, low), high), low, high)


def warn_stale_vix_derived(
    minute_bars: Sequence[CandleBar],
    daily_bars: Sequence[CandleBar],
) -> int:
    """Observe-only freshness check of the derived VIX ``1day`` vs its 1-minute base.

    The ``1day`` VIX partition is DERIVED from the stored 1-minute by the pull
    pipeline's prebuild (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 /
    DEC-PIPELINE-AUTOPREBUILD-001). A 1-minute backfill AFTER the last prebuild
    can leave the stored daily stale; because the stamper serves ``1day``-swept
    trades FROM that stored partition, a drift is surfaced LOUDLY here — ONE
    ``reference_wire.vix_daily_stale`` WARNING — but never blocks the run
    (observe-only: stamping proceeds from the stored bars; the prebuild owns
    the fix, this read path never rewrites the lake).

    Mechanics: re-derive daily bars from *minute_bars* via the ONE canonical
    :func:`~brutex.data.spot_daily_resample.resample_1m_to_daily` (CLAUDE.md
    §11 COMMON — never a re-implemented aggregation) and compare OHLCV per
    stored daily bar. HONEST abstain envelope (Golden Rule #1 — never guess):

    * either side empty → nothing comparable → ``0``;
    * a stored daily bar on a window-EDGE IST date (the first or last date the
      minute window covers) → the window may have truncated that session's
      minutes, so the re-derivation is unprovable → abstained;
    * a stored daily bar whose date the re-derivation lacks (the minute window
      has no bars for that session) → unprovable → abstained.

    Pure READ path: this function touches only the two in-memory bar
    sequences — it never writes the lake, so
    :class:`~brutex.data.errors.LakeWriteHalt` is STRUCTURALLY IMPOSSIBLE here
    (documented rather than guarded by an uncoverable except branch).

    Parameters
    ----------
    minute_bars
        The VIX 1-minute slice (chronological, unique timestamps — the
        :func:`~brutex.engine.lake_slice.load_lake_slice` contract).
    daily_bars
        The stored derived VIX ``1day`` slice (midnight-IST-stamped).

    Returns
    -------
    int
        The number of stored daily bars whose OHLCV drifted from the fresh
        re-derivation (``0`` = fresh or abstained).
    """
    if not minute_bars or not daily_bars:
        return 0

    from datetime import timedelta

    import polars as pl

    from brutex.calendars.nse import IST
    from brutex.data.spot_daily_resample import resample_1m_to_daily

    frame = pl.DataFrame(
        {
            "ts_utc": [bar.timestamp for bar in minute_bars],
            "open": [float(bar.open) for bar in minute_bars],
            "high": [float(bar.high) for bar in minute_bars],
            "low": [float(bar.low) for bar in minute_bars],
            "close": [float(bar.close) for bar in minute_bars],
            "volume": [bar.volume for bar in minute_bars],
        }
    )
    # `before` = the day AFTER the last covered minute's IST date, so every
    # session present in the minute window is re-derivable; the window-EDGE
    # abstention below (not this cutoff) owns truncated-session honesty.
    last_ist_date = minute_bars[-1].timestamp.astimezone(IST).date()
    derived_by_ts = {
        bar.timestamp: bar
        for bar in resample_1m_to_daily(frame, before=last_ist_date + timedelta(days=1))
    }
    edge_dates = {minute_bars[0].timestamp.astimezone(IST).date(), last_ist_date}

    stale: list[datetime] = []
    for stored in daily_bars:
        if stored.timestamp.astimezone(IST).date() in edge_dates:
            continue  # window-edge session — possibly truncated, unprovable
        fresh = derived_by_ts.get(stored.timestamp)
        if fresh is None:
            continue  # no 1-minute base for this session — unprovable
        if (stored.open, stored.high, stored.low, stored.close, stored.volume) != (
            fresh.open,
            fresh.high,
            fresh.low,
            fresh.close,
            fresh.volume,
        ):
            stale.append(stored.timestamp)
    if stale:
        _LOG.warning(
            "reference_wire.vix_daily_stale",
            symbol=VIX_LAKE_SYMBOL,
            stale_days=len(stale),
            sample=[ts.isoformat() for ts in stale[:_STALE_SAMPLE_CAP]],
            remediation="re-run the pull pipeline prebuild to re-derive the 1day partition",
        )
    return len(stale)


def build_vix_stamper(
    *,
    lake_root: Path,
    start: datetime,
    end: datetime,
    catalog_path: Path | None = None,
) -> TradeReferenceStamper | None:
    """Build the run's India-VIX stamper from the lake, or ``None`` honestly.

    Reads the ``NSE / INDEX / INDIAVIX`` 1-minute + derived 1-day slices for
    the ``[start, end]`` window (BOUNDED — never a whole-lake scan) through the
    ONE COMMON :func:`~brutex.engine.lake_slice.load_lake_slice` law
    (catalog-reconcile before read → L1 heal → feed → keep-FIRST dedup), runs
    the observe-only :func:`warn_stale_vix_derived` freshness check, and
    returns ONE :class:`~brutex.engine.reference_series.TradeReferenceStamper`
    over the union series.

    HONEST degrade (Golden Rule #1): a lake with NO VIX partition on disk
    (``reference_wire.vix_lake_missing`` — checked BEFORE any catalog I/O, so a
    VIX-less lake never even gains a ``catalog.duckdb``) or with no VIX bar in
    the window (``reference_wire.vix_lake_empty``) WARNs observe-only and
    returns ``None`` — the run proceeds with every trade honestly UNSTAMPED
    (``vix_at_entry = vix_at_exit = None``), never a fabricated series and
    never a failed sweep.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    start, end
        The tz-aware run window (the swept slice's window) bounding the read.
    catalog_path
        The catalog DuckDB path (default ``<lake_root>/catalog.duckdb``).

    Returns
    -------
    TradeReferenceStamper | None
        The stamper, or ``None`` when the lake carries no usable VIX data.
    """
    from brutex.data.catalog import Catalog
    from brutex.data.partition import BAR_DIR_NAME
    from brutex.engine.lake_slice import load_lake_slice
    from brutex.engine.reference_series import TradeReferenceStamper

    vix_dir = lake_root / BAR_DIR_NAME / VIX_LAKE_EXCHANGE / VIX_LAKE_SEGMENT / VIX_LAKE_SYMBOL
    if not (vix_dir.is_dir() and any(vix_dir.rglob("*.parquet"))):
        _LOG.warning(
            "reference_wire.vix_lake_missing",
            symbol=VIX_LAKE_SYMBOL,
            vix_dir=str(vix_dir),
            detail="no VIX partition on disk — trades will carry vix_at_entry/exit=None",
        )
        return None

    catalog_file = catalog_path if catalog_path is not None else lake_root / "catalog.duckdb"
    bars_by_interval: dict[str, list[CandleBar]] = {}
    with Catalog.open(catalog_file) as catalog:
        for interval in VIX_STAMPER_INTERVALS:
            bars_by_interval[interval] = load_lake_slice(
                lake_root=lake_root,
                catalog=catalog,
                exchange=VIX_LAKE_EXCHANGE,
                segment=VIX_LAKE_SEGMENT,
                symbol=VIX_LAKE_SYMBOL,
                interval=interval,
                start=start,
                end=end,
                dedup_source=f"reference_wire:{VIX_LAKE_SYMBOL}/{interval}",
            )
    minute_bars, daily_bars = (bars_by_interval[i] for i in VIX_STAMPER_INTERVALS)
    if not minute_bars and not daily_bars:
        _LOG.warning(
            "reference_wire.vix_lake_empty",
            symbol=VIX_LAKE_SYMBOL,
            start=start.isoformat(),
            end=end.isoformat(),
            detail=(
                "VIX partition holds no bar in the window — trades unstamped. "
                "If parquet files DO exist on disk, check for preceding "
                "catalog.reconcile.parquet_skip WARNings: a corrupt/unreadable "
                "partition file is skipped loudly by the reconcile and presents "
                "here as an empty window (repair the file, don't re-pull)"
            ),
        )
        return None
    warn_stale_vix_derived(minute_bars, daily_bars)
    _LOG.info(
        "reference_wire.vix_stamper_built",
        symbol=VIX_LAKE_SYMBOL,
        minute_bars=len(minute_bars),
        daily_bars=len(daily_bars),
    )
    return TradeReferenceStamper.from_vix_bars([*minute_bars, *daily_bars])


def vix_stamper_provider(
    *,
    lake_root: Path,
    start: datetime,
    end: datetime,
    catalog_path: Path | None = None,
) -> Callable[[], TradeReferenceStamper | None]:
    """Return a lazy, once-per-run cached provider of the India-VIX stamper.

    The zero-arg closure defers :func:`build_vix_stamper` until the FIRST call
    (a consumer that never simulates trades never reads the VIX lake) and
    caches the outcome — INCLUDING an honest ``None`` — so the lake read
    happens AT MOST ONCE per closure regardless of how many slices/tuples
    consume the stamper. Per-run closure state only, no global mutable state
    (CLAUDE.md §4).

    HONEST envelope (Golden Rule #10, round-2 review — the #935/#1014
    dormant-surface class, stated rather than hidden): NO production path
    routes through this provider today. Every production launcher binds
    EAGERLY — ``cmd_sweep_all`` and the sweep-verify recount call
    :func:`build_vix_stamper` once at launch and hand stage-3 a
    ``stamper_for=lambda: reference_stamper`` over that already-built object
    (the cascade/dashboard manifest replay builds NOTHING since round-6
    deleted its dead ``vix_stamper_for_manifest`` seam) — so the
    production once-per-run property is owned by those single builds, NOT by
    this closure, and the lazy never-read-if-never-simulating benefit is
    unrealised in production (a library-level convenience for embedders).
    Also NOT thread-safe (a plain check-then-set closure): a future
    multi-threaded consumer could double-build — idempotent and harmless
    (same lake, same window) but not "at most once" under races; production
    launch paths are single-threaded at build time.

    Parameters
    ----------
    lake_root, start, end, catalog_path
        Forwarded verbatim to :func:`build_vix_stamper` on first call.

    Returns
    -------
    Callable[[], TradeReferenceStamper | None]
        The cached provider.
    """
    built = False
    stamper: TradeReferenceStamper | None = None

    def provide() -> TradeReferenceStamper | None:
        nonlocal built, stamper
        if not built:
            stamper = build_vix_stamper(
                lake_root=lake_root, start=start, end=end, catalog_path=catalog_path
            )
            built = True
        return stamper

    return provide


__all__ = [
    "VIX_LAKE_EXCHANGE",
    "VIX_LAKE_SEGMENT",
    "VIX_LAKE_SYMBOL",
    "VIX_STAMPER_INTERVALS",
    "aggregate_trade_vix",
    "build_vix_stamper",
    "vix_stamper_provider",
    "warn_stale_vix_derived",
]
