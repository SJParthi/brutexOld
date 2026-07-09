"""Unit coverage for :mod:`brutex.data.price_sanity` (DEC-STORED-DATA-VALIDATION-001).

100% line + branch on ``brutex/data/price_sanity.py`` — the ONE frame-level
source of truth for "is this stored OHLC row sane?" (positivity + ordering),
consumed by the prebuild's 1-minute read boundary and the lake invariant
scanner. Every violation class is pinned individually (fails-before: the
round-2 audit live-reproduced a ``high < low`` 1m bar laundering into a
self-consistent derived bucket because NO frame-level check existed).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("polars")

import polars as pl
from brutex.data.errors import ResampleInputError
from brutex.data.price_sanity import (
    PRICE_SANITY_COLUMNS,
    count_price_violations,
    price_row_insane,
    price_violation_expr,
    validate_frame_price_sanity,
)
from hypothesis import given
from hypothesis import strategies as st


class TestPriceRowInsaneScalar:
    """The scalar :func:`price_row_insane` — the twin of :func:`price_violation_expr`."""

    def test_clean_row_is_sane(self) -> None:
        assert price_row_insane(*_CLEAN) is False

    def test_degenerate_flat_row_is_sane(self) -> None:
        assert price_row_insane(50.0, 50.0, 50.0, 50.0) is False

    @pytest.mark.parametrize(
        "row",
        [
            pytest.param((0.0, 101.0, 0.0, 100.5), id="zero-open-low"),
            pytest.param((100.0, 101.0, 99.0, 0.0), id="zero-close"),
            pytest.param((-5.0, -1.0, -10.0, -2.0), id="negative-but-ordered"),
            pytest.param((100.0, 1.0, 50.0, 100.5), id="high-below-low"),
            pytest.param((102.0, 101.0, 99.0, 100.5), id="open-above-high"),
            pytest.param((98.0, 101.0, 99.0, 100.5), id="open-below-low"),
            pytest.param((100.0, 101.0, 99.0, 102.0), id="close-above-high"),
            pytest.param((100.0, 101.0, 99.0, 98.0), id="close-below-low"),
            pytest.param((None, 101.0, 99.0, 100.5), id="null-open"),
            pytest.param((100.0, None, 99.0, 100.5), id="null-high"),
            pytest.param((100.0, 101.0, None, 100.5), id="null-low"),
            pytest.param((100.0, 101.0, 99.0, None), id="null-close"),
        ],
    )
    def test_each_violation_kind_is_insane(
        self, row: tuple[float | None, float | None, float | None, float | None]
    ) -> None:
        assert price_row_insane(*row) is True

    @given(
        rows=st.lists(
            st.tuples(
                st.one_of(st.none(), st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False)),
                st.one_of(st.none(), st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False)),
                st.one_of(st.none(), st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False)),
                st.one_of(st.none(), st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False)),
            ),
            min_size=1,
            max_size=25,
        )
    )
    def test_scalar_equals_vector_row_wise(
        self, rows: list[tuple[float | None, float | None, float | None, float | None]]
    ) -> None:
        """ANTI-DIVERGENCE (scalar==vector): the scalar twin matches the expr row-wise."""
        frame = pl.DataFrame(
            {
                "open": [r[0] for r in rows],
                "high": [r[1] for r in rows],
                "low": [r[2] for r in rows],
                "close": [r[3] for r in rows],
            }
        )
        vector = frame.select(price_violation_expr().alias("bad")).get_column("bad").to_list()
        scalar = [price_row_insane(*r) for r in rows]
        assert scalar == vector


_T0 = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)


def _frame(rows: list[tuple[float | None, float | None, float | None, float | None]]):
    """Build a 1m-shaped frame from (open, high, low, close) tuples."""
    return pl.DataFrame(
        {
            "ts_utc": [_T0 + timedelta(minutes=i) for i in range(len(rows))],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [10] * len(rows),
        }
    )


_CLEAN = (100.0, 101.0, 99.0, 100.5)


class TestCountPriceViolations:
    def test_clean_frame_counts_zero(self) -> None:
        assert count_price_violations(_frame([_CLEAN, (50.0, 50.0, 50.0, 50.0)])) == 0

    def test_empty_frame_counts_zero(self) -> None:
        assert count_price_violations(_frame([])) == 0

    @pytest.mark.parametrize(
        "row",
        [
            pytest.param((0.0, 101.0, 0.0, 100.5), id="zero-open-low"),
            pytest.param((100.0, 101.0, 99.0, 0.0), id="zero-close"),
            pytest.param((-5.0, -1.0, -10.0, -2.0), id="negative-but-ordered (L1-a)"),
            pytest.param((100.0, 1.0, 50.0, 100.5), id="high-below-low (L1-b)"),
            pytest.param((102.0, 101.0, 99.0, 100.5), id="open-above-high"),
            pytest.param((98.0, 101.0, 99.0, 100.5), id="open-below-low"),
            pytest.param((100.0, 101.0, 99.0, 102.0), id="close-above-high"),
            pytest.param((100.0, 101.0, 99.0, 98.0), id="close-below-low"),
            pytest.param((None, 101.0, 99.0, 100.5), id="null-open"),
            pytest.param((100.0, None, 99.0, 100.5), id="null-high"),
            pytest.param((100.0, 101.0, None, 100.5), id="null-low"),
            pytest.param((100.0, 101.0, 99.0, None), id="null-close"),
        ],
    )
    def test_each_violation_kind_counts(
        self, row: tuple[float | None, float | None, float | None, float | None]
    ) -> None:
        assert count_price_violations(_frame([_CLEAN, row])) == 1

    def test_multiple_bad_rows_all_counted(self) -> None:
        frame = _frame([_CLEAN, (-1.0, -1.0, -1.0, -1.0), (100.0, 1.0, 50.0, 60.0)])
        assert count_price_violations(frame) == 2

    def test_expr_never_null(self) -> None:
        """A null price folds to True via the is_null arm — the mask has no nulls."""
        frame = _frame([(None, None, None, None), _CLEAN])
        mask = frame.select(price_violation_expr().alias("bad")).get_column("bad")
        assert mask.null_count() == 0
        assert mask.to_list() == [True, False]

    def test_columns_constant(self) -> None:
        assert PRICE_SANITY_COLUMNS == ("open", "high", "low", "close")


class TestValidateFramePriceSanity:
    def test_clean_frame_passes(self) -> None:
        assert validate_frame_price_sanity(_frame([_CLEAN])) is None

    def test_empty_frame_passes(self) -> None:
        assert validate_frame_price_sanity(_frame([])) is None

    def test_corrupt_frame_raises_naming_context_ts_and_count(self) -> None:
        frame = _frame([_CLEAN, (100.0, 1.0, 50.0, 60.0), (-5.0, -1.0, -10.0, -2.0)])
        with pytest.raises(ResampleInputError) as excinfo:
            validate_frame_price_sanity(frame, context="NSE/INDEX/NIFTY/1minute")
        message = str(excinfo.value)
        assert "NSE/INDEX/NIFTY/1minute" in message
        assert "2 price-insane" in message
        # First offender = the second row's ts_utc (polars extracts a
        # ZoneInfo-tz datetime, so pin the digits, not the stdlib repr).
        assert "2024, 1, 2, 3, 46" in message

    def test_corrupt_frame_without_context_keeps_bare_message(self) -> None:
        with pytest.raises(ResampleInputError) as excinfo:
            validate_frame_price_sanity(_frame([(0.0, 0.0, 0.0, 0.0)]))
        assert " in " not in str(excinfo.value).split("carry")[0]
        assert "1 price-insane" in str(excinfo.value)

    def test_missing_ts_column_degrades_message_not_detection(self) -> None:
        frame = _frame([(100.0, 1.0, 50.0, 60.0)]).drop("ts_utc")
        with pytest.raises(ResampleInputError) as excinfo:
            validate_frame_price_sanity(frame, context="X")
        assert "'<unknown>'" in str(excinfo.value)

    def test_custom_ts_column(self) -> None:
        frame = _frame([(100.0, 1.0, 50.0, 60.0)]).rename({"ts_utc": "timestamp"})
        with pytest.raises(ResampleInputError) as excinfo:
            validate_frame_price_sanity(frame, ts_column="timestamp")
        assert "2024, 1, 2, 3, 45" in str(excinfo.value)
