"""Unit tests for brutex.universe — the Track 1 spot-index universe.

Rewritten 2026-05-15 for the Groww + spot-index pivot. The prior tests
expected an 8-named-index F&O universe with futures/options/stock
contracts; that scope was wiped (see ``docs/MIGRATION.md``). Track 1
universe is exactly 3 spot indexes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import (
    ALLOWED_UNDERLYINGS,
    BRUTE_FORCE_INDEX_UNDERLYINGS,
    BRUTE_FORCE_INDICES,
    INDEX_UNDERLYINGS,
    REFERENCE_ONLY_INDICES,
    V1_INDICES,
    V1_UNDERLYINGS,
    UnderlyingSpec,
    build_fno_eligible_underlyings,
    classify_underlying_kind,
    is_in_brute_force_scope,
    is_in_fetch_scope,
    is_in_scope,
    is_index_underlying,
    is_universe_underlying,
)

# ────────────────────────────────────────────────────────────────────────
# UnderlyingSpec
# ────────────────────────────────────────────────────────────────────────


class TestUnderlyingSpec:
    """The frozen dataclass that anchors each Track 1 index."""

    def test_str_repr(self) -> None:
        spec = UnderlyingSpec(
            exchange=Exchange.NSE,
            underlying_symbol="NIFTY",
            display_name="NIFTY 50",
            spot_live_since_year=1996,
        )
        assert str(spec) == "NSE:NIFTY"

    def test_frozen(self) -> None:
        spec = V1_INDICES[0]
        with pytest.raises(Exception):  # FrozenInstanceError
            spec.underlying_symbol = "FOO"  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# V1_INDICES — the 4 Track 1 indices allow-list (NIFTY + BANKNIFTY + SENSEX + INDIAVIX)
# ────────────────────────────────────────────────────────────────────────


class TestV1IndicesComposition:
    """V1_INDICES is the Track 1 named-index allow-list (exactly 4)."""

    def test_has_exactly_four_indices(self) -> None:
        assert len(V1_INDICES) == 4

    def test_nse_contains_nifty_banknifty_indiavix(self) -> None:
        nse_syms = {u.underlying_symbol for u in V1_INDICES if u.exchange == Exchange.NSE}
        assert nse_syms == {"NIFTY", "BANKNIFTY", "INDIAVIX"}

    def test_contains_sensex_on_bse(self) -> None:
        bse_syms = {u.underlying_symbol for u in V1_INDICES if u.exchange == Exchange.BSE}
        assert bse_syms == {"SENSEX"}

    def test_ordering_groups_nse_with_bse_between(self) -> None:
        """Ordering: NIFTY, BANKNIFTY (NSE) → SENSEX (BSE) → INDIAVIX (NSE)."""
        exchanges = [u.exchange for u in V1_INDICES]
        # NSE-NSE-BSE-NSE: a deliberate ordering preserving the original
        # 3-index sequence with INDIAVIX appended at the end.
        assert exchanges == [Exchange.NSE, Exchange.NSE, Exchange.BSE, Exchange.NSE]

    def test_spot_live_years_are_historical(self) -> None:
        """Every index must have been live before 2021 (Track 1 backtest window)."""
        for spec in V1_INDICES:
            assert spec.spot_live_since_year < 2021

    def test_display_names_match_v1_spec(self) -> None:
        """Display names match Groww's instrument master conventions."""
        names = {u.display_name for u in V1_INDICES}
        assert names == {"NIFTY 50", "NIFTY BANK", "BSE SENSEX", "India VIX"}

    def test_is_tuple_not_list_frozen(self) -> None:
        assert isinstance(V1_INDICES, tuple)

    def test_v1_underlyings_alias(self) -> None:
        """V1_UNDERLYINGS is a backwards-compat alias for V1_INDICES."""
        assert V1_UNDERLYINGS is V1_INDICES


# ────────────────────────────────────────────────────────────────────────
# Reference-only vs brute-force partitioning
# ────────────────────────────────────────────────────────────────────────


