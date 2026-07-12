"""DhanHQ v2 headless auth — PIN+TOTP mint + RenewToken (the SINGLE secret boundary).

DEC-DHAN-AUTH-001. Mints the daily access token via the headless
``POST https://auth.dhan.co/app/generateAccessToken?dhanClientId&pin&totp`` flow (VERIFIED in
the official SDK) and renews via ``GET .../v2/RenewToken`` (headers only). This module is the
ONE place a transport exception may originate, so it is the ONE place secret scrubbing is
enforced: EVERY transport exception is caught here and re-raised as a scrub-at-construction
:class:`~brutex.adapters.dhan.errors.DhanAuthError` with ``from None`` — the raw urllib
object (whose ``__str__`` embeds the secret-bearing mint URL) NEVER escapes the module, and
is never chained onto ``__cause__``.

Both side effects — the TOTP code (``pyotp``) and the HTTPS call (``urllib``) — are injected
callables, so every branch is unit-tested with fakes and no network. The real shims are the
only ``# pragma: no cover`` lines.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_API_BASE,
    DHAN_AUTH_HTTP_TIMEOUT_S,
    DHAN_AUTH_MINT_URL,
    DHAN_CLIENT_ID_HEADER,
    DHAN_RENEW_PATH,
    DHAN_TOKEN_HEADER,
    DHAN_TOKEN_ISSUANCE_RETRY_ATTEMPTS,
    DHAN_TOKEN_ISSUANCE_RETRY_BASE_S,
    DHAN_TOKEN_ISSUANCE_RETRY_CAP_S,
)
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanAuthInvalidCredentials,
    DhanAuthTransportError,
    DhanTokenMintThrottled,
    DhanTokenRenewError,
    is_retryable_auth,
)
from brutex.config.credentials_types import DhanCredentials

_LOG = structlog.get_logger("brutex.adapters.dhan.auth")

#: HTTP statuses that mean the CREDENTIALS are bad (terminal — never retried).
_INVALID_CRED_STATUSES: frozenset[int] = frozenset({400, 401, 403})
#: HTTP status that means the vendor THROTTLED the mint (retryable).
_THROTTLE_STATUS: int = 429
#: Response keys the minted access token may live under (response shape UNVERIFIED — Phase-0).
_TOKEN_KEYS: tuple[str, ...] = ("accessToken", "access_token", "token")


class HttpTransport(Protocol):
    """The narrow HTTPS surface the auth calls use — one JSON request.

    ``request_json`` performs *method* on *url* with *headers* and returns the parsed JSON
    mapping on a 2xx; it RAISES on any non-2xx / network / timeout error (the raised object
    may carry a ``status``/``code`` int the classifier reads). The real implementation is a
    urllib shim; a fake drives every branch.
    """

    def request_json(
        self, method: str, url: str, *, headers: Mapping[str, str], timeout_s: float
    ) -> Mapping[str, Any]:
        """Return the parsed JSON body on 2xx, or RAISE on any error."""
        raise NotImplementedError  # structural Protocol stub — never executed


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A freshly obtained Dhan access token + WHEN and HOW it was obtained.

    Attributes
    ----------
    token
        The access-token JWT (``field(repr=False)`` is not used — the token is the payload,
        but callers must never log it; the store logs only ``sha256(token)[:8]``).
    minted_at_utc
        OUR mint/renew instant (used for the expiry budget — never a JWT claim).
    source
        ``"mint"`` (fresh PIN+TOTP) or ``"renew"`` (RenewToken on an active token).
    """

    token: str
    minted_at_utc: datetime
    source: Literal["mint", "renew"]


def _now_utc() -> datetime:
    """The current UTC instant (injectable in tests)."""
    return datetime.now(tz=UTC)


def _real_totp(secret: str) -> str:  # pragma: no cover — needs pyotp + a real secret
    """Default TOTP minter: ``pyotp.TOTP(secret).now()`` (lazy import)."""
    import pyotp

    return str(pyotp.TOTP(secret).now())


def _parse_json_object(body: str) -> Mapping[str, Any]:
    """Parse *body* as JSON, requiring a JSON OBJECT (a scalar/array is an anomalous response).

    Extracted from the urllib shim so the defensive non-object guard is UNIT-TESTED (it is
    the real safety net for a Dhan endpoint returning a JSON array/scalar — without it a
    non-``Mapping`` would reach :func:`_extract_token` as a ``.get``-less value and raise an
    uncaught ``AttributeError`` OUTSIDE the transport try/except). A malformed body raises
    ``json.JSONDecodeError`` (a ``ValueError``), caught by the auth boundary's broad handler
    and re-raised scrubbed. O(len(body)).
    """
    import json

    parsed = json.loads(body)
    if not isinstance(parsed, Mapping):
        raise DhanAuthError("Dhan auth response was not a JSON object")
    return parsed


