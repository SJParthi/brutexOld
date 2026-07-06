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
    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "0",
            0,
            0.0,
            "0.0",
            Decimal("0"),
            # DEC-ADVERSARIAL-HARDENING-001 FIX 3: a NEGATIVE price is un-storable
            # junk too (a traded Indian price is never <= 0), treated like zero.
            -5,
            -0.05,
            "-100.5",
            Decimal("-1"),
        ],
    )
    def test_no_price(self, value: object) -> None:
        assert has_no_price(value) is True

    @pytest.mark.parametrize("value", ["1", 1, 0.05, "100.5"])
    def test_has_price(self, value: object) -> None:
        assert has_no_price(value) is False

    def test_unparseable_nonzero_is_not_empty(self) -> None:
        # An unparseable non-empty value is NOT "no price" — it's malformed,
        # so the strict to_price() flags it instead.
        assert has_no_price("abc") is False

    @pytest.mark.parametrize(
        "value", [float("-inf"), float("inf"), float("nan"), Decimal("-Infinity")]
    )
    def test_non_finite_is_not_no_price(self, value: object) -> None:
        # DEC-PULL-QUARANTINE-ROBUST-001 FIX 3: a NON-FINITE value (incl. ``-inf``,
        # which Decimal compares ``-Infinity <= 0`` → True) must return False so it
        # falls through to the strict to_price() non-finite guard — never SILENTLY
        # swallowed as a "no-price" partial-bar skip. This keeps ``-inf`` LOUD,
        # symmetric with ``+inf`` / ``nan`` (the asymmetry the fix closes).
        assert has_no_price(value) is False


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

    def test_int64_ceiling_kept(self) -> None:
        # The exact int64 max is storable — the boundary is INCLUSIVE.
        assert to_int(2**63 - 1, "volume") == 2**63 - 1

    @pytest.mark.parametrize("field", ["volume", "open_interest"])
    def test_int64_overflow_rejected(self, field: str) -> None:
        # DEC-PULL-QUARANTINE-ROBUST-001 FIX 2: a value above the lake's Int64
        # storage ceiling (``2**63 - 1``) is rejected at PARSE time with a
        # quarantinable CandleParseError — instead of passing parsing and crashing
        # the Parquet write with a raw TypeError (Int128) that escapes the
        # per-symbol quarantine and kills the whole pull.
        with pytest.raises(CandleParseError, match="exceeds the lake's int64 storage ceiling"):
            to_int(2**70, field)


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

    @pytest.mark.parametrize("zero", [0, 0.0, "0", "0.0", "0.00"])
    @pytest.mark.parametrize("field", ["open_", "high", "low", "close"])
    def test_partial_zero_price_is_skipped(self, field: str, zero: object) -> None:
        # DEC-CANDLE-PARTIAL-ZERO-SKIP-001: a bar that is NOT all-empty but has a REQUIRED
        # OHLC price of ZERO among real prices (a vendor glitch — a traded Indian price is
        # never 0, tick ≥ 0.05) is un-storable junk, the symmetric twin of the partial-ABSENT
        # skip. Storing it would FABRICATE a zero price AND (via the high/low clamp) a zero
        # range — the exact Golden-Rule-#1 corruption the all-empty check already guards.
        with pytest.raises(EmptyCandleRow, match="partial vendor bar"):
            self._call(**{field: zero})

    def test_partial_zero_open_does_not_clobber_low_to_zero(self) -> None:
        # Regression for the proven corruption: pre-fix, ``open=0`` with real high/low/close
        # was STORED as ``open=0, low=0`` (the clamp pulled low to the junk zero). It must now
        # be a graceful skip, never a stored bar with a fabricated zero price + zero range.
        with pytest.raises(EmptyCandleRow):
            self._call(open_=0, high="100", low="50", close="75")

    @pytest.mark.parametrize("neg", [-5, -0.05, "-100.5", Decimal("-1")])
    @pytest.mark.parametrize("field", ["open_", "high", "low", "close"])
    def test_partial_negative_price_is_skipped(self, field: str, neg: object) -> None:
        # DEC-ADVERSARIAL-HARDENING-001 FIX 3: a NEGATIVE OHLC price among real prices is
        # un-storable junk, the symmetric twin of the partial-ZERO / partial-ABSENT skip (a
        # traded Indian price is never <= 0). Pre-fix, ``has_no_price`` rejected absent/zero
        # but NOT negative, so a ``-5`` open was silently STORED (and the high=max/low=min
        # clamp HID it). It must now be a graceful skip, never a stored negative bar.
        with pytest.raises(EmptyCandleRow, match="partial vendor bar"):
            self._call(**{field: neg})

    def test_negative_open_does_not_clobber_low_negative(self) -> None:
        # The clamp-hides-the-junk corruption for the negative case: pre-fix ``open=-5`` with
        # real high/low/close was STORED with ``open=-5`` and ``low`` clamped to ``-5`` (the
        # min pulled the bar negative). Must be a graceful skip, never a stored negative bar.
        with pytest.raises(EmptyCandleRow):
            self._call(open_="-5", high="100", low="50", close="75")

    def test_all_negative_prices_is_empty_candle_row(self) -> None:
        # Every OHLC price negative → the all-empty guard treats them as no-price → skip.
        with pytest.raises(EmptyCandleRow, match="no OHLC data"):
            self._call(open_="-1", high="-1", low="-1", close="-1")

    @pytest.mark.parametrize("field", ["open_", "high", "low", "close"])
    def test_partial_negative_infinity_is_loud_not_skipped(self, field: str) -> None:
        # DEC-PULL-QUARANTINE-ROBUST-001 FIX 3: a present ``-inf`` price among real prices is
        # NON-FINITE junk — it must raise the LOUD non-finite CandleParseError (a genuine
        # malformation worth surfacing), NOT be silently swallowed as a "partial vendor bar"
        # EmptyCandleRow skip the way it was pre-fix (``has_no_price(-inf)`` returned True).
        # Symmetric with +inf / nan, which already hard-errored.
        with pytest.raises(CandleParseError, match="non-finite"):
            self._call(**{field: float("-inf")})

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


