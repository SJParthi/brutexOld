"""Tests for the Dhan rolling-ATM option Parquet writer (honest rolling-ATM lake).

DEC-DHAN-ROLLING-OPTIONS-001.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from brutex.adapters.dhan.rolling_option_constants import ROLLING_OPTIONS_SCHEMA_VERSION
from brutex.adapters.dhan.rolling_option_parser import (
    RESOLVED_EXPIRY_SOURCE_DERIVED,
    RollingOptionBar,
)
from brutex.adapters.dhan.rolling_option_writer import (
    RollingOptionWriter,
    RollingOptionWriterError,
    strike_offset_safe,
)
from brutex.data.option_parquet_writer import OptionSeriesCoordinate

_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM+1",
)


def _bar(
    minute: int,
    *,
    close: str = "101",
    open_: str = "100",
    high: str = "102",
    low: str = "99",
    strike: str | None = "24000",
    expiry: date | None = date(2024, 6, 27),
    iv: str | None = "0.15",
) -> RollingOptionBar:
    return RollingOptionBar(
        timestamp=datetime(2024, 6, 3, 3, minute, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10,
        open_interest=5,
        iv=None if iv is None else Decimal(iv),
        spot=Decimal("24010"),
        resolved_strike=None if strike is None else Decimal(strike),
        resolved_expiry_date=expiry,
        resolved_expiry_source=RESOLVED_EXPIRY_SOURCE_DERIVED,
    )


def _write(writer: RollingOptionWriter, bars: list[RollingOptionBar], **over: object) -> int:
    return writer.write(
        coordinate=_COORD,
        option_type=str(over.get("option_type", "CALL")),
        interval=str(over.get("interval", "1minute")),
        bars=bars,
    )


def _the_file(lake_root: Path) -> Path:
    files = list(lake_root.glob("rolling_options/**/*.parquet"))
    assert len(files) == 1, files
    return files[0]


# ── strike_offset_safe ────────────────────────────────────────────────────────


def test_strike_offset_safe_mapping() -> None:
    assert strike_offset_safe("ATM") == "ATM"
    assert strike_offset_safe("ATM+1") == "ATMp1"
    assert strike_offset_safe("ATM-10") == "ATMm10"


def test_strike_offset_safe_empty_fails_loud() -> None:
    with pytest.raises(RollingOptionWriterError, match="non-empty"):
        strike_offset_safe("")


# ── partition path ────────────────────────────────────────────────────────────


def test_write_builds_the_honest_rolling_partition_path(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    added = _write(writer, [_bar(45)])
    assert added == 1
    path = _the_file(tmp_path)
    rel = path.relative_to(tmp_path).as_posix()
    assert rel == "rolling_options/NSE/NIFTY/WEEK/EC0/ATMp1/CALL/1minute/2024/06.parquet"


def test_write_empty_bars_returns_zero_and_writes_nothing(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    assert _write(writer, []) == 0
    assert list(tmp_path.glob("rolling_options/**/*.parquet")) == []


def test_row_schema_has_echo_perrow_and_null_greeks_overlay(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    _write(writer, [_bar(45)])
    df = pl.read_parquet(_the_file(tmp_path))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["schema_version"] == ROLLING_OPTIONS_SCHEMA_VERSION
    # coordinate echo
    assert row["exch_id"] == "NSE"
    assert row["exchange_segment"] == "NSE_FNO"
    assert row["security_id"] == 13
    assert row["underlying_symbol"] == "NIFTY"
    assert row["expiry_flag"] == "WEEK"
    assert row["expiry_code"] == 0
    assert row["strike_offset"] == "ATM+1"  # honest label kept verbatim
    assert row["option_type"] == "CALL"
    assert row["interval"] == "1minute"
    # per-ROW resolved metadata (the rolling-ATM crux — COLUMNS not keys)
    assert row["resolved_strike"] == 24000.0
    assert row["resolved_expiry_date"] == date(2024, 6, 27)
    assert row["resolved_expiry_source"] == "derived"
    # nullable Greeks overlay reserved (all-null this pass)
    for name in (
        "iv_used",
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
        "spot_at_bar",
        "t_years_used",
        "rate_used",
        "greeks_provenance_id",
        "iv_source",
    ):
        assert name in df.columns
        assert row[name] is None
    assert df.schema["greeks_provenance_id"] == pl.Int64
    assert df.schema["iv_source"] == pl.Utf8


def test_money_2dp_half_up_snap(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    _write(writer, [_bar(45, open_="100.005", high="200", low="1", close="100")])
    df = pl.read_parquet(_the_file(tmp_path))
    assert df.row(0, named=True)["open"] == 100.01  # HALF-UP, not truncation


def test_iv_keeps_full_precision(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    _write(writer, [_bar(45, iv="0.123456789")])
    df = pl.read_parquet(_the_file(tmp_path))
    assert df.row(0, named=True)["iv"] == pytest.approx(0.123456789)


# ── dedup (timestamp-only keep-first) ────────────────────────────────────────


def test_repull_is_idempotent_bars_net_new_zero(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    assert _write(writer, [_bar(45), _bar(46)]) == 2
    # Re-pull the SAME bars → nothing new.
    assert _write(writer, [_bar(45), _bar(46)]) == 0
    df = pl.read_parquet(_the_file(tmp_path))
    assert df.height == 2


def test_timestamp_only_dedup_keep_first(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    _write(writer, [_bar(45, close="101")])
    # Same timestamp, DIFFERENT (in-range) close → keep-FIRST (stored value wins), 0 new.
    assert _write(writer, [_bar(45, close="100")]) == 0
    df = pl.read_parquet(_the_file(tmp_path))
    assert df.height == 1
    assert df.row(0, named=True)["close"] == 101.0


def test_one_expiry_code_partition_spans_multiple_resolved_expiries(tmp_path: Path) -> None:
    # THE honesty pin: a FIXED expiry_code partition legitimately holds >=2 distinct
    # resolved_expiry_date across a roll (they are per-ROW columns, not keys).
    writer = RollingOptionWriter(lake_root=tmp_path)
    added = _write(
        writer,
        [
            _bar(45, expiry=date(2024, 6, 6)),
            _bar(46, expiry=date(2024, 6, 13)),  # rolled to the next weekly expiry
        ],
    )
    assert added == 2
    df = pl.read_parquet(_the_file(tmp_path))
    assert df.height == 2
    distinct_expiries = set(df.get_column("resolved_expiry_date").to_list())
    assert distinct_expiries == {date(2024, 6, 6), date(2024, 6, 13)}
    # All in ONE partition file under the SAME fixed EC0 selector.
    assert set(df.get_column("expiry_code").to_list()) == {0}


def test_multi_month_writes_two_partitions(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    jun = _bar(45)
    jul = RollingOptionBar(
        timestamp=datetime(2024, 7, 1, 3, 45, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=10,
        open_interest=5,
        iv=Decimal("0.15"),
        spot=Decimal("24010"),
        resolved_strike=Decimal("24000"),
        resolved_expiry_date=date(2024, 7, 25),
        resolved_expiry_source=RESOLVED_EXPIRY_SOURCE_DERIVED,
    )
    assert _write(writer, [jun, jul]) == 2
    files = sorted(tmp_path.glob("rolling_options/**/*.parquet"))
    assert [f.name for f in files] == ["06.parquet", "07.parquet"]


def test_corrupt_existing_partition_raises(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    _write(writer, [_bar(45)])
    _the_file(tmp_path).write_bytes(b"not a parquet file")
    with pytest.raises(RollingOptionWriterError, match="corrupt existing rolling-option"):
        _write(writer, [_bar(46)])


# ── _safe path-segment guard ─────────────────────────────────────────────────


def test_write_rejects_path_hazard_in_coordinate(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    bad = OptionSeriesCoordinate(
        exch_id="../escape",
        exchange_segment="NSE_FNO",
        security_id=13,
        underlying_symbol="NIFTY",
        expiry_flag="WEEK",
        expiry_code=0,
        strike_offset="ATM",
    )
    with pytest.raises(RollingOptionWriterError, match="unsafe exchange"):
        writer.write(coordinate=bad, option_type="CALL", interval="1minute", bars=[_bar(45)])


def test_write_rejects_empty_segment(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    with pytest.raises(RollingOptionWriterError, match="option_type must be non-empty"):
        writer.write(coordinate=_COORD, option_type="", interval="1minute", bars=[_bar(45)])


def test_lake_root_property(tmp_path: Path) -> None:
    writer = RollingOptionWriter(lake_root=tmp_path)
    assert writer.lake_root == tmp_path
