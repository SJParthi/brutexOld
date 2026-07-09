"""Node-executed goldens for the losing-streak resilience builders in ``bx-oneclick.js``.

The /strategy hero panel + the /operator rank-1 hooks read the sibling
``resilience`` block: the MAX cold streak from
``row.metrics.max_consecutive_losses`` (no rival column) and every other cell
from ``row.resilience.*``. These pure builders are node-golden-tested here
(the bx-oneclick.js / bx-vix-band precedent — the browser IIFE self-guards on
``document`` so the CommonJS exports are the pure primitives only):

* ``bxResilienceCells`` renders the LOCKED panel cell strings for a LIVE row,
  degrades every cell to "—" + the honest unavailable guide for a NULL row,
  and handles the 0-loss / still-underwater honesty branches;
* ``bxResilienceSelfLocate`` is the pure "you are N losses in" verdict with the
  three locked colour tiers (● inside / ▲ rare-but-seen / ✕ unprecedented);
* ``bxIstDateTime`` is TZ-invariant (pinned under ``TZ=America/New_York``);
* a hostile string in a resilience field passes through as DATA (the builder
  never composes markup — the template's textContent is the render law).

Skipped (visibly) where node is absent; the CI node-presence guard lives in
``test_dashboard_oneclick_frontend.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_ONECLICK_JS = _STATIC / "bx-oneclick.js"

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")

#: The pinned honest copy (ONE constant in the JS — drift fails here).
_UNAVAILABLE = "Resilience stats unavailable (re-simulation parity mismatch or no trades)."

#: A signed integer-paisa → ₹ formatter, inlined into the node expression (the
#: bx-explain exact converter is separately tested; here we only pin the cell
#: COMPOSITION "money (pct%)").
_FMT = "(p)=>((p>=0?'+₹':'−₹')+(Math.abs(p)/100).toFixed(2))"

#: A fully-populated LIVE resilience row.
_LIVE_ROW = {
    "metrics": {"max_consecutive_losses": 5},
    "resilience": {
        "stats_live": True,
        "n_trades": 50,
        "losing_streak_p50": 2,
        "losing_streak_p95": 4,
        "losing_streak_count": 8,
        "worst_streak_pnl_paisa": -125000,
        "worst_streak_pnl_pct": -12.5,
        "worst_streak_len": 4,
        "worst_streak_start": "2024-03-01T04:15:00+00:00",
        "worst_streak_end": "2024-03-05T09:20:00+00:00",
        "longest_underwater_trades": 7,
        "longest_underwater_days": 30,
        "still_underwater_at_end": False,
        "worst_loss_month": "2024-03",
        "worst_loss_month_count": 6,
        "worst_loss_week": "2024-W10",
        "worst_loss_week_count": 3,
        "win_rate_by_year": {"2024": [30, 50]},
        "win_rate_year_spread": 0.15,
        "losses_by_month": {"2024-03": 6},
        "fingerprint": "fp",
    },
}

#: The all-None NULL block (resilience_payload(None) shape).
_NULL_RESILIENCE = {
    "stats_live": False,
    "n_trades": None,
    "losing_streak_p50": None,
    "losing_streak_p95": None,
    "losing_streak_count": None,
    "worst_streak_pnl_paisa": None,
    "worst_streak_pnl_pct": None,
    "worst_streak_len": None,
    "worst_streak_start": None,
    "worst_streak_end": None,
    "longest_underwater_trades": None,
    "longest_underwater_days": None,
    "still_underwater_at_end": None,
    "worst_loss_month": None,
    "worst_loss_month_count": None,
    "worst_loss_week": None,
    "worst_loss_week_count": None,
    "win_rate_by_year": {},
    "win_rate_year_spread": None,
    "losses_by_month": {},
    "fingerprint": None,
}
_NULL_ROW = {"metrics": {"max_consecutive_losses": 0}, "resilience": _NULL_RESILIENCE}

#: A LIVE 0-loss strategy (real 0 max, None percentiles, never a fabricated streak).
_ZERO_LOSS_ROW = {
    "metrics": {"max_consecutive_losses": 0},
    "resilience": {
        **_NULL_RESILIENCE,
        "stats_live": True,
        "n_trades": 40,
        "losing_streak_count": 0,
        "longest_underwater_trades": 0,
        "longest_underwater_days": 0,
        "still_underwater_at_end": False,
        "win_rate_by_year": {"2024": [40, 40]},
    },
}


def _node_call(expr: str, *argv: str, env: dict[str, str] | None = None) -> Any:
    """Run the REAL shipped bx-oneclick.js under node and return the JSON result."""
    script = (
        f"const O = require({json.dumps(str(_ONECLICK_JS))});"
        "const ARGS = process.argv.slice(1).map((a) => JSON.parse(a));"
        f"console.log(JSON.stringify({expr}));"
    )
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", "-e", script, *argv],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return json.loads(proc.stdout)


class TestResilienceCellsLive:
    """bxResilienceCells — the LOCKED cell strings for a fully-populated row."""

    def test_live_row_renders_every_locked_cell(self) -> None:
        out = _node_call(f"O.bxResilienceCells(ARGS[0], {_FMT})", json.dumps(_LIVE_ROW))
        assert out["live"] is True
        assert out["worstStreak"] == "5 in a row"
        assert out["worstStreakSub"] == "typical 2 · P95 4"
        assert out["cost"] == "−₹1250.00 (-12.5%)"
        # UTC 04:15 + 05:30 = 09:45 IST; 09:20 + 05:30 = 14:50 IST.
        assert out["costSub"] == "4 losses · 2024-03-01 09:45 IST → 2024-03-05 14:50 IST"
        assert out["underwater"] == "7 trades · 30 days"
        assert out["underwaterBadge"] == "● recovered"
        assert out["badgeLevel"] == "ok"
        assert out["worstMonth"] == "2024-03 (6 losses)"
        assert out["worstMonthSub"] == "worst week 2024-W10"
        assert out["winSpread"] == "swing 0.15"
        assert out["guide"].startswith(
            "Inside its envelope: cold streaks typically run 2, 95% stay at or under 4"
        )

    def test_money_cell_reads_dash_without_a_formatter(self) -> None:
        out = _node_call("O.bxResilienceCells(ARGS[0])", json.dumps(_LIVE_ROW))
        assert out["cost"] == "—"

    def test_still_underwater_badge_and_lower_bound_guide(self) -> None:
        row = json.loads(json.dumps(_LIVE_ROW))
        row["resilience"]["still_underwater_at_end"] = True
        row["resilience"]["longest_underwater_days"] = 45
        out = _node_call(f"O.bxResilienceCells(ARGS[0], {_FMT})", json.dumps(row))
        assert out["underwaterBadge"] == "STILL UNDERWATER at backtest end"
        assert out["badgeLevel"] == "bad"
        assert "NOT recovered by the backtest end" in out["guide"]
        assert "45-day underwater figure is a lower bound" in out["guide"]


class TestResilienceCellsDegrades:
    """bxResilienceCells — NULL + 0-loss honesty (never a fabricated streak)."""

    def test_null_row_every_cell_dash_and_unavailable_guide(self) -> None:
        out = _node_call(f"O.bxResilienceCells(ARGS[0], {_FMT})", json.dumps(_NULL_ROW))
        assert out["live"] is False
        for cell in (
            "worstStreak",
            "worstStreakSub",
            "cost",
            "costSub",
            "underwater",
            "worstMonth",
            "winSpread",
        ):
            assert out[cell] == "—", cell
        assert out["badgeLevel"] == "na"
        assert out["guide"] == _UNAVAILABLE

    def test_missing_resilience_key_degrades_like_null(self) -> None:
        out = _node_call(f"O.bxResilienceCells(ARGS[0], {_FMT})", json.dumps({"metrics": {}}))
        assert out["live"] is False
        assert out["guide"] == _UNAVAILABLE

    def test_zero_loss_live_row_reads_no_streaks_never_a_measured_zero(self) -> None:
        out = _node_call(f"O.bxResilienceCells(ARGS[0], {_FMT})", json.dumps(_ZERO_LOSS_ROW))
        assert out["live"] is True
        assert out["worstStreak"] == "0 in a row"
        assert out["worstStreakSub"] == "typical — · P95 —"
        assert out["cost"] == "—"
        assert out["costSub"] == "no losing runs recorded"
        assert out["worstMonth"] == "no losing month"
        assert out["worstMonthSub"] == ""
        assert out["winSpread"] == "—"
        assert out["guide"] == (
            "This strategy had NO losing streaks in the backtest — treat any live "
            "cold run as unmapped territory and size down."
        )


class TestResilienceSelfLocate:
    """bxResilienceSelfLocate — the three locked verdict tiers (pure, no backend)."""

    @pytest.mark.parametrize(
        ("n", "level", "symbol"),
        [(0, "ok", "●"), (4, "ok", "●"), (5, "warn", "▲"), (6, "bad", "✕"), (100, "bad", "✕")],
    )
    def test_tier_thresholds(self, n: int, level: str, symbol: str) -> None:
        out = _node_call(
            "O.bxResilienceSelfLocate(ARGS[0], ARGS[1])", json.dumps(n), json.dumps(_LIVE_ROW)
        )
        assert out["level"] == level
        assert out["symbol"] == symbol

    def test_verdict_texts_are_the_locked_copy(self) -> None:
        ok = _node_call("O.bxResilienceSelfLocate(2, ARGS[0])", json.dumps(_LIVE_ROW))
        warn = _node_call("O.bxResilienceSelfLocate(5, ARGS[0])", json.dumps(_LIVE_ROW))
        bad = _node_call("O.bxResilienceSelfLocate(9, ARGS[0])", json.dumps(_LIVE_ROW))
        assert ok["text"] == "inside envelope — historically normal, hold"
        assert warn["text"] == "rare but seen — this strategy has been here and recovered"
        assert bad["text"] == "unprecedented — deeper than anything in the backtest. Reassess."

    @pytest.mark.parametrize("n_js", ["-1", "2.5", "NaN"])
    def test_invalid_count_reads_na(self, n_js: str) -> None:
        out = _node_call(f"O.bxResilienceSelfLocate({n_js}, ARGS[0])", json.dumps(_LIVE_ROW))
        assert out["level"] == "na"
        assert out["text"] == "enter a whole number of losses in a row"

    def test_null_resilience_reads_na(self) -> None:
        out = _node_call("O.bxResilienceSelfLocate(1, ARGS[0])", json.dumps(_NULL_ROW))
        assert out["level"] == "na"
        assert out["text"] == "resilience stats unavailable for this strategy"

    def test_zero_loss_envelope_collapses_to_max(self) -> None:
        """p95 is null for a 0-loss strategy → n=0 ok, any n≥1 unprecedented."""
        ok = _node_call("O.bxResilienceSelfLocate(0, ARGS[0])", json.dumps(_ZERO_LOSS_ROW))
        bad = _node_call("O.bxResilienceSelfLocate(1, ARGS[0])", json.dumps(_ZERO_LOSS_ROW))
        assert ok["level"] == "ok"
        assert bad["level"] == "bad"


class TestIstDateTime:
    """bxIstDateTime — TZ-invariant IST rendering (never a NaN string)."""

    def test_utc_iso_reads_ist_wall_clock(self) -> None:
        assert _node_call('O.bxIstDateTime("2024-03-01T04:15:00+00:00")') == "2024-03-01 09:45 IST"

    @pytest.mark.parametrize("bad_js", ["null", '""', '"not-a-date"', "7"])
    def test_null_or_junk_reads_dash(self, bad_js: str) -> None:
        assert _node_call(f"O.bxIstDateTime({bad_js})") == "—"

    def test_is_tz_invariant_under_new_york(self) -> None:
        env = {**os.environ, "TZ": "America/New_York"}
        out = _node_call('O.bxIstDateTime("2024-03-01T04:15:00+00:00")', env=env)
        assert out == "2024-03-01 09:45 IST"


class TestResilienceXss:
    """A hostile string in a resilience field passes through as DATA, never markup."""

    def test_hostile_month_string_is_verbatim_data(self) -> None:
        row = json.loads(json.dumps(_LIVE_ROW))
        payload = "<script>alert(1)</script>"
        row["resilience"]["worst_loss_month"] = payload
        out = _node_call(f"O.bxResilienceCells(ARGS[0], {_FMT})", json.dumps(row))
        # The builder returns the string verbatim (the template's textContent
        # renders it inert — no HTML is ever composed here).
        assert out["worstMonth"] == payload + " (6 losses)"


class TestExports:
    """The builders are exported for the templates AND the node harness."""

    def test_unavailable_constant_is_exported(self) -> None:
        assert _node_call("O.BX_RESILIENCE_UNAVAILABLE") == _UNAVAILABLE
