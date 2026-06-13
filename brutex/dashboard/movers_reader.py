"""Production lake reader + JSON builder for the ``/movers`` dashboard page.

``DEC-MOVERS-DASHBOARD-PAGE-001`` (``docs/MIGRATION.md`` entry #706). This is
the deferred production wiring the two already-merged screener cores pointed at:

* :func:`brutex.screeners.screen_gainers_losers` (``DEC-MOVERS-GAINERS-001``,
  entry #701) — top gainers / losers by window % change.
* :func:`brutex.screeners.screen_movers` (``DEC-MOVERS-VOLUME-001``, entry
  #702) — volume-surge + realised-range-volatility movers.

Both cores shipped with ONLY a ``Protocol`` seam + in-memory fakes; the
production lake reader was deferred to this page (their module docstrings say
so). :class:`MoversLakeReader` binds that seam to the real lake via the lake
:class:`~brutex.data.catalog.Catalog` (``Catalog.get_bars``), exactly mirroring
the production replay path :func:`brutex.engine.cascade_wire.load_bars_for_manifest`
(``Catalog.open`` → :func:`~brutex.data.catalog_reconcile.reconcile_catalog_from_lake`
→ ``get_bars`` → :func:`~brutex.feed.replay.arrow_table_to_bars`).

## Why reconcile

The dashboard spot pull builds its ``ParquetBarSink`` with NO catalog
(``brutex/dashboard/pull_controller.py``), so the catalog ``partitions`` table
is EMPTY after a dashboard pull and ``Catalog.get_bars`` would honestly find
zero bars. :func:`~brutex.data.catalog_reconcile.reconcile_catalog_from_lake`
rebuilds the table from the on-disk Parquet (the established recovery pattern),
so the screen sees exactly what was pulled — never a fabricated row
(Golden Rule #1).

## Scope guard (CLAUDE.md §3)

This is a **REFERENCE_ONLY** display over the lake. It reads OHLCV the operator
already ingested and ranks it; it NEVER touches the brute-force engine or the
52-condition vocabulary. Screening an instrument here does not admit it to the
engine (mirrors the screener cores' own scope assertions).

## Honest envelope (Golden Rule #10)

* **NOT O(1)-total.** A screen reconciles the catalog from the lake (an O(P)
  one-time pass over the chosen interval's partitions, hashing each file —
  see :func:`~brutex.data.catalog_reconcile.reconcile_catalog_from_lake`) and
  then reads + ranks ``M`` instruments. The page therefore loads on demand
  (no auto-poll timer) and defaults to the coarse ``1day`` interval so the
  reconcile + reads stay bounded; the ``/movers`` template fetches once on open
  plus an explicit Refresh button. This module never claims O(1)-total.
* **Local data only.** The numbers are only as good as the ingested lake — an
  empty lake yields an honest "pull spot first" note, never invented movers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import structlog

from brutex.data.errors import CatalogError
from brutex.data.models import CandleBar
from brutex.feed.replay import arrow_table_to_bars
from brutex.money import format_inr, rupees_to_paisa
from brutex.screeners import (
    InstrumentRef,
    Mover,
    MoverMetrics,
    MoversReport,
    ScreenResult,
    ScreenTarget,
    screen_gainers_losers,
    screen_movers,
)

if TYPE_CHECKING:  # pragma: no cover — typing-only import (duckdb stays lazy)
    from brutex.data.catalog import Catalog

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_RECENT_BARS",
    "DEFAULT_TOP_N",
    "MoversLakeReader",
    "build_movers_payload",
    "empty_movers_payload",
    "parse_window",
    "run_movers_screen",
]

_LOG = structlog.get_logger("brutex.dashboard.movers")

#: Default screen timeframe — the coarse daily bar (CLAUDE.md §3; one of the 21
#: timeframes). Coarse on purpose: the reconcile + reads stay bounded vs 1minute.
DEFAULT_INTERVAL: Final[str] = "1day"

#: Default ranking depth for each of the four tables.
DEFAULT_TOP_N: Final[int] = 10

#: Default volume-surge recent-window size (last N bars vs the baseline).
DEFAULT_RECENT_BARS: Final[int] = 5

#: The rolling Track-1 range floor (CLAUDE.md §3 — ``2021-01-01 → today``). Used
#: as the default screen window start when the operator gives no ``from`` date.
_RANGE_START: Final[datetime] = datetime(2021, 1, 1, tzinfo=UTC)


# ────────────────────────────────────────────────────────────────────────
# Reader (binds the lake Catalog; satisfies BOTH screener protocols)
# ────────────────────────────────────────────────────────────────────────


class _MoversReader(Protocol):
    """The combined read seam :func:`build_movers_payload` talks through.

    Structurally a superset of both screener protocols
    (:class:`brutex.screeners.LakeReader` + :class:`brutex.screeners.LakeBarReader`)
    plus :meth:`list_screen_targets`. :class:`MoversLakeReader` satisfies it over
    the real lake; the unit tests satisfy it with an in-memory fake (no I/O).
    """

    def list_instruments(self) -> Sequence[InstrumentRef]:
        """Return the instruments to screen (gainers/losers seam)."""
        ...

    def list_screen_targets(self) -> Sequence[ScreenTarget]:
        """Return the screen targets to rank (volume/volatility seam)."""
        ...

    def read_window(
        self, ref: InstrumentRef, *, interval: str, start: datetime, end: datetime
    ) -> Sequence[CandleBar]:
        """Return *ref*'s bars in ``[start, end]`` at *interval* (gainers seam)."""
        ...

    def read_bars(
        self, target: ScreenTarget, *, start: datetime, end: datetime
    ) -> Sequence[CandleBar]:
        """Return *target*'s bars in ``[start, end]`` (volume seam)."""
        ...


