"""Groww client — the thin SDK wrapper (the only network surface).

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5, PR-F). Wraps an **injected**
``growwapi.GrowwAPI`` instance behind the three neutral calls the ingest layer
needs (candles / expiries / contracts), translating every SDK exception through
:func:`brutex.adapters.groww.errors.from_groww_exception` and owning a bounded
in-call fast-retry for transient stalls (mirrors the canonical client pattern).

The SDK is injected (the :class:`GrowwSDK` Protocol), so every branch — happy
path, transient-retry, exhaustion, terminal — is unit-tested with a fake; no
``growwapi`` install and no network. Rate limiting + the work-item re-queue stay
the orchestrator's job (CLAUDE.md §11 COMMON); a rate-limit error is therefore
NEVER fast-retried here (the token bucket owns 429 backoff) — only a
:class:`GrowwTimeoutError` gets the bounded retry. O(1): the retry budget is a
fixed constant; the client holds no per-symbol state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from brutex.adapters.groww.errors import GrowwTimeoutError, from_groww_exception


def _noop_pace() -> None:
    """Default rate-bucket hook — no-op until a controller is injected."""


def _noop_sleep(_seconds: float) -> None:  # pragma: no cover — replaced in tests + by real backoff
    """Default backoff sleeper (injected in tests so no real time passes)."""
    import time

    time.sleep(_seconds)


@runtime_checkable
class GrowwSDK(Protocol):
    """The subset of the ``growwapi.GrowwAPI`` surface BruteX calls.

    Methods accept ``**_kwargs`` (the real SDK fixes the exact keyword names);
    the client below always calls them with the explicit Groww keyword names.
    """

    def get_historical_candles(self, **_kwargs: object) -> Mapping[str, object]:
        """Return one historical-candle window (row-of-arrays payload)."""
        ...

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        """Return the FNO expiries for an underlying."""
        ...

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        """Return the contract symbols for an underlying + expiry."""
        ...


@dataclass(frozen=True, slots=True)
class GrowwClient:
    """Neutral wrapper over an injected :class:`GrowwSDK`.

    Attributes
    ----------
    sdk
        The injected SDK (real ``GrowwAPI`` in production, a fake in tests).
    pace
        Rate-bucket acquire hook fired before every physical call (defence in
        depth; the orchestrator owns the real limiter).
    sleep
        Backoff sleeper (injected in tests).
    max_attempts
        Bounded fast-retry budget for a transient timeout (O(1)).
    backoff_seconds
        Base linear backoff between retry attempts.
    """

    sdk: GrowwSDK
    pace: Callable[[], None] = _noop_pace
    sleep: Callable[[float], None] = _noop_sleep
    max_attempts: int = 3
    backoff_seconds: float = 0.5

    def _call(self, fn: Callable[[], Mapping[str, object]]) -> Mapping[str, object]:
        """Run *fn*, mapping SDK errors and fast-retrying only a transient timeout."""
        attempt = 0
        while True:
            attempt += 1
            self.pace()
            try:
                return fn()
            except Exception as exc:  # broad: any SDK error → neutral GrowwError
                err = from_groww_exception(exc)
                if isinstance(err, GrowwTimeoutError) and attempt < self.max_attempts:
                    self.sleep(self.backoff_seconds * attempt)
                    continue
                raise err from exc

    def historical_candles(
        self,
        *,
        exchange: str,
        segment: str,
        groww_symbol: str,
        interval: str,
        start: str,
        end: str,
    ) -> Mapping[str, object]:
        """Fetch one historical-candle window (the row-of-arrays payload)."""
        return self._call(
            lambda: self.sdk.get_historical_candles(
                exchange=exchange,
                segment=segment,
                groww_symbol=groww_symbol,
                start_time=start,
                end_time=end,
                candle_interval=interval,
            )
        )

    def expiries(
        self, *, exchange: str, underlying_symbol: str, year: int, month: int
    ) -> Mapping[str, object]:
        """List FNO expiries for an underlying in a given year/month."""
        return self._call(
            lambda: self.sdk.get_expiries(
                exchange=exchange, underlying_symbol=underlying_symbol, year=year, month=month
            )
        )

    def contracts(
        self, *, exchange: str, underlying_symbol: str, expiry_date: str
    ) -> Mapping[str, object]:
        """List contract symbols for an underlying + expiry."""
        return self._call(
            lambda: self.sdk.get_contracts(
                exchange=exchange, underlying_symbol=underlying_symbol, expiry_date=expiry_date
            )
        )


__all__ = ["GrowwClient", "GrowwSDK"]
