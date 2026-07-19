"""O(trades) + O(√n)-memory bench for the resilience fold (annotation-only).

``fold_resilience`` does ONE linear pass over the trade list — a fixed amount
of work per trade (O(1) running scalars + O(1) dict upserts) — so the per-trade
cost must be independent of how many trades precede it (CLAUDE.md §5). The
streak-length histogram is the only per-mask growing structure, and its key
count is O(√n) by construction (distinct positive run lengths summing to ≤ n).

## De-flake: best-of-N timing

Each tier is the minimum of N repeats (least perturbed by upward-only jitter).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from brutex.engine.resilience_streaks import fold_resilience, losing_run_length_hist

from tests.bench._o1_tiers import ratio_ceiling

_IST = timezone(timedelta(hours=5, minutes=30))
_N_SMALL: int = 1_000
_N_BIG: int = 100_000
_REPEATS: int = 5
_START = 1_000_000


@dataclass(frozen=True, slots=True)
class _FakeTrade:
    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _trades(n: int) -> list[_FakeTrade]:
    """A deterministic ``n``-trade stream with a fixed loss cadence."""
    base = datetime(2020, 1, 1, 10, 0, tzinfo=_IST)
    out: list[_FakeTrade] = []
    for i in range(n):
        # A repeating win/loss pattern → bounded, constant run-length physics.
        pnl = -100 if i % 3 == 0 else 200
        entry = (base + timedelta(minutes=5 * i)).astimezone(UTC)
        exit_ = (base + timedelta(minutes=5 * i + 1)).astimezone(UTC)
        out.append(_FakeTrade(pnl_paisa=pnl, entry_ts=entry, exit_ts=exit_))
    return out


def _per_trade_seconds(n: int) -> float:
    trades = _trades(n)
    best = math.inf
    for _ in range(_REPEATS):
        start = time.perf_counter()
        fold_resilience("r", trades, parity_ok=True, start_equity_paisa=_START, fingerprint="f")
        best = min(best, time.perf_counter() - start)
    return best / n


def test_fold_is_flat_per_trade() -> None:
    small = _per_trade_seconds(_N_SMALL)
    big = _per_trade_seconds(_N_BIG)
    ceiling = ratio_ceiling(shared_ci_default=5.0)
    assert big <= small * ceiling, (
        f"per-trade fold cost grew {big / small:.2f}× (ceiling {ceiling}×)"
    )


def test_streak_histogram_key_count_is_sqrt_bounded() -> None:
    # Adversarial: the maximal-distinct-run-length input 1,2,3,… losing runs.
    pnls: list[int] = []
    length = 1
    while len(pnls) + length + 1 <= _N_BIG:
        pnls.extend([-1] * length)  # a losing run of this length
        pnls.append(1)  # a win breaks it
        length += 1
    hist = losing_run_length_hist(pnls)
    assert len(hist) <= math.isqrt(2 * len(pnls)) + 1
