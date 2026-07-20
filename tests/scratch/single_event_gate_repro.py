"""C2 MEASUREMENT repro — single-event sensitivity gate (REBUILT, not the original).

HONEST: the original verification scripts are NOT on this box. This is a
REBUILD from the recorded description: a 30-trade strategy whose P&L is
dominated by ONE +100,000-paisa trade (extreme-event windfall); the other 29
trades are small. Reference figure to match: cpcv_pass_rate ~= 0.89 at the
default strict->0 criterion. Shapes are frozen with a fixed seed.

Uses the REAL production functions (never a reimplementation):
  - brutex.metrics.cpcv.cpcv_pass_rate  (candidate A: min_sharpe floor)
  - brutex.engine.session_lens.ist_trade_date  (candidate B day grouping law,
    the worst5_days law of combo_aggregates._worst_days_json)

Run:  $V/bin/python c2-repro.py   (from the bruteX-segate worktree root)
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from brutex.engine.session_lens import ist_trade_date
from brutex.metrics.cpcv import cpcv_pass_rate

START_EQUITY_PAISA = 10_000_000  # Rs 1,00,000
BARS_PER_DAY = 75  # 5-minute tf session bars
SEED = 20260719

# One (day, pnl_paisa) trade list per strategy; one trade exits per listed day.
# Days are consecutive NSE-ish weekdays starting 2024-01-01 (weekends skipped).


def trading_days(n: int) -> list[datetime]:
    days: list[datetime] = []
    d = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)  # 15:30 IST-ish exit instant
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def fluke_trades() -> list[tuple[datetime, int]]:
    """30 trades / 30 days; ONE +100,000-paisa windfall; 29 small scratches.

    Small trades: alternating wins/losses with a slight positive tilt
    (win +140 / loss -100, 15 wins 14 losses) - the realistic shape that
    sneaks past PF/win-rate hard gates on the windfall's back.
    """
    rng = random.Random(SEED)
    days = trading_days(30)
    pnls: list[int] = []
    for i in range(29):
        if i % 2 == 0:
            pnls.append(rng.randint(90, 170))  # small win
        else:
            pnls.append(-rng.randint(90, 170))  # small loss (symmetric band —
            # near-zero drift so non-windfall folds are coin-flips; tuned to
            # land the ~0.89 reference baseline, not to flatter either gate)
    pnls.insert(14, 100_000)  # the extreme-event windfall mid-series
    return list(zip(days, pnls, strict=True))


def steady_winner_trades() -> list[tuple[datetime, int]]:
    """LEGIT (a): 120 trades over 120 days, 60% win, similar magnitudes."""
    rng = random.Random(SEED + 1)
    days = trading_days(120)
    out = []
    for d in days:
        win = rng.random() < 0.60
        pnl = rng.randint(300, 900) if win else -rng.randint(250, 700)
        out.append((d, pnl))
    return out


def modest_edge_trades() -> list[tuple[datetime, int]]:
    """LEGIT (b): 240 trades over 120 days (2/day), 52% win, positive expectancy
    via a mild win/loss asymmetry (avg win 600 vs avg loss 480)."""
    rng = random.Random(SEED + 2)
    days = trading_days(120)
    out = []
    for d in days:
        for k in range(2):
            win = rng.random() < 0.52
            pnl = rng.randint(400, 800) if win else -rng.randint(320, 640)
            out.append((d + timedelta(minutes=30 * k), pnl))
    return out


def lumpy_legit_trades() -> list[tuple[datetime, int]]:
    """LEGIT (c): a lumpier-but-real edge — 60 trades / 60 days, 55% win,
    with a fat-but-not-dominant best day (~15% of total)."""
    rng = random.Random(SEED + 3)
    days = trading_days(60)
    out = []
    for d in days:
        win = rng.random() < 0.55
        pnl = rng.randint(500, 1500) if win else -rng.randint(400, 1100)
        out.append((d, pnl))
    # one good-but-honest day: 3x a normal win, not 300x
    out[20] = (out[20][0], 4_500)
    return out


def per_bar_returns(trades: list[tuple[datetime, int]]) -> list[float]:
    """The cascade CandidateInput ``returns`` shape: per-bar fractional equity
    returns over the whole window; zero on no-exit bars, pnl/equity on the
    trade's exit bar (compounding equity, mirroring an equity-curve fold)."""
    days = sorted({ist_trade_date(ts) for ts, _ in trades})
    day_index = {d: i for i, d in enumerate(days)}
    pnl_by_bar: dict[int, int] = {}
    per_day_count: dict[int, int] = {}
    for ts, pnl in trades:
        di = day_index[ist_trade_date(ts)]
        slot = per_day_count.get(di, 0)
        per_day_count[di] = slot + 1
        # place exits mid-session, spaced within the day
        pnl_by_bar[di * BARS_PER_DAY + 30 + slot * 7] = (
            pnl_by_bar.get(di * BARS_PER_DAY + 30 + slot * 7, 0) + pnl
        )
    n_bars = len(days) * BARS_PER_DAY
    equity = float(START_EQUITY_PAISA)
    rets: list[float] = []
    for b in range(n_bars):
        pnl = pnl_by_bar.get(b, 0)
        rets.append(pnl / equity)
        equity += pnl
    return rets


