"""Shared harness for the session-integrity goldens (DEC-EXTENDED-SESSION-OUTAGE-001
+ DEC-SESSION-DENSITY-CENSUS-001).

Imported by BOTH the generator (``generate_session_integrity_golden.py``) and
the consuming test (``test_session_integrity_golden.py``) — the generator never
imports the test module (the preopen-prevday harness precedent, CodeQL
``py/cyclic-import`` discipline). Two artifacts:

* the SYNTHETIC 2021-02-24 extended-day trade list — fabricated flat prices on
  the REAL extended session date (the date is the calendar anchor; the prices
  are openly synthetic, never asserted as market data — Golden Rule #1),
  frozen so the 16:50 forced-exit law can never drift silently;
* the session-density census verdict over a REAL regular 2024 date span with a
  FABRICATED 45-minute hole (labeled synthetic-hole; the dates are only the
  calendar anchor).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar
from brutex.data.session_density import (
    SESSION_DENSITY_ABS_FLOOR,
    SESSION_DENSITY_MIN_FRACTION,
    classify_session_density,
)
from brutex.engine.trade_sim import simulate_trades
from brutex.strategies.types import Side

_IST = ZoneInfo("Asia/Kolkata")

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_TRADES = FIXTURES / "extended_session_trades.json"
GOLDEN_CENSUS = FIXTURES / "session_density_census.json"

#: The 2021-02-24 NSE-outage extended session (close 17:00 IST — SEBI press
#: releases, verified 2026-07-11); prices below are SYNTHETIC.
OUTAGE_DAY = date(2021, 2, 24)


def _bar(hh: int, mm: int, price: str) -> CandleBar:
    ts = datetime(
        OUTAGE_DAY.year, OUTAGE_DAY.month, OUTAGE_DAY.day, hh, mm, tzinfo=_IST
    ).astimezone(UTC)
    p = Decimal(price)
    return CandleBar(timestamp=ts, open=p, high=p, low=p, close=p, volume=0, open_interest=None)


def extended_day_bars() -> list[CandleBar]:
    """A synthetic extended-day feed straddling BOTH the old 15:20 boundary and
    the real 16:50 one — the trade must ride through 15:20 and exit at 16:50."""
    return [
        _bar(15, 15, "100"),
        _bar(15, 19, "100.10"),
        _bar(15, 20, "100.20"),  # the OLD boundary — must NOT force here
        _bar(15, 25, "100.30"),
        _bar(16, 0, "100.40"),
        _bar(16, 49, "100.50"),
        _bar(16, 50, "100.70"),  # the REAL extended boundary — forced here
        _bar(16, 55, "200"),  # never reached
    ]


def extended_day_trades_payload() -> dict[str, Any]:
    """The frozen trade-list payload: signal on bar 0, ride to the forced exit."""
    bars = extended_day_bars()
    signals = [i == 0 for i in range(len(bars))]
    trades = simulate_trades(
        bars,
        signals,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=100_000,
        take_profit_paisa=100_000,
    )
    return {
        "note": (
            "SYNTHETIC prices on the REAL 2021-02-24 extended session "
            "(DEC-EXTENDED-SESSION-OUTAGE-001) — the 16:50 forced-exit golden"
        ),
        "trades": [
            {
                "entry_ts": t.entry_ts.astimezone(UTC).isoformat(),
                "exit_ts": t.exit_ts.astimezone(UTC).isoformat(),
                "pnl_paisa": t.pnl_paisa,
                "mfe_paisa": t.mfe_paisa,
                "mae_paisa": t.mae_paisa,
                "holding_bars": t.holding_bars,
                "direction": t.direction.name,
            }
            for t in trades
        ],
    }


#: The REAL regular 2024 span the census golden anchors on (Tue 2024-01-02 →
#: Thu 2024-01-04); the 45-minute 10:00-10:44 hole on the Wednesday is
#: FABRICATED (synthetic-hole — the dates are only the calendar anchor).
CENSUS_DAYS = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
CENSUS_HOLE_MINUTES = frozenset(range(10 * 60, 10 * 60 + 45))


def census_bar_timestamps() -> list[datetime]:
    out: list[datetime] = []
    for d in CENSUS_DAYS:
        for i in range(375):
            minute_of_day = 9 * 60 + 15 + i
            if d == CENSUS_DAYS[1] and minute_of_day in CENSUS_HOLE_MINUTES:
                continue
            hh, mm = divmod(minute_of_day, 60)
            out.append(datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).astimezone(UTC))
    return out


def census_payload() -> dict[str, Any]:
    """The frozen census-verdict payload over the synthetic-hole span.

    HERMETIC oracle (review round-2): BOTH deficiency thresholds are pinned
    explicitly to the module defaults, so an exported operator env knob
    (``BRUTEX_SESSION_DENSITY_MIN_FRACTION`` / ``_ABS_FLOOR``) can never move
    the frozen bytes — the classifier's fail-soft env fallbacks are reached
    only when an override is not injected.
    """
    verdict = classify_session_density(
        census_bar_timestamps(),
        min_fraction=SESSION_DENSITY_MIN_FRACTION,
        abs_floor=SESSION_DENSITY_ABS_FLOOR,
        today_ist=date(2026, 12, 31),
    )
    return {
        "note": (
            "REAL 2024-01-02..04 calendar anchor, FABRICATED 45-min hole "
            "(DEC-SESSION-DENSITY-CENSUS-001 census golden)"
        ),
        "sessions_checked": verdict.sessions_checked,
        "abstained": [list(pair) for pair in verdict.abstained],
        "flags": [
            {
                "session": f.session.isoformat(),
                "expected": f.expected,
                "observed": f.observed,
                "fraction": f.fraction,
                "largest_gap_minutes": f.largest_gap_minutes,
                "gap_start_ist": f.gap_start_ist.isoformat() if f.gap_start_ist else None,
                "gap_end_ist": f.gap_end_ist.isoformat() if f.gap_end_ist else None,
                "shape": f.shape,
                "known_irregular": f.known_irregular,
            }
            for f in verdict.flags
        ],
        "known_irregular": [f.session.isoformat() for f in verdict.known_irregular],
    }


def render(payload: dict[str, Any]) -> bytes:
    """Deterministic frozen-bytes rendering (sorted keys, no NaN, LF newline)."""
    return (json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
