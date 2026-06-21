"""Property invariants for the economics-integrity audit (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001).

Over randomly generated leg sets: green ⟺ zero within-expiry lot mismatch; the mismatch count
never exceeds the number of (underlying, expiry) groups; and the verdict is order-independent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.data.economics_integrity import audit_economics_integrity, report_to_dict
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from hypothesis import given
from hypothesis import strategies as st

_NOW = datetime(2026, 6, 21, tzinfo=UTC)
_EXPIRIES = (date(2026, 6, 25), date(2026, 7, 30), date(2026, 8, 27))


def _leg(sid: int, under: str, expiry: date, lot: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=f"L{sid}",
        display_name=f"L{sid}",
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=under,
        underlying_security_id=100,
        lot_size=lot,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=lot * 10,
        sm_expiry_date=expiry,
        strike_price=Decimal("100"),
        asm_gsm_flag=False,
    )


@st.composite
def _legs(draw: st.DrawFn) -> list[Instrument]:
    rows = draw(
        st.lists(
            st.tuples(
                st.sampled_from(["AAA", "BBB", "CCC"]),
                st.sampled_from(_EXPIRIES),
                st.sampled_from([50, 75, 500, 750]),
            ),
            min_size=0,
            max_size=10,
        )
    )
    return [_leg(1000 + i, u, e, lot) for i, (u, e, lot) in enumerate(rows)]


@given(_legs())
def test_green_iff_no_within_expiry_mismatch(legs: list[Instrument]) -> None:
    rep = audit_economics_integrity(legs, now=_NOW)
    assert rep.is_green == (rep.lot_mismatch_expiries == 0)
    assert rep.lot_mismatch_expiries <= rep.expiry_groups


@given(_legs())
def test_order_independent(legs: list[Instrument]) -> None:
    forward = report_to_dict(audit_economics_integrity(legs, now=_NOW))
    backward = report_to_dict(audit_economics_integrity(list(reversed(legs)), now=_NOW))
    for field in (
        "is_green",
        "lot_mismatch_expiries",
        "expiry_groups",
        "total_legs",
        "underlyings_with_lot_revisions",
    ):
        assert forward[field] == backward[field]
