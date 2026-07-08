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
from brutex.data.partition import SPOT_PROBE_SEGMENTS

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

    def test_accepts_index_segment(self, tmp_path: Path) -> None:
        # DEC-UNDERLYING-LEG-ECONOMICS-001 / DEC-INDEX-SEGMENT-PK-FIX-001 (#406):
        # index instruments carry segment='INDEX', so a NIFTY/BANKNIFTY spot
        # candle pull routes 'INDEX' through the partition path — it must be
        # accepted (it crashed every index spot pull before this).
        p = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            year=2024,
            month=1,
        )
        assert "INDEX" in p.parts

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
                interval="45minute",  # not in the 23-interval whitelist
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
        # DEC-P0-070: removed an earlier ambiguous ``ist =`` assignment
        # that was immediately overwritten by the explicit IST +05:30
        # below. Closes CodeQL alert #3 (``py/multiple-definition``).
        from datetime import timedelta

        ist = timezone(timedelta(hours=5, minutes=30))
        ts = datetime(2024, 3, 1, 0, 30, tzinfo=ist)
        assert partition_for_timestamp(ts) == (2024, 2)

    def test_rejects_naive_datetime(self) -> None:
        ts = datetime(2024, 1, 1, 0, 0)  # naive
        with pytest.raises(PartitionError, match="tz-aware"):
            partition_for_timestamp(ts)


# ────────────────────────────────────────────────────────────────────────
# Mutation-kill pins (DEC-MUTATION-RUNTIME-001)
# ────────────────────────────────────────────────────────────────────────


class TestIntervalWhitelistPins:
    """Every one of the 23 canonical intervals must be ACCEPTED by name.

    mutmut's "XX…XX"-wrapped frozenset-member mutants silently drop the
    real spelling from ``VALID_INTERVALS``; pinning acceptance of each
    literal turns every such mutant into a PartitionError, killing it
    (DEC-MUTATION-RUNTIME-001).
    """

    @pytest.mark.parametrize(
        "interval",
        [
            "1minute",
            "2minute",
            "3minute",
            "4minute",
            "5minute",
            "6minute",
            "7minute",
            "8minute",
            "9minute",
            "10minute",
            "11minute",
            "12minute",
            "13minute",
            "14minute",
            "15minute",
            "30minute",
            "1hour",
            "2hour",
            "3hour",
            "4hour",
            "1day",
            "1week",
            "1month",
        ],
    )
    def test_accepts_each_canonical_interval(self, tmp_path: Path, interval: str) -> None:
        p = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="X",
            interval=interval,
            year=2024,
            month=1,
        )
        assert p.parts[-3] == interval


class TestYearBoundaryPins:
    """Pin the inclusive ``[2000, 2100]`` year envelope at both edges.

    Accepting EXACTLY 2000 kills both the ``MIN_PARTITION_YEAR`` 2000 →
    2001 constant mutant and the ``year < MIN`` → ``year <= MIN`` flip;
    accepting EXACTLY 2100 kills the ``year > MAX`` → ``year >= MAX``
    flip. The existing reject tests (1999 / 2101) cannot see any of the
    three (DEC-MUTATION-RUNTIME-001).
    """

    def test_accepts_min_year_exactly_2000(self, tmp_path: Path) -> None:
        p = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="X",
            interval="1minute",
            year=2000,
            month=1,
        )
        assert p.parts[-2] == "2000"

    def test_accepts_max_year_exactly_2100(self, tmp_path: Path) -> None:
        p = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="X",
            interval="1minute",
            year=2100,
            month=12,
        )
        assert p.parts[-2] == "2100"


class TestForbiddenSymbolCharPins:
    """All 10 forbidden filesystem chars are rejected, message byte-exact.

    The 5-char sample above leaves the ``\\``, ``*``, ``"``, ``>``, ``|``
    and NUL set-member mutants alive; covering every member makes each
    "XX…XX" frozenset-member mutant flip an expected rejection into an
    accepted path. The ``==`` on ``str(exc.value)`` additionally kills
    the wrapped-message mutant, which a substring ``match=`` cannot
    (DEC-MUTATION-RUNTIME-001).
    """

    @pytest.mark.parametrize("ch", ["/", "\\", ":", "*", "?", '"', "<", ">", "|", "\0"])
    def test_rejects_each_forbidden_char(self, tmp_path: Path, ch: str) -> None:
        symbol = f"a{ch}b"
        with pytest.raises(PartitionError) as exc:
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol=symbol,
                interval="1minute",
                year=2024,
                month=1,
            )
        assert str(exc.value) == f"symbol {symbol!r} contains forbidden characters: [{ch!r}]"


