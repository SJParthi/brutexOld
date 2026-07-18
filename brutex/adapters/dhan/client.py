"""Dhan client — the thin transport wrapper (the only network surface).

DEC-DHAN-ADAPTER-001. Wraps an **injected** :class:`~brutex.adapters.dhan.transport.\
DhanTransport` behind the typed Data-API calls the (future) ingest layer needs
(historical / intraday candles, option chain, expiry list, rolling-option,
RenewToken, profile validation), translating every failure through
:func:`brutex.adapters.dhan.errors.from_dhan_response` /
:func:`~brutex.adapters.dhan.errors.from_transport_exception` and owning a bounded
in-call fast-retry + a bounded token-refresh self-heal.

Adaptive rate limiting lives HERE, at the one choke point every Dhan call funnels
through (mirroring the Groww client): each :meth:`_call` (a) ``pace()``-acquires a
token, (b) on a RETRYABLE error backs off + retries within ``max_attempts``, (c)
fires ``on_rate_limit`` on every 429 and ``on_success`` on a clean call. The
option-chain call additionally honours the SPECIAL 1-request/3-seconds pacer.

The transport is injected, so every branch is unit-tested with a fake — no
``requests`` install, no network. **INERT / reference-only** (DEC-DHAN-ADAPTER-001):
nothing production wires a live transport into it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, Self, runtime_checkable

import structlog

from brutex.adapters.call_meter import CallMeter
from brutex.adapters.dhan.constants import (
    DHAN_ENDPOINT_EXPIRY_LIST,
    DHAN_ENDPOINT_HISTORICAL,
    DHAN_ENDPOINT_INTRADAY,
    DHAN_ENDPOINT_OPTION_CHAIN,
    DHAN_ENDPOINT_PROFILE,
    DHAN_ENDPOINT_RENEW_TOKEN,
    DHAN_ENDPOINT_ROLLING_OPTION,
    DHAN_HEADER_ACCESS_TOKEN,
    DHAN_HEADER_CLIENT_ID,
    DHAN_HEADER_DHAN_CLIENT_ID_UNVERIFIED,
    DHAN_MAX_REAUTHS,
)
from brutex.adapters.dhan.errors import (
    DhanError,
    body_signals_failure,
    from_dhan_response,
    from_transport_exception,
    is_auth_failure,
    is_rate_limit,
    is_retryable,
)
from brutex.adapters.dhan.reauth import DhanTokenRefresher
from brutex.adapters.dhan.transport import DhanTransport

_LOG = structlog.get_logger("brutex.adapters.dhan.client")

#: The HTTP success band (2xx). A non-2xx is ALWAYS classified by
#: :func:`~brutex.adapters.dhan.errors.from_dhan_response`; a 2xx is a success
#: ONLY when its body carries no documented failure marker
#: (:func:`~brutex.adapters.dhan.errors.body_signals_failure` — Dhan can deliver
#: its DH-9xx / ``{status:"failure"}`` envelope on an HTTP 200).
_HTTP_OK_FLOOR = 200
_HTTP_OK_CEILING = 300


def _noop_pace() -> None:
    """No-op rate-bucket acquire (the offline / inert default)."""


def _noop_sleep(_seconds: float) -> None:
    """No-op backoff sleeper (deterministic tests inject a recorder)."""


def _noop_hook() -> None:
    """No-op rate-feedback hook (decrement / recover) — the offline default."""


@dataclass(frozen=True, slots=True)
class DhanClient:
    """Neutral wrapper over an injected :class:`DhanTransport`.

    Attributes
    ----------
    transport
        The injected HTTP transport (real ``requests`` in production, a fake in tests).
    access_token, client_id
        The Dhan JWT + client-id sent on requests (the client-id header is added only
        where the endpoint requires it, e.g. the option chain).
    pace
        Rate-bucket acquire hook fired before every physical call — INCLUDING the
        option-chain call (every endpoint here is a Data-API request against the
        shared 5/s aggregate cap).
    option_chain_pace
        The SPECIAL 1-request/3-seconds pacer fired before the option-chain call
        (Dhan's option-chain rate limit) — a no-op by default. ADDITIONAL to —
        never instead of — the shared :attr:`pace` (see
        :meth:`_option_chain_acquire`).
    sleep
        Backoff sleeper (injected in tests).
    on_rate_limit, on_success
        AIMD feedback hooks (decrement on 429, recover on a clean call).
    refresher
        Optional single-flight token refresher; when set, an AUTH-error call re-reads
        the shared token (read-only broker self-heal) and retries. ``None`` keeps the
        pre-refresher behaviour (an auth error raises terminally).
    max_attempts
        Bounded fast-retry budget for a transient error (O(1)).
    max_reauths
        Bounded token-refresh budget — a genuinely-dead token re-reads at most this
        many times then raises terminally (never an infinite loop).
    clock
        Wall-clock source for the per-call latency meter (injected in tests).
    meter
        Achieved-throughput meter (excluded from eq/repr — mutable observability).
    """

    transport: DhanTransport
    access_token: str = field(repr=False)  # a JWT secret — never rendered in repr/logs
    client_id: str
    pace: Callable[[], None] = _noop_pace
    option_chain_pace: Callable[[], None] = _noop_pace
    sleep: Callable[[float], None] = field(default=_noop_sleep)
    on_rate_limit: Callable[[], None] = field(default=_noop_hook)
    on_success: Callable[[], None] = field(default=_noop_hook)
    refresher: DhanTokenRefresher | None = field(default=None, compare=False, repr=False)
    max_attempts: int = 6
    max_reauths: int = DHAN_MAX_REAUTHS
    backoff_seconds: float = 0.5
    rate_limit_backoff_seconds: float = 2.0
    clock: Callable[[], float] = time.perf_counter
    meter: CallMeter = field(default_factory=CallMeter, compare=False, repr=False)

    @property
    def self_paces(self) -> bool:
        """Whether this client admits every physical call through a REAL ``pace`` hook."""
        return self.pace is not _noop_pace

    @property
    def self_reports_rate_limits(self) -> bool:
        """Whether this client feeds every 429 to a REAL ``on_rate_limit`` hook."""
        return self.on_rate_limit is not _noop_hook

    def _resolve_token(self) -> str:
        """Return the token to send — the refresher's live one if wired, else :attr:`access_token`."""
        return self.refresher.access_token if self.refresher is not None else self.access_token

    def _headers(self, *, include_client_id: bool = False) -> dict[str, str]:
        """Build request headers — the access token, plus the client-id where required."""
        headers = {DHAN_HEADER_ACCESS_TOKEN: self._resolve_token()}
        if include_client_id:
            headers[DHAN_HEADER_CLIENT_ID] = self.client_id
        return headers

    def _error_for(self, status: int, body: Mapping[str, object]) -> DhanError | None:
        """Classify ``(status, body)`` — ``None`` iff the response is a GENUINE success.

        A non-2xx status is always classified. A 2xx is a success ONLY when the
        body carries no documented failure marker (:func:`~brutex.adapters.dhan.\
        errors.body_signals_failure`): Dhan can deliver a DH-9xx /
        ``{status:"failure", remarks:{...}}`` failure envelope ON an HTTP 200 —
        the errors module's second failure mechanism — and it must ride the SAME
        :func:`~brutex.adapters.dhan.errors.from_dhan_response` law: a
        200-delivered DH-901 drives the reauth/terminal-auth path, and every
        other 200-body failure classifies to its TYPED TERMINAL error (no DH
        code maps to a rate limit and status 200 is not in the HTTP-status
        table, so a 200-body failure without a mapped DH code lands on the
        conservative terminal ``DhanError`` base) — never a fake success dying
        downstream as a misleading parse error. ``on_rate_limit`` fires only on
        the non-2xx branch (a 429 STATUS — where Dhan documents its throttles).
        The marker gate is POSITIVE-presence only, so a clean success payload
        can never be misclassified.
        """
        if _HTTP_OK_FLOOR <= status < _HTTP_OK_CEILING and not body_signals_failure(body):
            return None
        return from_dhan_response(status, body)

    def _option_chain_acquire(self) -> None:
        """Acquire the SHARED Data-API token, THEN the option-chain special interval.

        The option chain is a Data-API request, so it must consume a shared 5/s
        token like every sibling endpoint AND honour the SPECIAL 1-req/3s minimum
        interval — :attr:`option_chain_pace` is ADDITIONAL to, never instead of,
        the shared :attr:`pace`. (Paced by the 3s pacer alone, a live wiring
        would issue option-chain requests OUTSIDE the shared bucket: 5/s candles
        + 0.33/s chain = 5.33/s aggregate, breaching the VERIFIED Data-API 5/s
        cap, with the shared AIMD fed 429s it never issued into.) Order law: the
        shared token FIRST, the special interval LAST — closest to the physical
        issue, so a blocking shared acquire can never erode the 3 s inter-issue
        guarantee (a late issue after a token grab only ever LOWERS the realised
        rate, never raises it).
        """
        self.pace()
        self.option_chain_pace()

    def _backoff(self, *, rate_limited: bool, attempt: int) -> float:
        """Linear backoff (``base × attempt``); the longer base for a rate-limit."""
        base = self.rate_limit_backoff_seconds if rate_limited else self.backoff_seconds
        return base * attempt

    def _call(
        self,
        call: Callable[[], tuple[int, Mapping[str, object]]],
        *,
        pace: Callable[[], None],
        label: str,
    ) -> Mapping[str, object]:
        """Run *call* through the adaptive limiter: pace → classify → retry / reauth / raise.

        Every physical call acquires a token (*pace*), then runs ``call()`` (which does
        the transport request and returns ``(status, body)``). A 2xx whose body carries
        no failure marker returns the body and fires :attr:`on_success`; a non-2xx, a
        marker-carrying 2xx body, or a transport exception is classified. A
        RETRYABLE failure backs off + retries within :attr:`max_attempts` (a rate-limit
        additionally fires :attr:`on_rate_limit` on every occurrence). A terminal AUTH
        failure with a wired :attr:`refresher` and budget remaining RE-READS the shared
        token and retries WITHOUT counting against :attr:`max_attempts`; a re-read that
        is still stale raises the retryable :class:`~brutex.adapters.dhan.errors.\
        DhanTokenStillStaleError` OUT for the window to re-pull later. Anything else
        re-raises the neutral :class:`~brutex.adapters.dhan.errors.DhanError`. O(1) state.
        """
        attempt = 0
        reauths = 0
        while True:
            attempt += 1
            pace()
            self.meter.start()
            started = self.clock()
            completed = False
            try:
                try:
                    status, body = call()
                except Exception as exc:  # broad: any transport error → neutral DhanError
                    err = from_transport_exception(exc)
                else:
                    classified = self._error_for(status, body)
                    if classified is None:
                        completed = True
                        self.on_success()
                        return body
                    err = classified
                if is_rate_limit(err):
                    self.on_rate_limit()
                if (
                    is_auth_failure(err)
                    and not is_retryable(err)
                    and self.refresher is not None
                    and reauths < self.max_reauths
                ):
                    reauths += 1
                    attempt -= 1  # a reauth retry is free — never counts against the retry budget
                    _LOG.info("dhan.call.reauth", label=label, reauths=reauths)
                    self.refresher.reauth()  # raises DhanTokenStillStaleError (retryable) if stale
                    continue
                if is_retryable(err) and attempt < self.max_attempts:
                    _LOG.info(
                        "dhan.call.retry",
                        label=label,
                        attempt=attempt,
                        error_type=type(err).__name__,
                    )
                    self.sleep(self._backoff(rate_limited=is_rate_limit(err), attempt=attempt))
                    continue
                raise err
            finally:
                self.meter.stop(elapsed_ms=(self.clock() - started) * 1000.0, completed=completed)

    # ── typed endpoint methods ─────────────────────────────────────────────

    def historical_candles(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        from_date: str,
        to_date: str,
        oi: bool = False,
        expiry_code: int | None = None,
    ) -> Mapping[str, object]:
        """Fetch one DAILY historical window (``POST /v2/charts/historical``)."""
        body: dict[str, object] = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "oi": oi,
            "fromDate": from_date,
            "toDate": to_date,
        }
        if expiry_code is not None:
            body["expiryCode"] = expiry_code
        return self._call(
            lambda: self.transport.post_json(
                path=DHAN_ENDPOINT_HISTORICAL, body=body, headers=self._headers()
            ),
            pace=self.pace,
            label="historical",
        )

    def intraday_candles(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        interval: str,
        from_date: str,
        to_date: str,
        oi: bool = False,
    ) -> Mapping[str, object]:
        """Fetch one INTRADAY window (``POST /v2/charts/intraday``)."""
        body: dict[str, object] = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": interval,
            "oi": oi,
            "fromDate": from_date,
            "toDate": to_date,
        }
        return self._call(
            lambda: self.transport.post_json(
                path=DHAN_ENDPOINT_INTRADAY, body=body, headers=self._headers()
            ),
            pace=self.pace,
            label="intraday",
        )

    def option_chain(
        self, *, underlying_scrip: int, underlying_seg: str, expiry: str
    ) -> Mapping[str, object]:
        """Fetch the option chain (``POST /v2/optionchain``) — shared pace + 1-req/3s pacer.

        Requires the ``client-id`` header. Every physical attempt acquires BOTH the
        shared Data-API token (:attr:`pace`) AND the special option-chain interval
        (:attr:`option_chain_pace` — a no-op by default; a live site wires a
        3-second min-interval pacer ON TOP of the shared 5/s controller). See
        :meth:`_option_chain_acquire` for the composition + order law.
        """
        body: dict[str, object] = {
            "UnderlyingScrip": underlying_scrip,
            "UnderlyingSeg": underlying_seg,
            "Expiry": expiry,
        }
        return self._call(
            lambda: self.transport.post_json(
                path=DHAN_ENDPOINT_OPTION_CHAIN,
                body=body,
                headers=self._headers(include_client_id=True),
            ),
            pace=self._option_chain_acquire,
            label="option_chain",
        )

    def expiry_list(self, *, underlying_scrip: int, underlying_seg: str) -> Mapping[str, object]:
        """Fetch the expiry list (``POST /v2/optionchain/expirylist``)."""
        body: dict[str, object] = {
            "UnderlyingScrip": underlying_scrip,
            "UnderlyingSeg": underlying_seg,
        }
        return self._call(
            lambda: self.transport.post_json(
                path=DHAN_ENDPOINT_EXPIRY_LIST,
                body=body,
                headers=self._headers(include_client_id=True),
            ),
            pace=self.pace,
            label="expiry_list",
        )

    def rolling_option(
        self,
        *,
        exchange_segment: str,
        security_id: str,
        instrument: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        interval: str,
        from_date: str,
        to_date: str,
        required_data: Sequence[str],
    ) -> Mapping[str, object]:
        """Fetch one rolling-option (expired, ATM-relative) window (``POST /v2/charts/rollingoption``)."""
        body: dict[str, object] = {
            "exchangeSegment": exchange_segment,
            "securityId": security_id,
            "instrument": instrument,
            "expiryFlag": expiry_flag,
            "expiryCode": expiry_code,
            "strike": strike,
            "drvOptionType": drv_option_type,
            "interval": interval,
            "fromDate": from_date,
            "toDate": to_date,
            "requiredData": list(required_data),
        }
        return self._call(
            lambda: self.transport.post_json(
                path=DHAN_ENDPOINT_ROLLING_OPTION, body=body, headers=self._headers()
            ),
            pace=self.pace,
            label="rolling_option",
        )

    def renew_token(self) -> Mapping[str, object]:
        """Renew an ACTIVE token for another 24 h (``POST /v2/RenewToken``).

        VERIFIED — Dhan refreshes an ACTIVE token; renewing an EXPIRED token errors
        (the vendor returns an auth error → the caller re-mints). Sends the ASSUMED
        ``dhanClientId`` header (the spelling is model-inferred — probe-gated,
        :data:`~brutex.adapters.dhan.constants.DHAN_HEADER_DHAN_CLIENT_ID_UNVERIFIED`).
        This is the NON-read-only renewal path (a read-only
        broker host self-heals via :attr:`refresher` re-read instead) — documented +
        tested but not wired into the inert reference-only pull.
        """
        headers = self._headers()
        headers[DHAN_HEADER_DHAN_CLIENT_ID_UNVERIFIED] = self.client_id
        return self._call(
            lambda: self.transport.post_json(
                path=DHAN_ENDPOINT_RENEW_TOKEN, body={}, headers=headers
            ),
            pace=self.pace,
            label="renew_token",
        )

    def validate_token(self) -> bool:
        """Validate the current token via ``GET /v2/profile`` — ``True`` on a 2xx.

        Any classified failure raises the neutral :class:`~brutex.adapters.dhan.errors.\
        DhanError` (an auth failure surfaces as :class:`~brutex.adapters.dhan.errors.\
        DhanAuthError`); a 2xx returns ``True``.
        """
        self._call(
            lambda: self.transport.get_json(path=DHAN_ENDPOINT_PROFILE, headers=self._headers()),
            pace=self.pace,
            label="validate_token",
        )
        return True

    # ── builder seams ──────────────────────────────────────────────────────

    def with_pace(self, pace: Callable[[], None]) -> DhanClient:
        """Return a copy bound to *pace* (the live limiter's acquire hook)."""
        return replace(self, pace=pace)

    def with_rate_hooks(
        self,
        *,
        pace: Callable[[], None],
        on_rate_limit: Callable[[], None],
        on_success: Callable[[], None],
        option_chain_pace: Callable[[], None] | None = None,
    ) -> DhanClient:
        """Return a copy bound to the full rate-feedback triple (+ optional option-chain pacer)."""
        return replace(
            self,
            pace=pace,
            on_rate_limit=on_rate_limit,
            on_success=on_success,
            option_chain_pace=self.option_chain_pace
            if option_chain_pace is None
            else option_chain_pace,
        )


