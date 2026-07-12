"""DhanHQ v2 auth error hierarchy — SCRUB-AT-CONSTRUCTION (secret safety is paramount).

DEC-DHAN-AUTH-001. Every auth error is a :class:`DhanAuthError`, whose constructor
UNCONDITIONALLY scrubs its message: it masks every provided secret (``scrub_secrets``) AND
redacts the query string of every URL it finds (``redact_url_query``). This makes it
STRUCTURALLY IMPOSSIBLE to construct a leaky auth error — a urllib ``HTTPError.__str__``
embeds the secret-bearing mint URL (``…/generateAccessToken?dhanClientId=…&pin=…&totp=…``),
so the raw transport object is caught at the auth boundary and re-raised as one of these
types with the secrets already gone. ``str(exc)`` / ``repr(exc)`` / ``exc.safe_message`` all
carry only the scrubbed text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from brutex.adapters.secret_scrub import redact_url_query, scrub_secrets

#: Matches a URL token in free-text (an exception message). Trailing punctuation is stripped
#: before redaction so ``…?pin=1: 400`` redacts to ``…?[REDACTED]: 400``.
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
#: Trailing chars that are message punctuation, never part of the URL.
_URL_TRAILING = ")]},.;:'\">"


def _redact_message(message: str, secrets: Iterable[str]) -> str:
    """Scrub every secret AND redact every URL query in *message* (the constructor core).

    Runs :func:`~brutex.adapters.secret_scrub.scrub_secrets` FIRST (so a secret that appears
    outside a URL is masked), then redacts the query string of every URL substring via
    :func:`~brutex.adapters.secret_scrub.redact_url_query` (defense-in-depth for a
    ``HTTPError`` that embedded the mint URL). O(len(message)).
    """
    scrubbed = scrub_secrets(message, secrets)

    def _sub(match: re.Match[str]) -> str:
        raw = match.group(0)
        trail = ""
        while raw and raw[-1] in _URL_TRAILING:
            trail = raw[-1] + trail
            raw = raw[:-1]
        return redact_url_query(raw) + trail

    return _URL_RE.sub(_sub, scrubbed)


class DhanError(Exception):
    """Base for every DhanHQ adapter error (auth + future data-plane)."""


class DhanAuthError(DhanError):
    """An auth-plane failure whose message is SCRUBBED at construction (secret hygiene).

    The constructor masks every *secrets* occurrence AND redacts every embedded URL's query,
    so no subclass can ever carry a raw credential or a secret-bearing URL. ``safe_message``
    is the scrubbed text (identical to ``str(self)``), kept as an attribute so an audit /
    dashboard persist path reads the sanctioned field explicitly.

    Parameters
    ----------
    message
        The raw message (may contain secrets / a mint URL) — scrubbed immediately.
    secrets
        The secret values to mask (client id / pin / totp secret / minted code). A blank
        secret is skipped by the scrubber. Defaults to none (the URL-query redaction still
        runs — the belt that catches an HTTPError even when no secrets were passed).
    """

    def __init__(self, message: object, *, secrets: Iterable[str] = ()) -> None:
        safe = _redact_message(str(message), tuple(secrets))
        super().__init__(safe)
        self.safe_message: str = safe


class DhanAuthInvalidCredentials(DhanAuthError):
    """TERMINAL — the credentials themselves are rejected (wrong PIN/TOTP/client id).

    NEVER retried (a retry would re-send bad credentials + risk a lockout) and LATCHES the
    reauth lockdown on the FIRST occurrence (DEC-DHAN-AUTH-001).
    """


class DhanTokenMintThrottled(DhanAuthError):
    """RETRYABLE — the vendor throttled the mint (rate-limit / 429). Back off + retry."""


class DhanAuthTransportError(DhanAuthError):
    """RETRYABLE — a transient transport failure (5xx / network / timeout) on an auth call."""


class DhanTokenRenewError(DhanAuthError):
    """The RenewToken optimization failed — the caller FALLS THROUGH to a fresh mint.

    Renew is never load-bearing (it only shortcuts a mint), so a renew failure is neither a
    terminal auth failure nor a retryable-in-place error — it is handled by falling through.
    """


class DhanTokenStoreError(DhanAuthError):
    """Persisting the minted token to the SecureString failed (PutParameter raised)."""


class DhanAuthLockdown(DhanAuthError):
    """LATCHED TERMINAL — auth is locked down after an invalid credential / repeated failure.

    Once latched, every later auth call fails fast with ZERO HTTP until the process restarts
    (no env escape hatch — recovery is a deliberate operator action per the runbook). One
    CRITICAL Telegram alert fires on the latch.
    """


def is_retryable_auth(exc: Exception) -> bool:
    """True iff *exc* is a TRANSIENT auth failure worth retrying in place.

    Retryable: a mint THROTTLE (:class:`DhanTokenMintThrottled`) or a transient TRANSPORT
    error (:class:`DhanAuthTransportError`). NOT retryable: a terminal invalid credential
    (:class:`DhanAuthInvalidCredentials`), a lockdown (:class:`DhanAuthLockdown`), a store
    failure (:class:`DhanTokenStoreError`), or a renew failure (:class:`DhanTokenRenewError`
    — handled by falling through to a mint, not by an in-place retry). O(1).
    """
    return isinstance(exc, (DhanTokenMintThrottled, DhanAuthTransportError))


__all__ = [
    "DhanAuthError",
    "DhanAuthInvalidCredentials",
    "DhanAuthLockdown",
    "DhanAuthTransportError",
    "DhanError",
    "DhanTokenMintThrottled",
    "DhanTokenRenewError",
    "DhanTokenStoreError",
    "is_retryable_auth",
]
