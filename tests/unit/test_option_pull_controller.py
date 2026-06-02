"""Tests for `brutex.dashboard.option_pull_controller` — the options pull core.

100% line+branch via the synthetic dry-run path + injected fake client/sink:
the instrument→underlying mapping (IDX/EQ/blank/derivative), the report
aggregation (completed series + quarantined failures grouped by underlying),
the dry-run default path, the injected-factory (non-dry-run) branch, the
empty-list no-op, and the ``to_dict`` projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.dhan.errors import DhanError
from brutex.adapters.dhan.option_ingest import JsonPoster, OptionBarSink
from brutex.adapters.dhan.option_synthetic import SyntheticOptionClient
from brutex.dashboard.option_pull_controller import (
    OptionsPullReport,
    OptionSymbolResult,
    instrument_to_option_underlying,
    pull_options,
)
from brutex.data.option_sink import NullOptionBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_NOW = datetime(2021, 1, 6, tzinfo=UTC)  # tiny window so the synthetic fan-out is fast


def _index(symbol: str = "13", exchange: Exchange = Exchange.NSE) -> Instrument:
    """A spot index underlying (IDX → OPTIDX / ATM±10)."""
    return Instrument(
        exchange=exchange,
        exchange_token=26000,
        trading_symbol="NIFTY",
        vendor_symbol=symbol,
        name="NIFTY",
        instrument_type=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=0,
        tick_size=Decimal("0"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _stock(symbol: str = "1333") -> Instrument:
    """A spot equity underlying (EQ → OPTSTK / ATM±3)."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=1333,
        trading_symbol="HDFCBANK",
        vendor_symbol=symbol,
        name="HDFCBANK",
        instrument_type=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin="INE040A01034",
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=550,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _option_leg() -> Instrument:
    """A CE leg — never a valid options fan-out *underlying*."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=35044,
        trading_symbol="NIFTY25MAR29050CE",
        vendor_symbol="NSE-NIFTY-27Mar25-29050-CE",
        name=None,
        instrument_type=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_exchange_token=26000,
        lot_size=25,
        tick_size=Decimal("0.05"),
        freeze_quantity=7200,
        expiry_date=date(2025, 3, 27),
        strike_price=Decimal("29050"),
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


# ── instrument_to_option_underlying ──────────────────────────────────────


class TestInstrumentToOptionUnderlying:
    def test_index_maps_to_optidx_wide_window(self) -> None:
        u = instrument_to_option_underlying(_index(symbol="13", exchange=Exchange.NSE))
        assert u.security_id == "13"
        assert u.instrument == "OPTIDX"
        assert u.exchange_segment == "NSE_FNO"
        assert u.strike_offsets == 10

    def test_bse_index_maps_to_bse_fno(self) -> None:
        u = instrument_to_option_underlying(_index(symbol="51", exchange=Exchange.BSE))
        assert u.exchange_segment == "BSE_FNO"

    def test_stock_maps_to_optstk_narrow_window(self) -> None:
        u = instrument_to_option_underlying(_stock(symbol="1333"))
        assert u.instrument == "OPTSTK"
        assert u.exchange_segment == "NSE_FNO"
        assert u.strike_offsets == 3

    def test_blank_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty Dhan SECURITY_ID"):
            instrument_to_option_underlying(_index(symbol="   "))

    def test_derivative_leg_is_not_an_underlying(self) -> None:
        with pytest.raises(ValueError, match="only IDX / EQ"):
            instrument_to_option_underlying(_option_leg())


# ── pull_options ─────────────────────────────────────────────────────────


class TestPullOptions:
    def test_dry_run_single_index_counts_synthetic_bars(self) -> None:
        report = pull_options([_index()], lake_root=Path("/unused"), dry_run=True, now=_NOW)
        assert report.dry_run is True
        assert report.start_date == "2021-01-01"
        # No explicit to_date → default end is the previous day (now=01-06 →
        # 01-05); the in-progress current session is excluded by default (FIX 5).
        assert report.end_date == "2021-01-05"
        assert report.total_symbols == 1
        # 21 strikes × 2 flags × 3 codes × 2 types = 252 series, all complete.
        assert report.symbols[0].series_completed == 21 * 2 * 3 * 2
        assert report.symbols[0].series_failed == 0
        assert report.symbols[0].instrument == "OPTIDX"
        # Every series appends synthetic in-session bars → strictly positive.
        assert report.total_bars > 0
        assert report.total_windows == report.symbols[0].series_completed  # 1 window each

    def test_dry_run_bulk_mix_of_index_and_stock(self) -> None:
        report = pull_options(
            [_index(symbol="13"), _stock(symbol="1333")],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
        )
        assert report.total_symbols == 2
        by_symbol = {s.symbol: s for s in report.symbols}
        assert by_symbol["13"].series_completed == 21 * 2 * 3 * 2  # ATM±10
        assert by_symbol["1333"].series_completed == 7 * 2 * 3 * 2  # ATM±3

    def test_empty_list_is_clean_noop(self) -> None:
        report = pull_options([], lake_root=Path("/unused"), dry_run=True, now=_NOW)
        assert report.total_symbols == 0
        assert report.total_bars == 0
        assert report.total_series_completed == 0

    def test_injected_factory_non_dry_run_branch(self) -> None:
        """A fake (live-shaped) factory exercises the not-dry-run branch with no network."""
        client = SyntheticOptionClient(bars_per_window=2)
        sink = NullOptionBarSink()

        captured: list[Path] = []

        def factory(lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            captured.append(lake_root)
            return client, sink

        report = pull_options(
            [_index()],
            lake_root=Path("/lake"),
            dry_run=False,  # not dry-run, but injected factory ⇒ no live rate/network
            now=_NOW,
            client_factory=factory,
        )
        assert captured == [Path("/lake")]
        assert report.dry_run is False
        assert report.total_series_completed == 21 * 2 * 3 * 2

    def test_terminal_failure_is_quarantined_per_underlying(self) -> None:
        """A fake client that always errors terminally → every series quarantined."""

        class _AlwaysTerminal:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                raise DhanError("DH-902 terminal")

        def factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return _AlwaysTerminal(), NullOptionBarSink()

        report = pull_options(
            [_stock(symbol="1333")],
            lake_root=Path("/lake"),
            dry_run=False,
            now=_NOW,
            client_factory=factory,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        # 7 strikes × 1 flag × 1 code × 1 type = 7 series, all terminal → quarantined.
        assert report.symbols[0].series_failed == 7
        assert report.symbols[0].series_completed == 0
        assert report.total_bars == 0

    def test_axis_overrides_shrink_the_fan_out(self) -> None:
        report = pull_options(
            [_index()],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        assert report.symbols[0].series_completed == 21  # 21 strikes × 1 × 1 × 1

    def test_on_progress_forwarded_to_driver(self) -> None:
        """pull_options threads on_progress → fires once per underlying (dry-run)."""
        events: list[tuple[str, int, bool]] = []
        pull_options(
            [_index(symbol="13"), _stock(symbol="1333")],
            lake_root=Path("/unused"),
            dry_run=True,
            now=_NOW,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
            on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
        )
        # One event per underlying, in order, each with bars>0 and not failed.
        assert [e[0] for e in events] == ["13", "1333"]
        assert all(bars > 0 and failed is False for _, bars, failed in events)

    def test_now_defaults_to_wall_clock(self) -> None:
        """Omitting *now* uses the real clock — the window still starts at the floor."""
        report = pull_options(
            [_index()],
            lake_root=Path("/unused"),
            dry_run=True,
            expiry_flags=("WEEK",),
            expiry_codes=(1,),
            option_types=("CALL",),
        )
        assert report.start_date == "2021-01-01"
        assert report.total_symbols == 1


# ── report dataclasses ───────────────────────────────────────────────────


class TestOptionsPullReport:
    def test_aggregate_properties_and_to_dict(self) -> None:
        report = OptionsPullReport(
            dry_run=True,
            start_date="2021-01-01",
            end_date="2021-01-05",
            symbols=[
                OptionSymbolResult(
                    symbol="13",
                    instrument="OPTIDX",
                    series_completed=4,
                    series_failed=1,
                    bars_appended=40,
                    windows=4,
                ),
                OptionSymbolResult(
                    symbol="1333",
                    instrument="OPTSTK",
                    series_completed=2,
                    series_failed=0,
                    bars_appended=10,
                    windows=2,
                ),
            ],
        )
        assert report.total_symbols == 2
        assert report.total_series_completed == 6
        assert report.total_series_failed == 1
        assert report.total_bars == 50
        assert report.total_windows == 6

        payload = report.to_dict()
        assert payload["total_symbols"] == 2
        assert payload["total_bars"] == 50
        assert payload["symbols"][0] == {
            "symbol": "13",
            "instrument": "OPTIDX",
            "series_completed": 4,
            "series_failed": 1,
            "bars_appended": 40,
            "windows": 4,
        }

    def test_empty_report_totals_are_zero(self) -> None:
        report = OptionsPullReport(dry_run=False, start_date="2021-01-01", end_date="2021-01-05")
        assert report.total_symbols == 0
        assert report.total_bars == 0
        assert report.to_dict()["symbols"] == []
