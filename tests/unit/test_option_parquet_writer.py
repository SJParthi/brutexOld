"""Tests for `brutex.data.option_parquet_writer` — the expired-options table.

100% line+branch via a REAL temp lake (polars round-trip, no mocks): write +
read back, the 2dp money rounding (with full-precision iv), idempotent
re-pull (0 new), partial merge, month-split, empty input, and the
path-safety guards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import (
    OptionParquetWriter,
    OptionSeriesCoordinate,
    OptionWriterError,
)

_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"

_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM",
)


def _bar(
    *, side: str = "CE", minute: int = 0, month: int = 1, strike: str | None = "17500.005"
) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2021, month, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.005"),  # 3dp → must round to 100.01
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.004"),  # → 105.00
        volume=250,
        open_interest=1200,
        side=side,
        strike=None if strike is None else Decimal(strike),
        spot=Decimal("17480.567"),  # → 17480.57
        iv=Decimal("0.1523"),  # NOT money → keeps 4dp
    )


def _writer(tmp_path: Path) -> OptionParquetWriter:
    return OptionParquetWriter(lake_root=tmp_path)


def _read(tmp_path: Path, segment: str = "NSE_FNO__13__WEEK__0__ATM__CALL") -> pl.DataFrame:
    (f,) = (tmp_path / "options" / segment).glob("*.parquet")
    return pl.read_parquet(f)


class TestWrite:
    def test_write_rounds_money_keeps_iv_precision(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        n = w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        assert n == 1
        df = _read(tmp_path)
        assert df["open"][0] == 100.01  # 100.005 → 100.01 (half-up, 2dp)
        assert df["close"][0] == 105.00  # 105.004 → 105.00
        assert df["spot"][0] == 17480.57  # 17480.567 → 17480.57
        assert df["strike"][0] == 17500.01  # 17500.005 → 17500.01
        assert df["iv"][0] == pytest.approx(0.1523)  # NOT rounded — full precision
        assert df["side"][0] == "CE"

    def test_addressing_columns_written(self, tmp_path: Path) -> None:
        # DEC-EXPIRED-OPTIONS-TABLE-001: every series-addressing field is a
        # real, self-describing column on every bar (promoted from the path).
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        df = _read(tmp_path)
        # Final column order is the full 18-column expired-options schema.
        assert df.columns == [
            "exch_id",
            "exchange_segment",
            "security_id",
            "underlying_symbol",
            "expiry_flag",
            "expiry_code",
            "side",
            "strike_offset",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
            "strike",
            "spot",
            "iv",
        ]
        assert df["exch_id"][0] == "NSE"
        assert df["exchange_segment"][0] == "NSE_FNO"
        assert df["security_id"][0] == 13
        assert df["security_id"].dtype == pl.Int64
        assert df["underlying_symbol"][0] == "NIFTY"
        assert df["expiry_flag"][0] == "WEEK"
        assert df["expiry_code"][0] == 0
        assert df["expiry_code"].dtype == pl.Int64
        assert df["strike_offset"][0] == "ATM"  # requested selector, distinct from strike

    def test_empty_bars_returns_zero(self, tmp_path: Path) -> None:
        written = _writer(tmp_path).write(partition_key=_PK, coordinate=_COORD, bars=[])
        assert written == 0

    def test_idempotent_repull_writes_nothing(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        bars = [_bar(minute=0), _bar(minute=1)]
        first = w.write(partition_key=_PK, coordinate=_COORD, bars=bars)
        assert first == 2
        second = w.write(partition_key=_PK, coordinate=_COORD, bars=bars)
        assert second == 0  # all dup → no new rows
        assert _read(tmp_path).height == 2

    def test_partial_merge_adds_only_new(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        added = w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0), _bar(minute=1)])
        assert added == 1  # minute 0 dup, minute 1 new
        assert _read(tmp_path).height == 2

    def test_ce_and_pe_same_minute_both_kept(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        added = w.write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_bar(side="CE", minute=0), _bar(side="PE", minute=0)],
        )
        assert added == 2  # distinct side → distinct (side, ts) key
        assert _read(tmp_path).height == 2

    def test_bars_split_across_months(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)
        added = w.write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_bar(month=1, minute=0), _bar(month=2, minute=0)],
        )
        assert added == 2
        files = sorted((tmp_path / "options" / "NSE_FNO__13__WEEK__0__ATM__CALL").glob("*.parquet"))
        assert [f.name for f in files] == ["2021-01.parquet", "2021-02.parquet"]

    def test_none_strike_spot_persist_as_null(self, tmp_path: Path) -> None:
        w = _writer(tmp_path)

        bar = OptionBar(
            timestamp=datetime(2021, 1, 4, 4, 0, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=0,
            open_interest=None,
            side="CE",
            strike=None,
            spot=None,
            iv=None,
        )
        written = w.write(partition_key=_PK, coordinate=_COORD, bars=[bar])
        assert written == 1
        df = _read(tmp_path)
        assert df["strike"][0] is None
        assert df["open_interest"][0] is None


class TestAtomicWriteCleanup:
    def test_tmp_file_cleaned_up_on_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force os.replace to fail AFTER the tmp parquet is written → the
        # finally-branch must unlink the orphan tmp so no .tmp leaks.
        import brutex.data.option_parquet_writer as mod

        def _boom(src: object, dst: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(mod.os, "replace", _boom)
        w = _writer(tmp_path)
        with pytest.raises(OSError, match="disk full"):
            w.write(partition_key=_PK, coordinate=_COORD, bars=[_bar(minute=0)])
        # No orphan tmp file left behind.
        leftovers = list((tmp_path / "options").rglob("*.tmp"))
        assert leftovers == []


class TestPathSafety:
    def test_empty_partition_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OptionWriterError, match="non-empty"):
            _writer(tmp_path).write(partition_key="", coordinate=_COORD, bars=[_bar()])

    @pytest.mark.parametrize("bad", ["a/b", "a\\b", ".."])
    def test_unsafe_partition_key_raises(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(OptionWriterError, match="unsafe"):
            _writer(tmp_path).write(partition_key=bad, coordinate=_COORD, bars=[_bar()])

    def test_lake_root_property(self, tmp_path: Path) -> None:
        assert _writer(tmp_path).lake_root == tmp_path
