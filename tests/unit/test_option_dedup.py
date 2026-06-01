"""Tests for `brutex.data.option_dedup` — O(1) option-bar dedup.

100% line+branch: canonical key (incl. None strike), the Bloom + exact
two-tier `seen`/`add`/`add_if_new` (Bloom-negative short-circuit, Bloom-
positive confirm, and the false-positive case via an injected always-true
Bloom), the inject-vs-default ctor branches, and batch `unique_option_bars`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import OptionBar
from brutex.data.option_dedup import (
    OptionDedupIndex,
    key_for_option_bar,
    option_bar_key,
    unique_option_bars,
)
from brutex.orchestrator.bloom import BloomFilter

_TS = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)  # 09:30 IST


def _bar(*, side: str = "CE", strike: str | None = "17500", minute: int = 0) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=None if strike is None else Decimal(strike),
        spot=Decimal("17480.5"),
        iv=Decimal("0.15"),
    )


def _small_index() -> OptionDedupIndex:
    # Small Bloom keeps the test light (the 100M default is ~419 MB).
    return OptionDedupIndex(capacity=1024, fpr_target=1e-3)


class _AlwaysTrueBloom:
    """A Bloom double whose `might_contain` is always True (forces confirm tier)."""

    def __init__(self) -> None:
        self.added: list[bytes] = []

    def add(self, content_hash: bytes) -> None:
        self.added.append(content_hash)

    def might_contain(self, content_hash: bytes) -> bool:
        del content_hash
        return True


class TestKey:
    def test_key_is_32_byte_digest(self) -> None:
        key = option_bar_key(
            underlying="NIFTY", expiry="2021-01-07", strike="17500", side="CE", epoch=1609732800
        )
        assert isinstance(key, bytes)
        assert len(key) == 32

    def test_distinct_tuples_distinct_keys(self) -> None:
        a = option_bar_key(underlying="NIFTY", expiry="E", strike="100", side="CE", epoch=1)
        b = option_bar_key(underlying="NIFTY", expiry="E", strike="100", side="PE", epoch=1)
        assert a != b

    def test_key_for_bar_uses_bar_fields(self) -> None:
        bar = _bar(side="PE", strike="200")
        expected = option_bar_key(
            underlying="NIFTY",
            expiry="2021-01-07",
            strike="200",
            side="PE",
            epoch=int(bar.timestamp.timestamp()),
        )
        assert key_for_option_bar(bar, underlying="NIFTY", expiry="2021-01-07") == expected

    def test_none_strike_stringifies(self) -> None:
        bar = _bar(strike=None)
        key = key_for_option_bar(bar, underlying="NIFTY", expiry="E")
        # Same as an explicit "None" strike — deterministic, no crash.
        assert key == option_bar_key(
            underlying="NIFTY",
            expiry="E",
            strike="None",
            side="CE",
            epoch=int(bar.timestamp.timestamp()),
        )


class TestDedupIndex:
    def test_new_key_not_seen(self) -> None:
        idx = _small_index()
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        assert idx.seen(key) is False

    def test_add_then_seen(self) -> None:
        idx = _small_index()
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        idx.add(key)
        assert idx.seen(key) is True

    def test_add_if_new_returns_true_then_false(self) -> None:
        idx = _small_index()
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        assert idx.add_if_new(key) is True
        assert idx.add_if_new(key) is False

    def test_bloom_false_positive_does_not_false_dedup(self) -> None:
        # Injected Bloom claims membership for everything; the exact tier must
        # still report an unknown key as new (no false dedup).
        idx = OptionDedupIndex(bloom=_AlwaysTrueBloom())
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        assert idx.seen(key) is False  # Bloom-positive, but exact tier says new
        idx.add(key)
        assert idx.seen(key) is True

    def test_injected_exact_backing_is_used(self) -> None:
        backing: set[bytes] = set()
        idx = OptionDedupIndex(capacity=1024, fpr_target=1e-3, exact=backing)
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        idx.add(key)
        assert key in backing

    def test_injected_bloom_is_used(self) -> None:
        bloom = BloomFilter(capacity=1024, fpr_target=1e-3)
        idx = OptionDedupIndex(bloom=bloom)
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        idx.add(key)
        assert bloom.might_contain(key) is True


class TestUniqueOptionBars:
    def test_drops_duplicate_rerun(self) -> None:
        idx = _small_index()
        first = [_bar(minute=0), _bar(minute=1)]
        # First pass: both new.
        kept = unique_option_bars(first, idx, underlying="NIFTY", expiry="2021-01-07")
        assert len(kept) == 2
        # Re-pull the same window + one new bar → only the new one survives.
        second = [_bar(minute=0), _bar(minute=1), _bar(minute=2)]
        kept2 = unique_option_bars(second, idx, underlying="NIFTY", expiry="2021-01-07")
        assert [b.timestamp.minute for b in kept2] == [2]

    def test_ce_and_pe_same_minute_are_distinct(self) -> None:
        idx = _small_index()
        bars = [_bar(side="CE", minute=0), _bar(side="PE", minute=0)]
        kept = unique_option_bars(bars, idx, underlying="NIFTY", expiry="E")
        assert len(kept) == 2  # different side → different key → both kept