class _UrllibTransport:  # pragma: no cover — real network
    """The production :class:`HttpTransport` backed by ``urllib.request`` (JSON over HTTPS)."""

    def request_json(
        self, method: str, url: str, *, headers: Mapping[str, str], timeout_s: float
    ) -> Mapping[str, Any]:
        import urllib.request

        req = urllib.request.Request(url, headers=dict(headers), method=method)  # noqa: S310  # https only (constant hosts)
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310  # nosec B310 — https only, constant Dhan hosts
            body = resp.read().decode("utf-8")
        return _parse_json_object(body)


def _real_transport() -> HttpTransport:  # pragma: no cover — real network
    """Build the production urllib transport (injected in tests)."""
    return _UrllibTransport()


def _http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status off a transport exception (``status`` / ``code``), else ``None``.

    A urllib ``HTTPError`` carries ``.code``; some clients carry ``.status``. A ``bool`` is
    rejected (an ``int`` subclass but never a status). A digit string is coerced. O(1).
    """
    for attr in ("status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
    return None


def _classify_transport_error(exc: Exception, secrets: tuple[str, ...]) -> DhanAuthError:
    """Map a caught transport exception to a typed, SCRUBBED :class:`DhanAuthError`.

    400/401/403 ⇒ terminal :class:`DhanAuthInvalidCredentials`; 429 ⇒ retryable
    :class:`DhanTokenMintThrottled`; anything else (5xx / network / timeout / no status) ⇒
    retryable :class:`DhanAuthTransportError`. The message is ``str(exc)`` — which the
    constructor SCRUBS (secrets masked + URL query redacted) — so the raw object's
    secret-bearing text never survives. O(1).
    """
    status = _http_status(exc)
    message = f"Dhan auth call failed (status={status}): {exc}"
    if status in _INVALID_CRED_STATUSES:
        return DhanAuthInvalidCredentials(message, secrets=secrets)
    if status == _THROTTLE_STATUS:
        return DhanTokenMintThrottled(message, secrets=secrets)
    return DhanAuthTransportError(message, secrets=secrets)


def _extract_token(payload: Mapping[str, Any]) -> str | None:
    """Extract the access token from a mint/renew response (shape UNVERIFIED — Phase-0).

    Searches the known top-level keys, then a nested ``data`` object. Returns the non-blank
    token or ``None`` (a missing token is an anomalous response the caller raises on). O(1).
    """
    for source in (
        payload,
        payload.get("data") if isinstance(payload.get("data"), Mapping) else {},
    ):
        for key in _TOKEN_KEYS:
            value = source.get(key)  # type: ignore[union-attr]
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _issuance_backoff(attempt: int) -> float:
    """Bounded exponential back-off (seconds) before retry *attempt* + 1. O(1)."""
    return min(
        DHAN_TOKEN_ISSUANCE_RETRY_BASE_S * (2.0**attempt),
        DHAN_TOKEN_ISSUANCE_RETRY_CAP_S,
    )


def _build_mint_url(creds: DhanCredentials, totp: str) -> str:
    """Build the headless mint URL with the secret PIN + TOTP in the query string.

    The URL is passed to the transport but NEVER logged / audited raw — every exception off it
    is scrubbed (its query redacted) at :class:`DhanAuthError` construction. O(1).
    """
    query = urlencode({"dhanClientId": creds.client_id, "pin": creds.pin, "totp": totp})
    return f"{DHAN_AUTH_MINT_URL}?{query}"


def mint_access_token(
    creds: DhanCredentials,
    *,
    transport: HttpTransport | None = None,
    totp_minter: Callable[[str], str] = _real_totp,
    clock: Callable[[], datetime] = _now_utc,
    sleeper: Callable[[float], None] = time.sleep,
) -> MintedToken:
    """Mint a fresh Dhan access token via the headless PIN+TOTP flow.

    A TERMINAL invalid-credential response (400/401/403) is raised with ZERO retry (the
    transport is called EXACTLY once — a retry would re-send bad credentials and risk a
    lockout; call-count is mutant-pinned). A throttle (429) or transient transport failure
    (5xx / network / timeout) is retried with a bounded back-off (the TOTP is recomputed each
    attempt so a code that rolled over is fresh), then the typed retryable error is raised.
    EVERY transport exception is caught here and re-raised scrubbed with ``from None`` — the
    raw object never escapes.

    Parameters
    ----------
    creds
        The Dhan credential triple (from AWS SSM).
    transport
        Injected :class:`HttpTransport` (``None`` builds the real urllib shim,
        ``# pragma: no cover``).
    totp_minter, clock, sleeper
        Injected side effects (defaults wrap real pyotp / UTC clock / ``time.sleep``).

    Returns
    -------
    MintedToken
        ``source="mint"``, stamped with OUR mint instant.

    Raises
    ------
    DhanAuthInvalidCredentials
        Terminal — the credentials were rejected (no retry).
    DhanTokenMintThrottled | DhanAuthTransportError
        After the bounded retry budget is exhausted (retryable — the caller re-pulls later).
    DhanAuthError
        A 2xx response carrying no access token (an anomalous / precondition failure).
    """
    active_transport = transport if transport is not None else _real_transport()
    attempts = max(1, DHAN_TOKEN_ISSUANCE_RETRY_ATTEMPTS)
    for attempt in range(attempts):
        totp = totp_minter(creds.totp_secret)
        secrets = (creds.client_id, creds.pin, creds.totp_secret, totp)
        url = _build_mint_url(creds, totp)
        # The raw transport object is classified INSIDE the handler but the typed error is
        # RAISED OUTSIDE it — so the raw urllib object (whose repr embeds the secret-bearing
        # mint URL) is never retained on the typed error's ``__context__`` either (not just
        # ``__cause__``). Secret safety is paramount: the raw object never escapes the module.
        typed_error: DhanAuthError | None = None
        payload: Mapping[str, Any] = {}
        try:
            payload = active_transport.request_json(
                "POST", url, headers={}, timeout_s=DHAN_AUTH_HTTP_TIMEOUT_S
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught — classified + scrubbed; raw never escapes
            typed_error = _classify_transport_error(exc, secrets)
        if typed_error is not None:
            if not is_retryable_auth(typed_error) or attempt == attempts - 1:
                _LOG.error(
                    "dhan.token.mint_failed",
                    attempt=attempt + 1,
                    retryable=is_retryable_auth(typed_error),
                    error_type=type(typed_error).__name__,
                )
                raise typed_error from None
            _LOG.warning(
                "dhan.token.mint_retry", attempt=attempt + 1, error_type=type(typed_error).__name__
            )
            sleeper(_issuance_backoff(attempt))
            continue
        token = _extract_token(payload)
        if token is None:
            raise DhanAuthError(
                "Dhan mint returned a 2xx response with no access token", secrets=secrets
            ) from None
        _LOG.info("dhan.token.mint", source="mint")
        return MintedToken(token=token, minted_at_utc=clock(), source="mint")
    # Unreachable: the loop always returns or raises on the final attempt (attempts >= 1).
    raise AssertionError("unreachable: mint loop always returns or raises")  # pragma: no cover


def renew_access_token(
    token: str,
    client_id: str,
    *,
    transport: HttpTransport | None = None,
    clock: Callable[[], datetime] = _now_utc,
) -> MintedToken:
    """Renew an ACTIVE Dhan access token via ``GET .../v2/RenewToken`` (headers only).

    No query secrets — the token + client id ride the ``access-token`` / ``dhanClientId``
    headers. ANY failure (a transport error, or a 2xx with no token) raises a
    :class:`DhanTokenRenewError`; the caller treats renew as an OPTIMIZATION and falls through
    to a fresh mint, so renew is never load-bearing. The raw transport object never escapes
    (scrubbed at construction, ``from None``).

    Returns
    -------
    MintedToken
        ``source="renew"``, stamped with OUR renew instant.
    """
    active_transport = transport if transport is not None else _real_transport()
    url = f"{DHAN_API_BASE}{DHAN_RENEW_PATH}"
    headers = {DHAN_TOKEN_HEADER: token, DHAN_CLIENT_ID_HEADER: client_id}
    # Classify inside the handler, RAISE outside it — the raw transport object is never
    # retained on ``__context__`` (secret hygiene, see ``mint_access_token``).
    renew_error: DhanTokenRenewError | None = None
    payload: Mapping[str, Any] = {}
    try:
        payload = active_transport.request_json(
            "GET", url, headers=headers, timeout_s=DHAN_AUTH_HTTP_TIMEOUT_S
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — scrubbed; raw never escapes
        renew_error = DhanTokenRenewError(
            f"Dhan RenewToken failed: {exc}", secrets=(token, client_id)
        )
    if renew_error is not None:
        raise renew_error from None
    new_token = _extract_token(payload)
    if new_token is None:
        raise DhanTokenRenewError(
            "Dhan RenewToken returned no token", secrets=(token, client_id)
        ) from None
    _LOG.info("dhan.token.renew", source="renew")
    return MintedToken(token=new_token, minted_at_utc=clock(), source="renew")


__all__ = [
    "HttpTransport",
    "MintedToken",
    "mint_access_token",
    "renew_access_token",
]
