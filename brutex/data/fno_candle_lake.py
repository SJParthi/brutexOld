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

from brutex.data.fno_symbol import FNO_INSTRUMENT_TYPES, OPTION_TYPES, parse_fno_symbol

#: The logical table name surfaced in ``/db`` + registered in ``schema_meta``.
FNO_CANDLE_LAKE_TABLE: Final[str] = "fno_candles"

#: The lake segment every F&O candle partition lives under (the
#: :mod:`brutex.data.partition` path component; vendor ``FNO``).
FNO_CANDLE_LAKE_SEGMENT: Final[str] = "FNO"

#: Schema version of the F&O candle logical table. Bumped only on a non-additive
#: shape change; the registration is idempotent (``INSERT OR REPLACE``) so re-opening
#: an existing master always re-stamps the current descriptor. v3: the ten nullable
#: Greeks-overlay columns (``iv``…``greeks_provenance_id``) became PHYSICAL
#: ``source="bar"`` columns written by the generation-time enrichment
#: (`DEC-GREEKS-GENERATION-TIME-001` / plan §15.3 / ADR-0015 D11 — a shape change, so
#: the version bumps). The ON-DISK change is nevertheless ADDITIVE: every new column
#: is nullable and appended after ``open_interest``, so a legacy 7-column partition
#: reads them as NULL with NO migration (the writer's schema-tolerant read-back).
#: v2: the two #865 derived columns ``(moneyness, atm_offset)`` were collapsed into
#: ONE combined ``moneyness`` label (DEC-FNO-MONEYNESS-LABEL-001) — a column dropped
#: from the advertised set is a non-additive change, so the version bumped (no
#: on-disk migration: both were always ``source="derived"``, never physical columns).
FNO_CANDLE_LAKE_SCHEMA_VERSION: Final[int] = 3

#: The on-disk partition path template (``str.format`` placeholders), documenting
#: where a row physically lives. ``groww_symbol`` is the full contract symbol
#: (``NSE-NIFTY-30Sep25-24650-CE``), the single key the candle fetch + lake-write use.
FNO_CANDLE_LAKE_PATH_TEMPLATE: Final[str] = (
    "bars/{exchange}/FNO/{groww_symbol}/{interval}/{year:04d}/{month:02d}.parquet"
)


