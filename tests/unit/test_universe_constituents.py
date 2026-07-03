"""Unit tests for :mod:`brutex.universe_constituents` (constituent → SECURITY_ID).

DEC-NIFTY-TOTAL-MARKET-MAP-001 — Stage 2a. 100% line+branch on the pure
resolver: ISIN-primary / ticker-fallback, NSE-cash-equity filter, deterministic
lowest-security_id tiebreak, honest unmatched reporting, and the report
properties/summary.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.dashboard.nse_constituents import ConstituentRow, parse_constituents
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe_constituents import (
    ConstituentCoverageError,
    held_members,
    resolve_constituents,
    resolve_reference_security_ids,
)
from structlog.testing import capture_logs


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


class TestDuplicateKeyCollisionWarnings:
    """A duplicate ISIN / ticker collapse keeps the lowest-``security_id`` winner
    but is now LOUD, naming BOTH candidate rows (fail-before: the old code chose
    silently)."""

    def test_isin_collision_warns_naming_both_rows(self) -> None:
        isin = "INE002A01018"
        instruments = [
            _inst(2885, "RELIANCE INDUSTRIES LTD", isin),
            _inst(1000, "RELIANCE DUP", isin),  # lower id → kept (deterministic)
        ]
        with capture_logs() as logs:
            report = resolve_constituents([ConstituentRow("RELIANCE", isin)], instruments)
        assert report.security_ids == (1000,)  # deterministic choice unchanged
        warns = [
            e for e in logs if e.get("event") == "universe_constituents.duplicate_key_collapsed"
        ]
        assert len(warns) == 1  # only the ISIN collides (the two symbols differ)
        entry = warns[0]
        assert entry["log_level"] == "warning"
        assert entry["key_kind"] == "isin"
        assert entry["key"] == isin
        assert entry["kept_security_id"] == 1000
        assert entry["kept_symbol"] == "RELIANCE DUP"
        assert entry["dropped_security_id"] == 2885
        assert entry["dropped_symbol"] == "RELIANCE INDUSTRIES LTD"

    def test_ticker_collision_warns_naming_both_rows(self) -> None:
        instruments = [
            _inst(777, "ACME", None),
            _inst(222, "ACME", None),  # lower id → kept
        ]
        with capture_logs() as logs:
            report = resolve_constituents([ConstituentRow("ACME", None)], instruments)
        assert report.security_ids == (222,)
        warns = [
            e for e in logs if e.get("event") == "universe_constituents.duplicate_key_collapsed"
        ]
        assert len(warns) == 1  # no ISIN key (both None) → only the ticker collides
        entry = warns[0]
        assert entry["key_kind"] == "ticker"
        assert entry["key"] == "ACME"
        assert entry["kept_security_id"] == 222
        assert entry["dropped_security_id"] == 777

    def test_clean_master_emits_no_collision_warning(self) -> None:
        instruments = [
            _inst(1, "RELIANCE", "INE002A01018"),
            _inst(2, "TCS", "INE467B01029"),
        ]
        with capture_logs() as logs:
            resolve_constituents([ConstituentRow("RELIANCE", "INE002A01018")], instruments)
        assert [
            e for e in logs if e.get("event") == "universe_constituents.duplicate_key_collapsed"
        ] == []


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


class TestResolveReferenceSecurityIds:
    """DEC-NIFTY-TOTAL-MARKET-WIRE-001 — fail-loud id-set resolution."""

    def test_full_coverage_returns_id_set(self) -> None:
        instruments = [
            _inst(2885, "RELIANCE INDUSTRIES LTD", "INE002A01018"),
            _inst(11536, "TCS", "INE467B01029"),
        ]
        cons = [ConstituentRow("RELIANCE", "INE002A01018"), ConstituentRow("TCS", "INE467B01029")]
        assert resolve_reference_security_ids(cons, instruments) == frozenset({2885, 11536})

    def test_unmatched_raises_by_default(self) -> None:
        instruments = [_inst(2885, "RELIANCE INDUSTRIES LTD", "INE002A01018")]
        cons = [ConstituentRow("RELIANCE", "INE002A01018"), ConstituentRow("GHOST", "INE000Z01010")]
        with pytest.raises(ConstituentCoverageError, match="unmatched 1: \\[GHOST\\]"):
            resolve_reference_security_ids(cons, instruments)

    def test_unmatched_allowed_when_coverage_not_required(self) -> None:
        instruments = [_inst(2885, "RELIANCE INDUSTRIES LTD", "INE002A01018")]
        cons = [ConstituentRow("RELIANCE", "INE002A01018"), ConstituentRow("GHOST", "INE000Z01010")]
        ids = resolve_reference_security_ids(cons, instruments, require_full_coverage=False)
        assert ids == frozenset({2885})

    def test_empty_constituents_is_empty_set(self) -> None:
        assert resolve_reference_security_ids([], [_inst(1, "X LTD", "INE0X")]) == frozenset()


# ── held_members (DEC-INDEX-CONSTITUENTS-NSE-001) ─────────────────────────────


class TestHeldMembers:
    """The rename-proof ``/universe`` tick matcher — held symbols, ISIN→ticker."""

    def test_matches_held_by_ticker(self) -> None:
        # A constituent whose ticker IS a held stock → included by ticker.
        instruments = [_inst(1, "RELIANCE", "INE002A01018"), _inst(2, "TCS", "INE467B01029")]
        members = held_members(
            [ConstituentRow("RELIANCE", None)], instruments, held_symbols=["RELIANCE", "TCS"]
        )
        assert members == ["RELIANCE"]

    def test_matches_held_by_isin_when_ticker_renamed(self) -> None:
        # The rename-proof case: the constituent's ticker ("OLDNAME") matches no
        # held symbol, but its ISIN matches the held instrument (now "NEWNAME"),
        # so it is STILL ticked — by ISIN.
        instruments = [_inst(1, "NEWNAME", "INE002A01018")]
        members = held_members(
            [ConstituentRow("OLDNAME", "INE002A01018")], instruments, held_symbols=["NEWNAME"]
        )
        assert members == ["NEWNAME"]

    def test_matched_but_not_held_is_skipped(self) -> None:
        # A constituent that resolves to a real NSE equity we do NOT hold is dropped
        # (the False branch of the held filter) — never ticked.
        instruments = [_inst(1, "RELIANCE", "INE002A01018"), _inst(2, "ZEEL", "INE256A01028")]
        members = held_members(
            [ConstituentRow("ZEEL", "INE256A01028")], instruments, held_symbols=["RELIANCE"]
        )
        assert members == []

    def test_deduplicates_two_constituents_hitting_one_instrument(self) -> None:
        # Two constituent rows (one by ISIN, one by ticker) resolve to the SAME held
        # instrument → it appears at most once (the `name not in seen` branch).
        instruments = [_inst(1, "RELIANCE", "INE002A01018")]
        members = held_members(
            [ConstituentRow("RELIANCE", None), ConstituentRow("ALIAS", "INE002A01018")],
            instruments,
            held_symbols=["RELIANCE"],
        )
        assert members == ["RELIANCE"]

    def test_preserves_constituent_order(self) -> None:
        instruments = [
            _inst(1, "RELIANCE", "INE002A01018"),
            _inst(2, "TCS", "INE467B01029"),
            _inst(3, "INFY", "INE009A01021"),
        ]
        members = held_members(
            [ConstituentRow("INFY", None), ConstituentRow("RELIANCE", None)],
            instruments,
            held_symbols=["RELIANCE", "TCS", "INFY"],
        )
        assert members == ["INFY", "RELIANCE"]

    def test_empty_constituents_yields_empty(self) -> None:
        assert (
            held_members([], [_inst(1, "RELIANCE", "INE002A01018")], held_symbols=["RELIANCE"])
            == []
        )

    def test_cross_segment_security_id_collision_returns_correct_name(self) -> None:
        # A cash EQUITY and an INDEX share the numeric security_id 13 (the observed
        # cross-segment collision). The name lookup must return the CASH-equity name
        # (the partition every match resolves from), NOT the index that happened to
        # last-write id 13 in an un-partitioned map. Fail-before: the old
        # {inst.security_id: inst.symbol_name for inst in instruments} last-wins to
        # "NIFTY" (id 13 not held → dropped), returning [] instead of ["RELIANCE"].
        cash_reliance = _inst(13, "RELIANCE", "INE002A01018")
        index_collider = _inst(
            13,
            "NIFTY",
            None,
            segment=Segment.INDEX,
            instrument=InstrumentType.IDX,
        )
        members = held_members(
            [ConstituentRow("RELIANCE", "INE002A01018")],
            # index row AFTER the cash row → old un-partitioned map would clobber id 13.
            [cash_reliance, index_collider],
            held_symbols=["RELIANCE"],
        )
        assert members == ["RELIANCE"]
