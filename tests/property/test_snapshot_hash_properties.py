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
from hypothesis import assume, given
from hypothesis import strategies as st

#: A separator-free key alphabet — the SAFE domain production slice keys live in
#: (``sweep_scope`` allowlisted symbols + the fixed interval vocabulary carry no
#: ``|`` / ``\n``). ``data_snapshot_hash`` is injective here.
_KEY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"

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


@given(
    base=st.text(alphabet=_KEY_ALPHABET, min_size=3, max_size=12),
    i_seed=st.integers(min_value=1, max_value=1_000),
    j_seed=st.integers(min_value=1, max_value=1_000),
)
def test_separator_free_slice_keys_cannot_alias(base: str, i_seed: int, j_seed: int) -> None:
    # DEC-DEDUP-IDENTITY-EVIDENCE-PACK-001: in the SAFE (separator-free) key
    # domain — the only domain production inputs live in — two DIFFERENT
    # (symbol, interval) splits of one string build DIFFERENT preimages (the
    # slice-header '|' delimiters sit at different offsets), so the hashes differ.
    n = len(base)
    i = 1 + (i_seed % (n - 1))
    j = 1 + (j_seed % (n - 1))
    assume(i != j)
    bars = [_bar(0, 0, 1, None)]
    h_i = data_snapshot_hash(**_SCOPE, slices={(base[:i], base[i:]): bars})
    h_j = data_snapshot_hash(**_SCOPE, slices={(base[:j], base[j:]): bars})
    assert h_i != h_j


def test_crafted_separator_keys_alias_at_preimage_is_the_documented_limit() -> None:
    # DOCUMENTED LIMIT (tripwire pin): the slice header '\n{sym}|{iv}|{len}' has
    # no '|'/'\n' rejection, so ('A|B', 'C') and ('A', 'B|C') build the
    # BYTE-IDENTICAL preimage '\nA|B|C|1' and hash EQUAL at HEAD. This is
    # production-UNREACHABLE — the sweep_scope symbol allowlist + the fixed
    # interval vocabulary + `_safe_segment` mean no '|'/'\n' key ever reaches
    # here — so the reject-guard on the Rule-4 identity-critical snapshot_hash.py
    # is deliberately DEFERRED (named follow-up DEC-SNAPSHOT-SLICE-KEY-GUARD-001).
    # When that guard lands it flips this equality to `!=` and this pin FAILS
    # LOUDLY, forcing the frozen-digest byte-neutrality re-proof at that time.
    bars = [_bar(0, 0, 1, None)]
    h_left = data_snapshot_hash(**_SCOPE, slices={("A|B", "C"): bars})
    h_right = data_snapshot_hash(**_SCOPE, slices={("A", "B|C"): bars})
    assert h_left == h_right
