"""Model-layer pins: roll-up truth table, evidence invariants, exit-code law.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1797). These are the
mutant-killers for the verdict/exit laws — the whole command's honesty rests
on them.
"""

from __future__ import annotations

import itertools

import pytest
from brutex.acceptance.model import (
    ABSTAIN_STREAK_LIMIT,
    SCHEMA_VERSION,
    SELECTION_CAP,
    STRUCTURAL_ABSTAINS,
    AbstainReason,
    AcceptanceModelError,
    Coverage,
    OverallVerdict,
    TeethVerdict,
    Verdict,
    exit_code_for,
    make_check,
    make_coverage,
    make_subcheck,
    rollup_check_verdict,
    rollup_overall,
    totals_for,
    with_streak,
)


def _cov(mode: str = "full") -> Coverage:
    return make_coverage(mode=mode, population=10, examined=10, seed="2026-07-10")


def _sub(id_: str, verdict: Verdict, *, units: int = 1, reason: AbstainReason | None = None):
    return make_subcheck(
        id_, verdict, units_checked=units, evidence={"k": 1}, abstain_reason=reason
    )


class TestSchemaTokens:
    def test_schema_version_frozen(self) -> None:
        assert SCHEMA_VERSION == "brutex-acceptance-report-v1"

    def test_structural_abstains_frozen_list(self) -> None:
        # The budget/streak exemption list is FROZEN — extending it is a
        # ledgered decision. This pin fails on any silent widening.
        assert (
            frozenset(
                {
                    AbstainReason.NO_PER_CALL_PERSISTENCE,
                    AbstainReason.NO_PER_CALL_LOG,
                    AbstainReason.NO_OI_COMPARE_SEAM,
                    # Round-8 (MIGRATION.md #1797): an ABSENT rotating-log sink
                    # (headless / dashboard never run) is a standing by-design
                    # gap, budget/streak-EXEMPT so it never turns a healthy
                    # headless lake permanently amber.
                    AbstainReason.NO_RATE_LOG_CONFIGURED,
                }
            )
            == STRUCTURAL_ABSTAINS
        )

    def test_abstain_reason_enum_frozen(self) -> None:
        assert {r.value for r in AbstainReason} == {
            "nothing_to_check",
            "pull_in_progress",
            "input_moved_during_check",
            "daily_cutoff_ambiguous",
            "no_per_call_persistence",
            "no_oi_compare_seam",
            "prebuild_gated",
            "no_fno_data",
            "no_ledger",
            "no_bridge_file",
            "no_rate_log_configured",
            "no_rate_log_lines",
            "no_per_call_log",
            "no_dataset",
            "no_datasets",
            "no_lake",
            "no_instrument_store",
            "out_of_calendar_coverage",
            "time_budget_exhausted",
            # Round-2 review folds (MIGRATION.md #1797): the registry
            # schema-drift refusal (fix 2) + the non-default-law replan
            # shape routing (fix 3) — extensions are reviewed acts.
            "registry_schema_drift",
            "non_default_window_shape",
            # Round-6 F2 (MIGRATION.md #1797): a movers route 400/500 is a
            # genuine route/data error, NOT an absent master (Golden Rule #10).
            "route_error",
        }


class TestCoverage:
    def test_bad_mode_refused(self) -> None:
        with pytest.raises(AcceptanceModelError, match="unknown coverage mode"):
            Coverage(mode="everything", population=1, examined=1, seed="s")

    def test_selection_cap_truncates(self) -> None:
        cov = make_coverage(
            mode="sample",
            population=500,
            examined=200,
            seed="s",
            selection=[f"k{i}" for i in range(SELECTION_CAP + 5)],
        )
        assert len(cov.selection) == SELECTION_CAP
        assert cov.selection_truncated is True

    def test_selection_under_cap_not_truncated(self) -> None:
        cov = make_coverage(mode="sample", population=5, examined=2, seed="s", selection=["a"])
        assert cov.selection == ("a",)
        assert cov.selection_truncated is False

    def test_to_dict_shape(self) -> None:
        d = _cov().to_dict()
        assert set(d) == {
            "mode",
            "population",
            "examined",
            "seed",
            "selection",
            "selection_truncated",
        }


