"""Property tests for the Groww contract-symbol parser (DEC-GROWW-OPTION-INGEST-001).

Hypothesis proves the :func:`~brutex.adapters.groww.contracts.parse_contract_symbol`
invariants GENERATIVELY (the golden corpus pins exact bytes; this proves the
rules hold across the input space):

* **Round-trip identity** — the parsed ``symbol`` is the input verbatim, and the
  authoritative ``underlying`` / ``expiry`` arguments pass through unchanged.
* **Type discrimination** — the trailing ``CE`` / ``PE`` / ``FUT`` token maps to
  ``contract_type``; a future has ``strike=None``, an option a positive ``strike``.
* **Right-anchored robustness** — a (hypothetical) HYPHENATED underlying never
  confuses the type/strike tokens (they are read from the right).
* **Fail-loud** — a too-short symbol always raises ``ValueError``.
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.adapters.groww.contracts import parse_contract_symbol
from hypothesis import given
from hypothesis import strategies as st

_EXCHANGES = st.sampled_from(["NSE", "BSE"])
# Underlying tokens: 1–10 upper-alnum chars (real NSE tickers), no hyphen.
_UNDERLYINGS = st.text(
    alphabet=st.characters(min_codepoint=65, max_codepoint=90), min_size=1, max_size=10
)
_EXPIRY_TOKENS = st.sampled_from(["02Jan25", "30Sep25", "25Jan24", "28Dec23"])
_STRIKES = st.integers(min_value=1, max_value=1_000_000)
_EXPIRY = date(2025, 1, 2)


@given(
    exch=_EXCHANGES,
    und=_UNDERLYINGS,
    exp=_EXPIRY_TOKENS,
    strike=_STRIKES,
    opt=st.sampled_from(["CE", "PE"]),
)
def test_option_round_trip(exch: str, und: str, exp: str, strike: int, opt: str) -> None:
    symbol = f"{exch}-{und}-{exp}-{strike}-{opt}"
    c = parse_contract_symbol(symbol, underlying_symbol=und, expiry=_EXPIRY)
    assert c.symbol == symbol
    assert c.contract_type == opt
    assert c.strike == float(strike)
    assert c.underlying_symbol == und
    assert c.expiry == _EXPIRY


@given(exch=_EXCHANGES, und=_UNDERLYINGS, exp=_EXPIRY_TOKENS)
def test_future_has_no_strike(exch: str, und: str, exp: str) -> None:
    c = parse_contract_symbol(f"{exch}-{und}-{exp}-FUT", underlying_symbol=und, expiry=_EXPIRY)
    assert c.contract_type == "FUT"
    assert c.strike is None


@given(strike=_STRIKES, opt=st.sampled_from(["CE", "PE"]))
def test_right_anchored_survives_hyphenated_underlying(strike: int, opt: str) -> None:
    # A multi-token underlying ("A-B-C") must not shift the trailing type/strike.
    symbol = f"NSE-A-B-C-02Jan25-{strike}-{opt}"
    c = parse_contract_symbol(symbol, underlying_symbol="A-B-C", expiry=_EXPIRY)
    assert c.contract_type == opt and c.strike == float(strike)


@given(tokens=st.lists(st.from_regex(r"[A-Z0-9]+", fullmatch=True), min_size=1, max_size=3))
def test_too_short_symbol_raises(tokens: list[str]) -> None:
    # Fewer than 4 "-"-joined fields can never be a valid derivative symbol.
    with pytest.raises(ValueError):
        parse_contract_symbol("-".join(tokens), underlying_symbol="X", expiry=_EXPIRY)