class TestOnClampObserveHook:
    """The observe-only OHLC-clamp hook (DEC-STORED-DATA-VALIDATION-001).

    Fails-before: the DEC-OHLC-VENDOR-NORMALIZE-001 clamp at the high=max /
    low=min site fired SILENTLY — no counter, no log — so a vendor
    systematically under-reporting highs could never surface.
    """

    def _call(self, *, high: object, low: object, on_clamp: object) -> CandleBar:
        return assemble_candle(
            ts_raw="ignored",
            parse_timestamp=_fixed_ts,
            open_="100",
            high=high,
            low=low,
            close="105",
            volume="10",
            open_interest=None,
            on_clamp=on_clamp,  # type: ignore[arg-type]
        )

    def test_hook_fires_once_when_high_clamped(self) -> None:
        calls: list[int] = []
        bar = self._call(high="101", low="90", on_clamp=lambda: calls.append(1))
        assert calls == [1]  # close 105 > vendor high 101 → clamp moved high
        assert bar.high == Decimal("105")

    def test_hook_fires_when_low_clamped(self) -> None:
        calls: list[int] = []
        bar = self._call(high="110", low="102", on_clamp=lambda: calls.append(1))
        assert calls == [1]  # open 100 < vendor low 102 → clamp moved low
        assert bar.low == Decimal("100")

    def test_hook_silent_on_consistent_bar(self) -> None:
        calls: list[int] = []
        bar = self._call(high="110", low="90", on_clamp=lambda: calls.append(1))
        assert calls == []
        assert (bar.high, bar.low) == (Decimal("110"), Decimal("90"))

    def test_default_none_hook_returns_identical_bar(self) -> None:
        clamped_with_hook = self._call(high="101", low="90", on_clamp=lambda: None)
        clamped_without = assemble_candle(
            ts_raw="ignored",
            parse_timestamp=_fixed_ts,
            open_="100",
            high="101",
            low="90",
            close="105",
            volume="10",
            open_interest=None,
        )
        assert clamped_with_hook == clamped_without  # parse behavior unchanged
