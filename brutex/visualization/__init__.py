"""Static PNG chart generation — matplotlib Agg backend, no GUI.

Day 1 deliverable: per top-10 strategy, generate cumulative PnL PNG
chart for operator dopamine.

Per Agent C observability research:
- matplotlib Agg backend (no GUI dependency)
- 200 DPI, seaborn-v0_8-darkgrid style
- Single figure, multi-axes per chart
- Saved to `/Volumes/QuantData/results/day{N}/charts/`
- Attached to Telegram end-of-sweep digest
"""
