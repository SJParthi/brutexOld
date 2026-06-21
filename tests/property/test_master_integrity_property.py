"""Property invariants for the instrument-master integrity audit (DEC-INSTRUMENT-MASTER-INTEGRITY-001).

Over randomly generated masters (random IDX/EQ spots + random FUT/CE/PE legs naming a
subset of those spots by symbol and/or token, plus ghosts and special chars):

* **Partition completeness** — every leg lands in EXACTLY one bucket, so the seven bucket
  counts sum to the leg total.
* **Green ⟺ no gating failure**.
* **Determinism** — row order does not change the verdict (`report_to_dict` is stable).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.data.master_integrity import audit_master_integrity, report_to_dict
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from hypothesis import given
from hypothesis import strategies as st

_NOW = datetime(2026, 6, 21, tzinfo=UTC)
_EXPIRY = date(2024, 1, 25)
_SYMS = st.text(alphabet="ABCDM&-", min_size=1, max_size=5)


def _spot(symbol: str, sid: int, numeric_token: bool) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
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
        feed_token=str(sid) if numeric_token else symbol,
    )


def _leg(sid: int, under_sym: str, under_sid: int | None) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=f"L{sid}",
        display_name=f"L{sid}",
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=under_sym,
        underlying_security_id=under_sid,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=_EXPIRY,
        strike_price=Decimal("100"),
        asm_gsm_flag=False,
    )


@st.composite
def _master(draw: st.DrawFn) -> list[Instrument]:
    spots = draw(
        st.lists(st.tuples(_SYMS, st.integers(1, 50), st.booleans()), min_size=0, max_size=6)
    )
    instruments: list[Instrument] = []
    used_sids: set[int] = set()
    for sym, sid, numeric in spots:
        if sid in used_sids:
            continue
        used_sids.add(sid)
        instruments.append(_spot(sym, sid, numeric))
    legs = draw(
        st.lists(
            st.tuples(_SYMS, st.one_of(st.none(), st.integers(1, 60))),
            min_size=0,
            max_size=8,
        )
    )
    for i, (under_sym, under_sid) in enumerate(legs):
        instruments.append(_leg(1000 + i, under_sym, under_sid))
    return instruments


@given(_master())
def test_buckets_partition_the_legs(instruments: list[Instrument]) -> None:
    rep = audit_master_integrity(instruments, now=_NOW)
    bucket_sum = (
        rep.resolved_both
        + rep.resolved_symbol_only
        + rep.resolved_token_only
        + rep.orphan_count
        + rep.ambiguous_count
        + rep.disagreement_count
        + rep.empty_underlying_count
    )
    assert bucket_sum == rep.type_counts.legs


@given(_master())
def test_green_iff_no_gating_failure(instruments: list[Instrument]) -> None:
    rep = audit_master_integrity(instruments, now=_NOW)
    assert rep.is_green == (rep.gating_failures == 0)


@given(_master())
def test_order_independent_verdict(instruments: list[Instrument]) -> None:
    forward = report_to_dict(audit_master_integrity(instruments, now=_NOW))
    backward = report_to_dict(audit_master_integrity(list(reversed(instruments)), now=_NOW))
    # The aggregate verdict + counts are order-independent (the orphan SAMPLE order may differ,
    # so compare the stable scalar fields).
    for field in (
        "is_green",
        "gating_failures",
        "resolved_both",
        "resolved_symbol_only",
        "resolved_token_only",
        "orphan_count",
        "ambiguous_count",
        "disagreement_count",
        "empty_underlying_count",
        "spots_with_no_legs",
    ):
        assert forward[field] == backward[field]
