"""BruteX — extreme drill-down brute-force backtesting engine for Indian spot indexes.

This is the top-level package. See ``docs/TRACK1_BACKTEST_SPEC.md`` for the
authoritative Track 1 specification. Scope-locked to 4 Indian spot indexes:
NIFTY 50, BANKNIFTY, SENSEX, and INDIA VIX (reference only), fetched via
the Groww Trade API (post-DEC-VENDOR-PIVOT-001, 2026-05-26). See CLAUDE.md §3.
"""

from __future__ import annotations

__version__ = "0.2.0a0"

__all__ = ["__version__"]
