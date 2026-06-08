"""Dhan token holder — a thread-safe mutable cell for the live access token.

A 24h Dhan web access token is renewed *in place* by the background keepalive
(:mod:`brutex.adapters.dhan.token_keepalive`, DEC-DHAN-TOKEN-KEEPALIVE-001).
When ``RenewToken`` issues a fresh token it **invalidates the old one**, so a
client still holding the previous string would 401 on its next call. The fix is
ONE shared mutable cell: every live :class:`~brutex.adapters.dhan.client.DhanClient`
reads the token through ``holder.get`` (its ``token_provider`` seam) rather than
a frozen copy, so a mid-life renew propagates to BOTH in-flight window workers
AND the next pull the instant :meth:`set` runs.

The holder is the SINGLE source of the current token at runtime (CLAUDE.md §11
COMMON). It owns no global state — exactly one holder is created per app
lifetime and threaded through explicitly. Every operation is O(1) under one
:class:`threading.Lock` so concurrent reads (every worker's ``_headers()``) and
the single keepalive writer never tear.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class TokenHolder:
    """A thread-safe mutable holder for the current Dhan access token.

    Shared by every live client of a pull and written by the background
    keepalive on each renew. The lock makes a concurrent :meth:`get` (a worker
    building its request headers) and :meth:`set` (the keepalive swapping in the
    fresh token) atomic, so a reader always sees a whole token string — never a
    torn intermediate (DEC-DHAN-TOKEN-KEEPALIVE-001).

    WRITE-THROUGH (DEC-DHAN-TOKEN-SINGLE-SOURCE-001). An optional ``on_set`` hook
    is invoked after every :meth:`set`, so the holder is a write-through cache over
    a SINGLE durable store: the keepalive's renewal (which ROTATES — the new token
    invalidates the old one) and the operator's mid-session credential save both go
    through :meth:`set`, so both persist the new token to the store. Without it, a
    renewed token lived ONLY in memory and a restart re-loaded the now-invalidated
    pre-rotation token (and a saved token never reached the in-memory holder at all)
    — two ways the live token and the persisted token drifted apart. The hook is
    called OUTSIDE the lock (it may do file I/O) and MUST NOT raise (the wiring
    swallows + logs its own errors), so a persist failure can never crash a renewal
    or tear the in-memory token.

    Parameters
    ----------
    token
        The initial access token (the resolved 24h web token at app start).
    on_set
        Optional write-through persist hook ``(token) -> None`` invoked after each
        :meth:`set`. ``None`` (the default) keeps the in-memory-only behaviour for
        callers that do not persist (tests / the CLI without a keepalive).
    """

    __slots__ = ("_lock", "_on_set", "_token")

    def __init__(self, token: str, *, on_set: Callable[[str], None] | None = None) -> None:
        """Build a holder seeded with *token* (optionally write-through via *on_set*)."""
        self._lock = threading.Lock()
        self._token = token
        self._on_set = on_set

    def get(self) -> str:
        """Return the current access token — O(1), thread-safe.

        This is the callable wired into :attr:`DhanClient.token_provider`, so
        every physical POST reads the LIVE token (a renew propagates instantly).

        Returns
        -------
        str
            The current access token string.
        """
        with self._lock:
            return self._token

    def set(self, token: str) -> None:
        """Replace the held token with *token* — O(1), thread-safe, write-through.

        Called by the keepalive after a successful ``RenewToken`` AND by the
        mid-session credential save; the next :meth:`get` (this pull's next call,
        or an in-flight worker's next call) immediately uses the fresh token. The
        in-memory swap happens under the lock; the optional ``on_set`` persist hook
        is then invoked OUTSIDE the lock (it may do file I/O) so the SINGLE durable
        store also holds the new token (DEC-DHAN-TOKEN-SINGLE-SOURCE-001). ``on_set``
        must not raise — its wiring swallows + logs its own errors — so persistence
        can never tear the in-memory token or crash a renewal mid-pull.

        Parameters
        ----------
        token
            The freshly renewed (or freshly saved) access token string.
        """
        with self._lock:
            self._token = token
        if self._on_set is not None:
            self._on_set(token)


__all__ = ["TokenHolder"]