def day_pnls(trades: list[tuple[datetime, int]]) -> dict[str, int]:
    """Candidate B grouping — the worst5_days law: fold pnl by IST exit date."""
    acc: dict[str, int] = {}
    for ts, pnl in trades:
        key = ist_trade_date(ts).isoformat()
        acc[key] = acc.get(key, 0) + pnl
    return acc


STRATEGIES = {
    "fluke": fluke_trades(),
    "steady_60pct": steady_winner_trades(),
    "modest_52pct": modest_edge_trades(),
    "lumpy_legit": lumpy_legit_trades(),
}

FLOORS = [0.0, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1]
PASS_BAR = 0.5  # CascadeConfig.cpcv_min_pass_rate default


def main() -> None:
    print("=== Candidate A: cpcv_pass_rate vs min_sharpe floor (defaults 16/8) ===")
    header = "strategy        " + "".join(f"  ms={f:<6}" for f in FLOORS)
    print(header)
    a_rates: dict[str, list[float]] = {}
    for name, trades in STRATEGIES.items():
        rets = per_bar_returns(trades)
        rates = [cpcv_pass_rate(rets, min_sharpe=f) for f in FLOORS]
        a_rates[name] = rates
        cells = "".join(f"  {r:<8.4f}" for r in rates)
        print(f"{name:<16}{cells}")
    print(f"pass bar = {PASS_BAR} (CascadeConfig.cpcv_min_pass_rate default)")

    print()
    print("=== Candidate B: total minus best single IST exit day ===")
    print(
        f"{'strategy':<16}{'total':>10}{'best_day':>10}{'t-minus-b':>11}"
        f"{'remain%':>9}  strict(<=0 FAIL)"
    )
    for name, trades in STRATEGIES.items():
        dp = day_pnls(trades)
        total = sum(dp.values())
        best = max(dp.values())
        rem = total - best
        rem_pct = (rem / total * 100.0) if total > 0 else float("nan")
        verdict = "FAIL" if rem <= 0 else "pass"
        print(f"{name:<16}{total:>10}{best:>10}{rem:>11}{rem_pct:>8.1f}%  {verdict}")

    print()
    print("=== Candidate B threshold variant: FAIL iff remaining < X% of total ===")
    xs = [0.0, 1.0, 5.0, 10.0, 20.0, 30.0, 50.0]
    print("strategy        " + "".join(f"  X={x:<5}" for x in xs))
    for name, trades in STRATEGIES.items():
        dp = day_pnls(trades)
        total = sum(dp.values())
        best = max(dp.values())
        rem_pct = ((total - best) / total * 100.0) if total > 0 else float("nan")
        cells = "".join(f"  {'FAIL ' if rem_pct < x else 'pass ':<7}" for x in xs)
        print(f"{name:<16}{cells}  (remain={rem_pct:.1f}%)")


if __name__ == "__main__":
    main()
