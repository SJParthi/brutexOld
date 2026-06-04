"""Unit tests for :mod:`brutex.engine.sweep_report` — the bars→table report core.

Pure (bars in, text out); every line+branch exercised for the strict 100% engine
gate. The funnel internals (sweep / rank / Stage-1B / render) are covered by their
own modules' tests — here we pin the report ASSEMBLY: the header context, the
honest empty-board line, and that the guardrails propagate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_report import sweep_report
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
