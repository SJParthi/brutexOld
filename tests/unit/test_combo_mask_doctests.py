"""Doctest teeth for ``brutex.engine.combo_mask`` (DEC-CORP-ACTION-GATE-001 fold).

The 2026-07-03 audit found the module's ``count_combos`` doctests still
answered with the historical 52-condition vocabulary (52 / 1326 / 22100)
after DEC-VOCAB-U128-CODEGEN-001 grew the layout — and NOTHING ran them, so the drift
was invisible. This runs doctest over the WHOLE module so any future example
drift (a vocab-size change, a renamed helper, a stale sample value) fails CI
instead of rotting silently.
"""

from __future__ import annotations

import doctest

from brutex.engine import combo_mask


def test_combo_mask_module_doctests_all_pass() -> None:
    results = doctest.testmod(combo_mask, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest failure(s) in combo_mask"
    # Teeth: the module genuinely carries doctests (an empty run would be a
    # silent no-op gate, not a gate).
    assert results.attempted >= 6


def test_count_combos_examples_match_the_74_vocab() -> None:
    # The exact examples the audit caught stale, pinned against the live
    # vocabulary size so a future vocab change forces BOTH updates together.
    from brutex.engine.combo_mask import VOCAB_BITS, count_combos

    # 66 -> 74 at DEC-FIB-TEMPLATE-EXPRESS-001 (MIGRATION.md #1935).
    assert VOCAB_BITS == 74
    assert count_combos(1) == 74
    assert count_combos(2) == 2701
    assert count_combos(3) == 64824
