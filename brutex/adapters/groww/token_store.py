"""Daily-token backend abstraction — local disk (default) OR shared read-only AWS (opt-in).

DEC-TOKEN-BROKER-AWS-001 (operator's FINAL design, 2026-07-01). The daily Groww access
token is minted ONCE per session and cached so the 3 pipeline phases + restarts reuse it
(DEC-GROWW-TOKEN-DISK-CACHE-001, #1041). That cache is PER-MACHINE, so two apps on ONE
Groww account (BruteX + TickVault) mint independently — and the vendor's single-active-
token behaviour means a mint by one silently revokes the other's live token. The operator's
fix: a dedicated Lambda mints the daily token into ONE AWS Systems Manager Parameter Store
SecureString, and BOTH apps READ it — BruteX strictly read-only. This module makes the
backend PLUGGABLE so BruteX can consume that shared token:

* :class:`TokenStore` — the two-method backend contract (``load`` / ``store``), which the
  EXISTING disk functions (:func:`~brutex.adapters.groww.token_cache.load_cached_token` /
  :func:`~brutex.adapters.groww.token_cache.store_token`) already satisfy. ``load`` is
  BEST-EFFORT and NEVER raises — the mint spine's ``_safe_cache_load`` contract
  (:mod:`brutex.adapters.groww.reauth`).
* :class:`DiskTokenStore` — the DEFAULT: a trivial object wrapping the disk functions,
  byte-identical to #1041 (a local run is unchanged, imports ZERO AWS code).
* :func:`resolve_token_store` — reads ``BRUTEX_TOKEN_BROKER_READONLY`` (via
  :func:`~brutex.adapters.groww.constants.resolve_token_broker_readonly`) and returns the
  read-only Parameter-Store store when the flag is set AND boto3 is present, else the disk
  store (fail-safe to local on unset/blank/bad/missing-boto3, mirroring
  :func:`~brutex.adapters.groww.constants.resolve_pool_size`).

The result is MEMOISED per env-config toggle (DEC-TOKEN-BROKER-AWS-001): a single pipeline
builds THREE refreshers (spot + metadata + F&O), and building a fresh boto3 client per
refresher would (a) triple the credential-chain resolution cost and (b) risk a split-brain
(phase 1 AWS-backed, phase 2 disk-backed if a client build transiently fails). The lazy
singleton makes all three share ONE client + ONE backend decision.
:func:`reset_token_store_cache` clears it (tests + an explicit re-resolve).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import structlog

from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_BACKEND_AWS,
    GROWW_TOKEN_BACKEND_LOCAL,
    resolve_token_broker_readonly,
)
from brutex.adapters.groww.token_cache import (
    CachedToken,
    load_cached_token,
    store_token,
)

_LOG = structlog.get_logger("brutex.adapters.groww.token_store")


@runtime_checkable
class TokenStore(Protocol):
    """A daily-token backend. ``load`` is BEST-EFFORT and NEVER raises.

    An implementation reuses the backend-agnostic session anchoring
    (:func:`~brutex.adapters.groww.token_cache.current_session`) so a token is valid only
    for ITS 06:00-IST session. ``store`` persists a fresh mint on the LOCAL backend; on the
    read-only AWS backend it fails LOUD (BruteX never mints the shared token).
    """

    def load(self, creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
        """Return the cached same-session token for *creds*, else ``None`` (NEVER raises)."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def store(self, creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
        """Persist *token* for *creds* (local: best-effort ``bool``; read-only AWS: raises)."""
        raise NotImplementedError  # structural Protocol stub — never executed


