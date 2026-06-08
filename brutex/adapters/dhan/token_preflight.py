"""Token pre-flight — classify a Dhan token (valid / expiring-soon / dead) BEFORE a bulk pull.

A bulk spot pull burns thousands of paced (``≤ 5 r/s``) vendor calls. If the
operator's 24-hour Dhan web token is already DEAD when the pull starts, every one
of those windows fires a doomed 401/``DH-906`` (the fail-fast guard then drains
the rest — DEC-DHAN-AUTH-FAILFAST-RENEW-001), wasting minutes and quota on a pull
that can NEVER land a single bar. ``RenewToken`` cannot revive a dead token
(physics: an expired token errors), so the only fix is a fresh manual token.

This module is the pre-flight gate that ties the merged auto-retry
(DEC-INGEST-AUTO-RETRY-001 #483) + pull verdict (DEC-PULL-VERDICT-001 #485)
together: **before** a bulk pull it classifies the token into one of three
states and, if it is DEAD, the caller REFUSES to start and emits the loud,
already-built ``auth_expired`` :class:`~brutex.adapters.dhan.pull_verdict.PullVerdict`
(reusing :func:`~brutex.adapters.dhan.pull_verdict.classify_pull` — NOT a second
verdict vocabulary) instead of burning a doomed pull.

Design (CLAUDE.md §11 COMMON + Golden Rule #1/#10):

* The **pure core** (:func:`classify_token_lifetime` / :func:`classify_token` /
  :func:`preflight_pull_verdict`) does NO network and NO clock of its own — it
  classifies from an injected ``now`` + a parsed ``expiry_deadline`` (epoch
  seconds) or an explicit ``probe_auth_failed`` flag. Every branch is unit-tested
  at 100% line+branch.
* The **live probe** (:func:`probe_token_live`) is the ONLY network seam — it
  fires one ``RenewToken`` to learn the fresh ``expiryTime`` (and, as a bonus,
  pushes the token ``+24h``); a refusal means the token is dead. It is
  ``# pragma: no cover`` (needs a real token + the ``urllib`` transport, absent
  in CI), exactly like the other live-only Dhan seams.

**Honest envelope (Golden Rule #10):** the pre-flight can only CLASSIFY + ADVISE
— it cannot revive a dead token. A ``dead`` verdict means a human must regenerate
the token at web.dhan.co; ``expiring_soon`` means renew before the next long pull
(the 12h keepalive normally handles this); ``valid`` means proceed. The
classifier is O(1) (a constant number of comparisons), bounded memory — never an
"O(1)-total" claim about the pull it gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from brutex.adapters.dhan.pull_verdict import PullVerdict, classify_pull
from brutex.adapters.dhan.token_keepalive import RENEW_BEFORE_EXPIRY_S

if TYPE_CHECKING:
    from collections.abc import Callable

    from brutex.adapters.dhan.client import DhanClient

#: The three mutually-exclusive token states, worst → best for branching clarity.
TokenState = Literal["dead", "expiring_soon", "valid"]

#: The fixed, test-pinnable one-line summary per state (kept as DATA, not an
#: f-string, so it is deterministic + golden-stable — the dynamic remaining life
#: is carried separately on :attr:`TokenPreflight.remaining_seconds`).
_SUMMARY: Final[dict[str, str]] = {
    "dead": "🔑 Dhan token is DEAD — regenerate at web.dhan.co before pulling.",
    "expiring_soon": "⏳ Dhan token is expiring soon — renew before a long bulk pull.",
    "valid": "✅ Dhan token healthy — ample life remaining.",
}


@dataclass(frozen=True, slots=True)
class TokenPreflight:
    """A token's pre-flight classification — its :data:`TokenState` + remaining life.

    Immutable + JSON-serialisable. ``remaining_seconds`` is the parsed life left
    at classification time (``None`` when a probe confirmed death WITHOUT a usable
    expiry, or when no expiry could be learned). ``reason`` is the fixed per-state
    summary (:data:`_SUMMARY`).
    """

    state: TokenState
    remaining_seconds: float | None
    reason: str

    @property
    def is_dead(self) -> bool:
        """True iff the token is DEAD — the caller must REFUSE the pull + re-auth."""
        return self.state == "dead"

    @property
    def is_valid(self) -> bool:
        """True iff the token is healthy (``valid``) — safe to start a long pull."""
        return self.state == "valid"

    @property
    def needs_renew(self) -> bool:
        """True iff the token should be renewed before a long pull (dead OR expiring-soon)."""
        return self.state in ("dead", "expiring_soon")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for a route / structured log."""
        return {
            "state": self.state,
            "remaining_seconds": self.remaining_seconds,
            "reason": self.reason,
            "is_dead": self.is_dead,
            "needs_renew": self.needs_renew,
        }


def _make(state: TokenState, remaining_seconds: float | None) -> TokenPreflight:
    """Build a :class:`TokenPreflight` for *state* with its canonical summary."""
    return TokenPreflight(state=state, remaining_seconds=remaining_seconds, reason=_SUMMARY[state])


