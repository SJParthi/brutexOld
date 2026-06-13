"""Sustained-load stress test for the synthetic generator (DEC-SYNTHETIC-ADAPTER-001).

Generates a long multi-month run of bars and asserts every one is valid and the
whole sequence is byte-for-byte reproducible on a re-run (idempotent resume).
Bounded by default (~a few months → tens of thousands of bars, ~seconds on CI);
override ``SYNTHETIC_STRESS_DAYS`` for a longer local soak.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal

import pytest
from brutex.adapters.synthetic import SyntheticMarketAdapter
from brutex.calendars import IST, get_session
from brutex.models import Exchange

pytestmark = pytest.mark.stress

#: Calendar span (days) to sweep. Default keeps CI ~seconds; bump locally.
_STRESS_DAYS = int(os.environ.get("SYNTHETIC_STRESS_DAYS", "120"))
_START = date(2021, 1, 1)


def test_sustained_generation_is_valid_and_idempotent() -> None:
    adapter = SyntheticMarketAdapter()
    end = _START + timedelta(days=_STRESS_DAYS)
    bars = adapter.fetch_ohlcv(
        symbol="SYNTH", exchange=Exchange.NSE, interval="1minute", start=_START, end=end
    )
    assert bars  # the span includes real sessions

    prev_ts = None
    for b in bars:
        assert b.low <= b.open <= b.high
        assert b.low <= b.close <= b.high
        assert b.high >= b.low
        for px in (b.open, b.high, b.low, b.close):
            assert px == px.quantize(Decimal("0.01"))
        assert b.volume >= 0
        ist = b.timestamp.astimezone(IST)
        session = get_session(ist.date())
        assert session is not None
        assert session.open_time_ist <= ist.time() < session.close_time_ist
        if prev_ts is not None:
            assert b.timestamp > prev_ts
        prev_ts = b.timestamp

    # Idempotent resume: a second sweep reproduces the run bit-for-bit.
    again = adapter.fetch_ohlcv(
        symbol="SYNTH", exchange=Exchange.NSE, interval="1minute", start=_START, end=end
    )
    assert again == bars
