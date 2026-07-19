"""Hypothesis differential for the fno_symbol Rust seam (REAL wheel).

DEC-DATA-RUST-PURELAW-001 (MIGRATION.md #1940). The routed facade in the
post-burn-in steady state (flag forced ON, ``reset(remaining=0)`` — the
oracle is OUT of the serving path) must equal the LIVE Python oracle under
the STRICT compare (dataclass equality + ``str(strike)``) and NEVER raise,
over both a STRUCTURED symbol generator (token pools assembled with
adversarial joins) and RAW text blobs.

Wheel-gated (``importorskip`` — skips in main CI; teeth in the
data-rust-parity lane, the ``test_feed_rust`` parity precedent).

HONESTY: for a NON-ASCII input the facade routes to the oracle via the
ASCII pre-gate, so that arm proves the GATE (identical behavior by
construction), not the Rust mirror — the Rust-facing domain is the ASCII
arm. Both arms are kept: the gate is part of the shipped contract.
"""

from __future__ import annotations

import pytest
from brutex.data import fno_symbol_rust as fsr
from brutex.data.fno_symbol import (
    FnoSymbolParts,
    _parse_fno_symbol_python,
    parse_fno_symbol,
    parse_strike_token,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# --------------------------------------------------------------------------
# Structured symbol generator — token pools with adversarial joins/drops.
# --------------------------------------------------------------------------

_EXCHANGES = st.sampled_from(["NSE", "BSE", "nse", "", " NSE", "MCX"])
_UNDERLYINGS = st.sampled_from(
    ["NIFTY", "BANKNIFTY", "SENSEX", "BAJAJ-AUTO", "M&M", "M&MFIN", "", "A", "X Y"]
)
_EXPIRY_TOKENS = st.sampled_from(
    [
        "30Sep25",
        "3Sep25",
        "03Sep25",
        " 3Sep25",
        " 30Sep25",
        "29Feb24",
        "29Feb25",
        "31Sep25",
        "30SEP25",
        "30sep25",
        "30Sept25",
        "30Sep2025",
        "30Jun75",
        "01Jan19",
        "31Dec18",
        "31Dec59",
        "01Jan60",
        "30Sep68",
        "30Sep69",
        "00Sep25",
        "030Sep25",
        "30ene25",
        "",
        "junk",
    ]
)
_STRIKE_TOKENS = st.sampled_from(
    [
        "24650",
        "24000.5",
        "24000.50",
        "1E4",
        "24_000",
        "+24000",
        " 24000",
        "0",
        "-5",
        "inf",
        "nan",
        "abc",
        "",
    ]
)
_TYPE_TOKENS = st.sampled_from(["FUT", "CE", "PE", "fut", "ce", "Pe", "G1S", "EQ", ""])


@st.composite
def _structured_symbols(draw: st.DrawFn) -> str:
    """Assemble a symbol from token pools with random joins/drops."""
    keep = [draw(_EXCHANGES), draw(_UNDERLYINGS), draw(_EXPIRY_TOKENS)]
    if draw(st.booleans()):
        keep.append(draw(_STRIKE_TOKENS))
    keep.append(draw(_TYPE_TOKENS))
    # Random drops keep short/degenerate token counts in the domain.
    for _ in range(draw(st.integers(min_value=0, max_value=2))):
        if keep:
            keep.pop(draw(st.integers(min_value=0, max_value=len(keep) - 1)))
    return "-".join(keep)


def _assert_parity(symbol: str) -> None:
    """Steady-state routed result ≡ oracle, strict compare, never raises."""
    with pytest.MonkeyPatch.context() as mp:
        pytest.importorskip("brutex_data")
        mp.setenv(fsr.DATA_RUST_ENV, "1")
        fsr.reset_fno_symbol_rust_for_tests(remaining=0)  # steady state
        try:
            routed = parse_fno_symbol(symbol)
        finally:
            latched = fsr.data_rust_disabled()
            fsr.reset_fno_symbol_rust_for_tests()
    oracle = _parse_fno_symbol_python(symbol)
    assert fsr._parts_equal_strict(routed, oracle), (
        f"divergence on {symbol!r}: routed={routed!r} oracle={oracle!r}"
    )
    # Anti-vacuous: an engaged ASCII call must never have latched the seam
    # OFF (a latch would demote the property to oracle-vs-oracle).
    assert not latched, f"seam latched OFF on {symbol!r}"


class TestStructuredDifferential:
    @settings(max_examples=300, deadline=None)
    @given(symbol=_structured_symbols())
    def test_structured_ascii_domain(self, symbol: str) -> None:
        _assert_parity(symbol)


class TestRawTextDifferential:
    @settings(max_examples=200, deadline=None)
    @given(symbol=st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=64))
    def test_raw_ascii_blobs(self, symbol: str) -> None:
        _assert_parity(symbol)

    @settings(max_examples=100, deadline=None)
    @given(symbol=st.text(max_size=64))
    def test_raw_unicode_blobs_prove_the_gate(self, symbol: str) -> None:
        # Non-ASCII inputs route to the oracle (the ASCII pre-gate) — this
        # arm proves the GATE, not the Rust mirror (module docstring).
        _assert_parity(symbol)


class TestSteadyStateStrikeRepresentation:
    def test_representation_strikes_ship_identically(self) -> None:
        pytest.importorskip("brutex_data")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(fsr.DATA_RUST_ENV, "1")
            fsr.reset_fno_symbol_rust_for_tests(remaining=0)
            try:
                for token, expected_repr in [
                    ("24000.50", "24000.50"),
                    ("1E4", "1E+4"),
                    ("24_000", "24000"),
                ]:
                    sym = f"NSE-NIFTY-30Sep25-{token}-CE"
                    routed = parse_fno_symbol(sym)
                    assert isinstance(routed, FnoSymbolParts)
                    assert str(routed.strike) == expected_repr
                    assert routed.strike == parse_strike_token(token)
            finally:
                fsr.reset_fno_symbol_rust_for_tests()
