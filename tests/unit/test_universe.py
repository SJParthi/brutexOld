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
    ENGINE_SWEEP_SPOT_TRIPLES,
    INDEX_UNDERLYINGS,
    REFERENCE_ONLY_INDEX_UNDERLYINGS,
    REFERENCE_ONLY_INDICES,
    V1_INDICES,
    V1_UNDERLYINGS,
    OptionUnderlyingGap,
    UnderlyingEconomics,
    UnderlyingSpec,
    build_fno_eligible_underlyings,
    build_options_eligible_underlyings,
    build_underlying_economics,
    classify_engine_sweep_target,
    classify_underlying_kind,
    derive_lake_underlyings,
    diagnose_unmatched_option_underlyings,
    is_engine_sweep_symbol,
    is_in_brute_force_scope,
    is_in_fetch_scope,
    is_in_scope,
    is_index_instrument,
    is_index_underlying,
    is_option_pullable,
    is_reference_only,
    is_spot_only_index,
    is_universe_underlying,
    normalize_underlying_symbol,
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
        """Display names match the Dhan instrument master conventions."""
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
        exch_id=exchange,
        security_id=hash(symbol) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _make_equity(exchange: Exchange, symbol: str) -> Instrument:
    """Build a cash-equity Instrument (out of scope for Track 1)."""
    return Instrument(
        exch_id=exchange,
        security_id=hash(symbol) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


# ────────────────────────────────────────────────────────────────────────
# is_index_instrument (DEC-COMPLETENESS-INDEX-CATEGORY-001)
# ────────────────────────────────────────────────────────────────────────


class TestIsIndexInstrument:
    """The by-TYPE index predicate — every ``IDX`` is an index, brute-forced or not."""

    def test_non_brute_forced_index_is_still_an_index(self) -> None:
        # FINNIFTY: a real NSE index that is NOT brute-forced. It must read as an index
        # (the bug fix — /completeness used to bucket it "equity (cash)").
        finnifty = _make_idx(Exchange.NSE, "FINNIFTY")
        assert is_index_instrument(finnifty) is True
        assert is_in_brute_force_scope(finnifty) is False  # not swept, but still an index

    def test_brute_forced_index_is_an_index(self) -> None:
        exch, sym = next(iter(BRUTE_FORCE_INDEX_UNDERLYINGS))
        assert is_index_instrument(_make_idx(exch, sym)) is True

    def test_reference_index_is_an_index_by_type(self) -> None:
        # India VIX is an index BY TYPE; the route classifies reference FIRST, so it still
        # displays REFERENCE — this predicate only tests the type.
        exch, sym = next(iter(REFERENCE_ONLY_INDEX_UNDERLYINGS))
        assert is_index_instrument(_make_idx(exch, sym)) is True

    def test_equity_is_not_an_index(self) -> None:
        assert is_index_instrument(_make_equity(Exchange.NSE, "RELIANCE")) is False


# ────────────────────────────────────────────────────────────────────────
# is_reference_only (DEC-NIFTY-TOTAL-MARKET-REFTAG-001)
# ────────────────────────────────────────────────────────────────────────


class TestIsReferenceOnly:
    def test_vix_is_reference_only(self) -> None:
        # India VIX (the only reference_only=True UnderlyingSpec today).
        exch, sym = next(iter(REFERENCE_ONLY_INDEX_UNDERLYINGS))
        vix = _make_idx(exch, sym)
        assert is_reference_only(vix) is True
        # Mutual exclusion with the engine tier (the locked invariant).
        assert is_in_brute_force_scope(vix) is False

    def test_brute_force_index_is_not_reference_only(self) -> None:
        exch, sym = next(iter(BRUTE_FORCE_INDEX_UNDERLYINGS))
        idx = _make_idx(exch, sym)
        assert is_reference_only(idx) is False
        assert is_in_brute_force_scope(idx) is True

    def test_equity_is_never_reference_only(self) -> None:
        # reference_only is an IDX-only tier today (it keys off the index set);
        # an equity named like VIX must not be mislabelled.
        exch, sym = next(iter(REFERENCE_ONLY_INDEX_UNDERLYINGS))
        assert is_reference_only(_make_equity(exch, sym)) is False

    def test_unknown_index_is_not_reference_only(self) -> None:
        assert is_reference_only(_make_idx(Exchange.NSE, "NIFTY IT")) is False


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
        exch_id=exchange,
        security_id=hash((underlying, instrument_type)) % 100_000,
        symbol_name=trading_symbol or f"{underlying}-DERIV",
        display_name=underlying,
        instrument=instrument_type,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=None,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=Decimal("28500") if is_option else None,
        asm_gsm_flag=False,
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


class TestBuildOptionsEligibleUnderlyings:
    """Derive the OPTIONS-only eligible set (CE/PE underlyings; FUT excluded)."""

    def test_collects_option_underlyings_only(self) -> None:
        # CE (index/stock call) + PE (put) qualify; FUT does NOT (options-only).
        instruments = [
            _make_derivative(Exchange.NSE, InstrumentType.CE, "NIFTY"),  # index option
            _make_derivative(Exchange.NSE, InstrumentType.PE, "RELIANCE"),  # stock option
            _make_derivative(Exchange.NSE, InstrumentType.FUT, "NIFTY100"),  # FUTURE → excluded
        ]
        eligible = build_options_eligible_underlyings(instruments)
        assert eligible == frozenset({(Exchange.NSE, "NIFTY"), (Exchange.NSE, "RELIANCE")})
        assert (Exchange.NSE, "NIFTY100") not in eligible  # a futures-only name is NOT eligible

    def test_skips_non_options(self) -> None:
        instruments = [
            _make_idx(Exchange.NSE, "NIFTY 100"),  # a spot index (no options)
            _make_equity(Exchange.NSE, "SOMESTOCK"),
            _make_derivative(Exchange.NSE, InstrumentType.FUT, "X"),  # future only
        ]
        assert build_options_eligible_underlyings(instruments) == frozenset()

    def test_empty_iterable(self) -> None:
        assert build_options_eligible_underlyings([]) == frozenset()


class TestIsSpotOnlyIndex:
    """The spot-only-index rule (DEC-NON-OPTION-INDEX-REFTAG-001).

    An ``IDX`` with no listed options that is not a brute-force index is
    "spot-only" → tagged reference_only at ingest → NOT option-pullable on the
    dashboard. The 3 brute-force indices and the options-bearing indices are
    NOT spot-only (they keep their pullable chain).
    """

    #: FINNIFTY has a listed option chain (a CE/PE leg names it) in this universe.
    _OPTIONS = frozenset({(Exchange.NSE, "FINNIFTY")})

    def test_non_option_sectoral_index_is_spot_only(self) -> None:
        # A sectoral / broad-market index with no option leg → spot-only.
        assert is_spot_only_index(_make_idx(Exchange.NSE, "NIFTYIT"), self._OPTIONS) is True
        assert is_spot_only_index(_make_idx(Exchange.NSE, "NIFTY100"), self._OPTIONS) is True

    def test_india_vix_is_spot_only(self) -> None:
        # INDIA VIX is a reference index with no options → spot-only too.
        assert is_spot_only_index(_make_idx(Exchange.NSE, "INDIAVIX"), self._OPTIONS) is True

    def test_options_bearing_index_is_not_spot_only(self) -> None:
        # FINNIFTY has options (in the eligible set) → NOT spot-only → keeps OPT.
        assert is_spot_only_index(_make_idx(Exchange.NSE, "FINNIFTY"), self._OPTIONS) is False

    def test_brute_force_index_is_never_spot_only(self) -> None:
        # The 3 brute-force indices are always individually tradable → never
        # spot-only, even SENSEX which has no NSE option leg (BSE options are
        # outside the NSE-only options set) — the brute-force guard wins first.
        for exch, sym in BRUTE_FORCE_INDEX_UNDERLYINGS:
            assert is_spot_only_index(_make_idx(exch, sym), self._OPTIONS) is False

    def test_equity_is_not_spot_only(self) -> None:
        # A non-IDX row (equity) is decided by F&O-eligibility, not this rule.
        assert is_spot_only_index(_make_equity(Exchange.NSE, "RELIANCE"), self._OPTIONS) is False


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

    def test_reference_security_id_admits_non_fno_equity(self) -> None:
        # DEC-NIFTY-TOTAL-MARKET-ADMIT-001: a non-F&O NSE equity is admitted
        # when its SECURITY_ID is in reference_security_ids (matched by id).
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        penny = _make_equity(Exchange.NSE, "SOMEPENNY")
        assert is_universe_underlying(penny, eligible) is False
        assert is_universe_underlying(penny, eligible, frozenset({penny.security_id})) is True

    def test_reference_set_does_not_admit_bse_equity(self) -> None:
        # Reference admission is NSE-only, same as the F&O equity clause.
        bse = _make_equity(Exchange.BSE, "SOMEPENNY")
        assert is_universe_underlying(bse, self._NO_EQUITIES, frozenset({bse.security_id})) is False

    def test_reference_set_does_not_admit_derivative(self) -> None:
        # A derivative leg is rejected even if its id is in the reference set.
        deriv = _make_derivative(Exchange.NSE, InstrumentType.FUT, "RELIANCE")
        assert (
            is_universe_underlying(deriv, self._NO_EQUITIES, frozenset({deriv.security_id}))
            is False
        )

    def test_token_join_security_id_admits_symbol_mismatched_fno_equity(self) -> None:
        # DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001: a symbol-mismatched F&O stock (its spot
        # symbol is NOT the leg's underlying_symbol, so the ticker key misses it) is
        # admitted when its SECURITY_ID is in the id-keyed token-join set.
        eligible = frozenset({(Exchange.NSE, "BAJAJAUTO")})  # the LEG spelling
        bajaj = _make_equity(Exchange.NSE, "BAJAJ-AUTO")  # the SPOT spelling — misses ticker key
        assert is_universe_underlying(bajaj, eligible) is False
        assert (
            is_universe_underlying(bajaj, eligible, frozenset(), frozenset({bajaj.security_id}))
            is True
        )

    def test_token_join_set_does_not_admit_bse_equity(self) -> None:
        # The token-join arm is NSE-only, same as the F&O + reference clauses.
        bse = _make_equity(Exchange.BSE, "BAJAJ-AUTO")
        assert (
            is_universe_underlying(
                bse, self._NO_EQUITIES, frozenset(), frozenset({bse.security_id})
            )
            is False
        )

    def test_token_join_set_does_not_admit_derivative(self) -> None:
        # A derivative leg is rejected even if its id is in the token-join set.
        deriv = _make_derivative(Exchange.NSE, InstrumentType.CE, "BAJAJAUTO")
        assert (
            is_universe_underlying(
                deriv, self._NO_EQUITIES, frozenset(), frozenset({deriv.security_id})
            )
            is False
        )

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


# ────────────────────────────────────────────────────────────────────────
# build_underlying_economics — leg-derived lot/tick/freeze backfill source
# (DEC-UNDERLYING-ECONOMICS-001). Lives here so the strict per-module 100%
# coverage gate (--cov=brutex.universe over tests/unit/test_universe.py)
# covers the helper.
# ────────────────────────────────────────────────────────────────────────


def _econ_idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """Build a minimal IDX row (blank economics, as the master ships)."""
    return Instrument(
        exch_id=exchange,
        security_id=0,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _econ_eq(symbol: str, *, lot_size: int = 1) -> Instrument:
    """Build a minimal EQ underlying row (blank economics, as the master ships)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin="INE000A01000",
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _econ_leg(
    symbol: str,
    underlying: str,
    itype: InstrumentType,
    *,
    lot_size: int,
    tick_size: Decimal,
    freeze_quantity: int | None,
    exchange: Exchange = Exchange.NSE,
    strike: Decimal | None = None,
) -> Instrument:
    """Build a derivative leg carrying explicit economics."""
    return Instrument(
        exch_id=exchange,
        security_id=2,
        symbol_name=symbol,
        display_name=symbol,
        instrument=itype,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=None,
        lot_size=lot_size,
        tick_size=tick_size,
        sm_freeze_qty=freeze_quantity,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=strike,
        asm_gsm_flag=False,
    )


def test_economics_harvests_from_legs() -> None:
    """Economics map carries each underlying's leg-derived lot/tick/freeze."""
    legs = [
        _econ_leg(
            "RELIANCE25JANFUT",
            "RELIANCE",
            InstrumentType.FUT,
            lot_size=500,
            tick_size=Decimal("0.05"),
            freeze_quantity=5000,
        ),
        _econ_leg(
            "NIFTY25JAN24000CE",
            "NIFTY",
            InstrumentType.CE,
            lot_size=75,
            tick_size=Decimal("0.05"),
            freeze_quantity=1800,
            strike=Decimal("24000"),
        ),
    ]
    econ = build_underlying_economics(legs)
    assert econ[(Exchange.NSE, "RELIANCE")] == (500, Decimal("0.05"), 5000)
    assert econ[(Exchange.NSE, "NIFTY")] == (75, Decimal("0.05"), 1800)


def test_economics_first_leg_wins() -> None:
    """The first leg seen for an underlying wins (deterministic, order-stable)."""
    legs = [
        _econ_leg(
            "RELIANCE25JANFUT",
            "RELIANCE",
            InstrumentType.FUT,
            lot_size=500,
            tick_size=Decimal("0.05"),
            freeze_quantity=5000,
        ),
        _econ_leg(
            "RELIANCE25JAN3000CE",
            "RELIANCE",
            InstrumentType.CE,
            lot_size=999,
            tick_size=Decimal("0.10"),
            freeze_quantity=1,
            strike=Decimal("3000"),
        ),
    ]
    econ = build_underlying_economics(legs)
    assert econ[(Exchange.NSE, "RELIANCE")] == (500, Decimal("0.05"), 5000)


def test_economics_ignores_non_derivatives() -> None:
    """IDX / EQ rows never contribute — only derivative legs do."""
    rows = [_econ_idx("NIFTY"), _econ_eq("RELIANCE", lot_size=1)]
    assert build_underlying_economics(rows) == {}


def test_economics_empty_input() -> None:
    """No instruments → empty economics map."""
    result: dict[tuple[Exchange, str], UnderlyingEconomics] = build_underlying_economics([])
    assert result == {}


def test_economics_skips_leg_without_underlying() -> None:
    """A malformed derivative with no underlying_symbol is skipped, not raised.

    ``Instrument.__post_init__`` forbids a real derivative with a None
    underlying, so we use a duck-typed fake exposing only the surface the helper
    reads. This exercises the ``underlying is None`` guard branch.
    """

    class _FakeLeg:
        underlying_symbol = None
        is_derivative = True
        exch_id = Exchange.NSE
        lot_size = 75
        tick_size = Decimal("0.05")
        sm_freeze_qty = 1800

    assert build_underlying_economics([_FakeLeg()]) == {}  # type: ignore[list-item]


# ────────────────────────────────────────────────────────────────────────
# derive_lake_underlyings (DEC-COVERAGE-LEDGER-001 Part A)
# ────────────────────────────────────────────────────────────────────────


def _make_idx_no_underlying(exchange: Exchange, symbol: str) -> Instrument:
    """Build an IDX row whose ``underlying_symbol`` is None (the real master shape)."""
    return Instrument(
        exch_id=exchange,
        security_id=hash(symbol) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class TestDeriveLakeUnderlyings:
    """The lake-pull scope fix: fan out over IDX + EQ, not just indices."""

    def test_includes_indices_and_stocks(self) -> None:
        instruments = [
            _make_idx(Exchange.NSE, "NIFTY"),
            _make_idx(Exchange.BSE, "SENSEX"),
            _make_equity(Exchange.NSE, "RELIANCE"),
            _make_equity(Exchange.NSE, "INFY"),
        ]
        result = derive_lake_underlyings(instruments)
        assert result == [
            ("INFY", "NSE"),
            ("NIFTY", "NSE"),
            ("RELIANCE", "NSE"),
            ("SENSEX", "BSE"),
        ]

    def test_uses_underlying_symbol_when_present(self) -> None:
        # _make_idx sets underlying_symbol == trading_symbol; pick that branch.
        result = derive_lake_underlyings([_make_idx(Exchange.NSE, "NIFTY")])
        assert result == [("NIFTY", "NSE")]

    def test_falls_back_to_trading_symbol_when_no_underlying(self) -> None:
        # underlying_symbol is None → uses trading_symbol.
        result = derive_lake_underlyings([_make_idx_no_underlying(Exchange.NSE, "BANKNIFTY")])
        assert result == [("BANKNIFTY", "NSE")]

    def test_dedup_and_sorted(self) -> None:
        instruments = [
            _make_idx(Exchange.NSE, "NIFTY"),
            _make_idx(Exchange.NSE, "NIFTY"),  # duplicate
            _make_equity(Exchange.NSE, "AAA"),
        ]
        result = derive_lake_underlyings(instruments)
        assert result == [("AAA", "NSE"), ("NIFTY", "NSE")]

    def test_empty(self) -> None:
        assert derive_lake_underlyings([]) == []


class TestIsOptionPullable:
    """The option-chain pull guard (DEC-OPTIONS-PULL-GUARD-001)."""

    def test_fno_eligible_stock_is_pullable(self) -> None:
        rel = _make_equity(Exchange.NSE, "RELIANCE")
        assert is_option_pullable(rel, reference_only_ids=frozenset()) is True

    def test_reference_only_index_is_not_pullable(self) -> None:
        # NIFTY Total Market: reference-only + not F&O → blocked.
        ntm = _make_idx(Exchange.NSE, "NIFTY TOTAL MKT")
        assert is_option_pullable(ntm, reference_only_ids=frozenset({ntm.security_id})) is False

    def test_reference_only_non_fno_stock_is_not_pullable(self) -> None:
        acme = _make_equity(Exchange.NSE, "ACME")
        assert is_option_pullable(acme, reference_only_ids=frozenset({acme.security_id})) is False

    def test_pullable_is_purely_not_reference_only(self) -> None:
        # The F&O-vs-reference decision is made at TAGGING time
        # (instrument_refresh._is_reference excludes F&O-eligible stocks), so an id
        # IN the reference set is not pullable here — a genuinely-F&O stock is never
        # in that set (it's tagged reference_only=FALSE), so it stays pullable.
        rel = _make_equity(Exchange.NSE, "RELIANCE")
        assert is_option_pullable(rel, reference_only_ids=frozenset({rel.security_id})) is False
        assert is_option_pullable(rel, reference_only_ids=frozenset()) is True

    def test_non_reference_index_stays_pullable(self) -> None:
        # A curated index (not reference-only) keeps its prior pullable behaviour.
        nifty = _make_idx(Exchange.NSE, "NIFTY")
        assert is_option_pullable(nifty, reference_only_ids=frozenset()) is True


# ────────────────────────────────────────────────────────────────────────
# Engine sweep-entry scope law (DEC-ENGINE-SCOPE-GUARD-001, Layer 0)
# ────────────────────────────────────────────────────────────────────────


class TestEngineSweepSpotTriples:
    """The derived spot-triple allowlist."""

    def test_triples_derive_from_brute_force_underlyings(self) -> None:
        expected = {(exch.value, "INDEX", sym) for exch, sym in BRUTE_FORCE_INDEX_UNDERLYINGS}
        assert frozenset(expected) == ENGINE_SWEEP_SPOT_TRIPLES

    def test_exactly_the_three_rule3_triples(self) -> None:
        assert (
            frozenset(
                {
                    ("NSE", "INDEX", "NIFTY"),
                    ("NSE", "INDEX", "BANKNIFTY"),
                    ("BSE", "INDEX", "SENSEX"),
                }
            )
            == ENGINE_SWEEP_SPOT_TRIPLES
        )


class TestIsEngineSweepSymbol:
    """The bare-symbol tier of the scope law (Layer-1's predicate)."""

    @pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "SENSEX"])
    def test_spot_indexes_allowed(self, symbol: str) -> None:
        assert is_engine_sweep_symbol(symbol) is True

    def test_series_alias_refused_spot_only(self) -> None:
        # FLIPPED by DEC-ENGINE-SPOT-ONLY-001 (was: allowed, the ADR-0012 /
        # DEC-FUTURES-ADDRESSING-PIN-001 admit): the operator's 2026-07-13
        # spot-only directive retires the ONE futures addressing shape from
        # engine sweep scope — the alias survives ONLY as lake tooling.
        assert is_engine_sweep_symbol("NSE-NIFTY-FUT") is False

    @pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "SENSEX"])
    def test_spot_only_truth_table_allows_exactly_the_three_indexes(self, symbol: str) -> None:
        # The DEC-ENGINE-SPOT-ONLY-001 truth table, positive arm.
        assert is_engine_sweep_symbol(symbol) is True

    @pytest.mark.parametrize(
        "symbol",
        [
            "NSE-NIFTY-FUT",  # the series alias — RETIRED from engine scope
            "NSE-NIFTY-30Sep25-FUT",  # a vendor per-contract futures symbol
            "NSE-NIFTY-30Jul26-24000-CE",  # an option
            "RELIANCE",  # an F&O stock
            "INDIAVIX",  # REFERENCE_ONLY
            "junk-🙂",  # arbitrary junk
        ],
    )
    def test_spot_only_truth_table_refuses_every_non_spot_instrument(self, symbol: str) -> None:
        # The DEC-ENGINE-SPOT-ONLY-001 truth table, negative arm.
        assert is_engine_sweep_symbol(symbol) is False

    def test_series_key_retired_shape_refused_with_hint(self) -> None:
        # FLIPPED by DEC-FUTURES-ADDRESSING-PIN-001 (was: allowed): the
        # pre-launch series_key form is refused naming the pinned alias —
        # and, since DEC-ENGINE-SPOT-ONLY-001, naming the track's own
        # engine-scope retirement (never a "did you mean" pointing at a
        # symbol that would itself be refused).
        assert is_engine_sweep_symbol("NIFTY-FUT-2026-07-30") is False
        verdict = classify_engine_sweep_target(
            exchange="NSE", segment="FNO", symbol="NIFTY-FUT-2026-07-30"
        )
        assert verdict.allowed is False
        assert verdict.tier == "refused"
        assert "RETIRED" in verdict.reason
        assert "'NSE-NIFTY-FUT'" in verdict.reason
        assert "DEC-ENGINE-SPOT-ONLY-001" in verdict.reason

    def test_lake_contract_retired_shape_refused_with_hint(self) -> None:
        # FLIPPED by DEC-FUTURES-ADDRESSING-PIN-001 (was: allowed): a
        # per-contract groww_symbol is a lake dir, never the engine address —
        # and the futures track itself is engine-retired
        # (DEC-ENGINE-SPOT-ONLY-001).
        assert is_engine_sweep_symbol("NSE-NIFTY-30Jul26-FUT") is False
        verdict = classify_engine_sweep_target(
            exchange="NSE", segment="FNO", symbol="NSE-NIFTY-30Jul26-FUT"
        )
        assert verdict.allowed is False
        assert verdict.tier == "refused"
        assert "RETIRED" in verdict.reason
        assert "'NSE-NIFTY-FUT'" in verdict.reason
        assert "DEC-ENGINE-SPOT-ONLY-001" in verdict.reason

    def test_alias_never_parses_as_a_contract_symbol(self) -> None:
        # Grammar collision-proofing: parse_fno_symbol can never absorb the
        # alias (3 hyphen tokens < the 4-token future minimum), so no vendor
        # contract symbol can ever collide with it.
        from brutex.data.fno_symbol import parse_fno_symbol

        assert parse_fno_symbol("NSE-NIFTY-FUT") is None

    def test_alias_import_identity_with_the_resolver(self) -> None:
        # brutex.data.futures_series consumes THIS module's constant — no
        # second literal exists to drift (DEC-FUTURES-ADDRESSING-PIN-001).
        from brutex import universe as universe_module
        from brutex.data import futures_series

        assert (
            futures_series.NIFTY_FUTURES_SERIES_SYMBOL
            is universe_module.NIFTY_FUTURES_SERIES_SYMBOL
        )
        assert futures_series.is_futures_series_symbol(universe_module.NIFTY_FUTURES_SERIES_SYMBOL)

    def test_series_key_regex_drift_pin_against_real_futures_contract(self) -> None:
        # Drift-pin (no universe→futures_roll import — that module pulls the
        # trading_core wheel), REGENERATED by DEC-FUTURES-ADDRESSING-PIN-001:
        # the RETIRED-shape hint regex MUST still fullmatch a REAL
        # FuturesContract(...).series_key so the did-you-mean hint can never
        # rot apart from the real retired shape — while the predicate now
        # REFUSES it (the alias is the one admitted form).
        from brutex.data.futures_roll import FuturesContract
        from brutex.options.lot_size_history import OptionsUnderlying
        from brutex.universe import _RETIRED_NIFTY_FUTURES_SERIES_RE

        contract = FuturesContract(underlying=OptionsUnderlying.NIFTY, expiry=date(2026, 7, 30))
        assert _RETIRED_NIFTY_FUTURES_SERIES_RE.fullmatch(contract.series_key) is not None
        assert is_engine_sweep_symbol(contract.series_key) is False
        verdict = classify_engine_sweep_target(
            exchange="NSE", segment="FNO", symbol=contract.series_key
        )
        assert verdict.allowed is False
        assert "RETIRED" in verdict.reason
        assert "NSE-NIFTY-FUT" in verdict.reason

    @pytest.mark.parametrize(
        "symbol",
        [
            "INDIAVIX",  # REFERENCE_ONLY — never swept
            "RELIANCE",  # F&O stock — lake-only
            "BANKNIFTY-FUT-2026-07-30",  # ADR-0012 authorizes NIFTY futures ONLY
            "SENSEX-FUT-2026-07-30",
            "NSE-BANKNIFTY-30Jul26-FUT",  # wrong underlying (lake shape)
            "BSE-NIFTY-30Jul26-FUT",  # wrong embedded exchange
            "NSE-NIFTY-30Jul26-24000-CE",  # an option is never engine scope
            "NSE-NIFTY-30Jul26-24000-PE",
            "nifty",  # lake keys are case-exact
            "NSE-NIFTY-FUT ",  # trailing space — never a fuzzy alias match
            "nse-nifty-fut",  # the alias is case-exact too
            "NSE-BANKNIFTY-FUT",  # alias-SHAPED but the wrong underlying
            "BSE-NIFTY-FUT",  # alias-shaped but the wrong exchange
            "",
        ],
    )
    def test_everything_else_refused(self, symbol: str) -> None:
        assert is_engine_sweep_symbol(symbol) is False