def is_option_contract_coords(segment: str, symbol: str) -> bool:
    """``True`` iff ``(segment, symbol)`` names a CE/PE OPTION contract partition.

    The ONE 1-minute-native gate predicate (CLAUDE.md §11 COMMON) behind
    DEC-OPTIONS-PREBUILD-RETIRE-001 — the operator ruling of 2026-07-15 as
    relayed via the coordinator (ADR-0021 RULING B, draft PR #1555): option
    data stays 1-minute-native, so derived-timeframe producers (the pipeline
    prebuild, the lake-heal daily recall) and the derived-``1day`` readers
    (the movers prev-close/prev-OI baselines, the doctor freshness probe)
    all gate on exactly this predicate — never a re-minted copy.

    A coordinate is an option contract iff its lake segment is the F&O
    candle-lake segment (:data:`FNO_CANDLE_LAKE_SEGMENT`) AND its symbol
    parses under the canonical contract grammar
    (:func:`brutex.data.fno_symbol.parse_fno_symbol`) to a CE/PE
    ``instrument_type``. A FUT contract, a spot coordinate, and an
    UNDECODABLE F&O symbol all read ``False`` — futures/spot keep their
    derived timeframes byte-identically, and an unparsable symbol
    conservatively keeps its pre-ruling behaviour (never guessed to be an
    option). Pure — no I/O.
    """
    if segment != FNO_CANDLE_LAKE_SEGMENT:
        return False
    parts = parse_fno_symbol(symbol)
    return parts is not None and parts.instrument_type in OPTION_TYPES


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
        bar), ``"partition"`` (encoded in the lake path), ``"symbol"`` (decoded
        from the contract ``groww_symbol`` via
        :func:`~brutex.data.fno_symbol.parse_fno_symbol`), or ``"derived"`` (computed
        at READ time by the ``/db`` browser against the underlying spot — NEVER stored,
        so it can never go stale: :mod:`brutex.data.moneyness`, DEC-FNO-MONEYNESS-001).
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
    # STORED Greeks overlay (DEC-GREEKS-GENERATION-TIME-001 / DEC-GREEKS-ENGINE-PR2B-001,
    # plan §15.3 / ADR-0015 D11) — the ten nullable PHYSICAL columns the post-pull
    # ``greeks_enrich_after_pull`` writes for OPTION (CE/PE) bars via the shared Rust
    # Black-76 kernel. The EXPENSIVE per-bar-IV-solve family is stored once at
    # generation time; the CHEAP family below (moneyness/spot/intrinsic/…) stays
    # read-time derived. NULL on a legacy / never-enriched partition, on every FUT bar
    # (never a bogus Greek for a future), and on any honestly-unsolvable bar (the
    # stamped reason rides ``greeks_provenance_id`` — plan §15.8). ``iv``…``rho`` are
    # FULL precision (DEC-MONEY-2DP-001 forbids rounding statistical values);
    # ``spot_at_bar`` is the ONE 2dp-snapped price.
    FnoCandleColumn("iv", "float", "bar", True, "Solved implied volatility (annualized)."),
    FnoCandleColumn("delta", "float", "bar", True, "∂price/∂spot (signed; CE + / PE −)."),
    FnoCandleColumn("gamma", "float", "bar", True, "∂delta/∂spot (>= 0)."),
    FnoCandleColumn("theta", "float", "bar", True, "∂price/∂time per Act/365 calendar day."),
    FnoCandleColumn("vega", "float", "bar", True, "∂price/∂σ per 1 vol-point (>= 0)."),
    FnoCandleColumn("rho", "float", "bar", True, "∂price/∂r per 1% rate (signed)."),
    FnoCandleColumn(
        "spot_at_bar",
        "decimal",
        "bar",
        True,
        "Underlying spot close at the SAME minute, materialized by the enrich join (₹, 2-dp).",
    ),
    FnoCandleColumn(
        "t_years_used", "float", "bar", True, "Time-to-expiry (years) frozen into the Greeks."
    ),
    FnoCandleColumn("rate_used", "float", "bar", True, "Risk-free r frozen into the Greeks."),
    FnoCandleColumn(
        "greeks_provenance_id",
        "int",
        "bar",
        True,
        "Packed provenance stamp (formula/rate/q/T/style/status) — the staleness key.",
    ),
    FnoCandleColumn("exchange", "string", "partition", False, "NSE / BSE."),
    FnoCandleColumn("segment", "string", "partition", False, "Always FNO."),
    FnoCandleColumn("underlying", "string", "symbol", False, "Underlying (NIFTY / RELIANCE)."),
    FnoCandleColumn("expiry_date", "date", "symbol", False, "Contract expiry (DDMmmYY token)."),
    FnoCandleColumn("instrument_type", "string", "symbol", False, "FUT / CE / PE."),
    FnoCandleColumn("strike", "decimal", "symbol", True, "Option strike (₹); None for a future."),
    FnoCandleColumn("option_type", "string", "symbol", True, "CE / PE; None for a future."),
    FnoCandleColumn("interval", "string", "partition", False, "Candle interval (1minute fetched)."),
    # READ-time derived (never stored) — moneyness of THIS option vs the underlying
    # spot at each bar's instant (DEC-FNO-MONEYNESS-001). Time-varying (the strike is
    # fixed but the spot moves), so it must be derived, never persisted. One combined
    # cell: bucket + signed strike-depth ("ITM -1" / "OTM +10" / "ATM"); "—" for a
    # future, "?" when the underlying spot is absent from the lake.
    FnoCandleColumn(
        "moneyness",
        "string",
        "derived",
        True,
        'ITM/ATM/OTM + signed strike-depth vs the per-bar spot ("ITM -1" / "OTM +10"; '
        "— future · ? if no spot).",
    ),
    # READ-time derived value columns (DEC-DB-FNO-COLUMNS-001) — the option's worth at a
    # glance, all reusing the SAME per-bar spot join moneyness already does (no extra read).
    # ``None`` for a future / when the underlying spot is absent; ``days_to_expiry`` is pure
    # (no I/O) and resolves for any decodable contract (option OR future).
    FnoCandleColumn(
        "spot", "decimal", "derived", True, "Underlying spot at the bar (₹, 2-dp); None if absent."
    ),
    FnoCandleColumn(
        "intrinsic",
        "decimal",
        "derived",
        True,
        "Intrinsic max(0, spot−strike) CE / max(0, strike−spot) PE (₹, 2-dp); None if no spot.",
    ),
    FnoCandleColumn(
        "time_value",
        "decimal",
        "derived",
        True,
        "Time value = close − intrinsic (₹, 2-dp); None if no spot.",
    ),
    FnoCandleColumn(
        "days_to_expiry",
        "int",
        "derived",
        True,
        "Calendar days from the bar's IST date to expiry; None if undecodable.",
    ),
    # READ-time derived Greeks status (DEC-DB-FNO-DISPLAY-TRUTH-001 item 3) — the stored
    # ``greeks_provenance_id`` stamp's packed status decoded into words per bar via the ONE
    # COMMON ``brutex.data.greeks_provenance.greeks_status_label``, so a NULL iv/delta is
    # never explained only by a bare packed int. ADDITIVE — no schema-version bump (the
    # version bumps only on a non-additive shape change; contrast v2, which DROPPED a column).
    FnoCandleColumn(
        "greeks_status",
        "string",
        "derived",
        True,
        'Decoded Greeks provenance status ("ok" / "below intrinsic — no unique IV" / …; '
        '"not enriched" when no stamp — Greeks are stored on the 1minute partition; '
        '"—" for a future).',
    ),
)


class SchemaMetaWriter(Protocol):
    """The minimal DuckDB-connection surface :func:`register_fno_candle_lake` needs.

    Declared as a :class:`~typing.Protocol` so this module stays free of any DuckDB
    import (it is registered from the instrument-master open path, which owns the real
    connection). ``parameters`` is positional to match the DuckDB ``execute`` call shape.
    """

    def execute(self, _query: str, _parameters: object = ..., /) -> object:
        """Execute a SQL statement with optional bound parameters (positional-only).

        The parameters are positional-only (``/``) so this Protocol matches DuckDB's
        ``execute(query, parameters)`` by POSITION; the names carry a leading underscore
        only because a structural stub never reads them (the vulture dead-code gate).
        """
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
    "is_option_contract_coords",
    "register_fno_candle_lake",
]