@runtime_checkable
class DhanClientProtocol(Protocol):
    """The client surface the adapter + orchestration call — satisfied by client AND pool."""

    def historical_candles(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        from_date: str,
        to_date: str,
        oi: bool = False,
        expiry_code: int | None = None,
    ) -> Mapping[str, object]:
        """Fetch one daily historical window."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def intraday_candles(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        interval: str,
        from_date: str,
        to_date: str,
        oi: bool = False,
    ) -> Mapping[str, object]:
        """Fetch one intraday window."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def option_chain(
        self, *, underlying_scrip: int, underlying_seg: str, expiry: str
    ) -> Mapping[str, object]:
        """Fetch the option chain."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def expiry_list(self, *, underlying_scrip: int, underlying_seg: str) -> Mapping[str, object]:
        """Fetch the expiry list."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def rolling_option(
        self,
        *,
        exchange_segment: str,
        security_id: str,
        instrument: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        interval: str,
        from_date: str,
        to_date: str,
        required_data: Sequence[str],
    ) -> Mapping[str, object]:
        """Fetch one rolling-option window."""
        raise NotImplementedError  # structural Protocol stub — never executed

    @property
    def meter(self) -> CallMeter:
        """The shared achieved-throughput meter."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def with_pace(self, pace: Callable[[], None]) -> Self:
        """Return a copy bound to *pace*."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def with_rate_hooks(
        self,
        *,
        pace: Callable[[], None],
        on_rate_limit: Callable[[], None],
        on_success: Callable[[], None],
        option_chain_pace: Callable[[], None] | None = None,
    ) -> Self:
        """Return a copy bound to the rate-feedback hooks."""
        raise NotImplementedError  # structural Protocol stub — never executed


__all__ = ["DhanClient", "DhanClientProtocol"]
