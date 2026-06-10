"""Unit tests for brutex.data.parser — the Groww candle row → CandleBar."""

# Whitebox: `_IST` is imported directly to pin the timezone constant's
# name + offset byte-exact for the mutation gate (DEC-MUTATION-RUNTIME-001).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.parser import (
    _IST as _PARSER_IST,
)
from brutex.data.parser import (
    CandleRowError,
    parse_candle_row,
    parse_candle_rows,
)

_IST = timezone(timedelta(hours=5, minutes=30))


# ────────────────────────────────────────────────────────────────────────
# Happy path
# ────────────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_fno_row_ist_to_utc(self) -> None:
        row = [
            "2024-01-25T09:15:00",
            22000.0,
            22010.0,
            21990.0,
            22005.0,
            100,
            500,
        ]
        bar = parse_candle_row(row)

        # 09:15 IST = 03:45 UTC
        assert bar.timestamp.tzinfo == UTC
        assert bar.timestamp.hour == 3
        assert bar.timestamp.minute == 45
        assert bar.open == Decimal("22000.0")
        assert bar.volume == 100
        assert bar.open_interest == 500

    def test_equity_row_none_oi(self) -> None:
        row = ["2024-01-25T09:15:00", 2500.0, 2502.0, 2498.0, 2501.0, 10000, None]
        bar = parse_candle_row(row)
        assert bar.open_interest is None

    def test_price_precision_preserved_from_string(self) -> None:
        """Floats are round-tripped via str to preserve shown precision."""
        row = ["2024-01-25T09:15:00", 100.05, 100.10, 100.00, 100.05, 1, None]
        bar = parse_candle_row(row)
        assert bar.open == Decimal("100.05")
        assert bar.close == Decimal("100.05")

    def test_t_separator_and_space_both_parse(self) -> None:
        t_row = ["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0, None]
        space_row = ["2024-01-25 09:15:00", 1.0, 1.0, 1.0, 1.0, 0, None]
        assert parse_candle_row(t_row).timestamp == parse_candle_row(space_row).timestamp

    def test_batch_parses_all(self) -> None:
        rows = [
            ["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0, None],
            ["2024-01-25T09:16:00", 2.0, 2.0, 2.0, 2.0, 0, None],
        ]
        bars = parse_candle_rows(rows)
        assert len(bars) == 2
        assert bars[0].open == Decimal("1.0")
        assert bars[1].open == Decimal("2.0")


# ────────────────────────────────────────────────────────────────────────
# Error paths
# ────────────────────────────────────────────────────────────────────────


class TestErrorPaths:
    def test_rejects_wrong_length(self) -> None:
        with pytest.raises(CandleRowError, match="7 elements"):
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0])

    def test_rejects_non_string_timestamp(self) -> None:
        with pytest.raises(CandleRowError, match="timestamp"):
            parse_candle_row([1234567890, 1.0, 1.0, 1.0, 1.0, 0, None])

    def test_rejects_bad_iso(self) -> None:
        with pytest.raises(CandleRowError, match="ISO"):
            parse_candle_row(["not-a-date", 1.0, 1.0, 1.0, 1.0, 0, None])

    def test_rejects_non_numeric_price(self) -> None:
        with pytest.raises(CandleRowError, match="open"):
            parse_candle_row(["2024-01-25T09:15:00", None, 1.0, 1.0, 1.0, 0, None])

    def test_rejects_non_integer_volume(self) -> None:
        with pytest.raises(CandleRowError, match="volume"):
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, "twelve", None])

    def test_rejects_non_integer_oi(self) -> None:
        with pytest.raises(CandleRowError, match="open_interest"):
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0, "fifty"])

    def test_rejects_high_less_than_low(self) -> None:
        # CandleBar.__post_init__ validates OHLC ordering; parser
        # surfaces that as CandleRowError.
        with pytest.raises(CandleRowError, match="high"):
            parse_candle_row(["2024-01-25T09:15:00", 100.0, 99.0, 100.0, 100.0, 0, None])


# ────────────────────────────────────────────────────────────────────────
# Decimal-coercion paths (closes lines 145, 149 of _to_decimal)
# ────────────────────────────────────────────────────────────────────────


class TestDecimalCoercion:
    """Cover the Decimal-passthrough and string-input paths of
    ``_to_decimal``, plus the tz-aware timestamp branch where the input
    already carries a timezone offset (branch 82→84)."""

    def test_decimal_input_passes_through(self) -> None:
        """Cover line 145: when the raw price is already ``Decimal``,
        ``_to_decimal`` returns it unchanged (no str() round-trip)."""
        from decimal import Decimal

        bar = parse_candle_row(
            [
                "2024-01-25T09:15:00",
                Decimal("100.05"),  # already Decimal
                Decimal("100.05"),
                Decimal("100.05"),
                Decimal("100.05"),
                0,
                None,
            ]
        )
        assert bar.open == Decimal("100.05")
        assert bar.close == Decimal("100.05")

    def test_string_input_parses_to_decimal(self) -> None:
        """Cover line 149: when the raw price is a ``str``, ``_to_decimal``
        constructs Decimal directly from the string."""
        from decimal import Decimal

        bar = parse_candle_row(
            [
                "2024-01-25T09:15:00",
                "100.05",  # string price
                "100.05",
                "100.05",
                "100.05",
                0,
                None,
            ]
        )
        assert bar.open == Decimal("100.05")
        assert bar.close == Decimal("100.05")

    def test_iso_timestamp_with_offset_preserved(self) -> None:
        """Cover branch 82→84: timestamp string with explicit timezone
        offset skips the IST-attach branch and goes straight to
        ``astimezone(UTC)``."""
        # IST is UTC+05:30; offset-aware ISO should parse + convert.
        bar = parse_candle_row(
            ["2024-01-25T09:15:00+05:30", 100.05, 100.05, 100.05, 100.05, 0, None]
        )
        # IST 09:15 == UTC 03:45
        assert bar.timestamp.hour == 3
        assert bar.timestamp.minute == 45


