"""Unit tests for ``brutex.adapters.dhan.parser``.

Pins:

* Empty arrays return empty tuple (OK_EMPTY semantics).
* Decimal precision via repr-then-quantise (no binary-float garbage).
* Length mismatch → ``DhanErrorDH908``.
* Missing key → ``DhanErrorDH908``.
* Non-list value → ``DhanErrorDH908``.
* Sort by timestamp ascending.
* Dedup by timestamp (first occurrence wins).
* Timestamp is converted from Dhan IST-anchored epoch to UTC datetime.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan.parser import OHLCBar, parse_response
from brutex.adapters.dhan.timeconv import UTC
from brutex.orchestrator.dhan_errors import DhanErrorDH908


def _make_payload(
    open_: list[float],
    high: list[float],
    low: list[float],
    close: list[float],
    volume: list[int],
    timestamp: list[int],
) -> dict[str, list[float] | list[int]]:
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "timestamp": timestamp,
    }


class TestEmptyResponse:
    """Empty arrays = OK_EMPTY (no bars in window)."""

    def test_all_empty_returns_empty_tuple(self) -> None:
        result = parse_response(_make_payload([], [], [], [], [], []))
        assert result == ()

    def test_empty_result_is_a_tuple(self) -> None:
        result = parse_response(_make_payload([], [], [], [], [], []))
        assert isinstance(result, tuple)


class TestSingleBar:
    """One-bar parse — pins every field."""

    @pytest.fixture
    def one_bar(self) -> tuple[OHLCBar, ...]:
        return parse_response(
            _make_payload(
                open_=[24500.05],
                high=[24550.10],
                low=[24490.00],
                close=[24530.25],
                volume=[0],
                timestamp=[1709284500],
            )
        )

    def test_returns_tuple_of_length_one(self, one_bar: tuple[OHLCBar, ...]) -> None:
        assert len(one_bar) == 1

    def test_timestamp_is_utc(self, one_bar: tuple[OHLCBar, ...]) -> None:
        bar = one_bar[0]
        assert bar.timestamp_utc == datetime(2024, 3, 1, 3, 45, 0, tzinfo=UTC)

    def test_open_is_decimal(self, one_bar: tuple[OHLCBar, ...]) -> None:
        assert one_bar[0].open == Decimal("24500.05")
        assert isinstance(one_bar[0].open, Decimal)

    def test_high_quantised(self, one_bar: tuple[OHLCBar, ...]) -> None:
        assert one_bar[0].high == Decimal("24550.10")

    def test_low_preserves_zero_paise(self, one_bar: tuple[OHLCBar, ...]) -> None:
        """24490.00 must round-trip as Decimal('24490.00'), NOT '24490'."""
        assert str(one_bar[0].low) == "24490.00"

    def test_close_quantised(self, one_bar: tuple[OHLCBar, ...]) -> None:
        assert one_bar[0].close == Decimal("24530.25")

    def test_volume_zero(self, one_bar: tuple[OHLCBar, ...]) -> None:
        assert one_bar[0].volume == 0


class TestDecimalPrecision:
    """``Decimal(repr(f))`` preserves intended decimals; ``Decimal(f)`` does not."""

    def test_no_binary_float_garbage(self) -> None:
        """24500.05 must not become 24500.04999...."""
        result = parse_response(
            _make_payload([24500.05], [24500.05], [24500.05], [24500.05], [0], [1709284500])
        )
        assert result[0].open == Decimal("24500.05")
        assert result[0].open != Decimal("24500.04")


class TestLengthMismatch:
    """All 6 arrays must have equal lengths or DH-908 is synthesized.

    The detailed_message goes to ``exc.args[0]`` per BruteXError
    convention (PR #50d safe_message discipline); ``str(exc)`` returns
    the categorical safe message instead.
    """

    def test_open_shorter_raises_dh908(self) -> None:
        with pytest.raises(DhanErrorDH908) as exc_info:
            parse_response(
                _make_payload(
                    open_=[1.0],
                    high=[1.0, 2.0],
                    low=[1.0, 2.0],
                    close=[1.0, 2.0],
                    volume=[0, 0],
                    timestamp=[100, 200],
                )
            )
        assert "length mismatch" in exc_info.value.args[0]

    def test_timestamp_shorter_raises_dh908(self) -> None:
        with pytest.raises(DhanErrorDH908) as exc_info:
            parse_response(
                _make_payload(
                    open_=[1.0, 2.0],
                    high=[1.0, 2.0],
                    low=[1.0, 2.0],
                    close=[1.0, 2.0],
                    volume=[0, 0],
                    timestamp=[100],
                )
            )
        assert "length mismatch" in exc_info.value.args[0]

    def test_safe_message_does_not_leak_lengths(self) -> None:
        """``str(exc)`` returns the categorical safe message, not the lengths dict."""
        with pytest.raises(DhanErrorDH908) as exc_info:
            parse_response(
                _make_payload(
                    open_=[1.0],
                    high=[1.0, 2.0],
                    low=[1.0, 2.0],
                    close=[1.0, 2.0],
                    volume=[0, 0],
                    timestamp=[100, 200],
                )
            )
        assert "lengths" not in str(exc_info.value)


class TestMissingOrMalformedKeys:
    """Schema-shape failures synthesize DH-908."""

    def test_missing_open_raises_dh908(self) -> None:
        payload = _make_payload([], [], [], [], [], [])
        del payload["open"]
        with pytest.raises(DhanErrorDH908) as exc_info:
            parse_response(payload)
        assert "missing required key" in exc_info.value.args[0]

    def test_non_list_value_raises_dh908(self) -> None:
        payload: dict[str, object] = _make_payload([], [], [], [], [], [])  # type: ignore[assignment]
        payload["open"] = "not_a_list"
        with pytest.raises(DhanErrorDH908) as exc_info:
            parse_response(payload)
        assert "not a list" in exc_info.value.args[0]


class TestSortAscending:
    """Bars come back sorted by timestamp regardless of input order."""

    def test_descending_input_sorts_ascending(self) -> None:
        result = parse_response(
            _make_payload(
                open_=[3.0, 2.0, 1.0],
                high=[3.0, 2.0, 1.0],
                low=[3.0, 2.0, 1.0],
                close=[3.0, 2.0, 1.0],
                volume=[0, 0, 0],
                timestamp=[300, 200, 100],
            )
        )
        timestamps = [bar.timestamp_utc for bar in result]
        assert timestamps == sorted(timestamps)


class TestDedup:
    """Duplicates on timestamp are removed; first occurrence wins."""

    def test_duplicate_timestamp_collapsed(self) -> None:
        result = parse_response(
            _make_payload(
                open_=[1.0, 1.5, 2.0],
                high=[1.0, 1.5, 2.0],
                low=[1.0, 1.5, 2.0],
                close=[1.0, 1.5, 2.0],
                volume=[0, 0, 0],
                timestamp=[100, 100, 200],
            )
        )
        assert len(result) == 2
        # First-occurrence-wins: 1.0 not 1.5
        assert result[0].open == Decimal("1.00")
        assert result[1].open == Decimal("2.00")
