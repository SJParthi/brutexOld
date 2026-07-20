"""``build_groww_binding`` — the default feed's thin, identity-pinned binding.

DEC-FEED-INTERFACE-001 (MIGRATION.md #1795), Decision 3. The default feed proves
:class:`~brutex.feeds.binding.FeedBinding` conformance WITHOUT retrofitting the
ingest spine: this shim assembles the binding from the EXACT live objects the
spine already imports — the window-tiling policy, the error triple, the
self-pacing predicates, the rate ceilings, the ledger-market namespaces, the
adapter class, and the verified history floor. Unit tests pin each field by
OBJECT IDENTITY (``binding.window_days_for is window_days_for_interval`` etc.), so
the shim structurally cannot drift from the real spine coupling without turning a
test red, and the ingest spine shows a byte-zero diff (the strongest identity
evidence). Spine consumption of the binding is the named follow-up
DEC-FEED-SPINE-GENERALIZE-001 (PR-3).

The history floor is sourced from the adapter-owned
:data:`~brutex.adapters.groww.constants.EARLIEST_HISTORY` (``2020-01-01`` — the
probe-verified 1-minute depth, DEC-PULL-FLOOR-2020-001; the same value the
dashboard's ``GROWW_HISTORY_FLOOR`` carries, sourced from the light constants
module to keep this registry-loaded shim a leaf). See TRACK1_BACKTEST_SPEC.md
§6 verified-facts table and ADR-0021.
"""

from __future__ import annotations

from functools import partial

from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.chunks import window_days_for_interval
from brutex.adapters.groww.client import (
    client_self_paces,
    client_self_reports_rate_limits,
    client_self_reports_successes,
)
from brutex.adapters.groww.constants import (
    EARLIEST_HISTORY,
    GROWW_DAILY_QUOTA_UNVERIFIED,
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
    GROWW_RATE_LIMIT_PER_MIN,
    GROWW_RATE_LIMIT_RPS_CEILING,
)
from brutex.adapters.groww.errors import (
    GrowwError,
    is_rate_limit,
    is_retryable,
)
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.data.feed_root import DEFAULT_FEED, feed_lake_root
from brutex.feeds.binding import FeedBinding, FeedErrorPolicy, FeedPacing


def build_groww_binding() -> FeedBinding:
    """Assemble the default feed's :class:`FeedBinding` from the live spine objects.

    Every callable / class / constant is the SAME object the ingest spine uses;
    the tests pin that by identity. ``lake_root_for`` is
    :func:`~brutex.data.feed_root.feed_lake_root` bound to the default feed, so it
    resolves to the top lake root by identity (the Groww byte-identity law).
    ``daily_quota`` is honestly ``None`` (the backtesting quota is UNVERIFIED per
    §6; Golden Rule #1).
    """
    return FeedBinding(
        feed_id=DEFAULT_FEED,
        ledger_market=GROWW_COVERAGE_MARKET,
        ledger_market_future=GROWW_FUTURE_COVERAGE_MARKET,
        ledger_market_option=GROWW_OPTION_COVERAGE_MARKET,
        adapter_factory=GrowwMarketAdapter,
        window_days_for=window_days_for_interval,
        error_policy=FeedErrorPolicy(
            is_retryable=is_retryable,
            is_rate_limit=is_rate_limit,
            terminal_base=GrowwError,
        ),
        pacing=FeedPacing(
            ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
            ceiling_per_min=GROWW_RATE_LIMIT_PER_MIN,
            daily_quota=GROWW_DAILY_QUOTA_UNVERIFIED,
        ),
        client_self_paces=client_self_paces,
        client_self_reports_rate_limits=client_self_reports_rate_limits,
        client_self_reports_successes=client_self_reports_successes,
        history_floor=EARLIEST_HISTORY,
        lake_root_for=partial(feed_lake_root, feed=DEFAULT_FEED),
    )


__all__ = ["build_groww_binding"]
