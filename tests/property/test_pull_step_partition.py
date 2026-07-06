"""Hypothesis properties for the six-step pull-order vocabulary.

DEC-PULL-SIX-STEP-ORDER-001 — P1 (spot partition totality): for arbitrary
instrument mixes, :func:`brutex.adapters.ingest_common.partition_spot_steps`
places every instrument in EXACTLY one group, the multiset union equals the
input, group order is (INDICES_SPOT, STOCKS_SPOT) with empty groups omitted,
within-group input order is preserved, and the partition is deterministic.
"""

from __future__ import annotations

from decimal import Decimal

from brutex.adapters.ingest_common import PullStep, partition_spot_steps
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from hypothesis import given
from hypothesis import strategies as st


def _instrument(symbol: str, kind: InstrumentType, security_id: int) -> Instrument:
    """Build a minimal spot instrument of the given *kind*."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=None,
        instrument=kind,
        segment=Segment.INDEX if kind == InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if kind == InstrumentType.IDX else 500,
        tick_size=Decimal("0") if kind == InstrumentType.IDX else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


_instruments = st.lists(
    st.builds(
        _instrument,
        symbol=st.text(
            alphabet=st.characters(min_codepoint=65, max_codepoint=90), min_size=1, max_size=8
        ),
        kind=st.sampled_from([InstrumentType.IDX, InstrumentType.EQ]),
        security_id=st.integers(min_value=1, max_value=10_000),
    ),
    max_size=40,
)


@given(_instruments)
def test_every_instrument_in_exactly_one_group(instruments: list[Instrument]) -> None:
    """P1 totality: the flattened groups are a permutation-free copy of the input."""
    groups = partition_spot_steps(instruments)
    flattened = [inst for _step, members in groups for inst in members]
    assert len(flattened) == len(instruments)
    # Multiset union == input (identity-level: the SAME objects, no copies).
    assert sorted(map(id, flattened)) == sorted(map(id, instruments))


@given(_instruments)
def test_group_order_and_emptiness(instruments: list[Instrument]) -> None:
    """Groups appear in strict (1, 2) rank order, are non-empty, at most one each."""
    groups = partition_spot_steps(instruments)
    steps = [step for step, _members in groups]
    assert steps == sorted(steps)
    assert len(set(steps)) == len(steps)
    assert set(steps) <= {PullStep.INDICES_SPOT, PullStep.STOCKS_SPOT}
    for _step, members in groups:
        assert members  # an empty group is OMITTED, never emitted


@given(_instruments)
def test_classification_is_total_and_correct(instruments: list[Instrument]) -> None:
    """IDX ⇒ INDICES_SPOT; everything else ⇒ STOCKS_SPOT."""
    groups = dict(partition_spot_steps(instruments))
    for inst in instruments:
        expected = PullStep.INDICES_SPOT if inst.is_index else PullStep.STOCKS_SPOT
        assert inst in groups[expected]


@given(_instruments)
def test_within_group_order_preserved(instruments: list[Instrument]) -> None:
    """Stability: each group preserves the input's relative order."""
    groups = dict(partition_spot_steps(instruments))
    # Identity-keyed positions: st.builds yields a fresh object per draw, so
    # id() is a collision-free position key even for value-equal duplicates.
    pos_by_id = {id(inst): pos for pos, inst in enumerate(instruments)}
    for members in groups.values():
        positions = [pos_by_id[id(inst)] for inst in members]
        assert positions == sorted(positions)


@given(_instruments)
def test_deterministic(instruments: list[Instrument]) -> None:
    """The partition is a pure function — identical calls, identical output."""
    assert partition_spot_steps(instruments) == partition_spot_steps(instruments)
