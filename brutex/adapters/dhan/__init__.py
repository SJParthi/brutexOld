"""Dhan adapter — Track 1 backtesting data source (DEC-GROWW-DHAN-REVERT-001).

The Dhan v2 REST adapter is the ONLY component that speaks Dhan's native
protocol; everything above L1 consumes normalised :mod:`brutex.models`
types and is vendor-agnostic (see :mod:`brutex.adapters.protocol`).

Verified vendor facts live in ``docs/dhan-ref/`` (the single source of
truth — Golden Rule #1/#2); this package's :mod:`~brutex.adapters.dhan.constants`
mirrors them in code. Built incrementally across installments of the
Groww→Dhan revert (MIGRATION.md #341):

* constants — endpoints, intervals, segments, rate limits, master URLs.
* parser — columnar parallel-array response → :class:`CandleBar`.
* chunks — 90-day spot / 30-day options window planner.
* auth — ``access-token`` (JWT) + ``dhanClientId`` via TOTP.
* client + ``DhanMarketAdapter`` — the protocol implementation.
"""

from __future__ import annotations

__all__: list[str] = []