class TestClassifyEngineSweepTarget:
    """The coordinate tier of the scope law (Layer-2's classifier)."""

    @pytest.mark.parametrize(
        ("exchange", "symbol"),
        [("NSE", "NIFTY"), ("NSE", "BANKNIFTY"), ("BSE", "SENSEX")],
    )
    def test_spot_triples_allowed(self, exchange: str, symbol: str) -> None:
        verdict = classify_engine_sweep_target(exchange=exchange, segment="INDEX", symbol=symbol)
        assert verdict.allowed is True
        assert verdict.tier == "spot_index"
        assert "CLAUDE.md §2 Rule 3" in verdict.reason

    @pytest.mark.parametrize(
        ("exchange", "segment"),
        [("NSE", "FNO"), ("NSE", "INDEX"), ("BSE", "FNO"), ("BSE", "INDEX")],
    )
    def test_series_alias_refused_on_every_coordinate(self, exchange: str, segment: str) -> None:
        # FLIPPED by DEC-ENGINE-SPOT-ONLY-001 (was: allowed under NSE/FNO —
        # the ADR-0012 / DEC-FUTURES-ADDRESSING-PIN-001 admit): the alias is
        # refused on EVERY coordinate, incl. its own former NSE/FNO lake
        # coordinate, citing the superseding DEC + the remediation.
        verdict = classify_engine_sweep_target(
            exchange=exchange, segment=segment, symbol="NSE-NIFTY-FUT"
        )
        assert verdict.allowed is False
        assert verdict.tier == "refused"
        assert "DEC-ENGINE-SPOT-ONLY-001" in verdict.reason
        assert "spot-only" in verdict.reason
        assert "lake/REFERENCE_ONLY" in verdict.reason

    @pytest.mark.parametrize("symbol", ["NIFTY-FUT-2026-07-30", "NSE-NIFTY-30Jul26-FUT"])
    def test_retired_futures_shapes_refused_with_hint(self, symbol: str) -> None:
        # Hint-never-admit: BOTH retired pre-launch shapes point the operator
        # at the ONE pinned alias, on any coordinates.
        for exchange, segment in [("NSE", "FNO"), ("NSE", "INDEX")]:
            verdict = classify_engine_sweep_target(
                exchange=exchange, segment=segment, symbol=symbol
            )
            assert verdict.allowed is False
            assert verdict.tier == "refused"
            assert "RETIRED" in verdict.reason
            assert "'NSE-NIFTY-FUT'" in verdict.reason

    def test_spot_symbol_under_wrong_exchange_refused_with_expected_coords(self) -> None:
        verdict = classify_engine_sweep_target(exchange="NSE", segment="INDEX", symbol="SENSEX")
        assert verdict.allowed is False
        assert "BSE/INDEX" in verdict.reason

    def test_spot_symbol_under_wrong_segment_refused(self) -> None:
        verdict = classify_engine_sweep_target(exchange="NSE", segment="FNO", symbol="NIFTY")
        assert verdict.allowed is False
        assert "NSE/INDEX" in verdict.reason

    def test_wrong_coordinate_reasons_repr_quote_the_operator_pair(self) -> None:
        # Round-8 finding 2: --exchange/--segment are operator-typed and the
        # reason is echoed verbatim to stderr by the sweep mouths — an
        # embedded \n/ESC must never forge a '[brutex]' line (the #907/#919
        # repr-naming convention the round-4 symbol fix already follows).
        forged = "NSE\n[brutex] forged"
        # The alias refusal (DEC-ENGINE-SPOT-ONLY-001) no longer echoes the
        # coordinate pair at all — nothing operator-typed beyond the exact
        # known-constant symbol reaches the rendered reason, so a forged
        # exchange can never inject a line through it.
        alias = classify_engine_sweep_target(
            exchange=forged, segment="INDEX", symbol="NSE-NIFTY-FUT"
        )
        assert alias.allowed is False
        assert repr(forged) not in alias.reason
        assert "\n" not in alias.reason
        spot = classify_engine_sweep_target(
            exchange="NSE\x1b[31m", segment="INDEX", symbol="SENSEX"
        )
        assert spot.allowed is False
        assert repr("NSE\x1b[31m") in spot.reason
        assert "\x1b" not in spot.reason

    def test_india_vix_refused_with_reference_only_reason(self) -> None:
        verdict = classify_engine_sweep_target(exchange="NSE", segment="INDEX", symbol="INDIAVIX")
        assert verdict.allowed is False
        assert "REFERENCE_ONLY" in verdict.reason
        assert "CLAUDE.md §2 Rule 3" in verdict.reason

    def test_fno_stock_refused_with_generic_scope_reason(self) -> None:
        verdict = classify_engine_sweep_target(exchange="NSE", segment="INDEX", symbol="RELIANCE")
        assert verdict.allowed is False
        assert "outside the brute-force engine scope" in verdict.reason
        # The generic fall-through names the spot-only law
        # (DEC-ENGINE-SPOT-ONLY-001 message reword — the pre-flip "plus the
        # NIFTY-futures series alias" admit clause is GONE).
        assert "DEC-ENGINE-SPOT-ONLY-001" in verdict.reason
        assert "spot-only" in verdict.reason

    def test_hostile_symbol_is_repr_quoted_never_raw(self) -> None:
        # Round-4 finding 3: an operator-typed symbol embedding \n + ESC is
        # echoed into stderr/log refusal messages — repr-quoting (the
        # #907/#919 convention) keeps a forged '[brutex]' line / ANSI
        # injection out of the rendered reason.
        hostile = "EVIL\n[brutex] FORGED line \x1b[31m"
        verdict = classify_engine_sweep_target(exchange="NSE", segment="INDEX", symbol=hostile)
        assert verdict.allowed is False
        assert repr(hostile) in verdict.reason  # escaped, quoted form
        assert "\n" not in verdict.reason  # no RAW newline (repr renders '\\n')
        assert "\x1b" not in verdict.reason  # no RAW ESC byte

    @pytest.mark.parametrize(
        "symbol",
        [
            "BANKNIFTY-FUT-2026-07-30",
            "NSE-NIFTY-30Jul26-24000-CE",
            "NSE-BANKNIFTY-30Jul26-FUT",  # wrong underlying — NO retired hint
            "BSE-NIFTY-30Jul26-FUT",  # wrong embedded exchange — NO hint
            "NSE-BANKNIFTY-FUT",  # alias-shaped, wrong underlying — NO hint
            "BSE-NIFTY-FUT",  # alias-shaped, wrong exchange — NO hint
        ],
    )
    def test_non_nifty_future_and_options_refused_even_under_nse_fno(self, symbol: str) -> None:
        # Deny-by-default teeth: only the exact NIFTY retired shapes earn the
        # did-you-mean hint — every neighbour keeps the generic refusal.
        verdict = classify_engine_sweep_target(exchange="NSE", segment="FNO", symbol=symbol)
        assert verdict.allowed is False
        assert verdict.tier == "refused"
        assert "RETIRED" not in verdict.reason

    @pytest.mark.parametrize("symbol", ["nifty", "indiavix"])
    def test_lower_cased_known_symbol_gets_a_did_you_mean_hint(self, symbol: str) -> None:
        verdict = classify_engine_sweep_target(exchange="NSE", segment="INDEX", symbol=symbol)
        assert verdict.allowed is False
        assert f"did you mean {symbol.upper()!r}?" in verdict.reason

    def test_lower_cased_futures_alias_gets_the_case_hint(self) -> None:
        # Review round-2 finding 3: the docstring promises EVERY lower-cased
        # in-scope symbol a did-you-mean hint — the futures alias became
        # in-scope at launch, so `nse-nifty-fut` must earn the case pointer
        # (hint-never-admit), not the generic out-of-scope refusal.
        verdict = classify_engine_sweep_target(
            exchange="NSE", segment="FNO", symbol="nse-nifty-fut"
        )
        assert verdict.allowed is False
        assert verdict.tier == "refused"
        assert "did you mean 'NSE-NIFTY-FUT'?" in verdict.reason

    def test_lower_cased_unknown_symbol_gets_the_generic_refusal(self) -> None:
        verdict = classify_engine_sweep_target(exchange="NSE", segment="INDEX", symbol="reliance")
        assert verdict.allowed is False
        assert "did you mean" not in verdict.reason


