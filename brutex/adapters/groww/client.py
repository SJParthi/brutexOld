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
from dataclasses import dataclass, field, replace
from typing import Protocol, Self, runtime_checkable

import structlog

from brutex.adapters.call_meter import CallMeter
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
    #: ACHIEVED-throughput meter (DEC-GROWW-RATE-ACHIEVED-METER-001) — records every
    #: physical call's start/stop so the dashboard + ``/logs`` show the REAL calls/s
    #: (not the controller's target). Defaults to a private per-client meter; the live
    #: pull injects ONE shared meter so a multi-connection pool reports an aggregate.
    #: Excluded from eq/repr — it is mutable observability state, not client identity
    #: (two clients differing only by meter instance stay equal; ``replace`` keeps it).
    meter: CallMeter = field(default_factory=CallMeter, compare=False, repr=False)

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
            # Bracket the physical call for the achieved-throughput meter
            # (DEC-GROWW-RATE-ACHIEVED-METER-001): start AFTER pace() so in-flight
            # measures the call itself, not the token wait; stop on BOTH outcomes so the
            # in-flight slot is always released (a failed attempt counts only its
            # in-flight, never as achieved throughput — that stays SUCCESSFUL calls).
            self.meter.start()
            started = self.clock()
            elapsed_ms = 0.0
            completed = False
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
                elapsed_ms = round((self.clock() - started) * 1000.0, 1)
                completed = True  # a clean call IS achieved throughput (recorded below)
                self.on_success()  # recover the rate up on a clean call
                # Per-call latency observability (DEC-GROWW-CALL-LATENCY-LOG-001): the
                # REAL response time of THIS Groww REST call, so the operator can SEE
                # it live in /logs instead of it being back-derived/assumed.
                _LOG.info(
                    "groww.call",
                    method=label,
                    attempt=attempt,
                    elapsed_ms=elapsed_ms,
                )
                return result
            finally:
                # ALWAYS release the in-flight slot (success, retry, terminal, OR a
                # BaseException — KeyboardInterrupt / a futures cancellation — that
                # `except Exception` would miss and leak the very gauge this DEC makes
                # trustworthy, DEC-GROWW-RATE-ACHIEVED-METER-001); a clean call also
                # records the completion (completed=True), a failed one does not.
                self.meter.stop(elapsed_ms=elapsed_ms, completed=completed)

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

    def with_pace(self, pace: Callable[[], None]) -> GrowwClient:
        """Return a copy of this client with its ``pace`` hook rebound (O(1), immutable).

        The :class:`GrowwClientProtocol` seam the ingest spine uses
        (:func:`~brutex.adapters.groww.run_ingest._with_paced_client`) to wire the shared
        rate bucket into the client's internal fast-retry AFTER the adapter is built —
        replacing the prior raw :func:`dataclasses.replace`, so the SAME call also re-fans
        the pace into every leaf of a multi-connection :class:`~brutex.adapters.groww.pool.\
ClientPool` (DEC-GROWW-CLIENT-POOL-001). Frozen dataclass → :func:`replace`, never a
        mutation; the shared :attr:`meter` is carried through unchanged (``compare=False``).
        """
        return replace(self, pace=pace)


@runtime_checkable
class GrowwClientProtocol(Protocol):
    """The neutral client surface the spot adapter + option enumerator depend on.

    BOTH the single :class:`GrowwClient` AND the multi-connection
    :class:`~brutex.adapters.groww.pool.ClientPool` satisfy this structurally, so the
    adapter's ``client`` slot (and the shared candle / contract fetchers) accept EITHER
    without change (DEC-GROWW-CLIENT-POOL-001). In both cases ONE shared
    :class:`~brutex.adapters.rate_common.RateController` caps the AGGREGATE issue rate and
    ONE shared :class:`~brutex.adapters.call_meter.CallMeter` reports the achieved rate;
    the pool merely fans physical calls across K independent SDK connections so the worker
    threads stop serialising on a single shared socket (the measured 0.49-in-flight starve).
    """

    @property
    def meter(self) -> CallMeter:
        """The shared achieved-throughput meter the dashboard rate panel reads.

        Read-only (``_observe_rate`` → ``adapter.client.meter.snapshot()``); both a frozen
        :class:`GrowwClient` (a dataclass field) and a :class:`~brutex.adapters.groww.pool.\
ClientPool` (a ``@property`` over its ONE aggregate meter) satisfy it.
        """
        ...

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
        ...

    def expiries(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> Mapping[str, object]:
        """List FNO expiries for an underlying."""
        ...

    def contracts(
        self, *, exchange: str, underlying_symbol: str, expiry_date: str
    ) -> Mapping[str, object]:
        """List contract symbols for an underlying + expiry."""
        ...

    def with_pace(self, pace: Callable[[], None]) -> Self:
        """Return a copy with the rate-bucket ``pace`` hook rebound (re-fanned to leaves)."""
        ...


__all__ = ["GrowwClient", "GrowwClientProtocol", "GrowwSDK"]
