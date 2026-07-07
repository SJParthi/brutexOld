"""Unit tests for the kept :class:`MarketStructureSignal` container.

DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 deleted the hand-synced Python
``MarketStructureState``/``Trend`` twin (with its ``test_market_structure.py``
state-machine suite — the behaviour is owned by ``trading_core.
MarketStructureState``, replayed against the frozen golden fixture in
``tests/unit/test_twin_state_golden_parity.py`` and P-06-proven in
``tests/property/test_market_structure_lookahead.py``). This file is the
dedicated suite for the ONE symbol production Python still imports from
``brutex.indicators.market_structure``: the frozen signal dataclass threaded
into ``brutex/engine/stage1a.py`` + ``brutex/vocabulary/evaluator.py``.
100% line + branch on ``brutex.indicators.market_structure``.
"""

from __future__ import annotations

import pytest
from brutex.indicators.market_structure import MarketStructureSignal


def test_default_signal_is_all_false() -> None:
    """The warm-up default: no confirmed swing yet -> no Group-12 bit fires."""
    sig = MarketStructureSignal()
    assert not (sig.bos_bullish or sig.bos_bearish or sig.choch_bullish or sig.choch_bearish)


def test_signal_is_frozen() -> None:
    """The container is immutable — a resolved bar signal can never be edited."""
    sig = MarketStructureSignal(bos_bullish=True)
    with pytest.raises(AttributeError):
        sig.bos_bullish = False  # type: ignore[misc]  # frozen dataclass


def test_explicit_construction_carries_each_flag_independently() -> None:
    """Each of the four flags is an independent field (the 4-bool wire order
    shared with the Rust FFI tuple: bos_bullish, bos_bearish, choch_bullish,
    choch_bearish)."""
    sig = MarketStructureSignal(
        bos_bullish=True,
        bos_bearish=False,
        choch_bullish=False,
        choch_bearish=True,
    )
    assert (sig.bos_bullish, sig.bos_bearish, sig.choch_bullish, sig.choch_bearish) == (
        True,
        False,
        False,
        True,
    )


def test_value_equality() -> None:
    """Dataclass value semantics: equal flags compare equal, differing do not."""
    assert MarketStructureSignal(choch_bearish=True) == MarketStructureSignal(choch_bearish=True)
    assert MarketStructureSignal() != MarketStructureSignal(bos_bearish=True)
