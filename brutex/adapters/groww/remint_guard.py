"""Concurrency-safe one-shot auth re-mint around thread-safe Groww SDK calls.

The Groww SDK is concurrency-safe: every call is an independent module-level
``requests.get``/``requests.post`` with NO shared ``requests.Session``
(HARD-VERIFIED against ``growwapi`` 1.5.0 ``groww/client.py`` —
``_request_get``/``_request_post`` call ``requests.get``/``post`` directly,
each of which spins up + closes its own ephemeral session). So SDK calls need
NO serialization lock — N :class:`~brutex.orchestrator.parallel.PoolRunParallel`
workers can call the ONE shared client concurrently (true parallelism under the
shared rate bucket).

:class:`RemintGuard` therefore locks ONLY the one-shot token re-mint: a long
phase outlives one short-lived Groww access token (resets 6 AM IST,
``CLAUDE.md §6``), so on the FIRST auth error the guard refreshes the token +
rebinds the fetchers' SDK ONCE (latch-bounded), and every worker retries
against the fresh client.

This REPLACES the prior whole-call lock (DEC-UNIVERSE-PULL-PARALLEL-001
finding #6) that was added on the now-disproven assumption the shared SDK was
not thread-safe — that lock pinned the pull to ~3 r/s sequential; removing it
restores the intended N-way parallelism (DEC-UNIVERSE-PULL-CONCURRENCY-001).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Final

#: Substring fingerprints of a Groww auth failure (token expired / invalid),
#: matched case-insensitively against ``str(exc)``. Sourced from the broker's
#: documented auth-error wording (``CLAUDE.md §6``).
AUTH_ERROR_FINGERPRINTS: Final[tuple[str, ...]] = (
    "authentication failed",
    "token has either expired",
    "invalid token",
    "unauthorized",
    "401",
)


def is_auth_error(
    exc: BaseException, fingerprints: tuple[str, ...] = AUTH_ERROR_FINGERPRINTS
) -> bool:
    """Return whether *exc*'s message matches a known Groww auth-failure fingerprint.

    Parameters
    ----------
    exc
        The (already neutral-translated) exception raised by an SDK call.
    fingerprints
        Lower-case substrings that mark an auth failure (default
        :data:`AUTH_ERROR_FINGERPRINTS`).

    Returns
    -------
    bool
        ``True`` if any fingerprint occurs in ``str(exc).lower()``.
    """
    text = str(exc).lower()
    return any(fingerprint in text for fingerprint in fingerprints)


class RemintGuard:
    """Run thread-safe SDK calls concurrently; re-mint the token ONCE on auth failure.

    Parameters
    ----------
    rebind
        Zero-arg callback that refreshes auth + rebinds every fetcher's SDK
        (production: ``auth.refresh()`` then set each fetcher's ``.sdk``).
        Invoked at most once per guard instance (latch-bounded).
    auth_error
        Predicate classifying an exception as an auth failure (default
        :func:`is_auth_error`).
    """

    __slots__ = ("_auth_error", "_lock", "_rebind", "_reminted")

    def __init__(
        self,
        rebind: Callable[[], None],
        *,
        auth_error: Callable[[BaseException], bool] = is_auth_error,
    ) -> None:
        self._rebind = rebind
        self._auth_error = auth_error
        self._lock = threading.Lock()
        self._reminted = False

    def call[T](self, fn: Callable[[], T]) -> T:
        """Run *fn* with NO lock (the SDK is thread-safe); re-mint once + retry on auth error.

        Concurrent workers all run *fn* in parallel. On an auth failure the
        FIRST worker to take the lock re-mints (``rebind``) and sets the latch;
        others see the latch set, skip the re-mint, and retry against the
        already-refreshed client. A non-auth error propagates immediately; an
        auth error still failing after the single re-mint propagates too (the
        caller records it failed and the O(1) skip retries it next run).
        """
        try:
            return fn()
        except Exception as exc:
            if not self._auth_error(exc):
                raise
            with self._lock:
                if not self._reminted:
                    self._rebind()
                    self._reminted = True
            return fn()


__all__ = ["AUTH_ERROR_FINGERPRINTS", "RemintGuard", "is_auth_error"]
