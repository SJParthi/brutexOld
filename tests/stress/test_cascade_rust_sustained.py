"""Sustained large-input stress for the Rust cascade/results-depth laws.

DEC-RUST-CASCADE-RESULTS-001. Drives the Rust-backed public API at a scale
two orders beyond any real finalist (10k trades / 5k minute bars) and pins
(a) determinism — two identical runs produce identical records — and (b) the
independently-recomputed conservation invariants (exit tallies sum to
n_trades; the year buckets sum to the net P&L). Bounded to seconds — the
per-PR stress step budget.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.minute_path import MinuteIndex, ist_session_dates, session_equity_series
from brutex.strategies.types import Side

_T0 = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)  # 09:16 IST


class _Trade:
    def __init__(self, entry_ts: datetime, exit_ts: datetime, pnl: int) -> None:
        self.entry_ts = entry_ts
        self.exit_ts = exit_ts
        self.direction = Side.BUY if pnl >= 0 else Side.SELL
        self.pnl_paisa = pnl
        self.entry_px = Decimal("100.00")


def _bars(n: int) -> list[CandleBar]:
    out: list[CandleBar] = []
    for i in range(n):
        # ~340 in-session minutes per day, then roll to the next day.
        day, minute = divmod(i, 340)
        out.append(
            CandleBar(
                timestamp=_T0 + timedelta(days=day, minutes=minute),
                open=Decimal("100.00"),
                high=Decimal(f"{100 + (i % 7) * 0.25:.2f}"),
                low=Decimal(f"{100 - (i % 5) * 0.5:.2f}"),
                close=Decimal("100.00"),
                volume=1,
                open_interest=None,
            )
        )
    return out


def test_sustained_equity_series_is_deterministic_and_conserving() -> None:
    bars = _bars(5_000)
    index = MinuteIndex.from_bars(bars)
    sessions = ist_session_dates(bars)
    trades: list[_Trade] = []
    cursor = 0
    pnl_cycle = (250, -125, 0, 1_000, -775)
    for i in range(10_000):
        if cursor + 1 >= len(bars):
            break
        entry = bars[cursor].timestamp
        exit_ts = bars[cursor + 1].timestamp
        trades.append(_Trade(entry, exit_ts, pnl_cycle[i % len(pnl_cycle)]))
        cursor += 2
    runs = [
        session_equity_series(index, trades, sessions=sessions, start_equity_paisa=10_000_000)
        for _ in range(2)
    ]
    assert runs[0] == runs[1]  # determinism at scale
    points, status = runs[0]
    assert status.value == "ok"
    # Conservation: the final close equals base + Σ pnl (independent recompute).
    assert points[-1].equity_close_paisa == 10_000_000 + sum(t.pnl_paisa for t in trades)
    assert sum(p.trades_closed for p in points) == len(trades)


def test_sustained_aggregate_fold_conserves_the_year_buckets() -> None:
    from brutex.engine.cascade_rust import require_cascade_rust

    rs = require_cascade_rust()
    n = 10_000
    entry_us = [i * 60_000_000 + 1_704_166_000_000_000 for i in range(n)]
    exit_us = [us + 60_000_000 for us in entry_us]
    pnl = [((i % 11) - 5) * 137 for i in range(n)]
    fold = rs.aggregate_fold(
        entry_us,
        exit_us,
        pnl,
        [abs(p) for p in pnl],
        [abs(p) * 2 for p in pnl],
        [None] * n,
        [None] * n,
        [False] * n,
        ["signal"] * n,
        [None] * n,
        [],
        [],
        [],
        [],
        [],
        [],
        n,
        sum(pnl),
        0,
        True,
        10_000_000,
    )
    assert fold["exits_signal"] == n
    assert sum(json.loads(fold["pnl_by_year_json"]).values()) == sum(pnl)
    assert fold["n_trades"] == n
