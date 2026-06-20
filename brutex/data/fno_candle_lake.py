"""The F&O candle lake — its logical-table schema + ``schema_meta`` registration.

BruteX already STORES F&O (futures + options) historical OHLC: the Groww pull
(``get_expiries`` → ``get_contracts`` → ``get_historical_candles``) writes every
contract's OHLCV + open-interest bars through the shared
:class:`~brutex.data.sink.ParquetBarSink` to the canonical candle lake at
``bars/<exchange>/FNO/<groww_symbol>/<interval>/<yyyy>/<mm>.parquet`` — the SAME
partition tree + writer + 2-dp price grid as spot, only with ``segment="FNO"`` and a
full contract ``groww_symbol`` (DEC-1MINUTE-ONLY-DERIVE-1DAY-001;
``brutex/adapters/groww/option_ingest.py`` ``sink.append(segment="FNO", …)``). What
was MISSING was a *first-class, documented logical table* for that data: a named
schema, surfaced in the ``/db`` browser and registered in the master's
``schema_meta`` bookkeeping. This module is that formalization (DEC-FNO-CANDLE-LAKE-001).

It is **pure** (stdlib only — no Parquet / DuckDB imports) so it is importable from
the instrument-master open path (to register the descriptor) and from
:mod:`brutex.dashboard.db_view` (to drive the F&O candle browser) without forcing the
P2 data-lake extras into either. The actual row discovery / aggregation lives in
:mod:`brutex.dashboard.db_view` (which already owns the DuckDB connection); this
module owns only the *contract* — the schema + the registration.

The "table" is a partition-projected view, NOT a physical SQL table: the seven OHLCV
columns are the physical Parquet columns; the identity columns
(exchange / underlying / expiry / strike / instrument_type / option_type / interval)
are encoded in the partition path + the contract ``groww_symbol`` (decoded by
:func:`brutex.data.fno_symbol.parse_fno_symbol`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from brutex.data.fno_symbol import FNO_INSTRUMENT_TYPES

#: The logical table name surfaced in ``/db`` + registered in ``schema_meta``.
FNO_CANDLE_LAKE_TABLE: Final[str] = "fno_candles"

#: The lake segment every F&O candle partition lives under (the
#: :mod:`brutex.data.partition` path component; vendor ``FNO``).
FNO_CANDLE_LAKE_SEGMENT: Final[str] = "FNO"

#: Schema version of the F&O candle logical table. Bumped only on a non-additive
#: shape change; the registration is idempotent (``INSERT OR REPLACE``) so re-opening
#: an existing master always re-stamps the current descriptor.
FNO_CANDLE_LAKE_SCHEMA_VERSION: Final[int] = 1

#: The on-disk partition path template (``str.format`` placeholders), documenting
#: where a row physically lives. ``groww_symbol`` is the full contract symbol
#: (``NSE-NIFTY-30Sep25-24650-CE``), the single key the candle fetch + lake-write use.
FNO_CANDLE_LAKE_PATH_TEMPLATE: Final[str] = (
    "bars/{exchange}/FNO/{groww_symbol}/{interval}/{year:04d}/{month:02d}.parquet"
)


@dataclass(frozen=True, slots=True)
class FnoCandleColumn:
    """One column of the F&O candle logical table.

    Attributes
    ----------
    name
        The column name as shown in ``/db``.
    dtype
        A human logical type label (``timestamp`` / ``decimal`` / ``int`` / ``string``
        / ``date``) — documentation only; the physical Parquet dtypes are the
        :mod:`brutex.data.parquet_writer` schema (the OHLCV columns) and the identity
        columns are derived strings / numbers.
    source
        Where the value comes from: ``"bar"`` (a physical Parquet column written per
        bar), ``"partition"`` (encoded in the lake path), or ``"symbol"`` (decoded
        from the contract ``groww_symbol`` via
        :func:`~brutex.data.fno_symbol.parse_fno_symbol`).
    nullable
        Whether the column may be absent / ``None`` for a valid row (e.g. ``strike`` /
        ``option_type`` are ``None`` for a future leg).
    doc
        One-line human description.
    """

    name: str
    dtype: str
    source: str
    nullable: bool
    doc: str


#: The full F&O candle logical schema — the 7 physical OHLCV columns
#: (DEC-PAISA-DOC-RECONCILE-001: prices are Decimal rupees snapped to the 2-dp tick
#: grid at the lake-write boundary; ``open_interest`` is a FIRST-CLASS non-null column
#: for FUT/CE/PE, unlike spot where it is ``None``) followed by the partition / symbol
#: identity columns that make a row uniquely an F&O contract bar.
FNO_CANDLE_COLUMNS: Final[tuple[FnoCandleColumn, ...]] = (
    FnoCandleColumn("timestamp", "timestamp", "bar", False, "Bar open time (UTC; displayed IST)."),
    FnoCandleColumn("open", "decimal", "bar", False, "Open price (₹, 2-dp tick grid)."),
    FnoCandleColumn("high", "decimal", "bar", False, "High price (₹, 2-dp tick grid)."),
    FnoCandleColumn("low", "decimal", "bar", False, "Low price (₹, 2-dp tick grid)."),
    FnoCandleColumn("close", "decimal", "bar", False, "Close price (₹, 2-dp tick grid)."),
    FnoCandleColumn("volume", "int", "bar", False, "Traded contracts in the bar."),
    FnoCandleColumn(
        "open_interest", "int", "bar", False, "Open interest — FIRST-CLASS for F&O (FUT/CE/PE)."
    ),
    FnoCandleColumn("exchange", "string", "partition", False, "NSE / BSE."),
    FnoCandleColumn("segment", "string", "partition", False, "Always FNO."),
    FnoCandleColumn("underlying", "string", "symbol", False, "Underlying (NIFTY / RELIANCE)."),
    FnoCandleColumn("expiry_date", "date", "symbol", False, "Contract expiry (DDMmmYY token)."),
    FnoCandleColumn("instrument_type", "string", "symbol", False, "FUT / CE / PE."),
    FnoCandleColumn("strike", "decimal", "symbol", True, "Option strike (₹); None for a future."),
    FnoCandleColumn("option_type", "string", "symbol", True, "CE / PE; None for a future."),
    FnoCandleColumn("interval", "string", "partition", False, "Candle interval (1minute fetched)."),
)


class SchemaMetaWriter(Protocol):
    """The minimal DuckDB-connection surface :func:`register_fno_candle_lake` needs.

    Declared as a :class:`~typing.Protocol` so this module stays free of any DuckDB
    import (it is registered from the instrument-master open path, which owns the real
    connection). ``parameters`` is positional to match the DuckDB ``execute`` call shape.
    """

    def execute(self, query: str, parameters: object = ..., /) -> object:
        """Execute *query* with optional bound *parameters*."""
        raise NotImplementedError  # structural Protocol stub — never executed


def fno_candle_lake_schema_meta() -> dict[str, str]:
    """Return the ``schema_meta`` key→value pairs that register the F&O candle lake.

    Every value is a ``VARCHAR`` (the ``schema_meta`` table is a flat key-value store),
    so the descriptor round-trips losslessly and is browsable in ``/db`` under the
    ``schema_meta`` table. Keys are namespaced under ``fno_candle_lake.`` so they never
    collide with the master's ``schema_version`` bookkeeping.
    """
    return {
        "fno_candle_lake.table": FNO_CANDLE_LAKE_TABLE,
        "fno_candle_lake.segment": FNO_CANDLE_LAKE_SEGMENT,
        "fno_candle_lake.schema_version": str(FNO_CANDLE_LAKE_SCHEMA_VERSION),
        "fno_candle_lake.path_template": FNO_CANDLE_LAKE_PATH_TEMPLATE,
        "fno_candle_lake.instrument_types": ",".join(sorted(FNO_INSTRUMENT_TYPES)),
        "fno_candle_lake.columns": ",".join(c.name for c in FNO_CANDLE_COLUMNS),
    }


def register_fno_candle_lake(conn: SchemaMetaWriter) -> None:
    """Idempotently register the F&O candle lake descriptor into ``schema_meta``.

    Called from :meth:`brutex.data.instrument_store.InstrumentStore.open` AFTER the
    ``schema_meta`` table exists and the master ``schema_version`` is stamped. Each key
    is written with ``INSERT OR REPLACE`` so re-opening a store always converges on the
    current descriptor (no migration needed at v1; a future shape bump re-stamps).

    Parameters
    ----------
    conn
        Any object exposing DuckDB's ``execute(query, parameters)`` (see
        :class:`SchemaMetaWriter`).
    """
    for key, value in fno_candle_lake_schema_meta().items():
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            [key, value],
        )


__all__ = [
    "FNO_CANDLE_COLUMNS",
    "FNO_CANDLE_LAKE_PATH_TEMPLATE",
    "FNO_CANDLE_LAKE_SCHEMA_VERSION",
    "FNO_CANDLE_LAKE_SEGMENT",
    "FNO_CANDLE_LAKE_TABLE",
    "FnoCandleColumn",
    "SchemaMetaWriter",
    "fno_candle_lake_schema_meta",
    "register_fno_candle_lake",
]
