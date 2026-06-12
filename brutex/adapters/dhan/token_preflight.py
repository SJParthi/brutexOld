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
* The **live probes** (:func:`probe_token_data_live` / :func:`probe_token_live`)
  are the ONLY network seams. The DEFAULT gate uses :func:`probe_token_data_live`
  — it fires ONE tiny ``charts/intraday`` call (the exact surface a pull needs)
  and classifies the token **dead only on a genuine auth failure**
  (:func:`~brutex.adapters.dhan.errors.is_auth_failure` — a 401/403 OR a 400
  carrying ``DH-906`` / "Invalid Token"), so a transient 429 / 5xx / timeout /
  plain-400 / no-data outcome NEVER false-deads a working token
  (DEC-TOKEN-PREFLIGHT-DATAPROBE-001). The legacy :func:`probe_token_live` fires
  one ``RenewToken`` to learn the fresh ``expiryTime`` (and push the token
  ``+24h``) — but ``RenewToken`` is a POOR validity oracle (it only renews
  *web-generated, active* tokens and refuses TOTP-/OAuth-minted ones that are
  perfectly valid for data calls — ``docs/dhan-ref/02``), so it too now treats
  ONLY an :func:`~brutex.adapters.dhan.errors.is_auth_failure` as dead. Both are
  ``# pragma: no cover`` (need a real token + the ``urllib`` transport, absent in
  CI), exactly like the other live-only Dhan seams.

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

from brutex.adapters.dhan.errors import DhanError, is_auth_failure
from brutex.adapters.dhan.token_keepalive import RENEW_BEFORE_EXPIRY_S
from brutex.adapters.ingest_common import PullVerdict, classify_pull

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from brutex.adapters.dhan.adapter import DhanMarketAdapter
    from brutex.adapters.dhan.client import DhanClient
    from brutex.models import Instrument

#: Track-1's fetched intraday interval label — the 1-minute base the data probe
#: hits (matches ``DHAN_FETCH_INTERVALS``; the adapter resamples everything else).
_PROBE_INTERVAL: Final[str] = "1minute"

#: Lookback (days) for the data probe's tiny window. A short recent span is
#: enough to learn "does this token fetch bars?"; an all-holiday span returns
#: empty/no-data, which is correctly classified NOT-dead (it is not an auth fail).
_PROBE_LOOKBACK_DAYS: Final[int] = 7

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


def classify_data_probe(
    error: DhanError | None,
    *,
    now: float,
    expiring_threshold_s: float = RENEW_BEFORE_EXPIRY_S,
) -> TokenPreflight:
    """Classify a DATA-endpoint probe outcome — DEAD only on a genuine auth failure.

    The data probe (one tiny ``charts/intraday`` call) tests the exact thing a
    pull needs: can this token fetch bars? The mapping (DEC-TOKEN-PREFLIGHT-
    DATAPROBE-001) deliberately reuses the project's single source of truth for
    "the token died", :func:`~brutex.adapters.dhan.errors.is_auth_failure`, so the
    pre-flight GATE can never be more trigger-happy than the in-pull fail-fast
    guard it fronts:

    * ``error is None`` — the probe fetched (or got an empty / no-data window)
      WITHOUT an auth failure: the token works → **valid**.
    * ``is_auth_failure(error)`` (a 401/403, or a 400 carrying ``DH-906`` /
      "Invalid Token") — conclusively **dead**: a human must regenerate the token.
    * any OTHER :class:`~brutex.adapters.dhan.errors.DhanError` (429 / 5xx /
      timeout / transport / plain-400 / ``DH-905`` no-data) — transient or
      unrelated, NOT a dead token: report **valid** so the pull PROCEEDS (the
      in-pull guard still catches a real mid-pull death). This is the fix for the
      false-dead loop where a transient ``RenewToken`` hiccup — or a token
      ``RenewToken`` structurally refuses — wrongly aborted every pull.

    Pure + O(1) (one predicate + the :func:`classify_token` dispatch). ``now`` is
    injected (epoch seconds) so tests are deterministic; ``remaining_seconds`` is
    ``None`` (the data probe learns no expiry — it only proves the token is alive).
    """
    auth_failed = error is not None and is_auth_failure(error)
    return classify_token(
        probe_auth_failed=auth_failed,
        expiry_deadline=None,
        now=now,
        expiring_threshold_s=expiring_threshold_s,
    )


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
    from brutex.adapters.dhan.token_keepalive import parse_expiry_deadline

    clock = now if now is not None else time.time
    try:
        info = renew_token(client, pace=pace)
    except DhanError as exc:
        # NARROW: a RenewToken refusal is the token's death ONLY when it is a
        # genuine auth failure (is_auth_failure — 401/403 or DH-906). A transient
        # 429 / 5xx / timeout — or a 400 from a token RenewToken structurally
        # refuses (TOTP-/OAuth-minted) — must NOT false-dead a token that can
        # still fetch data (DEC-TOKEN-PREFLIGHT-DATAPROBE-001). On a non-auth
        # error we report "valid" so the pull proceeds; the in-pull fail-fast
        # guard still catches a real mid-pull death.
        return classify_data_probe(exc, now=clock(), expiring_threshold_s=expiring_threshold_s)
    deadline = parse_expiry_deadline(info.expiry_time, now=clock)
    return classify_token(
        probe_auth_failed=False,
        expiry_deadline=deadline,
        now=clock(),
        expiring_threshold_s=expiring_threshold_s,
    )


