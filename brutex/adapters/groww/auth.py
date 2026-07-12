"""Groww auth — TOTP access-token issuance (the AUTOMATED unattended flow).

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5, PR-E). Groww's TOTP flow
(``docs/groww/api-reference/01-introduction.md`` §"TOTP Flow") mints an access
token from an API key + a TOTP code; the API-key+secret flow is REFUSED because
it "requires daily approval" (violates CLAUDE.md §11 AUTOMATED "zero clicks/day").

Both side-effecting calls — ``pyotp.TOTP(secret).now()`` and
``GrowwAPI.get_access_token(...)`` — are **injected callables**, so every branch
is unit-testable without ``pyotp`` / ``growwapi`` installed (the optional-extra
resilience, DEC-GROWW-DEPS-REINSTATE-001). The real SDK/pyotp default shims are
the only ``# pragma: no cover`` lines, mirroring the canonical adapter auth layout.

Secret hygiene: :class:`GrowwAuthError` is raised with a ``safe_message`` so
``str(exc)`` (which the audit log / dashboard persist) NEVER leaks the api_key or
TOTP secret. The ``safe_message`` now carries the SANITIZED vendor reason (the SDK
exception's text — typically ``Groww API Error 400: <displayMessage>``) with every
api_key / TOTP-secret / 6-digit-code occurrence scrubbed to ``***``
(:func:`_sanitize_reason`), so the operator finally sees WHY auth failed (wrong
TOTP secret, expired key, IP not whitelisted, …) instead of only a bare
"issuance failed" — DEC-GROWW-AUTH-ERROR-DETAIL-001. The same sanitized reason
is emitted on a ``groww.token.issuance_failed`` structlog event so it lands in
``brutex.log``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import structlog

from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwRateLimitError,
    GrowwTokenIssuanceThrottled,
    from_groww_exception,
)
from brutex.adapters.secret_scrub import scrub_secrets

_LOG = structlog.get_logger("brutex.adapters.groww.auth")

#: RETIRED env-var NAME for the Groww TOTP api key (DEC-AWS-ONLY-CREDS-001: nothing
#: reads it for auth — creds come from AWS SSM ``/tickvault/prod/groww/api-key``; the
#: constant survives only for the doctor's secret-redaction belt + back-compat exports).
ENV_API_KEY: Final[str] = "BRUTEX_GROWW_API_KEY"
#: RETIRED env-var NAME for the Groww TOTP shared secret (DEC-AWS-ONLY-CREDS-001: nothing
#: reads it for auth — creds come from AWS SSM ``/tickvault/prod/groww/totp-secret``).
ENV_TOTP_SECRET: Final[str] = "BRUTEX_GROWW_TOTP_SECRET"  # noqa: S105  # env var name, not a secret


@dataclass(frozen=True, slots=True)
class GrowwCredentials:
    """A Groww TOTP api key + its shared secret (held only to mint codes)."""

    api_key: str
    totp_secret: str


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """A freshly issued Groww access token (no documented expiry under TOTP)."""

    access_token: str


def _real_totp(secret: str) -> str:  # pragma: no cover — needs pyotp + a real secret
    """Default TOTP minter: ``pyotp.TOTP(secret).now()`` (lazy import).

    No inline ignore: the ``pyotp``/``growwapi`` dual-env import hygiene lives in the
    ``[[tool.mypy.overrides]]`` per-module table (DEC-MYPY-GROWWAPI-STUBS-001), so the
    import is clean BOTH with and without the optional runtime deps installed.
    """
    import pyotp

    return str(pyotp.TOTP(secret).now())


def _real_token_factory(api_key: str, totp: str) -> str:  # pragma: no cover — needs growwapi
    """Default token factory: ``GrowwAPI.get_access_token(...)`` (lazy import).

    No inline ignore — see :func:`_real_totp` (DEC-MYPY-GROWWAPI-STUBS-001).
    """
    from growwapi import GrowwAPI

    return str(GrowwAPI.get_access_token(api_key=api_key, totp=totp))


def _sanitize_reason(text: str, *secrets: str) -> str:
    """Scrub every *secrets* occurrence in *text* to ``***`` (secret hygiene).

    Used to make the vendor's failure reason VISIBLE without leaking the api_key,
    the TOTP shared secret, or the minted 6-digit TOTP code. DELEGATES to the COMMON
    :func:`brutex.adapters.secret_scrub.scrub_secrets` (DEC-AWS-SSM-SEAM-COMMON-001,
    MIGRATION.md #1754 — the scrubber moved verbatim so a second vendor shares it);
    this thin wrapper keeps the ``(text, *secrets)`` varargs signature byte-compatible
    for every existing call site (the Groww suites are the oracle).
    """
    return scrub_secrets(text, secrets)


def _vendor_status(exc: Exception) -> int | None:
    """Best-effort HTTP-ish status off an SDK exception (``code``), else ``None``.

    The Groww SDK's ``GrowwAPIException`` carries a ``code`` (e.g. ``400``, or the
    digit-string ``"503"``); a plain exception (``RuntimeError``) carries none. A
    ``bool`` is rejected (it is an ``int`` subclass but never a status). Only a
    genuine NUMERIC status survives — an int, or a digit-only string coerced to int
    (mirroring :func:`brutex.adapters.groww.errors._classify_generic`) — so an
    arbitrary vendor-controlled ``code`` string can NEVER reach the ``status`` log
    field unscrubbed (the secret surface stays narrow; ``reason`` is the only
    free-text channel and it is sanitized). Anything non-numeric returns ``None``.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.strip().isdigit():
        return int(code)
    return None


