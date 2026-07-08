"""Tests for `brutex.data.option_dedup` — O(1) option-bar dedup.

100% line+branch: canonical key (incl. None strike), the Bloom + exact
two-tier `seen`/`add`/`add_if_new` (Bloom-negative short-circuit, Bloom-
positive confirm, and the false-positive case via an injected always-true
Bloom), the inject-vs-default ctor branches, and batch `unique_option_bars`.

Ordering pins (DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001): a spy exact tier
proves a Bloom-NEGATIVE never probes the exact backing (bloom-before-exact),
and an always-true Bloom proves a false positive can never phantom-drop a
genuinely-new key at either probe entry (`add_if_new` / `unique_option_bars`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.data.candle_fields import CandleParseError
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


class _SpyExact:
    """A set-backed exact tier that counts membership probes (the ordering spy)."""

    def __init__(self) -> None:
        self._backing: set[bytes] = set()
        self.contains_probes = 0

    def __contains__(self, key: bytes, /) -> bool:
        self.contains_probes += 1
        return key in self._backing

    def add(self, key: bytes, /) -> None:
        self._backing.add(key)


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

    @pytest.mark.parametrize("field", ["underlying", "expiry", "strike", "side"])
    def test_separator_in_value_rejected(self, field: str) -> None:
        # DEC-PULL-QUARANTINE-ROBUST-001 FIX 5: a field VALUE carrying the reserved unit
        # separator is rejected with a quarantinable CandleParseError. Without the guard two
        # DISTINCT tuples could hash to the SAME key (e.g. underlying="A\x1fB",expiry="E" vs
        # underlying="A",expiry="B\x1fE") — a SILENT false-dedup that DROPS a real bar.
        kwargs: dict[str, object] = {
            "underlying": "A",
            "expiry": "E",
            "strike": "100",
            "side": "CE",
            "epoch": 1,
        }
        kwargs[field] = "x\x1fy"
        with pytest.raises(CandleParseError, match="reserved unit separator"):
            option_bar_key(**kwargs)  # type: ignore[arg-type]

    def test_separator_collision_is_prevented(self) -> None:
        # The exact collision the guard exists to prevent now raises instead of returning
        # equal keys for two distinct tuples.
        with pytest.raises(CandleParseError):
            option_bar_key(underlying="A\x1fB", expiry="E", strike="S", side="CE", epoch=1)

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

    def test_bloom_negative_short_circuits_exact_tier(self) -> None:
        # Ordering pin (DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001): `_seen_locked`
        # probes the Bloom FIRST — an empty real Bloom is deterministically
        # negative, and the negative answers "definitely new" WITHOUT ever
        # touching the exact tier (bloom-before-exact).
        spy = _SpyExact()
        idx = OptionDedupIndex(capacity=1024, fpr_target=1e-3, exact=spy)
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        assert idx.seen(key) is False
        assert spy.contains_probes == 0  # negative short-circuited the exact probe
        assert idx.add_if_new(key) is True  # add_if_new's probe short-circuits too
        assert spy.contains_probes == 0
        # Once added, the Bloom-positive falls through to the exact confirm.
        assert idx.seen(key) is True
        assert spy.contains_probes == 1

    def test_add_if_new_under_false_positive_bloom_admits_new_key(self) -> None:
        # No-phantom pin (DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001): with an
        # all-positive Bloom (every probe a false positive) the exact tier
        # must still admit a genuinely-NEW key — it is NEVER reported seen /
        # dropped — while a true duplicate still dedups.
        idx = OptionDedupIndex(bloom=_AlwaysTrueBloom())
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        assert idx.add_if_new(key) is True  # new key admitted despite the FP
        assert idx.add_if_new(key) is False  # exact-confirmed duplicate dropped

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

    def test_unique_option_bars_under_false_positive_bloom_keeps_new_bars(self) -> None:
        # No-phantom pin at the batch entry (DEC-OPTION-DEDUP-BLOOM-VERIFY-
        # PIN-001): every genuinely-new bar survives an all-positive Bloom
        # (the exact tier disambiguates each false positive), and the
        # idempotent re-pull still collapses to nothing.
        idx = OptionDedupIndex(bloom=_AlwaysTrueBloom())
        bars = [_bar(minute=0), _bar(minute=1)]
        kept = unique_option_bars(bars, idx, underlying="NIFTY", expiry="E")
        assert [b.timestamp.minute for b in kept] == [0, 1]  # nothing phantom-dropped
        assert unique_option_bars(bars, idx, underlying="NIFTY", expiry="E") == []