@dataclass(slots=True)
class MoversLakeReader:
    """Read the lake for the movers screens via the lake :class:`Catalog`.

    Implements both screener protocols (and :meth:`list_screen_targets`). All
    reads funnel through :meth:`_read` — ``Catalog.get_bars`` →
    :func:`~brutex.feed.replay.arrow_table_to_bars` — behind a **request-scoped
    cache** keyed on ``(exchange, segment, symbol, interval, start, end)`` so the
    two screens (which both touch the same instruments over the same window) read
    each series from disk only ONCE.

    Attributes
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``); passed to
        ``Catalog.get_bars`` to resolve partition files.
    catalog
        An OPEN, already-reconciled :class:`~brutex.data.catalog.Catalog`.
    interval
        The single timeframe this reader screens (e.g. ``"1day"``).
    datasets
        The ``list_candle_datasets`` rows (already filtered to :attr:`interval`)
        the screen ranks — each a ``{"exchange", "segment", "symbol", ...}``
        mapping.
    """

    lake_root: Path
    catalog: Catalog
    interval: str
    datasets: Sequence[Mapping[str, str]]
    _cache: dict[tuple[str, str, str, str, datetime, datetime], list[CandleBar]] = field(
        default_factory=dict, init=False, repr=False
    )

    def list_instruments(self) -> list[InstrumentRef]:
        """Return one :class:`InstrumentRef` per discovered dataset."""
        return [
            InstrumentRef(symbol=d["symbol"], exchange=d["exchange"], segment=d["segment"])
            for d in self.datasets
        ]

    def list_screen_targets(self) -> list[ScreenTarget]:
        """Return one :class:`ScreenTarget` per discovered dataset (at :attr:`interval`)."""
        return [
            ScreenTarget(
                exchange=d["exchange"],
                segment=d["segment"],
                symbol=d["symbol"],
                interval=self.interval,
            )
            for d in self.datasets
        ]

    def read_window(
        self, ref: InstrumentRef, *, interval: str, start: datetime, end: datetime
    ) -> list[CandleBar]:
        """Return *ref*'s window bars at *interval* (the gainers/losers seam)."""
        return self._read(
            exchange=ref.exchange,
            segment=ref.segment,
            symbol=ref.symbol,
            interval=interval,
            start=start,
            end=end,
        )

    def read_bars(self, target: ScreenTarget, *, start: datetime, end: datetime) -> list[CandleBar]:
        """Return *target*'s window bars (the volume/volatility seam)."""
        return self._read(
            exchange=target.exchange,
            segment=target.segment,
            symbol=target.symbol,
            interval=target.interval,
            start=start,
            end=end,
        )

    def _read(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[CandleBar]:
        """Read one series' window bars, memoised per ``(coords, window)``."""
        key = (exchange, segment, symbol, interval, start, end)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        table = self.catalog.get_bars(
            lake_root=self.lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
        )
        bars = arrow_table_to_bars(table)
        self._cache[key] = bars
        return bars


# ────────────────────────────────────────────────────────────────────────
# Window parsing (pure)
# ────────────────────────────────────────────────────────────────────────


def _parse_date(value: str, *, end_of_day: bool) -> datetime:
    """Parse an ISO ``YYYY-MM-DD`` *value* into a UTC tz-aware datetime.

    ``end_of_day`` snaps the ``to`` bound to ``23:59:59`` (inclusive of the whole
    day) vs the ``from`` bound's midnight. Raises :class:`ValueError` (naming the
    bad value, never echoing it as a date) on a malformed string.
    """
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"malformed date {value!r} — expected ISO YYYY-MM-DD") from exc
    clock = time(23, 59, 59) if end_of_day else time(0, 0, 0)
    return datetime.combine(parsed, clock, tzinfo=UTC)