class TestEngineScopeAgreesWithLakeSideTruth:
    """Cross-module drift pin (ported from DEC-CORP-ACTION-GATE-001).

    ``BRUTE_FORCE_INDEX_SYMBOLS`` (``adapters/groww/constants.py``) is the
    lake-side truth ``{"NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX"}``; the
    engine law derives its triples from the universe-side
    ``BRUTE_FORCE_INDEX_UNDERLYINGS``. The two are defined in SEPARATE
    modules (the engine imports nothing from ``brutex.adapters`` —
    layering), so this pin makes any drift between them a test failure.
    """

    def test_spot_triples_agree_with_the_groww_index_keys(self) -> None:
        from brutex.adapters.groww.constants import BRUTE_FORCE_INDEX_SYMBOLS

        derived = frozenset(
            (key.split("-", 1)[0], "INDEX", key.split("-", 1)[1])
            for key in BRUTE_FORCE_INDEX_SYMBOLS
        )
        assert derived == ENGINE_SWEEP_SPOT_TRIPLES


class TestClassifyEngineSweepTargetAdversarialCorpus:
    """Hard-coded homoglyph/encoding vectors, expectations hand-derived.

    Ported from DEC-CORP-ACTION-GATE-001 and re-derived against the
    classifier's OWN exact-case, deny-by-default semantics (no strip/upper
    normalization exists here — every vector must be REFUSED because it is
    not byte-equal to an allowed form; the ``upper()`` fold in the
    did-you-mean branch must only ever HINT, never admit).
    """

    @pytest.mark.parametrize(
        "symbol",
        [
            "NIFTY\u200b",  # trailing ZERO WIDTH SPACE
            "\u200bNIFTY",  # leading ZERO WIDTH SPACE
            "\u00a0NIFTY\u00a0",  # NBSP padding (no strip here — refused)
            "NIF TY",  # interior ASCII space
            "\uff2e\uff29\uff26\uff34\uff39",  # fullwidth NIFTY
            "B\u0410NKNIFTY",  # Cyrillic A homoglyph for Latin A
            "\u0130NDIAVIX",  # Turkish dotted capital I
        ],
    )
    def test_homoglyph_and_encoding_variants_refused(self, symbol: str) -> None:
        verdict = classify_engine_sweep_target(exchange="NSE", segment="INDEX", symbol=symbol)
        assert verdict.allowed is False
        assert verdict.tier == "refused"

    def test_turkish_dotless_i_gets_the_hint_but_stays_refused(self) -> None:
        # '\u0131'.upper() == "I" under Python's locale-independent casing,
        # so "n\u0131fty".upper() == "NIFTY" — the did-you-mean branch fires,
        # and the verdict MUST still be a refusal (hint, never admission).
        verdict = classify_engine_sweep_target(exchange="NSE", segment="INDEX", symbol="n\u0131fty")
        assert verdict.allowed is False
        assert "did you mean 'NIFTY'?" in verdict.reason