def _is_issuance_throttle(exc: Exception) -> bool:
    """True if *exc* is a token-issuance RATE-LIMIT (a throttle), not a terminal auth fail.

    Reuses the SINGLE source of truth :func:`~brutex.adapters.groww.errors.\
    from_groww_exception` (CLAUDE.md §11 COMMON) — so the SDK's
    ``GrowwAPIRateLimitException`` class name, an HTTP ``429`` ``code``, AND the live
    "Rate limit … exceeded" message-on-a-generic-exception shape are ALL recognised
    without re-listing the hints here. A throttle classifies to a
    :class:`~brutex.adapters.groww.errors.GrowwRateLimitError`; a wrong TOTP / expired
    key / un-whitelisted IP does not, so it stays a terminal :class:`GrowwAuthError`.
    """
    return isinstance(from_groww_exception(exc), GrowwRateLimitError)


def get_access_token(
    creds: GrowwCredentials,
    *,
    token_factory: Callable[[str, str], str] = _real_token_factory,
    totp_minter: Callable[[str], str] = _real_totp,
) -> TokenInfo:
    """Mint a TOTP code and issue a Groww access token.

    Both side effects are injected (defaults wrap the real pyotp/SDK calls). Any
    failure is wrapped as a :class:`GrowwAuthError` whose SANITIZED vendor reason
    is the ``safe_message`` (so the operator sees WHY — wrong TOTP secret, expired
    key, IP not whitelisted — not just "issuance failed") with every api_key /
    TOTP-secret / minted-code occurrence scrubbed to ``***`` so no secret can reach
    a log or the dashboard (DEC-GROWW-AUTH-ERROR-DETAIL-001). The same sanitized
    reason is also attached as :attr:`GrowwAuthError.detail` and emitted on a
    ``groww.token.issuance_failed`` structlog event (with the vendor status when
    present).

    A failure that is a token-ISSUANCE RATE-LIMIT (the vendor throttled the mint —
    :func:`_is_issuance_throttle`) is raised as a RETRYABLE
    :class:`~brutex.adapters.groww.errors.GrowwTokenIssuanceThrottled` (a
    :class:`GrowwAuthError` subclass) — classified BEFORE the generic terminal arm
    (hostile-review M2 fold) — so the cold-start mint can back off + reuse a cached
    token instead of failing the pull (DEC-GROWW-TOKEN-DISK-CACHE-001). The SAME
    sanitization + ``.detail`` + structlog contract applies, so no secret leaks through
    the throttle path either.
    """
    totp = totp_minter(creds.totp_secret)
    try:
        token = token_factory(creds.api_key, totp)
    except Exception as exc:
        reason = _sanitize_reason(str(exc), creds.api_key, creds.totp_secret, totp)
        status = _vendor_status(exc)
        # ORDER (M2 fold): classify the issuance THROTTLE arm FIRST so a vendor
        # rate-limit on the mint raises the RETRYABLE subclass, NOT the terminal
        # GrowwAuthError. Both arms keep the SAME sanitized reason + structlog event.
        if _is_issuance_throttle(exc):
            safe_message = f"Groww access-token issuance throttled: {reason}"
            _LOG.warning(
                "groww.token.issuance_throttled",
                reason=reason,
                status=status,
                error_type=type(exc).__name__,
            )
            throttle = GrowwTokenIssuanceThrottled(safe_message, safe_message=safe_message)
            throttle.detail = reason
            raise throttle from exc
        safe_message = f"Groww access-token issuance failed: {reason}"
        _LOG.error(
            "groww.token.issuance_failed",
            reason=reason,
            status=status,
            error_type=type(exc).__name__,
        )
        error = GrowwAuthError(safe_message, safe_message=safe_message)
        error.detail = reason
        raise error from exc
    return TokenInfo(access_token=token)


__all__ = [
    "ENV_API_KEY",
    "ENV_TOTP_SECRET",
    "GrowwCredentials",
    "TokenInfo",
    "get_access_token",
]
