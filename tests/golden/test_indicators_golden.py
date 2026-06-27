"""Frozen golden answer-key for ``brutex.indicators``.

A language-neutral, stdlib-``json`` fixture
(``fixtures/indicators_golden.json``) that locks the four evaluate-time
indicators against accidental drift:

* **pivots** — one ORACLE-ANCHORED case (NIFTY 14-May-2026, the verified
  TradingView "Pivots Traditional Auto" screenshot already pinned in
  ``tests/unit/test_pivots.py``).
* **fib** — one ORACLE-ANCHORED case (BANKNIFTY 13-May-2026 BEARISH, the
  ES Trading Academy AmiBroker v11 screenshot pinned in
  ``tests/unit/test_fib_levels.py``).
* **ema** — the hand-computed period-3 oracle, a flat-series case
  (constant input → constant EMA, warm-up bars frozen ``null``), and the
  period-1 identity case.
* **day_extremes** — a multi-bar single-day running-extremes case plus a
  next-IST-day reset.

The pivot/fib expected vectors are copied verbatim from the unit-test
oracles so the golden is GROUNDED in the operator's screenshots, not
self-referential. The test reads the JSON literals and asserts the live
functions reproduce them EXACTLY.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from brutex.indicators.day_extremes import compute_day_extremes
from brutex.indicators.ema import compute_ema
from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.pivots import compute_pivot_levels

_FIXTURE = Path(__file__).parent / "fixtures" / "indicators_golden.json"
_DATA: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_pivots_reproduce_every_golden_vector() -> None:
    """``compute_pivot_levels`` must reproduce each frozen pivot vector exactly."""
    for case in _DATA["pivots"]:
        levels = compute_pivot_levels(
            trading_day=date.fromisoformat(case["trading_day"]),
            pdh_paisa=case["pdh_paisa"],
            pdl_paisa=case["pdl_paisa"],
            pdc_paisa=case["pdc_paisa"],
        )
        exp = case["expected"]
        got = {
            "p_paisa": levels.p_paisa,
            "r1_paisa": levels.r1_paisa,
            "r2_paisa": levels.r2_paisa,
            "r3_paisa": levels.r3_paisa,
            "r4_paisa": levels.r4_paisa,
            "s1_paisa": levels.s1_paisa,
            "s2_paisa": levels.s2_paisa,
            "s3_paisa": levels.s3_paisa,
            "s4_paisa": levels.s4_paisa,
        }
        assert got == exp, f"pivot drift in case {case['id']}: {got} != {exp}"


def test_fib_reproduces_every_golden_vector() -> None:
    """``compute_fib_levels`` must reproduce each frozen fib vector exactly."""
    for case in _DATA["fib"]:
        levels = compute_fib_levels(
            trading_day=date.fromisoformat(case["trading_day"]),
            pdh_paisa=case["pdh_paisa"],
            pdl_paisa=case["pdl_paisa"],
        )
        assert levels.range_paisa == case["expected_range_paisa"], (
            f"fib range drift in case {case['id']}"
        )
        for label, expected in case["expected_bearish"].items():
            got = levels.get_by_bearish_label(label)
            assert got == expected, (
                f"fib drift in case {case['id']} label {label}: {got} != {expected}"
            )


def test_ema_reproduces_every_golden_vector() -> None:
    """``compute_ema`` must reproduce each frozen EMA series exactly,
    including the warm-up ``null`` prefix."""
    for case in _DATA["ema"]:
        got = compute_ema(case["values"], period=case["period"])
        assert got == case["expected"], (
            f"EMA drift in case {case['id']}: {got} != {case['expected']}"
        )


def test_day_extremes_reproduce_every_golden_vector() -> None:
    """``compute_day_extremes`` must reproduce each frozen running-extremes
    sequence exactly, across the day boundary reset."""
    for case in _DATA["day_extremes"]:
        bars = [
            (
                datetime.fromisoformat(bar["ts_ist"]),
                bar["high_paisa"],
                bar["low_paisa"],
            )
            for bar in case["bars"]
        ]
        result = compute_day_extremes(bars)
        got = [
            {"day_high_paisa": r.day_high_paisa, "day_low_paisa": r.day_low_paisa} for r in result
        ]
        assert got == case["expected"], (
            f"day-extremes drift in case {case['id']}: {got} != {case['expected']}"
        )
