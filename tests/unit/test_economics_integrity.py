"""Unit tests for the instrument-master economics integrity audit (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001).

Pure, offline, over hand-built `Instrument` legs. 100% line + branch over
`audit_economics_integrity` + `report_to_dict` + `report_from_dict`, covering the gated
within-expiry lot mismatch, the informational cross-expiry revision, test-leg exclusion,
and the healthy path. lot/tick positivity is guaranteed upstream by the model, so it is not
re-checked here (no dead code).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.data.economics_integrity import (
    SAMPLE_CAP,
    audit_economics_integrity,
    report_from_dict,
    report_to_dict,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_NOW = datetime(2026, 6, 21, tzinfo=UTC)
_E1 = date(2026, 6, 25)
_E2 = date(2026, 7, 30)


def _leg(
    symbol: str,
    *,
    sid: int,
    under: str,
    expiry: date,
    lot: int,
    kind: InstrumentType = InstrumentType.CE,
    tick: str = "0.05",
) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=symbol,
        display_name=symbol,
        instrument=kind,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=under,
        underlying_security_id=100,
        lot_size=lot,
        tick_size=Decimal(tick),
        sm_freeze_qty=lot * 10,
        sm_expiry_date=expiry,
        strike_price=Decimal("100") if kind is not InstrumentType.FUT else None,
        asm_gsm_flag=False,
    )


def test_uniform_lot_per_expiry_is_green() -> None:
    rep = audit_economics_integrity(
        [
            _leg("RCE1", sid=1, under="RELIANCE", expiry=_E1, lot=500),
            _leg("RPE1", sid=2, under="RELIANCE", expiry=_E1, lot=500, kind=InstrumentType.PE),
            _leg("RFUT", sid=3, under="RELIANCE", expiry=_E1, lot=500, kind=InstrumentType.FUT),
        ],
        now=_NOW,
    )
    assert rep.is_green
    assert rep.total_legs == 3
    assert rep.expiry_groups == 1
    assert rep.lot_mismatch_expiries == 0
    assert rep.underlyings_with_lot_revisions == 0


def test_lot_mismatch_within_expiry_is_red() -> None:
    rep = audit_economics_integrity(
        [
            _leg("RCE1", sid=1, under="RELIANCE", expiry=_E1, lot=500),
            _leg(
                "RCE2", sid=2, under="RELIANCE", expiry=_E1, lot=750
            ),  # different lot, SAME expiry
        ],
        now=_NOW,
    )
    assert not rep.is_green
    assert rep.lot_mismatch_expiries == 1
    m = rep.mismatches[0]
    assert m.underlying == "RELIANCE" and m.expiry == _E1.isoformat() and m.lots == (500, 750)


def test_cross_expiry_revision_is_informational_not_gating() -> None:
    # Lot changes BETWEEN expiries (legit NSE revision) — green, but reported as a revision.
    rep = audit_economics_integrity(
        [
            _leg("RCE1", sid=1, under="AMBUJACEM", expiry=_E1, lot=1050),
            _leg("RCE2", sid=2, under="AMBUJACEM", expiry=_E2, lot=1200),
        ],
        now=_NOW,
    )
    assert rep.is_green  # cross-expiry revision does NOT gate
    assert rep.lot_mismatch_expiries == 0
    assert rep.underlyings_with_lot_revisions == 1
    assert rep.lot_revisions[0].underlying == "AMBUJACEM"
    assert rep.lot_revisions[0].lots == (1050, 1200)


def test_test_legs_excluded() -> None:
    rep = audit_economics_integrity(
        [
            _leg("T1", sid=1, under="031NSETEST", expiry=_E1, lot=500, kind=InstrumentType.FUT),
            _leg("T2", sid=2, under="031NSETEST", expiry=_E1, lot=999, kind=InstrumentType.FUT),
        ],
        now=_NOW,
    )
    assert rep.total_legs == 0  # both excluded → no mismatch despite differing lots
    assert rep.is_green


def test_empty_underlying_leg_skipped() -> None:
    rep = audit_economics_integrity(
        [_leg("F", sid=1, under="", expiry=_E1, lot=500, kind=InstrumentType.FUT)],
        now=_NOW,
    )
    assert rep.total_legs == 0 and rep.is_green


def test_non_leg_rows_ignored() -> None:
    spot = Instrument(
        exch_id=Exchange.NSE,
        security_id=100,
        symbol_name="RELIANCE",
        display_name="RELIANCE",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
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
    rep = audit_economics_integrity([spot], now=_NOW)
    assert rep.total_legs == 0 and rep.expiry_groups == 0 and rep.is_green


def test_sample_caps() -> None:
    # More than SAMPLE_CAP mismatching expiries → mismatches sample capped, count exact.
    legs = []
    n = SAMPLE_CAP + 5
    for i in range(n):
        # distinct underlying per group so each is its own (under, expiry) with 2 lots
        legs.append(_leg(f"A{i}", sid=2 * i, under=f"U{i}", expiry=_E1, lot=500))
        legs.append(_leg(f"B{i}", sid=2 * i + 1, under=f"U{i}", expiry=_E1, lot=750))
    rep = audit_economics_integrity(legs, now=_NOW)
    assert rep.lot_mismatch_expiries == n  # exact
    assert len(rep.mismatches) == SAMPLE_CAP  # capped


def test_round_trip() -> None:
    rep = audit_economics_integrity(
        [
            _leg("RCE1", sid=1, under="RELIANCE", expiry=_E1, lot=500),
            _leg("RCE2", sid=2, under="RELIANCE", expiry=_E1, lot=750),
            _leg("ACE1", sid=3, under="AMBUJACEM", expiry=_E1, lot=1050),
            _leg("ACE2", sid=4, under="AMBUJACEM", expiry=_E2, lot=1200),
        ],
        now=_NOW,
        run_id="r1",
        csv_source_sha256="abc",
    )
    assert report_from_dict(report_to_dict(rep)) == rep
