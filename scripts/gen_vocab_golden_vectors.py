#!/usr/bin/env python3
"""Generate the cross-language golden answer-key for the condition vocabulary.

Phase 0 of the shared-core migration (DEC-SHARED-CORE-001): freeze the
*current* Python ``evaluate_atoms`` output over a diverse, deterministic set of
inputs into a language-neutral JSON fixture. This fixture is the **oracle**:

* the Python test (``tests/golden/test_vocab_golden.py``) asserts Python still
  reproduces every frozen ``u128`` mask — locking the behaviour against accidental
  drift;
* the Rust ``trading-core`` crate MUST reproduce the same ``u128`` mask for
  the same inputs **bit-for-bit** (``rust/trading-core/tests/golden_vectors.rs``)
  before BruteX/TickVault trust it.

The cases are built with a fixed seed so the fixture is reproducible. Prices are
int64 paisa. Run with ``python scripts/gen_vocab_golden_vectors.py`` to (re)write
``tests/golden/fixtures/vocab_golden_vectors.json`` (atomic, and ONLY after the
coverage verdict passes), or with ``--check`` to fail if the on-disk fixture is
stale versus a fresh in-memory generation (the truth-table staleness pattern).

## The 60→66 reshape migration invariant (frozen inputs)

The vocab reshape (VWAP bits 52/53 tombstoned; CPR bits 60-63 implemented;
SuperTrend 64/65 then declared layout-only) regenerated this fixture under a
FROZEN seeded-RNG input stream: the retired VWAP draw is still CONSUMED
(burned) in exactly its historical position and recorded as the INERT
``vwap_paisa`` fixture field (it feeds no evaluator input —
``IndicatorSnapshot`` carries no VWAP), so every one of the 400 case INPUTS is
byte-identical to the pre-reshape frozen fixture and only ``expected_mask``
moved. The permanent migration test
(``tests/golden/test_vocab_golden_migration.py``) pins that invariant per case
against a committed copy of the old masks + per-case input SHA-256s. The CPR
bits need NO new draws — they derive from the prev-day OHLC anchors the
generator already draws.

## The version-4 SuperTrend flip (appended draw, GOLD-02 stream law)

The vocab-66 SuperTrend implemented-flip (crate 0.5.0) made bits 64/65 real:
version 4 APPENDS the ``supertrend_x2_paisa`` input draw STRICTLY AFTER every
pre-existing per-case RNG draw (the VWAP burn stays consumed in its historical
position), so every version-3-era input field is byte-identical on
seed-identical cases and bits 0-63 of every mask are unchanged — pinned per
case by the migration harness against the committed
``vocab_golden_migration_v3_masks.json`` companion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from brutex.data.atomic_io import atomic_write_simple
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

_OUT_PATH = Path(__file__).resolve().parents[1] / "tests/golden/fixtures/vocab_golden_vectors.json"

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


def _build_case(
    rng: random.Random, case_id: int
) -> tuple[dict[str, Any], IndicatorSnapshot, ProximityParams, PriorBarHistory]:
    """Build one deterministic case (version-3 draw stream, byte-frozen).

    Consumes EXACTLY the historical (version-3) per-case RNG draws — the
    version-4 SuperTrend draw does NOT live here (it would shift every later
    case's stream); :func:`_append_supertrend` draws it AFTER the whole
    400-case historical stream (GOLD-02). Returns the case dict (mask not yet
    final) plus the rebuilt evaluator inputs for the second pass.
    """
    # Previous-day anchors (drive pivots + Fib + CPR); keep pdh > pdl, pdc inside.
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

    # RETIRED VWAP draw — BURNED, never fed to the evaluator (GOLD-02): the
    # exact historical conditional pair of RNG draws is still consumed here so
    # the seeded stream feeding every LATER draw (market structure, tod, prox,
    # prior) is unshifted and the 400 case INPUTS stay byte-identical to the
    # pre-reshape frozen fixture. The value is recorded as the INERT
    # ``vwap_paisa`` fixture field (``IndicatorSnapshot`` has no VWAP field;
    # bits 52/53 are tombstones the evaluator never sets).
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
        pivots=pivots,
        fib=fib,
        day_high_paisa=day_high,
        day_low_paisa=day_low,
        open_of_day_paisa=open_of_day,
        market_structure=market_structure,
    )
    mask = evaluate_atoms(snap, prox, prior)

    case: dict[str, Any] = {
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
        # (Re-stamped by ``_append_supertrend`` once the ST draw is folded in —
        # bits 0-63 provably unchanged: the ST line touches only bits 64/65.)
        "expected_mask": f"{mask:0{_MASK_HEX_WIDTH}x}",
    }
    return case, snap, prox, prior


def _append_supertrend(
    rng: random.Random,
    case: dict[str, Any],
    snap: IndicatorSnapshot,
    prox: ProximityParams,
    prior: PriorBarHistory,
) -> dict[str, Any]:
    """Draw the version-4 SuperTrend input + re-stamp the case's mask.

    The vocab-66 SuperTrend implemented-flip (crate 0.5.0): the
    ``supertrend_x2_paisa`` draw continues the SAME seeded stream STRICTLY
    AFTER the entire historical 400-case draw sequence (GOLD-02 — the
    version-3-era draws are consumed untouched first, so every pre-existing
    input field is byte-identical on seed-identical cases and bits 0-63 of
    every mask are unchanged; the migration harness pins both per case).

    ~12% ``None`` (warm-up — both bits clear), ~8% EXACT equality with
    close×2 (the strict-inequality law leaves both bits clear), else a random
    offset around close×2 so bit 64 (close×2 > line) AND bit 65
    (close×2 < line) each fire AND clear across the 400 cases.
    """
    close = case["close_paisa"]
    st_roll = rng.random()
    if st_roll < 0.12:
        supertrend_x2: int | None = None
    elif st_roll < 0.20:
        supertrend_x2 = 2 * close
    else:
        supertrend_x2 = max(1, 2 * close + rng.randint(-80_000, 80_000))
    case["supertrend_x2_paisa"] = supertrend_x2
    mask = evaluate_atoms(replace(snap, supertrend_x2_paisa=supertrend_x2), prox, prior)
    case["expected_mask"] = f"{mask:0{_MASK_HEX_WIDTH}x}"
    return case


def generate() -> dict[str, Any]:
    """Build the full deterministic fixture payload."""
    rng = random.Random(_SEED)  # noqa: S311 — deterministic fixture generation, not crypto
    # Pass 1: the byte-frozen historical (version-3) draw stream, untouched.
    built = [_build_case(rng, i) for i in range(_N_RANDOM)]
    # Pass 2: the version-4 SuperTrend draws continue the SAME stream strictly
    # AFTER the whole historical sequence (GOLD-02 — appending them inside
    # pass 1 would shift every later case's draws).
    cases = [_append_supertrend(rng, case, snap, prox, prior) for case, snap, prox, prior in built]
    # Coverage audit over the 64 ACTIVE bits (IMPLEMENTED_MASK = bits 0-51 +
    # 54-65): every IMPLEMENTED bit must be set in >=1 case AND clear in >=1
    # case, so the oracle exercises every condition the evaluator actually
    # computes both ways. The CPR bits 60-63 derive from the prev-day anchors
    # (60-62 partition every warm case; 63 fires whenever pdc sits close
    # enough to the pdh/pdl midpoint that the CPR width is <= 30 bps of P).
    # The SuperTrend bits 64/65 fire off the appended ``supertrend_x2_paisa``
    # draw (strict close×2-vs-line inequality; None/equality → both clear)
    # and joined the both-ways set at the vocab-66 implemented-flip. The
    # NON-implemented bits — the RETIRED VWAP tombstones 52/53 and every
    # reserved bit >= 66 — are excluded from the both-ways gate and must
    # NEVER be set in any case (the must-never-set gate below).
    set_seen = 0
    clr_seen = 0
    stray = 0
    for c in cases:
        mask = int(c["expected_mask"], 16)
        set_seen |= mask
        clr_seen |= (~mask) & IMPLEMENTED_MASK
        stray |= mask & ~IMPLEMENTED_MASK
    full = (1 << TOTAL_CONDITIONS) - 1
    return {
        # version 4: the vocab-66 SuperTrend implemented-flip (crate 0.5.0) —
        # bits 64/65 live, the ``supertrend_x2_paisa`` input draw APPENDED
        # strictly after every pre-existing draw so the version-3-era input
        # fields are byte-identical on seed-identical cases and bits 0-63 of
        # every mask are unchanged (the migration invariant, pinned against
        # the ``vocab_golden_migration_v3_masks.json`` companion). Version 3
        # was the 60→66 reshape (VWAP 52/53 tombstoned — ``vwap_paisa`` an
        # inert RNG-burn record — CPR 60-63 implemented); version 2 the
        # u64→u128 hex-string move.
        "version": 4,
        "generated_from": "brutex.vocabulary.evaluator.evaluate_atoms",
        "seed": _SEED,
        "total_conditions": TOTAL_CONDITIONS,
        "implemented_mask": f"{IMPLEMENTED_MASK:0{_MASK_HEX_WIDTH}x}",
        "n_cases": len(cases),
        "bits_set_somewhere": f"{set_seen:0{_MASK_HEX_WIDTH}x}",
        "bits_clear_somewhere": f"{clr_seen:0{_MASK_HEX_WIDTH}x}",
        "bits_stray_nonimplemented": f"{stray:0{_MASK_HEX_WIDTH}x}",
        "full_mask": f"{full:0{_MASK_HEX_WIDTH}x}",
        "cases": cases,
    }


def render(payload: dict[str, Any]) -> str:
    """Serialise the payload to the canonical on-disk JSON text."""
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def coverage_ok(payload: dict[str, Any]) -> bool:
    """The both-ways gate over the implemented bits + the must-never-set gate."""
    set_ok = int(payload["bits_set_somewhere"], 16) == IMPLEMENTED_MASK
    clr_ok = int(payload["bits_clear_somewhere"], 16) == IMPLEMENTED_MASK
    stray_ok = int(payload["bits_stray_nonimplemented"], 16) == 0
    print(f"all implemented bits set-somewhere : {set_ok}")
    print(f"all implemented bits clear-somewhere: {clr_ok}")
    print(f"no non-implemented bit ever set     : {stray_ok}")
    return set_ok and clr_ok and stray_ok


def main(argv: list[str] | None = None) -> int:
    """Verify coverage FIRST, then atomically write (or ``--check`` staleness)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the on-disk fixture differs from a fresh generation",
    )
    args = parser.parse_args(argv)
    payload = generate()
    fresh = render(payload)
    if not coverage_ok(payload):
        # GOLD-03: the coverage verdict gates the WRITE — a coverage-failing
        # generation must never land on disk (nor pass --check).
        print("COVERAGE FAILED — fixture NOT written", file=sys.stderr)
        return 1
    if args.check:
        current = _OUT_PATH.read_text(encoding="utf-8") if _OUT_PATH.exists() else ""
        if current != fresh:
            sys.stderr.write(
                f"{_OUT_PATH} is stale — re-run `python {Path(__file__).name}` to regenerate.\n"
            )
            return 1
        sha = hashlib.sha256(fresh.encode("utf-8")).hexdigest()
        print(f"OK: fixture up to date ({payload['n_cases']} cases, sha256={sha})")
        return 0
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (tmp + fsync + os.replace) — a crash mid-write can never
    # leave a torn fixture behind (GOLD-03).
    atomic_write_simple(_OUT_PATH, lambda tmp: tmp.write_text(fresh, encoding="utf-8"))
    sha = hashlib.sha256(fresh.encode("utf-8")).hexdigest()
    print(f"wrote {payload['n_cases']} cases → {_OUT_PATH}")
    print(f"sha256={sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
