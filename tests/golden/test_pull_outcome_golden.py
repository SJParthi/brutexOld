"""Golden gate: the pull-outcome classification contract (DEC-RUST-PULL-STATE-001).

The committed fixtures (``pull_outcome_classification.json`` +
``pull_outcome_kernel_cases.tsv``, captured from the pre-conversion Python by
``generate_pull_outcome_golden.py``) pin every classification the Rust
pull-state lane must preserve bit-for-bit:

* the full ``classify_last_pull_outcome`` lattice — the state machine
  (running/wedged/stalled → error → done → idle fall-through), the
  error-detail classification (the market-hours deferral prefix, the
  ``LakeWriteHalt`` reason split, the ``_HALT_NAME_OUTCOMES`` table, the
  generic-error arm), the done-arm precedence (deferral BEFORE verdict;
  ``failed > 0`` outranks a clean verdict) and the verdict-shape sweep
  (colours via the ONE Python authority ``banner_for``);
* the ``classify_retained_pull_outcome`` run-id correlation law;
* the kernel-contract TSV the crate's ``cargo test`` also reads (the
  cross-language pin — one committed fixture, two consumers).

Any divergence — a Rust-arm drift, an accidental oracle edit, OR a
legitimate vocabulary/wording move under its own DEC — fails LOUDLY here and
forces a deliberate regeneration via ``BRUTEX_REGEN_PULL_OUTCOME_GOLDEN=1``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.golden._pull_outcome_harness import (
    compute_classify_table,
    compute_kernel_rows,
    compute_retained_table,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODULE_FIXTURE = FIXTURES / "pull_outcome_classification.json"
KERNEL_FIXTURE = FIXTURES / "pull_outcome_kernel_cases.tsv"

#: The full outcome vocabulary (the module docstring's stable machine tokens).
EXPECTED_OUTCOMES = {
    "never_run",
    "running",
    "running_stalled_suspect",
    "wedged",
    "clean",
    "completed",
    "completed_with_failures",
    "deferred_market_hours",
    "halted_market_hours",
    "halted_disk_full",
    "halted_oom",
    "halted_lake_write",
    "halted_quota",
    "halted_pool_broken",
    "halted_vendor_down",
    "halted_mass_empty",
    "halted_auth",
    "refused_busy",
    "error",
    "unknown",
    # every non-clean classified verdict kind in the banner authority's table
    "verdict_credentials_missing",
    "verdict_auth_expired",
    "verdict_mass_empty_halt",
    "verdict_quota_exhausted",
    "verdict_transient_residual",
    "verdict_quarantined_symbols",
    "verdict_known_gap_only",
}


def _load_module_fixture() -> dict[str, Any]:
    payload = json.loads(MODULE_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_module_fixture_matches_live_classifier() -> None:
    """The recompute-and-compare gate: live output ≡ the frozen oracle."""
    frozen = _load_module_fixture()
    assert frozen["schema"] == 1
    assert frozen["classify"] == compute_classify_table()
    assert frozen["retained"] == compute_retained_table()


def test_kernel_fixture_matches_live_projection() -> None:
    """The kernel-contract TSV ≡ the live projection (the cargo test's twin)."""
    frozen = KERNEL_FIXTURE.read_text(encoding="utf-8").splitlines()
    assert frozen == compute_kernel_rows()


def test_fixture_covers_the_full_outcome_and_tone_vocabulary() -> None:
    """Anti-vacuous: EVERY outcome token + EVERY tone appears in the capture."""
    frozen = _load_module_fixture()
    rows = list(frozen["classify"]) + list(frozen["retained"])
    outcomes = {row["out"]["outcome"] for row in rows}
    tones = {row["out"]["tone"] for row in rows}
    assert outcomes == EXPECTED_OUTCOMES
    assert tones == {"ok", "warn", "bad", "idle"}


def test_kernel_fixture_covers_every_arm_marker() -> None:
    """Anti-vacuous: the kernel rows exercise both verdict markers, every
    detail code, every halt-table headline code and the correlation law."""
    rows = [line.split("\t") for line in KERNEL_FIXTURE.read_text(encoding="utf-8").splitlines()]
    classify = [r for r in rows if r[0] == "classify"]
    correlates = [r for r in rows if r[0] == "correlates"]
    assert classify and correlates
    outcomes = {r[7] for r in classify}
    assert {"@verdict", "@verdict_prepull"} <= outcomes
    detail_codes = {r[10] for r in classify}
    assert detail_codes == {"input", "empty", "completed", ""}
    headline_codes = {r[9] for r in classify}
    from brutex.dashboard.pull_outcome import _HALT_NAME_OUTCOMES

    assert {f"halt:{name}" for name in _HALT_NAME_OUTCOMES} <= headline_codes
    assert {"1", "0"} == {r[3] for r in correlates}
