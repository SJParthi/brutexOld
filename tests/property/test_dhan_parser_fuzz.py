"""Robustness fuzz for the Dhan columnar candle parser (DEC-PARSER-FUZZ-001).

The Dhan Data API returns OHLC as parallel columnar arrays
(``{open[], high[], low[], close[], timestamp[], volume[], open_interest[]}``).
A vendor can ship malformed payloads at any time — ragged arrays, null sides,
NaN/inf prices, epoch overflow, wrong types, missing columns. Per CLAUDE.md
Golden Rule #1 (no hallucination about external systems) + the reliability
decree (cover every malformed-input scenario), the parser must be a TOTAL
function over arbitrary input: it either returns validated ``CandleBar`` rows
or raises the declared :class:`CandleParseError` — it must NEVER leak a raw
``KeyError`` / ``TypeError`` / ``ValueError`` / ``OverflowError`` /
``decimal.InvalidOperation`` to the caller.

This Hypothesis fuzz drives adversarial payloads at
:func:`parse_candle_columns` and asserts exactly that contract.
"""

from __future__ import annotations

import math

from brutex.adapters.dhan.parser import CandleParseError, parse_candle_columns
from brutex.data.models import CandleBar
from hypothesis import given, settings
from hypothesis import strategies as st

# Column names the parser knows about, plus junk keys to exercise the
# "unexpected extra key is ignored" path.
_KNOWN_COLUMNS = ("open", "high", "low", "close", "timestamp", "volume", "open_interest")
_JUNK_KEYS = ("status", "data", "ce", "pe", "", "OPEN", "ts")

# Adversarial scalar cell values: the cross-product of types a JSON decoder
# (or a buggy vendor) could place in a column array.
_scalars = st.one_of(
    st.integers(min_value=-(2**63), max_value=2**63),
    st.integers(min_value=10**15, max_value=10**40),  # epoch / price overflow range
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(min_value=-1e9, max_value=1e9),
    st.text(max_size=8),
    st.booleans(),
    st.none(),
)

# A "column" is usually an array, but a malformed payload may put a bare
# scalar (non-array) where an array is expected — exercise both.
_columns = st.one_of(
    st.lists(_scalars, max_size=8),
    _scalars,
)

# A payload maps a random subset of known (+ junk) keys to columns.
_payloads = st.dictionaries(
    keys=st.sampled_from(_KNOWN_COLUMNS + _JUNK_KEYS),
    values=_columns,
    max_size=10,
)


@given(payload=_payloads, drop_volume=st.booleans())
@settings(max_examples=800)
def test_parser_is_total_over_arbitrary_payloads(
    payload: dict[str, object], drop_volume: bool
) -> None:
    """The parser is a total function: any input yields either validated
    ``CandleBar`` rows or a declared ``CandleParseError`` — never a leaked
    raw exception (the reliability contract)."""
    try:
        rows = parse_candle_columns(payload, drop_volume=drop_volume)
    except CandleParseError:
        return  # the one declared, acceptable failure mode
    # On success every row is a fully-formed, finite, ordered CandleBar.
    assert isinstance(rows, list)
    for bar in rows:
        assert isinstance(bar, CandleBar)
        assert bar.high >= bar.low
        for price in (bar.open, bar.high, bar.low, bar.close):
            assert price.is_finite()
        assert bar.volume >= 0


@given(
    n=st.integers(min_value=0, max_value=6),
    ts_len_delta=st.integers(min_value=-3, max_value=3),
)
@settings(max_examples=200)
def test_parser_rejects_ragged_columns(n: int, ts_len_delta: int) -> None:
    """A column whose length differs from the timestamp column is rejected
    with ``CandleParseError`` (never an ``IndexError`` from over-indexing)."""
    base = float(1_700_000_000)
    open_len = max(0, n + ts_len_delta)
    payload: dict[str, object] = {
        "timestamp": [base + i * 60 for i in range(n)],
        "open": [100.0] * open_len,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.5] * n,
    }
    try:
        rows = parse_candle_columns(payload)
    except CandleParseError:
        return
    # Only reaches here when all lengths actually matched (delta made them equal).
    assert open_len == n
    assert len(rows) <= n


def test_parser_rejects_non_finite_prices() -> None:
    """NaN / inf prices raise ``CandleParseError`` (never propagate as a
    silent non-finite Decimal into the lake)."""
    for bad in (math.nan, math.inf, -math.inf):
        payload = {
            "timestamp": [1_700_000_000.0],
            "open": [bad],
            "high": [bad],
            "low": [bad],
            "close": [bad],
        }
        try:
            parse_candle_columns(payload)
        except CandleParseError:
            continue
        raise AssertionError(f"non-finite price {bad!r} was not rejected")
