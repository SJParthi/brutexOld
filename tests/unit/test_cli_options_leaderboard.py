"""Tests for the ``brutex options-sim`` leaderboard renderer.

``_render_options_leaderboard`` is a pure formatter over ``(row,
OptionsRuleResult…)`` with no lake I/O, and the DEC-ETC-REGIME-HISTORY-001
honesty footer inside it carries real derivation logic (the unverified-count
sum + the data-derived refusal-boundary date), so the function is directly
unit-tested and its former ``# pragma: no cover`` was LIFTED — the round-1
adversarial review confirmed the footer was executed by NOTHING while the
ledger claimed coverage (Golden Rule #10); these tests are that regression
pin. The executor SIBLING of the footer (the aggregate
``options.sim.unverified_cost_window`` WARN) is pinned separately in
tests/unit/test_options_executor_rejects.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from brutex.cli import _render_options_leaderboard
from brutex.options.executor import OptionsRuleResult


@dataclass(frozen=True)
class _FakeRow:
    """Minimal winning-run row shape the renderer reads."""

    instrument: str = "NIFTY"
    tf: str = "5m"
    direction: str = "LONG"
    run_id: str = "abcdef0123456789deadbeef"


def _result(
    *,
    rule_name: str,
    net_pnl_paisa: int,
    n_rejected_cost_regime_unverified: int = 0,
) -> OptionsRuleResult:
    """Build a REAL OptionsRuleResult (not a fake) so a field rename on the
    executor dataclass fails these tests loudly at construction."""
    n_accepted = 2
    return OptionsRuleResult(
        rule_name=rule_name,
        n_signals=n_accepted + n_rejected_cost_regime_unverified,
        n_accepted=n_accepted,
        n_rejected_no_data=0,
        n_rejected_tick_floor=0,
        gross_pnl_paisa=net_pnl_paisa + 2_000,
        total_charges_paisa=2_000,
        net_pnl_paisa=net_pnl_paisa,
        n_winners=1,
        n_losers=1,
        n_rejected_cost_regime_unverified=n_rejected_cost_regime_unverified,
    )


def test_footer_present_with_count_and_data_derived_boundary() -> None:
    """Unverified rejections render ONE footer line carrying the summed count
    and the 2024-10-01 boundary DERIVED from the regime tables (never a
    hardcoded date copy)."""
    results = (
        _result(rule_name="atm", net_pnl_paisa=8_000, n_rejected_cost_regime_unverified=3),
        _result(rule_name="otm1", net_pnl_paisa=12_000, n_rejected_cost_regime_unverified=2),
    )
    out = _render_options_leaderboard(_FakeRow(), results)
    lines = out.splitlines()
    footer = lines[-1]
    assert footer.startswith("⚠ 5 trade(s) fell in the UNVERIFIED pre-2024-10-01 ETC")
    assert "skipped, never priced with a fabricated rate" in footer
    assert "COSTS_VERIFIED §20 row 1" in footer
    assert sum(line.startswith("⚠") for line in lines) == 1
    # Header carries the run identity; rows sort net-desc (otm1 first).
    assert lines[0] == "options-sim · NIFTY 5m LONG · run abcdef012345…"
    assert lines[2].startswith("otm1")
    assert lines[3].startswith("atm")


def test_no_footer_when_no_unverified_rejections() -> None:
    """Zero cost-regime rejections ⇒ NO footer line (the leaderboard ends at
    the last rule row)."""
    results = (_result(rule_name="atm", net_pnl_paisa=8_000),)
    out = _render_options_leaderboard(_FakeRow(), results)
    assert "UNVERIFIED" not in out
    assert "⚠" not in out
    assert out.splitlines()[-1].startswith("atm")


def test_footer_open_fallback_when_all_windows_open_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An all-open-ended refusal-window table (no dated end anywhere) renders
    the honest ``pre-open`` fallback instead of a fabricated date."""
    import brutex.costs.constants as costs_constants

    monkeypatch.setattr(
        costs_constants,
        "etc_unverified_windows",
        lambda: {"NSE": ((date(2020, 1, 1), None),)},
    )
    results = (_result(rule_name="atm", net_pnl_paisa=8_000, n_rejected_cost_regime_unverified=1),)
    out = _render_options_leaderboard(_FakeRow(), results)
    assert "UNVERIFIED pre-open ETC" in out.splitlines()[-1]


def test_missing_count_field_fails_loud() -> None:
    """The footer sum uses DIRECT attribute access — a result object without
    ``n_rejected_cost_regime_unverified`` (e.g. after a future field rename)
    raises AttributeError instead of silently rendering no footer forever
    (the getattr-default defect pinned by the round-1 review)."""
    bogus: Any = SimpleNamespace(
        rule_name="atm",
        n_accepted=1,
        win_rate=1.0,
        gross_pnl_paisa=1_000,
        total_charges_paisa=100,
        net_pnl_paisa=900,
    )
    with pytest.raises(AttributeError, match="n_rejected_cost_regime_unverified"):
        _render_options_leaderboard(_FakeRow(), (bogus,))
