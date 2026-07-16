"""Unit tests for the S+/S/A tier classifier (DEC-RANKING-SPLUS-TIER-001).

100% line+branch on ``brutex/ranking/tiers.py`` STANDALONE (its own ci.yml
gate): every axis boundary (exact-at-threshold both sides), every
``None``/``NaN`` arm, the zero-loss PF/WLR pass arms, the stop-missing +
minute-path-absent honest degrades, every cascade state, the blocked_next
token-order stability law, and the config-override seam.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

import pytest

from brutex.metrics.lockbox import LOCKBOX_DEFAULT_MIN_TRADES
from brutex.ranking import MIN_TRADES
from brutex.ranking.tiers import (
    A_THRESHOLDS,
    DEFAULT_TIER_CONFIG,
    MISSING_INPUT_PREFIX,
    S_THRESHOLDS,
    SPLUS_THRESHOLDS,
    TIER_A,
    TIER_CASCADE,
    TIER_CONSECUTIVE_LOSSES,
    TIER_EXPECTANCY_HEADROOM,
    TIER_LABELS,
    TIER_MAX_DRAWDOWN,
    TIER_MIN_TRADES,
    TIER_MINUTE_PATH_DRAWDOWN,
    TIER_NON_FINITE_INPUT,
    TIER_PAYOFF,
    TIER_PROFIT_FACTOR,
    TIER_REASON_ORDER,
    TIER_S,
    TIER_SPLUS,
    TIER_STOP_MISSING,
    TIER_STOP_NOT_CONTAINED,
    TIER_STOP_TOO_WIDE,
    TIER_WIN_RATE,
    TierConfig,
    TierInputs,
    TierThresholds,
    TierVerdict,
    classify_tier,
    tier_config_payload,
)

# ── The S+-passing baseline (every axis clears every tier) ───────────────────
#
# entry ₹24,000 = 2_400_000 paisa; sl 2_000 paisa → 2000/2400000 × 10000 =
# 8.33… bps ≤ 10; mae 2_500 ≤ 1.5 × 2000 = 3_000; H = 1000 (the default).

_BASE: dict[str, Any] = {
    "total_trades": 600,
    "win_rate": 0.70,
    "win_loss_ratio": 1.5,
    "losing_trades": 180,
    "profit_factor": 2.5,
    "gross_loss_paisa": 50_000,
    "expectancy_paisa": 3_000,
    "sl_paisa": 2_000,
    "entry_price_paisa": 2_400_000,
    "mae_1m_worst_paisa": 2_500,
    "trades_with_1m": 600,
    "equity_status": "ok",
    "max_drawdown_pct": 4.0,
    "max_dd_minute_path_pct": 6.0,
    "max_consecutive_losses": 5,
    "cascade_status": "pass",
}


def _inputs(**overrides: Any) -> TierInputs:
    return TierInputs(**{**_BASE, **overrides})


def _tier(**overrides: Any) -> str | None:
    return classify_tier(_inputs(**overrides)).tier


class TestSPlusBaselineAndBoundaries:
    def test_baseline_is_splus_with_empty_blocked_next(self) -> None:
        verdict = classify_tier(_inputs())
        assert verdict == TierVerdict(tier=TIER_SPLUS, blocked_next=())

    def test_exact_at_every_splus_threshold_still_splus(self) -> None:
        # Inclusive boundaries: ≥ floors pass AT the floor, ≤ ceilings pass
        # AT the ceiling; expectancy S+ is inclusive (≥ 2×H); bps exactly
        # 10.0 (sl 2400 on a 2_400_000-paisa entry); mae exactly 1.5×sl.
        verdict = classify_tier(
            _inputs(
                total_trades=500,
                win_rate=0.65,
                win_loss_ratio=1.0,
                profit_factor=2.0,
                expectancy_paisa=2_000,
                sl_paisa=2_400,
                mae_1m_worst_paisa=3_600,  # 1.5 × 2400
                max_drawdown_pct=5.0,
                max_dd_minute_path_pct=7.5,
                max_consecutive_losses=6,
            )
        )
        assert verdict.tier == TIER_SPLUS
        assert verdict.blocked_next == ()

    @pytest.mark.parametrize(
        ("overrides", "token"),
        [
            ({"total_trades": 499}, TIER_MIN_TRADES),
            ({"win_rate": 0.6499999}, TIER_WIN_RATE),
            ({"win_loss_ratio": 0.9999}, TIER_PAYOFF),
            ({"profit_factor": 1.9999}, TIER_PROFIT_FACTOR),
            ({"expectancy_paisa": 1_999}, TIER_EXPECTANCY_HEADROOM),
            ({"sl_paisa": 2_401}, TIER_STOP_TOO_WIDE),  # 10.004… bps > 10
            ({"mae_1m_worst_paisa": 3_001}, TIER_STOP_NOT_CONTAINED),  # > 1.5×2000
            ({"max_drawdown_pct": 5.0000001}, TIER_MAX_DRAWDOWN),
            ({"max_dd_minute_path_pct": 7.5000001}, TIER_MINUTE_PATH_DRAWDOWN),
            ({"max_consecutive_losses": 7}, TIER_CONSECUTIVE_LOSSES),
            ({"cascade_status": "provisional_pass"}, TIER_CASCADE),
        ],
    )
    def test_one_axis_past_the_splus_bar_demotes_to_s_naming_it(
        self, overrides: dict[str, Any], token: str
    ) -> None:
        verdict = classify_tier(_inputs(**overrides))
        assert verdict.tier == TIER_S
        assert verdict.blocked_next == (token,)


class TestSTierBoundaries:
    def test_exact_at_every_s_threshold_is_s(self) -> None:
        verdict = classify_tier(
            _inputs(
                total_trades=200,
                win_rate=0.55,
                win_loss_ratio=0.8,
                profit_factor=1.6,
                expectancy_paisa=1_000,
                sl_paisa=4_800,  # exactly 20 bps of 2_400_000
                mae_1m_worst_paisa=9_600,  # exactly 2.0 × 4800
                max_drawdown_pct=10.0,
                max_dd_minute_path_pct=15.0,
                max_consecutive_losses=8,
                cascade_status="provisional_pass",
            )
        )
        assert verdict.tier == TIER_S
        # blocked_next names the S+ blockers, in stable evaluation order.
        assert verdict.blocked_next == (
            TIER_MIN_TRADES,
            TIER_WIN_RATE,
            TIER_PAYOFF,
            TIER_PROFIT_FACTOR,
            TIER_EXPECTANCY_HEADROOM,
            TIER_STOP_TOO_WIDE,
            TIER_STOP_NOT_CONTAINED,
            TIER_MAX_DRAWDOWN,
            TIER_MINUTE_PATH_DRAWDOWN,
            TIER_CONSECUTIVE_LOSSES,
            TIER_CASCADE,
        )

    @pytest.mark.parametrize(
        ("overrides", "token"),
        [
            ({"total_trades": 199}, TIER_MIN_TRADES),
            ({"win_rate": 0.5499}, TIER_WIN_RATE),
            ({"win_loss_ratio": 0.7999}, TIER_PAYOFF),
            ({"profit_factor": 1.5999}, TIER_PROFIT_FACTOR),
            ({"expectancy_paisa": 999}, TIER_EXPECTANCY_HEADROOM),
            ({"sl_paisa": 4_801}, TIER_STOP_TOO_WIDE),  # > 20 bps
            ({"mae_1m_worst_paisa": 4_001}, TIER_STOP_NOT_CONTAINED),  # > 2.0×2000
            ({"max_drawdown_pct": 10.001}, TIER_MAX_DRAWDOWN),
            ({"max_dd_minute_path_pct": 15.001}, TIER_MINUTE_PATH_DRAWDOWN),
            ({"max_consecutive_losses": 9}, TIER_CONSECUTIVE_LOSSES),
            ({"cascade_status": "not_validated"}, TIER_CASCADE),
        ],
    )
    def test_one_axis_past_the_s_bar_demotes_to_a_naming_it(
        self, overrides: dict[str, Any], token: str
    ) -> None:
        verdict = classify_tier(_inputs(**overrides))
        assert verdict.tier == TIER_A
        assert token in verdict.blocked_next


class TestATierAndBelow:
    def test_a_tier_needs_no_stop_and_no_minute_path(self) -> None:
        # The honest no-minute-data lake shape: A is still reachable.
        verdict = classify_tier(
            _inputs(
                total_trades=30,
                win_rate=0.40,
                win_loss_ratio=0.6,
                profit_factor=1.2,
                expectancy_paisa=1,
                sl_paisa=None,
                mae_1m_worst_paisa=None,
                trades_with_1m=0,
                equity_status="no_1m_data",
                max_dd_minute_path_pct=None,
                max_drawdown_pct=30.0,
                max_consecutive_losses=10,
                cascade_status=None,
            )
        )
        assert verdict.tier == TIER_A
        # blocked_next names the S blockers — the stop + minute-path axes
        # degrade honestly (stop_missing + ONE deduped missing_input:mae_1m).
        assert verdict.blocked_next == (
            TIER_MIN_TRADES,
            TIER_WIN_RATE,
            TIER_PAYOFF,
            TIER_PROFIT_FACTOR,
            TIER_EXPECTANCY_HEADROOM,
            TIER_STOP_MISSING,
            TIER_MAX_DRAWDOWN,  # 30.0 is AT the A ceiling but past S's 10.0
            f"{MISSING_INPUT_PREFIX}mae_1m",
            TIER_CONSECUTIVE_LOSSES,
            TIER_CASCADE,
        )

    def test_expectancy_zero_fails_a_strictly(self) -> None:
        # A's arm is STRICT: expectancy > 0 (exactly 0 is not an edge).
        verdict = classify_tier(_inputs(expectancy_paisa=0))
        assert verdict.tier is None
        assert verdict.blocked_next == (TIER_EXPECTANCY_HEADROOM,)

    @pytest.mark.parametrize(
        ("overrides", "token"),
        [
            ({"total_trades": 29}, TIER_MIN_TRADES),
            ({"win_rate": 0.3999}, TIER_WIN_RATE),
            ({"win_loss_ratio": 0.5999}, TIER_PAYOFF),
            ({"profit_factor": 1.1999}, TIER_PROFIT_FACTOR),
            ({"max_drawdown_pct": 30.001}, TIER_MAX_DRAWDOWN),
            ({"max_consecutive_losses": 11}, TIER_CONSECUTIVE_LOSSES),
            ({"cascade_status": "fail"}, TIER_CASCADE),
        ],
    )
    def test_one_axis_past_the_a_bar_yields_no_tier_naming_it(
        self, overrides: dict[str, Any], token: str
    ) -> None:
        verdict = classify_tier(_inputs(**overrides))
        assert verdict.tier is None
        assert verdict.blocked_next == (token,)

    def test_multi_axis_below_a_names_every_blocker_in_order(self) -> None:
        verdict = classify_tier(
            _inputs(
                total_trades=10,
                win_rate=0.30,
                win_loss_ratio=0.5,
                profit_factor=1.0,
                expectancy_paisa=-100,
                max_drawdown_pct=45.0,
                max_consecutive_losses=12,
                cascade_status="fail",
            )
        )
        assert verdict.tier is None
        assert verdict.blocked_next == (
            TIER_MIN_TRADES,
            TIER_WIN_RATE,
            TIER_PAYOFF,
            TIER_PROFIT_FACTOR,
            TIER_EXPECTANCY_HEADROOM,
            TIER_MAX_DRAWDOWN,
            TIER_CONSECUTIVE_LOSSES,
            TIER_CASCADE,
        )


class TestNoneRatioArms:
    def test_none_wlr_with_zero_losing_trades_passes_payoff(self) -> None:
        assert _tier(win_loss_ratio=None, losing_trades=0) == TIER_SPLUS

    def test_none_wlr_with_losing_trades_fails_payoff_honestly(self) -> None:
        verdict = classify_tier(_inputs(win_loss_ratio=None, losing_trades=5))
        assert verdict.tier is None  # payoff fails at EVERY tier
        assert verdict.blocked_next == (TIER_PAYOFF,)

    def test_zero_gross_loss_passes_pf_regardless_of_the_sentinel(self) -> None:
        # ResultRow persists an undefined PF as the 0.0 sentinel
        # (sweep_run._metric_or_zero) — the zero-loss arm keys on
        # gross_loss_paisa == 0, never on the stored value.
        assert _tier(gross_loss_paisa=0, profit_factor=0.0) == TIER_SPLUS

    def test_none_pf_with_gross_loss_fails_pf_honestly(self) -> None:
        verdict = classify_tier(_inputs(profit_factor=None))
        assert verdict.tier is None
        assert verdict.blocked_next == (TIER_PROFIT_FACTOR,)


class TestStopAxes:
    def test_missing_stop_blocks_s_and_splus_only(self) -> None:
        verdict = classify_tier(_inputs(sl_paisa=None))
        assert verdict.tier == TIER_A
        assert TIER_STOP_MISSING in verdict.blocked_next
        # ONE token — the width/containment sub-axes are unjudgeable
        # without a stop, never three tokens.
        assert TIER_STOP_TOO_WIDE not in verdict.blocked_next
        assert TIER_STOP_NOT_CONTAINED not in verdict.blocked_next

    def test_missing_entry_price_degrades_the_width_axis_honestly(self) -> None:
        verdict = classify_tier(_inputs(entry_price_paisa=None))
        assert verdict.tier == TIER_A
        assert f"{MISSING_INPUT_PREFIX}avg_entry_price" in verdict.blocked_next

    @pytest.mark.parametrize(
        "overrides",
        [
            {"mae_1m_worst_paisa": None},
            {"trades_with_1m": 0},
            {"equity_status": "no_1m_data"},
        ],
    )
    def test_minute_path_absent_degrades_s_and_splus_via_missing_input(
        self, overrides: dict[str, Any]
    ) -> None:
        verdict = classify_tier(_inputs(**overrides))
        assert verdict.tier == TIER_A
        # Deduped: axis 6 (containment) AND axis 7 (minute dd) both need the
        # minute path — ONE token, first occurrence kept.
        assert verdict.blocked_next.count(f"{MISSING_INPUT_PREFIX}mae_1m") == 1

    def test_minute_dd_none_alone_degrades_via_the_same_token(self) -> None:
        verdict = classify_tier(_inputs(max_dd_minute_path_pct=None))
        assert verdict.tier == TIER_A
        assert f"{MISSING_INPUT_PREFIX}mae_1m" in verdict.blocked_next

    def test_custom_row_requiring_stop_without_sub_axes(self) -> None:
        # A custom config CAN demand stop PRESENCE without width/containment
        # ceilings — both optional sub-axes then skip.
        row = replace(SPLUS_THRESHOLDS, max_stop_bps=None, max_mae_sl_multiple=None)
        config = TierConfig(splus=row)
        assert classify_tier(_inputs(entry_price_paisa=None), config=config).tier == TIER_SPLUS

    def test_minute_path_available_property(self) -> None:
        assert _inputs().minute_path_available
        assert not _inputs(mae_1m_worst_paisa=None).minute_path_available
        assert not _inputs(trades_with_1m=0).minute_path_available
        assert not _inputs(equity_status="no_1m_data").minute_path_available


class TestConsecutiveLossesUnknown:
    def test_none_streak_fails_every_tier_via_missing_input(self) -> None:
        verdict = classify_tier(_inputs(max_consecutive_losses=None))
        assert verdict.tier is None
        assert verdict.blocked_next == (f"{MISSING_INPUT_PREFIX}max_consecutive_losses",)


class TestCascadeAxis:
    @pytest.mark.parametrize(
        ("status", "expected_tier"),
        [
            ("pass", TIER_SPLUS),
            ("provisional_pass", TIER_S),
            ("not_validated", TIER_A),
            (None, TIER_A),
            ("fail", None),
        ],
    )
    def test_cascade_status_ladder(self, status: str | None, expected_tier: str | None) -> None:
        verdict = classify_tier(_inputs(cascade_status=status))
        assert verdict.tier == expected_tier
        if expected_tier != TIER_SPLUS:
            assert TIER_CASCADE in verdict.blocked_next


class TestFailClosedNonFinite:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"win_rate": float("nan")},
            {"win_loss_ratio": float("inf")},
            {"profit_factor": float("nan")},
            {"max_drawdown_pct": float("-inf")},
            {"max_dd_minute_path_pct": float("nan")},
        ],
    )
    def test_non_finite_input_yields_the_single_fail_closed_token(
        self, overrides: dict[str, Any]
    ) -> None:
        verdict = classify_tier(_inputs(**overrides))
        assert verdict == TierVerdict(tier=None, blocked_next=(TIER_NON_FINITE_INPUT,))


class TestConfigOverride:
    def test_haircut_override_moves_the_expectancy_floors(self) -> None:
        config = TierConfig(haircut_paisa=500)
        # 1_000 clears 2×500 at S+ under the override…
        assert classify_tier(_inputs(expectancy_paisa=1_000), config=config).tier == TIER_SPLUS
        # …while the default H=1000 demotes the same inputs to S.
        assert classify_tier(_inputs(expectancy_paisa=1_000)).tier == TIER_S

    def test_default_config_haircut_is_the_approved_1000_paisa(self) -> None:
        assert DEFAULT_TIER_CONFIG.haircut_paisa == 1000

    def test_ladder_is_best_first(self) -> None:
        assert DEFAULT_TIER_CONFIG.ladder() == (SPLUS_THRESHOLDS, S_THRESHOLDS, A_THRESHOLDS)


class TestVocabularyStability:
    def test_reason_order_is_the_locked_wire_vocabulary(self) -> None:
        assert TIER_REASON_ORDER == (
            "non_finite_input",
            "min_trades",
            "win_rate",
            "payoff",
            "profit_factor",
            "expectancy_headroom",
            "stop_missing",
            "stop_too_wide",
            "stop_not_contained",
            "max_drawdown",
            "minute_path_drawdown",
            "consecutive_losses",
            "cascade",
        )

    def test_labels_are_the_locked_wire_vocabulary(self) -> None:
        assert TIER_LABELS == ("S+", "S", "A")
        assert (TIER_SPLUS, TIER_S, TIER_A) == TIER_LABELS
        assert MISSING_INPUT_PREFIX == "missing_input:"

    def test_blocked_next_follows_the_reason_order(self) -> None:
        # Every fixed token emitted by a multi-fail verdict appears in
        # TIER_REASON_ORDER order (the stable-order law).
        verdict = classify_tier(
            _inputs(
                win_rate=0.30,
                max_consecutive_losses=20,
                total_trades=5,
                cascade_status="fail",
                max_drawdown_pct=50.0,
            )
        )
        fixed = [t for t in verdict.blocked_next if not t.startswith(MISSING_INPUT_PREFIX)]
        positions = [TIER_REASON_ORDER.index(t) for t in fixed]
        assert positions == sorted(positions)


class TestThresholdCrossPins:
    def test_splus_min_trades_is_the_lockbox_floor(self) -> None:
        assert SPLUS_THRESHOLDS.min_trades == LOCKBOX_DEFAULT_MIN_TRADES == 500

    def test_a_tier_floors_are_the_hard_gate_floors(self) -> None:
        # The A tier mirrors the 5 hard rejection gates where they exist —
        # cross-pinned against the ONE gate home (brutex.ranking /
        # top100_artifact's re-pins) so the two laws can never drift.
        from brutex.engine.top100_artifact import (
            GATE_MAX_CONSECUTIVE_LOSSES,
            GATE_MAX_DRAWDOWN_PCT,
            GATE_MIN_PROFIT_FACTOR,
        )

        assert A_THRESHOLDS.min_trades == MIN_TRADES == 30
        assert A_THRESHOLDS.min_profit_factor == GATE_MIN_PROFIT_FACTOR == 1.2
        assert A_THRESHOLDS.max_drawdown_pct == GATE_MAX_DRAWDOWN_PCT == 30.0
        assert A_THRESHOLDS.max_consecutive_losses == GATE_MAX_CONSECUTIVE_LOSSES == 10

    def test_the_approved_threshold_table_verbatim(self) -> None:
        assert (
            SPLUS_THRESHOLDS.min_trades,
            SPLUS_THRESHOLDS.min_win_rate,
            SPLUS_THRESHOLDS.min_payoff,
            SPLUS_THRESHOLDS.min_profit_factor,
            SPLUS_THRESHOLDS.expectancy_haircut_multiple,
            SPLUS_THRESHOLDS.max_stop_bps,
            SPLUS_THRESHOLDS.max_mae_sl_multiple,
            SPLUS_THRESHOLDS.max_drawdown_pct,
            SPLUS_THRESHOLDS.max_minute_dd_pct,
            SPLUS_THRESHOLDS.max_consecutive_losses,
            SPLUS_THRESHOLDS.cascade_allowed,
        ) == (500, 0.65, 1.0, 2.0, 2.0, 10.0, 1.5, 5.0, 7.5, 6, frozenset({"pass"}))
        assert (
            S_THRESHOLDS.min_trades,
            S_THRESHOLDS.min_win_rate,
            S_THRESHOLDS.min_payoff,
            S_THRESHOLDS.min_profit_factor,
            S_THRESHOLDS.expectancy_haircut_multiple,
            S_THRESHOLDS.max_stop_bps,
            S_THRESHOLDS.max_mae_sl_multiple,
            S_THRESHOLDS.max_drawdown_pct,
            S_THRESHOLDS.max_minute_dd_pct,
            S_THRESHOLDS.max_consecutive_losses,
            S_THRESHOLDS.cascade_allowed,
        ) == (200, 0.55, 0.8, 1.6, 1.0, 20.0, 2.0, 10.0, 15.0, 8, frozenset({"pass", "provisional_pass"}))
        assert (
            A_THRESHOLDS.min_trades,
            A_THRESHOLDS.min_win_rate,
            A_THRESHOLDS.min_payoff,
            A_THRESHOLDS.min_profit_factor,
            A_THRESHOLDS.expectancy_haircut_multiple,
            A_THRESHOLDS.expectancy_exclusive,
            A_THRESHOLDS.requires_stop,
            A_THRESHOLDS.max_stop_bps,
            A_THRESHOLDS.max_mae_sl_multiple,
            A_THRESHOLDS.max_drawdown_pct,
            A_THRESHOLDS.max_minute_dd_pct,
            A_THRESHOLDS.max_consecutive_losses,
            A_THRESHOLDS.cascade_allowed,
        ) == (30, 0.40, 0.6, 1.2, 0.0, True, False, None, None, 30.0, None, 10, None)
        assert SPLUS_THRESHOLDS.requires_stop and S_THRESHOLDS.requires_stop
        assert not SPLUS_THRESHOLDS.expectancy_exclusive
        assert not S_THRESHOLDS.expectancy_exclusive


class TestValidation:
    def test_bad_label_refused(self) -> None:
        with pytest.raises(ValueError, match="label"):
            replace(A_THRESHOLDS, label="B")

    def test_negative_min_trades_refused(self) -> None:
        with pytest.raises(ValueError, match="min_trades"):
            replace(A_THRESHOLDS, min_trades=-1)

    @pytest.mark.parametrize("win_rate", [-0.1, 1.1])
    def test_out_of_range_win_rate_refused(self, win_rate: float) -> None:
        with pytest.raises(ValueError, match="min_win_rate"):
            replace(A_THRESHOLDS, min_win_rate=win_rate)

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("min_payoff", -0.1),
            ("min_profit_factor", float("nan")),
            ("expectancy_haircut_multiple", float("inf")),
            ("max_drawdown_pct", -1.0),
        ],
    )
    def test_non_finite_or_negative_required_float_refused(self, name: str, value: float) -> None:
        with pytest.raises(ValueError, match=name):
            replace(A_THRESHOLDS, **{name: value})

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("max_stop_bps", 0.0),
            ("max_mae_sl_multiple", float("nan")),
            ("max_minute_dd_pct", -1.0),
        ],
    )
    def test_bad_optional_ceiling_refused(self, name: str, value: float) -> None:
        with pytest.raises(ValueError, match=name):
            replace(SPLUS_THRESHOLDS, **{name: value})

    def test_negative_streak_ceiling_refused(self) -> None:
        with pytest.raises(ValueError, match="max_consecutive_losses"):
            replace(A_THRESHOLDS, max_consecutive_losses=-1)

    def test_empty_cascade_allowed_refused(self) -> None:
        with pytest.raises(ValueError, match="cascade_allowed"):
            replace(SPLUS_THRESHOLDS, cascade_allowed=frozenset())

    @pytest.mark.parametrize("haircut", [0, -1])
    def test_non_positive_haircut_refused(self, haircut: int) -> None:
        with pytest.raises(ValueError, match="haircut_paisa"):
            TierConfig(haircut_paisa=haircut)

    def test_mislabelled_config_row_refused(self) -> None:
        with pytest.raises(ValueError, match="label"):
            TierConfig(splus=S_THRESHOLDS)


class TestTierConfigPayload:
    def test_payload_is_json_safe_and_self_describing(self) -> None:
        payload = tier_config_payload()
        # Deterministic, NaN-free JSON (the artifact rendering law).
        rendered = json.dumps(payload, sort_keys=True, allow_nan=False)
        assert json.loads(rendered) == payload
        assert payload["haircut_paisa"] == 1000
        tiers = payload["tiers"]
        assert isinstance(tiers, dict)
        assert set(tiers) == {"S+", "S", "A"}
        assert tiers["S+"]["min_trades"] == 500
        assert tiers["S+"]["cascade_allowed"] == ["pass"]
        assert tiers["S"]["cascade_allowed"] == ["pass", "provisional_pass"]
        assert tiers["A"]["cascade_allowed"] is None
        assert tiers["A"]["expectancy_exclusive"] is True
        assert tiers["A"]["max_stop_bps"] is None

    def test_payload_echoes_a_custom_config(self) -> None:
        payload = tier_config_payload(TierConfig(haircut_paisa=250))
        assert payload["haircut_paisa"] == 250

    def test_non_finite_wlr_guard_ignores_none(self) -> None:
        # The _non_finite helper's None arm: a None ratio is NOT non-finite —
        # it keeps its per-axis None handling (the ranking guard law).
        assert classify_tier(_inputs(win_loss_ratio=None, losing_trades=0)).tier == TIER_SPLUS
        assert math.isfinite(_BASE["win_rate"])  # baseline sanity