class TestSubCheckInvariants:
    def test_empty_evidence_refused(self) -> None:
        from brutex.acceptance.model import SubCheckResult

        with pytest.raises(AcceptanceModelError, match="empty evidence"):
            SubCheckResult(id="x", verdict=Verdict.PASS, evidence={})

    def test_evidence_without_units_refused_at_dataclass(self) -> None:
        from brutex.acceptance.model import SubCheckResult

        with pytest.raises(AcceptanceModelError, match="units_checked"):
            SubCheckResult(id="x", verdict=Verdict.PASS, evidence={"k": 1})

    def test_abstain_without_reason_refused(self) -> None:
        with pytest.raises(AcceptanceModelError, match="without a named reason"):
            make_subcheck("x", Verdict.ABSTAIN, units_checked=0, evidence={"k": 1})

    def test_reason_on_non_abstain_refused(self) -> None:
        with pytest.raises(AcceptanceModelError, match="non-ABSTAIN"):
            make_subcheck(
                "x",
                Verdict.FAIL,
                units_checked=1,
                evidence={"k": 1},
                abstain_reason=AbstainReason.NO_LAKE,
            )

    def test_zero_unit_pass_coerced_to_nothing_to_check(self) -> None:
        # LAW 2: no verdict without work — the dormant-feature trap closed.
        sub = make_subcheck("x", Verdict.PASS, units_checked=0, evidence={"k": 1})
        assert sub.verdict is Verdict.ABSTAIN
        assert sub.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_zero_unit_pass_refused_at_dataclass_layer(self) -> None:
        from brutex.acceptance.model import SubCheckResult

        with pytest.raises(AcceptanceModelError, match="zero units"):
            SubCheckResult(id="x", verdict=Verdict.PASS, evidence={"units_checked": 0, "k": 1})

    def test_zero_unit_fail_stays_fail(self) -> None:
        sub = make_subcheck("x", Verdict.FAIL, units_checked=0, evidence={"k": 1})
        assert sub.verdict is Verdict.FAIL

    def test_structural_flag(self) -> None:
        structural = _sub("a", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_PER_CALL_LOG)
        plain = _sub("b", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_LAKE)
        assert structural.structural is True
        assert plain.structural is False
        assert structural.to_dict()["structural"] is True

    def test_with_streak(self) -> None:
        sub = _sub("a", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_LAKE)
        assert with_streak(sub, 4).abstain_streak == 4


class TestCheckRollup:
    @pytest.mark.parametrize(
        ("verdicts", "expected"),
        [
            ((Verdict.PASS, Verdict.PASS), Verdict.PASS),
            ((Verdict.PASS, Verdict.ABSTAIN), Verdict.PASS),
            ((Verdict.ABSTAIN, Verdict.ABSTAIN), Verdict.ABSTAIN),
            ((Verdict.PASS, Verdict.FAIL), Verdict.FAIL),
            ((Verdict.FAIL, Verdict.ERROR), Verdict.ERROR),
            ((Verdict.ABSTAIN, Verdict.ERROR), Verdict.ERROR),
            ((), Verdict.ABSTAIN),
        ],
    )
    def test_rollup_truth_table(self, verdicts, expected) -> None:
        subs = [
            _sub(
                f"s{i}",
                v,
                units=0 if v is not Verdict.PASS else 1,
                reason=AbstainReason.NO_LAKE if v is Verdict.ABSTAIN else None,
            )
            for i, v in enumerate(verdicts)
        ]
        assert rollup_check_verdict(subs) is expected

    def test_make_check_defaults_fingerprint(self) -> None:
        check = make_check("unique", [_sub("s", Verdict.PASS)], coverage=_cov())
        assert check.input_fingerprint == {"moved": False, "retried": False}
        assert check.to_dict()["check_id"] == "unique"


class TestExitCodeLaw:
    def test_property_style_matrix(self) -> None:
        """Exhaustive truth table over the exit-law inputs (kills flip mutants)."""
        for teeth, err, fail, abstains, budget, streak, trunc in itertools.product(
            (TeethVerdict.TEETH_PROVEN, TeethVerdict.TEETH_FAILED, TeethVerdict.TEETH_SKIPPED),
            (False, True),
            (False, True),
            (0, 3, 4),
            (3,),
            (0, ABSTAIN_STREAK_LIMIT - 1, ABSTAIN_STREAK_LIMIT),
            (False, True),
        ):
            code = exit_code_for(
                teeth=teeth,
                any_error=err,
                any_fail=fail,
                nonstructural_abstains=abstains,
                abstain_budget=budget,
                max_nonstructural_streak=streak,
                time_truncated=trunc,
            )
            if teeth is TeethVerdict.TEETH_FAILED or err:
                assert code == 2
            elif fail:
                assert code == 3
            elif abstains > budget or streak >= ABSTAIN_STREAK_LIMIT or trunc:
                assert code == 4
            else:
                assert code == 0

    def test_precedence_2_over_3_over_4(self) -> None:
        assert (
            exit_code_for(
                teeth=TeethVerdict.TEETH_FAILED,
                any_error=False,
                any_fail=True,
                nonstructural_abstains=99,
                abstain_budget=0,
                max_nonstructural_streak=99,
                time_truncated=True,
            )
            == 2
        )


