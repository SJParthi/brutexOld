"""Stage-2.5 A/B oracle — does early-stop preserve the ranked leaderboard?

DEC-SPRT-AB-ORACLE-001 — the empirical guard for ADR-0006 blocker B2 (and the
demonstration of B4). It drives the **inert** ``brutex.engine.early_stop``
decision over a deterministic pool of synthetic combos, ranks them with the
**real** 8-term ``brutex.ranking.rank_combos``, and compares the top-K of a full
sweep vs a sweep where the early-stop-killed combos are dropped.

The headline finding it pins down (honest, no-illusion — Golden Rule #10):

* with the **literal spec threshold** (the pool's 95th-percentile Sharpe), early
  stop is **UNSAFE** — it kills combos that sit in the full-sweep top-K, so the
  leaderboard *changes*. This is exactly blocker B4: a fixed 95 is wrong.
* with a **``top_k``-tracked threshold** (the ``(1 − top_k/N)``-percentile Sharpe),
  early stop kills only clear duds and the **top-K is preserved** (top-3 recall
  100%, top-10 recall ≥ 95%).

So this test is BOTH the B2 empirical guard AND the proof that the threshold must
track ``top_k`` (B4). It does NOT wire early-stop into the sweep — the module
stays inert; wiring remains blocked until B4 lands.

Honest envelope: this is a spot-check on one deterministic fixture, NOT a proof
over the 33B-trial space (the hostile review's caveat). A worst-case combo that
is low-Sharpe but high on a non-Sharpe composite term is the residual B2 risk and
is out of this fixture's scope.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from brutex.engine.early_stop import Decision, RunningSharpe, SprtConfig, sprt_decision
from brutex.metrics import ClosedTrade, EquityCurve, ResultMetrics, compute_metrics
from brutex.ranking import rank_combos
from brutex.strategies.types import Side

_T0 = datetime(2025, 1, 1, 9, 15, tzinfo=UTC)
_START = 1_000_000  # paisa notional base


def _combo_metrics(returns: list[float]) -> ResultMetrics:
    """Turn a per-trade return stream into a full ResultMetrics (the ranking input)."""
    trades: list[ClosedTrade] = []
    equity = [_START]
    running = _START
    for i, r in enumerate(returns):
        pnl = round(r * _START)
        swing = abs(pnl) + 1
        trades.append(
            ClosedTrade(
                pnl_paisa=pnl,
                mfe_paisa=max(pnl, 0) + swing,
                mae_paisa=max(-pnl, 0) + swing,
                holding_bars=1,
                entry_ts=_T0 + timedelta(minutes=i),
                exit_ts=_T0 + timedelta(minutes=i + 1),
                direction=Side.BUY,
            )
        )
        running += pnl
        equity.append(running)
    curve = EquityCurve(equity_paisa=tuple(equity), start_equity_paisa=_START, span_years=1.0)
    return compute_metrics(trades, curve, total_bars=len(returns))


def _build_pool() -> list[tuple[int, list[float], ResultMetrics]]:
    """Deterministic 30-combo pool: 3 strong winners, 7 moderates, 20 duds."""
    rng = random.Random(20260608)
    specs: list[tuple[float, float]] = (
        [(0.040, 0.020)] * 3  # strong winners  → high Sharpe
        + [(0.012, 0.020)] * 7  # moderates       → positive Sharpe
        + [(-0.015, 0.025)] * 20  # duds          → negative Sharpe
    )
    pool: list[tuple[int, list[float], ResultMetrics]] = []
    for mask, (mean, sigma) in enumerate(specs):
        returns = [rng.gauss(mean, sigma) for _ in range(60)]
        pool.append((mask, returns, _combo_metrics(returns)))
    return pool


def _killed(returns: list[float], p95: float, cfg: SprtConfig) -> bool:
    """True if early-stop would KILL this combo at any point in its stream."""
    stat = RunningSharpe()
    for r in returns:
        stat.update(r)
        if sprt_decision(stat, p95_threshold=p95, cfg=cfg) is Decision.KILL:
            return True
    return False


def _topk_masks(candidates: list[tuple[int, ResultMetrics]], k: int) -> list[int]:
    return [rc.mask for rc in rank_combos(candidates)[:k]]


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def test_naive_95th_percentile_threshold_is_unsafe() -> None:
    """B4 demonstration: a fixed 95th-percentile kill bar drops top-K combos →
    the leaderboard CHANGES. (This is why the threshold must track top_k.)"""
    pool = _build_pool()
    cfg = SprtConfig()
    candidates = [(mask, m) for mask, _r, m in pool]
    baseline_top10 = set(_topk_masks(candidates, 10))

    sharpes = [m.sharpe_ratio or 0.0 for _mask, _r, m in pool]
    naive_p95 = _percentile(sharpes, 0.95)  # the literal spec bar

    survivors = [(mask, m) for mask, r, m in pool if not _killed(r, naive_p95, cfg)]
    es_top10 = set(_topk_masks(survivors, 10))

    # The naive bar kills at least one full-sweep top-10 combo → recall < 100%.
    assert not baseline_top10.issubset(es_top10), (
        "expected the naive 95th-pct threshold to break the top-10 (it should "
        "kill in-the-money combos) — if this passes, the fixture no longer "
        "exercises the B4 hazard"
    )


def test_topk_tracked_threshold_preserves_the_leaderboard() -> None:
    """B2 guard: with a top_k-tracked kill bar, early-stop kills only clear duds
    and the ranked top-K is preserved (top-3 recall 100%, top-10 recall ≥ 95%)."""
    pool = _build_pool()
    cfg = SprtConfig()
    candidates = [(mask, m) for mask, _r, m in pool]
    baseline = _topk_masks(candidates, 10)
    baseline_top3, baseline_top10 = set(baseline[:3]), set(baseline)

    sharpes = [m.sharpe_ratio or 0.0 for _mask, _r, m in pool]
    # The (1 − top_k/N)-percentile bar — combos that can't plausibly reach the
    # top-K Sharpe are killed; the top-K themselves sit above the bar (B4).
    safe_bar = _percentile(sharpes, 1.0 - 10.0 / len(pool))

    survivors = [(mask, m) for mask, r, m in pool if not _killed(r, safe_bar, cfg)]
    killed = len(pool) - len(survivors)
    es_top10 = _topk_masks(survivors, 10)
    es_top3, es_top10_set = set(es_top10[:3]), set(es_top10)

    # Early-stop must actually prune (else the test proves nothing) ...
    assert killed > 0, "expected the safe bar to prune the clear duds"
    # ... but never a top-3 combo, and ≥95% of the top-10 survive in place.
    assert baseline_top3 == es_top3, "top-3 leaderboard changed under early-stop"
    recall10 = len(baseline_top10 & es_top10_set) / len(baseline_top10)
    assert recall10 >= 0.95, f"top-10 recall {recall10:.2f} < 0.95 under early-stop"