class TestErrorMessagesByteExact:
    """Every PartitionError message is pinned with byte-exact ``==``.

    ``pytest.raises(match=...)`` is a regex *search*, so mutmut's
    "XX…XX"-wrapped f-string mutants sail straight through the existing
    ``match=`` assertions; only full-string equality kills them — same
    discipline as test_ranking.py (DEC-MUTATION-GATE-RANKING-002,
    extended here by DEC-MUTATION-RUNTIME-001).
    """

    def test_exchange_message_byte_exact(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError) as exc:
            build_partition_path(
                lake_root=tmp_path,
                exchange="MCX",
                segment="FNO",
                symbol="X",
                interval="1minute",
                year=2024,
                month=1,
            )
        assert str(exc.value) == "exchange must be one of ['BSE', 'NSE'], got 'MCX'"

    def test_segment_message_byte_exact(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError) as exc:
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="COMMODITY",
                symbol="X",
                interval="1minute",
                year=2024,
                month=1,
            )
        assert str(exc.value) == "segment must be one of ['CASH', 'FNO', 'INDEX'], got 'COMMODITY'"

    def test_interval_message_byte_exact(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError) as exc:
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="45minute",
                year=2024,
                month=1,
            )
        assert str(exc.value) == (
            "interval must be one of the 23 canonical intervals, got '45minute'"
        )

    def test_empty_symbol_message_byte_exact(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError) as exc:
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="",
                interval="1minute",
                year=2024,
                month=1,
            )
        assert str(exc.value) == "symbol must be a non-empty string"

    def test_year_message_byte_exact(self, tmp_path: Path) -> None:
        # One pin covers BOTH f-string fragments of the two-line message.
        with pytest.raises(PartitionError) as exc:
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="1minute",
                year=1999,
                month=1,
            )
        assert str(exc.value) == ("year must be a 4-digit Gregorian year in [2000, 2100], got 1999")

    def test_month_message_byte_exact(self, tmp_path: Path) -> None:
        with pytest.raises(PartitionError) as exc:
            build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="X",
                interval="1minute",
                year=2024,
                month=13,
            )
        assert str(exc.value) == "month must be in [1, 12], got 13"

    def test_outside_lake_root_message_byte_exact(self, tmp_path: Path) -> None:
        outside = tmp_path / "other" / "bars" / "NSE" / "FNO" / "X" / "1minute" / "2024"
        outside_file = outside / "01.parquet"
        outside_file.parent.mkdir(parents=True)
        outside_file.touch()
        lake_root = tmp_path / "lake"
        with pytest.raises(PartitionError) as exc:
            parse_partition_path(outside_file, lake_root=lake_root)
        # The path embeds tmp_path, so the expected string is computed —
        # the mutant's XX-wrapped literal fragments still cannot match.
        assert str(exc.value) == f"path {outside_file!r} is not inside lake_root {lake_root!r}"

    def test_too_short_path_message_byte_exact(self) -> None:
        # One pin covers all THREE f-string fragments of the message.
        with pytest.raises(PartitionError) as exc:
            parse_partition_path(Path("bars/NSE/FNO/01.parquet"))
        assert str(exc.value) == (
            "partition path must have at least 7 components "
            "(bars / exchange / segment / symbol / interval / yyyy / mm.parquet), "
            "got 4: ('bars', 'NSE', 'FNO', '01.parquet')"
        )

    def test_missing_bars_dir_message_byte_exact(self) -> None:
        bogus = Path("not_bars/NSE/FNO/NSE-NIFTY/1minute/2024/01.parquet")
        with pytest.raises(PartitionError) as exc:
            parse_partition_path(bogus)
        assert str(exc.value) == "partition must live under bars/, got 'not_bars'"

    def test_non_parquet_message_byte_exact(self) -> None:
        bogus = Path("bars/NSE/FNO/NSE-NIFTY/1minute/2024/01.json")
        with pytest.raises(PartitionError) as exc:
            parse_partition_path(bogus)
        assert str(exc.value) == "partition filename must end with .parquet, got '01.json'"

    def test_non_integer_year_message_byte_exact(self) -> None:
        bogus = Path("bars/NSE/FNO/NSE-NIFTY/1minute/twenty24/01.parquet")
        with pytest.raises(PartitionError) as exc:
            parse_partition_path(bogus)
        assert str(exc.value) == "year segment must be an integer, got 'twenty24'"

    def test_non_integer_month_message_byte_exact(self) -> None:
        bogus = Path("bars/NSE/FNO/NSE-NIFTY/1minute/2024/Jan.parquet")
        with pytest.raises(PartitionError) as exc:
            parse_partition_path(bogus)
        assert str(exc.value) == "month filename must be <mm>.parquet, got 'Jan.parquet'"

    def test_naive_timestamp_message_byte_exact(self) -> None:
        with pytest.raises(PartitionError) as exc:
            partition_for_timestamp(datetime(2024, 1, 1, 0, 0))
        assert str(exc.value) == (
            "partition_for_timestamp requires tz-aware datetime, "
            "got naive datetime.datetime(2024, 1, 1, 0, 0)"
        )


class TestSpotProbeSegmentsPins:
    """``SPOT_PROBE_SEGMENTS`` is the ONE COMMON spot probe order.

    The movers underlying-spot resolver (``movers_futures._SPOT_SEGMENTS``),
    the ``/db`` per-bar moneyness spot join (``db_view._SPOT_PROBE_SEGMENTS``),
    the Greeks spot join (``spot_join``), the drift scanner (``spot_drift``),
    the lake invariant scanner and the completeness rollup ALL import this
    tuple, so its exact members AND their order (an index underlying is
    probed under ``INDEX`` BEFORE the single-stock ``CASH``) are a
    cross-surface contract (CLAUDE.md §11 COMMON; DEC-FNO-MONEYNESS-001).
    test_data_partition.py never referenced it, so mutmut's member /
    tuple-``None`` mutants survived the DEC-MUTATION-RUNTIME-003 rehearsal
    probe — pin the tuple byte-exact to kill all three
    (mutating a member would silently break every consumer's spot probe).
    """

    def test_probe_order_is_index_then_cash(self) -> None:
        assert SPOT_PROBE_SEGMENTS == ("INDEX", "CASH")

    def test_every_probe_segment_routes_through_the_partition_path(self, tmp_path: Path) -> None:
        # Each probe segment must be a VALID partition segment — a member
        # `_validate_segment` rejects could never exist on disk, so every
        # consumer's probe of it would be dead code.
        for segment in SPOT_PROBE_SEGMENTS:
            p = build_partition_path(
                lake_root=tmp_path,
                exchange="NSE",
                segment=segment,
                symbol="NSE-NIFTY",
                interval="1minute",
                year=2024,
                month=1,
            )
            assert segment in p.parts