class TestReferenceOnlyPartition:
    """India VIX is fetched + stored but never in brute-force combinations."""

    def test_india_vix_is_reference_only(self) -> None:
        vix = next(u for u in V1_INDICES if u.underlying_symbol == "INDIAVIX")
        assert vix.reference_only is True

    def test_three_spot_indices_are_brute_forced(self) -> None:
        for sym in ("NIFTY", "BANKNIFTY", "SENSEX"):
            spec = next(u for u in V1_INDICES if u.underlying_symbol == sym)
            assert spec.reference_only is False

    def test_brute_force_indices_excludes_vix(self) -> None:
        symbols = {u.underlying_symbol for u in BRUTE_FORCE_INDICES}
        assert symbols == {"NIFTY", "BANKNIFTY", "SENSEX"}
        assert "INDIAVIX" not in symbols

    def test_brute_force_count_is_three(self) -> None:
        assert len(BRUTE_FORCE_INDICES) == 3

    def test_reference_only_contains_only_vix(self) -> None:
        symbols = {u.underlying_symbol for u in REFERENCE_ONLY_INDICES}
        assert symbols == {"INDIAVIX"}

    def test_reference_only_count_is_one(self) -> None:
        assert len(REFERENCE_ONLY_INDICES) == 1

    def test_partition_is_complete(self) -> None:
        """V1_INDICES == BRUTE_FORCE_INDICES ∪ REFERENCE_ONLY_INDICES, disjoint."""
        bf = set(BRUTE_FORCE_INDICES)
        ro = set(REFERENCE_ONLY_INDICES)
        assert bf.isdisjoint(ro)
        assert bf | ro == set(V1_INDICES)


# ────────────────────────────────────────────────────────────────────────
# INDEX_UNDERLYINGS — the O(1) membership set
# ────────────────────────────────────────────────────────────────────────


class TestIndexUnderlyings:
    def test_is_frozenset(self) -> None:
        assert isinstance(INDEX_UNDERLYINGS, frozenset)

    def test_has_four_entries(self) -> None:
        assert len(INDEX_UNDERLYINGS) == 4

    def test_contains_expected_pairs(self) -> None:
        assert (
            frozenset(
                {
                    (Exchange.NSE, "NIFTY"),
                    (Exchange.NSE, "BANKNIFTY"),
                    (Exchange.BSE, "SENSEX"),
                    (Exchange.NSE, "INDIAVIX"),
                }
            )
            == INDEX_UNDERLYINGS
        )

    def test_allowed_underlyings_alias(self) -> None:
        """ALLOWED_UNDERLYINGS is the backwards-compat alias."""
        assert ALLOWED_UNDERLYINGS is INDEX_UNDERLYINGS


# ────────────────────────────────────────────────────────────────────────
# is_index_underlying
# ────────────────────────────────────────────────────────────────────────


class TestIsIndexUnderlying:
    @pytest.mark.parametrize(
        ("exchange", "symbol"),
        [
            (Exchange.NSE, "NIFTY"),
            (Exchange.NSE, "BANKNIFTY"),
            (Exchange.BSE, "SENSEX"),
            (Exchange.NSE, "INDIAVIX"),
        ],
    )
    def test_named_indices_accepted(self, exchange: Exchange, symbol: str) -> None:
        assert is_index_underlying(exchange, symbol) is True

    @pytest.mark.parametrize(
        ("exchange", "symbol"),
        [
            (Exchange.NSE, "RELIANCE"),
            (Exchange.NSE, "FINNIFTY"),
            (Exchange.NSE, "SENSEX"),
            (Exchange.BSE, "NIFTY"),
            (Exchange.NSE, "TCS"),
        ],
    )
    def test_rejects_out_of_scope(self, exchange: Exchange, symbol: str) -> None:
        assert is_index_underlying(exchange, symbol) is False


# ────────────────────────────────────────────────────────────────────────
# Helpers — build Instrument fixtures
# ────────────────────────────────────────────────────────────────────────


def _make_idx(exchange: Exchange, symbol: str) -> Instrument:
    """Build a minimal IDX-type Instrument for scope testing."""
    return Instrument(
        exchange=exchange,
        exchange_token=hash(symbol) % 100_000,
        trading_symbol=symbol,
        vendor_symbol=symbol,
        name=symbol,
        instrument_type=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=symbol,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=False,
        sell_allowed=False,
    )


