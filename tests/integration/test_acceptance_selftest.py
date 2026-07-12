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
    # round-4 B4-3: per-leg unique teeth (census-only + engine-only).
    "census_nonengine_dup",
    "engine_oldmonth_dup",
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

    def test_net_new_law_sabotage_flips_battery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Round-4 B4-2 knock-on regression: with the pull.partition_presence
        # 1-day-window span bug, poisons 6-8 all rode the presence false
        # positive, so an unconditional-PASS pull.net_new_law sabotage
        # SURVIVED the full battery (measured TEETH_PROVEN 15/15 pre-fix).
        # With the span fixed, poisons 6-7 must depend on the REAL comparator
        # — the sabotage flips the battery.
        from dataclasses import replace

        from brutex.acceptance.checks import pull as pull_check
        from brutex.acceptance.model import Verdict, make_check

        real_run = pull_check.run

        def _sabotaged(ctx):
            check = real_run(ctx)
            subs = [
                replace(sub, verdict=Verdict.PASS) if sub.id == "pull.net_new_law" else sub
                for sub in check.sub_checks
            ]
            return make_check(
                check.check_id,
                subs,
                coverage=check.coverage,
                repro=check.repro,
                limits=list(check.limits),
                input_fingerprint=check.input_fingerprint,
            )

        monkeypatch.setattr(pull_check, "run", _sabotaged)
        result = selftest.run_full_battery(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_FAILED
        missed = {o.name for o in result.outcomes if not o.detected}
        assert missed == {"net_new_gt_bars", "counter_invariant"}

    def test_census_sabotage_flips_battery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Round-4 B4-3 regression: an unconditional-PASS unique.census
        # sabotage previously survived the whole battery (every dup poison
        # sat on an ENGINE coordinate the engine leg caught anyway) — the
        # non-engine dup tooth must now be MISSED under it.
        from dataclasses import replace

        from brutex.acceptance.checks import unique as unique_check
        from brutex.acceptance.model import Verdict, make_check

        real_run = unique_check.run

        def _sabotaged(ctx):
            check = real_run(ctx)
            subs = [
                replace(sub, verdict=Verdict.PASS) if sub.id == "unique.census" else sub
                for sub in check.sub_checks
            ]
            return make_check(
                check.check_id,
                subs,
                coverage=check.coverage,
                repro=check.repro,
                limits=list(check.limits),
                input_fingerprint=check.input_fingerprint,
            )

        monkeypatch.setattr(unique_check, "run", _sabotaged)
        result = selftest.run_full_battery(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_FAILED
        missed = {o.name for o in result.outcomes if not o.detected}
        assert "census_nonengine_dup" in missed

    def test_engine_leg_sabotage_flips_battery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Round-4 B4-3 (symmetric): an always-clean unique.engine sabotage
        # must be MISSED by the out-of-months-bound engine dup tooth.
        from dataclasses import replace

        from brutex.acceptance.checks import unique as unique_check
        from brutex.acceptance.model import Verdict, make_check

        real_run = unique_check.run

        def _sabotaged(ctx):
            check = real_run(ctx)
            subs = [
                replace(sub, verdict=Verdict.PASS) if sub.id == "unique.engine" else sub
                for sub in check.sub_checks
            ]
            return make_check(
                check.check_id,
                subs,
                coverage=check.coverage,
                repro=check.repro,
                limits=list(check.limits),
                input_fingerprint=check.input_fingerprint,
            )

        monkeypatch.setattr(unique_check, "run", _sabotaged)
        result = selftest.run_full_battery(now_utc=NOW)
        assert result.verdict is TeethVerdict.TEETH_FAILED
        missed = {o.name for o in result.outcomes if not o.detected}
        assert "engine_oldmonth_dup" in missed