def parse_window(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    """Resolve the optional ISO date bounds into a UTC tz-aware ``(start, end)``.

    Defaults (CLAUDE.md §3 rolling range): a blank ``start`` is the 2021-01-01
    floor; a blank ``end`` is ``now`` (UTC). Both returned datetimes are ALWAYS
    timezone-aware (UTC) so the catalog read and the volume screen's tz-aware
    requirement are satisfied uniformly. Raises :class:`ValueError` on a
    malformed date or a ``from`` strictly after ``to``.
    """
    start_dt = _RANGE_START if not start else _parse_date(start, end_of_day=False)
    end_dt = datetime.now(UTC) if not end else _parse_date(end, end_of_day=True)
    if start_dt > end_dt:
        raise ValueError(
            f"'from' date {start_dt.date()} must be on or before 'to' date {end_dt.date()}"
        )
    return start_dt, end_dt


# ────────────────────────────────────────────────────────────────────────
# JSON projection (pure)
# ────────────────────────────────────────────────────────────────────────


def _decimal_str(value: Decimal | None) -> str | None:
    """Render a statistical :class:`~decimal.Decimal` metric at FULL precision.

    Returns ``None`` unchanged so an undefined metric stays honestly null.
    Statistical metrics are NEVER money-rounded (CLAUDE.md §4 /
    ``DEC-MONEY-2DP-001`` is money-only); the full-precision string keeps the
    value exact for the UI to format for display only.
    """
    return None if value is None else str(value)


def _mover_row(mover: Mover) -> dict[str, Any]:
    """Project one gainers/losers :class:`~brutex.screeners.Mover` to JSON.

    Prices are the 2dp-snapped rupee endpoints (the screener already snapped them
    via ``round_money``); they are rendered as ``₹`` money strings through
    :mod:`brutex.money` (``format_inr`` takes int64 PAISA, so the rupee Decimal
    is first put through :func:`~brutex.money.rupees_to_paisa`). ``pct_change``
    is a statistical metric kept at full precision (never money-rounded).
    """
    return {
        "symbol": mover.ref.symbol,
        "exchange": mover.ref.exchange,
        "segment": mover.ref.segment,
        "base_price": format_inr(rupees_to_paisa(mover.base_price)),
        "last_price": format_inr(rupees_to_paisa(mover.last_price)),
        "pct_change": str(mover.pct_change),
        "bar_count": mover.bar_count,
    }


def _metric_row(metric: MoverMetrics, *, value: Decimal | None) -> dict[str, Any]:
    """Project one volume/volatility :class:`~brutex.screeners.MoverMetrics` to JSON.

    *value* is the row's ranked metric (volume-surge ratio OR realised-range
    volatility) — a statistical metric rendered at full precision via
    :func:`_decimal_str`. The representative ``last_close`` is the screener's
    sole money surface, taken from the ``last_close_inr`` property (already a
    2dp ``₹`` string from int64 paisa).
    """
    return {
        "symbol": metric.target.symbol,
        "exchange": metric.target.exchange,
        "segment": metric.target.segment,
        "interval": metric.target.interval,
        "value": _decimal_str(value),
        "last_close": metric.last_close_inr,
        "bar_count": metric.bar_count,
    }


def _movers_note(
    *,
    instrument_count: int,
    gainers: Sequence[Any],
    losers: Sequence[Any],
    volume_movers: Sequence[Any],
    volatility_movers: Sequence[Any],
) -> str:
    """Return an honest one-line status note (Golden Rule #1 / #10).

    Distinguishes the three states the operator must be able to tell apart: an
    EMPTY lake (pull spot first), a populated lake whose window held NO usable
    bars (widen the window), and a normal ranked result.
    """
    if instrument_count == 0:
        return (
            "No spot data in the lake yet — pull spot first on the Coverage page, "
            "then refresh. (Reference-only lake view, CLAUDE.md §3.)"
        )
    if not gainers and not losers and not volume_movers and not volatility_movers:
        return (
            f"{instrument_count} instrument(s) in the lake, but none had usable bars in this "
            "window — widen the date range (or pull more), then refresh."
        )
    return (
        f"Ranked {instrument_count} instrument(s) over the selected window — reference-only "
        "lake view (CLAUDE.md §3); never feeds the brute-force engine or the 52-vocab."
    )


def _assemble_payload(
    *,
    interval: str,
    start: datetime,
    end: datetime,
    top_n: int,
    recent_bars: int,
    instrument_count: int,
    gainers: list[dict[str, Any]],
    losers: list[dict[str, Any]],
    volume_movers: list[dict[str, Any]],
    volatility_movers: list[dict[str, Any]],
    skipped_count: int,
    note: str,
) -> dict[str, Any]:
    """Assemble the canonical ``/api/v1/movers`` JSON dict (one source of truth).

    Every value is JSON-native (datetimes are ``isoformat`` strings, money is a
    ``₹`` string, statistical metrics are full-precision strings) so Starlette's
    ``JSONResponse`` — which has no ``Decimal``/``datetime`` encoder — never
    raises.
    """
    return {
        "interval": interval,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "top_n": top_n,
        "recent_bars": recent_bars,
        "instrument_count": instrument_count,
        "gainers": gainers,
        "losers": losers,
        "volume_movers": volume_movers,
        "volatility_movers": volatility_movers,
        "skipped_count": skipped_count,
        "note": note,
    }


def empty_movers_payload(
    *,
    interval: str,
    start: datetime,
    end: datetime,
    top_n: int,
    recent_bars: int,
) -> dict[str, Any]:
    """Return the honest empty payload — no lake wired, or an empty lake.

    ``instrument_count`` is 0 and the note is the "pull spot first" guidance, so
    a store with no lake root AND a lake with no spot datasets render identically
    (never a 500, never a fabricated mover — Golden Rule #1).
    """
    return _assemble_payload(
        interval=interval,
        start=start,
        end=end,
        top_n=top_n,
        recent_bars=recent_bars,
        instrument_count=0,
        gainers=[],
        losers=[],
        volume_movers=[],
        volatility_movers=[],
        skipped_count=0,
        note=_movers_note(
            instrument_count=0,
            gainers=[],
            losers=[],
            volume_movers=[],
            volatility_movers=[],
        ),
    )


def build_movers_payload(
    reader: _MoversReader,
    *,
    interval: str,
    start: datetime,
    end: datetime,
    top_n: int,
    recent_bars: int,
) -> dict[str, Any]:
    """Run both screens over *reader* and project the result to the JSON payload.

    Pure orchestration (no I/O of its own — all reads go through *reader*): runs
    :func:`~brutex.screeners.screen_gainers_losers` and
    :func:`~brutex.screeners.screen_movers` over the SAME parsed ``[start, end]``
    window and the same interval, so the reader's request-scoped cache makes the
    second screen's reads pure cache hits.

    Parameters
    ----------
    reader
        The lake read seam (production :class:`MoversLakeReader` or a fake).
    interval
        The timeframe both screens read (must match the reader's targets).
    start, end
        The inclusive UTC tz-aware window.
    top_n
        Ranking depth for each of the four tables (``>= 1``).
    recent_bars
        Volume-surge recent-window size (``>= 1``).

    Returns
    -------
    dict[str, Any]
        The JSON-native payload (see :func:`_assemble_payload`).
    """
    gl: ScreenResult = screen_gainers_losers(
        reader, interval=interval, start=start, end=end, top_n=top_n
    )
    vm: MoversReport = screen_movers(
        reader=reader,
        targets=reader.list_screen_targets(),
        start=start,
        end=end,
        recent_bars=recent_bars,
        top_n=top_n,
    )
    gainers = [_mover_row(m) for m in gl.gainers]
    losers = [_mover_row(m) for m in gl.losers]
    volume_movers = [_metric_row(m, value=m.volume_surge_ratio) for m in vm.top_volume_surge]
    volatility_movers = [
        _metric_row(m, value=m.realised_range_volatility) for m in vm.top_realised_volatility
    ]
    instrument_count = len(vm.metrics) + len(vm.skipped)
    # Honest skipped count: a target the lake had no window bars for (reported by
    # BOTH screens identically; vm.skipped is the canonical set).
    skipped_count = len(vm.skipped)
    note = _movers_note(
        instrument_count=instrument_count,
        gainers=gainers,
        losers=losers,
        volume_movers=volume_movers,
        volatility_movers=volatility_movers,
    )
    return _assemble_payload(
        interval=interval,
        start=start,
        end=end,
        top_n=top_n,
        recent_bars=recent_bars,
        instrument_count=instrument_count,
        gainers=gainers,
        losers=losers,
        volume_movers=volume_movers,
        volatility_movers=volatility_movers,
        skipped_count=skipped_count,
        note=note,
    )


# ────────────────────────────────────────────────────────────────────────
# Live orchestration (opens + reconciles the lake Catalog)
# ────────────────────────────────────────────────────────────────────────


def run_movers_screen(
    *,
    lake_root: Path,
    interval: str,
    start: datetime,
    end: datetime,
    top_n: int,
    recent_bars: int,
) -> dict[str, Any]:
    """Screen the lake end to end and return the ``/api/v1/movers`` JSON payload.

    Discovers the datasets at *interval* from the filesystem
    (:func:`~brutex.dashboard.db_view.list_candle_datasets` — no catalog needed),
    short-circuits to an honest empty payload when none exist, else opens the
    lake :class:`~brutex.data.catalog.Catalog`, reconciles it from the on-disk
    Parquet (so a dashboard pull's bars are visible), and delegates the ranking
    to :func:`build_movers_payload`.

    Crash-tolerant (never raises to the caller / never a 500): a
    :class:`~brutex.data.errors.CatalogError` (the catalog file is busy — e.g. a
    sweep holds it — or the optional data-lake dependencies are missing) is
    caught and rendered as an honest "lake catalog unavailable, retry" payload.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    interval, start, end, top_n, recent_bars
        The screen parameters (see :func:`build_movers_payload`).

    Returns
    -------
    dict[str, Any]
        The JSON-native payload.
    """
    from brutex.dashboard.db_view import list_candle_datasets

    datasets = [d for d in list_candle_datasets(lake_root) if d["interval"] == interval]
    if not datasets:
        return empty_movers_payload(
            interval=interval, start=start, end=end, top_n=top_n, recent_bars=recent_bars
        )

    from brutex.data.catalog import Catalog
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

    catalog_path = lake_root / "catalog.duckdb"
    try:
        with Catalog.open(catalog_path) as catalog:
            reconcile_catalog_from_lake(lake_root=lake_root, catalog=catalog, interval=interval)
            reader = MoversLakeReader(
                lake_root=lake_root,
                catalog=catalog,
                interval=interval,
                datasets=datasets,
            )
            return build_movers_payload(
                reader,
                interval=interval,
                start=start,
                end=end,
                top_n=top_n,
                recent_bars=recent_bars,
            )
    except CatalogError as exc:
        _LOG.warning("movers.catalog_unavailable", error=str(exc), interval=interval)
        return _assemble_payload(
            interval=interval,
            start=start,
            end=end,
            top_n=top_n,
            recent_bars=recent_bars,
            instrument_count=len(datasets),
            gainers=[],
            losers=[],
            volume_movers=[],
            volatility_movers=[],
            skipped_count=0,
            note=(
                "Lake catalog unavailable (busy or the data-lake dependencies are "
                "missing) — retry shortly. The lake itself is untouched."
            ),
        )