class TestOverallRollup:
    def test_pass_clean(self) -> None:
        checks = [make_check("unique", [_sub("s", Verdict.PASS)], coverage=_cov())]
        overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3)
        assert overall.verdict is OverallVerdict.PASS
        assert overall.exit_code == 0
        assert overall.passes_full == 1
        assert overall.passes_sample == 0

    def test_pass_with_abstains_and_sample_counter(self) -> None:
        checks = [
            make_check(
                "tf",
                [
                    _sub("a", Verdict.PASS),
                    _sub("b", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_OI_COMPARE_SEAM),
                ],
                coverage=_cov("sample"),
            )
        ]
        overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3)
        assert overall.verdict is OverallVerdict.PASS_WITH_ABSTAINS
        assert overall.abstain_subchecks_structural == 1
        assert overall.abstain_subchecks_nonstructural == 0
        assert overall.passes_sample == 1

    def test_fail_beats_abstain(self) -> None:
        checks = [make_check("pull", [_sub("s", Verdict.FAIL)], coverage=_cov())]
        overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3)
        assert overall.verdict is OverallVerdict.FAIL
        assert overall.exit_code == 3
        assert overall.fail_checks == ("pull",)

    def test_error_suspect(self) -> None:
        checks = [make_check("rate", [_sub("s", Verdict.ERROR, units=0)], coverage=_cov())]
        overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3)
        assert overall.verdict is OverallVerdict.SUSPECT
        assert overall.exit_code == 2
        assert overall.error_checks == ("rate",)

    def test_nonstructural_budget_breach_exit_4(self) -> None:
        subs = [
            _sub(f"s{i}", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_DATASET)
            for i in range(4)
        ]
        checks = [make_check("latency", [*subs, _sub("ok", Verdict.PASS)], coverage=_cov())]
        overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3)
        assert overall.exit_code == 4
        assert overall.abstain_subchecks_nonstructural == 4

    def test_structural_abstains_never_amber(self) -> None:
        subs = [
            _sub(f"s{i}", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_PER_CALL_PERSISTENCE)
            for i in range(10)
        ]
        checks = [make_check("rate", [*subs, _sub("ok", Verdict.PASS)], coverage=_cov())]
        overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=0)
        assert overall.exit_code == 0

    def test_streak_limit_exit_4(self) -> None:
        sub = with_streak(
            _sub("s", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_DATASET),
            ABSTAIN_STREAK_LIMIT,
        )
        checks = [make_check("latency", [sub, _sub("ok", Verdict.PASS)], coverage=_cov())]
        overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3)
        assert overall.exit_code == 4

    def test_headless_absent_rate_log_streak_never_amber(self) -> None:
        # Round-8: 3 sequential HEADLESS nights (no rotating-log sink) carry a
        # NO_RATE_LOG_CONFIGURED abstain with an ever-growing streak. Because it
        # is STRUCTURAL it must NEVER escalate a healthy lake to exit 4.
        for streak in range(1, ABSTAIN_STREAK_LIMIT + 3):
            sub = with_streak(
                _sub(
                    "rate.log_window",
                    Verdict.ABSTAIN,
                    units=0,
                    reason=AbstainReason.NO_RATE_LOG_CONFIGURED,
                ),
                streak,
            )
            checks = [make_check("rate", [sub, _sub("ok", Verdict.PASS)], coverage=_cov())]
            overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=0)
            assert overall.exit_code == 0, f"headless streak {streak} must stay non-amber"
            assert overall.abstain_subchecks_nonstructural == 0

    def test_present_but_silent_rate_log_still_streak_amber(self) -> None:
        # Round-8: the present-but-empty sink (NO_RATE_LOG_LINES) is a possible
        # real problem — it stays NON-structural and DOES escalate at the streak
        # limit, exactly like before the split.
        sub = with_streak(
            _sub(
                "rate.log_window", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_RATE_LOG_LINES
            ),
            ABSTAIN_STREAK_LIMIT,
        )
        checks = [make_check("rate", [sub, _sub("ok", Verdict.PASS)], coverage=_cov())]
        overall = rollup_overall(checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3)
        assert overall.exit_code == 4
        assert overall.abstain_subchecks_nonstructural == 1

    def test_time_truncated_exit_4(self) -> None:
        checks = [make_check("unique", [_sub("s", Verdict.PASS)], coverage=_cov())]
        overall = rollup_overall(
            checks, teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3, time_truncated=True
        )
        assert overall.exit_code == 4
        # zero-FAIL inconclusive stays PASS-shaped in the verdict token
        assert overall.verdict is OverallVerdict.PASS

    def test_totals(self) -> None:
        checks = [
            make_check(
                "x",
                [
                    _sub("a", Verdict.PASS),
                    _sub("b", Verdict.FAIL),
                    _sub("c", Verdict.ABSTAIN, units=0, reason=AbstainReason.NO_LAKE),
                    _sub("d", Verdict.ERROR, units=0),
                ],
                coverage=_cov(),
            )
        ]
        assert totals_for(checks) == {"pass": 1, "fail": 1, "abstain": 1, "error": 1}

    def test_overall_to_dict_shape(self) -> None:
        overall = rollup_overall([], teeth=TeethVerdict.TEETH_PROVEN, abstain_budget=3)
        assert set(overall.to_dict()) == {
            "verdict",
            "exit_code",
            "fail_checks",
            "error_checks",
            "abstain_subchecks_structural",
            "abstain_subchecks_nonstructural",
            "passes_full",
            "passes_sample",
        }
