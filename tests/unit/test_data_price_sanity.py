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

#: One price cell for the scalar==vector parity pin: None, a bounded finite
#: float, OR an explicit non-finite special (hypothesis refuses
#: ``allow_nan=True`` beside min/max bounds, so the specials are sampled
#: explicitly — DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001 widened the space).
_PRICE_CELL = st.one_of(
    st.none(),
    st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
    st.sampled_from([float("nan"), float("inf"), float("-inf")]),
)


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

    @pytest.mark.parametrize("field", [0, 1, 2, 3], ids=["open", "high", "low", "close"])
    def test_nan_price_is_insane_in_each_field(self, field: int) -> None:
        """The scalar NaN arm (round-2 refuter): a NaN price in ANY field is insane.

        Historically a DELIBERATE scalar-vs-vector asymmetry (the vectorized
        ``price_violation_expr`` was NaN-blind in HIGH); since
        DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001 the vectorized twin carries the
        mirrored per-column ``~is_finite`` arm, so this pin now guards a
        BOTH-planes invariant (see ``test_scalar_equals_vector_row_wise``).
        """
        row: list[float] = [100.0, 101.0, 99.0, 100.5]
        row[field] = float("nan")
        assert price_row_insane(*row) is True

    @pytest.mark.parametrize("field", [0, 1, 2, 3], ids=["open", "high", "low", "close"])
    @pytest.mark.parametrize("value", [float("inf"), float("-inf")], ids=["+inf", "-inf"])
    def test_infinite_price_is_insane_in_each_field(self, field: int, value: float) -> None:
        """DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001 (fails-before for ``+inf``
        HIGH): a ``+inf`` HIGH passed EVERY scalar check — ``inf`` compares
        fine, so ordering + positivity held vacuously and the row read SANE.
        The widened ``isnan`` → ``not isfinite`` arm flags any ``±inf``."""
        row: list[float] = [100.0, 101.0, 99.0, 100.5]
        row[field] = value
        assert price_row_insane(*row) is True

    @pytest.mark.parametrize("field", [0, 1, 2, 3], ids=["open", "high", "low", "close"])
    def test_str_price_is_insane_in_each_field_never_raises(self, field: int) -> None:
        """Non-numeric guard (round-4 fold): a str price cell (DuckDB
        union_by_name VARCHAR promotion — lake writers never produce it) is
        INSANE, never a TypeError — pre-fix ``math.isnan(str)`` raised through
        screen_rows into a /db page 500."""
        row: list[object] = [100.0, 101.0, 99.0, 100.5]
        row[field] = "101.5"
        assert price_row_insane(*row) is True  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", [0, 1, 2, 3], ids=["open", "high", "low", "close"])
    def test_bool_price_is_insane_in_each_field(self, field: int) -> None:
        """A bool "price" is junk (round-4 fold): ``isinstance(True, int)`` is
        True, so without the explicit bool exclusion ``True`` would coerce to a
        1.0 price — deliberately classified insane instead (documented in the
        docstring)."""
        row: list[object] = [100.0, 101.0, 99.0, 100.5]
        row[field] = True
        assert price_row_insane(*row) is True  # type: ignore[arg-type]

    # CONSCIOUSLY WIDENED input space (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001):
    # NaN/±inf were historically excluded (allow_nan=False/allow_infinity=False)
    # because the scalar was STRICTER on NaN by design; both planes now carry
    # the non-finite arm, so scalar == vector holds over the FULL float plane
    # and the parity pin covers it.
    @given(
        rows=st.lists(
            st.tuples(_PRICE_CELL, _PRICE_CELL, _PRICE_CELL, _PRICE_CELL),
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

    @pytest.mark.parametrize("field", [0, 1, 2, 3], ids=["open", "high", "low", "close"])
    @pytest.mark.parametrize(
        "value", [float("inf"), float("-inf"), float("nan")], ids=["+inf", "-inf", "nan"]
    )
    def test_nonfinite_price_counts_in_each_field(self, field: int, value: float) -> None:
        """DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001 — FAILS-BEFORE for
        ``high ∈ {+inf, NaN}``: the pre-fix vectorized mask counted 0 there
        (``high >= low`` holds vacuously for ``+inf``; polars' total float
        ordering places NaN above everything, so no ordering/positivity arm
        ever tripped) — the assurance session executed-repro'd the miss
        laundering a derived 5-minute bucket ``high = inf``. Every other
        column/value pair already tripped an ordering or ``<= 0`` arm; this
        pins ALL twelve pairs so the guardrail can never narrow."""
        row: list[float] = [100.0, 101.0, 99.0, 100.5]
        row[field] = value
        assert count_price_violations(_frame([_CLEAN, (row[0], row[1], row[2], row[3])])) == 1

    def test_nonfinite_mask_is_never_null(self) -> None:
        """The non-finite arm keeps the no-null mask law intact."""
        frame = _frame([(100.0, float("nan"), 99.0, 100.5), _CLEAN])
        mask = frame.select(price_violation_expr().alias("bad")).get_column("bad")
        assert mask.null_count() == 0
        assert mask.to_list() == [True, False]

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

    @pytest.mark.parametrize("value", [float("inf"), float("nan")], ids=["+inf", "nan"])
    def test_nonfinite_high_frame_raises(self, value: float) -> None:
        """FAILS-BEFORE (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001): a frame whose
        only defect is ``high ∈ {+inf, NaN}`` returned ``None`` (a MISS) —
        the prebuild read boundary then served it to the resamplers."""
        frame = _frame([_CLEAN, (100.0, value, 99.0, 100.5)])
        with pytest.raises(ResampleInputError, match="1 price-insane"):
            validate_frame_price_sanity(frame, context="NSE/INDEX/NIFTY/1minute")

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


# ────────────────────────────────────────────────────────────────────────
# validate_frame_ts_datetime_range — the read-boundary TIMESTAMP-range gate
# (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001)
# ────────────────────────────────────────────────────────────────────────

from brutex.data.price_sanity import (  # noqa: E402 — grouped with its test class
    validate_frame_ts_datetime_range,
)

#: ~year 148108 as microseconds since epoch — within i64, far past
#: ``datetime.max``; the executed repro's poison value.
_POISON_US = 2**62

_EPOCH = datetime(1970, 1, 1)


def _offset_us(instant: datetime) -> int:
    """Independent exact epoch-offset oracle (integer timedelta arithmetic)."""
    delta = instant - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _ts_frame(physical: list[int | None], *, unit: str = "us", tz: str | None = "UTC"):
    """A minimal frame whose ``ts_utc`` holds the given raw physical values."""
    return pl.DataFrame({"ts_utc": physical}).with_columns(
        pl.col("ts_utc").cast(pl.Datetime(time_unit=unit, time_zone=tz))  # type: ignore[arg-type]
    )


class TestValidateFrameTsDatetimeRange:
    def test_clean_frame_passes(self) -> None:
        frame = _ts_frame([_offset_us(datetime(2026, 7, 10, 3, 45))])
        assert validate_frame_ts_datetime_range(frame) is None

    def test_out_of_range_positive_raises_naming_context_and_value(self) -> None:
        """FAILS-BEFORE: no gate existed — the 2**62-us stamp panicked the
        Rust→Python conversion downstream (pyo3 PanicException, a
        BaseException) past every ``except Exception`` per-symbol quarantine."""
        frame = _ts_frame([_offset_us(datetime(2026, 7, 10)), _POISON_US])
        with pytest.raises(ResampleInputError) as excinfo:
            validate_frame_ts_datetime_range(frame, context="NSE/INDEX/NIFTY/1minute")
        message = str(excinfo.value)
        assert "NSE/INDEX/NIFTY/1minute" in message  # names the symbol
        assert "1 timestamp(s)" in message  # counts the offenders
        assert str(_POISON_US) in message  # names the offending physical value
        assert "DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001" in message

    def test_out_of_range_negative_raises(self) -> None:
        below_min = _offset_us(datetime.min) - 1
        frame = _ts_frame([below_min, _offset_us(datetime(2026, 7, 10))])
        with pytest.raises(ResampleInputError, match="outside the\\s+representable"):
            validate_frame_ts_datetime_range(frame)

    def test_boundary_max_and_min_datetimes_pass(self) -> None:
        """Boundary-EXACT: stored ``datetime.max`` / ``datetime.min`` convert
        cleanly, so the gate passes them (and one-past each side raises)."""
        frame = _ts_frame([_offset_us(datetime.min), _offset_us(datetime.max)])
        assert validate_frame_ts_datetime_range(frame) is None
        with pytest.raises(ResampleInputError):
            validate_frame_ts_datetime_range(_ts_frame([_offset_us(datetime.max) + 1]))
        with pytest.raises(ResampleInputError):
            validate_frame_ts_datetime_range(_ts_frame([_offset_us(datetime.min) - 1]))

    def test_gate_probe_never_panics(self) -> None:
        """The probe itself works on raw i64 physicals — the poisoned frame
        raises the TYPED error, never a PanicException (the very class the
        gate exists to stop)."""
        frame = _ts_frame([_POISON_US])
        with pytest.raises(ResampleInputError):
            validate_frame_ts_datetime_range(frame)

    def test_ms_unit_bounds_are_exact(self) -> None:
        max_ms = _offset_us(datetime.max) // 1_000
        assert validate_frame_ts_datetime_range(_ts_frame([max_ms], unit="ms")) is None
        with pytest.raises(ResampleInputError, match="'ms'"):
            validate_frame_ts_datetime_range(_ts_frame([max_ms + 1], unit="ms"))

    def test_ns_unit_is_vacuously_representable(self) -> None:
        """Every storable i64 ns value is inside the Python datetime range —
        the check is vacuously true there (documented)."""
        frame = _ts_frame([2**62, -(2**62)], unit="ns")
        assert validate_frame_ts_datetime_range(frame) is None

    def test_naive_datetime_column_is_gated_too(self) -> None:
        with pytest.raises(ResampleInputError):
            validate_frame_ts_datetime_range(_ts_frame([_POISON_US], tz=None))

    def test_empty_frame_returns(self) -> None:
        assert validate_frame_ts_datetime_range(_ts_frame([])) is None

    def test_absent_ts_column_returns(self) -> None:
        frame = pl.DataFrame({"other": [1]})
        assert validate_frame_ts_datetime_range(frame) is None

    def test_non_datetime_dtype_returns(self) -> None:
        """An out-of-band non-Datetime ts column (no lake writer produces one)
        is left to downstream typed handling — never a raise here."""
        frame = pl.DataFrame({"ts_utc": [2**62]})
        assert validate_frame_ts_datetime_range(frame) is None

    def test_all_null_ts_returns(self) -> None:
        """All-null timestamps are the resampler null guard's class."""
        assert validate_frame_ts_datetime_range(_ts_frame([None, None])) is None

    def test_bounds_constants_match_independent_oracle(self) -> None:
        from brutex.data.price_sanity import (
            _TS_UNIT_BOUNDS,  # pyright: ignore[reportPrivateUsage]
        )

        low_us, high_us = _TS_UNIT_BOUNDS["us"]
        assert low_us == _offset_us(datetime.min)
        assert high_us == _offset_us(datetime.max)
        assert _TS_UNIT_BOUNDS["ms"] == (low_us // 1_000, high_us // 1_000)
        assert _TS_UNIT_BOUNDS["ns"] == (low_us * 1_000, high_us * 1_000)
        # datetime.min is a whole day, so its floor-to-ms is EXACT.
        assert low_us % 1_000 == 0

    def test_mixed_offenders_counted(self) -> None:
        frame = _ts_frame(
            [
                _offset_us(datetime.min) - 5,
                _offset_us(datetime(2026, 7, 10)),
                _POISON_US,
            ]
        )
        with pytest.raises(ResampleInputError, match="2 timestamp"):
            validate_frame_ts_datetime_range(frame)
