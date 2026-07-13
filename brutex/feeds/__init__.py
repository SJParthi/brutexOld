"""The ``brutex.feeds`` package — the COMMON historical-feed interface.

DEC-FEED-INTERFACE-001 (MIGRATION.md #1779). A historical feed is defined by
composition over the existing :class:`~brutex.adapters.protocol.MarketAdapter`
fetch waist (:class:`~brutex.feeds.binding.FeedBinding`), resolved from a registry
(:func:`~brutex.feeds.registry.resolve_feed_binding`) and written through the ONE
sanctioned sink constructor (:func:`~brutex.feeds.binding.build_feed_sink`), which
nests a non-default feed's tree under ``<lake_root>/feeds/<feed_id>/``
(DEC-FEED-LAKE-ROOT-001) with a hash-chained audit
(:class:`~brutex.data.feed_audit.FeedAuditSink`). PR-2 ships the default feed
only; the package carries no vendor token.
"""

from __future__ import annotations

from brutex.feeds.binding import (
    FeedBinding,
    FeedErrorPolicy,
    FeedPacing,
    build_feed_sink,
)
from brutex.feeds.registry import (
    UnknownFeedError,
    registered_feed_ids,
    resolve_feed_binding,
)

__all__ = [
    "FeedBinding",
    "FeedErrorPolicy",
    "FeedPacing",
    "UnknownFeedError",
    "build_feed_sink",
    "registered_feed_ids",
    "resolve_feed_binding",
]
