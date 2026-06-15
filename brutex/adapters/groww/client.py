"""Groww client — the thin SDK wrapper (the only network surface).

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5, PR-F). Wraps an **injected**
``growwapi.GrowwAPI`` instance behind the three neutral calls the ingest layer
needs (candles / expiries / contracts), translating every SDK exception through
:func:`brutex.adapters.groww.errors.from_groww_exception` and owning a bounded
in-call fast-retry for transient stalls (mirrors the canonical client pattern).

The SDK is injected (the :class:`GrowwSDK` Protocol), so every branch — happy
path, transient-retry, exhaustion, terminal — is unit-tested with a fake; no
``growwapi`` install and no network.

**Adaptive rate limiting lives HERE, at the one choke point EVERY Groww call
funnels through (DEC-GROWW-RATELIMIT-RETRY-001)** — so spot / expiries / contracts
/ futures / options / historical are uniformly protected, never one bypassed: each
:meth:`_call` (a) ``pace()``-acquires a token (≤ the injected ceiling), (b) on a
RETRYABLE error (rate-limit OR timeout, classified by
:func:`~brutex.adapters.groww.errors.from_groww_exception` — which now recognises a
generic ``GrowwAPIException("Rate limit …")``, the live 429 shape) backs off +
retries within a bounded budget, (c) fires ``on_rate_limit`` to **decrement** the
shared token bucket on every 429 and ``on_success`` to **recover** it on a clean
call (the operator's decremental/incremental contract). The orchestrator's
work-item re-queue (layer 3) still re-pulls any window that exhausts this in-call
budget — belt-and-suspenders. O(1): the retry budget is a fixed constant; the
client holds no per-symbol state.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import structlog

from brutex.adapters.groww.errors import (
    GrowwRateLimitError,
    from_groww_exception,
    is_retryable,
)

#: Per-call observability (DEC-GROWW-CALL-LATENCY-LOG-001): every physical Groww REST
#: call logs its wall-clock latency at INFO (``groww.call``) so the operator can SEE
#: the real per-call response time live in ``/logs`` — instead of back-deriving it.
#: Covers ALL three calls (historical / expiries / contracts), so the historical
#: pull's true latency is finally measurable rather than assumed.
_LOG = structlog.get_logger("brutex.adapters.groww.client")


def _noop_pace() -> None:
    """Default rate-bucket hook — no-op until a controller is injected."""


def _noop_hook() -> None:
    """Default decrement/recover feedback hook — no-op until a controller is injected."""


def _noop_sleep(_seconds: float) -> None:  # pragma: no cover — replaced in tests + by real backoff
    """Default backoff sleeper (injected in tests so no real time passes)."""
    import time

    time.sleep(_seconds)


def run_with_deadline(
    fn: Callable[[], Mapping[str, object]], seconds: float
) -> Mapping[str, object]:
    """Run *fn* in a daemon thread, raising a timeout if it blows the wall-clock deadline.

    THE HISTORICAL-PULL FREEZE GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001). The Groww
    ``get_historical_candles`` SDK call carries no inherent wall-clock bound, so a
    stalled connection (half-open socket, server not responding) would block the
    pull worker FOREVER — the :class:`GrowwClient` retry loop cannot help because it
    only fires on a *raised* error, and a hung call never raises. This caps EVERY
    call: if *fn* has not returned within *seconds*, raise a :class:`TimeoutError`
    whose message carries the ``deadline exceeded`` hint, so
    :func:`~brutex.adapters.groww.errors.from_groww_exception` classifies it as a
    retryable :class:`~brutex.adapters.groww.errors.GrowwTimeoutError`. The bounded
    retry loop then re-issues it; a persistently-stalled window finally FAILS (and
    is re-attempted next pull) instead of freezing the whole pull. The abandoned
    worker is a daemon — it dies with the process and never blocks shutdown.
    """
    import threading

    result: dict[str, Mapping[str, object]] = {}
    failure: dict[str, Exception] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            result["value"] = fn()
        except Exception as exc:  # re-raised on the caller thread below (any SDK error)
            failure["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_runner, name="groww-pull-call", daemon=True).start()
    if not done.wait(seconds):
        raise TimeoutError(f"Groww historical pull call deadline exceeded ({seconds:g}s)")
    if "error" in failure:
        raise failure["error"]
    return result["value"]


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
    on_rate_limit: Callable[[], None] = field(default=_noop_hook)
    on_success: Callable[[], None] = field(default=_noop_hook)
    max_attempts: int = 6
    backoff_seconds: float = 0.5
    rate_limit_backoff_seconds: float = 2.0
    #: Hard per-call wall-clock deadline (seconds) — the historical-pull FREEZE GUARD
    #: (DEC-GROWW-PULL-CALL-DEADLINE-001). ``None`` (the default) keeps the bare
    #: behaviour for unit tests + dry-runs; the LIVE pull build sites inject
    #: ``GROWW_PULL_CALL_TIMEOUT_S`` so a stalled SDK call can never hang the pull.
    call_timeout_seconds: float | None = None
    #: Wall-clock source for the per-call latency log (DEC-GROWW-CALL-LATENCY-LOG-001);
    #: injected in tests for deterministic ``elapsed_ms`` assertions.
    clock: Callable[[], float] = time.perf_counter

    def _call(
        self, fn: Callable[[], Mapping[str, object]], *, label: str = ""
    ) -> Mapping[str, object]:
        """Run *fn* through the adaptive limiter: pace → retry retryable → decrement/recover.

        Every physical call acquires a token (:attr:`pace`), then on a RETRYABLE
        failure (rate-limit OR timeout — :func:`is_retryable`) backs off + retries
        within :attr:`max_attempts`; a rate-limit additionally fires
        :attr:`on_rate_limit` (decrement the shared bucket) on EVERY occurrence and
        uses the longer :attr:`rate_limit_backoff_seconds`. A clean call fires
        :attr:`on_success` (recover the rate up). A terminal error (auth / bad-
        request / not-found / unknown), or a retryable one that exhausts the budget,
        re-raises the neutral :class:`GrowwError` for the caller to handle (the
        orchestrator's layer-3 re-queue). Linear backoff (``base × attempt``) keeps
        it O(1)-state + bounded.
        """
        attempt = 0
        while True:
            attempt += 1
            self.pace()
            started = self.clock()
            try:
                # Bound EVERY physical call by the wall-clock deadline so a stalled
                # connection can never freeze the pull (DEC-GROWW-PULL-CALL-DEADLINE-001);
                # a timeout raises → classified retryable → handled by this same loop.
                result = (
                    fn()
                    if self.call_timeout_seconds is None
                    else run_with_deadline(fn, self.call_timeout_seconds)
                )
            except Exception as exc:  # broad: any SDK error → neutral GrowwError
                err = from_groww_exception(exc)
                if isinstance(err, GrowwRateLimitError):
                    self.on_rate_limit()  # decrement the shared bucket on EVERY 429
                if is_retryable(err) and attempt < self.max_attempts:
                    base = (
                        self.rate_limit_backoff_seconds
                        if isinstance(err, GrowwRateLimitError)
                        else self.backoff_seconds
                    )
                    self.sleep(base * attempt)
                    continue
                raise err from exc
            else:
                self.on_success()  # recover the rate up on a clean call
                # Per-call latency observability (DEC-GROWW-CALL-LATENCY-LOG-001): the
                # REAL response time of THIS Groww REST call, so the operator can SEE
                # it live in /logs instead of it being back-derived/assumed.
                _LOG.info(
                    "groww.call",
                    method=label,
                    attempt=attempt,
                    elapsed_ms=round((self.clock() - started) * 1000.0, 1),
                )
                return result

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
            ),
            label="historical_candles",
        )

    def expiries(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> Mapping[str, object]:
        """List FNO expiries for an underlying, optionally scoped to a year/month.

        ``year`` and ``month`` are OPTIONAL per the SDK
        (``docs/groww/api-reference/03-backtesting.md:60-79``): a ``None`` ``year``
        defaults to the current year, a ``None`` ``month`` to the WHOLE year. Each
        is OMITTED from the SDK call when ``None`` (never forwarded as ``None``), so
        the option enumerator can issue ONE call per historical year (month omitted)
        to cover the full FNO depth (DEC-GROWW-OPTION-INGEST-001).
        """
        kwargs: dict[str, object] = {"exchange": exchange, "underlying_symbol": underlying_symbol}
        if year is not None:
            kwargs["year"] = year
        if month is not None:
            kwargs["month"] = month
        return self._call(lambda: self.sdk.get_expiries(**kwargs), label="get_expiries")

    def contracts(
        self, *, exchange: str, underlying_symbol: str, expiry_date: str
    ) -> Mapping[str, object]:
        """List contract symbols for an underlying + expiry."""
        return self._call(
            lambda: self.sdk.get_contracts(
                exchange=exchange, underlying_symbol=underlying_symbol, expiry_date=expiry_date
            ),
            label="get_contracts",
        )


__all__ = ["GrowwClient", "GrowwSDK"]
