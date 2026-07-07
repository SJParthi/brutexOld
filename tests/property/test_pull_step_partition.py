# pyright: reportPrivateUsage=none
"""Hypothesis properties for the six-step pull-order vocabulary.

DEC-PULL-SIX-STEP-ORDER-001 — three property families:

* **P1 (spot partition totality):** for arbitrary instrument mixes,
  :func:`brutex.adapters.ingest_common.partition_spot_steps` places every
  instrument in EXACTLY one group, the multiset union equals the input, group
  order is (INDICES_SPOT, STOCKS_SPOT) with empty groups omitted, within-group
  input order is preserved, and the partition is deterministic.
* **P2 (F&O bucket sort totality):** ``historical_pull._bucket_sort_key`` is a
  total, deterministic order for ANY ``(market, bool)`` set — every FUT key
  before every OPT key, index before stock within a market, unknown markets
  LAST with stable string tie-breaks.
* **P3 (contract classification totality):** ``historical_pull._contract_bucket``
  places every row in exactly one bucket (union == input); an unknown
  underlying classifies stock-side (never dropped, never raised).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
)
from brutex.adapters.ingest_common import PullStep, partition_spot_steps
from brutex.dashboard import historical_pull as hp
from brutex.data.registry_store import ContractRow
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


# ── P2 — the F&O 4-key bucket sort is a TOTAL, deterministic order ────────────

_KNOWN_MARKETS = (GROWW_FUTURE_COVERAGE_MARKET, GROWW_OPTION_COVERAGE_MARKET)

_buckets = st.lists(
    st.tuples(
        st.one_of(
            st.sampled_from(_KNOWN_MARKETS),
            st.text(
                alphabet=st.characters(min_codepoint=97, max_codepoint=122),
                min_size=1,
                max_size=12,
            ),
        ),
        st.booleans(),
    ),
    max_size=30,
    unique=True,
)


@given(_buckets)
def test_bucket_sort_is_total_and_deterministic(buckets: list[tuple[str, bool]]) -> None:
    """Sorting any bucket set never raises, is stable, and is permutation-invariant."""
    ordered = sorted(buckets, key=hp._bucket_sort_key)
    assert sorted(ordered, key=hp._bucket_sort_key) == ordered
    rotated = buckets[1:] + buckets[:1]  # a permutation — the sort must agree
    assert sorted(rotated, key=hp._bucket_sort_key) == ordered
    assert len(ordered) == len(buckets)


@given(_buckets)
def test_bucket_sort_futures_before_options_unknown_last(
    buckets: list[tuple[str, bool]],
) -> None:
    """Every FUT key < every OPT key; index < stock per market; unknowns LAST."""
    ordered = sorted(buckets, key=hp._bucket_sort_key)
    ranks = [hp._bucket_sort_key(b)[0] for b in ordered]
    assert ranks == sorted(ranks)
    for market, is_index in ordered:
        if market == GROWW_FUTURE_COVERAGE_MARKET:
            expected = PullStep.INDICES_FUTURES if is_index else PullStep.STOCKS_FUTURES
            assert hp._bucket_sort_key((market, is_index))[0] == int(expected)
        elif market == GROWW_OPTION_COVERAGE_MARKET:
            expected = PullStep.INDICES_OPTIONS if is_index else PullStep.STOCKS_OPTIONS
            assert hp._bucket_sort_key((market, is_index))[0] == int(expected)
        else:
            assert hp._bucket_sort_key((market, is_index))[0] == 99
    fut_positions = [i for i, (m, _f) in enumerate(ordered) if m == GROWW_FUTURE_COVERAGE_MARKET]
    opt_positions = [i for i, (m, _f) in enumerate(ordered) if m == GROWW_OPTION_COVERAGE_MARKET]
    unknown_positions = [i for i, (m, _f) in enumerate(ordered) if m not in _KNOWN_MARKETS]
    if fut_positions and opt_positions:
        assert max(fut_positions) < min(opt_positions)
    if opt_positions and unknown_positions:
        assert max(opt_positions) < min(unknown_positions)
    if fut_positions and unknown_positions:
        assert max(fut_positions) < min(unknown_positions)
    # Index before stock WITHIN each known market.
    for market in _KNOWN_MARKETS:
        idx = [i for i, b in enumerate(ordered) if b == (market, True)]
        stk = [i for i, b in enumerate(ordered) if b == (market, False)]
        if idx and stk:
            assert idx[0] < stk[0]


# ── P3 — the contract classification is TOTAL (every row in exactly one bucket) ──


def _contract_row(underlying: str, side: str) -> ContractRow:
    """A minimal :class:`ContractRow` for the pure classifier."""
    return ContractRow(
        exch="NSE",
        underlying=underlying,
        expiry_date=date(2024, 1, 25),
        contract_symbol=f"NSE-{underlying}-25Jan24-{side}",
        strike=None if side == "FUT" else 21000.0,
        side=side,
        candle_status="none",
        last_run_id="prop",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


_underlying_names = st.text(
    alphabet=st.characters(min_codepoint=65, max_codepoint=90), min_size=1, max_size=10
)

_rows = st.lists(
    st.builds(
        _contract_row,
        underlying=_underlying_names,
        side=st.sampled_from(["FUT", "CE", "PE"]),
    ),
    max_size=30,
)

_kind_maps = st.one_of(
    st.none(),
    st.dictionaries(_underlying_names, st.booleans(), max_size=15),
)


@given(_rows, _kind_maps)
def test_contract_bucket_totality(rows: list[ContractRow], kinds: dict[str, bool] | None) -> None:
    """Every row lands in exactly one bucket; the union equals the input; no raise."""
    by_bucket: dict[tuple[str, bool], list[ContractRow]] = {}
    unresolved = 0
    for row in rows:
        bucket_key, resolved = hp._contract_bucket(row, kinds)
        if not resolved:
            unresolved += 1
        by_bucket.setdefault(bucket_key, []).append(row)
    flattened = [row for bucket in by_bucket.values() for row in bucket]
    assert len(flattened) == len(rows)
    assert sorted(map(id, flattened)) == sorted(map(id, rows))
    for (market, is_index), bucket in by_bucket.items():
        for row in bucket:
            # The market half is ALWAYS the ledger namespace (side-derived).
            expected_market = (
                GROWW_FUTURE_COVERAGE_MARKET if row.side == "FUT" else GROWW_OPTION_COVERAGE_MARKET
            )
            assert market == expected_market
            if kinds is None:
                assert is_index is False
            else:
                assert is_index is kinds.get(row.underlying, False)
    if kinds is None:
        assert unresolved == 0  # the legacy degrade is never "unresolved"
    else:
        assert unresolved == sum(1 for row in rows if row.underlying not in kinds)


@given(_rows, _kind_maps)
def test_contract_bucket_unknown_never_ahead_of_index(
    rows: list[ContractRow], kinds: dict[str, bool] | None
) -> None:
    """An unresolved row classifies STOCK-side — never into an index bucket."""
    for row in rows:
        bucket_key, resolved = hp._contract_bucket(row, kinds)
        if not resolved:
            assert bucket_key[1] is False
