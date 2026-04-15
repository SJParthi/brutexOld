"""Unit tests for brutex.data.partition — pure path computation."""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest
from brutex.data import (
    BAR_DIR_NAME,
    PartitionError,
    build_partition_path,
    parse_partition_path,
    partition_for_timestamp,
)

# ────────────────────────────────────────────────────────────────────────
# build_partition_path
# ────────────────────────────────────────────────────────────────────────


class TestBuildPartitionPath:
    def test_happy_path_nse_fno(self, tmp_path: Path) -> None:
        p = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-02Jan25-28500-PE",
            interval="1minute",
            year=2025,
            month=1,
        )
        assert p == (
            tmp_path
            / "bars"
            / "NSE"
            / "FNO"
            / "NSE-NIFTY-02Jan25-28500-PE"
            / "1minute"
            / "2025"
            / "01.parquet"
        )

    def test_happy_path_bse_fno(self, tmp_path: Path) -> None:
        p = build_partition_path(
            lake_root=tmp_path,
            exchange="BSE",
            segment="FNO",
            symbol="BSE-SENSEX-25Sep25-79500-CE",
            interval="15minute",
            year=2025,
            month=9,
        )
        assert p.name == "09.parquet"
        assert "BSE" in p.parts
        assert "SENSEX" in str(p)

    def test_month_zero_padded(self, tmp_path: Path) -> None:
        p = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1day",
            year=2020,
            month=3,
        )
        assert p.name == "03.parquet"

    def test_uses_bar_dir_name_constant(self, tmp_path: Path) -> None:
        p = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            year=2024,
            month=12,
        )
        assert BAR_DIR_NAME in p.parts


class TestBuildPartitionValidation:
    def test_rejects_unknown_exchange(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError, match="exchange"):
            build_partition_path(
                lake_root=tmp_path,
                exchange="MCX",
                segment="FNO",
                symbol="X",
                interval="1minute",
                year=2024,
                month=1,
            )

    def test_rejects_unknown_segment(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError, match="segment"):
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="COMMODITY",
                symbol="X",
                interval="1minute",
                year=2024,
                month=1,
            )

    def test_rejects_unknown_interval(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError, match="interval"):
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="2hour",  # not in the 12-interval whitelist
                year=2024,
                month=1,
            )

    def test_rejects_empty_symbol(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError, match="symbol"):
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="",
                interval="1minute",
                year=2024,
                month=1,
            )

    @pytest.mark.parametrize("bad", ["a/b", "c:d", "e*f", "g?h", "i<j"])
    def test_rejects_symbol_with_forbidden_chars(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(PartitionError, match="forbidden"):
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol=bad,
                interval="1minute",
                year=2024,
                month=1,
            )

    @pytest.mark.parametrize("bad_month", [0, 13, -1, 100])
    def test_rejects_bad_month(self, tmp_path: Path, bad_month: int) -> None:
        with pytest.raises(PartitionError, match="month"):
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="1minute",
                year=2024,
                month=bad_month,
            )

    @pytest.mark.parametrize("bad_year", [1999, 2101, 0, -1])
    def test_rejects_bad_year(self, tmp_path: Path, bad_year: int) -> None:
        with pytest.raises(PartitionError, match="year"):
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="1minute",
                year=bad_year,
                month=1,
            )


# ────────────────────────────────────────────────────────────────────────
# parse_partition_path — inverse of build_partition_path
# ────────────────────────────────────────────────────────────────────────


class TestParsePartitionPath:
    def test_round_trip(self, tmp_path: Path) -> None:
        built = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-02Jan25-28500-PE",
            interval="1minute",
            year=2025,
            month=1,
        )
        parsed = parse_partition_path(built)
        assert parsed == {
            "exchange": "NSE",
            "segment": "FNO",
            "symbol": "NSE-NIFTY-02Jan25-28500-PE",
            "interval": "1minute",
            "year": 2025,
            "month": 1,
        }

    def test_round_trip_with_lake_root(self, tmp_path: Path) -> None:
        built = build_partition_path(
            lake_root=tmp_path,
            exchange="BSE",
            segment="FNO",
            symbol="BSE-SENSEX-25Sep25-79500-CE",
            interval="1day",
            year=2025,
            month=9,
        )
        built.parent.mkdir(parents=True, exist_ok=True)
        built.touch()
        parsed = parse_partition_path(built, lake_root=tmp_path)
        assert parsed["exchange"] == "BSE"
        assert parsed["month"] == 9

    def test_rejects_too_short_path(self) -> None:
        with pytest.raises(PartitionError, match="at least 7"):
            parse_partition_path(Path("bars/NSE/FNO/01.parquet"))

    def test_rejects_missing_bars_dir(self) -> None:
        # Construct a path that has 7+ components but the wrong top
        bogus = Path("not_bars/NSE/FNO/NSE-NIFTY/1minute/2024/01.parquet")
        with pytest.raises(PartitionError, match="under bars"):
            parse_partition_path(bogus)

    def test_rejects_non_parquet_file(self) -> None:
        bogus = Path("bars/NSE/FNO/NSE-NIFTY/1minute/2024/01.json")
        with pytest.raises(PartitionError, match=r"\.parquet"):
            parse_partition_path(bogus)

    def test_rejects_non_integer_month(self) -> None:
        bogus = Path("bars/NSE/FNO/NSE-NIFTY/1minute/2024/Jan.parquet")
        with pytest.raises(PartitionError, match="month"):
            parse_partition_path(bogus)

    def test_rejects_non_integer_year(self) -> None:
        bogus = Path("bars/NSE/FNO/NSE-NIFTY/1minute/twenty24/01.parquet")
        with pytest.raises(PartitionError, match="year"):
            parse_partition_path(bogus)

    def test_rejects_path_outside_lake_root(self, tmp_path: Path) -> None:
        other = tmp_path / "other"
        other.mkdir()
        outside_file = other / "bars" / "NSE" / "FNO" / "X" / "1minute" / "2024" / "01.parquet"
        outside_file.parent.mkdir(parents=True)
        outside_file.touch()
        with pytest.raises(PartitionError, match="not inside"):
            parse_partition_path(outside_file, lake_root=tmp_path / "lake")


# ────────────────────────────────────────────────────────────────────────
# partition_for_timestamp
# ────────────────────────────────────────────────────────────────────────


class TestPartitionForTimestamp:
    def test_utc_midnight(self) -> None:
        ts = datetime(2024, 7, 15, 0, 0, tzinfo=UTC)
        assert partition_for_timestamp(ts) == (2024, 7)

    def test_ist_converts_to_utc(self) -> None:
        # 2024-03-01 00:30 IST = 2024-02-29 19:00 UTC — so partition (2024, 2)
        ist = timezone(offset=datetime.now(UTC).utcoffset() or UTC.utcoffset(datetime.now(UTC)))
        # Use a fixed IST +05:30 explicitly:
        from datetime import timedelta

        ist = timezone(timedelta(hours=5, minutes=30))
        ts = datetime(2024, 3, 1, 0, 30, tzinfo=ist)
        assert partition_for_timestamp(ts) == (2024, 2)

    def test_rejects_naive_datetime(self) -> None:
        ts = datetime(2024, 1, 1, 0, 0)  # naive
        with pytest.raises(PartitionError, match="tz-aware"):
            partition_for_timestamp(ts)