def probe_token_data_live(  # pragma: no cover — live Dhan token + data network, absent in CI
    adapter: DhanMarketAdapter,
    instrument: Instrument,
    *,
    today: date | None = None,
    now: Callable[[], float] | None = None,
    expiring_threshold_s: float = RENEW_BEFORE_EXPIRY_S,
) -> TokenPreflight:
    """Probe the LIVE token with ONE tiny ``charts/intraday`` call (the DEFAULT gate).

    DEC-TOKEN-PREFLIGHT-DATAPROBE-001. Fires a single 1-minute fetch for
    *instrument* over a short recent window (:data:`_PROBE_LOOKBACK_DAYS` days,
    ending the *today* reference) through the live
    :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter` — the EXACT surface a
    bulk pull uses — and classifies via :func:`classify_data_probe`:

    * the fetch returns (bars OR an empty / no-data window) → **valid**;
    * a genuine auth failure (:func:`~brutex.adapters.dhan.errors.is_auth_failure`
      — 401/403 or a ``DH-906`` 400) → **dead**;
    * any other :class:`~brutex.adapters.dhan.errors.DhanError` (429 / 5xx /
      timeout / transport / ``DH-905``) → **valid** (proceed — not a dead token).

    This is the wrong-oracle fix: unlike ``RenewToken`` (which refuses TOTP-/OAuth-
    minted tokens that ARE valid for data — ``docs/dhan-ref/02``), a data probe
    asks the only question that matters: *can this token pull bars?* ``# pragma: no
    cover`` — needs a real token + network; its decision logic is the 100%-covered
    pure :func:`classify_data_probe`.
    """
    import time
    from datetime import UTC, datetime, timedelta

    clock = now if now is not None else time.time
    ref = today if today is not None else datetime.now(UTC).date()
    # ``end`` is the EXCLUSIVE half-open boundary the adapter tiles on (it maps an
    # inclusive end day D to D+1 itself), so a [ref - lookback, ref) window is a
    # plausible recent span without ever requesting the future.
    probe_start = ref - timedelta(days=_PROBE_LOOKBACK_DAYS)
    try:
        adapter.fetch_ohlcv(
            symbol=instrument.symbol_name,
            exchange=instrument.exch_id,
            interval=_PROBE_INTERVAL,
            start=probe_start,
            end=ref,
        )
    except DhanError as exc:
        return classify_data_probe(exc, now=clock(), expiring_threshold_s=expiring_threshold_s)
    return classify_data_probe(None, now=clock(), expiring_threshold_s=expiring_threshold_s)


__all__ = [
    "TokenPreflight",
    "TokenState",
    "classify_data_probe",
    "classify_token",
    "classify_token_lifetime",
    "preflight_pull_verdict",
    "probe_token_data_live",
    "probe_token_live",
]
