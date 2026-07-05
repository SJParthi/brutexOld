"""Unit tests for brutex.data.models — CandleBar + OptionBar + CatalogEntry."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.data.models import (
    GREEKS_OVERLAY_FIELDS,
    GREEKS_PROVENANCE_ID_BOUND,
    CandleBar,
    CatalogEntry,
    FnoGreeksBar,
    OptionBar,
)

# ────────────────────────────────────────────────────────────────────────
# FnoGreeksBar (DEC-GREEKS-ENGINE-PR2B-001 — the Greeks-overlay bar)
# ────────────────────────────────────────────────────────────────────────


def _greeks_bar(**overrides: object) -> FnoGreeksBar:
    """A fully-populated enriched bar; keyword overrides poke one field."""
    fields: dict[str, object] = {
        "timestamp": _ts(),
        "open": Decimal("200.00"),
        "high": Decimal("205.00"),
        "low": Decimal("195.00"),
        "close": Decimal("200.00"),
        "volume": 10,
        "open_interest": 100,
        "iv": 0.125,
        "delta": 0.53,
        "gamma": 0.00087,
        "theta": -8.68,
        "vega": 14.31,
        "rho": 3.2,
        "spot_at_bar": Decimal("21500.00"),
        "t_years_used": 0.0281,
        "rate_used": 0.065,
        "greeks_provenance_id": 1_050_000,
    }
    fields.update(overrides)
    return FnoGreeksBar(**fields)  # type: ignore[arg-type]


class TestFnoGreeksBarHappyPath:
    def test_full_overlay_constructs_and_is_a_candlebar(self) -> None:
        bar = _greeks_bar()
        assert isinstance(bar, CandleBar)
        assert bar.iv == 0.125
        assert bar.greeks_provenance_id == 1_050_000

    def test_defaults_are_all_none(self) -> None:
        bar = FnoGreeksBar(
            timestamp=_ts(),
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1.5"),
            volume=1,
            open_interest=None,
        )
        assert all(getattr(bar, name) is None for name in GREEKS_OVERLAY_FIELDS)


class TestFnoGreeksBarValidation:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_greek_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="finite"):
            _greeks_bar(iv=bad)

    @pytest.mark.parametrize(
        "field", ["iv", "delta", "gamma", "theta", "vega", "rho", "t_years_used", "rate_used"]
    )
    def test_every_float_field_checks_finiteness(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            _greeks_bar(**{field: float("nan")})

    @pytest.mark.parametrize("bad_spot", [Decimal("0"), Decimal("-1")])
    def test_non_positive_spot_rejected(self, bad_spot: Decimal) -> None:
        with pytest.raises(ValueError, match="spot_at_bar"):
            _greeks_bar(spot_at_bar=bad_spot)

    @pytest.mark.parametrize("pid", [-1, GREEKS_PROVENANCE_ID_BOUND])
    def test_out_of_range_provenance_rejected(self, pid: int) -> None:
        with pytest.raises(ValueError, match="greeks_provenance_id"):
            _greeks_bar(greeks_provenance_id=pid)

    def test_price_invariants_still_enforced(self) -> None:
        with pytest.raises(ValueError, match="high"):
            _greeks_bar(high=Decimal("100"))  # high < low → the shared OHLC guard


# ────────────────────────────────────────────────────────────────────────
# CandleBar
# ────────────────────────────────────────────────────────────────────────


def _ts(minute: int = 15) -> datetime:
    return datetime(2024, 1, 25, 9, minute, 0, tzinfo=UTC)


class TestCandleBarHappyPath:
    def test_minimum_valid_fno_bar(self) -> None:
        bar = CandleBar(
            timestamp=_ts(),
            open=Decimal("22000.00"),
            high=Decimal("22010.00"),
            low=Decimal("21990.00"),
            close=Decimal("22005.00"),
            volume=12500,
            open_interest=500,
        )
        assert bar.open_interest == 500
        assert bar.volume == 12500

    def test_equity_bar_with_none_oi(self) -> None:
        bar = CandleBar(
            timestamp=_ts(),
            open=Decimal("2500.00"),
            high=Decimal("2502.00"),
            low=Decimal("2498.00"),
            close=Decimal("2501.00"),
            volume=100_000,
            open_interest=None,
        )
        assert bar.open_interest is None

    def test_locked_bar_high_equal_low(self) -> None:
        # Limit-locked: all four prices identical.
        price = Decimal("100.0")
        bar = CandleBar(
            timestamp=_ts(),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0,
            open_interest=0,
        )
        assert bar.open == bar.high == bar.low == bar.close


class TestCandleBarValidation:
    def test_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ValueError, match="tz-aware|timezone-aware"):
            CandleBar(
                timestamp=datetime(2024, 1, 25, 9, 15),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=0,
                open_interest=None,
            )

    def test_rejects_negative_volume(self) -> None:
        with pytest.raises(ValueError, match="volume"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=-1,
                open_interest=None,
            )

    def test_rejects_negative_open_interest(self) -> None:
        with pytest.raises(ValueError, match="open_interest"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=0,
                open_interest=-5,
            )

    def test_rejects_high_less_than_low(self) -> None:
        with pytest.raises(ValueError, match="high.*low"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("100"),
                high=Decimal("99"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=0,
                open_interest=None,
            )

    def test_rejects_open_outside_range(self) -> None:
        with pytest.raises(ValueError, match="open"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("150"),  # above high
                high=Decimal("100"),
                low=Decimal("90"),
                close=Decimal("95"),
                volume=0,
                open_interest=None,
            )

    def test_rejects_close_outside_range(self) -> None:
        with pytest.raises(ValueError, match="close"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("95"),
                high=Decimal("100"),
                low=Decimal("90"),
                close=Decimal("80"),  # below low
                volume=0,
                open_interest=None,
            )

    def test_bar_is_frozen(self) -> None:
        bar = CandleBar(
            timestamp=_ts(),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=0,
            open_interest=None,
        )
        with pytest.raises((AttributeError, TypeError)):
            bar.volume = 42  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# OptionBar
# ────────────────────────────────────────────────────────────────────────


def _option(**overrides: object) -> OptionBar:
    defaults: dict[str, object] = {
        "timestamp": _ts(),
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("90"),
        "close": Decimal("105"),
        "volume": 250,
        "open_interest": 1200,
        "side": "CE",
        "strike": Decimal("17500"),
        "spot": Decimal("17480.5"),
        "iv": Decimal("0.15"),
    }
    defaults.update(overrides)
    return OptionBar(**defaults)  # type: ignore[arg-type]


class TestOptionBarHappyPath:
    def test_full_fields(self) -> None:
        bar = _option()
        assert bar.side == "CE"
        assert bar.strike == Decimal("17500")
        assert bar.spot == Decimal("17480.5")
        assert bar.iv == Decimal("0.15")

    def test_pe_side_with_none_extras(self) -> None:
        bar = _option(side="PE", strike=None, spot=None, iv=None, open_interest=None)
        assert bar.side == "PE"
        assert bar.strike is None and bar.spot is None and bar.iv is None
        assert bar.open_interest is None

    def test_zero_iv_allowed(self) -> None:
        assert _option(iv=Decimal("0")).iv == Decimal("0")


class TestOptionBarValidation:
    def test_shared_ohlc_invariant_uses_option_owner(self) -> None:
        # The shared validator stamps the OptionBar owner in its message.
        with pytest.raises(ValueError, match="OptionBar.timestamp"):
            _option(timestamp=datetime(2024, 1, 25, 9, 15, 0))  # naive

    def test_rejects_unknown_side(self) -> None:
        with pytest.raises(ValueError, match="side must be one of"):
            _option(side="CALL")

    def test_rejects_non_positive_strike(self) -> None:
        with pytest.raises(ValueError, match="strike must be positive"):
            _option(strike=Decimal("0"))

    def test_rejects_non_positive_spot(self) -> None:
        with pytest.raises(ValueError, match="spot must be positive"):
            _option(spot=Decimal("-1"))

    def test_rejects_negative_iv(self) -> None:
        with pytest.raises(ValueError, match="iv must be non-negative"):
            _option(iv=Decimal("-0.01"))

    def test_is_frozen(self) -> None:
        bar = _option()
        with pytest.raises((AttributeError, TypeError)):
            bar.side = "PE"  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# CatalogEntry
# ────────────────────────────────────────────────────────────────────────


def _valid_entry(**overrides: object) -> CatalogEntry:
    defaults: dict[str, object] = {
        "exchange": "NSE",
        "segment": "FNO",
        "symbol": "NSE-NIFTY",
        "interval": "1minute",
        "year": 2024,
        "month": 1,
        "raw_hash": "0" * 64,
        "row_count": 100,
        "first_ts": _ts(minute=15),
        "last_ts": _ts(minute=55),
        "fetched_at": _ts(minute=59),
    }
    defaults.update(overrides)
    return CatalogEntry(**defaults)  # type: ignore[arg-type]


class TestCatalogEntryValidation:
    def test_happy_path(self) -> None:
        entry = _valid_entry()
        assert entry.year == 2024
        assert len(entry.raw_hash) == 64

    def test_rejects_short_hash(self) -> None:
        with pytest.raises(ValueError, match="raw_hash"):
            _valid_entry(raw_hash="abc123")

    @pytest.mark.parametrize("bad_month", [0, 13, -1, 100])
    def test_rejects_bad_month(self, bad_month: int) -> None:
        with pytest.raises(ValueError, match="month"):
            _valid_entry(month=bad_month)

    def test_rejects_negative_row_count(self) -> None:
        with pytest.raises(ValueError, match="row_count"):
            _valid_entry(row_count=-1)

    def test_rejects_first_after_last(self) -> None:
        with pytest.raises(ValueError, match="first_ts"):
            _valid_entry(
                first_ts=_ts(minute=55),
                last_ts=_ts(minute=15),
            )

    def test_rejects_naive_first_ts(self) -> None:
        with pytest.raises(ValueError, match="first_ts"):
            _valid_entry(first_ts=datetime(2024, 1, 1, 9, 15))

    def test_rejects_naive_last_ts(self) -> None:
        with pytest.raises(ValueError, match="last_ts"):
            _valid_entry(last_ts=datetime(2024, 1, 1, 9, 55))

    def test_rejects_naive_fetched_at(self) -> None:
        with pytest.raises(ValueError, match="fetched_at"):
            _valid_entry(fetched_at=datetime(2024, 1, 1, 9, 59))


# ────────────────────────────────────────────────────────────────────────
# Price positivity (DEC-STORED-DATA-VALIDATION-001) — the L1-a read gate
# ────────────────────────────────────────────────────────────────────────


class TestPricePositivity:
    """Non-positive prices are rejected by the SHARED per-bar validator.

    Fails-before (round-2 audit, live-reproduced): an internally-ORDERED
    all-negative bar (open=-5, high=-1, low=-10, close=-2) satisfied every
    ordering invariant and passed silently — a legacy pre-#912 stored
    partition could flow a day_low of -1000 into Stage-1A + run identity.
    """

    def test_rejects_ordered_all_negative_bar(self) -> None:
        # THE audit repro: ordered, yet every price is negative.
        with pytest.raises(ValueError, match="positive price"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("-5"),
                high=Decimal("-1"),
                low=Decimal("-10"),
                close=Decimal("-2"),
                volume=5,
                open_interest=None,
            )

    @pytest.mark.parametrize("field", ["open", "high", "low", "close"])
    def test_rejects_zero_price_naming_field(self, field: str) -> None:
        prices: dict[str, Decimal] = {
            "open": Decimal("100"),
            "high": Decimal("101"),
            "low": Decimal("99"),
            "close": Decimal("100"),
        }
        # Zeroing one field may ALSO break ordering — positivity fires FIRST
        # (pinned below), so the message names the real defect per field.
        prices[field] = Decimal("0")
        with pytest.raises(ValueError, match=f"CandleBar.{field} must be a positive price"):
            CandleBar(
                timestamp=_ts(),
                open=prices["open"],
                high=prices["high"],
                low=prices["low"],
                close=prices["close"],
                volume=0,
                open_interest=None,
            )

    def test_error_names_the_bar_timestamp(self) -> None:
        with pytest.raises(ValueError, match="2024-01-25T09:15:00"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("0"),
                high=Decimal("0"),
                low=Decimal("0"),
                close=Decimal("0"),
                volume=0,
                open_interest=None,
            )

    def test_positivity_fires_before_ordering(self) -> None:
        # An UN-ordered negative bar reports positivity, not ordering.
        with pytest.raises(ValueError, match="positive price"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("-5"),
                high=Decimal("-10"),  # also high < low — ordering broken too
                low=Decimal("-1"),
                close=Decimal("-2"),
                volume=0,
                open_interest=None,
            )

    def test_option_bar_shares_the_positivity_gate(self) -> None:
        with pytest.raises(ValueError, match="OptionBar.open must be a positive price"):
            _option(open=Decimal("-5"), high=Decimal("-1"), low=Decimal("-10"), close=Decimal("-2"))

    def test_greeks_bar_shares_the_positivity_gate(self) -> None:
        with pytest.raises(ValueError, match="FnoGreeksBar.open must be a positive price"):
            _greeks_bar(
                open=Decimal("-5"), high=Decimal("-1"), low=Decimal("-10"), close=Decimal("-2")
            )

    def test_smallest_tick_price_is_accepted(self) -> None:
        # 0.05 — the smallest real Indian tick — stays a valid bar.
        tick = Decimal("0.05")
        bar = CandleBar(
            timestamp=_ts(),
            open=tick,
            high=tick,
            low=tick,
            close=tick,
            volume=1,
            open_interest=None,
        )
        assert bar.low == tick
