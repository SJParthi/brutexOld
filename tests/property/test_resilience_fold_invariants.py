"""Property invariants for the resilience fold (hypothesis over random trades).

The max-consistency tooth (the single-source proof we ship NO rival streak
max), the nearest-rank oracle, the count/sum invariants, determinism, and the
O(√n) streak-histogram memory bound.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from brutex.engine.combo_aggregates import _consecutive_runs, nearest_rank
from brutex.engine.resilience_streaks import (
    _nearest_rank_from_hist,
    fold_resilience,
    losing_run_length_hist,
)
from hypothesis import given
from hypothesis import strategies as st

_IST = timezone(timedelta(hours=5, minutes=30))
_BASE = datetime(2021, 1, 1, 10, 0, tzinfo=_IST)


@dataclass(frozen=True, slots=True)
class _FakeTrade:
    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _trades(pnls: list[int]) -> list[_FakeTrade]:
    out: list[_FakeTrade] = []
    for i, pnl in enumerate(pnls):
        entry = (_BASE + timedelta(days=5 * i)).astimezone(UTC)
        exit_ = (_BASE + timedelta(days=5 * i, minutes=1)).astimezone(UTC)
        out.append(_FakeTrade(pnl_paisa=pnl, entry_ts=entry, exit_ts=exit_))
    return out


_PNLS = st.lists(st.integers(min_value=-10_000, max_value=10_000), max_size=80)
_NONEMPTY_PNLS = st.lists(st.integers(min_value=-10_000, max_value=10_000), min_size=1, max_size=80)


@given(pnls=_PNLS)
def test_max_consistency_tooth(pnls: list[int]) -> None:
    hist = losing_run_length_hist(pnls)
    if hist:
        assert max(hist) == _consecutive_runs(pnls)[0]


@given(pnls=_NONEMPTY_PNLS)
def test_p95_never_exceeds_the_longest_streak(pnls: list[int]) -> None:
    # The single-source proof: the persisted P95 is never above the max the
    # agg carries (== _consecutive_runs[0]); we ship no rival max column.
    rec = fold_resilience(
        "r", _trades(pnls), parity_ok=True, start_equity_paisa=1_000_000, fingerprint="f"
    )
    longest = _consecutive_runs(pnls)[0]
    if rec.losing_streak_p95 is not None:
        assert rec.losing_streak_p95 <= longest


@given(pnls=_NONEMPTY_PNLS, pct=st.sampled_from([50, 95]))
def test_nearest_rank_oracle(pnls: list[int], pct: int) -> None:
    hist = losing_run_length_hist(pnls)
    if not hist:
        return
    expanded = sorted(length for length, freq in hist.items() for _ in range(freq))
    assert _nearest_rank_from_hist(hist, pct) == nearest_rank(expanded, pct)


@given(pnls=_NONEMPTY_PNLS)
def test_count_and_sum_invariants(pnls: list[int]) -> None:
    import json

    rec = fold_resilience(
        "r", _trades(pnls), parity_ok=True, start_equity_paisa=1_000_000, fingerprint="f"
    )
    losses = sum(1 for p in pnls if p < 0)
    by_month = json.loads(rec.losses_by_month_json)
    assert sum(by_month.values()) == losses
    by_year = json.loads(rec.win_rate_by_year_json)
    assert sum(total for _wins, total in by_year.values()) == len(pnls)
    assert rec.longest_underwater_trades is not None
    assert 0 <= rec.longest_underwater_trades <= len(pnls)
    assert rec.worst_streak_pnl_paisa is None or rec.worst_streak_pnl_paisa <= 0
    if rec.losing_streak_p50 is not None and rec.losing_streak_p95 is not None:
        assert rec.losing_streak_p50 <= rec.losing_streak_p95
    if rec.losing_streak_count == 0:
        assert rec.losing_streak_p50 is None and rec.losing_streak_p95 is None


@given(pnls=_NONEMPTY_PNLS)
def test_fold_is_deterministic(pnls: list[int]) -> None:
    trades = _trades(pnls)
    a = fold_resilience("r", trades, parity_ok=True, start_equity_paisa=1_000_000, fingerprint="f")
    b = fold_resilience("r", trades, parity_ok=True, start_equity_paisa=1_000_000, fingerprint="f")
    assert a == b


@given(pnls=_PNLS)
def test_streak_histogram_memory_is_sqrt_bounded(pnls: list[int]) -> None:
    # Distinct positive run lengths summing to <= n ⇒ <= sqrt(2n) distinct keys.
    hist = losing_run_length_hist(pnls)
    assert len(hist) <= math.isqrt(2 * len(pnls)) + 1
