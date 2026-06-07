"""Continuous background keepalive for the 24h Dhan web access token.

While the app/dashboard is running, this keeps the Dhan **24-hour web access
token** alive so a live pull never 401s on an expired token mid-run
(DEC-DHAN-TOKEN-KEEPALIVE-001, builds on the fail-fast + proactive-renew
DEC-DHAN-AUTH-FAILFAST-RENEW-001 #555). A single daemon thread polls the
token's expiry and, when the remaining life drops below
:data:`RENEW_BEFORE_EXPIRY_S` (**12 hours**, operator-chosen), fires one
``RenewToken`` POST. The 12h threshold means a 24h token is renewed at its
half-life, so it always carries ≥ 12h of buffer — resilient to a restart or a
missed cycle (vs a 2h threshold which would leave only 2h of slack).

Because ``RenewToken`` **invalidates the old token** when it issues the new one,
the fresh token is written into a shared :class:`~brutex.adapters.dhan.token_holder.TokenHolder`
so every live client (and every in-flight pull worker) reading through its
``token_provider`` seam picks it up instantly — a stale string would 401.

Honest envelope (Golden Rule #10 — NO illusion). "Never expires while running"
holds ONLY while all three are true:

* the token is **web-generated** — ``RenewToken`` refuses a TOTP-minted token;
* the **process is running** to fire the timer (this is in-process, not a cron);
* the **first** token is **active** — ``RenewToken`` cannot revive a dead token
  (physics: an expired token errors).

When all three hold, the 12h keepalive keeps the token alive indefinitely. This
is NOT the §11 "zero clicks/day across restarts/downtime" guarantee — that needs
TOTP, which is explicitly out of scope here. Only the per-tick holder/keepalive
work is O(1); nothing here is "O(1) total". Steady-state cost is ≈ 2 RenewToken
POSTs/day — negligible vs the 100,000/day quota — and each is paced through the
≤ 5 r/s bucket (CLAUDE.md §2 Rule 6) via the injected ``pace`` hook.

All decision logic lives in the synchronous, injected-clock :meth:`TokenKeepalive._tick`,
so every branch is unit-tested deterministically with a fake clock + fake
transport — NO real threads, NO real sleeps. The thread wrapper
(:meth:`start` / :meth:`stop` / :meth:`_run`) is thin.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Final

import structlog

from brutex.adapters.dhan.auth import renew_token
from brutex.adapters.dhan.errors import DhanError

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from brutex.adapters.dhan.client import DhanClient
    from brutex.adapters.dhan.token_holder import TokenHolder

#: Module logger — emits ``auth.renewed`` / ``auth.renew_failed`` /
#: ``auth.keepalive.expiry_unparsed`` into the operator's structlog stream.
_LOG = structlog.get_logger("brutex.adapters.dhan.token_keepalive")

#: Renew when the token's remaining life drops below this (12 hours, the named,
#: operator-chosen, tunable threshold). A 24h token renewed at its 12h mark
#: always retains ≥ 12h of buffer — resilient to restarts / missed cycles.
RENEW_BEFORE_EXPIRY_S: Final[float] = 12 * 3600.0

#: Hard upper bound (seconds) on a single inter-tick sleep, so the loop wakes at
#: least hourly even far from expiry (clock skew / suspended-laptop hygiene).
MAX_SLEEP_S: Final[float] = 3600.0

#: Sleep before the next tick after a renew that FAILED or when there is no
#: learnable expiry yet — retry hourly so a freshly pasted active token is picked
#: up on a later tick (RenewToken cannot revive a dead one — physics).
RETRY_WHEN_DEAD_S: Final[float] = 3600.0

#: Fallback assumed token lifetime (seconds) when ``expiryTime`` cannot be
#: parsed — a fresh 24h token. Used so a parse glitch degrades to "renew in ~12h"
#: instead of crashing the keepalive (logged once as ``auth.keepalive.expiry_unparsed``).
_FALLBACK_LIFETIME_S: Final[float] = 24 * 3600.0

#: Dhan ``expiryTime`` formats (IST wall-clock, per docs/dhan-ref/02), tried in
#: order: with milliseconds (``2026-01-01T00:00:00.000``) then without.
_EXPIRY_FORMATS: Final[tuple[str, ...]] = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S")


def parse_expiry_deadline(
    expiry_time: str,
    *,
    now: Callable[[], float] = time.time,
    log: BoundLogger | None = None,
) -> float:
    """Parse a Dhan ``expiryTime`` string into an epoch-seconds deadline.

    Dhan returns e.g. ``"2026-01-01T00:00:00.000"`` (IST wall-clock, docs/dhan-ref/02).
    The string is naive (no offset), so it is interpreted as a local wall-clock
    timestamp and converted to epoch seconds. On ANY parse failure this NEVER
    raises: it logs ONE ``auth.keepalive.expiry_unparsed`` WARNING and falls back
    to ``now() + 24h`` (a fresh-token assumption), so a vendor format drift can
    never crash the keepalive — the next tick simply re-evaluates.

    Parameters
    ----------
    expiry_time
        The ``expiryTime`` field from a ``RenewToken`` response.
    now
        Injected epoch-seconds clock (default :func:`time.time`); used only for
        the fallback deadline so tests are deterministic.
    log
        Optional structlog logger for the unparsed-fallback warning.

    Returns
    -------
    float
        The expiry deadline as epoch seconds (parsed, or ``now() + 24h`` fallback).
    """
    logger = log if log is not None else _LOG
    for fmt in _EXPIRY_FORMATS:
        try:
            parsed = datetime.strptime(expiry_time, fmt)  # naive IST wall-clock, see below
        except ValueError:
            continue
        # The string is a naive IST wall-clock with no offset; interpret it as a
        # local timestamp. epoch math only needs to be self-consistent with the
        # injected ``now`` (both wall-clock seconds) for the 12h delta to hold.
        return parsed.timestamp()
    logger.warning(
        "auth.keepalive.expiry_unparsed",
        expiry_time=expiry_time,
        note="unparseable expiryTime; assuming now+24h so the keepalive cannot crash",
    )
    return now() + _FALLBACK_LIFETIME_S


class TokenKeepalive:
    """Continuous background renewer for the live Dhan web access token.

    Constructed with the renew :class:`~brutex.adapters.dhan.client.DhanClient`
    (built with ``token_provider=holder.get`` so it always POSTs with the LIVE
    token) and the shared :class:`~brutex.adapters.dhan.token_holder.TokenHolder`.
    :meth:`start` spawns ONE daemon thread that loops ``sleep(self._tick())``
    until :meth:`stop` sets the stop event; all decision logic is in the
    synchronous, injected-clock :meth:`_tick`.

    Parameters
    ----------
    client
        The :class:`DhanClient` performing the ``RenewToken`` POST (live token via
        its ``token_provider``).
    holder
        The shared :class:`TokenHolder`; a successful renew writes the fresh token
        here so every reader picks it up.
    pace
        Optional rate-bucket acquire hook gating the physical ``RenewToken`` POST
        (≤ current r/s ≤ 5, CLAUDE.md §2 Rule 6); forwarded to
        :func:`~brutex.adapters.dhan.auth.renew_token`. ``None`` falls back to the
        client's own ``pace``.
    now
        Injected epoch-seconds clock (default :func:`time.time`) for the
        expiry/remaining math — overridden in tests for determinism.
    sleep
        Injected sleep hook (default :func:`time.sleep`); the thread loop waits on
        the value :meth:`_tick` returns.
    log
        Optional structlog logger (defaults to this module's logger).
    """

    __slots__ = (
        "_client",
        "_expiry_deadline",
        "_holder",
        "_log",
        "_now",
        "_pace",
        "_sleep",
        "_stop",
        "_thread",
    )

    def __init__(
        self,
        client: DhanClient,
        holder: TokenHolder,
        *,
        pace: Callable[[], None] | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: BoundLogger | None = None,
    ) -> None:
        """Build a keepalive; the expiry deadline is unknown until the first renew."""
        self._client = client
        self._holder = holder
        self._pace = pace
        self._now = now
        self._sleep = sleep
        self._log = log if log is not None else _LOG
        #: Epoch-seconds expiry deadline; ``None`` until the first renew LEARNS it.
        self._expiry_deadline: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _renew_once(self) -> float:
        """Fire ONE ``RenewToken``; on success update holder + deadline. Return next sleep.

        Best-effort: on success writes the fresh token to the holder, refreshes
        the stored deadline from the new ``expiryTime``, logs ``auth.renewed`` and
        schedules the next tick for ``remaining - 12h`` (clamped). On a
        :class:`DhanError` (RenewToken refused — token expired / TOTP-minted) logs
        ONE ``auth.renew_failed``, KEEPS the old deadline, and retries in
        :data:`RETRY_WHEN_DEAD_S` so a later operator-pasted active token succeeds.
        """
        try:
            info = renew_token(self._client, pace=self._pace)
        except DhanError as exc:
            self._log.warning(
                "auth.renew_failed",
                reason=str(exc),
                note="RenewToken refused (token may be expired or TOTP-minted); will retry",
            )
            return RETRY_WHEN_DEAD_S
        self._holder.set(info.access_token)
        self._expiry_deadline = parse_expiry_deadline(
            info.expiry_time, now=self._now, log=self._log
        )
        self._log.info("auth.renewed", expiry_time=info.expiry_time)
        remaining = self._expiry_deadline - self._now()
        return min(max(remaining - RENEW_BEFORE_EXPIRY_S, 0.0), MAX_SLEEP_S)

    def _tick(self) -> float:
        """Evaluate the token once; renew if due. Return seconds to sleep before the next tick.

        Deterministic + side-effect-isolated (the only effects are the injected
        ``renew_token`` POST + ``holder.set`` + logging), so the whole policy is
        unit-tested with a fake clock + fake transport — no threads, no real sleeps.

        Logic:

        * If the expiry deadline is unknown (first tick), do an immediate renew to
          LEARN the expiry + refresh the token.
        * Else compute ``remaining = expiry_deadline - now()``; if it is below
          :data:`RENEW_BEFORE_EXPIRY_S` (12h), renew now; otherwise sleep until the
          token is ~12h from expiry (clamped to :data:`MAX_SLEEP_S`).

        Returns
        -------
        float
            Seconds to sleep before the next tick.
        """
        if self._expiry_deadline is None:
            return self._renew_once()
        remaining = self._expiry_deadline - self._now()
        if remaining < RENEW_BEFORE_EXPIRY_S:
            return self._renew_once()
        return min(remaining - RENEW_BEFORE_EXPIRY_S, MAX_SLEEP_S)

    def _run(self) -> None:
        """Daemon loop: sleep for one tick's verdict until stopped.

        Thin wrapper over :meth:`_tick` — waits on the stop :class:`threading.Event`
        for the returned interval (so :meth:`stop` interrupts a long sleep
        promptly) and exits when the event is set.
        """
        while not self._stop.is_set():
            wait_s = self._tick()
            # Event.wait returns True if the stop flag was set during the wait —
            # break promptly instead of looping into another tick.
            if self._stop.wait(wait_s):
                break

    def start(self) -> None:  # pragma: no cover — thread-loop glue (logic tested via _tick/_run)
        """Spawn the single daemon keepalive thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="dhan-token-keepalive", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:  # pragma: no cover — thread-loop glue
        """Signal the loop to stop and join the thread (bounded by *timeout*)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


__all__ = [
    "MAX_SLEEP_S",
    "RENEW_BEFORE_EXPIRY_S",
    "RETRY_WHEN_DEAD_S",
    "TokenKeepalive",
    "parse_expiry_deadline",
]
