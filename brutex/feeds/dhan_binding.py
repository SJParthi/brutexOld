"""``build_dhan_binding`` — the Dhan feed's :class:`FeedBinding`.

DEC-DHAN-FEED-BINDING-001 (ADR-0022). Dhan is the REAL SECOND CONSUMER of the
COMMON feed interface (DEC-FEED-INTERFACE-001): this shim assembles the
:class:`~brutex.feeds.binding.FeedBinding` from the Dhan leaf objects — the flat
90-day window tiler, the error-classifier triple, the ``lambda _c: False``
self-pacing predicates (the Dhan client does NOT self-pace; the spot spine owns
per-window admission), the 5 r/s + 100k/day pacing, the ``dhan`` ledger-market
namespaces (the prefix law → no Groww collision), the adapter class, and the honest
``history_floor`` (``None`` — UNVERIFIED until a probe transcript flips it, Golden
Rule #1). ``lake_root_for`` is :func:`~brutex.data.feed_root.feed_lake_root` bound
to ``dhan``, so the sink nests under ``<lake_root>/feeds/dhan/`` (path-structural
attribution → Groww bytes untouched).

Unit tests pin each field by OBJECT IDENTITY, so this shim cannot drift from the
Dhan leaf layer without turning a test red. See ADR-0022.
"""

from __future__ import annotations

from functools import partial

from brutex.adapters.dhan.adapter import DhanMarketAdapter
from brutex.adapters.dhan.chunks import window_days_for_interval
from brutex.adapters.dhan.client import (
    client_self_paces,
    client_self_reports_rate_limits,
    client_self_reports_successes,
)
from brutex.adapters.dhan.constants import (
    DHAN_COVERAGE_MARKET,
    DHAN_DAILY_QUOTA,
    DHAN_FEED_ID,
    DHAN_HISTORY_FLOOR,
    DHAN_RATE_LIMIT_PER_MIN,
    DHAN_RATE_LIMIT_RPS,
)
from brutex.adapters.dhan.errors import (
    DhanError,
    is_rate_limit,
    is_retryable,
)
from brutex.data.feed_root import feed_lake_root
from brutex.feeds.binding import FeedBinding, FeedErrorPolicy, FeedPacing


def build_dhan_binding() -> FeedBinding:
    """Assemble the Dhan feed's :class:`FeedBinding` from the Dhan leaf objects.

    Every callable / class / constant is the SAME object the Dhan adapter/spine use;
    the tests pin that by identity. ``ledger_market_future`` / ``ledger_market_option``
    follow the prefix law (``dhan-future`` / ``dhan-option``) even though the SPOT
    track pulls neither — the binding invariant requires them. ``history_floor`` is
    honestly ``None`` (UNVERIFIED, Golden Rule #1).
    """
    return FeedBinding(
        feed_id=DHAN_FEED_ID,
        ledger_market=DHAN_COVERAGE_MARKET,
        ledger_market_future=f"{DHAN_COVERAGE_MARKET}-future",
        ledger_market_option=f"{DHAN_COVERAGE_MARKET}-option",
        adapter_factory=DhanMarketAdapter,
        window_days_for=window_days_for_interval,
        error_policy=FeedErrorPolicy(
            is_retryable=is_retryable,
            is_rate_limit=is_rate_limit,
            terminal_base=DhanError,
        ),
        pacing=FeedPacing(
            ceiling_rps=DHAN_RATE_LIMIT_RPS,
            ceiling_per_min=DHAN_RATE_LIMIT_PER_MIN,
            daily_quota=DHAN_DAILY_QUOTA,
        ),
        client_self_paces=client_self_paces,
        client_self_reports_rate_limits=client_self_reports_rate_limits,
        client_self_reports_successes=client_self_reports_successes,
        history_floor=DHAN_HISTORY_FLOOR,
        lake_root_for=partial(feed_lake_root, feed=DHAN_FEED_ID),
    )


__all__ = ["build_dhan_binding"]