# ────────────────────────────────────────────────────────────────────────
# F&O-index symbol-mismatch diagnostic (DEC-FNO-INDEX-MATCH-DIAG-001)
# ────────────────────────────────────────────────────────────────────────
# Thin module-paired coverage of the pure diagnostic helpers; the loader
# wiring + the full four-shape scenario matrix live in
# tests/unit/test_fno_index_diagnostic.py (not duplicated here).


class TestNormalizeUnderlyingSymbol:
    """Uppercase + strip spaces/hyphens; never a per-index alias map."""

    def test_folds_case_spaces_and_hyphens(self) -> None:
        assert normalize_underlying_symbol("Nifty Next-50") == "NIFTYNEXT50"

    def test_genuine_abbreviation_does_not_fold(self) -> None:
        assert normalize_underlying_symbol("MIDCPNIFTY") != normalize_underlying_symbol(
            "NIFTY MIDCAP SELECT"
        )


class TestDiagnoseUnmatchedOptionUnderlyings:
    """The observe-only index-option underlying-gap scan (pure, zero I/O)."""

    def test_mixed_master_reports_only_index_like_gaps_sorted(self) -> None:
        # One master exercising every classification arm: an exact-matched
        # index (FINNIFTY, no gap), a case/space mismatch (NIFTY NEXT 50 →
        # nearest resolved by normalization), a genuine abbreviation
        # (MIDCPNIFTY → nearest None), a stock option (RELIANCE names an EQ
        # row, no gap), a FUT leg (not an option — contributes nothing), and
        # a non-NSE option leg (skipped).
        instruments = [
            _make_derivative(Exchange.NSE, InstrumentType.CE, "NIFTY NEXT 50"),
            _make_idx(Exchange.NSE, "NIFTYNEXT50"),
            _make_derivative(Exchange.NSE, InstrumentType.PE, "MIDCPNIFTY"),
            _make_idx(Exchange.NSE, "NIFTY MIDCAP SELECT"),
            _make_idx(Exchange.NSE, "FINNIFTY"),
            _make_derivative(Exchange.NSE, InstrumentType.CE, "FINNIFTY"),
            _make_equity(Exchange.NSE, "RELIANCE"),
            _make_derivative(Exchange.NSE, InstrumentType.CE, "RELIANCE"),
            _make_derivative(Exchange.NSE, InstrumentType.FUT, "NIFTY NEXT 50"),
            _make_derivative(Exchange.BSE, InstrumentType.CE, "SOMEIDX"),
        ]
        gaps = diagnose_unmatched_option_underlyings(instruments)
        assert gaps == [
            OptionUnderlyingGap(
                exchange=Exchange.NSE,
                underlying_symbol="MIDCPNIFTY",
                matched_index_symbol_name=None,
                nearest_index_symbol_name=None,
            ),
            OptionUnderlyingGap(
                exchange=Exchange.NSE,
                underlying_symbol="NIFTY NEXT 50",
                matched_index_symbol_name=None,
                nearest_index_symbol_name="NIFTYNEXT50",
            ),
        ]

    def test_alias_resolved_underlying_is_not_a_gap(self) -> None:
        # DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001: an underlying the
        # groww-symbol bridge already resolved to a spot IDX row is skipped.
        instruments = [_make_derivative(Exchange.NSE, InstrumentType.CE, "MIDCPNIFTY")]
        aliases = frozenset({(Exchange.NSE, "MIDCPNIFTY")})
        assert diagnose_unmatched_option_underlyings(instruments, aliases) == []

    def test_empty_master_yields_no_gaps(self) -> None:
        assert diagnose_unmatched_option_underlyings([]) == []
