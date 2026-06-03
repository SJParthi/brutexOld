"""Golden cross-language oracle for the 52-condition vocabulary.

Phase 0 of the shared-core migration (``DEC-SHARED-CORE-001``, proposed): a
frozen ``input → u64`` answer-key (``fixtures/vocab_golden_vectors.json``,
produced by ``scripts/gen_vocab_golden_vectors.py``) that:

* **locks** today's Python :func:`brutex.vocabulary.evaluator.evaluate_atoms`
  against accidental drift (this module), and
* becomes the **contract** the future Rust ``trading-core`` crate must satisfy
  bit-for-bit before BruteX / TickVault switch to it.

The fixture is language-neutral (raw int64-paisa inputs), so the same vectors
drive the Python check here and the Rust check later — that is what makes the
two engines provably identical.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.pivots import compute_pivot_levels
from brutex.vocabulary import TOTAL_CONDITIONS
from brutex.vocabulary.evaluator import (
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    evaluate_atoms,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "vocab_golden_vectors.json"
_DATA: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CASES: list[dict[str, Any]] = _DATA["cases"]


def _rebuild(case: dict[str, Any]) -> tuple[IndicatorSnapshot, ProximityParams, PriorBarHistory]:
    """Reconstruct the evaluator inputs from a language-neutral fixture case."""
    ts = datetime.fromisoformat(case["timestamp_ist"])
    day = ts.date()
    pivots = compute_pivot_levels(
        day, case["prev_pdh_paisa"], case["prev_pdl_paisa"], case["prev_pdc_paisa"]
    )
    fib = compute_fib_levels(day, case["prev_pdh_paisa"], case["prev_pdl_paisa"])
    snapshot = IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=case["open_paisa"],
        high_paisa=case["high_paisa"],
        low_paisa=case["low_paisa"],
        close_paisa=case["close_paisa"],
        ema20_paisa=case["ema20_paisa"],
        ema200_paisa=case["ema200_paisa"],
        pivots=pivots,
        fib=fib,
        day_high_paisa=case["day_high_paisa"],
        day_low_paisa=case["day_low_paisa"],
        open_of_day_paisa=case["open_of_day_paisa"],
    )
    proximity = ProximityParams(
        pivot_proximity_bps=case["proximity"]["pivot_bps"],
        pdh_pdl_proximity_bps=case["proximity"]["pdh_pdl_bps"],
        fib_proximity_bps=case["proximity"]["fib_bps"],
    )
    prior = PriorBarHistory(prior_n=case["prior"]["prior_n"], flags=list(case["prior"]["flags"]))
    return snapshot, proximity, prior


def test_python_reproduces_every_golden_vector() -> None:
    """``evaluate_atoms`` must reproduce every frozen ``u64`` bit-for-bit."""
    mismatches: list[str] = []
    for case in _CASES:
        snapshot, proximity, prior = _rebuild(case)
        got = evaluate_atoms(snapshot, proximity, prior)
        if got != case["expected_u64"]:
            mismatches.append(
                f"case {case['id']}: expected {case['expected_u64']:#015x}, got {got:#015x}"
            )
    assert not mismatches, "golden-vector drift:\n" + "\n".join(mismatches[:20])


def test_fixture_exercises_all_52_bits_both_ways() -> None:
    """The oracle must set AND clear every one of the 52 bits at least once."""
    full = (1 << TOTAL_CONDITIONS) - 1
    set_any = 0
    clear_any = 0
    for case in _CASES:
        set_any |= case["expected_u64"]
        clear_any |= (~case["expected_u64"]) & full
    assert set_any == full, "a condition never fires across the oracle"
    assert clear_any == full, "a condition is never false across the oracle"


def test_fixture_metadata_is_consistent() -> None:
    """Guard the fixture header against silent corruption / regeneration drift."""
    assert _DATA["total_conditions"] == TOTAL_CONDITIONS
    assert _DATA["n_cases"] == len(_CASES)
    assert len({case["id"] for case in _CASES}) == len(_CASES)
    assert _DATA["generated_from"] == "brutex.vocabulary.evaluator.evaluate_atoms"
