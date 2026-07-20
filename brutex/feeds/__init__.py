"""The ``brutex.feeds`` package — the COMMON historical-feed interface.

DEC-FEED-INTERFACE-001 (MIGRATION.md #1795). A historical feed is defined by
composition over the existing :class:`~brutex.adapters.protocol.MarketAdapter`
fetch waist (:class:`~brutex.feeds.binding.FeedBinding`), resolved from a registry
(:func:`~brutex.feeds.registry.resolve_feed_binding`) and written through the ONE
sanctioned sink constructor (:func:`~brutex.feeds.binding.build_feed_sink`), which
nests a non-default feed's tree under ``<lake_root>/feeds/<feed_id>/``
(DEC-FEED-LAKE-ROOT-001) with a hash-chained audit
(:class:`~brutex.data.feed_audit.FeedAuditSink`). PR-2 ships the default feed
only; the package carries no vendor token.

ALSO the COMMON home for the N-feed vendor-profile abstraction + registry
(DEC-FEED-REGISTRY-COMMON-001, MIGRATION.md #1903) — the ``vendor_profile`` /
``registry_build`` leaf modules stay import-light and are deliberately NOT
re-exported here (the profile layer never becomes an import hub over the
pull spines; consumers import those leaves by module path).
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
