"""Tests for `brutex.data.candle_fields` — the shared candle-field rules.

Targets 100% line+branch of the vendor-neutral helpers that the Groww and
Dhan parsers both delegate to (CLAUDE.md §11 COMMON).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
    has_no_price,
    is_absent,
    to_int,
    to_price,
)
from brutex.data.models import CandleBar

_TS = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)


def _fixed_ts(_raw: object) -> datetime:
    return _TS


class TestIsAbsent:
    @pytest.mark.parametrize("value", [None, "", "   ", "\t"])
    def test_absent_values(self, value: object) -> None:
        assert is_absent(value) is True

    @pytest.mark.parametrize("value", ["1", "0", 0, 1.5, "x"])
    def test_present_values(self, value: object) -> None:
        assert is_absent(value) is False


class TestHasNoPrice:
    @pytest.mark.parametrize("value", [None, "", "0", 0, 0.0, "0.0", Decimal("0")])
    def test_no_price(self, value: object) -> None:
        assert has_no_price(value) is True

    @pytest.mark.parametrize("value", ["1", 1, 0.05, "100.5"])
    def test_has_price(self, value: object) -> None:
        assert has_no_price(value) is False

    def test_unparseable_nonzero_is_not_empty(self) -> None:
        # An unparseable non-empty value is NOT "no price" — it's malformed,
        # so the strict to_price() flags it instead.
        assert has_no_price("abc") is False


class TestToPrice:
    def test_valid(self) -> None:
        assert to_price("100.25", "open") == Decimal("100.25")

    def test_unparseable_raises(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable open price"):
            to_price("abc", "open")

    @pytest.mark.parametrize("value", [float("inf"), float("nan"), float("-inf")])
    def test_non_finite_raises(self, value: float) -> None:
        with pytest.raises(CandleParseError, match="non-finite high price"):
            to_price(value, "high")


class TestToInt:
    @pytest.mark.parametrize(("value", "expected"), [(5, 5), ("7", 7), (3.0, 3)])
    def test_valid(self, value: object, expected: int) -> None:
        assert to_int(value, "volume") == expected

    def test_bool_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="must be an integer, got bool"):
            to_int(True, "volume")

    def test_non_numeric_type_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable volume"):
            to_int([1], "volume")

    def test_unparseable_string_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable volume"):
            to_int("xyz", "volume")

    def test_overflow_rejected(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable open_interest"):
            to_int(float("inf"), "open_interest")


class TestAssembleCandle:
    def _call(
        self,
        *,
        open_: object = "100",
        high: object = "110",
        low: object = "90",
        close: object = "105",
        volume: object = "1000",
        open_interest: object = "50",
    ) -> CandleBar:
        return assemble_candle(
            ts_raw="ignored",
            parse_timestamp=_fixed_ts,
            open_=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            open_interest=open_interest,
        )

    def test_happy_path(self) -> None:
        bar = self._call()
        assert bar.timestamp == _TS
        assert bar.open == Decimal("100")
        assert bar.close == Decimal("105")
        assert bar.volume == 1000
        assert bar.open_interest == 50

    def test_empty_bar_raises(self) -> None:
        with pytest.raises(EmptyCandleRow):
            self._call(open_=0, high=0, low=0, close=0)

    def test_absent_volume_maps_to_zero(self) -> None:
        bar = self._call(volume=None)
        assert bar.volume == 0

    def test_absent_open_interest_is_none(self) -> None:
        bar = self._call(open_interest="")
        assert bar.open_interest is None

    def test_malformed_price_propagates(self) -> None:
        with pytest.raises(CandleParseError, match="unparseable open price"):
            self._call(open_="abc")

    @pytest.mark.parametrize("absent", [None, "", "   "])
    @pytest.mark.parametrize("field", ["open_", "high", "low", "close"])
    def test_partial_absent_price_is_skipped(self, field: str, absent: object) -> None:
        # DEC-CANDLE-PARTIAL-ABSENT-SKIP-001: a bar that is NOT all-empty but
        # has a REQUIRED OHLC price ABSENT (vendor null/empty — e.g. Groww's `open: None`)
        # is an un-storable PARTIAL bar → a graceful EmptyCandleRow skip, never the crash
        # `unparseable open price: None` that aborted the whole 779-symbol pull.
        with pytest.raises(EmptyCandleRow, match="partial vendor bar"):
            self._call(**{field: absent})

    def test_partial_absent_skip_is_an_empty_candle_row(self) -> None:
        # EmptyCandleRow IS-A CandleParseError, so callers catching either still work; the
        # Groww parser's `except EmptyCandleRow: continue` is what makes it a graceful skip.
        with pytest.raises(CandleParseError):
            self._call(open_=None)

    def test_present_but_unparseable_still_hard_errors(self) -> None:
        # The partial-absent skip must NOT mask a genuine malformation: a price that is
        # PRESENT but unparseable (a real shape / column problem) stays a hard error even
        # when ANOTHER field is absent — so the strict guard still surfaces it.
        with pytest.raises(CandleParseError, match="unparseable high price"):
            self._call(open_="100", high="abc", low="90", close="95")

    def test_normalizes_open_above_vendor_high(self) -> None:
        """Vendor quirk: open above the reported high → high clamped to open.

        DEC-OHLC-VENDOR-NORMALIZE-001 — the live Dhan failure
        (open 17289.0 > reported high 17287.0996) must NOT crash the pull.
        """
        bar = self._call(open_="17289.0", high="17287.0996", low="17264.8496", close="17270.0")
        assert bar.open == Decimal("17289.0")
        assert bar.high == Decimal("17289.0")  # clamped up to bound the traded open
        assert bar.low == Decimal("17264.8496")  # already the min — unchanged

    def test_normalizes_close_below_vendor_low(self) -> None:
        """A close below the reported low → low clamped down to bound the close."""
        bar = self._call(open_="100", high="110", low="95", close="90")
        assert bar.low == Decimal("90")  # clamped down to the traded close
        assert bar.high == Decimal("110")

    def test_consistent_bar_passes_through_unchanged(self) -> None:
        """A well-formed bar's reported high/low are left untouched."""
        bar = self._call(open_="100", high="110", low="90", close="105")
        assert bar.high == Decimal("110")
        assert bar.low == Decimal("90")

    def test_negative_volume_clamps_to_zero(self) -> None:
        """Vendor quirk: a negative volume (Dhan's index ``-2``) clamps to 0.

        DEC-VENDOR-VOLUME-NORMALIZE-001 — the live failure
        (``volume must be non-negative, got -2``) must NOT crash the pull.
        """
        bar = self._call(volume="-2")
        assert bar.volume == 0

    def test_negative_open_interest_clamps_to_zero(self) -> None:
        """A negative open-interest clamps to 0 (same DEC-VENDOR-VOLUME-NORMALIZE-001)."""
        bar = self._call(open_interest="-5")
        assert bar.open_interest == 0
