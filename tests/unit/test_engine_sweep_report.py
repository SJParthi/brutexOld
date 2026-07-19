"""Unit tests for :mod:`brutex.engine.sweep_report` — the bars→table report core.

Pure (bars in, text out); every line+branch exercised for the strict 100% engine
gate. The funnel internals (sweep / rank / Stage-1B / render) are covered by their
own modules' tests — here we pin the report ASSEMBLY: the header context, the
honest empty-board line, and that the guardrails propagate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_report import (
    SORT_KEYS,
    filter_oos_held,
    oos_section,
    render_oos_verdict,
    sort_ranked,
    sweep_report,
)
from brutex.engine.validation import OOSValidation
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import RankedCombo
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(minute: int, close: str) -> CandleBar:
    ts = datetime(2026, 5, 13, 9, minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("102.00"),
        low=Decimal("99.00"),
        close=Decimal(close),
        volume=100,
        open_interest=None,
    )


def _bars() -> list[CandleBar]:
    return [_bar(16, "100.50"), _bar(17, "101.50"), _bar(18, "100.40"), _bar(19, "99.20")]


class TestSweepReport:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            sweep_report(
                [],
                instrument="NIFTY",
                tf_label="1m",
                tf_minutes=1,
                direction=Side.BUY,
                condition_bits=[0, 1, 2],
                k=2,
            )

    def test_header_reports_context(self) -> None:
        report = sweep_report(
            _bars(),
            instrument="NIFTY",
            tf_label="1m",
            tf_minutes=1,
            direction=Side.BUY,
            condition_bits=[0, 1, 2],
            k=2,
        )
        # Self-describing header: instrument/TF/direction, the combo space (C(3,2)=3),
        # the bar count, and how many cleared the gates (0 on this tiny window).
        assert "BruteX sweep — NIFTY 1m BUY" in report
        assert "conditions=3  K=2  combos=3" in report
        assert "bars=4" in report
        assert "gate survivors=0" in report

    def test_no_survivors_renders_empty_message(self) -> None:
        # 4 synthetic bars never clear the 30-trade gate → the honest empty line,
        # not an error.
        report = sweep_report(
            _bars(),
            instrument="BANKNIFTY",
            tf_label="5m",
            tf_minutes=5,
            direction=Side.SELL,
            condition_bits=[10, 20, 30],
            k=1,
        )
        assert "(no combos cleared the gates)" in report

    def test_k_too_large_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid k"):
            sweep_report(
                _bars(),
                instrument="NIFTY",
                tf_label="1m",
                tf_minutes=1,
                direction=Side.BUY,
                condition_bits=[0, 1],
                k=3,
            )

    def test_over_cap_raises(self) -> None:
        # C(52,2)=1326 combos > max_combos=10 → refused before any compute.
        with pytest.raises(ValueError, match="exceeds max_combos=10"):
            sweep_report(
                _bars(),
                instrument="NIFTY",
                tf_label="1m",
                tf_minutes=1,
                direction=Side.BUY,
                condition_bits=list(range(52)),
                k=2,
                max_combos=10,
            )


def _rc(
    *,
    score: float = 0.0,
    net_pnl_paisa: int = 0,
    win_rate: float = 0.5,
    profit_factor: float | None = 1.5,
    mask: int = 1,
) -> RankedCombo:
    """A RankedCombo whose sort-relevant metrics are controllable."""
    base: dict[str, Any] = {
        "net_pnl_paisa": net_pnl_paisa,
        "total_return_pct": 0.0,
        "cagr_pct": 0.0,
        "gross_profit_paisa": 0,
        "gross_loss_paisa": 0,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.0,
        "calmar_ratio": 1.0,
        "mar_ratio": 0.0,
        "information_ratio": 0.0,
        "omega_ratio": 0.0,
        "max_drawdown_pct": 5.0,
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
        "win_rate": win_rate,
        "profit_factor": profit_factor,
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
        # The loss/growth trio (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
        # DEC-METRICS-KELLY-GROWTH-001): loss_rate consistent with the
        # counts above (losing 20 / total 50); the Kelly pair defaults
        # None (undefined) — override per test where the value matters.
        "loss_rate": 0.4,
        "kelly_fraction": None,
        "log_growth_at_half_kelly": None,
    }
    return RankedCombo(mask=mask, metrics=ResultMetrics(**base), composite_score=score)


class TestSortRanked:
    """DEC-SWEEP-SORT-001: re-order the gate-passing board by a chosen headline metric."""

    def test_sort_keys_constant(self) -> None:
        assert SORT_KEYS == ("composite", "pnl", "win", "pf")

    def test_sort_by_composite(self) -> None:
        combos = [_rc(score=1.0, mask=1), _rc(score=3.0, mask=2), _rc(score=2.0, mask=3)]
        assert [c.mask for c in sort_ranked(combos, "composite")] == [2, 3, 1]

    def test_sort_by_pnl(self) -> None:
        combos = [
            _rc(net_pnl_paisa=100, mask=1),
            _rc(net_pnl_paisa=300, mask=2),
            _rc(net_pnl_paisa=200, mask=3),
        ]
        assert [c.mask for c in sort_ranked(combos, "pnl")] == [2, 3, 1]

    def test_sort_by_win(self) -> None:
        combos = [_rc(win_rate=0.4, mask=1), _rc(win_rate=0.7, mask=2), _rc(win_rate=0.5, mask=3)]
        assert [c.mask for c in sort_ranked(combos, "win")] == [2, 3, 1]

    def test_sort_by_pf_with_none_last(self) -> None:
        # profit_factor None (no losing trades) sorts LAST.
        combos = [
            _rc(profit_factor=1.2, mask=1),
            _rc(profit_factor=None, mask=2),
            _rc(profit_factor=2.0, mask=3),
        ]
        assert [c.mask for c in sort_ranked(combos, "pf")] == [3, 1, 2]

    def test_sort_is_stable_on_ties(self) -> None:
        # Equal pnl → incoming (composite) order preserved (deterministic).
        combos = [_rc(net_pnl_paisa=100, mask=7), _rc(net_pnl_paisa=100, mask=9)]
        assert [c.mask for c in sort_ranked(combos, "pnl")] == [7, 9]

    def test_unknown_sort_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown sort key"):
            sort_ranked([], "bogus")


class TestSweepReportSort:
    def test_header_shows_sort_key(self) -> None:
        report = sweep_report(
            _bars(),
            instrument="NIFTY",
            tf_label="1m",
            tf_minutes=1,
            direction=Side.BUY,
            condition_bits=[0, 1, 2],
            k=2,
            sort_by="pnl",
        )
        assert "sort=pnl" in report

    def test_invalid_sort_key_propagates(self) -> None:
        with pytest.raises(ValueError, match="unknown sort key"):
            sweep_report(
                _bars(),
                instrument="NIFTY",
                tf_label="1m",
                tf_minutes=1,
                direction=Side.BUY,
                condition_bits=[0, 1, 2],
                k=2,
                sort_by="bogus",
            )


def _multiday_bars() -> list[CandleBar]:
    """16 in-session bars spanning TWO IST days (so the OOS split is valid)."""
    out: list[CandleBar] = []
    for day in (2, 3):  # 2024-01-02, 2024-01-03
        for i in range(8):
            ts = datetime(2024, 1, day, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal("100.00"),
                    high=Decimal("102.00"),
                    low=Decimal("99.00"),
                    close=Decimal("100.50"),
                    volume=1000,
                    open_interest=None,
                )
            )
    return out


def _oos(*, held: bool, gates: bool, sharpe: bool, dd: bool) -> OOSValidation:
    m = _rc().metrics
    return OOSValidation(
        train_metrics=m,
        test_metrics=m,
        train_days=3,
        test_days=2,
        gates_held=gates,
        sharpe_held=sharpe,
        drawdown_held=dd,
        held_up=held,
    )


class TestRenderOosVerdict:
    """DEC-SWEEP-OOS-001: the no-illusion 'did the edge survive unseen data?' line."""

    def test_held(self) -> None:
        text = render_oos_verdict(_oos(held=True, gates=True, sharpe=True, dd=True))
        assert "HELD ✓" in text
        assert "gates=✓ sharpe=✓ dd=✓" in text
        assert "train 3d → test 2d" in text
        assert "test trades=50" in text

    def test_failed_with_attributable_components(self) -> None:
        text = render_oos_verdict(_oos(held=False, gates=False, sharpe=True, dd=False))
        assert "FAILED ✗" in text
        assert "gates=✗ sharpe=✓ dd=✗" in text


class TestOosSection:
    def test_empty_ranked(self) -> None:
        assert "no gate-passing combos" in oos_section(
            _multiday_bars(),
            [],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=None,
            take_profit_paisa=None,
            start_equity_paisa=10_000_000,
            proximity=None,
            prior_n=3,
        )

    def test_single_day_window_is_na(self) -> None:
        # _bars() is one IST day → cannot split → honest n/a (same for all combos).
        out = oos_section(
            _bars(),
            [_rc(mask=0)],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=None,
            take_profit_paisa=None,
            start_equity_paisa=10_000_000,
            proximity=None,
            prior_n=3,
        )
        assert "n/a" in out

    def test_multiday_renders_verdict_per_combo(self) -> None:
        out = oos_section(
            _multiday_bars(),
            [_rc(mask=0)],
            direction=Side.BUY,
            tf_minutes=1,
            stop_loss_paisa=None,
            take_profit_paisa=None,
            start_equity_paisa=10_000_000,
            proximity=None,
            prior_n=3,
        )
        assert "Out-of-sample (train 70% → unseen 30%):" in out
        assert "#1" in out


class TestSweepReportOos:
    def test_validate_oos_appends_section(self) -> None:
        report = sweep_report(
            _bars(),
            instrument="NIFTY",
            tf_label="1m",
            tf_minutes=1,
            direction=Side.BUY,
            condition_bits=[0, 1, 2],
            k=2,
            validate_oos=True,
        )
        assert "Out-of-sample" in report

    def test_validate_oos_top_n_none(self) -> None:
        report = sweep_report(
            _bars(),
            instrument="NIFTY",
            tf_label="1m",
            tf_minutes=1,
            direction=Side.BUY,
            condition_bits=[0, 1, 2],
            k=2,
            validate_oos=True,
            top_n=None,
        )
        assert "Out-of-sample" in report


class TestFilterOosHeld:
    """DEC-SWEEP-OOS-ONLY-001: keep only combos whose edge held on unseen data."""

    def _params(self) -> dict[str, Any]:
        return {
            "direction": Side.BUY,
            "tf_minutes": 1,
            "stop_loss_paisa": None,
            "take_profit_paisa": None,
            "start_equity_paisa": 10_000_000,
            "proximity": None,
            "prior_n": 3,
        }

    def test_keeps_held_drops_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Deterministically: mask 1 holds, mask 2 fails out-of-sample.
        def fake_validate(_bars: object, mask: int, **_kw: object) -> OOSValidation:
            return _oos(held=(mask == 1), gates=(mask == 1), sharpe=True, dd=True)

        monkeypatch.setattr("brutex.engine.sweep_report.validate_out_of_sample", fake_validate)
        kept = filter_oos_held(_multiday_bars(), [_rc(mask=1), _rc(mask=2)], **self._params())
        assert [rc.mask for rc in kept] == [1]

    def test_empty_ranked_is_empty(self) -> None:
        assert filter_oos_held(_multiday_bars(), [], **self._params()) == []

    def test_short_window_propagates_value_error(self) -> None:
        # One IST day → validate_out_of_sample can't split → ValueError propagates.
        with pytest.raises(ValueError, match="distinct trading days"):
            filter_oos_held(_bars(), [_rc(mask=0)], **self._params())


class TestSweepReportOosOnly:
    def test_oos_only_header_note(self) -> None:
        # Synthetic board has no gate survivors → filter over [] → "0/0 held".
        report = sweep_report(
            _multiday_bars(),
            instrument="NIFTY",
            tf_label="1m",
            tf_minutes=1,
            direction=Side.BUY,
            condition_bits=[0, 1, 2],
            k=2,
            oos_only=True,
        )
        assert "oos-only=0/0 held" in report
