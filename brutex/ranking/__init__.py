"""Composite ranking + hard rejection gates per Track 1 spec.

Hard rejection gates (applied BEFORE composite scoring):
- total_trades < 30 → REJECT (statistical noise)
- profit_factor < 1.2 → REJECT (no edge)
- max_drawdown_pct > 30% → REJECT (uninvestable)
- win_rate < 25% AND win_loss_ratio < 2.0 → REJECT
- max_consecutive_losses > 10 → REJECT (account-blower)
- lockbox_sharpe / training_sharpe < 0.5 → REJECT (overfit)

Composite ranking score (after gates pass):
    0.25 × Sharpe + 0.20 × Sortino + 0.15 × Calmar
  + 0.15 × (Profit_Factor - 1.0) + 0.10 × Recovery_Factor
  + 0.10 × (Win_Rate × Win_Loss_Ratio) + 0.05 × Lockbox_Sharpe

Each term z-score normalized per (instrument, TF) leaderboard, clamped [-3, +3].
"""
