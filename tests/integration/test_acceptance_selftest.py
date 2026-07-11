"""CI-mandatory FULL poison battery — every detector demonstrated per merge.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756), design §7: the full
battery runs where flake is cheap (CI), so every check's detector has a
demonstrated-failure fixture on every merge — PLUS the comparator-inversion
teeth proving the battery itself has teeth.
"""

from __future__ import annotations

import pytest

from brutex.acceptance import selftest
from brutex.acceptance.model import TeethVerdict
from tests.fakes.acceptance_twin import NOW

EXPECTED_POISONS = {
    "dup_row",
    "tf_high_x10",
    "movers_cell",
    "foreign_month_row",
    "forming_day_stored",
    "net_new_gt_bars",
    "counter_invariant",
    "deleted_partition",
    "over_cap_bridge",
    "in_window_attempt",
    "negative_low",
    "high_lt_low",
    "interior_hole",
    "three_decimal_price",
    "prevclose_hole_unstamped",
}


class TestFullBattery:
    def test_every_poison_detected(self) -> None:
        result = selftest.run_full_battery(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_PROVEN, selftest.render_battery_table(result)
        assert {o.name for o in result.outcomes} == EXPECTED_POISONS
        assert result.poisons_detected == len(EXPECTED_POISONS)
        assert result.false_fails_on_clean == 0
        assert result.mode == "full"

    def test_default_clock_arm(self) -> None:
        result = selftest.run_full_battery()
        assert result.verdict is TeethVerdict.TEETH_PROVEN

    def test_comparator_inversion_flips_battery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.acceptance.checks import movers as movers_check

        monkeypatch.setattr(movers_check, "compare_spot_cells", lambda *a, **k: [])
        monkeypatch.setattr(movers_check, "contiguity_stamp_mismatch", lambda *a, **k: None)
        result = selftest.run_full_battery(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_FAILED
        missed = {o.name for o in result.outcomes if not o.detected}
        assert missed == {"movers_cell", "prevclose_hole_unstamped"}

    def test_crashing_detector_is_a_missed_tooth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.acceptance.checks import rate as rate_check

        def _boom(ctx):
            raise RuntimeError("detector crashed")

        monkeypatch.setattr(rate_check, "run", _boom)
        result = selftest.run_full_battery(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_FAILED
        crashed = [o for o in result.outcomes if "detector crashed" in o.detail]
        assert crashed and all(not o.detected for o in crashed)
