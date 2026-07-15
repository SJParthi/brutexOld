"""Dhan SDK refresher — thread-safe, single-flight, READ-ONLY-REREAD ONLY.

DEC-DHAN-READONLY-AUTH-001 (ADR-0022). The Dhan 24-h JWT access token is minted by
a dedicated Lambda / TickVault into AWS SSM; BruteX is a strictly READ-ONLY
consumer. This refresher is the Dhan twin of
:class:`~brutex.adapters.groww.reauth.SdkRefresher` with its **local-mint arm
structurally absent** — the ONLY self-heal path is a RE-READ of the shared SSM
token:

* **COLD START** builds the initial SDK from a bounded-retry READ of SSM
  (:meth:`_cold_start_sdk`); with the token still absent after the budget it FAILS
  LOUD naming the resolved parameter — it NEVER mints
  (:func:`_readonly_cold_start_error`).
* **``reauth(seen_generation)``** is THREAD-SAFE (a :class:`threading.Lock` guards
  every read + the re-read) and SINGLE-FLIGHT (a monotonic :attr:`generation`
  counter lets a 06:00 / rotation stampede re-read EXACTLY once). On a DH-901
  (:class:`~brutex.adapters.dhan.errors.DhanAuthError`) it RE-READS SSM
  (:meth:`_reread_and_adopt_sdk`) and adopts a NEW token, or raises the RETRYABLE
  :class:`~brutex.adapters.dhan.errors.DhanTokenStillStaleError` when the read
  returns the SAME dead token / an SSM miss (the minter has not rotated yet — the
  window quarantines + re-pulls LATER, never a loop, never a restart).
* **FREEZE-GUARDED** — the SSM re-read (and cold-start read) network calls are
  bounded by the wall-clock :func:`~brutex.config.deadline.run_with_deadline` so a
  hung read raises a RETRYABLE :class:`~brutex.adapters.dhan.errors.DhanTimeoutError`
  out of the released lock, never freezing every pool leaf.

There is NO disk token cache and NO session anchoring (a Dhan token is a plain
non-blank string, not 06:00-IST session-keyed).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV,
)
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanTokenStillStaleError,
    from_dhan_exception,
)
from brutex.adapters.dhan.token_store_aws import DhanCachedToken
from brutex.adapters.groww.constants import resolve_token_param_path
from brutex.config.deadline import run_with_deadline

_LOG = structlog.get_logger("brutex.adapters.dhan.reauth")

#: The wall-clock bound (seconds) for the SSM re-read / cold-start read network call.
DHAN_REREAD_TIMEOUT_S: float = 30.0

#: Bounded cold-start READ retries before failing loud (the minter's rotation may
#: land any moment). Small — the read is O(1) and the caller has its own budget.
DHAN_COLD_START_ATTEMPTS: int = 3

#: Fixed cold-start back-off (seconds) between READ retries — the read is cheap.
DHAN_COLD_START_BACKOFF_S: float = 1.0


@runtime_checkable
class DhanSdk(Protocol):
    """The read-path ``dhanhq`` SDK surface — exactly ``intraday_minute_data``.

    Kept structural (a :class:`~typing.Protocol`) so the fake in tests AND the real
    ``dhanhq`` object both satisfy it with no shared import. Returns the SDK's flat
    response envelope (``{"status", "data" | "remarks"}``).
    """

    def intraday_minute_data(
        self,
        *,
        security_id: int,
        exchange_segment: str,
        instrument_type: str,
        from_date: str,
        to_date: str,
        interval: int,
        oi: bool,
    ) -> Mapping[str, object]:
        """Fetch one 1-minute intraday window (the flat response envelope)."""
        ...


#: A token re-read hook: reads the shared SSM token (best-effort — NEVER raises,
#: returns ``None`` on a miss). In production this is the read-only store's ``load``.
TokenRereadHook = Callable[..., "DhanCachedToken | None"]

#: An SDK factory: builds the read-path SDK from the two credentials (NO network at
#: construction — ``DhanContext(client_id, access_token)`` is inert). Injected in
#: tests; the real factory needs the ``dhanhq`` SDK.
SdkFactory = Callable[[str, str], "DhanSdk"]


def _now_utc() -> datetime:
    """The current UTC instant — the production clock (injectable in tests)."""
    return datetime.now(tz=UTC)


def _real_sdk_factory(
    client_id: str, access_token: str
) -> DhanSdk:  # pragma: no cover — needs dhanhq
    """Default SDK factory: ``dhanhq(DhanContext(client_id, access_token))`` (no network).

    ``DhanContext`` makes NO network call at construction (VERIFIED-live: SDK
    ``dhan_context.py``); the returned ``dhanhq`` object exposes
    ``intraday_minute_data`` (the read path). Only reachable with the ``dhanhq`` SDK
    installed; the fake drives every branch that USES the SDK.
    """
    from dhanhq import DhanContext, dhanhq  # type: ignore[import-not-found, unused-ignore]

    sdk: DhanSdk = dhanhq(DhanContext(client_id, access_token))
    return sdk


def _readonly_cold_start_error(attempts: int) -> DhanAuthError:
    """The LOUD, actionable cold-start error for a read-only host with no readable token.

    Names the RESOLVED SSM parameter (the env override is honoured) + the operator
    fix. Raised INSTEAD of ever minting — this host is a strictly read-only consumer
    (the minter Lambda / TickVault is the SOLE minter). Contains NO secret values.
    """
    param = resolve_token_param_path(
        DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
    )
    return DhanAuthError(
        f"cold start on a READ-ONLY Dhan token host: no readable shared access token "
        f"after {attempts} read attempts — AWS Parameter Store parameter {param!r} is "
        f"absent or unreachable, and this host NEVER mints (the minter Lambda / "
        f"TickVault is the sole minter; a local mint could invalidate the shared token "
        f"another consumer is using). Fix: verify the minter is deployed + ran and "
        f"this host's IAM reader role grants ssm:GetParameter + kms:Decrypt on "
        f"{param!r}, then re-run the pull once the parameter is present."
    )


class DhanSdkRefresher:
    """Thread-safe single-flight RE-READER of a fresh Dhan read-path SDK.

    Parameters
    ----------
    client_id
        The (public) Dhan account client id (constant across the pull).
    token_reread
        The read-only token store's ``load`` (best-effort — NEVER raises, returns
        ``None`` on a miss). Used for BOTH the cold-start read and the mid-run
        re-read; the local-mint arm is structurally absent (read-only host).
    sdk_factory
        Builds the read-path SDK from ``(client_id, access_token)`` (default: the
        real ``dhanhq`` factory — no network at construction). Injected in tests.
    clock
        UTC-instant source passed to *token_reread* (injected in tests).
    call_timeout_seconds
        Wall-clock bound for the READ network call (the freeze guard).
    cold_start_attempts, cold_start_backoff, sleep
        The bounded cold-start READ retry budget + back-off (the minter's rotation
        may land any moment) + the injected sleeper.

    Raises
    ------
    DhanAuthError
        If the cold start finds no readable shared token after the bounded READ
        retries — the loud, actionable :func:`_readonly_cold_start_error` naming the
        resolved SSM parameter, raised BEFORE any mint could fire (there is none).
    DhanTimeoutError
        If the cold-start READ HANGS past *call_timeout_seconds* (the freeze guard).
    """

    __slots__ = (
        "_call_timeout_seconds",
        "_client_id",
        "_clock",
        "_cold_start_attempts",
        "_cold_start_backoff",
        "_current_token",
        "_generation",
        "_lock",
        "_sdk",
        "_sdk_factory",
        "_sleep",
        "_token_reread",
    )

    def __init__(
        self,
        *,
        client_id: str,
        token_reread: TokenRereadHook,
        sdk_factory: SdkFactory = _real_sdk_factory,
        clock: Callable[[], datetime] = _now_utc,
        call_timeout_seconds: float = DHAN_REREAD_TIMEOUT_S,
        cold_start_attempts: int = DHAN_COLD_START_ATTEMPTS,
        cold_start_backoff: float = DHAN_COLD_START_BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client_id = client_id
        self._token_reread = token_reread
        self._sdk_factory = sdk_factory
        self._clock = clock
        self._call_timeout_seconds = call_timeout_seconds
        self._cold_start_attempts = max(1, cold_start_attempts)
        self._cold_start_backoff = cold_start_backoff
        self._sleep = sleep
        self._lock = threading.Lock()
        self._generation = 0
        #: The token the live SDK was built from — tracked so a re-read can tell a
        #: rotated NEW token (adopt) from the SAME dead token still in SSM (retryable).
        self._current_token = ""  # nosec B105 — empty sentinel, not a credential
        self._sdk = self._cold_start_sdk()

    def _read_token(self) -> str | None:
        """Freeze-guarded SSM READ → the token string, or ``None`` on a miss.

        Bounds the best-effort ``token_reread`` by the wall-clock deadline so a hung
        read raises a RETRYABLE :class:`~brutex.adapters.dhan.errors.DhanTimeoutError`
        (mapped from the stdlib :class:`TimeoutError`) rather than freezing. A clean
        return yields the token string (``None`` on a miss).
        """
        try:
            cached = run_with_deadline(
                lambda: self._token_reread(now_utc=self._clock()), self._call_timeout_seconds
            )
        except TimeoutError as exc:
            raise from_dhan_exception(exc) from exc
        return cached.access_token if cached is not None else None

    def _cold_start_sdk(self) -> DhanSdk:
        """Build the INITIAL SDK from a bounded-retry READ — NEVER mints.

        Reads the shared token; the first non-blank read builds the SDK. On a miss it
        bounded-retries the READ (the minter's rotation may land any moment) and,
        when the budget exhausts, FAILS LOUD with :func:`_readonly_cold_start_error`.
        The back-off sleeps run on the constructing thread, outside any lock.
        """
        for attempt in range(self._cold_start_attempts):
            token = self._read_token()
            if token is not None:
                self._current_token = token
                return self._sdk_factory(self._client_id, token)
            if attempt == self._cold_start_attempts - 1:
                raise _readonly_cold_start_error(self._cold_start_attempts)
            _LOG.warning(
                "dhan.token.readonly_cold_start_retry",
                attempt=attempt + 1,
                backoff_s=self._cold_start_backoff,
            )
            self._sleep(self._cold_start_backoff)
        # Unreachable: the final iteration returns or raises above (the loop runs
        # >= 1 time). Assert the invariant for the type-checker.
        raise AssertionError(
            "unreachable: cold-start loop always returns or raises"
        )  # pragma: no cover

    def current_sdk(self) -> DhanSdk:
        """Return the live read-path SDK (lock-guarded read, O(1))."""
        with self._lock:
            return self._sdk

    def current_generation(self) -> int:
        """Return the current re-auth generation (lock-guarded read, O(1))."""
        with self._lock:
            return self._generation

    def reauth(self, seen_generation: int) -> int:
        """RE-READ the shared token + adopt a rotated one, SINGLE-FLIGHT (NEVER mints).

        Under the lock: if :attr:`generation` already moved past *seen_generation*
        (another thread re-read since — the rotation stampede), return the CURRENT
        generation with NO second re-read. Otherwise RE-READ SSM (freeze-guarded); a
        NEW token rebuilds the SDK + bumps the generation, while the SAME dead token /
        an SSM miss raises the RETRYABLE
        :class:`~brutex.adapters.dhan.errors.DhanTokenStillStaleError`.

        Raises
        ------
        DhanTokenStillStaleError
            The re-read returned the SAME dead token or an SSM miss (re-pull later).
        DhanTimeoutError
            The re-read HANGS past the freeze-guard deadline (re-pull that window).
        """
        with self._lock:
            if self._generation != seen_generation:
                return self._generation
            pinned_token = self._current_token
            new_sdk, new_token = self._reread_and_adopt_sdk(pinned_token)
            self._sdk = new_sdk
            self._current_token = new_token
            self._generation += 1
            return self._generation

    def _reread_and_adopt_sdk(self, pinned_token: str) -> tuple[DhanSdk, str]:
        """RE-READ SSM + adopt a rotated token (never mint); raise stale on same/absent.

        The rejected token to compare against is *pinned_token*, captured under the
        reauth lock at :meth:`reauth` entry. A read that returns a NEW token
        (``!= pinned_token``) rebuilds the SDK and logs ``dhan.token.reread`` (the
        self-heal); a read that returns the SAME dead token, or ``None`` (an SSM
        miss — the minter has not rotated yet), raises the RETRYABLE
        :class:`~brutex.adapters.dhan.errors.DhanTokenStillStaleError` so the window
        re-pulls LATER. Runs under the reauth lock; the READ is freeze-guarded.
        """
        fresh_token = self._read_token()
        if fresh_token is None or fresh_token == pinned_token:
            _LOG.warning("dhan.token.reread_still_stale", present=fresh_token is not None)
            raise DhanTokenStillStaleError(
                "read-only Dhan re-read returned no fresh token yet: the shared access "
                "token has not been re-minted by the minter; re-pull later."
            )
        _LOG.warning("dhan.token.reread")
        return (self._sdk_factory(self._client_id, fresh_token), fresh_token)


__all__ = [
    "DHAN_COLD_START_ATTEMPTS",
    "DHAN_COLD_START_BACKOFF_S",
    "DHAN_REREAD_TIMEOUT_S",
    "DhanSdk",
    "DhanSdkRefresher",
]