class DiskTokenStore:
    """The DEFAULT backend — the local on-disk cache (#1041), byte-identical to today.

    A thin object adapter over the existing free functions so the selection mechanism
    can treat local + AWS uniformly. It imports NO AWS code, so a purely-local run never
    touches boto3.
    """

    def load(self, creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
        """Delegate to :func:`~brutex.adapters.groww.token_cache.load_cached_token`."""
        return load_cached_token(creds, now_utc=now_utc)

    def store(self, creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
        """Delegate to :func:`~brutex.adapters.groww.token_cache.store_token`."""
        return store_token(creds, token, now_utc=now_utc)


@dataclass(slots=True)
class _StoreCache:
    """The lazy per-process singleton holder (DEC-TOKEN-BROKER-AWS-001).

    Holds the resolved store + the backend kind it was built for, so all three refreshers
    of one pull share ONE client + ONE backend decision (no per-refresher client rebuild,
    no split-brain). A mutable-instance holder keeps the state behind a lock WITHOUT a
    ``global`` statement.
    """

    kind: str | None = None
    store: TokenStore | None = None


_STORE_LOCK = threading.Lock()
_CACHED_STORE = _StoreCache()


def reset_token_store_cache() -> None:
    """Clear the memoised :class:`TokenStore` so the next resolve re-reads the env.

    Used by tests (each permutation sets a different ``BRUTEX_TOKEN_BROKER_READONLY`` / AWS
    config) and available to an operator that rotates the AWS config mid-process.
    """
    with _STORE_LOCK:
        _CACHED_STORE.kind = None
        _CACHED_STORE.store = None


def resolve_token_store() -> TokenStore:
    """Return the selected daily-token backend, MEMOISED per env-config kind.

    ``BRUTEX_TOKEN_BROKER_READONLY`` truthy (via
    :func:`~brutex.adapters.groww.constants.resolve_token_broker_readonly`) with boto3
    present selects the shared read-only Parameter-Store store; ANY other case — flag
    unset / blank / falsey / boto3-missing — resolves to the local :class:`DiskTokenStore`
    (fail-safe, CLAUDE.md §2 Rule 1). The decision is cached per kind so all three
    refreshers in one pull share it; :func:`reset_token_store_cache` clears it.
    """
    kind = GROWW_TOKEN_BACKEND_AWS if resolve_token_broker_readonly() else GROWW_TOKEN_BACKEND_LOCAL
    with _STORE_LOCK:
        cached = _CACHED_STORE.store
        if _CACHED_STORE.kind == kind and cached is not None:
            return cached
        store = _build_store(kind)
        _CACHED_STORE.kind = kind
        _CACHED_STORE.store = store
        return store


def _build_store(kind: str) -> TokenStore:
    """Build the backend for *kind*, degrading an unavailable AWS backend to local.

    Split out so the caching in :func:`resolve_token_store` stays trivial. An ``aws``
    kind whose read-only store cannot be built (boto3 missing / any construction error)
    logs a loud reason and returns a :class:`DiskTokenStore`, so the fail-safe-to-local
    decision happens ONCE at build time, never mid-pull.
    """
    if kind == GROWW_TOKEN_BACKEND_AWS:
        aws_store = _resolve_aws_store_or_none()
        if aws_store is not None:
            _LOG.info("groww.token.backend_selected", backend="aws-readonly")
            return aws_store
        # Loud fail-safe — the operator asked for the read-only broker but boto3/AWS is
        # unavailable; the pull still runs on the local cache + local mint until fixed.
        _LOG.warning("groww.token.aws_backend_unavailable_fallback_local")
    return DiskTokenStore()


def _resolve_aws_store_or_none() -> TokenStore | None:
    """Build the read-only Parameter-Store store, or ``None`` when it cannot be built.

    Imports the AWS backend LAZILY (so a local run never imports boto3) and swallows any
    construction error (missing extra / bad region) → ``None`` so the caller degrades to
    local. Kept tiny + fully covered by the fake-backed tests; the real boto3 client build
    inside ``build_readonly_token_store`` is ``# pragma: no cover``.
    """
    try:
        from brutex.adapters.groww.token_store_aws import build_readonly_token_store

        return build_readonly_token_store()
    except Exception as exc:  # pylint: disable=broad-exception-caught — degrade ANY build failure to local
        _LOG.warning("groww.token.aws_store_build_failed", error_type=type(exc).__name__)
        return None


__all__ = [
    "DiskTokenStore",
    "TokenStore",
    "reset_token_store_cache",
    "resolve_token_store",
]
