"""Hypothesis property tests for the gainers/losers screener (DEC-MOVERS-GAINERS-001).

Invariants that must hold for every valid input, complementing the known-value
unit tests in ``tests/unit/test_screeners_gainers_losers.py``:

1. **Sign law** — every gainer has ``pct_change > 0``, every loser ``< 0``;
   a flat mover is in neither list.
2. **Subset + disjoint** — gainers and losers are subsets of the input and
   share no instrument.
3. **Ordering** — gainers are sorted by descending % change (ascending key),
   losers by ascending % change, each with the ``(symbol, exchange, segment)``
   tiebreak; the order is a total order.
4. **Cap** — neither list exceeds ``top_n`` and each equals
   ``min(top_n, eligible count)``.
5. **Determinism** — the same input ranked twice is byte-identical (Golden
   Rule #4).
6. **Reduction sign** — :func:`_build_mover`'s ``pct_change`` shares the sign
   of ``last_close − first_open``.
"""

# pyright: reportPrivateUsage=none
# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): the Hypothesis `@st.composite` strategy
# `_movers` is called WITHOUT the `draw` argument — Hypothesis injects it at
# runtime. pyright does not model the `@composite` signature transform and
# reports a false "Argument missing for parameter draw" at every
# `@given(...=_movers())` site. The strategy is correct (the suite passes).
# Scoped to this file only; `reportCallIssue` stays a hard error everywhere else.

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.screeners.gainers_losers import (
    InstrumentRef,
    Mover,
    _build_mover,
    rank_movers,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.screener_lake import make_bar

_SYMBOLS = st.sampled_from(["A", "B", "C", "DUP"])
_EXCHANGES = st.sampled_from(["NSE", "BSE"])
_SEGMENTS = st.sampled_from(["INDEX", "CASH", "FNO"])
#: % change in tenths so ties (incl. exact 0) occur naturally.
_PCT_TENTHS = st.integers(min_value=-200, max_value=200)
_TOP_N = st.integers(min_value=1, max_value=10)


@st.composite
def _movers(draw: st.DrawFn) -> list[Mover]:
    """A list of :class:`Mover` with overlapping keys + signs (incl. flats)."""
    rows = draw(st.lists(st.tuples(_SYMBOLS, _EXCHANGES, _SEGMENTS, _PCT_TENTHS), max_size=20))
    return [
        Mover(
            ref=InstrumentRef(symbol=s, exchange=e, segment=g),
            base_price=Decimal("100.00"),
            last_price=Decimal("100.00"),
            pct_change=Decimal(p) / 10,
            bar_count=2,
        )
        for s, e, g, p in rows
    ]


def _gkey(m: Mover) -> tuple[Decimal, str, str, str]:
    return (-m.pct_change, m.ref.symbol, m.ref.exchange, m.ref.segment)


def _lkey(m: Mover) -> tuple[Decimal, str, str, str]:
    return (m.pct_change, m.ref.symbol, m.ref.exchange, m.ref.segment)


@given(movers=_movers(), top_n=_TOP_N)
def test_sign_law(movers: list[Mover], top_n: int) -> None:
    gainers, losers = rank_movers(movers, top_n=top_n)
    assert all(m.pct_change > 0 for m in gainers)
    assert all(m.pct_change < 0 for m in losers)


@given(movers=_movers(), top_n=_TOP_N)
def test_subset_and_disjoint(movers: list[Mover], top_n: int) -> None:
    gainers, losers = rank_movers(movers, top_n=top_n)
    pool = list(movers)
    assert all(m in pool for m in gainers)
    assert all(m in pool for m in losers)
    assert not ({id(m) for m in gainers} & {id(m) for m in losers})


@given(movers=_movers(), top_n=_TOP_N)
def test_ordering_is_a_total_order(movers: list[Mover], top_n: int) -> None:
    gainers, losers = rank_movers(movers, top_n=top_n)
    assert [_gkey(m) for m in gainers] == sorted(_gkey(m) for m in gainers)
    assert [_lkey(m) for m in losers] == sorted(_lkey(m) for m in losers)


@given(movers=_movers(), top_n=_TOP_N)
def test_cap_equals_min_of_top_n_and_eligible(movers: list[Mover], top_n: int) -> None:
    gainers, losers = rank_movers(movers, top_n=top_n)
    n_pos = sum(1 for m in movers if m.pct_change > 0)
    n_neg = sum(1 for m in movers if m.pct_change < 0)
    assert len(gainers) == min(top_n, n_pos)
    assert len(losers) == min(top_n, n_neg)


@given(movers=_movers(), top_n=_TOP_N)
def test_ranking_is_deterministic(movers: list[Mover], top_n: int) -> None:
    first = rank_movers(movers, top_n=top_n)
    second = rank_movers(movers, top_n=top_n)
    assert first == second


@given(
    base_paisa=st.integers(min_value=1, max_value=10_000_000),
    last_paisa=st.integers(min_value=1, max_value=10_000_000),
)
@settings(max_examples=200)
def test_build_mover_sign_matches_price_delta(base_paisa: int, last_paisa: int) -> None:
    base = Decimal(base_paisa) / 100
    last = Decimal(last_paisa) / 100
    hi = max(base, last)
    lo = min(base, last)
    bar = make_bar(
        minute=0,
        open_=str(base),
        close=str(last),
        high=str(hi),
        low=str(lo),
    )
    mover = _build_mover(InstrumentRef(symbol="X", exchange="NSE", segment="INDEX"), [bar])
    assert mover is not None
    if last > base:
        assert mover.pct_change > 0
    elif last < base:
        assert mover.pct_change < 0
    else:
        assert mover.pct_change == 0


@given(
    rows=st.lists(
        st.tuples(_SYMBOLS, st.integers(min_value=80, max_value=120), st.booleans()),
        max_size=12,
        unique_by=lambda t: t[0],
    ),
    top_n=_TOP_N,
)
def test_screen_partitions_every_instrument(rows: list[tuple[str, int, bool]], top_n: int) -> None:
    """Each lake instrument lands in exactly one of gain / lose / flat / skip."""
    from brutex.screeners.gainers_losers import screen_gainers_losers

    from tests.fakes.screener_lake import FakeLakeReader

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 12, 31, tzinfo=UTC)
    reader_data: dict[InstrumentRef, list[CandleBar]] = {}
    for sym, close_rupees, has_data in rows:
        ref = InstrumentRef(symbol=sym, exchange="NSE", segment="INDEX")
        # ``has_data`` False → empty window → must be skipped.
        reader_data[ref] = (
            [make_bar(minute=0, open_="100.00", close=f"{close_rupees}.00")] if has_data else []
        )
    reader = FakeLakeReader(data=reader_data)
    result = screen_gainers_losers(reader, interval="1day", start=start, end=end, top_n=top_n)

    gainer_refs = {m.ref for m in result.gainers}
    loser_refs = {m.ref for m in result.losers}
    skipped_refs = set(result.skipped)
    # The four buckets are pairwise disjoint.
    assert len(gainer_refs) + len(loser_refs) + len(skipped_refs) == (
        len(gainer_refs | loser_refs | skipped_refs)
    )
    # Every empty-data instrument is skipped; every skipped one had empty data.
    empty = {ref for ref, bars in reader_data.items() if not bars}
    assert skipped_refs == empty
    assert len(result.gainers) <= top_n
    assert len(result.losers) <= top_n
