"""Market structure — the resolved Break-of-Structure / Change-of-Character signal.

Per ADR-0012 D3/D4 (DEC-NIFTY-BOS-CHOCH-001): the brute-force NIFTY-futures track
needs market-structure conditions for the ``bos_bullish`` / ``bos_bearish`` /
``choch_bullish`` / ``choch_bearish`` conditions (bits 56-59).

## ONE detector, ONE source of truth (shared with TickVault)

The stateful BOS/CHoCH DETECTOR — the look-ahead-clean fractal-swing state
machine that RESOLVES these four booleans per bar — lives in the shared Rust
``trading-core`` crate and is consumed here as ``trading_core.
MarketStructureState`` (the PyO3 pyclass ``brutex/engine/stage1a.py`` drives
per bar; TickVault links the same core natively — DEC-SHARED-CORE-001 / D7).
The hand-synced Python twin that used to live in this module was DELETED by
DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001: production never imported it (only
the Rust pyclass), and the frozen twin-state golden fixture
(``tests/golden/fixtures/twin_state_golden_vectors.json`` — the TickVault
contract, never regenerated) is now replayed Rust-only by
``tests/unit/test_twin_state_golden_parity.py``. The same decision deleted the
sibling ``brutex.indicators.vwap`` module; the Rust ``VwapState`` itself was
later deleted with the bits-52/53 VWAP tombstone (crate 0.4.0 — the spot-only
engine has no traded volume).

This module keeps the one piece production Python imports: the FROZEN
:class:`MarketStructureSignal` container the detector's four resolved booleans
are threaded through (``brutex/engine/stage1a.py`` +
``brutex/vocabulary/evaluator.py``).

## Semantics of the four flags (resolved by the Rust detector)

Like every other atomic condition in the vocabulary, these are PER-BAR
LEVEL-POSITION PREDICATES (each is re-evaluated on every bar against the
confirmed swing levels known so far), NOT one-shot "events" or edges:

* **BOS** (Break of Structure — trend CONTINUATION): bullish on any bar whose
  close is above the most recent confirmed swing HIGH while the prevailing trend
  is up (or not yet set); bearish symmetrically against the confirmed swing LOW.
* **CHoCH** (Change of Character — trend REVERSAL): a break AGAINST the
  prevailing trend (which flips it), so the very next held-beyond bar reads as a
  BOS.

On any one bar at most ONE of ``{bos_bullish, choch_bullish}`` and at most ONE
of ``{bos_bearish, choch_bearish}`` reads ``true``; the up-side and down-side
are independent (a large engulfing bar can read one of each). All four are
``False`` during warm-up (before any confirmed swing). The no-look-ahead proof
(Property P-06 — a fractal swing is confirmed only ``k`` bars later, never the
current bar) is pinned against the Rust detector in
``tests/property/test_market_structure_lookahead.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketStructureSignal:
    """The four resolved BOS/CHoCH booleans for one bar.

    Fed into the evaluator (and the Rust ``evaluate_with_levels`` ABI) exactly
    like the resolved :class:`~brutex.vocabulary.evaluator.PriorBarHistory` flags.
    All-false during warm-up (before any confirmed swing) so the Group-12 bits
    simply do not fire. Resolved per bar by the shared Rust detector
    ``trading_core.MarketStructureState`` (the single source —
    DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001).
    """

    bos_bullish: bool = False
    bos_bearish: bool = False
    choch_bullish: bool = False
    choch_bearish: bool = False
