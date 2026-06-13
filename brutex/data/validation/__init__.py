"""Cross-verification harness — vendor data vs. external official sources.

DEC-NSE-1DAY-VALIDATION-001. This package holds **read-side validators** that
cross-check the data BruteX pulls from its vendor (Groww) against an
INDEPENDENT official source, so a data mismatch is caught BEFORE a multi-year
pull is scaled.

Its first member is :mod:`brutex.data.validation.nse_oneday` — a one-day OHLC
cross-check of Groww's ``1day`` index candles against the NSE/BSE official 1-day
OHLC for the three brute-force indices (``NSE-NIFTY`` / ``NSE-BANKNIFTY`` /
``BSE-SENSEX``). The public API lives there; import it directly, e.g.::

    from brutex.data.validation.nse_oneday import validate_brute_force_indices

Design (mirrors :mod:`brutex.dashboard.nse_constituents`): a pure vs. live split
(every parse/map/compare step is pure + 100% unit-tested; the live fetchers are
the only ``# pragma: no cover`` surfaces), injected openers so tests fake both,
an env-overridable index→source mapping DOCUMENTED "verify against NSE/BSE; may
change", graceful degradation (never crash), and NO hardcoded OHLC (Golden
Rule #1 — every number in a verdict comes from the injected fetchers).
"""

from __future__ import annotations
