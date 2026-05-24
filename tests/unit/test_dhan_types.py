"""Tests for :mod:`brutex.adapters.dhan.types` — FetchResult dataclass.

Designed for 100% line + branch coverage (DEC-COV-001 step 8).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from brutex.adapters.dhan.body import IntradayChunkRequest
from brutex.adapters.dhan.parser import OHLCBar
from brutex.adapters.dhan.types import FetchResult
from brutex.orchestrator.dhan_errors import (
    DhanError,
    DhanRateLimitError,
)
from brutex.orchestrator.state import Outcome


def _req() -> IntradayChunkRequest:
    return IntradayChunkRequest(
        security_id=13,
        exchange_segment="IDX_I",
        instrument="INDEX",
        interval="1",
        from_date="2026-05-01",
        to_date="2026-05-02",
        oi=False,
    )


class TestFetchResultConstruction:
    def test_ok_with_data(self) -> None:
        bar = OHLCBar(ts_utc=1.0, open=100.0, high=101.0, low=99.0, close=100.5, volume=100, oi=0)
        r = FetchResult(request=_req(), outcome=Outcome.OK_WITH_DATA, bars=(bar,), error=None)
        assert r.outcome is Outcome.OK_WITH_DATA
        assert r.bars == (bar,)
        assert r.error is None

    def test_ok_empty(self) -> None:
        r = FetchResult(request=_req(), outcome=Outcome.OK_EMPTY, bars=(), error=None)
        assert r.outcome is Outcome.OK_EMPTY
        assert r.bars == ()
        assert r.error is None

    def test_error_carries_typed_error(self) -> None:
        err: DhanError = DhanRateLimitError("rate-limited", status_code=429)
        r = FetchResult(request=_req(), outcome=Outcome.RATE_LIMIT, bars=(), error=err)
        assert r.outcome is Outcome.RATE_LIMIT
        assert r.bars == ()
        assert r.error is err

    def test_is_frozen(self) -> None:
        r = FetchResult(request=_req(), outcome=Outcome.OK_EMPTY, bars=(), error=None)
        with pytest.raises(FrozenInstanceError):
            r.outcome = Outcome.FATAL  # type: ignore[misc]
