"""Generate the frozen twin-state golden fixture (`twin_state_golden_vectors.json`).

R2-RC-F2 (goldens for the STATEFUL indicator states): the frozen 400-case
`vocab_golden_vectors.json` answer-key pins the EVALUATOR bits, not the
per-bar `.update()` derivation of the two stateful indicator machines —
`VwapState` (session-anchored VWAP, bits 52/53) and `MarketStructureState`
(BOS/CHoCH, bits 56-59). Production (`brutex/engine/stage1a.py`) drives ONLY
the Rust `trading_core` classes. This fixture froze the two machines'
per-bar contract while hand-synced Python twins still mirrored them in
`brutex/indicators/` (DEC-SHARED-CORE-001 / ADR-0012 D7); the twins were
deleted by DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 and the SAME frozen
fixture (never regenerated) now pins the single Rust source.

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

* VWAP: session open/close transitions (IST 09:15 reset via a new day ordinal),
  the 09:00-09:15 pre-open auction exclusion, zero/negative-volume skips,
  contract-roll (`session_key`) resets, warm same-day time-of-day rewinds,
  cold (not-warm) rewinds that must NOT reset, and large-magnitude bars at the
  FFI ceiling `MAX_BAR_PAISA = i64::MAX / 65536` whose running `cum_pv` far
  exceeds i64 (the i128-accumulator / unbounded-int truncating-division seam).
  Every stream keeps `typical_paisa >= 0` BY CONTRACT, not by accident: a
  negative typical is REJECTED with `ValueError` at the PyO3
  `VwapState.update` boundary (historically also by the deleted Python
  twin's mirror guard) because the trunc-vs-floor division semantics
  genuinely diverged there (Rust i128 `/` truncates toward zero, Python `//`
  floors — e.g. bars (-7, 1) then (0, 1) gave -3 vs -4 pre-guard). The
  rejection itself is pinned by `tests/unit/test_trading_core_ffi_guards.py`,
  so the closed domain hole cannot silently reopen.
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
# The PyO3 FFI overflow guard on VwapState.update (python.rs `MAX_BAR_PAISA`).
MAX_BAR_PAISA = I64_MAX // 65536
# 2026-01-05 (a Monday) — date.toordinal() == the Rust twin's i64 day ordinal.
DAY0 = 739621
OPEN_TOD = 9 * 3600 + 15 * 60  # 09:15:00 IST session open
PRE_OPEN_TOD = 9 * 3600  # 09:00:00 IST pre-open auction start
CLOSE_TOD = 15 * 3600 + 30 * 60  # 15:30:00 IST session close

_OUT = Path(__file__).parent / "fixtures" / "twin_state_golden_vectors.json"


def _vwap_bar(
    typical: int,
    volume: int,
    *,
    pre_open: bool = False,
    key: int = 1,
    day: int = DAY0,
    tod: int = OPEN_TOD,
) -> dict[str, Any]:
    """One VWAP input bar (expected output filled in later from Rust)."""
    return {
        "typical_paisa": typical,
        "volume": volume,
        "is_pre_open": pre_open,
        "session_key": key,
        "session_day_ordinal": day,
        "tod_seconds": tod,
    }


def _vwap_scripted_session_edges() -> list[dict[str, Any]]:
    """Hand-scripted VWAP stream hitting every reset/skip branch."""
    return [
        # Day 1: pre-open auction volume must be excluded (huge print, no bias).
        _vwap_bar(99_999_00, 1_000_000, pre_open=True, tod=PRE_OPEN_TOD),
        _vwap_bar(50_000_00, 2_000_000, pre_open=True, tod=PRE_OPEN_TOD + 300),
        # 09:15 open bar with zero volume → still warming (None).
        _vwap_bar(24_000_00, 0, tod=OPEN_TOD),
        # First counted volume.
        _vwap_bar(24_000_00, 100, tod=OPEN_TOD + 60),
        _vwap_bar(24_010_50, 300, tod=OPEN_TOD + 120),
        # A mid-session pre-open-flagged bar is skipped (never counted).
        _vwap_bar(1_00, 999_999, pre_open=True, tod=OPEN_TOD + 180),
        # Negative volume is skipped like zero.
        _vwap_bar(24_020_00, -5, tod=OPEN_TOD + 240),
        # A zero typical price still counts its volume (drags the VWAP down).
        _vwap_bar(0, 50, tod=OPEN_TOD + 300),
        # Last bar of the session at 15:30.
        _vwap_bar(23_990_25, 777, tod=CLOSE_TOD),
        # Day 2: the new IST day ordinal resets the session.
        _vwap_bar(25_000_00, 10, day=DAY0 + 1, tod=OPEN_TOD),
        _vwap_bar(25_100_00, 30, day=DAY0 + 1, tod=OPEN_TOD + 60),
        # Warm same-day time-of-day REWIND → reset (a re-opened session).
        _vwap_bar(10_000_00, 40, day=DAY0 + 1, tod=OPEN_TOD + 30),
        # Contract roll (session_key change) mid-day → reset.
        _vwap_bar(11_000_00, 60, key=2, day=DAY0 + 1, tod=OPEN_TOD + 90),
        _vwap_bar(11_500_00, 60, key=2, day=DAY0 + 1, tod=OPEN_TOD + 150),
        # Day 3, new key: pre-open-only bars leave the state COLD, so the
        # following tod rewind must NOT reset (the `warm and` guard).
        _vwap_bar(12_000_00, 500, pre_open=True, key=3, day=DAY0 + 2, tod=PRE_OPEN_TOD + 600),
        _vwap_bar(12_000_00, 500, pre_open=True, key=3, day=DAY0 + 2, tod=PRE_OPEN_TOD + 60),
        _vwap_bar(12_345_00, 7, key=3, day=DAY0 + 2, tod=OPEN_TOD),
        # Day 4 opens at a LATER time-of-day than day 3's last bar (a data hole
        # at the open), so the day-ordinal change is the ONLY reset trigger —
        # the tod-rewind branch must not be needed to shadow it.
        _vwap_bar(20_000_00, 11, key=3, day=DAY0 + 3, tod=OPEN_TOD + 3600),
        _vwap_bar(20_500_00, 33, key=3, day=DAY0 + 3, tod=OPEN_TOD + 3660),
        # Day 5 opens at the EXACT same time-of-day as day 4's last bar (an
        # equal tod is NOT a rewind — strict `<`), again day-branch-only.
        _vwap_bar(30_000_00, 5, key=3, day=DAY0 + 4, tod=OPEN_TOD + 3660),
    ]


def _vwap_large_magnitude() -> list[dict[str, Any]]:
    """FFI-ceiling bars: cum_pv exceeds i64::MAX by orders of magnitude.

    40 bars of `MAX_BAR_PAISA × MAX_BAR_PAISA` push cum_pv to ~8·10^29 — far
    past i64::MAX (~9.2·10^18) — exercising the Rust i128 accumulator against
    Python's unbounded ints, and the truncating `cum_pv / cum_vol` narrowing.
    """
    bars = [_vwap_bar(MAX_BAR_PAISA, MAX_BAR_PAISA, tod=OPEN_TOD + i * 60) for i in range(40)]
    # Asymmetric extremes: max price × 1 lot, then 1 paisa × max volume — the
    # division now has a huge remainder (truncation direction is load-bearing).
    bars.append(_vwap_bar(MAX_BAR_PAISA, 1, tod=OPEN_TOD + 40 * 60))
    bars.append(_vwap_bar(1, MAX_BAR_PAISA, tod=OPEN_TOD + 41 * 60))
    bars.append(_vwap_bar(3, 7, tod=OPEN_TOD + 42 * 60))
    return bars


def _vwap_unit_volume_precision(rnd: random.Random) -> list[dict[str, Any]]:
    """Volume-1 bars: any per-bar off-by-one in ``cum_pv``/``cum_vol`` moves the
    truncated quotient (``(Σ + n) // n == Σ // n + 1`` exactly), so the goldens
    pin the accumulation at single-paisa precision — the huge-volume streams
    alone cannot (a +1 on a 10^9 divisor almost never crosses a floor
    boundary)."""
    return [_vwap_bar(rnd.randint(1, 999), 1, tod=OPEN_TOD + i * 60) for i in range(12)]


def _vwap_random_multi_session(rnd: random.Random) -> list[dict[str, Any]]:
    """Seeded random stream: 3 sessions × 60 bars, mixed magnitudes + a roll."""
    bars: list[dict[str, Any]] = []
    key = 10
    for day_off in range(3):
        day = DAY0 + 7 + day_off
        # Two pre-open auction bars per session.
        for j in range(2):
            bars.append(
                _vwap_bar(
                    rnd.randint(1, 30_000_00),
                    rnd.randint(0, 1_000_000),
                    pre_open=True,
                    key=key,
                    day=day,
                    tod=PRE_OPEN_TOD + j * 300,
                )
            )
        for i in range(60):
            bucket = rnd.random()
            if bucket < 0.70:
                typical = rnd.randint(1_00, 90_000_00)  # normal futures magnitude
            elif bucket < 0.90:
                typical = rnd.randint(90_000_00, 10**12)  # stress magnitude
            else:
                typical = rnd.randint(10**12, MAX_BAR_PAISA)  # FFI-ceiling region
            volume = 0 if rnd.random() < 0.08 else rnd.randint(1, 10**9)
            if day_off == 1 and i == 30:
                key += 1  # one mid-session contract roll
            bars.append(_vwap_bar(typical, volume, key=key, day=day, tod=OPEN_TOD + i * 375))
    return bars


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


def _fill_vwap_expected(streams: list[dict[str, Any]]) -> None:
    """Compute every expected VWAP from the Rust production twin."""
    for stream in streams:
        state = trading_core.VwapState()
        for bar in stream["bars"]:
            bar["expected_vwap_paisa"] = state.update(
                bar["typical_paisa"],
                bar["volume"],
                bar["is_pre_open"],
                bar["session_key"],
                bar["session_day_ordinal"],
                bar["tod_seconds"],
            )


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
    vwap_streams: list[dict[str, Any]] = [
        {"id": "vwap_scripted_session_edges", "bars": _vwap_scripted_session_edges()},
        {"id": "vwap_large_magnitude_i64_edges", "bars": _vwap_large_magnitude()},
        {"id": "vwap_unit_volume_precision", "bars": _vwap_unit_volume_precision(rnd)},
        {"id": "vwap_random_multi_session", "bars": _vwap_random_multi_session(rnd)},
    ]
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
    _fill_vwap_expected(vwap_streams)
    _fill_ms_expected(ms_streams)

    doc = {
        "schema": "twin-state-golden-v1",
        "description": (
            "Language-neutral golden vectors for the stateful indicator twins: "
            "trading_core VwapState / MarketStructureState (Rust, PRODUCTION — "
            "expected values computed from it) vs brutex.indicators (Python "
            "mirrors). session_day_ordinal is the proleptic-Gregorian ordinal "
            "(Python date.toordinal() == the Rust i64 day ordinal). "
            "expected_signal is [bos_bullish, bos_bearish, choch_bullish, "
            "choch_bearish]."
        ),
        "seed": SEED,
        "generated_with_crate_version": trading_core.__build_crate_version__,
        "generator": "tests/golden/generate_twin_state_golden.py",
        "vwap_streams": vwap_streams,
        "market_structure_streams": ms_streams,
    }
    text = json.dumps(doc, indent=1) + "\n"
    _OUT.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    total_vwap = sum(len(s["bars"]) for s in vwap_streams)
    total_ms = sum(len(s["bars"]) for s in ms_streams)
    print(f"wrote {_OUT} ({total_vwap} vwap bars, {total_ms} ms bars)")
    print(f"sha256: {digest}")


if __name__ == "__main__":
    main()
