"""Day 1 (Phase 0.5) sprint orchestrator.

End-to-end runner that wires:
- Groww adapter (fetch 5y data)
- Calendar (NSE 2021-2026)
- Resampler (1m -> 21 TFs)
- Indicators (52 conditions)
- Backtest engine (Polars vectorized)
- Metrics (30+ per result)
- Ranking (composite + rejection gates)
- Visualization (PNG charts)

Produces:
- /Volumes/QuantData/results/day1/top10_strategies.json
- /Volumes/QuantData/results/day1/charts/*.png

Target: ~12.6M trials in 16-20h on M4 Pro (Python+Polars baseline).
"""
