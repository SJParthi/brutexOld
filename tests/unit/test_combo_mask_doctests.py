"""Doctest teeth for ``brutex.engine.combo_mask`` (DEC-CORP-ACTION-GATE-001 fold).

The 2026-07-03 audit found the module's ``count_combos`` doctests still
answered with the 52-condition vocabulary (52 / 1326 / 22100) after
DEC-VOCAB-U128-CODEGEN-001 grew it to 60 — and NOTHING ran them, so the drift
was invisible. This runs doctest over the WHOLE module so any future example
drift (a vocab-size change, a renamed helper, a stale sample value) fails CI
instead of rotting silently.
"""

from __future__ import annotations

import doctest
import threading

from brutex.engine import combo_mask


def test_combo_mask_module_doctests_all_pass() -> None:
    # Flake hardening (shard-reshuffle-surfaced, deterministic fix): an earlier
    # test in the same process can leave the lake-heal L3 startup sweep's
    # daemon thread (brutex/data/lake_heal.py, name "brutex-lake-heal-sweep",
    # daemon=True) still running; if it finishes WHILE doctest.testmod has
    # sys.stdout redirected, its output pollutes the doctest capture and
    # false-fails an unrelated example. Join any leaked sweep thread first.
    for t in threading.enumerate():
        if t.name == "brutex-lake-heal-sweep":
            t.join(timeout=30.0)
    results = doctest.testmod(combo_mask, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest failure(s) in combo_mask"
    # Teeth: the module genuinely carries doctests (an empty run would be a
    # silent no-op gate, not a gate).
    assert results.attempted >= 6


def test_count_combos_examples_match_the_60_vocab() -> None:
    # The exact examples the audit caught stale, pinned against the live
    # vocabulary size so a future vocab change forces BOTH updates together.
    from brutex.engine.combo_mask import VOCAB_BITS, count_combos

    assert VOCAB_BITS == 60
    assert count_combos(1) == 60
    assert count_combos(2) == 1770
    assert count_combos(3) == 34220
