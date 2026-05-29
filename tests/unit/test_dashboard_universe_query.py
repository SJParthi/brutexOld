"""Unit tests for :mod:`brutex.dashboard.universe_query`.

Pure-Python — no FastAPI, no DuckDB, no network. Builds
:class:`~brutex.models.Instrument` fixtures and a lightweight fake
snapshot (any iterable of instruments) and asserts the classification +
summary + listing logic at 100% line + branch coverage.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.dashboard.universe_query import (
    EXPLORER_KINDS,
    KIND_INDEX,
    KIND_STOCK,
    classify_instrument,
    list_underlyings,
    universe_summary,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import build_fno_eligible_underlyings

# ────────────────────────────────────────────────────────────────────────
# Fixture builders
# ────────────────────────────────────────────────────────────────────────


def _index(
    symbol: str, *, exchange: Exchange = Exchange.NSE, name: str | None = None
) -> Instrument:
    """Build an IDX instrument fixture."""
    return Instrument(
        exchange=exchange,
        exchange_token=1,
        trading_symbol=symbol,
        vendor_symbol=f"{exchange}-{symbol}",
        name=name,
        instrument_type=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _equity(symbol: str, *, name: str | None = None) -> Instrument:
    """Build an NSE EQ instrument fixture."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=2,
        trading_symbol=symbol,
        vendor_symbol=f"NSE-{symbol}",
        name=name,
        instrument_type=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _option(
    underlying: str, *, strike: str = "100", side: InstrumentType = InstrumentType.CE
) -> Instrument:
    """Build an NSE option leg fixture naming *underlying*."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=3,
        trading_symbol=f"{underlying}25JAN{strike}{side}",
        vendor_symbol=f"NSE-{underlying}-30Jan25-{strike}-{side}",
        name=None,
        instrument_type=side,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_exchange_token=2,
        lot_size=50,
        tick_size=Decimal("0.05"),
        freeze_quantity=1800,
        expiry_date=date(2025, 1, 30),
        strike_price=Decimal(strike),
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


# ────────────────────────────────────────────────────────────────────────
# classify_instrument
# ────────────────────────────────────────────────────────────────────────


class TestClassifyInstrument:
    def test_index_is_index(self) -> None:
        eligible = frozenset()
        assert classify_instrument(_index("NIFTY"), eligible) == KIND_INDEX

    def test_non_fno_sectoral_index_is_index(self) -> None:
        # A non-F&O / non-brute sectoral index (NIFTY IT) is still an IDX
        # instrument → classifies as "index". Locks the all-NSE-indices
        # behaviour (Refinement B): the Index tab is NOT limited to the 3
        # F&O / brute-force indices.
        eligible = frozenset()
        assert classify_instrument(_index("NIFTYIT"), eligible) == KIND_INDEX

    def test_fno_eligible_equity_is_stock(self) -> None:
        instruments = [_equity("RELIANCE"), _option("RELIANCE")]
        eligible = build_fno_eligible_underlyings(instruments)
        assert classify_instrument(_equity("RELIANCE"), eligible) == KIND_STOCK

    def test_non_fno_equity_is_none(self) -> None:
        # No derivative names WIPRO → not F&O-eligible → unclassified.
        eligible = build_fno_eligible_underlyings([_equity("WIPRO")])
        assert classify_instrument(_equity("WIPRO"), eligible) is None

    def test_derivative_leg_is_none(self) -> None:
        instruments = [_equity("RELIANCE"), _option("RELIANCE")]
        eligible = build_fno_eligible_underlyings(instruments)
        assert classify_instrument(_option("RELIANCE"), eligible) is None

    def test_bse_non_sensex_index_is_none(self) -> None:
        # Now that classify delegates to is_universe_underlying
        # (DEC-INSTRUMENT-UNIVERSE-FAST-001), a BSE non-SENSEX index is NOT a
        # universe underlying → unclassified (it is never persisted anyway).
        eligible = frozenset()
        assert classify_instrument(_index("BSE500", exchange=Exchange.BSE), eligible) is None

    def test_bse_sensex_index_is_index(self) -> None:
        eligible = frozenset()
        assert classify_instrument(_index("SENSEX", exchange=Exchange.BSE), eligible) == KIND_INDEX


# ────────────────────────────────────────────────────────────────────────
# universe_summary
# ────────────────────────────────────────────────────────────────────────


class TestUniverseSummary:
    def test_counts_index_and_stock(self) -> None:
        snapshot = [
            _index("NIFTY"),
            _index("BANKNIFTY"),
            _equity("RELIANCE"),
            _option("RELIANCE"),  # derivative — not counted
            _equity("WIPRO"),  # no derivative → not F&O-eligible, not counted
        ]
        summary = universe_summary(snapshot)
        assert summary == {"index_count": 2, "stock_count": 1, "total": 3}

    def test_empty_snapshot(self) -> None:
        assert universe_summary([]) == {"index_count": 0, "stock_count": 0, "total": 0}

    def test_precomputed_fno_eligible_candidates_only(self) -> None:
        # Fast path: candidates-only (IDX + EQ, no legs) + precomputed eligible
        # set still tallies the stock correctly (DEC-INSTRUMENT-UNIVERSE-FAST-001).
        candidates = [_index("NIFTY"), _index("BANKNIFTY"), _equity("RELIANCE")]
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        assert universe_summary(candidates, fno_eligible=eligible) == {
            "index_count": 2,
            "stock_count": 1,
            "total": 3,
        }


# ────────────────────────────────────────────────────────────────────────
# list_underlyings
# ────────────────────────────────────────────────────────────────────────


class TestListUnderlyings:
    def _snapshot(self) -> list[Instrument]:
        return [
            _index("NIFTY", name="NIFTY 50"),
            _index("BANKNIFTY", name="Nifty Bank"),
            _equity("RELIANCE", name="Reliance Industries"),
            _equity("TCS", name="Tata Consultancy"),
            _option("RELIANCE"),
            _option("TCS"),
            _equity("WIPRO", name="Wipro"),  # not F&O-eligible
        ]

    def test_no_filter_returns_index_then_stock_sorted(self) -> None:
        rows = list_underlyings(self._snapshot())
        kinds = [r["kind"] for r in rows]
        # indices come first (EXPLORER_KINDS order), then stocks
        assert kinds == [KIND_INDEX, KIND_INDEX, KIND_STOCK, KIND_STOCK]
        # within a kind, alphabetical by symbol
        assert [r["symbol"] for r in rows] == ["BANKNIFTY", "NIFTY", "RELIANCE", "TCS"]

    def test_filter_index(self) -> None:
        rows = list_underlyings(self._snapshot(), kind=KIND_INDEX)
        assert {r["symbol"] for r in rows} == {"NIFTY", "BANKNIFTY"}
        assert all(r["kind"] == KIND_INDEX for r in rows)

    def test_non_fno_sectoral_index_listed_under_index_kind(self) -> None:
        # Refinement B: a non-F&O sectoral index (NIFTY IT) must appear in
        # the Index tab alongside the brute-force indices — the explorer
        # surfaces ALL NSE indices, not just the 3 F&O ones. NIFTY IT has
        # no derivatives in the snapshot (spot-only), proving non-F&O
        # indices are still listed.
        snapshot = [
            _index("NIFTY", name="NIFTY 50"),
            _index("NIFTYIT", name="NIFTY IT"),
            _equity("WIPRO"),  # non-F&O equity → excluded, not the index
        ]
        rows = list_underlyings(snapshot, kind=KIND_INDEX)
        assert {r["symbol"] for r in rows} == {"NIFTY", "NIFTYIT"}
        assert all(r["kind"] == KIND_INDEX for r in rows)

    def test_filter_stock(self) -> None:
        rows = list_underlyings(self._snapshot(), kind=KIND_STOCK)
        assert {r["symbol"] for r in rows} == {"RELIANCE", "TCS"}
        assert all(r["kind"] == KIND_STOCK for r in rows)

    def test_name_falls_back_to_symbol(self) -> None:
        rows = list_underlyings([_index("NIFTY", name=None)])
        assert rows[0]["name"] == "NIFTY"

    def test_row_shape(self) -> None:
        inst = _index("NIFTY", name="NIFTY 50")
        rows = list_underlyings([inst])
        assert rows[0] == {
            "symbol": "NIFTY",
            "pull_symbol": inst.vendor_symbol,
            "name": "NIFTY 50",
            "kind": KIND_INDEX,
            "exchange": "NSE",
        }

    def test_pull_symbol_is_exchange_prefixed(self) -> None:
        # B1: the spot pull must POST the exchange-prefixed Groww symbol
        # (vendor_symbol) so symbol_to_exchange_segment can split the
        # leading exchange token — a bare trading_symbol would break it.
        snapshot = [
            _index("NIFTY", exchange=Exchange.NSE),
            _index("SENSEX", exchange=Exchange.BSE),
            _equity("RELIANCE"),
            _option("RELIANCE"),
        ]
        rows = list_underlyings(snapshot)
        assert rows  # non-empty
        for row in rows:
            assert row["pull_symbol"].startswith(("NSE-", "BSE-")), row
            # pull_symbol carries the exchange prefix; display symbol does not.
            assert row["pull_symbol"].endswith(row["symbol"])

    def test_precomputed_fno_eligible_candidates_only(self) -> None:
        # Fast path (DEC-INSTRUMENT-UNIVERSE-FAST-001): pass a candidates-only
        # snapshot (IDX + EQ, NO derivative legs) plus a precomputed eligible
        # set — RELIANCE must still classify as a stock even though no leg is
        # present to derive eligibility from.
        candidates = [_index("NIFTY", name="NIFTY 50"), _equity("RELIANCE")]
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        rows = list_underlyings(candidates, fno_eligible=eligible)
        assert {(r["symbol"], r["kind"]) for r in rows} == {
            ("NIFTY", KIND_INDEX),
            ("RELIANCE", KIND_STOCK),
        }

    def test_precomputed_fno_eligible_empty_drops_equity(self) -> None:
        # With an explicit empty eligible set, an EQ candidate is NOT a stock
        # (the precomputed set is authoritative; no rebuild from the snapshot).
        candidates = [_index("NIFTY"), _equity("RELIANCE"), _option("RELIANCE")]
        rows = list_underlyings(candidates, fno_eligible=frozenset())
        assert {r["symbol"] for r in rows} == {"NIFTY"}

    def test_invalid_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="kind must be one of"):
            list_underlyings([], kind="bogus")

    def test_explorer_kinds_constant(self) -> None:
        assert EXPLORER_KINDS == (KIND_INDEX, KIND_STOCK)
