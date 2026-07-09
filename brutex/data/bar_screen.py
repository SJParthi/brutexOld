"""ONE COMMON screened bar reader for the dashboard readers (DEC-DASHBOARD-READ-SCREEN-001).

The engine screens stored bars at its read boundary
(:func:`brutex.feed.replay.arrow_table_to_bars` — positivity + OHLC-ordering +
the :class:`~brutex.data.bad_tick.BadTickScreen`), but the two dashboard bar
readers (``/movers`` :mod:`~brutex.dashboard.movers_snapshot` /
:mod:`~brutex.dashboard.movers_futures`, and ``/db``
:mod:`~brutex.dashboard.db_view`) read the SAME stored partitions RAW. An ``×10``
fat-finger spike (a self-consistent SCALED bar — positive + ordered — the
:class:`~brutex.data.bad_tick.BadTickScreen` class) is DROPPED by the engine but
SERVED by the dashboard, so ``/movers`` shows a fabricated Top-Value / a spurious
%-move (and :class:`~brutex.dashboard.summary_cache.MoversSnapshotCache` caches
it) while ``/db`` shows the raw spike row — a "split-brain" where a corrupt bar
renders one number on ``/movers`` and a different one on ``/db``, and both
diverge from what the engine refuses.

This module COMPOSES the EXISTING PUBLIC predicates — it invents no screening
logic (Golden Rule #11 COMMON):

* :class:`~brutex.data.bad_tick.BadTickScreen` / :func:`~brutex.data.bad_tick.is_bad_tick`
  / :func:`~brutex.data.bad_tick.bad_tick_screen_enabled` (the ``BRUTEX_BAD_TICK_SCREEN``
  kill switch) — the stateful, causal, per-session bad-tick state machine the
  engine drives, reused VERBATIM here so a corrupt bar is dropped identically;
* :func:`~brutex.data.price_sanity.price_row_insane` — the scalar twin of the
  vectorized :func:`~brutex.data.price_sanity.price_violation_expr`
  (positivity + ordering + a scalar-only NaN arm), the plane the engine
  REFUSES: ``arrow_table_to_bars`` raises ``ValueError`` on a non-positive /
  mis-ordered bar, and the ``CandleBar`` constructor raises
  ``decimal.InvalidOperation`` on a NaN price (the NaN arm keeps outcome-parity
  — neither surface ever serves a NaN bar; see ``price_row_insane``'s
  deliberate-asymmetry note).

:class:`SessionBarScreen` is the single causal per-session screen; two thin
wrappers drive it — :func:`screen_frame` for the polars readers (``/movers``)
and :func:`screen_rows` for the ``/db`` DuckDB page (seedable for cross-page
bad-tick left-edge correctness). An anti-divergence property test pins
:class:`SessionBarScreen`'s KEEP-set equal to
:func:`~brutex.feed.replay.arrow_table_to_bars`'s kept set, so the two call
sites can never diverge from each other OR from the engine, and both inherit any
future internal change to first-bar handling (DEC-BADTICK-HEAD-SEED-001)
automatically.

Disposition (mirrors the engine, with ONE sanctioned divergence):

* **bad-tick** → DROP + one WARN per session (exactly the engine's behaviour —
  the drop is the split-brain fix);
* **insane** (positivity / ordering) → DROP + count. The engine RAISES on an
  insane bar (the whole-symbol quarantine); the dashboard instead DROPS it +
  counts (``insane_dropped``) so (a) ``/movers`` and ``/db`` AGREE with each
  other, (b) the corrupt bar is never SERVED (the engine's "refuse to serve"
  holds for that bar), and (c) the good remainder still renders. This is the ONE
  documented, sanctioned divergence from the engine's blast radius.

Clean-data byte-identity (the ship gate): on a lake with no insane + no
bad-tick bars every row classifies KEEP, so both wrappers return the SAME input
object (they only ever DROP — never edit / reorder / re-type), and every
existing ``/movers`` + ``/db`` golden passes byte-identical. HONEST COST: the
screen itself is an O(rows) Python pass per read (column materialisation +
per-row classify) even on clean data — same-object OUTPUT, not zero WORK
(bench-pinned linear; the polars-vectorized / Rust rewrite that would earn a
flat claim — the engine's vectorized ``price_violation_expr`` is the template —
is a named follow-up, not claimed).
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, cast

import structlog

from brutex.data.bad_tick import BadTickScreen, bad_tick_screen_enabled
from brutex.data.price_sanity import price_row_insane

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from datetime import date

    import polars as pl

_LOG = structlog.get_logger(__name__)

#: The polars time-zone region used to derive a bar's IST SESSION date (matches
#: the dashboard readers' own ``dt.convert_time_zone`` calls, CLAUDE.md §11).
_IST_TZ: Final[str] = "Asia/Kolkata"


class BarVerdict(Enum):
    """The per-bar screening verdict returned by :meth:`SessionBarScreen.classify`."""

    #: Serve the bar (positive + ordered + not a bad tick).
    KEEP = "keep"
    #: Drop a bad tick (scaled spike vs the previous in-session close).
    DROP_BAD_TICK = "drop_bad_tick"
    #: Drop a price-insane bar (non-positive price or broken OHLC ordering).
    DROP_INSANE = "drop_insane"


def session_date_from_ts(ts: datetime) -> date:
    """Return *ts*'s SESSION date — the bad-tick per-session grouping key.

    A tz-AWARE instant (the ``/movers`` frames + the engine bars) is converted
    to IST first, exactly as :func:`~brutex.feed.replay.arrow_table_to_bars`
    does (``ts.astimezone(IST).date()``); a tz-NAIVE instant (a DuckDB
    ``TIMESTAMP`` page cell, a UTC instant) uses its own date. Either way an NSE
    session (09:15-15:30 IST = 03:45-10:00 UTC) never crosses a date boundary in
    the chosen zone, so both yield a stable per-session key.
    """
    if ts.tzinfo is not None:
        from zoneinfo import ZoneInfo

        return ts.astimezone(ZoneInfo(_IST_TZ)).date()
    return ts.date()


class SessionBarScreen:
    """Stateful, causal, per-session bar screen composing the public predicates.

    Fed bars in chronological (ascending) order within each session; decides per
    bar KEEP / DROP_BAD_TICK / DROP_INSANE. Insane bars are dropped + counted and
    do NOT advance the bad-tick baseline (like a dropped spike), so the bad-tick
    comparison always runs against the last KEPT, positive, ordered close —
    exactly the engine's ordering (positivity BEFORE the bad-tick compare).

    Parameters
    ----------
    source
        Human-readable partition origin woven into the per-session bad-tick WARN
        (e.g. ``"NSE/INDEX/NSE-NIFTY/1minute"``), or ``None``.
    bad_tick_enabled
        Override for the bad-tick arm; ``None`` reads the
        :func:`~brutex.data.bad_tick.bad_tick_screen_enabled` kill switch
        (``BRUTEX_BAD_TICK_SCREEN``, default ON) — the SAME switch the engine
        obeys, so the two stay consistent. Disabling turns OFF only the bad-tick
        arm; the insane (positivity / ordering) drop is unconditional, mirroring
        the engine's always-on positivity guard.
    """

    __slots__ = ("_bad_tick", "_insane")

    def __init__(self, *, source: str | None = None, bad_tick_enabled: bool | None = None) -> None:
        enabled = bad_tick_screen_enabled() if bad_tick_enabled is None else bad_tick_enabled
        # NOTE (DEC-BADTICK-HEAD-SEED-001 coordination point): the bad-tick arm
        # is the EXISTING public BadTickScreen. If that fix later adds a public
        # first-bar seed (e.g. BadTickScreen(seed_prev_close=...)), thread the
        # reader's seed here — this is the ONE construction site. The
        # anti-divergence property test vs arrow_table_to_bars fails loudly on
        # any drift, so no forking of the logic is possible.
        self._bad_tick: BadTickScreen | None = BadTickScreen(source=source) if enabled else None
        self._insane: int = 0

    def classify(
        self,
        *,
        ist_date: date,
        o: float | None,
        h: float | None,
        l: float | None,  # noqa: E741 - o/h/l/c mirror the OHLC field names
        c: float | None,
    ) -> BarVerdict:
        """Classify one bar; the insane arm never advances the bad-tick baseline."""
        if price_row_insane(o, h, l, c):
            self._insane += 1
            return BarVerdict.DROP_INSANE
        # Past the insane guard every price is a non-None positive float (the
        # engine's positivity-BEFORE-bad-tick ordering); cast narrows for the
        # typed ``keep(close: float)`` with no runtime branch (coverage-safe).
        if self._bad_tick is not None and not self._bad_tick.keep(
            ist_date=ist_date, close=cast("float", c)
        ):
            return BarVerdict.DROP_BAD_TICK
        return BarVerdict.KEEP

    @property
    def insane_dropped(self) -> int:
        """How many price-insane bars were dropped (positivity / ordering)."""
        return self._insane

    def finish(self) -> None:
        """Flush the bad-tick WARN owed for the final session (call once at end)."""
        if self._bad_tick is not None:
            self._bad_tick.finish()


def screen_frame(
    df: pl.DataFrame,
    *,
    ts_col: str = "ts_utc",
    source: str | None = None,
    bad_tick_enabled: bool | None = None,
) -> pl.DataFrame:
    """Drop bad-tick + insane rows from an ASCENDING-by-*ts_col* bar frame.

    The polars wrapper for the ``/movers`` readers. Screens the frame's rows in
    their current (ascending session) order and DROPS the non-KEEP rows,
    preserving row order + dtypes (never edits / reorders / re-types). On a clean
    frame every row is KEEP, so the SAME object is returned (byte-identical, the
    steady-state spy-pin) — but the screen pass itself is O(rows) Python work per
    call (honest cost, bench-pinned linear). An empty frame is returned unchanged.

    Parameters
    ----------
    df
        The bar frame — must carry ``open``/``high``/``low``/``close`` + *ts_col*
        (a tz-aware datetime column), in ascending chronological order (the
        readers sort before calling; the bad-tick screen is causal).
    ts_col
        The timestamp column name (``"ts_utc"`` for the resampled readers,
        ``"timestamp"`` for the futures reader).
    source
        Partition origin for the bad-tick WARN.
    bad_tick_enabled
        Kill-switch override (``None`` reads ``BRUTEX_BAD_TICK_SCREEN``).
    """
    import polars as pl

    if df.height == 0:
        return df
    screen = SessionBarScreen(source=source, bad_tick_enabled=bad_tick_enabled)
    ist_dates = df.get_column(ts_col).dt.convert_time_zone(_IST_TZ).dt.date().to_list()
    opens = df.get_column("open").to_list()
    highs = df.get_column("high").to_list()
    lows = df.get_column("low").to_list()
    closes = df.get_column("close").to_list()
    keep = [
        screen.classify(ist_date=d, o=o, h=h, l=lo, c=c) is BarVerdict.KEEP
        for d, o, h, lo, c in zip(ist_dates, opens, highs, lows, closes, strict=True)
    ]
    screen.finish()
    if all(keep):
        return df
    return df.filter(pl.Series(keep, dtype=pl.Boolean))


def screen_rows(
    rows: list[tuple[Any, ...]],
    out_columns: list[str],
    *,
    source: str | None = None,
    seed_bar: tuple[Any, float, float, float, float] | None = None,
    bad_tick_enabled: bool | None = None,
) -> list[tuple[Any, ...]]:
    """Drop bad-tick + insane rows from a ``/db`` DuckDB page (any sort order).

    The columnar wrapper for the ``/db`` candle pager. The page may be sorted
    ASC or DESC and is a ``LIMIT``/``OFFSET`` slice, so the cross-row bad-tick
    screen runs over an ASCENDING copy (sorted by the ``timestamp`` cell) and the
    KEEP-set is mapped back to the page's RENDER order — the per-row insane arm is
    order-independent either way. When the page lacks any of
    ``timestamp``/``open``/``high``/``low``/``close`` (a custom projection),
    screening is skipped and the page is served raw (documented; the canonical
    grid always projects them). On a clean page the SAME list object is returned.

    *seed_bar* (``(ts, open, high, low, close)``) is the single in-session bar
    IMMEDIATELY before the page's earliest row — a bounded prior-bar read that
    mirrors :func:`~brutex.dashboard.db_view._intraday_prev_close_by_page_date`.
    It is fed FIRST so the page's leading row is judged against a real predecessor
    (the cross-page correctness the page-local screen otherwise lacks). HONEST
    residual: the double-edge case (a spike on the page's first row whose true
    predecessor was ITSELF a dropped spike) is not reproduced by a page-local
    screen — the ``/movers`` reader (whole-day) is always correct; this is the
    only ``/movers`` ↔ ``/db`` divergence, documented in the DEC blurb.

    .. warning::
       *bad_tick_enabled*'s ``None`` default reads the ``BRUTEX_BAD_TICK_SCREEN``
       kill switch — the SPOT/FUTURES (engine-parity) posture. A caller serving
       OPTION contracts MUST pass ``bad_tick_enabled=False`` explicitly (premia
       legitimately move ≥4×/minute — the ×4 threshold is index-calibrated); the
       recommended contract is KIND-DERIVED at the call site (decode the symbol,
       e.g. ``parse_fno_symbol(...).option_type is not None``), never
       remembered-per-caller — see the two ``db_view`` call sites.

    Resilience (the DEC-DB-CORRUPT-SKIP-001 never-500 posture): a row whose
    ``timestamp`` cell is NOT a datetime (a schema-drifted parquet's
    ``union_by_name`` null-fill) is served RAW — excluded from screening entirely
    (never sorted, never classified, never dropped) — matching the seed helper's
    ``isinstance`` guard; the datetime rows around it are still screened. A page
    mixing NAIVE + AWARE timestamps (incomparable — ``sorted`` raises
    ``TypeError``) is served RAW wholesale with one
    ``bar_screen.unsortable_page_served_raw`` WARN, never a 500. Duplicate-ts
    rows are screened in a CANONICAL order (ts, then the price cells as
    tie-break), so an ASC and a DESC page of the same data classify an
    equal-timestamp pair identically.
    """
    if not rows:
        return rows
    try:
        ts_idx = out_columns.index("timestamp")
        o_idx = out_columns.index("open")
        h_idx = out_columns.index("high")
        l_idx = out_columns.index("low")
        c_idx = out_columns.index("close")
    except ValueError:
        return rows
    screen = SessionBarScreen(source=source, bad_tick_enabled=bad_tick_enabled)
    if seed_bar is not None:
        s_ts, s_o, s_h, s_l, s_c = seed_bar
        # Advance the baseline from the predecessor; its verdict is not served.
        screen.classify(ist_date=session_date_from_ts(s_ts), o=s_o, h=s_h, l=s_l, c=s_c)
    # Screen ONLY the rows carrying a real datetime timestamp (see the docstring
    # resilience note) — a None/junk ts cell is served raw, never a TypeError.
    screenable = [i for i in range(len(rows)) if isinstance(rows[i][ts_idx], datetime)]

    def _sortable_cell(value: Any) -> tuple[int, float]:
        """A total-order key for one price cell: junk (None/NaN/non-numeric) sorts LAST."""
        if isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value)):
            return (0, float(value))
        return (1, 0.0)

    def _canonical_key(i: int) -> tuple[Any, ...]:
        """CANONICAL screening order (round-2 refuter MINOR-2): duplicate-ts rows
        tie-break on the full price row, so an ASC page and a DESC page classify
        an equal-timestamp pair IDENTICALLY (a stable ts-only sort kept PAGE
        order, making the kept bar direction-dependent). Render order is still
        restored via the original index."""
        row = rows[i]
        return (
            row[ts_idx],
            _sortable_cell(row[c_idx]),
            _sortable_cell(row[h_idx]),
            _sortable_cell(row[l_idx]),
            _sortable_cell(row[o_idx]),
        )

    try:
        order = sorted(screenable, key=_canonical_key)
    except TypeError:
        # Mixed naive/aware timestamps are incomparable (round-2 refuter NIT-1):
        # serve the page RAW rather than 500 — the never-500 posture. One WARN.
        _LOG.warning(
            "bar_screen.unsortable_page_served_raw",
            source=source,
            rows=len(rows),
        )
        return rows
    dropped: set[int] = set()
    for i in order:
        row = rows[i]
        verdict = screen.classify(
            ist_date=session_date_from_ts(row[ts_idx]),
            o=row[o_idx],
            h=row[h_idx],
            l=row[l_idx],
            c=row[c_idx],
        )
        if verdict is not BarVerdict.KEEP:
            dropped.add(i)
    screen.finish()
    if not dropped:
        return rows
    return [row for i, row in enumerate(rows) if i not in dropped]


__all__ = [
    "BarVerdict",
    "SessionBarScreen",
    "screen_frame",
    "screen_rows",
    "session_date_from_ts",
]
