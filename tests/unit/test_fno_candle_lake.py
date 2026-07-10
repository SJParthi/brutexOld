"""Unit tests for :mod:`brutex.data.fno_candle_lake` — the F&O candle logical table.

The descriptor (DEC-FNO-CANDLE-LAKE-001): the documented schema of the already-stored
F&O candle lake + its idempotent ``schema_meta`` registration. Covers the column set
(7 physical OHLCV + the identity columns; ``open_interest`` first-class non-null), the
``schema_meta`` key→value mapping, and the registration writing + idempotence.
"""

from __future__ import annotations

import pytest
from brutex.data.fno_candle_lake import (
    FNO_CANDLE_COLUMNS,
    FNO_CANDLE_LAKE_PATH_TEMPLATE,
    FNO_CANDLE_LAKE_SCHEMA_VERSION,
    FNO_CANDLE_LAKE_SEGMENT,
    FNO_CANDLE_LAKE_TABLE,
    FnoCandleColumn,
    fno_candle_lake_schema_meta,
    register_fno_candle_lake,
)


class TestDescriptorConstants:
    """The logical-table identity is stable + documented."""

    def test_table_segment_version(self) -> None:
        assert FNO_CANDLE_LAKE_TABLE == "fno_candles"
        assert FNO_CANDLE_LAKE_SEGMENT == "FNO"
        assert FNO_CANDLE_LAKE_SCHEMA_VERSION == 3

    def test_path_template_is_the_canonical_bars_layout(self) -> None:
        rendered = FNO_CANDLE_LAKE_PATH_TEMPLATE.format(
            exchange="NSE",
            groww_symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            year=2025,
            month=6,
        )
        assert rendered == "bars/NSE/FNO/NSE-NIFTY-30Jun25-24000-CE/1minute/2025/06.parquet"


class TestColumns:
    """The 7 OHLCV columns + the partition/symbol identity columns."""

    def test_seven_bar_columns_then_identity(self) -> None:
        names = [c.name for c in FNO_CANDLE_COLUMNS]
        assert names[:7] == ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
        # identity columns the spot key lacks
        for col in ("underlying", "expiry_date", "instrument_type", "strike", "option_type"):
            assert col in names

    def test_open_interest_is_first_class_non_null(self) -> None:
        oi = next(c for c in FNO_CANDLE_COLUMNS if c.name == "open_interest")
        assert oi.source == "bar"
        assert oi.nullable is False

    def test_strike_and_option_type_nullable_for_futures(self) -> None:
        for name in ("strike", "option_type"):
            col = next(c for c in FNO_CANDLE_COLUMNS if c.name == name)
            assert col.nullable is True

    def test_every_column_is_a_frozen_descriptor(self) -> None:
        col = FNO_CANDLE_COLUMNS[0]
        assert isinstance(col, FnoCandleColumn)
        with pytest.raises(AttributeError):
            col.name = "x"  # type: ignore[misc]

    def test_sources_are_known_kinds(self) -> None:
        assert {c.source for c in FNO_CANDLE_COLUMNS} <= {"bar", "partition", "symbol", "derived"}

    def test_derived_moneyness_column(self) -> None:
        # DEC-FNO-MONEYNESS-001: ONE read-time derived (never stored) combined column —
        # bucket + signed depth as a single "ITM -1" / "OTM +10" string.
        moneyness = next(c for c in FNO_CANDLE_COLUMNS if c.name == "moneyness")
        assert moneyness.source == "derived"
        assert moneyness.nullable is True
        assert moneyness.dtype == "string"
        # the old separate atm_offset column is folded into the moneyness label.
        assert all(c.name != "atm_offset" for c in FNO_CANDLE_COLUMNS)


class TestSchemaMetaMapping:
    """The flat key→value registration payload."""

    def test_keys_namespaced_and_complete(self) -> None:
        meta = fno_candle_lake_schema_meta()
        assert set(meta) == {
            "fno_candle_lake.table",
            "fno_candle_lake.segment",
            "fno_candle_lake.schema_version",
            "fno_candle_lake.path_template",
            "fno_candle_lake.instrument_types",
            "fno_candle_lake.columns",
        }

    def test_values_round_trip_as_strings(self) -> None:
        meta = fno_candle_lake_schema_meta()
        assert meta["fno_candle_lake.table"] == "fno_candles"
        assert meta["fno_candle_lake.segment"] == "FNO"
        assert meta["fno_candle_lake.schema_version"] == "3"
        assert meta["fno_candle_lake.instrument_types"] == "CE,FUT,PE"
        assert meta["fno_candle_lake.columns"].split(",") == [c.name for c in FNO_CANDLE_COLUMNS]
        assert all(isinstance(v, str) for v in meta.values())


class TestRegistration:
    """``register_fno_candle_lake`` writes the descriptor + is idempotent."""

    def test_registers_all_keys(self) -> None:
        duckdb = pytest.importorskip("duckdb")
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
        register_fno_candle_lake(conn)
        rows = dict(
            conn.execute(
                "SELECT key, value FROM schema_meta WHERE key LIKE 'fno_candle_lake%'"
            ).fetchall()
        )
        assert rows == fno_candle_lake_schema_meta()

    def test_idempotent(self) -> None:
        duckdb = pytest.importorskip("duckdb")
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
        register_fno_candle_lake(conn)
        register_fno_candle_lake(conn)  # re-stamp must not raise / duplicate
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM schema_meta WHERE key LIKE 'fno_candle_lake%'"
        ).fetchone()
        assert count == len(fno_candle_lake_schema_meta())


class TestStoredGreeksOverlayColumns:
    """The ten Phase-2b stored overlay columns (DEC-GREEKS-ENGINE-PR2B-001, plan §15.3)."""

    def test_overlay_columns_appended_after_open_interest(self) -> None:
        from brutex.data.models import GREEKS_OVERLAY_FIELDS

        names = [c.name for c in FNO_CANDLE_COLUMNS]
        oi_idx = names.index("open_interest")
        assert tuple(names[oi_idx + 1 : oi_idx + 1 + len(GREEKS_OVERLAY_FIELDS)]) == (
            GREEKS_OVERLAY_FIELDS
        )

    def test_overlay_columns_are_nullable_bar_columns(self) -> None:
        from brutex.data.models import GREEKS_OVERLAY_FIELDS

        for name in GREEKS_OVERLAY_FIELDS:
            col = next(c for c in FNO_CANDLE_COLUMNS if c.name == name)
            assert col.source == "bar"  # PHYSICAL — written by the enrich pass
            assert col.nullable is True  # legacy / FUT / unsolvable rows read NULL

    def test_cheap_family_stays_derived(self) -> None:
        # The COST-CLASS hybrid (ADR-0015 D10): moneyness/spot/intrinsic/
        # time_value/days_to_expiry remain read-time derived, never stored.
        for name in (
            "moneyness",
            "spot",
            "intrinsic",
            "time_value",
            "days_to_expiry",
            "greeks_status",
        ):
            col = next(c for c in FNO_CANDLE_COLUMNS if c.name == name)
            assert col.source == "derived"
