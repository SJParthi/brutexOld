"""Stage-2.5 residual-B2 PROOF — the Sharpe-only kill drops non-Sharpe rankers.

DEC-SPRT-B2-RESIDUAL-001 — the adversarial counterpart to the A/B oracle
(DEC-SPRT-AB-ORACLE-001). The oracle showed that, on a *benign* pool (clear
winners + duds), a ``top_k``-tracked early-stop preserves the leaderboard. This
test exercises the hostile review's residual-B2 worst case (ADR-0006 B2) and
proves it is **REAL, not theoretical**:

A combo can be **low-Sharpe** (so the Sharpe-based early-stop KILLS it) yet still
rank in the full-sweep **top-K** via the 8-term composite's *non-Sharpe* terms
(Risk-Efficiency = MFE/MAE, Calmar, Profit-Factor — `ranking/__init__.py:301-318`).
When such "adversarial" combos are in the pool, even the ``top_k``-tracked
early-stop **drops gate-passing top-K combos → the ranked leaderboard CHANGES**.

Conclusion (the honest, load-bearing reason B4-wiring + B5 stay BLOCKED):
**early-stop CANNOT be wired with a Sharpe-only kill criterion.** The kill must be
made *composite-aware* (or combos ranked over the full pool before pruning) — see
ADR-0006 B2. This test is a guardrail: it asserts the unsafe behavior exists, so
any future attempt to wire a Sharpe-only kill is reminded why it must not.

The module stays INERT — this drives the inert `sprt_decision` against the real
`rank_combos`; nothing is wired into the sweep.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from brutex.engine.early_stop import Decision, RunningSharpe, SprtConfig, sprt_decision
from brutex.metrics import ClosedTrade, EquityCurve, ResultMetrics, compute_metrics
from brutex.ranking import rank_combos
from brutex.strategies.types import Side

_T0 = datetime(2025, 1, 1, 9, 15, tzinfo=UTC)
_START = 1_000_000


def _metrics(returns: list[float], *, mfe_mult: float, mae_mult: float) -> ResultMetrics:
    """A combo's ResultMetrics, with tunable MFE/MAE so we can build a combo that
    is low-Sharpe but high Risk-Efficiency (the adversarial shape)."""
    trades: list[ClosedTrade] = []
    equity = [_START]
    running = _START
    for i, r in enumerate(returns):
        pnl = round(r * _START)
        trades.append(
            ClosedTrade(
                pnl_paisa=pnl,
                mfe_paisa=int(abs(pnl) * mfe_mult) + 500,
                mae_paisa=int(abs(pnl) * mae_mult) + 200,
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


def _killed(returns: list[float], p95: float, cfg: SprtConfig) -> bool:
    stat = RunningSharpe()
    for r in returns:
        stat.update(r)
        if sprt_decision(stat, p95_threshold=p95, cfg=cfg) is Decision.KILL:
            return True
    return False


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def test_sharpe_only_kill_drops_non_sharpe_top_k_rankers() -> None:
    """The residual-B2 worst case is real: at least one full-sweep top-10 combo is
    a LOW-Sharpe adversarial ranker that the Sharpe-based early-stop KILLS — so the
    leaderboard changes. Therefore a Sharpe-only kill is unsafe to wire."""
    rng = random.Random(0)
    # kind, returns, metrics. 5 winners, 10 moderates, 15 duds, 10 ADVERSARIAL
    # (low mean → low Sharpe, but MFE≫MAE → high Risk-Efficiency).
    pool: list[tuple[str, list[float], ResultMetrics]] = []
    for _ in range(5):
        r = [rng.gauss(0.040, 0.020) for _ in range(60)]
        pool.append(("WIN", r, _metrics(r, mfe_mult=1.0, mae_mult=1.0)))
    for _ in range(10):
        r = [rng.gauss(0.012, 0.020) for _ in range(60)]
        pool.append(("MOD", r, _metrics(r, mfe_mult=1.0, mae_mult=1.0)))
    for _ in range(15):
        r = [rng.gauss(-0.015, 0.025) for _ in range(60)]
        pool.append(("DUD", r, _metrics(r, mfe_mult=1.0, mae_mult=1.0)))
    for _ in range(10):
        r = [rng.gauss(0.002, 0.030) for _ in range(60)]
        pool.append(("ADV", r, _metrics(r, mfe_mult=6.0, mae_mult=0.4)))

    candidates = [(mask, m) for mask, (_t, _r, m) in enumerate(pool)]
    baseline_top10 = [rc.mask for rc in rank_combos(candidates)[:10]]

    sharpes = [(m.sharpe_ratio or 0.0) for _t, _r, m in pool]
    safe_bar = _percentile(sharpes, 1.0 - 10.0 / len(pool))  # the top_k-tracked bar

    # Which full-sweep top-10 combos does the Sharpe-based early-stop kill?
    killed_top10 = [
        mask for mask in baseline_top10 if _killed(pool[mask][1], safe_bar, cfg=SprtConfig())
    ]

    # The proof: the leaderboard IS corrupted — ≥1 top-10 combo is killed ...
    assert killed_top10, (
        "expected the Sharpe-only kill to drop a non-Sharpe top-10 ranker; if this "
        "is empty the adversarial fixture no longer exercises the residual-B2 risk"
    )
    # ... and every such killed top-10 combo is a LOW-Sharpe (below-bar) ADVERSARIAL
    # combo that ranked only via the composite's non-Sharpe terms — confirming the
    # exact mechanism the hostile review predicted.
    for mask in killed_top10:
        kind, _r, metrics = pool[mask]
        assert kind == "ADV", f"killed top-10 mask {mask} was a {kind}, expected ADV"
        assert (metrics.sharpe_ratio or 0.0) < safe_bar  # genuinely below the kill bar
