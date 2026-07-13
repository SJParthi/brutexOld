"""Unit tests for the composed candidate verdict (DEC-CANDIDATE-VERDICT-001).

``_compose_verdict`` is tested directly across every branch of the three-layer AND
(including the all-pass VALIDATED path), and ``validate_candidate`` is exercised
end-to-end on a synthetic multi-day session — for the strict 100% ``brutex.engine``
package gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from brutex.data.models import CandleBar
from brutex.engine.candidate import (
    CandidateVerdict,
    _compose_verdict,
    validate_candidate,
)
from brutex.engine.validation import OOSValidation
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.deflated_sharpe import DeflatedSharpeResult
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _dsr(dsr: float) -> DeflatedSharpeResult:
    """A DeflatedSharpeResult with a given deflated_sharpe_ratio (others nominal)."""
    return DeflatedSharpeResult(
        observed_sharpe=1.0,
        n_trials=10,
        expected_max_sharpe=0.5,
        deflated_sharpe_ratio=dsr,
        probabilistic_sharpe_ratio=0.999,
    )


def _bar(day: int, minute: int, base: int) -> CandleBar:
    ts = datetime(2026, 5, 13 + day, 9, 16 + minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(f"{base}.00"),
        high=Decimal(f"{base + 1}.00"),
        low=Decimal(f"{base - 1}.00"),
        close=Decimal(f"{base}.50"),
        volume=100,
        open_interest=None,
    )


def _multiday(n_days: int, bars_per_day: int = 5) -> list[CandleBar]:
    return [_bar(d, m, 100 + d) for d in range(n_days) for m in range(bars_per_day)]


def _m(**overrides: Any) -> ResultMetrics:
    base: dict[str, Any] = {
        "net_pnl_paisa": 0,
        "total_return_pct": 0.0,
        "cagr_pct": 0.0,
        "gross_profit_paisa": 0,
        "gross_loss_paisa": 0,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 1.0,
        "calmar_ratio": 1.0,
        "mar_ratio": 0.0,
        "information_ratio": 0.0,
        "omega_ratio": 0.0,
        "max_drawdown_pct": 10.0,
        "max_drawdown_paisa": 0,
        "max_drawdown_duration_bars": 0,
        "recovery_factor": 1.0,
        "ulcer_index": 0.0,
        "largest_losing_trade_paisa": 0,
        "max_consecutive_losses": 2,
        "max_consecutive_wins": 0,
        "total_trades": 50,
        "winning_trades": 30,
        "losing_trades": 20,
        "win_rate": 0.6,
        "profit_factor": 1.5,
        "avg_winning_trade_paisa": 0,
        "avg_losing_trade_paisa": 0,
        "win_loss_ratio": 1.5,
        "expectancy_paisa": 0,
        "avg_mfe_paisa": 0,
        "avg_mae_paisa": 0,
        "avg_holding_time_bars": 0.0,
        "max_holding_time_bars": 0,
        "time_in_market_pct": 0.0,
        "tail_ratio": 0.0,
        "skewness": 0.0,
        "kurtosis": 0.0,
        "returns_std_dev": 0.0,
    }
    base.update(overrides)
    return ResultMetrics(**base)


class TestComposeVerdict:
    def test_all_three_pass_is_validated(self) -> None:
        assert _compose_verdict(True, _dsr(0.99), 0.8, dsr_min=0.95, cpcv_min=0.5) is True

    def test_oos_fail_rejects(self) -> None:
        assert _compose_verdict(False, _dsr(0.99), 0.8, dsr_min=0.95, cpcv_min=0.5) is False

    def test_dsr_none_rejects(self) -> None:
        assert _compose_verdict(True, None, 0.8, dsr_min=0.95, cpcv_min=0.5) is False

    def test_dsr_below_floor_rejects(self) -> None:
        assert _compose_verdict(True, _dsr(0.90), 0.8, dsr_min=0.95, cpcv_min=0.5) is False

    def test_cpcv_none_rejects(self) -> None:
        assert _compose_verdict(True, _dsr(0.99), None, dsr_min=0.95, cpcv_min=0.5) is False

    def test_cpcv_below_floor_rejects(self) -> None:
        assert _compose_verdict(True, _dsr(0.99), 0.3, dsr_min=0.95, cpcv_min=0.5) is False


class TestValidateCandidate:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            validate_candidate([], 0, [], direction=Side.BUY, tf_minutes=1)

    def test_runs_all_three_and_composes(self) -> None:
        bars = _multiday(4, bars_per_day=5)
        result = validate_candidate(bars, 0, [(0, _m())], direction=Side.BUY, tf_minutes=1)
        assert isinstance(result, CandidateVerdict)
        assert result.mask == 0
        assert isinstance(result.oos, OOSValidation)
        # validated is EXACTLY the composition of the three component results.
        assert result.validated == _compose_verdict(
            result.oos.held_up,
            result.deflated_sharpe,
            result.cpcv_pass_rate,
            dsr_min=0.95,
            cpcv_min=0.5,
        )

    def test_roll_schedule_threads_into_both_composed_builds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ADR-0012 pass-through (review round-2 finding 2): validate_candidate
        # previously exposed NO way to pass a schedule — schedule-blind
        # bar_bits were the only possible result for roll-stitched futures
        # bars. Spy on BOTH composed Stage-1A builds (validation's OOS build
        # AND robustness's CPCV build) and prove the schedule reaches each.
        import brutex.engine.robustness as robustness_mod
        from brutex.engine import validation as validation_mod
        from brutex.engine.stage1a import build_bar_bits as real_build

        bars = _multiday(4, bars_per_day=5)
        covering = (bars[-1].timestamp.astimezone(_IST).date().toordinal(),)
        seen: dict[str, tuple[object, object]] = {}

        def _spy_for(site: str) -> Any:
            def spy(*args: Any, **kwargs: Any) -> Any:
                seen[site] = (kwargs.get("roll_schedule"), kwargs.get("apply_1day_mask"))
                return real_build(*args, **kwargs)

            return spy

        monkeypatch.setattr(validation_mod, "build_bar_bits", _spy_for("oos"))
        monkeypatch.setattr(robustness_mod, "build_bar_bits", _spy_for("cpcv"))
        validate_candidate(
            bars,
            0,
            [(0, _m())],
            direction=Side.BUY,
            tf_minutes=1,
            roll_schedule=covering,
            apply_1day_mask=True,
        )
        assert seen == {"oos": (covering, True), "cpcv": (covering, True)}

    def test_default_call_stays_the_spot_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.engine.robustness as robustness_mod
        from brutex.engine import validation as validation_mod
        from brutex.engine.stage1a import build_bar_bits as real_build

        bars = _multiday(4, bars_per_day=5)
        seen: dict[str, tuple[object, object]] = {}

        def _spy_for(site: str) -> Any:
            def spy(*args: Any, **kwargs: Any) -> Any:
                seen[site] = (kwargs.get("roll_schedule"), kwargs.get("apply_1day_mask"))
                return real_build(*args, **kwargs)

            return spy

        monkeypatch.setattr(validation_mod, "build_bar_bits", _spy_for("oos"))
        monkeypatch.setattr(robustness_mod, "build_bar_bits", _spy_for("cpcv"))
        validate_candidate(bars, 0, [(0, _m())], direction=Side.BUY, tf_minutes=1)
        assert seen == {"oos": (None, False), "cpcv": (None, False)}

    def test_policy_threads_into_both_composed_re_simulations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NIT-2 (DEC-FILL-WORST-CASE-001 fix round 2): validate_candidate was
        # fill-mode-blind — no way to validate a candidate under the SAME
        # mode its sweep ran. Spy on BOTH composed re-simulations (the OOS
        # holdout AND the CPCV return-series producer) via the candidate
        # module's own bindings and prove the policy reaches each; a severed
        # pass-through fails this pin.
        from brutex.engine import candidate as candidate_mod
        from brutex.engine.robustness import combo_returns as real_returns
        from brutex.engine.trade_sim import FillPolicy
        from brutex.engine.validation import validate_out_of_sample as real_oos
        from brutex.execution.fill_mode import FillMode

        bars = _multiday(4, bars_per_day=5)
        policy = FillPolicy(fill_mode=FillMode.WORST_CASE)
        seen: dict[str, object] = {}

        def _oos_spy(*args: Any, **kwargs: Any) -> Any:
            seen["oos"] = kwargs.get("policy")
            return real_oos(*args, **kwargs)

        def _returns_spy(*args: Any, **kwargs: Any) -> Any:
            seen["cpcv"] = kwargs.get("policy")
            return real_returns(*args, **kwargs)

        monkeypatch.setattr(candidate_mod, "validate_out_of_sample", _oos_spy)
        monkeypatch.setattr(candidate_mod, "combo_returns", _returns_spy)
        validate_candidate(bars, 0, [(0, _m())], direction=Side.BUY, tf_minutes=1, policy=policy)
        assert seen == {"oos": policy, "cpcv": policy}
        # The default call stays the library-PRECISE legacy path.
        seen.clear()
        validate_candidate(bars, 0, [(0, _m())], direction=Side.BUY, tf_minutes=1)
        from brutex.engine.trade_sim import DEFAULT_FILL_POLICY

        assert seen == {"oos": DEFAULT_FILL_POLICY, "cpcv": DEFAULT_FILL_POLICY}