def _make_equity(exchange: Exchange, symbol: str) -> Instrument:
    """Build a cash-equity Instrument (out of scope for Track 1)."""
    return Instrument(
        exchange=exchange,
        exchange_token=hash(symbol) % 100_000,
        trading_symbol=symbol,
        vendor_symbol=symbol,
        name=symbol,
        instrument_type=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=symbol,
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


# ────────────────────────────────────────────────────────────────────────
# is_in_scope
# ────────────────────────────────────────────────────────────────────────


class TestIsInScope:
    @pytest.mark.parametrize(
        ("exchange", "symbol"),
        [
            (Exchange.NSE, "NIFTY"),
            (Exchange.NSE, "BANKNIFTY"),
            (Exchange.BSE, "SENSEX"),
            (Exchange.NSE, "INDIAVIX"),
        ],
    )
    def test_four_track1_indices_in_scope(self, exchange: Exchange, symbol: str) -> None:
        assert is_in_scope(_make_idx(exchange, symbol)) is True

    def test_cash_equity_rejected(self) -> None:
        """Cash stocks are not in Track 1 scope."""
        assert is_in_scope(_make_equity(Exchange.NSE, "RELIANCE")) is False

    def test_non_v1_index_rejected(self) -> None:
        """FINNIFTY and other formerly-F&O indices are now out of scope."""
        assert is_in_scope(_make_idx(Exchange.NSE, "FINNIFTY")) is False

    def test_wrong_exchange_rejected(self) -> None:
        """SENSEX on NSE (wrong exchange) is rejected."""
        assert is_in_scope(_make_idx(Exchange.NSE, "SENSEX")) is False


# ────────────────────────────────────────────────────────────────────────
# classify_underlying_kind
# ────────────────────────────────────────────────────────────────────────


class TestClassifyUnderlyingKind:
    def test_returns_index_for_in_scope(self) -> None:
        for spec in V1_INDICES:
            inst = _make_idx(spec.exchange, spec.underlying_symbol)
            assert classify_underlying_kind(inst) == "index"

    def test_raises_on_equity(self) -> None:
        inst = _make_equity(Exchange.NSE, "RELIANCE")
        with pytest.raises(ValueError, match="not in Track 1 scope"):
            classify_underlying_kind(inst)

    def test_raises_on_non_v1_index(self) -> None:
        inst = _make_idx(Exchange.NSE, "FINNIFTY")
        with pytest.raises(ValueError, match="not in Track 1 scope"):
            classify_underlying_kind(inst)


# ────────────────────────────────────────────────────────────────────────
# Two-tier scope (DEC-FETCH-ALL-001)
# ────────────────────────────────────────────────────────────────────────


def _make_derivative(
    exchange: Exchange,
    instrument_type: InstrumentType,
    underlying: str,
    *,
    trading_symbol: str | None = None,
) -> Instrument:
    """Build a FUT / CE / PE derivative Instrument for fetch-scope testing."""
    is_option = instrument_type in (InstrumentType.CE, InstrumentType.PE)
    return Instrument(
        exchange=exchange,
        exchange_token=hash((underlying, instrument_type)) % 100_000,
        trading_symbol=trading_symbol or f"{underlying}-DERIV",
        vendor_symbol=f"{exchange}-{underlying}-{instrument_type}",
        name=underlying,
        instrument_type=instrument_type,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_exchange_token=None,
        lot_size=50,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=date(2025, 1, 30),
        strike_price=Decimal("28500") if is_option else None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


class TestIsInBruteForceScope:
    """The narrow ENGINE tier — only the 3 swept indexes."""

    @pytest.mark.parametrize(
        ("exchange", "symbol"),
        [
            (Exchange.NSE, "NIFTY"),
            (Exchange.NSE, "BANKNIFTY"),
            (Exchange.BSE, "SENSEX"),
        ],
    )
    def test_three_swept_indices_in_scope(self, exchange: Exchange, symbol: str) -> None:
        assert is_in_brute_force_scope(_make_idx(exchange, symbol)) is True

    def test_india_vix_excluded(self) -> None:
        """India VIX is reference-only — never brute-forced."""
        assert is_in_brute_force_scope(_make_idx(Exchange.NSE, "INDIAVIX")) is False

    def test_equity_rejected_by_type(self) -> None:
        assert is_in_brute_force_scope(_make_equity(Exchange.NSE, "RELIANCE")) is False

    def test_non_swept_index_rejected_by_membership(self) -> None:
        assert is_in_brute_force_scope(_make_idx(Exchange.NSE, "FINNIFTY")) is False

    def test_underlyings_set_excludes_vix(self) -> None:
        assert (Exchange.NSE, "INDIAVIX") not in BRUTE_FORCE_INDEX_UNDERLYINGS
        assert (Exchange.NSE, "NIFTY") in BRUTE_FORCE_INDEX_UNDERLYINGS
        assert len(BRUTE_FORCE_INDEX_UNDERLYINGS) == 3


class TestBuildFnoEligibleUnderlyings:
    """Derive the F&O-eligible underlying set from the instrument master."""

    def test_collects_nse_derivative_underlyings(self) -> None:
        instruments = [
            _make_derivative(Exchange.NSE, InstrumentType.FUT, "RELIANCE"),
            _make_derivative(Exchange.NSE, InstrumentType.CE, "TCS"),
        ]
        eligible = build_fno_eligible_underlyings(instruments)
        assert eligible == frozenset({(Exchange.NSE, "RELIANCE"), (Exchange.NSE, "TCS")})

    def test_skips_non_derivatives(self) -> None:
        instruments = [
            _make_idx(Exchange.NSE, "NIFTY"),
            _make_equity(Exchange.NSE, "RELIANCE"),
        ]
        assert build_fno_eligible_underlyings(instruments) == frozenset()

    def test_empty_iterable(self) -> None:
        assert build_fno_eligible_underlyings([]) == frozenset()


class TestIsInFetchScope:
    """The broad LAKE tier — everything we download."""

    _NO_EQUITIES: frozenset[tuple[Exchange, str]] = frozenset()

    def test_all_nse_indices_in_scope(self) -> None:
        # Even non-swept NSE indices (FINNIFTY etc.) are fetched.
        assert is_in_fetch_scope(_make_idx(Exchange.NSE, "FINNIFTY"), self._NO_EQUITIES) is True
        assert is_in_fetch_scope(_make_idx(Exchange.NSE, "NIFTY"), self._NO_EQUITIES) is True

    def test_bse_sensex_in_scope(self) -> None:
        assert is_in_fetch_scope(_make_idx(Exchange.BSE, "SENSEX"), self._NO_EQUITIES) is True

    def test_bse_non_sensex_index_rejected(self) -> None:
        assert is_in_fetch_scope(_make_idx(Exchange.BSE, "BSE500"), self._NO_EQUITIES) is False

    @pytest.mark.parametrize(
        "instrument_type",
        [InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE],
    )
    def test_nse_derivatives_in_scope(self, instrument_type: InstrumentType) -> None:
        deriv = _make_derivative(Exchange.NSE, instrument_type, "RELIANCE")
        assert is_in_fetch_scope(deriv, self._NO_EQUITIES) is True

    def test_bse_derivative_rejected(self) -> None:
        deriv = _make_derivative(Exchange.BSE, InstrumentType.FUT, "SENSEX")
        assert is_in_fetch_scope(deriv, self._NO_EQUITIES) is False

    def test_fno_eligible_equity_in_scope(self) -> None:
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        assert is_in_fetch_scope(_make_equity(Exchange.NSE, "RELIANCE"), eligible) is True

    def test_non_fno_equity_rejected(self) -> None:
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        # SOMEPENNY has no derivative → not eligible → rejected.
        assert is_in_fetch_scope(_make_equity(Exchange.NSE, "SOMEPENNY"), eligible) is False

    def test_bse_equity_rejected(self) -> None:
        eligible = frozenset({(Exchange.BSE, "RELIANCE")})
        assert is_in_fetch_scope(_make_equity(Exchange.BSE, "RELIANCE"), eligible) is False


class TestIsUniverseUnderlying:
    """Lean-ingest predicate (DEC-INSTRUMENT-UNIVERSE-FAST-001): persist only
    universe underlyings — admitted indices + F&O-eligible equities — never
    the derivative legs."""

    _NO_EQUITIES: frozenset[tuple[Exchange, str]] = frozenset()

    def test_all_nse_indices_admitted(self) -> None:
        assert (
            is_universe_underlying(_make_idx(Exchange.NSE, "FINNIFTY"), self._NO_EQUITIES) is True
        )
        assert is_universe_underlying(_make_idx(Exchange.NSE, "NIFTY"), self._NO_EQUITIES) is True

    def test_bse_sensex_admitted(self) -> None:
        assert is_universe_underlying(_make_idx(Exchange.BSE, "SENSEX"), self._NO_EQUITIES) is True

    def test_bse_non_sensex_index_rejected(self) -> None:
        assert is_universe_underlying(_make_idx(Exchange.BSE, "BSE500"), self._NO_EQUITIES) is False

    def test_fno_eligible_equity_admitted(self) -> None:
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        assert is_universe_underlying(_make_equity(Exchange.NSE, "RELIANCE"), eligible) is True

    def test_non_fno_equity_rejected(self) -> None:
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        assert is_universe_underlying(_make_equity(Exchange.NSE, "SOMEPENNY"), eligible) is False

    def test_bse_equity_rejected(self) -> None:
        eligible = frozenset({(Exchange.BSE, "RELIANCE")})
        assert is_universe_underlying(_make_equity(Exchange.BSE, "RELIANCE"), eligible) is False

    @pytest.mark.parametrize(
        "instrument_type",
        [InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE],
    )
    def test_every_derivative_leg_rejected(self, instrument_type: InstrumentType) -> None:
        # The whole point of lean ingest: legs are parsed for eligibility,
        # never persisted — even an NSE leg that IS in the broad fetch scope.
        deriv = _make_derivative(Exchange.NSE, instrument_type, "RELIANCE")
        assert is_universe_underlying(deriv, self._NO_EQUITIES) is False
        # ...and it is still in the broad fetch tier (the two filters differ).
        assert is_in_fetch_scope(deriv, self._NO_EQUITIES) is True
