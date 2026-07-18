"""Tests for `brutex.options.strike_rules` — 10 pre-trade rules.

Verifies:
- All 10 rules registered with unique names
- OFFSET rules resolve correctly for BULL (CE) and BEAR (PE) directions
- DELTA_TARGET rules pick the strike with closest |delta|
- Phase gating (delta-target requires Phase 4)
- Rule name lookup
"""

# Whitebox pins on the private Python oracles (DEC-OPTCOST-TAILS-RUST-001):
# pyright: reportPrivateUsage=none

from __future__ import annotations

import pytest
from brutex.options.strike_rules import (
    ALL_STRIKE_RULES,
    DELTA_TARGET_RULES,
    OFFSET_RULES,
    RULES_BY_NAME,
    StrikeRuleKind,
    TradeDirection,
    resolve_delta_target_strike,
    resolve_offset_strike,
)


class TestRuleRegistry:
    def test_10_rules_total(self) -> None:
        assert len(ALL_STRIKE_RULES) == 10

    def test_rule_names_unique(self) -> None:
        names = [r.name for r in ALL_STRIKE_RULES]
        assert len(names) == len(set(names))

    def test_7_offset_rules(self) -> None:
        assert len(OFFSET_RULES) == 7

    def test_3_delta_target_rules(self) -> None:
        assert len(DELTA_TARGET_RULES) == 3

    def test_atm_rule_has_param_zero(self) -> None:
        rule = RULES_BY_NAME["atm"]
        assert rule.kind == StrikeRuleKind.OFFSET
        assert rule.param == 0

    def test_delta_target_050_param(self) -> None:
        rule = RULES_BY_NAME["delta_target_050"]
        assert rule.kind == StrikeRuleKind.DELTA_TARGET
        assert rule.param == 0.50

    def test_offset_rules_do_not_require_greeks(self) -> None:
        for r in OFFSET_RULES:
            assert not r.requires_greeks

    def test_delta_target_rules_require_greeks(self) -> None:
        for r in DELTA_TARGET_RULES:
            assert r.requires_greeks


class TestPhaseGating:
    def test_offset_rules_available_phase_2(self) -> None:
        for r in OFFSET_RULES:
            assert r.applies_to_phase(2)
            assert r.applies_to_phase(4)

    def test_delta_rules_blocked_before_phase_4(self) -> None:
        for r in DELTA_TARGET_RULES:
            assert not r.applies_to_phase(2)
            assert not r.applies_to_phase(3)
            assert r.applies_to_phase(4)


