"""Bit-identity golden gate: the production shim's marshalling reproduces the
frozen oracle, cross-checked against an INDEPENDENT Rust recompute.

The BruteX vocabulary shim (``brutex.vocabulary.evaluator.evaluate_atoms``) has
NO Python implementation of the 52 conditions — the condition logic lives ONCE
in the shared Rust ``trading_core`` core (``DEC-SHARED-CORE-001`` #441, CLAUDE.md
§11 COMMON). The shim's only job is to MARSHAL a Python ``IndicatorSnapshot`` /
``ProximityParams`` / ``PriorBarHistory`` into the flat ABI of
``trading_core.evaluate_with_levels`` (the PRE-BUILT-levels entry point). So
comparing ``evaluate_atoms`` to ``evaluate_with_levels`` alone is near-tautological
— ``evaluate_atoms`` simply calls it.

This file EARNS its place by welding THREE views into ONE per-case, per-bit
equality gate over the 400 frozen vectors:

1. ``evaluate_atoms`` — the production shim (marshals pre-built levels into Rust).
2. ``evaluate_with_levels`` — the Rust entry point the shim delegates to, called
   directly (proves the shim's marshalling is faithful, not silently transposing).
3. ``evaluate_from_raw`` — the GENUINELY INDEPENDENT recompute path: it takes the
   previous day's raw ``pdh/pdl/pdc`` and recomputes the pivot + Fibonacci levels
   INSIDE Rust, rather than consuming the shim's Python-built levels. Agreement
   here proves the shim's Python ``compute_pivot_levels`` / ``compute_fib_levels``
   produce bit-identical levels to the Rust recompute — a real cross-check, not a
   re-derivation of the same code.
4. ``expected_mask`` — the frozen oracle answer-key (a u128 hex string).

For every golden case the four must agree bit-for-bit
(``evaluate_atoms == evaluate_with_levels == evaluate_from_raw == expected``), and
the per-bit sweep additionally proves every IMPLEMENTED condition decodes
identically across all four (both set and clear across the fixture union; the
LAYOUT-ONLY bits are proven inert by ``test_vocab_golden``).

The fixture is loaded via the existing ``test_vocab_golden`` helpers (the ONE
loader — no duplicated parser, CLAUDE.md §11 COMMON).

Whitebox: reuses the sibling golden module's private ``_CASES`` / ``_rebuild`` (the
ONE fixture loader, not a duplicated parser), so per the DEC-P0-017 convention this
file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import datetime
from typing import Any

import trading_core
from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.pivots import compute_pivot_levels
from brutex.vocabulary import NAME_TO_BIT
from brutex.vocabulary.bits import IMPLEMENTED_MASK
from brutex.vocabulary.evaluator import evaluate_atoms

from tests.golden.test_vocab_golden import _CASES, _expected, _rebuild


def _tod_seconds(ts_iso: str) -> int:
    """IST wall-clock seconds-of-day (the shim's Group-8 marshalling)."""
    ts = datetime.fromisoformat(ts_iso)
    return ts.hour * 3600 + ts.minute * 60 + ts.second


def _rust_with_levels(case: dict[str, Any]) -> int:
    """Drive ``evaluate_with_levels`` over Python-built levels (the shim's path)."""
    day = datetime.fromisoformat(str(case["timestamp_ist"])).date()
    pv = compute_pivot_levels(
        day, case["prev_pdh_paisa"], case["prev_pdl_paisa"], case["prev_pdc_paisa"]
    )
    fb = compute_fib_levels(day, case["prev_pdh_paisa"], case["prev_pdl_paisa"])
    pivot_levels = [
        pv.p_paisa,
        pv.r1_paisa,
        pv.r2_paisa,
        pv.r3_paisa,
        pv.s1_paisa,
        pv.s2_paisa,
        pv.s3_paisa,
        pv.pdh_paisa,
        pv.pdl_paisa,
        pv.pdc_paisa,
        pv.r5_paisa,
        pv.s5_paisa,
    ]
    fib_levels = [
        fb.retrace_level_at_pdh_paisa,
        fb.retrace_23_paisa,
        fb.retrace_38_paisa,
        fb.retrace_50_paisa,
        fb.retrace_62_paisa,
        fb.retrace_78_paisa,
        fb.retrace_level_at_pdl_paisa,
        fb.ext_up_127_paisa,
        fb.ext_up_162_paisa,
        fb.ext_up_200_paisa,
        fb.ext_up_262_paisa,
        fb.ext_down_127_paisa,
        fb.ext_down_162_paisa,
        fb.ext_down_200_paisa,
        fb.ext_down_262_paisa,
    ]
    proximity = case["proximity"]
    prior = case["prior"]
    result: int = trading_core.evaluate_with_levels(
        _tod_seconds(str(case["timestamp_ist"])),
        case["open_paisa"],
        case["high_paisa"],
        case["low_paisa"],
        case["close_paisa"],
        case["ema20_paisa"],
        case["ema200_paisa"],
        pivot_levels,
        fib_levels,
        case["day_high_paisa"],
        case["day_low_paisa"],
        case["open_of_day_paisa"],
        proximity["pivot_bps"],
        proximity["pdh_pdl_bps"],
        proximity["fib_bps"],
        prior["prior_n"],
        prior["flags"],
        case["market_structure"]["bos_bullish"],
        case["market_structure"]["bos_bearish"],
        case["market_structure"]["choch_bullish"],
        case["market_structure"]["choch_bearish"],
        case["supertrend_x2_paisa"],
    )
    return result


def _rust_from_raw(case: dict[str, Any]) -> int:
    """Drive ``evaluate_from_raw`` — the INDEPENDENT path that recomputes the
    pivot + Fibonacci levels inside Rust from the previous day's raw
    ``pdh/pdl/pdc`` (it does NOT consume the shim's pre-built levels)."""
    proximity = case["proximity"]
    prior = case["prior"]
    result: int = trading_core.evaluate_from_raw(
        _tod_seconds(str(case["timestamp_ist"])),
        case["open_paisa"],
        case["high_paisa"],
        case["low_paisa"],
        case["close_paisa"],
        case["ema20_paisa"],
        case["ema200_paisa"],
        case["prev_pdh_paisa"],
        case["prev_pdl_paisa"],
        case["prev_pdc_paisa"],
        case["day_high_paisa"],
        case["day_low_paisa"],
        case["open_of_day_paisa"],
        proximity["pivot_bps"],
        proximity["pdh_pdl_bps"],
        proximity["fib_bps"],
        prior["prior_n"],
        prior["flags"],
        case["market_structure"]["bos_bullish"],
        case["market_structure"]["bos_bearish"],
        case["market_structure"]["choch_bullish"],
        case["market_structure"]["choch_bearish"],
        case["supertrend_x2_paisa"],
    )
    return result


def test_shim_rust_recompute_and_oracle_agree_on_every_vector() -> None:
    """For all 400 cases: shim == with_levels == from_raw == frozen oracle.

    Four-way bit-for-bit agreement — the shim's marshalling, the pre-built-levels
    entry point it delegates to, the independent in-Rust recompute, and the frozen
    ``expected_mask`` answer-key — so a regression in the shim's marshalling, the
    Python level-builders, or the Rust core fails as a golden test.
    """
    mismatches: list[str] = []
    for case in _CASES:
        snapshot, proximity, prior = _rebuild(case)
        shim = evaluate_atoms(snapshot, proximity, prior)
        with_levels = _rust_with_levels(case)
        from_raw = _rust_from_raw(case)
        expected = _expected(case)
        if not (shim == with_levels == from_raw == expected):
            mismatches.append(
                f"case {case['id']}: shim={shim:#034x} with_levels={with_levels:#034x} "
                f"from_raw={from_raw:#034x} expected={expected:#034x}"
            )
    assert not mismatches, "shim↔with_levels↔from_raw↔oracle divergence:\n" + "\n".join(
        mismatches[:20]
    )


def test_every_condition_bit_agrees_across_all_paths() -> None:
    """Every one of the 52 conditions decodes identically across all four views.

    Beyond the whole-mask equality above, this asserts PER-BIT agreement for every
    condition name across every golden case — so a single condition can never
    silently diverge between the shim, either Rust entry point, and the oracle. The
    both-ways union is checked over the IMPLEMENTED bits only (the LAYOUT-ONLY bits
    are proven always-clear by ``test_vocab_golden``).
    """
    set_seen = 0
    clear_seen = 0
    per_bit_mismatches: list[str] = []
    for case in _CASES:
        snapshot, proximity, prior = _rebuild(case)
        shim = evaluate_atoms(snapshot, proximity, prior)
        with_levels = _rust_with_levels(case)
        from_raw = _rust_from_raw(case)
        expected = _expected(case)
        for name, bit in NAME_TO_BIT.items():
            shim_bit = (shim >> bit) & 1
            with_bit = (with_levels >> bit) & 1
            raw_bit = (from_raw >> bit) & 1
            exp_bit = (expected >> bit) & 1
            if not (shim_bit == with_bit == raw_bit == exp_bit):
                per_bit_mismatches.append(
                    f"case {case['id']} bit {name}: shim={shim_bit} with_levels={with_bit} "
                    f"from_raw={raw_bit} expected={exp_bit}"
                )
        set_seen |= shim
        clear_seen |= (~shim) & IMPLEMENTED_MASK
    assert not per_bit_mismatches, "per-bit divergence:\n" + "\n".join(per_bit_mismatches[:20])
    # Every IMPLEMENTED bit is exercised both ways across the fixture (name↔bit).
    assert set_seen == IMPLEMENTED_MASK, "an implemented condition is never set across the fixture"
    assert clear_seen == IMPLEMENTED_MASK, (
        "an implemented condition is never clear across the fixture"
    )
