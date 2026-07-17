"""Timeframe.duration_s — the seconds-keyed engine surface (DEC-TF-REGISTRY-DYNAMIC-001)."""

from __future__ import annotations

from brutex.engine.sweep_orchestrator import Timeframe


def test_duration_s_is_trading_seconds() -> None:
    assert Timeframe("5minute", 5).duration_s == 300


def test_duration_s_daily_is_session_seconds() -> None:
    """1day carries the 375-minute TRADING session — 22_500 s, never 86_400."""
    assert Timeframe("1day", 375).duration_s == 22_500
