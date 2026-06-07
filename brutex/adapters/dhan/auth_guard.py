"""Dhan auth fail-fast guard — a thread-safe one-way "token is dead" trip switch.

When the operator's 24h Dhan web token expires mid-pull, EVERY remaining
``(symbol, interval, window)`` would otherwise fire its own doomed 401 (a 401
is non-retryable, so each window quarantines one-by-one). On a real ``make run``
that was ~800 wasted calls + ~10 minutes of WARNING spam with NO single loud
"your token died" signal (DEC-DHAN-AUTH-FAILFAST-RENEW-001).

:class:`AuthGuard` is the shared one-way latch the ingest spine consults: the
FIRST auth failure :meth:`trip`\\ s it (recording the cause), and every window
worker checks :meth:`tripped` BEFORE its vendor call — so once the token is
known dead the rest of the pull drains instantly with zero further doomed calls.

**Honest envelope (Golden Rule #10):** the guard SHORT-CIRCUITS not-yet-dispatched
windows; it does NOT forcibly cancel threads already mid-flight in the pool —
those finish their current call, then the guard check at the next window start
drains them. It also cannot REVIVE a dead token (RenewToken only refreshes an
ACTIVE token); the guard's job is purely to stop the flood + surface ONE alert.

All operations are O(1) and thread-safe (a single :class:`threading.Lock`). No
global state — one guard is created per pull and threaded through explicitly.
"""

from __future__ import annotations

import threading


class AuthGuard:
    """A thread-safe one-way latch tripped the first time the Dhan token dies.

    Shared across every window worker of a pull. The first auth failure trips it
    (keeping that first cause); subsequent failures are idempotent no-ops. Once
    tripped, workers that check :meth:`tripped` before their vendor call
    short-circuit, so the doomed-call flood stops (DEC-DHAN-AUTH-FAILFAST-RENEW-001).

    Thread-safety
    -------------
    A single :class:`threading.Lock` guards the ``_tripped`` flag + ``_cause``
    string, so concurrent :meth:`trip` calls from racing workers never tear the
    state and exactly ONE cause is retained (the first writer wins). Every method
    is O(1).
    """

    __slots__ = ("_cause", "_lock", "_tripped")

    def __init__(self) -> None:
        """Build an un-tripped guard (no cause yet)."""
        self._lock = threading.Lock()
        self._tripped = False
        self._cause: str | None = None

    def trip(self, cause: str) -> None:
        """Trip the guard, recording *cause* — idempotent (only the FIRST cause wins).

        Called by a window worker the first time it sees a dead-token error
        (:func:`~brutex.adapters.dhan.errors.is_auth_failure`). If already tripped
        this is a no-op and the original cause is kept, so a later worker's
        (identical-root-cause) failure never overwrites the first observed one.

        Parameters
        ----------
        cause
            A human-readable description of the auth failure (typically
            ``str(exc)`` of the originating :class:`DhanError`).
        """
        with self._lock:
            if not self._tripped:
                self._tripped = True
                self._cause = cause

    def tripped(self) -> bool:
        """Return ``True`` once the guard has been tripped — O(1), thread-safe."""
        with self._lock:
            return self._tripped

    def cause(self) -> str | None:
        """Return the first recorded trip cause, or ``None`` if not tripped — O(1)."""
        with self._lock:
            return self._cause


__all__ = ["AuthGuard"]
