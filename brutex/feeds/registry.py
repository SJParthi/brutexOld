"""Feed registry — resolve a feed id to its :class:`FeedBinding`.

DEC-FEED-INTERFACE-001 (MIGRATION.md #1752). The registry maps a grammar-valid
feed id to the builder that assembles its :class:`~brutex.feeds.binding.FeedBinding`.
PR-2 is **vendor-token-free** (Golden Rule / the vendor-token CI guard): it
ships with ONLY the default feed. A second feed id is registered in a later PR by
a one-line data change closing over that PR's authenticated client (the handshake
recorded in ADR-0021 / DEC-FEED-INTERFACE-001); it is deliberately absent here so
PR-2 merges independently.

Resolution of an unknown feed id raises a typed :class:`UnknownFeedError` naming
the registered ids (never a bare ``KeyError``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from brutex.data.feed_root import DEFAULT_FEED
from brutex.feeds.binding import FeedBinding
from brutex.feeds.groww_binding import build_groww_binding
from brutex.models import BruteXError


class UnknownFeedError(BruteXError):
    """Raised when :func:`resolve_feed_binding` is asked for an unregistered feed."""


#: The registry: feed id → zero-arg builder of its :class:`FeedBinding`.
#: PR-2 ships ONLY the default feed (vendor-token-free).
_BUILDERS: Final[dict[str, Callable[[], FeedBinding]]] = {
    DEFAULT_FEED: build_groww_binding,
}


def registered_feed_ids() -> tuple[str, ...]:
    """Return the registered feed ids, sorted."""
    return tuple(sorted(_BUILDERS))


def resolve_feed_binding(feed_id: str) -> FeedBinding:
    """Resolve *feed_id* to its :class:`FeedBinding`.

    Raises
    ------
    UnknownFeedError
        If *feed_id* is not registered — the message names the registered ids.
    """
    try:
        builder = _BUILDERS[feed_id]
    except KeyError:
        raise UnknownFeedError(
            f"unknown feed_id {feed_id!r}; registered feeds: {list(registered_feed_ids())}"
        ) from None
    return builder()


__all__ = [
    "UnknownFeedError",
    "registered_feed_ids",
    "resolve_feed_binding",
]
