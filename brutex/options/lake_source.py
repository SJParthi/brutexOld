"""Read REAL option bars from the F&O candle lake for the executor (DEC-OPTIONS-LAKE-READER-001).

:mod:`brutex.options.executor` prices a winning Track-1 spot signal into a real
options trade through an injected :class:`~brutex.options.executor.OptionBarSource`
seam — but the only implementation was a ``Protocol`` stub, so no sweep could ever
price a leg from the lake. THIS is the production seam: given a leg's
``(instrument, expiry_date, strike, option_type, entry_ts, exit_ts)`` it resolves
the contract's ``groww_symbol`` partition in the F&O candle lake
(``bars/<exch>/FNO/<symbol>/1minute/<yyyy>/<mm>.parquet`` — the layout
:mod:`brutex.adapters.groww.option_ingest` writes, DEC-FNO-CANDLE-LAKE-001) and
returns the entry + forced-exit :class:`~brutex.options.executor.OptionBarPair`,
or ``None`` when the bars are absent (band-drift hole / not ingested / illiquid /
no bar at that exact minute) — **never a fabricated price** (Golden Rule #1).

## How a contract is resolved (robust, not reconstructed)

The exact ``groww_symbol`` is DISCOVERED, never rebuilt from a fragile format
string: the lake's ``FNO`` partition directories are scanned ONCE per
``(exchange, underlying)`` and decoded via
:func:`~brutex.data.fno_symbol.parse_fno_symbol`, building an
``(expiry, strike, option_type) → symbol`` index that is cached on the source. So a
leg is matched to the real on-disk contract regardless of strike/expiry-token
formatting quirks, or honestly misses (``None``).

## What the OptionBar carries

The F&O candle lake stores OHLCV + open-interest (a :class:`CandleBar`); the leg's
``side`` (CE/PE) and ``strike`` are KNOWN from the query and stamped onto the
returned :class:`~brutex.data.models.OptionBar`. ``spot`` / ``iv`` are ``None`` —
the candle lake stores neither (honest; the executor's ``iv_at_entry`` is already
documented as ``None`` when the side omits it). The entry/exit bars are matched on
the EXACT minute instant (``entry_ts`` / ``exit_ts`` localised to UTC) — a minute the
option did not trade is an honest ``None``, never the nearest-prior bar.

## O(1) discipline + bounded retention

The symbol index + each contract's ``{instant → OptionBar}`` map are built once and
cached, so a sweep that materialises many legs on the same contract pays the lake
read ONCE; each :meth:`load_pair` is then an O(1) dict lookup of the two instants.
The first touch of a contract is O(bars-in-contract) (a single small partition).
The per-contract bar cache is an **LRU bounded at** ``bar_cache_capacity``
**contracts** (default 256 — DEC-MEMORY-FOOTPRINT-HARDENING-001): the pre-fix
unbounded dict retained EVERY touched contract's full 1-minute bar map for the
source's lifetime, so a translation run across hundreds of contracts grew without
bound. Eviction is safe by construction — this is a RECOMPUTABLE read cache over
immutable stored bars, so a hit-after-evict re-reads the identical partition and
rebuilds the identical map (results byte-identical; only the re-read cost returns).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.costs.types import OptionType
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.models import OptionBar
from brutex.options.executor import OptionBarPair
from brutex.options.lot_size_history import OptionsUnderlying

__all__ = ["STRIKE_COLLISION_EVENT", "LakeOptionBarSource"]

_LOG = structlog.get_logger(__name__)

#: Structlog WARNING event emitted when two discovered FNO partition directories
#: decode to Decimal-EQUAL strikes for the same ``(expiry, option_type)`` — e.g.
#: ``…-24650-CE`` vs ``…-24650.0-CE`` (``Decimal("24650") == Decimal("24650.0")``
#: and they hash alike, so they are ONE dict key). Without the warning the later
#: directory silently shadowed the earlier one (DEC-LAKE-SOURCE-STRIKE-COLLIDE-001).
STRIKE_COLLISION_EVENT: Final[str] = "lake_source.strike_collision"

#: The exchange each Track-2 underlying's F&O contracts live under in the lake —
#: NIFTY / BANKNIFTY ride ``NSE``, SENSEX rides ``BSE`` (CLAUDE.md §6 scope).
_EXCHANGE_BY_UNDERLYING: dict[OptionsUnderlying, str] = {
    OptionsUnderlying.NIFTY: "NSE",
    OptionsUnderlying.BANKNIFTY: "NSE",
    OptionsUnderlying.SENSEX: "BSE",
}

#: The F&O lake segment (DEC-FNO-CANDLE-LAKE-001) + the only fetched interval.
_FNO_SEGMENT = "FNO"
_INTERVAL = "1minute"

#: The contract-resolution key — what a leg is matched on.
_ContractKey = tuple[date, Decimal, str]


def _utc_instant(ts: datetime) -> datetime:
    """Normalise a timezone-aware datetime to a UTC instant (the bar-map key).

    The lake stores bar timestamps as UTC; a leg's ``entry_ts`` / ``exit_ts`` are
    IST. Both localise to the SAME UTC instant, so the dict lookup matches the bar
    that opened at that minute regardless of the caller's zone. A naive datetime is
    read as UTC (defensive — the executor always passes tz-aware).
    """
    return ts.astimezone(UTC) if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class LakeOptionBarSource:
    """Production :class:`~brutex.options.executor.OptionBarSource` over the F&O candle lake.

    Construct with the lake root (the parent of ``bars/``); inject into
    :func:`~brutex.options.executor.materialize_trade` /
    :func:`~brutex.options.executor.execute_options_sim`. Stateless per call except
    for the two read-through caches (symbol index + per-contract bar map), so it is
    safe to reuse across a whole sweep.

    Parameters
    ----------
    lake_root
        The lake root (the parent of ``bars/``).
    bar_cache_capacity
        Maximum number of CONTRACTS whose bar maps stay resident (LRU —
        DEC-MEMORY-FOOTPRINT-HARDENING-001; the pre-fix dict was unbounded).
        Must be ``>= 1``; the default 256 comfortably holds a strike chain's
        worth of contracts while bounding a many-contract translation run.
        Eviction only ever costs a re-read (immutable stored bars → the
        rebuilt map is identical), never a changed result.
    """

    def __init__(self, lake_root: Path, *, bar_cache_capacity: int = 256) -> None:
        if bar_cache_capacity < 1:
            raise ValueError(f"bar_cache_capacity must be >= 1, got {bar_cache_capacity}")
        self._lake_root = lake_root
        self._bar_cache_capacity = bar_cache_capacity
        # (exchange, underlying) → {(expiry, strike, option_type): groww_symbol}, built
        # once per underlying by scanning + decoding its FNO partition dirs (bounded by
        # the 3-index engine scope — one small index per (exchange, underlying)).
        self._symbol_index: dict[tuple[str, str], dict[_ContractKey, str]] = {}
        # groww_symbol → {utc_instant: OptionBar}, built once per contract on first
        # touch — LRU-bounded at `bar_cache_capacity` contracts (single-threaded
        # executor use, no lock — the module's documented concurrency envelope).
        self._bar_cache: OrderedDict[str, dict[datetime, OptionBar]] = OrderedDict()

    def load_pair(
        self,
        *,
        instrument: OptionsUnderlying,
        expiry_date: date,
        strike: int,
        option_type: OptionType,
        entry_ts: datetime,
        exit_ts: datetime,
    ) -> OptionBarPair | None:
        """Return the leg's entry + forced-exit :class:`OptionBarPair`, or ``None`` if absent.

        Resolves the contract symbol, reads its bars (cached), then looks up the
        entry + exit bars by their exact UTC minute instant. ``None`` when the
        contract is not in the lake OR either bar is missing (the option did not
        trade that minute) — the trade is then rejected ``NO_DATA``, never faked.
        """
        exchange = _EXCHANGE_BY_UNDERLYING[instrument]
        symbol = self._resolve_symbol(exchange, instrument.value, expiry_date, strike, option_type)
        if symbol is None:
            return None
        bars = self._contract_bars(exchange, symbol, option_type, strike)
        entry_bar = bars.get(_utc_instant(entry_ts))
        exit_bar = bars.get(_utc_instant(exit_ts))
        if entry_bar is None or exit_bar is None:
            return None
        return OptionBarPair(entry_bar=entry_bar, exit_bar=exit_bar)

    def _resolve_symbol(
        self,
        exchange: str,
        underlying: str,
        expiry_date: date,
        strike: int,
        option_type: OptionType,
    ) -> str | None:
        """Resolve the on-disk contract ``groww_symbol`` for a leg, or ``None`` — cached.

        Builds the ``(expiry, strike, option_type) → symbol`` index for
        ``(exchange, underlying)`` on first use by scanning its ``FNO`` partition
        dirs + decoding each via :func:`parse_fno_symbol`, then an O(1) dict lookup.
        """
        index = self._symbol_index.get((exchange, underlying))
        if index is None:
            index = self._build_symbol_index(exchange, underlying)
            self._symbol_index[exchange, underlying] = index
        return index.get((expiry_date, Decimal(strike), option_type))

    def _build_symbol_index(self, exchange: str, underlying: str) -> dict[_ContractKey, str]:
        """Scan ``bars/<exchange>/FNO/`` and index this underlying's OPTION contracts.

        The scan is ``sorted()`` so the walk order is deterministic across
        filesystems (raw ``iterdir()`` order is OS-arbitrary). When sibling
        directories decode to Decimal-EQUAL strikes for the same
        ``(expiry, option_type)`` — e.g. ``…-24650-CE`` vs ``…-24650.0-CE`` —
        one :data:`STRIKE_COLLISION_EVENT` WARNING fires PER COLLIDED PAIR,
        naming the shadowed + kept raw directory names via ``repr()`` (an N-way
        collision emits N−1 warnings; every colliding raw dir is named across
        them, the intermediate winner appearing as ``kept`` in one and
        ``shadowed`` in the next). Then the keep-policy applies: the
        lexicographically-LATER raw name wins (last write of the sorted walk),
        matching the module's long-standing last-wins overwrite but now
        deterministic + visible. The shadowed directory's bars are unreachable
        for that key — warned, never silent, never a fabricated price
        (DEC-LAKE-SOURCE-STRIKE-COLLIDE-001).
        """
        index: dict[_ContractKey, str] = {}
        fno_dir = self._lake_root / "bars" / exchange / _FNO_SEGMENT
        if not fno_dir.is_dir():
            return index
        for contract_dir in sorted(fno_dir.iterdir()):
            if not contract_dir.is_dir():
                continue
            parts = parse_fno_symbol(contract_dir.name)
            # Options only (a future has no option_type / strike); match THIS underlying.
            if parts is None or parts.option_type is None or parts.strike is None:
                continue
            if parts.underlying != underlying:
                continue
            key: _ContractKey = (parts.expiry, parts.strike, parts.option_type)
            existing = index.get(key)
            if existing is not None:
                # Decimal-equal strike collision: warn once PER collided pair naming
                # both raw dirs, then keep the deterministic pick (later sorted name
                # wins; an N-way collision emits N−1 of these, naming every dir).
                _LOG.warning(
                    STRIKE_COLLISION_EVENT,
                    exchange=exchange,
                    underlying=underlying,
                    expiry=parts.expiry.isoformat(),
                    strike=str(parts.strike),
                    option_type=parts.option_type,
                    shadowed=repr(existing),
                    kept=repr(contract_dir.name),
                )
            index[key] = contract_dir.name
        return index

    def _contract_bars(
        self, exchange: str, symbol: str, option_type: OptionType, strike: int
    ) -> dict[datetime, OptionBar]:
        """Read the contract's 1-minute bars into ``{utc_instant: OptionBar}`` — cached.

        The leg ``side`` (CE/PE) + ``strike`` are stamped from the query; ``spot`` /
        ``iv`` are ``None`` (the F&O candle lake stores neither). A contract with no
        Parquet yet → an empty map (every lookup misses → honest ``None``). The map
        is LRU-cached per contract, bounded at ``bar_cache_capacity`` contracts
        (DEC-MEMORY-FOOTPRINT-HARDENING-001); an evicted contract simply re-reads
        its immutable partition on the next touch — the identical map, never a
        changed price.
        """
        cached = self._bar_cache.get(symbol)
        if cached is not None:
            self._bar_cache.move_to_end(symbol)  # mark most-recently-used.
            return cached
        import polars as pl

        from brutex.data.lake_read import dedup_bars_frame

        base = self._lake_root / "bars" / exchange / _FNO_SEGMENT / symbol / _INTERVAL
        bars: dict[datetime, OptionBar] = {}
        if base.is_dir() and any(base.rglob("*.parquet")):
            # Schema-tolerant tree read (DEC-GREEKS-ENGINE-PR2B-001): the enrich
            # pass appends nullable Greeks columns to OPTION month partitions
            # (plan §15.3), so one contract's tree can mix the raw 7-column and
            # the 17-column shape; `_row_to_option_bar` reads named keys, so
            # extra/absent overlay columns are irrelevant here.
            frame: pl.DataFrame = pl.scan_parquet(
                base.as_posix() + "/**/*.parquet",
                missing_columns="insert",
                extra_columns="ignore",
            ).collect()
            # Keep-FIRST read dedup (DEC-LAKE-READ-UNIQUE-001): the dict build
            # below was LAST-wins on a poisoned-era duplicate timestamp, so a
            # re-stated bar silently replaced the real leg price. Collapse the
            # frame first (warned, never silent) so the FIRST stored bar wins.
            frame, _dropped = dedup_bars_frame(frame, source=f"option_lake:{symbol}")
            strike_dec = Decimal(strike)
            for row in frame.iter_rows(named=True):
                bar = _row_to_option_bar(row, side=option_type, strike=strike_dec)
                bars[_utc_instant(bar.timestamp)] = bar
        self._bar_cache[symbol] = bars  # a fresh insert lands most-recently-used.
        while len(self._bar_cache) > self._bar_cache_capacity:
            self._bar_cache.popitem(last=False)  # evict the LRU contract's map.
        return bars


def _row_to_option_bar(row: Mapping[str, Any], *, side: OptionType, strike: Decimal) -> OptionBar:
    """Build an :class:`OptionBar` from one F&O candle-lake row + the known side/strike.

    *row* is one ``polars`` ``iter_rows(named=True)`` mapping — typed ``Mapping[str, Any]``
    exactly like the sibling lake reader (``movers_snapshot._row_to_bar``, CLAUDE.md §11
    COMMON) so the integer casts resolve under ``mypy --strict`` without a suppression.
    ``spot`` / ``iv`` are ``None`` (not stored in the F&O candle lake — honest).
    """
    raw_oi = row["open_interest"]
    return OptionBar(
        timestamp=_as_datetime(row["timestamp"]),
        open=_as_decimal(row["open"]),
        high=_as_decimal(row["high"]),
        low=_as_decimal(row["low"]),
        close=_as_decimal(row["close"]),
        volume=int(row["volume"]),
        open_interest=None if raw_oi is None else int(raw_oi),
        side=side,
        strike=strike,
        spot=None,
        iv=None,
    )


def _as_datetime(value: object) -> datetime:
    """Coerce a Parquet timestamp cell to a tz-aware datetime (UTC if naive)."""
    if not isinstance(value, datetime):  # pragma: no cover - parquet always yields datetime
        raise TypeError(f"expected a datetime timestamp, got {type(value).__name__}")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_decimal(value: object) -> Decimal:
    """Coerce a Parquet price cell (Float64 / Decimal) to an exact Decimal (rupees)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))
