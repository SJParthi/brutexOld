"""Daily-token backend — the SHARED read-only AWS Parameter-Store token, the SOLE source.

DEC-AWS-ONLY-CREDS-001 (operator's FINAL design, 2026-07-01, stacked on
DEC-TOKEN-BROKER-AWS-001). The daily Groww access token is minted by a dedicated Lambda
into ONE AWS Systems Manager Parameter Store SecureString (``/tickvault/prod/groww/\
access-token``, region ``ap-south-1``), and BOTH apps on the one Groww account (BruteX +
TickVault) READ it — BruteX strictly read-only. AWS is now the DEFAULT AND ONLY token
source: the local on-disk mint/cache path (the old ``DiskTokenStore``, #1041) AND the
``BRUTEX_TOKEN_BROKER_READONLY`` opt-in flag were REMOVED, so there is no env / file /
local-mint fallback (Golden Rule #1: a token comes from AWS or the pull fails LOUD).

* :class:`TokenStore` — the two-method backend contract (``load`` / ``store``) the mint
  spine (:mod:`brutex.adapters.groww.reauth`) calls. ``load`` is BEST-EFFORT and NEVER
  raises; ``store`` on the read-only AWS backend fails LOUD (BruteX never mints the shared
  token — the Lambda owns minting).
* :func:`resolve_token_store` — builds the read-only Parameter-Store store. It is MANDATORY
  and FAIL-FAST: a missing boto3 extra / an SSM-client build failure raises a clear
  :class:`~brutex.adapters.groww.errors.GrowwAuthError` naming the deploy pointer — it NEVER
  falls back to a local mint.

The result is MEMOISED (DEC-TOKEN-BROKER-AWS-001): a single pipeline builds THREE refreshers
(spot + metadata + F&O), and building a fresh boto3 client per refresher would triple the
credential-chain resolution cost. The lazy singleton makes all three share ONE client.
:func:`reset_token_store_cache` clears it (tests + an explicit re-resolve).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import structlog

from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.errors import GrowwAuthError
from brutex.adapters.groww.token_cache import CachedToken

_LOG = structlog.get_logger("brutex.adapters.groww.token_store")

#: How to fix an AWS token-backend build failure, appended to the raised error. Names the
#: fixed access-token path + the deploy pointer; contains NO secret values.
_AWS_BACKEND_HOW_TO_FIX: str = (
    "no Groww token backend in AWS Parameter Store "
    "(/tickvault/prod/groww/access-token) — deploy the minter "
    "(deploy/aws_token_minter/deploy.sh) first; BruteX reads the daily token ONLY from AWS "
    "(no env-var / local-file / local-mint fallback)"
)


@runtime_checkable
class TokenStore(Protocol):
    """A daily-token backend. ``load`` is BEST-EFFORT and NEVER raises.

    An implementation reuses the backend-agnostic session anchoring
    (:func:`~brutex.adapters.groww.token_cache.current_session`) so a token is valid only
    for ITS 06:00-IST session. ``store`` on the read-only AWS backend fails LOUD (BruteX
    never mints the shared token).
    """

    @property
    def readonly(self) -> bool:
        """``True`` iff this backend NEVER mints — a read-only shared-token consumer.

        A read-only backend (the shared-AWS Parameter-Store store — the SOLE source under
        DEC-AWS-ONLY-CREDS-001) drives the mid-run reauth to RE-READ the Lambda-minted token
        instead of minting locally (DEC-BROKER-READONLY-REAUTH-REREAD-001). Read by
        :func:`~brutex.adapters.groww.live._resolve_token_cache_hooks` to decide whether to
        wire the read-only re-read hook into the refresher.
        """
        raise NotImplementedError  # structural Protocol stub — never executed

    def load(self, creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
        """Return the cached same-session token for *creds*, else ``None`` (NEVER raises)."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def store(self, creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
        """Persist *token* for *creds* (read-only AWS: raises — BruteX never mints)."""
        raise NotImplementedError  # structural Protocol stub — never executed


@dataclass(slots=True)
class _StoreCache:
    """The lazy per-process singleton holder (DEC-TOKEN-BROKER-AWS-001).

    Holds the resolved store so all three refreshers of one pull share ONE client (no
    per-refresher client rebuild). A mutable-instance holder keeps the state behind a lock
    WITHOUT a ``global`` statement.
    """

    store: TokenStore | None = None


_STORE_LOCK = threading.Lock()
_CACHED_STORE = _StoreCache()


def reset_token_store_cache() -> None:
    """Clear the memoised :class:`TokenStore` so the next resolve rebuilds it.

    Used by tests (each permutation injects a different AWS config) and available to an
    operator that rotates the AWS config mid-process.
    """
    with _STORE_LOCK:
        _CACHED_STORE.store = None


def resolve_token_store() -> TokenStore:
    """Return the shared read-only AWS daily-token backend, MEMOISED — the SOLE source.

    AWS is the DEFAULT AND ONLY token source (DEC-AWS-ONLY-CREDS-001): there is no
    ``BRUTEX_TOKEN_BROKER_READONLY`` gate and no local-mint fallback. This is FAIL-FAST — a
    missing boto3 extra / an SSM-client build failure raises
    :class:`~brutex.adapters.groww.errors.GrowwAuthError` naming the deploy pointer. The
    store is cached so all three refreshers in one pull share ONE client;
    :func:`reset_token_store_cache` clears it.
    """
    with _STORE_LOCK:
        cached = _CACHED_STORE.store
        if cached is not None:
            return cached
        store = _build_readonly_store_or_raise()
        _CACHED_STORE.store = store
        return store


def _build_readonly_store_or_raise() -> TokenStore:
    """Build the read-only Parameter-Store store, raising a clear error when it cannot.

    Imports the AWS backend LAZILY. Any construction error (missing boto3 extra / bad
    region / credential-chain failure) is FAIL-FAST: it raises
    :class:`~brutex.adapters.groww.errors.GrowwAuthError` naming the deploy pointer — never a
    silent fall back to a local mint (DEC-AWS-ONLY-CREDS-001). The real boto3 client build
    inside ``build_readonly_token_store`` is ``# pragma: no cover``; the raise-on-failure
    branch is driven by a fake in tests.
    """
    try:
        from brutex.adapters.groww.token_store_aws import build_readonly_token_store

        store = build_readonly_token_store()
    except Exception as exc:  # pylint: disable=broad-exception-caught — any build failure is fail-fast
        _LOG.error("groww.token.aws_store_build_failed", error_type=type(exc).__name__)
        raise GrowwAuthError(
            f"could not build the AWS token backend "
            f"({type(exc).__name__}); {_AWS_BACKEND_HOW_TO_FIX}"
        ) from exc
    _LOG.info("groww.token.backend_selected", backend="aws-readonly")
    return store


__all__ = [
    "TokenStore",
    "reset_token_store_cache",
    "resolve_token_store",
]
