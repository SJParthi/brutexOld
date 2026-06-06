"""Unit tests for :mod:`brutex.universe_constituents` (constituent → SECURITY_ID).

DEC-NIFTY-TOTAL-MARKET-MAP-001 — Stage 2a. 100% line+branch on the pure
resolver: ISIN-primary / ticker-fallback, NSE-cash-equity filter, deterministic
lowest-security_id tiebreak, honest unmatched reporting, and the report
properties/summary.
"""

from __future__ import annotations

from decimal import Decimal

from brutex.dashboard.nse_constituents import ConstituentRow, parse_constituents
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe_constituents import resolve_constituents


def _inst(
    security_id: int,
    symbol_name: str,
    isin: str | None,
    *,
    exch: Exchange = Exchange.NSE,
    segment: Segment = Segment.CASH,
    instrument: InstrumentType = InstrumentType.EQ,
) -> Instrument:
    """Build a minimal Instrument for the resolver tests (cash-EQ by default)."""
    return Instrument(
        exch_id=exch,
        security_id=security_id,
        symbol_name=symbol_name,
        display_name=None,
        instrument=instrument,
        segment=segment,
        series="EQ",
        isin=isin,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


class TestResolveConstituents:
    def test_isin_primary_match(self) -> None:
        instruments = [_inst(2885, "RELIANCE INDUSTRIES LTD", "INE002A01018")]
        report = resolve_constituents([ConstituentRow("RELIANCE", "INE002A01018")], instruments)
        assert report.security_ids == (2885,)
        assert report.matched[0].matched_by == "isin"
        assert report.matched_by_isin == 1
        assert report.matched_by_ticker == 0
        assert report.unmatched == ()

    def test_ticker_fallback_when_isin_absent_from_dhan(self) -> None:
        # Constituent has an ISIN, but Dhan has no row with it; the ticker
        # (an F&O name whose symbol_name IS the ticker) still resolves it.
        instruments = [_inst(1330, "HDFCBANK", "INE040A01034")]
        report = resolve_constituents([ConstituentRow("HDFCBANK", "INE999X09999")], instruments)
        assert report.security_ids == (1330,)
        assert report.matched[0].matched_by == "ticker"
        assert report.matched_by_ticker == 1

    def test_ticker_fallback_when_constituent_has_no_isin(self) -> None:
        instruments = [_inst(11536, "TCS", None)]
        report = resolve_constituents([ConstituentRow("TCS", None)], instruments)
        assert report.security_ids == (11536,)
        assert report.matched[0].matched_by == "ticker"

    def test_non_fno_equity_long_name_only_resolves_by_isin(self) -> None:
        # The crux: a non-F&O equity's symbol_name is the LONG company name,
        # so the ticker can't match — ISIN is the only working key.
        instruments = [_inst(5097, "SOME SMALLCAP LIMITED", "INE111A01011")]
        by_isin = resolve_constituents([ConstituentRow("SMALLCAP", "INE111A01011")], instruments)
        assert by_isin.security_ids == (5097,)
        assert by_isin.matched[0].matched_by == "isin"
        # Without the ISIN, the same name is unmatched (ticker != long name).
        no_isin = resolve_constituents([ConstituentRow("SMALLCAP", None)], instruments)
        assert no_isin.security_ids == ()
        assert [c.symbol for c in no_isin.unmatched] == ["SMALLCAP"]

    def test_unmatched_is_reported_never_fabricated(self) -> None:
        report = resolve_constituents([ConstituentRow("GHOST", "INE000Z01010")], [])
        assert report.matched == ()
        assert [c.symbol for c in report.unmatched] == ["GHOST"]
        assert report.total == 1

    def test_bse_and_fno_and_index_rows_are_ignored(self) -> None:
        # Same ISIN on a BSE-EQ, an NSE-FNO, and an NSE-IDX row must NOT match;
        # only the NSE cash-EQ row counts.
        # Same ISIN on a BSE cash row (wrong exch), an NSE-FNO row (wrong
        # segment), and an NSE-IDX row (wrong instrument) — each fails one of
        # the three filter sub-conditions, so none match.
        isin = "INE002A01018"
        instruments = [
            _inst(500325, "RELIANCE", isin, exch=Exchange.BSE),
            _inst(99999, "RELIANCE", isin, segment=Segment.FNO),
            _inst(88888, "RELIANCE", isin, instrument=InstrumentType.IDX),
        ]
        report = resolve_constituents([ConstituentRow("RELIANCE", isin)], instruments)
        assert report.security_ids == ()
        assert report.unmatched[0].symbol == "RELIANCE"

    def test_one_isin_multiple_rows_lowest_security_id_wins(self) -> None:
        isin = "INE002A01018"
        instruments = [
            _inst(2885, "RELIANCE INDUSTRIES LTD", isin),
            _inst(1000, "RELIANCE DUP", isin),  # lower id → should win
        ]
        # Order-independence: feed both orders, same result.
        a = resolve_constituents([ConstituentRow("RELIANCE", isin)], instruments)
        b = resolve_constituents([ConstituentRow("RELIANCE", isin)], list(reversed(instruments)))
        assert a.security_ids == (1000,)
        assert b.security_ids == (1000,)

    def test_ticker_collision_lowest_security_id_wins(self) -> None:
        instruments = [
            _inst(777, "ACME", None),
            _inst(222, "ACME", None),  # lower id → wins
        ]
        # Both orders → same winner (covers the replace AND keep arcs).
        a = resolve_constituents([ConstituentRow("ACME", None)], instruments)
        b = resolve_constituents([ConstituentRow("ACME", None)], list(reversed(instruments)))
        assert a.security_ids == (222,)
        assert b.security_ids == (222,)

    def test_case_and_whitespace_insensitive(self) -> None:
        instruments = [_inst(2885, "RELIANCE INDUSTRIES LTD", "INE002A01018")]
        report = resolve_constituents([ConstituentRow("reliance", "  ine002a01018 ")], instruments)
        # ConstituentRow stores raw here; the resolver normalises on lookup.
        assert report.security_ids == (2885,)

    def test_instrument_with_blank_symbol_name_skipped_for_ticker_index(self) -> None:
        # A cash-EQ row with an empty symbol_name must not create a "" ticker key.
        instruments = [_inst(1, "", "INE002A01018")]
        report = resolve_constituents([ConstituentRow("", None)], instruments)
        assert report.security_ids == ()
        assert report.unmatched[0].symbol == ""

    def test_order_preserved_by_constituent_order(self) -> None:
        instruments = [
            _inst(10, "A LTD", "INE0A"),
            _inst(20, "B LTD", "INE0B"),
            _inst(30, "C LTD", "INE0C"),
        ]
        cons = [
            ConstituentRow("C", "INE0C"),
            ConstituentRow("A", "INE0A"),
            ConstituentRow("B", "INE0B"),
        ]
        report = resolve_constituents(cons, instruments)
        assert report.security_ids == (30, 10, 20)


class TestMatchReport:
    def test_summary_all_matched(self) -> None:
        instruments = [_inst(2885, "RELIANCE INDUSTRIES LTD", "INE002A01018")]
        report = resolve_constituents([ConstituentRow("RELIANCE", "INE002A01018")], instruments)
        assert report.summary() == "matched 1/1 (1 by ISIN, 0 by ticker)"

    def test_summary_with_unmatched_lists_them(self) -> None:
        instruments = [_inst(2885, "RELIANCE INDUSTRIES LTD", "INE002A01018")]
        cons = [ConstituentRow("RELIANCE", "INE002A01018"), ConstituentRow("GHOST", "INE000Z01010")]
        report = resolve_constituents(cons, instruments)
        assert report.summary() == "matched 1/2 (1 by ISIN, 0 by ticker); unmatched 1: [GHOST]"


class TestParseConstituentsIntegration:
    _CSV = (
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Refineries,RELIANCE,EQ,INE002A01018\n"
        "Tata Consultancy Services Ltd.,IT,TCS,EQ,INE467B01029\n"
        "Mahindra & Mahindra Ltd.,Auto,M&M,EQ,INE101A01026\n"
    )

    def test_parse_then_resolve_end_to_end(self) -> None:
        constituents = parse_constituents(self._CSV)
        assert [c.symbol for c in constituents] == ["RELIANCE", "TCS", "M&M"]
        assert constituents[0].isin == "INE002A01018"
        instruments = [
            _inst(2885, "RELIANCE INDUSTRIES LTD", "INE002A01018"),
            _inst(11536, "TCS", "INE467B01029"),
            # M&M intentionally absent from Dhan → unmatched.
        ]
        report = resolve_constituents(constituents, instruments)
        assert report.security_ids == (2885, 11536)
        assert [c.symbol for c in report.unmatched] == ["M&M"]
        assert report.summary() == "matched 2/3 (2 by ISIN, 0 by ticker); unmatched 1: [M&M]"
