"""Unit tests for ``brutex.adapters.dhan.types`` — the never-raises
``FetchResult`` return contract of every :class:`DhanClient` method.

This file replaces the deleted version from step 8 (DEC-COV-001) which
guessed at field shapes. Each test construction here uses the REAL
verified API signatures:

* ``IntradayChunkRequest(instrument, interval, from_date, to_date)``
  where ``instrument`` is a ``DhanSpotInstrument`` (pre-built
  singletons live in :mod:`brutex.adapters.dhan.constants`), ``interval``
  ∈ ``SUPPORTED_INTRADAY_INTERVALS = ("1", "5", "15", "25", "60")``,
  and ``(to_date - from_date).days < INTRADAY_MAX_WINDOW_DAYS`` (90).
* ``HistoricalDailyRequest(instrument, from_date, to_date)`` with
  ``to_date >= from_date``.
* ``OHLCBar(timestamp_utc, open, high, low, close, volume)`` — prices
  are ``Decimal`` quantised to 0.01; volume is ``int`` (spot indexes
  always 0 per CLAUDE.md §7).
* ``DhanError`` subclasses (e.g. ``DhanError800``) constructed with a
  single ``detailed_message`` string; ``safe_message_default`` is
  read from the class attribute.
* ``FetchResult(request, outcome, bars, error)`` — frozen+slots
  dataclass, exactly one of ``bars`` (non-empty) and ``error`` is
  populated per outcome class.

Coverage target: 100% line + 100% branch on
``brutex/adapters/dhan/types.py`` (61 lines of pure dataclass code,
zero branches).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan.body import HistoricalDailyRequest, IntradayChunkRequest
from brutex.adapters.dhan.constants import NIFTY
from brutex.adapters.dhan.parser import OHLCBar
from brutex.adapters.dhan.types import FetchResult
from brutex.orchestrator.dhan_errors import DhanError800
from brutex.orchestrator.state import Outcome

# ────────────────────────────────────────────────────────────────────────
# Fixtures — built from the verified-real API field shapes
# ────────────────────────────────────────────────────────────────────────


def _intraday_request() -> IntradayChunkRequest:
    """One Track 1 intraday request — NIFTY 1-minute, single trading day."""
    return IntradayChunkRequest(
        instrument=NIFTY,
        interval="1",
        from_date=date(2026, 5, 1),
        to_date=date(2026, 5, 1),
    )


def _historical_request() -> HistoricalDailyRequest:
    """One Track 1 daily historical request — NIFTY one-day window."""
    return HistoricalDailyRequest(
        instrument=NIFTY,
        from_date=date(2026, 5, 1),
        to_date=date(2026, 5, 1),
    )


def _one_bar() -> OHLCBar:
    """One OHLC bar — NIFTY 09:15 IST = 03:45 UTC, paise quantised."""
    return OHLCBar(
        timestamp_utc=datetime(2026, 5, 1, 3, 45, tzinfo=UTC),
        open=Decimal("24500.00"),
        high=Decimal("24510.00"),
        low=Decimal("24495.00"),
        close=Decimal("24505.00"),
        volume=0,  # spot indexes always 0 per CLAUDE.md §7
    )


# ────────────────────────────────────────────────────────────────────────
# FetchResult construction — one test per Outcome category
# ────────────────────────────────────────────────────────────────────────


class TestFetchResultOkWithData:
    def test_ok_with_data_carries_bars_and_no_error(self) -> None:
        bar = _one_bar()
        result = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.OK_WITH_DATA,
            bars=(bar,),
            error=None,
        )
        assert result.outcome is Outcome.OK_WITH_DATA
        assert result.bars == (bar,)
        assert result.error is None

    def test_ok_with_data_multiple_bars(self) -> None:
        b1 = _one_bar()
        b2 = OHLCBar(
            timestamp_utc=datetime(2026, 5, 1, 3, 46, tzinfo=UTC),
            open=Decimal("24505.00"),
            high=Decimal("24515.00"),
            low=Decimal("24502.00"),
            close=Decimal("24512.00"),
            volume=0,
        )
        result = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.OK_WITH_DATA,
            bars=(b1, b2),
            error=None,
        )
        assert len(result.bars) == 2
        assert result.bars[0] is b1
        assert result.bars[1] is b2


class TestFetchResultOkEmpty:
    def test_ok_empty_intraday(self) -> None:
        result = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.OK_EMPTY,
            bars=(),
            error=None,
        )
        assert result.bars == ()
        assert result.error is None
        assert result.outcome is Outcome.OK_EMPTY

    def test_ok_empty_historical_request(self) -> None:
        """Historical-daily request can also carry an OK_EMPTY outcome."""
        result = FetchResult(
            request=_historical_request(),
            outcome=Outcome.OK_EMPTY,
            bars=(),
            error=None,
        )
        assert isinstance(result.request, HistoricalDailyRequest)
        assert result.bars == ()
        assert result.error is None


class TestFetchResultErrorOutcomes:
    def test_generic_api_error_carries_error_and_empty_bars(self) -> None:
        err = DhanError800("upstream returned 500 (internal server error)")
        result = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.GENERIC_API_ERROR,
            bars=(),
            error=err,
        )
        assert result.outcome is Outcome.GENERIC_API_ERROR
        assert result.error is err
        assert result.bars == ()

    def test_error_field_carries_dhan_error_subclass_attrs(self) -> None:
        """The DhanError subclass keeps its dhan_error_code + safe_message_default."""
        err = DhanError800("internal detail")
        result = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.GENERIC_API_ERROR,
            bars=(),
            error=err,
        )
        assert result.error is not None
        assert result.error.dhan_error_code == "800"  # class attr
        # safe_message_default is a documented class attr; instance carries it via __str__
        assert "internal server error" in str(result.error).lower()


# ────────────────────────────────────────────────────────────────────────
# Frozen + slots invariants
# ────────────────────────────────────────────────────────────────────────


class TestFetchResultFrozenContract:
    def test_attribute_assignment_raises_frozen_error(self) -> None:
        """frozen=True rejects any post-construction mutation."""
        result = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.OK_EMPTY,
            bars=(),
            error=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.outcome = Outcome.OK_WITH_DATA  # type: ignore[misc]

    def test_no_extra_attributes_allowed_by_slots(self) -> None:
        """slots=True means setattr for an unknown attribute fails.

        Python's frozen+slots dataclass machinery may raise FrozenInstanceError,
        AttributeError, OR TypeError depending on the interaction between
        ``__setattr__`` override and slot descriptor lookup. The contract is
        "no mutation"; the specific exception is a Python internal detail.
        """
        result = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.OK_EMPTY,
            bars=(),
            error=None,
        )
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
            result.new_field = "x"  # type: ignore[attr-defined]

    def test_equality_is_structural(self) -> None:
        """dataclass eq=True (default) — same field values → equal."""
        a = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.OK_EMPTY,
            bars=(),
            error=None,
        )
        b = FetchResult(
            request=_intraday_request(),
            outcome=Outcome.OK_EMPTY,
            bars=(),
            error=None,
        )
        assert a == b
