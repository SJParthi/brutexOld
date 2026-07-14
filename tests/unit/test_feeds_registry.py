"""Unit tests for :mod:`brutex.feeds.registry` (DEC-FEED-INTERFACE-001, #1792).

100% line + branch. The registry ships the default feed plus the ``dhan`` feed
(DEC-DHAN-FEED-BINDING-001 — the real second consumer); an unknown id raises a
typed refusal naming the registered ids.
"""

from __future__ import annotations

import pytest
from brutex.data.feed_root import DEFAULT_FEED
from brutex.feeds.binding import FeedBinding
from brutex.feeds.registry import (
    UnknownFeedError,
    registered_feed_ids,
    resolve_feed_binding,
)


def test_registry_ships_default_and_dhan_feeds() -> None:
    # Sorted: the default feed (groww) + the dhan feed (the real second consumer).
    assert registered_feed_ids() == ("dhan", DEFAULT_FEED)


def test_resolve_default_returns_binding() -> None:
    binding = resolve_feed_binding(DEFAULT_FEED)
    assert isinstance(binding, FeedBinding)
    assert binding.feed_id == DEFAULT_FEED


def test_resolve_unknown_raises_naming_registered() -> None:
    with pytest.raises(UnknownFeedError) as exc:
        resolve_feed_binding("feedx")
    msg = str(exc.value)
    assert "feedx" in msg
    assert DEFAULT_FEED in msg
