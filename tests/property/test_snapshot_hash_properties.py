"""Hypothesis properties — full-content ``data_snapshot_hash`` (DEC-SNAPSHOT-HASH-FULL-001).

The v2 contract, proven against generated bar sequences instead of hand-picked
fixtures: (1) ANY single-bar single-field mutation changes the hash (the exact
class of edit the retired v1 sampled fingerprint was blind to for interior
bars); (2) the derivation is deterministic; (3) output is always 64 lowercase
hex. Mutations keep every :class:`~brutex.data.models.CandleBar` invariant
valid (``low <= open, close <= high``) so the property exercises the hash, not
the model validation.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.stage3_wire import data_snapshot_hash
from hypothesis import given
from hypothesis import strategies as st

_IST = timezone(timedelta(hours=5, minutes=30))

_SCOPE: dict[str, str] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "start": "2026-01-01",
    "end": "2026-12-31",
}

#: The 7 mutation channels — one per CandleBar field.
_FIELDS = ("timestamp", "open", "high", "low", "close", "volume", "open_interest")


def _bar(minute: int, cents: int, volume: int, oi: int | None) -> CandleBar:
    close = Decimal("100.00") + Decimal(cents) / 100
    return CandleBar(
        timestamp=datetime(2026, 1, 5, 9, 16, tzinfo=_IST).astimezone(UTC)
        + timedelta(minutes=minute),
        open=Decimal("100.00"),
        high=Decimal("105.00"),
        low=Decimal("95.00"),
        close=close,
        volume=volume,
        open_interest=oi,
    )


_bars_strategy = st.lists(
    st.builds(
        _bar,
        st.integers(min_value=0, max_value=200),
        st.integers(min_value=-400, max_value=400),
        st.integers(min_value=0, max_value=10_000),
        st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)),
    ),
    min_size=1,
    max_size=12,
)


def _mutate(bar: CandleBar, field: str) -> CandleBar:
    """Return *bar* with exactly one field changed, all OHLC invariants kept."""
    if field == "timestamp":
        return dataclasses.replace(bar, timestamp=bar.timestamp + timedelta(seconds=1))
    if field == "open":
        return dataclasses.replace(bar, open=bar.open + Decimal("0.01"))
    if field == "high":
        return dataclasses.replace(bar, high=bar.high + Decimal("0.01"))
    if field == "low":
        return dataclasses.replace(bar, low=bar.low - Decimal("0.01"))
    if field == "close":
        return dataclasses.replace(bar, close=bar.close + Decimal("0.01"))
    if field == "volume":
        return dataclasses.replace(bar, volume=bar.volume + 1)
    new_oi = 1 if bar.open_interest is None else bar.open_interest + 1
    return dataclasses.replace(bar, open_interest=new_oi)


@given(
    _bars_strategy,
    st.integers(min_value=0, max_value=10_000),
    st.sampled_from(_FIELDS),
)
def test_any_single_field_mutation_changes_the_hash(
    bars: list[CandleBar], index_seed: int, field: str
) -> None:
    index = index_seed % len(bars)
    edited = list(bars)
    edited[index] = _mutate(bars[index], field)
    base_hash = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): bars})
    edited_hash = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): edited})
    assert base_hash != edited_hash


@given(_bars_strategy)
def test_same_input_twice_yields_the_same_hash(bars: list[CandleBar]) -> None:
    a = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): bars})
    b = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): bars})
    assert a == b


@given(_bars_strategy)
def test_output_is_64_lowercase_hex(bars: list[CandleBar]) -> None:
    digest = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): bars})
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)
