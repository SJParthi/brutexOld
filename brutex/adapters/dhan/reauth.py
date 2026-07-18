"""Dhan token refresher — thread-safe single-flight self-heal for the 24-hour token.

DEC-DHAN-ADAPTER-001. Dhan's access token is a JWT with a **24-hour lifetime**
(VERIFIED). On a read-only broker host BruteX never mints — when the shared daily
token is rejected mid-run, the client's reauth branch calls :meth:`DhanTokenRefresher.\
reauth`, which RE-READS the shared token source (the Parameter-Store SecureString) and
ADOPTS a freshly-written token; if the re-read returns the SAME dead token (the minter
has not written the new one yet), it raises the RETRYABLE
:class:`~brutex.adapters.dhan.errors.DhanTokenStillStaleError` so the window re-pulls
later (mirroring Groww's :class:`~brutex.adapters.groww.reauth.SdkRefresher`
read-only re-read path).

Thread-safe + single-flight: a generation counter lets a second concurrent reauth
adopt the first's freshly-read token instead of re-reading (never a thundering herd).
Imports only stdlib + the pure error leaf, so it never joins an import cycle with the
client (the client imports it, not vice-versa).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import structlog

from brutex.adapters.dhan.errors import DhanTokenStillStaleError

_LOG = structlog.get_logger("brutex.adapters.dhan.reauth")


@runtime_checkable
class DhanTokenSource(Protocol):
    """A read-only source of the current shared Dhan access token (the SSM reader)."""

    def read_token(self) -> str | None:
        """Return the current shared access token, or ``None`` when absent/unreadable."""
        raise NotImplementedError  # structural Protocol stub — never executed


class DhanTokenRefresher:
    """Thread-safe single-flight refresher for the shared 24-hour Dhan access token.

    Parameters
    ----------
    initial_token
        The token the client starts with (the value read at build time).
    read_token
        A callable returning the CURRENT shared token (a read-only SSM read), or
        ``None`` when absent/unreadable. Injected so tests drive every branch with no
        network.
    """

    __slots__ = ("_generation", "_lock", "_read_token", "_token")

    def __init__(self, *, initial_token: str, read_token: Callable[[], str | None]) -> None:
        self._token = initial_token
        self._read_token = read_token
        self._lock = threading.Lock()
        self._generation = 0

    @property
    def access_token(self) -> str:
        """The current live token (the client reads this per call)."""
        return self._token

    @property
    def generation(self) -> int:
        """How many times a fresh token has been adopted (diagnostics + single-flight)."""
        return self._generation

    def reauth(self) -> None:
        """RE-READ the shared token and adopt a fresh one, else raise the retryable stale error.

        Single-flight: if another thread already adopted a fresh token since this call
        began (the generation advanced), this returns WITHOUT re-reading — it adopts
        that thread's token. Otherwise it re-reads; a NON-blank token that differs from
        the current one is adopted (the healthy rotation); a same / blank / unreadable
        token raises :class:`~brutex.adapters.dhan.errors.DhanTokenStillStaleError`
        (RETRYABLE — the window re-pulls later when the minter's fresh token lands).
        """
        seen = self._generation
        with self._lock:
            if self._generation != seen:
                _LOG.info("dhan.token.reread_adopted_concurrent", generation=self._generation)
                return  # another thread already refreshed — adopt its token
            fresh = self._read_token()
            if fresh is not None and fresh.strip() and fresh != self._token:
                self._token = fresh
                self._generation += 1
                _LOG.info("dhan.token.reread_adopted", generation=self._generation)
                return
            _LOG.warning("dhan.token.reread_still_stale")
            raise DhanTokenStillStaleError(
                "read-only Dhan access-token re-read returned the SAME dead token — "
                "the minter has not written the fresh one yet; the window re-pulls later"
            )


__all__ = ["DhanTokenRefresher", "DhanTokenSource"]
