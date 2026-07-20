"""Doctest teeth for ``brutex.engine.combo_mask`` (DEC-CORP-ACTION-GATE-001 fold).

The 2026-07-03 audit found the module's ``count_combos`` doctests still
answered with the 52-condition vocabulary (52 / 1326 / 22100) after
DEC-VOCAB-U128-CODEGEN-001 grew it to 60 — and NOTHING ran them, so the drift
was invisible. This runs doctest over the WHOLE module so any future example
drift (a vocab-size change, a renamed helper, a stale sample value) fails CI
instead of rotting silently.

Stdout-capture isolation: ``doctest.testmod`` swaps ``sys.stdout`` for its
capture buffer, so a BACKGROUND thread logging via unconfigured structlog
(default ``PrintLogger`` → the current ``sys.stdout``) contaminates the
examples' ``Got:`` output — the PR #1666 unit-shard-2 flake (CI jobs
88186693408 + 88192989598, twice consecutively: a leaked ``StartupSweeper``
daemon's ``lake_heal.sweep_complete`` line turned ``bits_to_mask()``'s
expected ``0`` into ``<log line>\\n0``). The gate now runs under the conftest
``_doctest_structlog_stderr`` sink routing; the regression test below pins the
immunity against the exact leak shape.
"""

from __future__ import annotations

import doctest
import threading
import time

import pytest
import structlog
from brutex.engine import combo_mask


@pytest.mark.usefixtures("_doctest_structlog_stderr")
def test_combo_mask_module_doctests_all_pass() -> None:
    results = doctest.testmod(combo_mask, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest failure(s) in combo_mask"
    # Teeth: the module genuinely carries doctests (an empty run would be a
    # silent no-op gate, not a gate).
    assert results.attempted >= 6


@pytest.mark.usefixtures("_doctest_structlog_stderr")
def test_doctest_capture_immune_to_background_structlog_leak() -> None:
    """Regression: the PR #1666 shard-2 doctest flake (fails-before-proven).

    A hostile background thread logs via UNCONFIGURED structlog on the EXACT
    leaked logger + event (``brutex.data.lake_heal`` ·
    ``lake_heal.sweep_complete`` — the un-joined ``StartupSweeper`` daemon a
    ``cmd_dashboard`` test spawned; see the conftest
    ``_lake_heal_sweep_daemon_belt``) in a tight loop WHILE the combo_mask
    doctests run. Without the ``_doctest_structlog_stderr`` sink routing, the
    default ``PrintLogger`` writes into doctest's swapped-in stdout buffer and
    this run reproduces the CI failure deterministically (verified at fix
    time: 3+ of 34 examples contaminated); with it, every example is clean.
    """
    leak_logger = structlog.get_logger("brutex.data.lake_heal")
    stop = threading.Event()

    def _hostile_daemon() -> None:
        while not stop.is_set():
            leak_logger.info("lake_heal.sweep_complete", files=0, findings=0, healed=0)
            time.sleep(0.001)

    thread = threading.Thread(target=_hostile_daemon, name="brutex-lake-heal-sweep", daemon=True)
    thread.start()
    try:
        results = doctest.testmod(combo_mask, verbose=False)
    finally:
        stop.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive(), "hostile daemon failed to stop — test bug"
    assert results.failed == 0, (
        f"{results.failed} doctest failure(s) under a background structlog "
        f"leak — the stdout-capture isolation regressed (PR #1666 shard-2 class)"
    )


def test_count_combos_examples_match_the_60_vocab() -> None:
    # The exact examples the audit caught stale, pinned against the live
    # vocabulary size so a future vocab change forces BOTH updates together.
    from brutex.engine.combo_mask import VOCAB_BITS, count_combos

    assert VOCAB_BITS == 60
    assert count_combos(1) == 60
    assert count_combos(2) == 1770
    assert count_combos(3) == 34220