def classify_token_lifetime(
    remaining_seconds: float,
    *,
    expiring_threshold_s: float = RENEW_BEFORE_EXPIRY_S,
) -> TokenPreflight:
    """Classify a token from its REMAINING life — the pure decision core.

    Three states, worst first:

    1. **dead** — ``remaining_seconds <= 0``: the token has crossed (or is exactly
       at) the 24h wall. A pull would only fire doomed 401s — REFUSE.
    2. **expiring_soon** — ``0 < remaining_seconds < expiring_threshold_s`` (the
       keepalive's 12h :data:`~brutex.adapters.dhan.token_keepalive.RENEW_BEFORE_EXPIRY_S`
       by default): still usable, but renew before a multi-hour pull.
    3. **valid** — ``remaining_seconds >= expiring_threshold_s``: ample life.

    Pure + O(1) (two comparisons). No I/O, no clock.
    """
    if remaining_seconds <= 0:
        return _make("dead", remaining_seconds)
    if remaining_seconds < expiring_threshold_s:
        return _make("expiring_soon", remaining_seconds)
    return _make("valid", remaining_seconds)


def classify_token(
    *,
    probe_auth_failed: bool,
    expiry_deadline: float | None,
    now: float,
    expiring_threshold_s: float = RENEW_BEFORE_EXPIRY_S,
) -> TokenPreflight:
    """Classify a token from a probe result — the pure wrapper :func:`probe_token_live` feeds.

    Resolves, in priority order:

    1. ``probe_auth_failed`` — the live probe got a 401/``DH-906`` (or any refusal):
       the token is conclusively **dead**, regardless of any stale expiry guess.
    2. ``expiry_deadline is None`` — the probe SUCCEEDED but no expiry could be
       parsed: the token clearly works right now, so report **valid** (the
       keepalive re-learns the real expiry on its next tick). ``remaining_seconds``
       is ``None`` (unknown).
    3. otherwise — classify the learned ``expiry_deadline - now`` lifetime via
       :func:`classify_token_lifetime`.

    Pure + O(1). ``now`` is injected (epoch seconds) so tests are deterministic.
    """
    if probe_auth_failed:
        return _make("dead", None)
    if expiry_deadline is None:
        return _make("valid", None)
    return classify_token_lifetime(expiry_deadline - now, expiring_threshold_s=expiring_threshold_s)


def preflight_pull_verdict(
    preflight: TokenPreflight, *, auth_cause: str | None = None
) -> PullVerdict | None:
    """Map a DEAD pre-flight to the loud ``auth_expired`` verdict; else ``None`` (proceed).

    Reuses :func:`~brutex.adapters.dhan.pull_verdict.classify_pull` (DEC-PULL-VERDICT-001,
    #485) — the SAME verdict vocabulary a finished pull emits — rather than
    inventing a parallel one (CLAUDE.md §11 COMMON). A non-dead pre-flight returns
    ``None`` so the caller simply starts the pull.

    Parameters
    ----------
    preflight
        The pre-flight classification.
    auth_cause
        Optional originating cause string (the probe's error), carried onto the
        ``auth_expired`` verdict for the operator. Defaults to *preflight*'s own
        :attr:`~TokenPreflight.reason` when not supplied.

    Returns
    -------
    PullVerdict | None
        The ``auth_expired`` :class:`PullVerdict` when the token is dead (so the
        caller can refuse + alert with the exact next step), else ``None``.
    """
    if not preflight.is_dead:
        return None
    return classify_pull(
        auth_aborted=True,
        still_failed_windows=0,
        failed_pairs=0,
        no_data=0,
        completed=0,
        auth_cause=auth_cause if auth_cause is not None else preflight.reason,
    )


def probe_token_live(  # pragma: no cover — live Dhan token + RenewToken network, absent in CI
    client: DhanClient,
    *,
    pace: Callable[[], None] | None = None,
    now: Callable[[], float] | None = None,
    expiring_threshold_s: float = RENEW_BEFORE_EXPIRY_S,
) -> TokenPreflight:
    """Probe the LIVE token via one ``RenewToken`` and classify it (the only network seam).

    Fires one ``RenewToken`` (``GET``, headers-only — DEC-DHAN-RENEWTOKEN-GET-001)
    through the injected-transport :class:`~brutex.adapters.dhan.client.DhanClient`:

    * a refusal (:class:`~brutex.adapters.dhan.errors.DhanError`) means the token
      is dead → :func:`classify_token` with ``probe_auth_failed=True``;
    * success returns the fresh ``expiryTime`` (and pushes the token ``+24h`` as a
      bonus), parsed by
      :func:`~brutex.adapters.dhan.token_keepalive.parse_expiry_deadline` into an
      epoch deadline and classified by the learned remaining life.

    Paced through the shared bucket so the physical probe is ``≤ 5 r/s``
    (CLAUDE.md §2 Rule 6). ``# pragma: no cover`` — needs a real token + network;
    its decision logic is the 100%-covered pure :func:`classify_token`.
    """
    import time

    from brutex.adapters.dhan.auth import renew_token
    from brutex.adapters.dhan.errors import DhanError
    from brutex.adapters.dhan.token_keepalive import parse_expiry_deadline

    clock = now if now is not None else time.time
    try:
        info = renew_token(client, pace=pace)
    except DhanError:
        return classify_token(
            probe_auth_failed=True,
            expiry_deadline=None,
            now=clock(),
            expiring_threshold_s=expiring_threshold_s,
        )
    deadline = parse_expiry_deadline(info.expiry_time, now=clock)
    return classify_token(
        probe_auth_failed=False,
        expiry_deadline=deadline,
        now=clock(),
        expiring_threshold_s=expiring_threshold_s,
    )


__all__ = [
    "TokenPreflight",
    "TokenState",
    "classify_token",
    "classify_token_lifetime",
    "preflight_pull_verdict",
    "probe_token_live",
]