# ────────────────────────────────────────────────────────────────────────
# Mutation-runtime pins (DEC-MUTATION-RUNTIME-001)
# ────────────────────────────────────────────────────────────────────────


class TestMutationPins:
    """Pin every parser error message + constant byte-exact for the
    mutation gate (DEC-MUTATION-RUNTIME-001).

    ``pytest.raises(match=...)`` is a substring/regex search, so mutmut's
    string mutants (``"open"`` → ``"XXopenXX"``, f-string fragments wrapped
    in ``XX..XX``) survive it. Every test below asserts ``str(exc.value)``
    with ``==`` — any single mutated byte in a message, or in the field
    name threaded into ``_to_decimal``, fails the test.
    """

    def test_ist_constant_name_and_offset_pinned(self) -> None:
        # Kills the `name="IST"` string mutant: the tz name is observable
        # ONLY via tzname()/repr (every parsed bar is already astimezone'd
        # to UTC), so it is pinned on the constant directly. The +05:30
        # offset on the same line stays pinned through the public API too
        # (test_fno_row_ist_to_utc: 09:15 IST == 03:45 UTC).
        assert _PARSER_IST.tzname(None) == "IST"
        assert _PARSER_IST.utcoffset(None) == timedelta(hours=5, minutes=30)

    def test_wrong_length_message_byte_exact(self) -> None:
        # Pins BOTH halves of the two-fragment f-string (mutants on the
        # "must have N elements" prefix AND the "[ts, ...]: row" suffix).
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0])
        assert str(exc.value) == (
            "the candle row must have 7 elements [ts, o, h, l, c, v, oi], "
            "got 6: ['2024-01-25T09:15:00', 1.0, 1.0, 1.0, 1.0, 0]"
        )

    def test_non_string_timestamp_message_byte_exact(self) -> None:
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row([1234567890, 1.0, 1.0, 1.0, 1.0, 0, None])
        assert str(exc.value) == "timestamp must be an ISO string, got int"

    def test_bad_iso_message_byte_exact(self) -> None:
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["not-a-date", 1.0, 1.0, 1.0, 1.0, 0, None])
        assert str(exc.value) == "timestamp 'not-a-date' is not valid ISO-8601"

    def test_naive_ist_timestamp_parses_to_exact_utc_datetime(self) -> None:
        # Kills the `naive = datetime.fromisoformat(ts_raw)` → `naive = None`
        # statement mutant (AttributeError under the mutant) and pins the
        # whole IST→UTC conversion as ONE exact aware datetime, not just
        # hour/minute fields.
        bar = parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0, None])
        assert bar.timestamp == datetime(2024, 1, 25, 3, 45, 0, tzinfo=UTC)

    def test_non_numeric_open_message_byte_exact(self) -> None:
        # Pins the "open" field name AND the full `_to_decimal` ValueError
        # text (re-raised verbatim as CandleRowError via str(exc)).
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["2024-01-25T09:15:00", None, 1.0, 1.0, 1.0, 0, None])
        assert str(exc.value) == "open must be numeric, got NoneType: None"

    def test_non_numeric_high_message_byte_exact(self) -> None:
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["2024-01-25T09:15:00", 1.0, None, 1.0, 1.0, 0, None])
        assert str(exc.value) == "high must be numeric, got NoneType: None"

    def test_non_numeric_low_message_byte_exact(self) -> None:
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, None, 1.0, 0, None])
        assert str(exc.value) == "low must be numeric, got NoneType: None"

    def test_non_numeric_close_message_byte_exact(self) -> None:
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, None, 0, None])
        assert str(exc.value) == "close must be numeric, got NoneType: None"

    def test_non_integer_volume_message_byte_exact(self) -> None:
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, "twelve", None])
        assert str(exc.value) == "volume 'twelve' is not an integer"

    def test_non_integer_oi_message_byte_exact(self) -> None:
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["2024-01-25T09:15:00", 1.0, 1.0, 1.0, 1.0, 0, "fifty"])
        assert str(exc.value) == "open_interest 'fifty' is not an integer or None"

    def test_invalid_bar_content_message_byte_exact(self) -> None:
        # CandleBar.__post_init__ rejects high < low; the parser re-wraps it
        # with the "invalid bar content: " prefix. Decimal str() keeps the
        # trailing ".0" (prices arrive as Decimal rupees, DEC-MONEY-2DP-001).
        with pytest.raises(CandleRowError) as exc:
            parse_candle_row(["2024-01-25T09:15:00", 100.0, 99.0, 100.0, 100.0, 0, None])
        assert str(exc.value) == (
            "invalid bar content: CandleBar: high (99.0) must be >= low (100.0)"
        )
