"""Cross-language Python ↔ Rust parity — the bit-identity contract anchor.

The single source of truth for the 52-condition vocabulary + the pivot/Fib
indicators is the Rust ``trading-core`` crate (``DEC-SHARED-CORE-001``,
``docs/MIGRATION.md`` #441). This test pins the two things that the golden
suite did not directly cover before the Phase-2b post-merge hostile review:

1. **The PRODUCTION entry point.** BruteX's per-bar evaluator delegates to
   ``trading_core.evaluate_with_levels`` (pre-built levels, no recompute), but
   the dedicated CI parity check (``scripts/check_vocab_rust_parity.py``) only
   exercised ``evaluate_from_raw`` (the recompute path). Here we drive
   ``evaluate_with_levels`` **directly** over every golden case, cross-check it
   against ``evaluate_from_raw``, and exercise the ``pdc == 0`` gap-day path the
   second entry point was specifically built for (absent from the 400 fixtures).

2. **The pivot/Fib wrapper faithfully delegates.** As of Phase 2d
   ``brutex.indicators.compute_pivot_levels`` / ``compute_fib_levels`` delegate
   their arithmetic to ``trading_core.compute_pivot_levels`` /
   ``compute_fib_levels`` — the Python copy of the math was REMOVED, so the math
   is single-source (Rust). This test pins the wrapper's pack order against the
   Rust tuple over a deterministic grid, so a packing/order regression fails CI.

This file supersedes the retired ``fno-math`` parity skeleton
(``DEC-PHASE1-INTEGRATION-SKELETONS-001``); the Greeks-stub operations it
referenced were removed in ``DEC-P0-025``.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime
from pathlib import Path
from typing import Any

import trading_core
from brutex.indicators.fib_levels import FibLevels, compute_fib_levels
from brutex.indicators.pivots import PivotLevels, compute_pivot_levels
from brutex.vocabulary import bits as B

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO_ROOT / "tests" / "golden" / "fixtures" / "vocab_golden_vectors.json"
_DATA: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CASES: list[dict[str, Any]] = _DATA["cases"]


def _tod_seconds(ts_iso: str) -> int:
    """IST wall-clock seconds-of-day from an ISO timestamp (offset ignored)."""
    ts = datetime.fromisoformat(ts_iso)
    return ts.hour * 3600 + ts.minute * 60 + ts.second


def _pivot_levels_10(pv: PivotLevels) -> list[int]:
    """The 10-element ``pivot_levels`` list ``evaluate_with_levels`` expects."""
    return [
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
    ]


def _fib_levels_15(fb: FibLevels) -> list[int]:
    """The 15-element ``fib_levels`` list ``evaluate_with_levels`` expects."""
    return [
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


def _with_levels(case: dict[str, Any], pivot_levels: list[int], fib_levels: list[int]) -> int:
    """Call the PRODUCTION entry point exactly as the BruteX shim does."""
    result: int = trading_core.evaluate_with_levels(
        _tod_seconds(case["timestamp_ist"]),
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
        case["proximity"]["pivot_bps"],
        case["proximity"]["pdh_pdl_bps"],
        case["proximity"]["fib_bps"],
        case["prior"]["prior_n"],
        case["prior"]["flags"],
    )
    return result


class TestProductionPathParity:
    """The path BruteX actually runs: ``evaluate_with_levels`` over real levels."""

    def test_evaluate_with_levels_reproduces_every_golden_vector(self) -> None:
        """The production entry point reproduces all 400 frozen ``u64``s."""
        mismatches: list[str] = []
        for case in _CASES:
            day = datetime.fromisoformat(case["timestamp_ist"]).date()
            pv = compute_pivot_levels(
                day, case["prev_pdh_paisa"], case["prev_pdl_paisa"], case["prev_pdc_paisa"]
            )
            fb = compute_fib_levels(day, case["prev_pdh_paisa"], case["prev_pdl_paisa"])
            got = _with_levels(case, _pivot_levels_10(pv), _fib_levels_15(fb))
            if got != case["expected_u64"]:
                mismatches.append(
                    f"case {case['id']}: expected {case['expected_u64']:#015x}, got {got:#015x}"
                )
        assert not mismatches, "production-path drift:\n" + "\n".join(mismatches[:20])

    def test_evaluate_with_levels_equals_evaluate_from_raw(self) -> None:
        """Both Rust entry points agree bit-for-bit on every golden case."""
        mismatches: list[str] = []
        for case in _CASES:
            day = datetime.fromisoformat(case["timestamp_ist"]).date()
            pv = compute_pivot_levels(
                day, case["prev_pdh_paisa"], case["prev_pdl_paisa"], case["prev_pdc_paisa"]
            )
            fb = compute_fib_levels(day, case["prev_pdh_paisa"], case["prev_pdl_paisa"])
            with_levels = _with_levels(case, _pivot_levels_10(pv), _fib_levels_15(fb))
            from_raw: int = trading_core.evaluate_from_raw(
                _tod_seconds(case["timestamp_ist"]),
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
                case["proximity"]["pivot_bps"],
                case["proximity"]["pdh_pdl_bps"],
                case["proximity"]["fib_bps"],
                case["prior"]["prior_n"],
                case["prior"]["flags"],
            )
            if with_levels != from_raw:
                mismatches.append(
                    f"case {case['id']}: with_levels {with_levels:#015x} != from_raw {from_raw:#015x}"
                )
        assert not mismatches, "entry-point divergence:\n" + "\n".join(mismatches[:20])

    def test_evaluate_with_levels_handles_zero_pdc_gap_day(self) -> None:
        """The ``pdc == 0`` hand-built path (the reason this entry point exists).

        ``compute_pivot_levels`` rejects ``pdc == 0``; the production engine can
        carry such levels for a session with no valid prior close, and the Group-9
        gap conditions must simply stay clear (no recompute to reject them).
        """
        # [p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc=0]
        pivot_levels = [
            24_000_00,
            24_050_00,
            24_100_00,
            24_150_00,
            23_950_00,
            23_900_00,
            23_850_00,
            24_050_00,
            23_950_00,
            0,
        ]
        fib_levels = [24_050_00] * 15
        mask: int = trading_core.evaluate_with_levels(
            10 * 3600 + 30 * 60,  # 10:30 IST
            24_000_00,
            24_010_00,
            23_990_00,
            24_005_00,
            None,
            None,
            pivot_levels,
            fib_levels,
            24_010_00,
            23_980_00,
            24_000_00,
            10,
            10,
            10,
            3,
            [],
        )
        assert not (mask & (1 << B.BIT_GAP_UP_DAY)), "gap-up must stay clear when pdc==0"
        assert not (mask & (1 << B.BIT_GAP_DOWN_DAY)), "gap-down must stay clear when pdc==0"


class TestIndicatorComputeParity:
    """The pivot/Fib math lives in both languages — prove it cannot drift."""

    def test_rust_pivot_compute_matches_python(self) -> None:
        """``trading_core.compute_pivot_levels`` == the Python wrapper, bit-for-bit."""
        rnd = random.Random(20260603)
        day = date(2026, 5, 13)
        mismatches = 0
        for _ in range(5000):
            pdl = rnd.randint(1_00, 80_000_00)
            pdh = pdl + rnd.randint(0, 5_000_00)
            pdc = rnd.randint(pdl, pdh)
            pv = compute_pivot_levels(day, pdh, pdl, pdc)
            rust = trading_core.compute_pivot_levels(pdh, pdl, pdc)
            expected = [
                pv.r4_paisa,
                pv.r3_paisa,
                pv.r2_paisa,
                pv.r1_paisa,
                pv.p_paisa,
                pv.s1_paisa,
                pv.s2_paisa,
                pv.s3_paisa,
                pv.s4_paisa,
            ]
            if list(rust) != expected:
                mismatches += 1
        assert mismatches == 0, f"{mismatches} pivot compute divergences"

    def test_rust_fib_compute_matches_python(self) -> None:
        """``trading_core.compute_fib_levels`` == the Python wrapper, bit-for-bit."""
        rnd = random.Random(20260604)
        day = date(2026, 5, 13)
        mismatches = 0
        for _ in range(5000):
            pdl = rnd.randint(1_00, 80_000_00)
            pdh = pdl + rnd.randint(0, 5_000_00)
            fb = compute_fib_levels(day, pdh, pdl)
            rust = trading_core.compute_fib_levels(pdh, pdl)
            expected = [
                fb.retrace_23_paisa,
                fb.retrace_38_paisa,
                fb.retrace_50_paisa,
                fb.retrace_62_paisa,
                fb.retrace_78_paisa,
                fb.ext_up_127_paisa,
                fb.ext_up_162_paisa,
                fb.ext_up_200_paisa,
                fb.ext_up_262_paisa,
                fb.ext_up_424_paisa,
                fb.ext_down_127_paisa,
                fb.ext_down_162_paisa,
                fb.ext_down_200_paisa,
                fb.ext_down_262_paisa,
                fb.ext_down_424_paisa,
            ]
            if list(rust) != expected:
                mismatches += 1
        assert mismatches == 0, f"{mismatches} fib compute divergences"
