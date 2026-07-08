#!/usr/bin/env python3
"""Generate the cross-language golden answer-key for the condition vocabulary.

Phase 0 of the shared-core migration (DEC-SHARED-CORE-001, proposed): freeze the
*current* Python ``evaluate_atoms`` output over a diverse, deterministic set of
inputs into a language-neutral JSON fixture. This fixture is the **oracle**:

* the Python test (``tests/golden/test_vocab_golden.py``) asserts Python still
  reproduces every frozen ``u128`` mask — locking the behaviour against accidental
  drift;
* the future Rust ``trading-core`` crate MUST reproduce the same ``u128`` mask for
  the same inputs **bit-for-bit** before BruteX/TickVault switch to it.

The cases are built with a fixed seed so the fixture is reproducible. Prices are
int64 paisa. Run with ``python scripts/gen_vocab_golden_vectors.py`` to (re)write
``tests/golden/fixtures/vocab_golden_vectors.json``.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.market_structure import MarketStructureSignal
from brutex.indicators.pivots import compute_pivot_levels
from brutex.vocabulary import TOTAL_CONDITIONS
from brutex.vocabulary.bits import IMPLEMENTED_MASK
from brutex.vocabulary.evaluator import (
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    evaluate_atoms,
)

#: Hex width to zero-pad a u128 mask string (32 nibbles). The mask is serialised
#: as a STRING (not a JSON number) because a mask using bits >= 53 would exceed the
#: 2**53 IEEE-754 double safe-integer range and lose precision as a JSON/JS number.
_MASK_HEX_WIDTH = 32

IST = timezone(timedelta(hours=5, minutes=30))
_SEED = 20260603
_N_RANDOM = 400

#: Time-of-day samples that land in each Group-8 window + the excluded edges.
_TOD_SAMPLES: tuple[time, ...] = (
    time(9, 16),  # early_morning start
    time(9, 45),  # early_morning
    time(10, 30),  # mid_morning
    time(11, 30),  # mid_morning end
    time(12, 30),  # midday
    time(14, 30),  # afternoon
    time(15, 15),  # afternoon (pre forced-exit)
    time(9, 0),  # pre-open (no window)
)
_PROXIMITY_BPS: tuple[int, ...] = (5, 10, 25, 50)
_PRIOR_FLAG_PATTERNS: tuple[tuple[bool, ...], ...] = (
    (True, True, True),
    (False, False, False),
    (True, False, True),
    (True, True, False),
    (),
)


def _build_case(rng: random.Random, case_id: int) -> dict[str, Any]:
    """Build one diverse, deterministic case + its frozen ``evaluate_atoms`` u128 mask."""
    # Previous-day anchors (drive pivots + Fib); keep pdh > pdl, pdc inside.
    pdl = rng.randint(1_500_000, 4_500_000)
    pdh = pdl + rng.randint(20_000, 400_000)
    pdc = rng.randint(pdl, pdh)
    day = datetime(2024, 1, 2).date()
    pivots = compute_pivot_levels(day, pdh, pdl, pdc)
    fib = compute_fib_levels(day, pdh, pdl)

    # Anchor the close: sometimes snap it onto a real level (to fire `near_*`),
    # sometimes free-float it inside/around the prior-day range.
    levels = [
        pivots.p_paisa,
        pivots.r1_paisa,
        pivots.r2_paisa,
        pivots.r3_paisa,
        pivots.s1_paisa,
        pivots.s2_paisa,
        pivots.s3_paisa,
        pdh,
        pdl,
        fib.retrace_23_paisa,
        fib.retrace_38_paisa,
        fib.retrace_50_paisa,
        fib.retrace_62_paisa,
        fib.retrace_78_paisa,
        fib.ext_up_127_paisa,
        fib.ext_up_162_paisa,
        # R5/S5 (PR-2): include so close sometimes snaps onto them and the new
        # near_pivot_r5 / near_pivot_s5 bits (54/55) fire across the 400 cases.
        pivots.r5_paisa,
        pivots.s5_paisa,
    ]
    if rng.random() < 0.6:
        close = max(1, levels[rng.randrange(len(levels))] + rng.randint(-300, 300))
    else:
        close = rng.randint(pdl - 200_000, pdh + 200_000)
    close = max(1, close)

    # Candle: vary body direction/size + wicks around the close.
    body = rng.randint(-5_000, 5_000)
    open_p = max(1, close - body)
    hi = max(open_p, close) + rng.randint(0, 8_000)
    lo = max(1, min(open_p, close) - rng.randint(0, 8_000))

    # Day extremes: sometimes pin close to day high/low (Group 7).
    day_high = max(hi, close)
    day_low = min(lo, close)
    if rng.random() < 0.25:
        close = day_high  # close_at_day_high
    elif rng.random() < 0.25:
        close = day_low  # close_at_day_low
    day_high = max(day_high, close)
    day_low = min(day_low, close)

    # Occasionally force an outside day (today's extremes engulf the prior range,
    # day_high > pdh AND day_low < pdl) so the `outside_day` bit (51) fires.
    if rng.random() < 0.18:
        day_high = max(day_high, pdh + rng.randint(1_000, 50_000))
        day_low = max(1, min(day_low, pdl - rng.randint(1_000, 50_000)))

    # Today's open vs prior close → gap up/down; vs prior range → inside/outside.
    open_of_day = pdc + rng.choice((-1, 1)) * rng.randint(0, 60_000)

    # EMAs: above/below the close, or warming up (None).
    ema20 = None if rng.random() < 0.12 else max(1, close + rng.randint(-30_000, 30_000))
    ema200 = None if rng.random() < 0.12 else max(1, close + rng.randint(-60_000, 60_000))

    # Session VWAP (PR-2): above/below the close, or warming-up/1day-NA (None).
    # Mirrors the EMA distribution so the Group-10 close_above/below_vwap bits
    # (52/53) are exercised BOTH ways across the 400 cases (the coverage audit).
    vwap = None if rng.random() < 0.12 else max(1, close + rng.randint(-40_000, 40_000))

    # BOS/CHoCH (DEC-NIFTY-BOS-CHOCH-001): the four resolved Group-12 flags
    # (bits 56-59). Each fires independently with ~25% probability so across the
    # 400 cases EVERY one is set in >=1 case AND clear in >=1 case (the both-ways
    # coverage gate). The look-ahead-clean RESOLUTION of these flags is owned +
    # proven by the shared Rust trading_core.MarketStructureState (the single
    # source since DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001; its golden-replay +
    # property + P-06 tests); the golden oracle only carries the resolved flags so
    # both languages set the same bits from them.
    market_structure = MarketStructureSignal(
        bos_bullish=rng.random() < 0.25,
        bos_bearish=rng.random() < 0.25,
        choch_bullish=rng.random() < 0.25,
        choch_bearish=rng.random() < 0.25,
    )

    tod = rng.choice(_TOD_SAMPLES)
    ts = datetime(2024, 1, 2, tod.hour, tod.minute, tzinfo=IST)

    prox = ProximityParams(
        pivot_proximity_bps=rng.choice(_PROXIMITY_BPS),
        pdh_pdl_proximity_bps=rng.choice(_PROXIMITY_BPS),
        fib_proximity_bps=rng.choice(_PROXIMITY_BPS),
    )
    flags = rng.choice(_PRIOR_FLAG_PATTERNS)
    prior_n = len(flags) if flags else rng.choice((1, 2, 3, 4))
    prior = PriorBarHistory(prior_n=prior_n, flags=list(flags))

    snap = IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=open_p,
        high_paisa=hi,
        low_paisa=lo,
        close_paisa=close,
        ema20_paisa=ema20,
        ema200_paisa=ema200,
        vwap_paisa=vwap,
        pivots=pivots,
        fib=fib,
        day_high_paisa=day_high,
        day_low_paisa=day_low,
        open_of_day_paisa=open_of_day,
        market_structure=market_structure,
    )
    mask = evaluate_atoms(snap, prox, prior)

    return {
        "id": case_id,
        "timestamp_ist": ts.isoformat(),
        "open_paisa": open_p,
        "high_paisa": hi,
        "low_paisa": lo,
        "close_paisa": close,
        "ema20_paisa": ema20,
        "ema200_paisa": ema200,
        "vwap_paisa": vwap,
        "prev_pdh_paisa": pdh,
        "prev_pdl_paisa": pdl,
        "prev_pdc_paisa": pdc,
        "day_high_paisa": day_high,
        "day_low_paisa": day_low,
        "open_of_day_paisa": open_of_day,
        "proximity": {
            "pivot_bps": prox.pivot_proximity_bps,
            "pdh_pdl_bps": prox.pdh_pdl_proximity_bps,
            "fib_bps": prox.fib_proximity_bps,
        },
        "prior": {"prior_n": prior_n, "flags": list(flags)},
        "market_structure": {
            "bos_bullish": market_structure.bos_bullish,
            "bos_bearish": market_structure.bos_bearish,
            "choch_bullish": market_structure.choch_bullish,
            "choch_bearish": market_structure.choch_bearish,
        },
        # u128 mask serialised as a 32-nibble hex STRING (not a JSON number) so a
        # mask using bits >= 53 keeps full precision across JSON/JS doubles.
        "expected_mask": f"{mask:0{_MASK_HEX_WIDTH}x}",
    }


def generate() -> dict[str, Any]:
    """Build the full deterministic fixture payload."""
    rng = random.Random(_SEED)  # noqa: S311 — deterministic fixture generation, not crypto
    cases = [_build_case(rng, i) for i in range(_N_RANDOM)]
    # Coverage audit: every IMPLEMENTED bit must be set in >=1 case AND clear in
    # >=1 case, so the oracle exercises every condition the evaluator actually
    # computes both ways. PR-2 flipped VWAP (52/53) + pivot R5/S5 (54/55) to
    # implemented; DEC-NIFTY-BOS-CHOCH-001 flipped the BOS/CHoCH bits (56-59) — so
    # all 60 bits are now part of this both-ways gate (the generator snaps close
    # onto R5/S5, varies the VWAP above/below close, and fires each of the four
    # resolved BOS/CHoCH flags ~25% of the time). No condition bit is layout-only
    # any more; only the reserved u128 headroom (60..127) stays inert.
    set_seen = 0
    clr_seen = 0
    for c in cases:
        mask = int(c["expected_mask"], 16)
        set_seen |= mask
        clr_seen |= (~mask) & IMPLEMENTED_MASK
    full = (1 << TOTAL_CONDITIONS) - 1
    return {
        # version 2: u128 mask, `expected_mask` is a STRING, the both-ways gate is
        # partitioned over the implemented bits (DEC-VOCAB-U128-CODEGEN-001).
        "version": 2,
        "generated_from": "brutex.vocabulary.evaluator.evaluate_atoms",
        "seed": _SEED,
        "total_conditions": TOTAL_CONDITIONS,
        "implemented_mask": f"{IMPLEMENTED_MASK:0{_MASK_HEX_WIDTH}x}",
        "n_cases": len(cases),
        "bits_set_somewhere": f"{set_seen:0{_MASK_HEX_WIDTH}x}",
        "bits_clear_somewhere": f"{clr_seen:0{_MASK_HEX_WIDTH}x}",
        "full_mask": f"{full:0{_MASK_HEX_WIDTH}x}",
        "cases": cases,
    }


def main() -> int:
    """Write the fixture and report coverage."""
    payload = generate()
    set_ok = int(payload["bits_set_somewhere"], 16) == IMPLEMENTED_MASK
    clr_ok = int(payload["bits_clear_somewhere"], 16) == IMPLEMENTED_MASK
    out = Path(__file__).resolve().parents[1] / "tests/golden/fixtures/vocab_golden_vectors.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {payload['n_cases']} cases → {out}")
    print(f"all implemented bits set-somewhere : {set_ok}")
    print(f"all implemented bits clear-somewhere: {clr_ok}")
    return 0 if (set_ok and clr_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