class TestResolveOffsetStrike_Bull:
    """For BULL (CE), 'plus' means HIGHER strike (more OTM call)."""

    def test_atm_returns_atm(self) -> None:
        rule = RULES_BY_NAME["atm"]
        s = resolve_offset_strike(rule, TradeDirection.BULL, atm_strike=24_000, strike_step=50)
        assert s == 24_000

    def test_atm_plus_1_in_dir_bull(self) -> None:
        # CE +1 step = ATM + 50 = 24,050 (further OTM call)
        rule = RULES_BY_NAME["atm_plus_1_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BULL, atm_strike=24_000, strike_step=50)
        assert s == 24_050

    def test_atm_plus_2_in_dir_bull(self) -> None:
        rule = RULES_BY_NAME["atm_plus_2_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BULL, atm_strike=24_000, strike_step=50)
        assert s == 24_100

    def test_atm_plus_5_in_dir_bull(self) -> None:
        rule = RULES_BY_NAME["atm_plus_5_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BULL, atm_strike=24_000, strike_step=50)
        assert s == 24_250

    def test_atm_minus_1_in_dir_bull(self) -> None:
        # CE -1 step = ATM - 50 = 23,950 (more ITM call)
        rule = RULES_BY_NAME["atm_minus_1_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BULL, atm_strike=24_000, strike_step=50)
        assert s == 23_950

    def test_atm_minus_5_in_dir_bull(self) -> None:
        rule = RULES_BY_NAME["atm_minus_5_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BULL, atm_strike=24_000, strike_step=50)
        assert s == 23_750


class TestResolveOffsetStrike_Bear:
    """For BEAR (PE), 'plus' means LOWER strike (more OTM put)."""

    def test_atm_plus_1_in_dir_bear(self) -> None:
        # PE +1 step = ATM - 50 = 23,950 (further OTM put)
        rule = RULES_BY_NAME["atm_plus_1_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BEAR, atm_strike=24_000, strike_step=50)
        assert s == 23_950

    def test_atm_plus_5_in_dir_bear(self) -> None:
        rule = RULES_BY_NAME["atm_plus_5_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BEAR, atm_strike=24_000, strike_step=50)
        assert s == 23_750

    def test_atm_minus_1_in_dir_bear(self) -> None:
        # PE -1 step = ATM + 50 = 24,050 (more ITM put)
        rule = RULES_BY_NAME["atm_minus_1_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BEAR, atm_strike=24_000, strike_step=50)
        assert s == 24_050


class TestStrikeStep:
    """Strike step matters: NIFTY=50, BANKNIFTY=100, SENSEX=100."""

    def test_banknifty_step_100(self) -> None:
        rule = RULES_BY_NAME["atm_plus_2_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BULL, atm_strike=50_000, strike_step=100)
        assert s == 50_200

    def test_sensex_step_100(self) -> None:
        rule = RULES_BY_NAME["atm_plus_5_in_dir"]
        s = resolve_offset_strike(rule, TradeDirection.BULL, atm_strike=80_000, strike_step=100)
        assert s == 80_500


class TestResolveDeltaTarget:
    @pytest.fixture
    def ce_chain(self) -> list[tuple[int, float]]:
        """Synthetic CE chain: 21 strikes, deltas range from 1.0 (deep ITM) to 0.0 (deep OTM)."""
        return [
            (24_000 - 500, 0.95),  # deep ITM
            (24_000 - 250, 0.80),
            (24_000 - 100, 0.65),
            (24_000 - 50, 0.55),
            (24_000, 0.50),  # ATM
            (24_000 + 50, 0.45),
            (24_000 + 100, 0.35),
            (24_000 + 200, 0.20),
            (24_000 + 350, 0.10),
            (24_000 + 500, 0.05),  # deep OTM
        ]

    def test_delta_target_050_picks_atm(self, ce_chain: list[tuple[int, float]]) -> None:
        rule = RULES_BY_NAME["delta_target_050"]
        s = resolve_delta_target_strike(rule, TradeDirection.BULL, ce_chain)
        assert s == 24_000

    def test_delta_target_030_picks_correct(self, ce_chain: list[tuple[int, float]]) -> None:
        # 0.30 target → strike 24,100 has delta 0.35, strike 24,200 has 0.20
        # |0.35 - 0.30| = 0.05; |0.20 - 0.30| = 0.10 → pick 24,100
        rule = RULES_BY_NAME["delta_target_030"]
        s = resolve_delta_target_strike(rule, TradeDirection.BULL, ce_chain)
        assert s == 24_100

    def test_delta_target_015_picks_far_otm(self, ce_chain: list[tuple[int, float]]) -> None:
        # 0.15 target → strike 24,200 has 0.20, strike 24,350 has 0.10
        # |0.20 - 0.15| = 0.05; |0.10 - 0.15| = 0.05 → tie, pick closer to median
        # The chain median strike is 24,000. 24,200 closer to 24,000 than 24,350 → 24,200
        rule = RULES_BY_NAME["delta_target_015"]
        s = resolve_delta_target_strike(rule, TradeDirection.BULL, ce_chain)
        assert s == 24_200

    def test_empty_chain_raises(self) -> None:
        rule = RULES_BY_NAME["delta_target_050"]
        with pytest.raises(ValueError, match="empty"):
            resolve_delta_target_strike(rule, TradeDirection.BULL, [])

    def test_delta_target_on_offset_rule_raises(self) -> None:
        offset_rule = RULES_BY_NAME["atm"]
        with pytest.raises(ValueError, match="non-DELTA"):
            resolve_delta_target_strike(offset_rule, TradeDirection.BULL, [(24_000, 0.5)])

    def test_offset_resolve_on_delta_rule_raises(self) -> None:
        delta_rule = RULES_BY_NAME["delta_target_050"]
        with pytest.raises(ValueError, match="non-OFFSET"):
            resolve_offset_strike(
                delta_rule,
                TradeDirection.BULL,
                24_000,
                50,
            )


class TestSymmetry:
    """A bull CE atm_plus_N at strike X should mirror a bear PE atm_plus_N at strike X' where X and X' are symmetric around ATM."""

    @pytest.mark.parametrize(
        "offset_name",
        [
            "atm_plus_1_in_dir",
            "atm_plus_2_in_dir",
            "atm_plus_5_in_dir",
            "atm_minus_1_in_dir",
            "atm_minus_2_in_dir",
            "atm_minus_5_in_dir",
        ],
    )
    def test_bull_bear_symmetric_around_atm(self, offset_name: str) -> None:
        rule = RULES_BY_NAME[offset_name]
        bull_strike = resolve_offset_strike(
            rule, TradeDirection.BULL, atm_strike=24_000, strike_step=50
        )
        bear_strike = resolve_offset_strike(
            rule, TradeDirection.BEAR, atm_strike=24_000, strike_step=50
        )
        # Bull and bear strikes equidistant from ATM (24,000) but opposite sides
        assert bull_strike + bear_strike == 24_000 * 2


# ── DEC-OPTCOST-TAILS-RUST-001: the flag-ON seam (ON == OFF, refusal bytes) ──


class TestOptcostRustSeamOn:
    """`resolve_offset_strike` under `BRUTEX_OPTCOST_TAILS_RUST=1` — the
    kernel-routed arithmetic matches the Python oracle in both directions;
    the non-OFFSET-rule ValueError stays pre-FFI with its exact bytes."""

    @pytest.fixture(autouse=True)
    def _seam_on(self, monkeypatch: pytest.MonkeyPatch):
        import brutex.optcost_rust as ocr

        pytest.importorskip("fno_math")
        monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, "1")
        ocr.reset_optcost_rust_for_tests()  # burn-in armed — oracle runs beside
        yield
        ocr.reset_optcost_rust_for_tests()

    def test_both_directions_match_the_oracle(self) -> None:
        import brutex.optcost_rust as ocr
        from brutex.options.strike_rules import _resolve_offset_strike_python

        for rule in OFFSET_RULES:
            for direction in TradeDirection:
                assert resolve_offset_strike(rule, direction, 24_000, 50) == (
                    _resolve_offset_strike_python(int(rule.param), direction, 24_000, 50)
                )
        assert not ocr.optcost_rust_disabled()

    def test_non_offset_rule_refusal_bytes_are_pre_ffi(self) -> None:
        rule = RULES_BY_NAME["delta_target_030"]
        with pytest.raises(
            ValueError, match=r"resolve_offset_strike called on non-OFFSET rule 'delta_target_030'"
        ):
            resolve_offset_strike(rule, TradeDirection.BULL, 24_000, 50)

    def test_out_of_i64_atm_takes_the_oracle_bigint_answer(self) -> None:
        rule = RULES_BY_NAME["atm_plus_1_in_dir"]
        atm = 2**64  # past i64 — the kernel abstains, Python's unbounded int serves
        assert resolve_offset_strike(rule, TradeDirection.BULL, atm, 50) == atm + 50
