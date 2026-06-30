"""Golden cross-language oracle for the shared condition vocabulary.

A frozen ``input → mask`` answer-key (``fixtures/vocab_golden_vectors.json``,
produced by ``scripts/gen_vocab_golden_vectors.py``) that:

* **locks** today's Python :func:`brutex.vocabulary.evaluator.evaluate_atoms`
  against accidental drift (this module), and
* is the **contract** the Rust ``trading-core`` crate satisfies bit-for-bit
  (``rust/trading-core/tests/golden_vectors.rs``), so BruteX (PyO3) and
  TickVault (native) are provably identical.

The fixture is language-neutral (raw int64-paisa inputs). Since
DEC-VOCAB-U128-CODEGEN-001 the mask is a **u128** serialised as a hex STRING
(``expected_mask``) so a bit >= 53 keeps full precision across JSON doubles, and
the "both ways" coverage gate is partitioned over the IMPLEMENTED bits only — the
LAYOUT-ONLY bits (VWAP/R5-S5/BOS/CHoCH, ``implemented=false``) are proven inert
by :func:`test_layout_only_bits_are_always_clear`.
"""

from __future__ import annotations

import json
from datetime import datetime
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

_FIXTURE = Path(__file__).parent / "fixtures" / "vocab_golden_vectors.json"
_DATA: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CASES: list[dict[str, Any]] = _DATA["cases"]


def _expected(case: dict[str, Any]) -> int:
    """Parse a case's ``expected_mask`` hex string into the u128 mask int."""
    return int(case["expected_mask"], 16)


def _rebuild(case: dict[str, Any]) -> tuple[IndicatorSnapshot, ProximityParams, PriorBarHistory]:
    """Reconstruct the evaluator inputs from a language-neutral fixture case."""
    ts = datetime.fromisoformat(case["timestamp_ist"])
    day = ts.date()
    pivots = compute_pivot_levels(
        day, case["prev_pdh_paisa"], case["prev_pdl_paisa"], case["prev_pdc_paisa"]
    )
    fib = compute_fib_levels(day, case["prev_pdh_paisa"], case["prev_pdl_paisa"])
    ms = case["market_structure"]
    snapshot = IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=case["open_paisa"],
        high_paisa=case["high_paisa"],
        low_paisa=case["low_paisa"],
        close_paisa=case["close_paisa"],
        ema20_paisa=case["ema20_paisa"],
        ema200_paisa=case["ema200_paisa"],
        vwap_paisa=case["vwap_paisa"],
        pivots=pivots,
        fib=fib,
        day_high_paisa=case["day_high_paisa"],
        day_low_paisa=case["day_low_paisa"],
        open_of_day_paisa=case["open_of_day_paisa"],
        market_structure=MarketStructureSignal(
            bos_bullish=ms["bos_bullish"],
            bos_bearish=ms["bos_bearish"],
            choch_bullish=ms["choch_bullish"],
            choch_bearish=ms["choch_bearish"],
        ),
    )
    proximity = ProximityParams(
        pivot_proximity_bps=case["proximity"]["pivot_bps"],
        pdh_pdl_proximity_bps=case["proximity"]["pdh_pdl_bps"],
        fib_proximity_bps=case["proximity"]["fib_bps"],
    )
    prior = PriorBarHistory(prior_n=case["prior"]["prior_n"], flags=list(case["prior"]["flags"]))
    return snapshot, proximity, prior


def test_python_reproduces_every_golden_vector() -> None:
    """``evaluate_atoms`` must reproduce every frozen mask bit-for-bit."""
    mismatches: list[str] = []
    for case in _CASES:
        snapshot, proximity, prior = _rebuild(case)
        got = evaluate_atoms(snapshot, proximity, prior)
        expected = _expected(case)
        if got != expected:
            mismatches.append(f"case {case['id']}: expected {expected:#034x}, got {got:#034x}")
    assert not mismatches, "golden-vector drift:\n" + "\n".join(mismatches[:20])


def test_fixture_exercises_all_implemented_bits_both_ways() -> None:
    """The oracle must set AND clear every IMPLEMENTED bit at least once.

    The LAYOUT-ONLY bits (``implemented=false``) are excluded — the evaluator
    emits 0 for them by construction; :func:`test_layout_only_bits_are_always_clear`
    proves that separately.
    """
    set_any = 0
    clear_any = 0
    for case in _CASES:
        mask = _expected(case)
        set_any |= mask
        clear_any |= (~mask) & IMPLEMENTED_MASK
    assert set_any == IMPLEMENTED_MASK, "an implemented condition never fires across the oracle"
    assert clear_any == IMPLEMENTED_MASK, (
        "an implemented condition is never false across the oracle"
    )


def test_layout_only_bits_are_always_clear() -> None:
    """Every non-implemented / reserved bit must be 0 in EVERY golden case.

    The honest PR-1 guarantee: the new VWAP/R5-S5/BOS/CHoCH bits are declared in
    the layout but the evaluator never sets them (their math lands in PR-2).
    """
    for case in _CASES:
        mask = _expected(case)
        assert mask & ~IMPLEMENTED_MASK == 0, (
            f"case {case['id']}: a layout-only/reserved bit fired (mask={mask:#034x})"
        )


def test_fixture_metadata_is_consistent() -> None:
    """Guard the fixture header against silent corruption / regeneration drift."""
    assert _DATA["version"] == 2
    assert _DATA["total_conditions"] == TOTAL_CONDITIONS
    assert int(_DATA["implemented_mask"], 16) == IMPLEMENTED_MASK
    assert int(_DATA["full_mask"], 16) == (1 << TOTAL_CONDITIONS) - 1
    assert _DATA["n_cases"] == len(_CASES)
    assert len({case["id"] for case in _CASES}) == len(_CASES)
    assert _DATA["generated_from"] == "brutex.vocabulary.evaluator.evaluate_atoms"
    # Every case carries a 32-nibble hex mask string (u128), not a JSON number.
    for case in _CASES:
        assert isinstance(case["expected_mask"], str)
        assert len(case["expected_mask"]) == 32
