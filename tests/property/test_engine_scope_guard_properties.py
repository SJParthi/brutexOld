"""Property invariants for the engine scope guard (DEC-CORP-ACTION-GATE-001).

Two invariants, hypothesis-driven:
- ANY symbol whose normalized form is outside the allowed set is refused —
  there is no bypass string, no encoding trick, no case/whitespace variant
  that slips an out-of-scope symbol into the engine.
- Normalization is idempotent: admitting an already-admitted output changes
  nothing (``ensure_engine_scope(ensure_engine_scope(x)) == ensure_engine_scope(x)``).
"""

from __future__ import annotations

import pytest
from brutex.engine.scope_guard import (
    ENGINE_SCOPE_SYMBOLS,
    EngineScopeError,
    ensure_engine_scope,
)
from hypothesis import given
from hypothesis import strategies as st

_symbols = st.text(min_size=0, max_size=24)


@given(symbol=_symbols)
def test_out_of_scope_symbol_always_refused(symbol: str) -> None:
    normalized = symbol.strip().upper()
    if normalized in ENGINE_SCOPE_SYMBOLS:
        assert ensure_engine_scope([symbol]) == (normalized,)
    else:
        with pytest.raises(EngineScopeError, match="corporate-action adjustment layer missing"):
            ensure_engine_scope([symbol])


@given(
    symbols=st.lists(
        st.sampled_from(sorted(ENGINE_SCOPE_SYMBOLS)).map(lambda s: f"  {s.lower()} "),
        min_size=1,
        max_size=6,
    )
)
def test_normalization_idempotent(symbols: list[str]) -> None:
    once = ensure_engine_scope(symbols)
    assert ensure_engine_scope(once) == once
