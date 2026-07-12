"""Unit tests for :mod:`brutex.feeds.registry` (DEC-FEED-INTERFACE-001, #1752).

100% line + branch. The registry ships ONLY the default feed (PR-2 is
vendor-token-free); an unknown id raises a typed refusal naming the registered
ids.
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


def test_registry_ships_only_default_feed() -> None:
    assert registered_feed_ids() == (DEFAULT_FEED,)


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
