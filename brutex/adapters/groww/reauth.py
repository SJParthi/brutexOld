"""Groww SDK refresher — thread-safe, single-flight access-token RE-MINT.

DEC-GROWW-TOKEN-REAUTH-001. Groww's daily access token resets at 06:00 IST. The
live client is built ONCE per pull (:func:`~brutex.adapters.groww.live.build_live_client`
/ :func:`~brutex.adapters.groww.live.build_live_pool` mint the token once and hand it
to ``sdk_factory(token)``), so the in-flight token is NEVER re-minted mid-pull — and
when the 06:00 reset invalidates it, EVERY subsequent call fails with a terminal
:class:`~brutex.adapters.groww.errors.GrowwAuthError` (which :func:`~brutex.adapters.\
groww.errors.is_retryable` returns ``False`` for → quarantined) and the overnight
pull dies needing a manual restart. ``errors.py`` already documented the intent (the
:class:`GrowwAuthError` docstring: "triggers re-auth, not blind retry") and
``ingest.py`` noted the latch "can be added if/when" — this is that sanctioned latch.

The refresher is the SINGLE mint path: it mints the INITIAL token + the *size*
independent SDK connections at construction (so there is ONE code path, not a
separate first-mint in ``live.py``) and re-mints on demand via :meth:`reauth`. A
SINGLE refresher backs both the single :class:`~brutex.adapters.groww.client.GrowwClient`
(``size=1``) and the K-leaf :class:`~brutex.adapters.groww.pool.ClientPool`
(``size=K``): each pool leaf binds to its own SDK index via :meth:`sdk_at`, so the
pool's K-way concurrency is preserved AND a single re-mint at 06:00 refreshes EVERY
leaf at once. It is:

* **THREAD-SAFE** — a :class:`threading.Lock` guards every read + the re-mint, so a
  multi-connection pool whose K worker threads all hit the expired token at 06:00 is
  correct.
* **SINGLE-FLIGHT** — a monotonically-rising :attr:`generation` counter lets the
  06:00 stampede mint EXACTLY ONCE: a caller passes the generation it last saw to
  :meth:`reauth`; if another thread already re-authed since (the stored generation
  moved past the caller's), the caller gets the CURRENT SDKs with NO second mint (no
  token stampede against the vendor).
* **BOUNDED** — the bound lives at the call site (:class:`~brutex.adapters.groww.\
  client.GrowwClient`'s ``max_reauths`` budget): a genuinely-bad credential re-mints
  once, the call fails again, and the client raises terminally — never an infinite
  loop.

Every side-effecting step is an **injected callable** (``token_factory`` /
``totp_minter`` flow through to :func:`~brutex.adapters.groww.auth.get_access_token`,
and ``sdk_factory`` builds the real ``GrowwAPI``), exactly the callables ``live.py``
already uses — so the whole refresher is unit-tested with fakes, no ``growwapi`` /
``pyotp`` install and no network. A mint failure surfaces as a redacting
:class:`GrowwAuthError` straight from :func:`get_access_token` (the secret never
reaches a log).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from brutex.adapters.groww.auth import (
    GrowwCredentials,
    _real_token_factory,
    _real_totp,
    get_access_token,
)
from brutex.adapters.groww.client import GrowwSDK


def _real_sdk_factory(token: str) -> GrowwSDK:  # pragma: no cover — needs growwapi + network
    """Default SDK factory: ``GrowwAPI(token)`` (lazy import of the real SDK)."""
    from growwapi import GrowwAPI  # type: ignore[import-not-found, unused-ignore]

    return GrowwAPI(token)  # type: ignore[no-any-return]


class SdkRefresher:
    """Thread-safe single-flight minter of *size* fresh Groww :class:`GrowwSDK` connections.

    Constructed with the SAME injected callables :func:`~brutex.adapters.groww.live.\
    build_live_client` uses; mints the INITIAL token + the *size* independent SDK
    connections eagerly (ONE mint path).

    Parameters
    ----------
    creds
        The Groww TOTP api key + shared secret (held only to mint codes).
    size
        The number of independent SDK connections to mint (clamped to ``>= 1``).
        ``1`` for the single client; ``K`` for a K-leaf pool (each leaf binds an index).
    token_factory
        Access-token issuer (default: the real ``GrowwAPI.get_access_token``).
    totp_minter
        TOTP code minter (default: the real ``pyotp.TOTP(...).now()``).
    sdk_factory
        Builds one SDK instance from the issued token (default: ``GrowwAPI(token)``);
        called *size* times per mint to fan independent connections off one token.

    Raises
    ------
    GrowwAuthError
        If the INITIAL token issuance fails (redacting ``safe_message`` — the secret
        never leaks), straight from :func:`~brutex.adapters.groww.auth.get_access_token`.
    """

    __slots__ = (
        "_creds",
        "_generation",
        "_lock",
        "_sdk_factory",
        "_sdks",
        "_size",
        "_token_factory",
        "_totp_minter",
    )

    def __init__(
        self,
        creds: GrowwCredentials,
        *,
        size: int = 1,
        token_factory: Callable[[str, str], str] = _real_token_factory,
        totp_minter: Callable[[str], str] = _real_totp,
        sdk_factory: Callable[[str], GrowwSDK] = _real_sdk_factory,
    ) -> None:
        self._creds = creds
        self._size = max(1, size)
        self._token_factory = token_factory
        self._totp_minter = totp_minter
        self._sdk_factory = sdk_factory
        self._lock = threading.Lock()
        #: Monotonically rising re-auth counter — starts at 0 for the INITIAL mint;
        #: each successful :meth:`reauth` bumps it, so a caller can prove single-flight.
        self._generation = 0
        #: The INITIAL mint — the ONE mint path (``live.py`` no longer mints separately).
        self._sdks = self._mint_sdks()

    def _mint_sdks(self) -> tuple[GrowwSDK, ...]:
        """Mint a fresh token then build *size* independent SDK connections off it.

        NOT lock-guarded — callers hold the lock. Wraps the side effects through
        :func:`~brutex.adapters.groww.auth.get_access_token`, which raises a redacting
        :class:`~brutex.adapters.groww.errors.GrowwAuthError` on a mint failure (the
        secret never reaches a log).
        """
        token_info = get_access_token(
            self._creds,
            token_factory=self._token_factory,
            totp_minter=self._totp_minter,
        )
        token = token_info.access_token
        return tuple(self._sdk_factory(token) for _ in range(self._size))

    @property
    def size(self) -> int:
        """The number of independent SDK connections this refresher fans (``>= 1``)."""
        return self._size

    def current_sdk(self) -> GrowwSDK:
        """Return the first live SDK (lock-guarded read, O(1)).

        The single-client convenience accessor; a pool leaf uses :meth:`sdk_at`.
        """
        with self._lock:
            return self._sdks[0]

    def sdk_at(self, index: int) -> GrowwSDK:
        """Return the live SDK bound to leaf *index*, modulo :attr:`size` (lock-guarded, O(1)).

        Each pool leaf calls this with its own fixed index, so a single re-mint
        refreshes EVERY leaf at once while preserving K independent connections. The
        modulo keeps an over-large index in range (defensive; the pool only ever passes
        ``0..size-1``).
        """
        with self._lock:
            return self._sdks[index % self._size]

    def current_generation(self) -> int:
        """Return the current re-auth generation (lock-guarded read, O(1))."""
        with self._lock:
            return self._generation

    def reauth(self, seen_generation: int) -> int:
        """Re-mint the token + SDK connections, SINGLE-FLIGHT against *seen_generation*.

        Under the lock: if :attr:`generation` already moved past *seen_generation*
        (another thread re-authed since the caller's last read — the 06:00 stampede),
        return the CURRENT generation WITHOUT a second mint. Otherwise mint a fresh
        token, rebuild all *size* SDK connections, bump the generation, store, and
        return the new generation. (Callers re-resolve their SDK via :meth:`sdk_at` /
        :meth:`current_sdk` on the retry — so a returned generation alone suffices.)

        Parameters
        ----------
        seen_generation
            The generation the caller last observed (via :meth:`current_generation`).
            If the stored generation differs, another thread already refreshed and this
            call is a no-op handoff (single-flight).

        Returns
        -------
        int
            The live generation after this call — bumped if THIS caller won the
            single-flight race, else the already-refreshed current generation.

        Raises
        ------
        GrowwAuthError
            If the re-mint itself fails (redacting ``safe_message``) — propagated so
            the client's bounded budget eventually raises terminally for a genuinely
            bad credential (never an infinite loop).
        """
        with self._lock:
            if self._generation != seen_generation:
                # Another thread already re-authed — single-flight: no second mint.
                return self._generation
            self._sdks = self._mint_sdks()
            self._generation += 1
            return self._generation


__all__ = ["SdkRefresher"]
