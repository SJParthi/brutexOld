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
the "both ways" coverage gate is partitioned over the IMPLEMENTED bits only —
the NON-implemented bits (the RETIRED VWAP tombstones 52/53 + the declared-only
SuperTrend bits 64/65, ``implemented=false``) are proven inert by
:func:`test_layout_only_bits_are_always_clear`. Version 3 is the 60→66 reshape
(``vwap_paisa`` survives as an INERT RNG-burn record — no longer an evaluator
input; the frozen-input migration invariant lives in
``test_vocab_golden_migration.py``); the fixture bytes are SHA-pinned + proven
byte-identical to a fresh generation (GOLD-06).
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
    # NOTE: the case's ``vwap_paisa`` field is deliberately NOT consumed —
    # it is the inert RNG-burn record of the retired bits-52/53 draw.
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

    The reshape guarantee: the RETIRED VWAP tombstones (52/53), the
    declared-only SuperTrend bits (64/65) and every reserved bit >= 66 are
    NEVER set by the evaluator.
    """
    for case in _CASES:
        mask = _expected(case)
        assert mask & ~IMPLEMENTED_MASK == 0, (
            f"case {case['id']}: a layout-only/reserved bit fired (mask={mask:#034x})"
        )


def test_fixture_metadata_is_consistent() -> None:
    """Guard the fixture header against silent corruption / regeneration drift."""
    assert _DATA["version"] == 3
    assert _DATA["total_conditions"] == TOTAL_CONDITIONS
    assert int(_DATA["implemented_mask"], 16) == IMPLEMENTED_MASK
    assert int(_DATA["full_mask"], 16) == (1 << TOTAL_CONDITIONS) - 1
    assert _DATA["n_cases"] == len(_CASES)
    assert len({case["id"] for case in _CASES}) == len(_CASES)
    assert _DATA["generated_from"] == "brutex.vocabulary.evaluator.evaluate_atoms"
    assert int(_DATA["bits_stray_nonimplemented"], 16) == 0
    # Every case carries a 32-nibble hex mask string (u128), not a JSON number.
    for case in _CASES:
        assert isinstance(case["expected_mask"], str)
        assert len(case["expected_mask"]) == 32


#: SHA-256 of the frozen fixture bytes (GOLD-06 — the README "SHA-256
#: manifest" drift guard). Regenerate with
#: ``python scripts/gen_vocab_golden_vectors.py`` then update this.
_EXPECTED_SHA256 = "0f0f5f85b29941beff20ec71a7e4942bce681d80e3c8f3c313e28da2445057a0"


def test_fixture_sha256_matches_pinned_manifest() -> None:
    """Byte-for-byte no-drift gate on the golden fixture file (GOLD-06)."""
    import hashlib

    digest = hashlib.sha256(_FIXTURE.read_bytes()).hexdigest()
    assert digest == _EXPECTED_SHA256


def test_fixture_is_byte_identical_to_a_fresh_generation() -> None:
    """The staleness gate (GOLD-06): re-running the generator reproduces the
    committed bytes exactly (the truth-table ``--check`` pattern, in-suite —
    this test needs the wheel, which this golden suite already requires)."""
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts import gen_vocab_golden_vectors as gen

    assert _FIXTURE.read_text(encoding="utf-8") == gen.render(gen.generate())
