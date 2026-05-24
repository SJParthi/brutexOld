"""Tests for :mod:`brutex.feed.errors` — exception hierarchy + inheritance.

Designed for 100% line + branch coverage of the module so it can be ratcheted
into the strict per-module 100% gate (DEC-COV-001 step 6).
"""

from __future__ import annotations

import pytest
from brutex.feed.errors import FeedError, LookAheadError, ResampleError
from brutex.models import BruteXError


class TestFeedErrorHierarchy:
    """Inheritance + instantiation contracts."""

    def test_feed_error_is_brutex_error(self) -> None:
        assert issubclass(FeedError, BruteXError)

    def test_lookahead_error_is_feed_error(self) -> None:
        assert issubclass(LookAheadError, FeedError)
        assert issubclass(LookAheadError, BruteXError)

    def test_resample_error_is_feed_error(self) -> None:
        assert issubclass(ResampleError, FeedError)
        assert issubclass(ResampleError, BruteXError)

    def test_feed_error_raises(self) -> None:
        with pytest.raises(FeedError, match="x"):
            raise FeedError("x")

    def test_lookahead_error_raises(self) -> None:
        with pytest.raises(LookAheadError, match="y"):
            raise LookAheadError("y")

    def test_resample_error_raises(self) -> None:
        with pytest.raises(ResampleError, match="z"):
            raise ResampleError("z")
