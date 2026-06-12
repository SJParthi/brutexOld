"""Groww Trade API adapter (Track-1 backtesting data source).

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009) — the Dhan → Groww re-migration.
This package is built additive-first: PR-C ships the pure leaf layer
(``constants`` · ``errors`` · ``parser`` · ``chunks``); the client, auth,
instruments, ingest, and the :class:`~brutex.adapters.protocol.MarketAdapter`
implementation land in later PRs (ADR-0009 §5). Dhan stays the live vendor
until the cutover (PR-G/H); nothing here is wired into the orchestrator yet.

Every Groww fact is traceable to ``docs/groww/api-reference/*.md`` or the
operator-verified ``docs/groww/probe/INSTRUMENT_MASTER_FINDINGS.md``
(Golden Rule #1). Live-only facts (rate-limit ceiling) are flagged
``*_UNVERIFIED`` in :mod:`brutex.adapters.groww.constants`.
"""

from __future__ import annotations

from brutex.adapters.groww.chunks import (
    DateWindow,
    plan_candle_windows,
    window_days_for_interval,
)
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwBadRequestError,
    GrowwError,
    GrowwFeedError,
    GrowwNotFoundError,
    GrowwRateLimitError,
    GrowwTimeoutError,
    from_groww_exception,
    is_auth_failure,
    is_rate_limit,
    is_retryable,
)
from brutex.adapters.groww.parser import parse_candle_rows, parse_timestamp_ist

__all__ = [
    "DateWindow",
    "GrowwAuthError",
    "GrowwBadRequestError",
    "GrowwError",
    "GrowwFeedError",
    "GrowwNotFoundError",
    "GrowwRateLimitError",
    "GrowwTimeoutError",
    "from_groww_exception",
    "is_auth_failure",
    "is_rate_limit",
    "is_retryable",
    "parse_candle_rows",
    "parse_timestamp_ist",
    "plan_candle_windows",
    "window_days_for_interval",
]
