"""Backtest engine — Polars-vectorized per-bar simulator.

Day 1 deliverable (Phase 0.5):
- Pure Python + Polars vectorized
- 12-worker concurrency model
- Per-worker working set <= 500 MB
- Mode A (SL/TP grid) + Mode B (signal-driven exit)
- Forced 15:20 IST exit
- Pessimistic fills (worst price within entry bar)
- 1-tick slippage (5 paisa) on entry + exit

Track 1 v2 spec sections:
- Strategy Specification
- 5-Stage Funnel / Stage 2
- Non-Negotiable Correctness Requirements
"""
