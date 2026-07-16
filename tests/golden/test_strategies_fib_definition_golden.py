"""Golden byte-equality for one Fib strategy DATA declaration + its render.

Freezes the FULL declaration of ``fib_prev_day`` (the canonical first strategy,
DEC-FIB-STRATEGY-REGISTRY-001) AND the exact Simple/Pro sentences a fixed swept
``(mask, direction)`` renders into via the FROZEN ``decode_combo``. Direction is a
SWEPT DIMENSION, so the strategy declares the BOTH-directions swept set and the
frozen render fixes a CHOSEN direction (Long). Any drift in the declaration fields,
the swept-directions set, the spec-ABI projection (hex mask + direction byte), or
the decoder's sentence composition would change these bytes — so a regression in
either the wrap layer or the decoder bridge is caught here.

Where ``tests/unit/test_strategies_fib_definitions.py`` asserts each behaviour
field-by-field, this pins the whole contract byte-for-byte.
"""

from __future__ import annotations

import json

from brutex.strategies import fib_definitions as F
from brutex.vocabulary.bits import (
    BIT_ABOVE_CPR_TC,
    BIT_EARLY_MORNING,
    BIT_EMA20_ABOVE_EMA200,
    BIT_NEAR_FIB_50,
)

# A fixed swept mask: trend-up + above-CPR-TC + Fib-50 + early-morning (the
# retired VWAP member was re-pointed to the new CPR bit at the 60→66 reshape —
# the composed sentence still exercises trend→level→time across groups).
_GOLDEN_MASK = (
    (1 << BIT_EMA20_ABOVE_EMA200)
    | (1 << BIT_ABOVE_CPR_TC)
    | (1 << BIT_NEAR_FIB_50)
    | (1 << BIT_EARLY_MORNING)
)

# ── Frozen golden: the full declaration + the rendered sentences (Long slice) ──
_GOLDEN_DECLARATION = {
    "name": "fib_prev_day",
    "anchor": "prev_day",
    "directions": ["long", "short"],  # direction is a SWEPT set, not a fixed property
    "rendered_direction": "long",  # the chosen swept direction for this golden render
    "rendered_direction_u8": 0,
    "symbols": ["NSE-NIFTY"],
    "timeframes": ["1minute"],
    "forced_exit_ist": "15:20:00",
    "stop_loss_bps": None,
    "take_profit_bps": None,
    # Re-locked at the 60→66 vocab reshape, then again at
    # DEC-GAP-MIDPOINT-CONDITIONS-001: the empty condition_scope candidate
    # mask IS IMPLEMENTED_MASK, which grew 66 → 69 declared bits (65 active —
    # the ONE moved key in this declaration; every other field byte-identical).
    "candidate_mask_hex": "000000000000001cffcfffffffffffff",
    "spec_mask_hex": "00000000000000001000100000400010",
}

_GOLDEN_SIMPLE = (
    "Buy on 15m when trend is up, at the halfway bounce zone, "
    "above the pivot-range ceiling and first hour (uptrend)."
)
_GOLDEN_PRO = "Buy 15m [uptrend]: EMA20>EMA200, Fib 50%, close>CPR-TC and early morning."


def test_fib_prev_day_declaration_is_frozen() -> None:
    d = F.PREV_DAY
    mask_hex, direction_u8 = d.spec_args(_GOLDEN_MASK, F.Direction.LONG)
    declaration = {
        "name": d.name,
        "anchor": d.anchor.value,
        "directions": [direction.value for direction in d.directions],
        "rendered_direction": F.Direction.LONG.value,
        "rendered_direction_u8": direction_u8,
        "symbols": list(d.symbols),
        "timeframes": list(d.timeframes),
        "forced_exit_ist": d.exit.forced_exit_ist.isoformat(),
        "stop_loss_bps": d.exit.stop_loss_bps,
        "take_profit_bps": d.exit.take_profit_bps,
        "candidate_mask_hex": f"{d.candidate_mask:032x}",
        "spec_mask_hex": mask_hex,
    }
    assert json.dumps(declaration, sort_keys=True) == json.dumps(
        _GOLDEN_DECLARATION, sort_keys=True
    )


def test_fib_prev_day_long_render_is_frozen() -> None:
    exp = F.render_definition(F.PREV_DAY, _GOLDEN_MASK, F.Direction.LONG, timeframe="15m")
    assert exp.simple_sentence == _GOLDEN_SIMPLE
    assert exp.pro_sentence == _GOLDEN_PRO
    assert exp.regime_label == "uptrend"
    assert exp.direction_label == "Buy"
