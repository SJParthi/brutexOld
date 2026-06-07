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


class TokenHolder:
    """A thread-safe mutable holder for the current Dhan access token.

    Shared by every live client of a pull and written by the background
    keepalive on each renew. The lock makes a concurrent :meth:`get` (a worker
    building its request headers) and :meth:`set` (the keepalive swapping in the
    fresh token) atomic, so a reader always sees a whole token string — never a
    torn intermediate (DEC-DHAN-TOKEN-KEEPALIVE-001).

    Parameters
    ----------
    token
        The initial access token (the resolved 24h web token at app start).
    """

    __slots__ = ("_lock", "_token")

    def __init__(self, token: str) -> None:
        """Build a holder seeded with *token*."""
        self._lock = threading.Lock()
        self._token = token

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
        """Replace the held token with *token* — O(1), thread-safe.

        Called by the keepalive after a successful ``RenewToken``; the next
        :meth:`get` (this pull's next call, or an in-flight worker's next call)
        immediately uses the fresh token.

        Parameters
        ----------
        token
            The freshly renewed access token string.
        """
        with self._lock:
            self._token = token


__all__ = ["TokenHolder"]
