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
any NON-implemented bit is proven inert by
:func:`test_layout_only_bits_are_always_clear` (VACUOUS on this tree: all 74
declared bits are IMPLEMENTED — DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md
#2206, reversed the bits-52/53 wipe, so ``vwap_paisa`` is a LIVE evaluator
input again, exactly its pre-wipe role). Version 4 is the vocab-train final
74-bit configuration (crate 0.6.0): 408 cases — the 400 seeded
migration-invariant cases (inputs byte-frozen; the ST/gap keys appended, the
un-burned vwap draw re-consumed) + the 8 deterministic express cases
(DEC-FIB-TEMPLATE-EXPRESS-001, ids 400-407 — keyless on the ST/gap/vwap
fields, carrying the swing pair instead). The frozen-input migration
invariants live in ``test_vocab_golden_migration.py``; the fixture bytes are
SHA-pinned + proven byte-identical to a fresh generation (GOLD-06).
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
    # The case's ``vwap_paisa`` field is CONSUMED again — bits 52/53 are
    # RETAINED-ACTIVE per DEC-VOCAB-VWAP-RETAIN-001 (MIGRATION.md #2206; the
    # wipe-era "inert RNG-burn record" reading is dead). The keyless express
    # cases (ids >= 400) default it to None — the abstain arm — exactly like
    # the Rust readers' ``#[serde(default)]``.
    snapshot = IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=case["open_paisa"],
        high_paisa=case["high_paisa"],
        low_paisa=case["low_paisa"],
        close_paisa=case["close_paisa"],
        ema20_paisa=case["ema20_paisa"],
        ema200_paisa=case["ema200_paisa"],
        vwap_paisa=case.get("vwap_paisa"),
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
        # The vocab-66 SuperTrend column (v4) + the Group-15 gap-midpoint
        # level (DEC-GAP-MIDPOINT-CONDITIONS-001) — carried VERBATIM like
        # the VWAP on the 400 seeded cases; the DETERMINISTIC express
        # cases (ids >= 400, DEC-FIB-TEMPLATE-EXPRESS-001) omit them, so
        # readers default them exactly like the Rust twin's serde
        # defaults (None -> the bits stay clear).
        supertrend_x2_paisa=case.get("supertrend_x2_paisa"),
        gap_mid_paisa=case.get("gap_mid_paisa"),
        # DEC-FIB-TEMPLATE-EXPRESS-001 — the swing anchors (bits 72/73).
        # The 400 migration-invariant cases carry NO swing keys
        # (byte-frozen input discipline); readers default them to
        # None / 10 exactly like the Rust twin's serde defaults.
        last_swing_high_paisa=case.get("last_swing_high_paisa"),
        last_swing_low_paisa=case.get("last_swing_low_paisa"),
    )
    proximity = ProximityParams(
        pivot_proximity_bps=case["proximity"]["pivot_bps"],
        pdh_pdl_proximity_bps=case["proximity"]["pdh_pdl_bps"],
        fib_proximity_bps=case["proximity"]["fib_bps"],
        gap_mid_proximity_bps=case["proximity"].get("gap_mid_bps", 10),
        swing_proximity_bps=case["proximity"].get("swing_bps", 10),
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

    The reshape guarantee: the RETIRED VWAP tombstones (52/53) and every
    reserved bit >= 74 are NEVER set by the evaluator (the SuperTrend bits
    64/65 left this set at the vocab-66 implemented-flip, the gap trio 66-68
    at the re-stack and the express bits 69-73 at
    DEC-FIB-TEMPLATE-EXPRESS-001).
    """
    for case in _CASES:
        mask = _expected(case)
        assert mask & ~IMPLEMENTED_MASK == 0, (
            f"case {case['id']}: a layout-only/reserved bit fired (mask={mask:#034x})"
        )


def test_fixture_metadata_is_consistent() -> None:
    """Guard the fixture header against silent corruption / regeneration drift."""
    assert _DATA["version"] == 4
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
#: DELIBERATE RE-LOCK (the vocab-train Regen — the ONE golden transition):
#: 408 cases, 74/74 both-ways coverage; the 400 seeded inputs byte-identical
#: to origin/main's v2 fixture modulo the enumerated appended keys
#: (``supertrend_x2_paisa`` / ``gap_mid_paisa`` / ``proximity.gap_mid_bps``)
#: and ``new_mask & (2**60 - 1) == v2_mask`` on 400/400 (#2206's low-60
#: identity law; 355/400 carry a 52/53 bit — the pre-wipe census exactly).
#: Old (wipe-era train) digest:
#: fcc4acaad86f5511dd0dbfe83ac880ade771a2364e1e40630b55e001c1e6adc0.
_EXPECTED_SHA256 = "5f645e284e95afa41edff53616ef6c7924d25a6b854244b05a6dc3c9a82c1cce"


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
