"""Groww SDK test fakes — FakeGrowwClient + FakeDataFrame + exceptions.

These are the canonical fakes used by every BruteX test that exercises
the Groww adapter or any higher layer. They are name-compatible with
the real ``growwapi`` SDK so that the retry classifier's MRO dispatch
correctly matches synthetic exceptions to real exception names.

## Why these live in tests/fakes/

Previously ``FakeGrowwClient`` etc. were defined inside
``tests/unit/test_adapters_groww_client.py`` and imported by
neighbouring tests via the ``from tests.unit.test_foo import Bar``
anti-pattern. That couples unrelated tests together and breaks the
moment a test file is renamed. This module is the consolidation.

## What's here

* Synthetic Groww exception classes (name-matched to
  ``growwapi.groww.exceptions``)
* ``FakeDataFrame`` — minimal pandas stand-in with ``.empty`` +
  ``len()`` + ``to_dict(orient='records')``
* ``FakeGrowwClient`` — implements :class:`GrowwClientProtocol` with
  per-method configurable responses + exceptions + call counters
"""

from __future__ import annotations

from typing import Any

# ────────────────────────────────────────────────────────────────────────
# Synthetic Groww exception classes
# ────────────────────────────────────────────────────────────────────────


class GrowwBaseException(Exception):
    """Stand-in for ``growwapi.groww.exceptions.GrowwBaseException``."""


class GrowwAPIException(GrowwBaseException):
    """Stand-in for ``GrowwAPIException``."""


class GrowwAPIRateLimitException(GrowwAPIException):
    """Stand-in for ``GrowwAPIRateLimitException``."""


class GrowwAPITimeoutException(GrowwAPIException):
    """Stand-in for ``GrowwAPITimeoutException``."""


class GrowwAPINotFoundException(GrowwAPIException):
    """Stand-in for ``GrowwAPINotFoundException``."""


class GrowwAPIBadRequestException(GrowwAPIException):
    """Stand-in for ``GrowwAPIBadRequestException``."""


class GrowwAPIAuthenticationException(GrowwAPIException):
    """Stand-in for ``GrowwAPIAuthenticationException``."""


class GrowwAPIAuthorisationException(GrowwAPIException):
    """British spelling — matches the real Groww SDK class name."""


class GrowwFeedException(GrowwBaseException):
    """Stand-in for ``GrowwFeedException``."""


class GrowwFeedConnectionException(GrowwFeedException):
    """Stand-in for ``GrowwFeedConnectionException``."""


class GrowwFeedNotSubscribedException(GrowwFeedException):
    """Stand-in for ``GrowwFeedNotSubscribedException``."""


# ────────────────────────────────────────────────────────────────────────
# FakeDataFrame — minimal pandas stand-in
# ────────────────────────────────────────────────────────────────────────


class FakeDataFrame:
    """Minimal pandas-like DataFrame for testing.

    Supports:
    * ``len(df)`` — number of rows
    * ``df.empty`` — True if zero rows
    * ``df.to_dict(orient='records')`` — list of row dicts

    These are all the methods BruteX's instruments normaliser uses.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def empty(self) -> bool:
        return len(self._rows) == 0

    def to_dict(self, orient: str = "records") -> list[dict[str, Any]]:
        if orient != "records":
            raise NotImplementedError(
                f"FakeDataFrame.to_dict only supports orient='records', got {orient!r}"
            )
        return list(self._rows)


# ────────────────────────────────────────────────────────────────────────
# FakeGrowwClient — implements GrowwClientProtocol
# ────────────────────────────────────────────────────────────────────────


class FakeGrowwClient:
    """Test double for ``growwapi.GrowwAPI``.

    Every method has a configurable ``_response`` and ``_exception``
    pair. The exception takes precedence if set. Call counters track
    how many times each method was invoked so tests can assert on
    dedup behaviour.
    """

    def __init__(self) -> None:
        # Phase A: get_all_instruments
        self.instruments_response: Any = FakeDataFrame([{"groww_symbol": "NSE-NIFTY"}])
        self.instruments_exception: Exception | None = None

        # Phase B: get_expiries
        self.expiries_response: dict[str, list[str]] = {"expiries": ["2024-01-25"]}
        self.expiries_exception: Exception | None = None

        # Phase C: get_contracts
        self.contracts_response: dict[str, list[str]] = {
            "contracts": ["NSE-NIFTY-25Jan24-22000-CE"]
        }
        self.contracts_exception: Exception | None = None

        # Phase D: get_historical_candles
        self.candles_response: dict[str, Any] = {
            "candles": [
                [
                    "2024-01-25T09:15:00",
                    22000.0,
                    22010.0,
                    21990.0,
                    22005.0,
                    100,
                    None,
                ]
            ],
            "closing_price": 22005.0,
            "start_time": "2024-01-25 09:15:00",
            "end_time": "2024-01-25 09:16:00",
            "interval_in_minutes": 1,
        }
        self.candles_exception: Exception | None = None

        # Call counters — tests assert on these
        self.get_all_instruments_calls = 0
        self.get_instrument_by_groww_symbol_calls = 0
        self.get_expiries_calls = 0
        self.get_contracts_calls = 0
        self.get_historical_candles_calls = 0

    def get_all_instruments(self) -> Any:
        self.get_all_instruments_calls += 1
        if self.instruments_exception is not None:
            raise self.instruments_exception
        return self.instruments_response

    def get_instrument_by_groww_symbol(self, groww_symbol: str) -> dict[str, Any]:
        self.get_instrument_by_groww_symbol_calls += 1
        return {"groww_symbol": groww_symbol}

    def get_expiries(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, list[str]]:
        self.get_expiries_calls += 1
        if self.expiries_exception is not None:
            raise self.expiries_exception
        return self.expiries_response

    def get_contracts(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        expiry_date: str,
    ) -> dict[str, list[str]]:
        self.get_contracts_calls += 1
        if self.contracts_exception is not None:
            raise self.contracts_exception
        return self.contracts_response

    def get_historical_candles(
        self,
        *,
        exchange: str,
        segment: str,
        groww_symbol: str,
        start_time: str,
        end_time: str,
        candle_interval: str,
    ) -> dict[str, Any]:
        self.get_historical_candles_calls += 1
        if self.candles_exception is not None:
            raise self.candles_exception
        return self.candles_response


__all__ = [
    "FakeDataFrame",
    "FakeGrowwClient",
    "GrowwAPIAuthenticationException",
    "GrowwAPIAuthorisationException",
    "GrowwAPIBadRequestException",
    "GrowwAPIException",
    "GrowwAPINotFoundException",
    "GrowwAPIRateLimitException",
    "GrowwAPITimeoutException",
    "GrowwBaseException",
    "GrowwFeedConnectionException",
    "GrowwFeedException",
    "GrowwFeedNotSubscribedException",
]
