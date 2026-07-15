"""Generate the frozen twin-state golden fixture (`twin_state_golden_vectors.json`).

R2-RC-F2 (goldens for the STATEFUL indicator states): the frozen 400-case
`vocab_golden_vectors.json` answer-key pins the EVALUATOR bits, not the
per-bar `.update()` derivation of the stateful `MarketStructureState`
machine (BOS/CHoCH, bits 56-59). Production (`brutex/engine/stage1a.py`)
drives ONLY the Rust `trading_core` class. This fixture froze the machine's
per-bar contract while a hand-synced Python twin still mirrored it in
`brutex/indicators/` (DEC-SHARED-CORE-001 / ADR-0012 D7); the twin was
deleted by DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 and the frozen fixture
now pins the single Rust source.

The former `VwapState` half of this golden was RETIRED with the bits 52-53
VWAP tombstone (the VWAP/volume wipe — the spot-only engine has no traded
volume; the Rust `VwapState` itself was deleted from the crate at 0.4.0),
so this generator now emits ONLY the market-structure streams. The
MarketStructureState core is byte-untouched by the wipe, so a regeneration
reproduces the frozen fixture's MS half bit-for-bit (a migration
invariant the regen run must verify).

HOW THE EXPECTED VALUES ARE COMPUTED (Golden Rule #1):

Every per-bar `expected_*` value is produced by the **Rust production state**
(the installed `trading_core` PyO3 wheel — the code path `stage1a` actually
runs), never hand-authored. The consuming test
(`tests/unit/test_twin_state_golden_parity.py`) replays the SAME bar streams
through the production pyclasses and asserts every per-bar output is
bit-identical to the frozen expectation — so a drift in the Rust core, the
PyO3 boundary, or a stale wheel fails CI.

The bar streams are a fixed-seed (`random.Random(SEED)`) mix of scripted edge
sequences + seeded random streams covering:

* Market structure: k ∈ {1, 2, 3}, the k-bar fractal confirmation delay, a
  break landing on the exact confirming bar, tied-extreme windows that confirm
  nothing (strict-inequality seam), flat windows, engulfing both-side breaks,
  full BOS→CHoCH trend-flip sequences, and i64-extreme bar levels.

The file is LANGUAGE-NEUTRAL (plain JSON, ints/bools/nulls only; `session_day`
is the proleptic-Gregorian ordinal shared by Python `date.toordinal()` and the
Rust twin's `i64` day ordinal) so TickVault's native Rust side can replay the
same contract.

Regenerate (deterministic — requires the built `trading_core` wheel):

    .venv/bin/python tests/golden/generate_twin_state_golden.py

then update `_EXPECTED_SHA256` in `tests/unit/test_twin_state_golden_parity.py`.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import trading_core

SEED = 20260703
I64_MAX = 2**63 - 1
I64_MIN = -(2**63)

_OUT = Path(__file__).parent / "fixtures" / "twin_state_golden_vectors.json"


def _ms_bar(high: int, low: int, close: int) -> dict[str, Any]:
    """One market-structure input bar (expected filled in later from Rust)."""
    return {"high": high, "low": low, "close": close}


def _ms_scripted_trend_flips_k2() -> list[dict[str, Any]]:
    """Scripted k=2 stream: confirmed swings → BOS both ways → CHoCH both ways."""
    seq = [
        # Build + confirm a swing high at 130_00 (5-bar fractal).
        (100_00, 90_00, 95_00),
        (110_00, 100_00, 105_00),
        (130_00, 120_00, 125_00),  # swing-high candidate
        (118_00, 95_00, 100_00),
        (116_00, 85_00, 90_00),  # confirms swing high 130_00
        # Build + confirm a swing low at 70_00.
        (108_00, 70_00, 75_00),  # swing-low candidate
        (112_00, 100_00, 105_00),
        (114_00, 102_00, 108_00),  # confirms swing low 70_00
        # Break the low from Trend::None → bearish BOS, trend → Down.
        (90_00, 60_00, 65_00),
        # Break the high while DOWN → bullish CHoCH, trend → Up.
        (140_00, 120_00, 135_00),
        # Still above the swing high while UP → bullish BOS (level predicate).
        (142_00, 122_00, 138_00),
        # Take out the swing low while UP → bearish CHoCH, trend → Down.
        (88_00, 58_00, 62_00),
        # Hold below while DOWN → bearish BOS.
        (80_00, 55_00, 60_00),
    ]
    return [_ms_bar(*b) for b in seq]


def _ms_confirm_edge_break_on_confirming_bar_k2() -> list[dict[str, Any]]:
    """The k-bar confirmation EDGE: the confirming bar itself breaks the swing.

    Bar 4 completes the 5-bar window (confirming bar 2's high 130_00 — its own
    high 126_00 stays strictly below) AND closes at 131_00 > 130_00, so the BOS
    must fire on the exact confirming update, not one bar later.
    """
    seq = [
        (100_00, 90_00, 95_00),
        (110_00, 100_00, 105_00),
        (130_00, 120_00, 125_00),
        (115_00, 105_00, 110_00),
        (126_00, 102_00, 131_00),  # confirms 130_00 AND closes above it
        # Confirm a swing low, hold above the high, then reverse through the low.
        (128_00, 101_00, 103_00),
        (129_00, 104_00, 106_00),  # confirms swing low 101_00 (bar idx 5)
        (150_00, 95_00, 149_00),  # engulfing: close > 130_00 high (bull side)
        (151_00, 90_00, 94_00),  # close < 101_00 low while UP → bearish CHoCH
    ]
    return [_ms_bar(*b) for b in seq]


def _ms_confirmation_vetoed_by_kth_bar_k2() -> list[dict[str, Any]]:
    """The other side of the k-bar confirmation edge: the k-th future bar VETOES
    the swing (its high ties/exceeds the centre's), so NO level exists and the
    final close above the would-be swing must yield NO signal. An
    early-confirming twin (one that latches the centre before all k future bars
    arrive) would fire a phantom bullish BOS on the last bar."""
    seq = [
        (100_00, 90_00, 95_00),
        (110_00, 100_00, 105_00),
        (130_00, 120_00, 125_00),  # swing-high candidate 130_00
        (118_00, 108_00, 112_00),  # first future bar stays below
        (135_00, 115_00, 132_00),  # 2nd (k-th) future bar VETOES (135 ≥ 130)
    ]
    return [_ms_bar(*b) for b in seq]


def _ms_engulf_both_sides_k2() -> list[dict[str, Any]]:
    """A single bar that breaks BOTH sides (doc: 'a large engulfing bar can read
    one of each'): a swing LOW at 140_00 is confirmed first, the tape then falls
    (bearish BOS → trend Down) and confirms a LOWER swing HIGH at 130_00, so a
    close at 135_00 sits above the swing high AND below the swing low — bullish
    CHoCH (entry trend Down) + bearish BOS on the SAME bar. Pins the
    entry-trend-vs-mutated-trend seam of the bearish branch (the bullish break
    flips the trend Up mid-bar; the bearish classification must still read the
    ENTRY trend)."""
    seq = [
        (152_00, 146_00, 150_00),
        (154_00, 144_00, 149_00),
        (150_00, 140_00, 143_00),  # swing-low candidate 140_00
        (149_00, 143_00, 146_00),
        (150_00, 145_00, 148_00),  # confirms swing low 140_00
        (147_00, 138_00, 139_00),  # close < 140_00 → bearish BOS, trend → Down
        (129_00, 124_00, 126_00),
        (127_00, 120_00, 122_00),
        (130_00, 123_00, 126_00),  # swing-high candidate 130_00 (a lower high)
        (126_00, 119_00, 121_00),
        (125_00, 118_00, 120_00),  # confirms swing high 130_00
        (137_00, 128_00, 135_00),  # ENGULF: choch_bullish + bos_bearish together
    ]
    return [_ms_bar(*b) for b in seq]


def _ms_tied_extremes_no_confirm_k2() -> list[dict[str, Any]]:
    """Tied window extremes: strict `<` / `>` means NO swing is confirmed."""
    seq = [
        (130_00, 90_00, 95_00),
        (110_00, 100_00, 105_00),
        (130_00, 90_00, 125_00),  # centre high TIES bar 0's high; low ties bar 0
        (115_00, 105_00, 110_00),
        (120_00, 100_00, 118_00),
        # Close far above every high — but with no confirmed swing, no signal.
        # (A twin that confirmed the TIED high 130_00 would fire a phantom BOS.)
        (200_00, 150_00, 199_00),
        # Close far below every low — again no signal (a twin that confirmed the
        # TIED low 90_00 would fire a phantom bearish BOS here).
        (95_00, 70_00, 75_00),
    ]
    return [_ms_bar(*b) for b in seq]


def _ms_flat_window_k2() -> list[dict[str, Any]]:
    """A flat tape confirms nothing and never signals."""
    return [_ms_bar(100_00, 100_00, 100_00) for _ in range(8)]


def _ms_i64_extremes_k1() -> list[dict[str, Any]]:
    """i64-edge bar levels (comparison-only machine — must not diverge)."""
    seq = [
        (I64_MAX - 2, I64_MIN + 2, 0),
        (I64_MAX, I64_MIN, 1),  # swing-high candidate at i64::MAX (k=1 centre)
        (I64_MAX - 1, I64_MIN + 1, -1),  # confirms high i64::MAX + low i64::MIN
        (I64_MAX, I64_MIN, I64_MAX),  # close == swing high → NOT a break (strict >)
        (I64_MAX, I64_MIN, I64_MIN),  # close == swing low → NOT a break (strict <)
        (I64_MAX - 3, I64_MIN + 3, I64_MIN + 1),
        (I64_MAX - 4, I64_MIN + 4, I64_MAX - 1),
    ]
    return [_ms_bar(*b) for b in seq]


def _ms_random_walk(rnd: random.Random, n: int) -> list[dict[str, Any]]:
    """Seeded random walk with occasional jumps — organic swings + breaks."""
    bars: list[dict[str, Any]] = []
    base = 24_000_00
    for _ in range(n):
        step = rnd.randint(-400_00, 400_00)
        if rnd.random() < 0.06:
            step *= rnd.randint(3, 8)  # a jump that takes out structure
        base = max(1_00, base + step)
        high = base + rnd.randint(0, 250_00)
        low = max(1, base - rnd.randint(0, 250_00))
        close = rnd.randint(low, high)
        bars.append(_ms_bar(high, low, close))
    return bars


def _burn_retired_vwap_draws(rnd: random.Random) -> None:
    """Reproduce the RNG consumption of the retired VWAP stream builders.

    The retired `_vwap_unit_volume_precision` drew 12 `randint(1, 999)`; the
    retired `_vwap_random_multi_session` drew, per each of 3 sessions: 2 × (a
    typical `randint(1, 30_000_00)` + a volume `randint(0, 1_000_000)`) for
    the pre-open bars, then 60 × (a bucket `random()`, a bucketed typical
    `randint`, a volume gate `random()` and — when the gate passes — a volume
    `randint(1, 10**9)`). Replayed here draw-for-draw so the downstream
    market-structure random walks see the SAME rng state as the frozen
    fixture's generation run.
    """
    for _ in range(12):
        rnd.randint(1, 999)
    for _day_off in range(3):
        for _ in range(2):
            rnd.randint(1, 30_000_00)
            rnd.randint(0, 1_000_000)
        for _ in range(60):
            bucket = rnd.random()
            if bucket < 0.70:
                rnd.randint(1_00, 90_000_00)
            elif bucket < 0.90:
                rnd.randint(90_000_00, 10**12)
            else:
                rnd.randint(10**12, I64_MAX // 65536)
            if rnd.random() >= 0.08:
                rnd.randint(1, 10**9)


def _fill_ms_expected(streams: list[dict[str, Any]]) -> None:
    """Compute every expected BOS/CHoCH signal from the Rust production twin."""
    for stream in streams:
        state = trading_core.MarketStructureState(stream["k"])
        for bar in stream["bars"]:
            sig = state.update(bar["high"], bar["low"], bar["close"])
            bar["expected_signal"] = list(sig)  # [bos_bull, bos_bear, choch_bull, choch_bear]


def main() -> None:
    """Build the streams, compute expecteds from Rust, freeze the JSON."""
    rnd = random.Random(SEED)
    # SEED-STREAM COMPATIBILITY: the retired VWAP builders used to consume
    # seeded draws BEFORE the market-structure random walks. To keep the MS
    # half bit-identical to the frozen fixture (the regen migration
    # invariant), burn the exact same number/shape of draws they made.
    _burn_retired_vwap_draws(rnd)
    ms_streams: list[dict[str, Any]] = [
        {"id": "ms_scripted_trend_flips_k2", "k": 2, "bars": _ms_scripted_trend_flips_k2()},
        {
            "id": "ms_confirm_edge_break_on_confirming_bar_k2",
            "k": 2,
            "bars": _ms_confirm_edge_break_on_confirming_bar_k2(),
        },
        {
            "id": "ms_confirmation_vetoed_by_kth_bar_k2",
            "k": 2,
            "bars": _ms_confirmation_vetoed_by_kth_bar_k2(),
        },
        {"id": "ms_engulf_both_sides_k2", "k": 2, "bars": _ms_engulf_both_sides_k2()},
        {"id": "ms_tied_extremes_no_confirm_k2", "k": 2, "bars": _ms_tied_extremes_no_confirm_k2()},
        {"id": "ms_flat_window_k2", "k": 2, "bars": _ms_flat_window_k2()},
        {"id": "ms_i64_extremes_k1", "k": 1, "bars": _ms_i64_extremes_k1()},
        {"id": "ms_random_walk_k1", "k": 1, "bars": _ms_random_walk(rnd, 250)},
        {"id": "ms_random_walk_k2", "k": 2, "bars": _ms_random_walk(rnd, 250)},
        {"id": "ms_random_walk_k3", "k": 3, "bars": _ms_random_walk(rnd, 250)},
    ]
    _fill_ms_expected(ms_streams)

    doc = {
        "schema": "twin-state-golden-v2",
        "description": (
            "Language-neutral golden vectors for the stateful indicator state: "
            "trading_core MarketStructureState (Rust, PRODUCTION — expected "
            "values computed from it). expected_signal is [bos_bullish, "
            "bos_bearish, choch_bullish, choch_bearish]. Schema v2: the "
            "vwap_streams half was RETIRED with the bits 52-53 VWAP tombstone "
            "(the VwapState core was deleted at crate 0.4.0); the MS streams "
            "are bit-identical to the v1 fixture's (the core is untouched — "
            "the regen run verifies it)."
        ),
        "seed": SEED,
        "generated_with_crate_version": trading_core.__build_crate_version__,
        "generator": "tests/golden/generate_twin_state_golden.py",
        "market_structure_streams": ms_streams,
    }
    text = json.dumps(doc, indent=1) + "\n"
    _OUT.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    total_ms = sum(len(s["bars"]) for s in ms_streams)
    print(f"wrote {_OUT} ({total_ms} ms bars)")
    print(f"sha256: {digest}")


if __name__ == "__main__":
    main()
