"""30+ trading metrics per result row.

Per Track 1 spec §Result Row Schema. Computed in 2 passes (one fused
Welford+Pébaÿ for moments+DD+counts, one nth_element for percentiles).

Metrics groups:
- Return (5): net_pnl, total_return, cagr, gross_profit, gross_loss
- Risk-adjusted (6): sharpe, sortino, calmar, mar, information, omega
- Drawdown (8): MDD pct + paisa, duration, recovery factor, ulcer,
  largest losing trade, max consec losses, max consec wins
- Trade quality (11): total/winning/losing, win rate, profit factor,
  avg win/loss, win/loss ratio, expectancy, MFE, MAE
- Timing (3): avg/max holding, time-in-market
- Distribution (4): tail ratio, skew, kurtosis, std dev
- Statistical (6 — Stage 4 only): DSR, PBO, CPCV, WFO, lockbox

Plus composite ranking score per §Composite ranking + hard rejection gates.
"""
