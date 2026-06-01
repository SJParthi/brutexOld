"""Tests for `brutex.adapters.dhan.synthetic` — dry-run spot-bar generator.

100% line+branch: count/spacing, valid OHLC ordering, in-session timestamps
(09:15 IST start, every bar survives the session filter), defaults, and the
non-positive-count guard.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan.synthetic import synthetic_spot_bars
from brutex.data.session import in_session


def test_count_and_spacing() -> None:
    bars = synthetic_spot_bars(date(2021, 1, 4), count=3, interval_minutes=5)
    assert len(bars) == 3
    # 09:15 IST == 03:45 UTC; +5m steps.
    assert bars[0].timestamp == datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
    assert bars[1].timestamp == datetime(2021, 1, 4, 3, 50, tzinfo=UTC)
    assert bars[2].timestamp == datetime(2021, 1, 4, 3, 55, tzinfo=UTC)


def test_valid_ohlc_and_volume() -> None:
    (bar,) = synthetic_spot_bars(date(2021, 1, 4), count=1, base_price=Decimal("250"))
    assert bar.open == Decimal("250")
    assert bar.high == Decimal("251")
    assert bar.low == Decimal("249")
    assert bar.close == Decimal("250.5")
    assert bar.volume == 1000
    assert bar.open_interest is None


def test_all_bars_are_in_session() -> None:
    # A full session of 1m bars must all pass the [09:15, 15:30) IST filter.
    bars = synthetic_spot_bars(date(2021, 1, 4), count=375)
    assert all(in_session(b.timestamp) for b in bars)


def test_defaults() -> None:
    bars = synthetic_spot_bars(date(2021, 1, 4))
    assert len(bars) == 5  # default count
    assert bars[0].open == Decimal("100")  # default base_price


def test_non_positive_count_raises() -> None:
    with pytest.raises(ValueError, match="count must be positive"):
        synthetic_spot_bars(date(2021, 1, 4), count=0)
