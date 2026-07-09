"""NSE market-hours pull guard — no Groww VENDOR pulls during the trading window.

DEC-GROWW-MARKET-HOURS-GUARD-001 (MIGRATION.md #1624). The Groww access token is
SHARED with TickVault's live feed on the SAME account (§2 Rule 6 — a single TOTP
token, one 500/min governor). A nightly BruteX pull (launched ~01:30 IST) that
runs long enough to bleed into the NSE trading session competes with TickVault
for the shared per-minute budget and trips the account throttle, so TickVault's
LIVE feed starts getting 429-rejected during market hours. This module is the
COMMON, dependency-light predicate every vendor-pull entrypoint consults to
REFUSE (at the start gates) and STOP (mid-run) a pull between **08:45 (inclusive)
and 15:35 (exclusive) IST on an NSE trading day**.

The window is deliberately WIDER than the 09:15-15:30 session: it opens 30 minutes
before the pre-open auction (so a pull is already stopped before the exchange's own
pre-open connection storm) and closes 5 minutes after the session so the account is
demonstrably quiet before a pull may resume.

Non-vendor work (Greeks-backfill, the timeframe prebuild, the movers warm) makes
ZERO vendor calls and is EXEMPT — those stages are never gated by this guard.

Honest envelope (Golden Rule #10): this guard STOPS an in-flight pull at 08:45 and
REFUSES a pull from starting/resuming until 15:35 on a trading day — a pull
interrupted at 08:45 resumes on the NEXT nightly run, NOT the same day at 15:35 (a
same-day 15:35 re-arm is a NAMED follow-up, not built here). The trading-day test
uses the NON-raising :func:`~brutex.calendars.nse.is_trading_day`, so an
out-of-holiday-coverage weekday reads as a trading day and BLOCKS during market
hours — the deliberately fail-safe posture that protects the shared account when
the holiday list is silent (a weekend reads non-trading → a pull is ALLOWED).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Final

from brutex.calendars.nse import is_trading_day, now_ist

#: The pull-blocked window START, INCLUSIVE (IST wall-clock). 30 minutes before the
#: 09:15 session so a pull is stopped ahead of the exchange's pre-open storm.
BLOCK_START: Final[time] = time(8, 45)

#: The pull-blocked window END, EXCLUSIVE (IST wall-clock). 5 minutes after the
#: 15:30 close so the shared account is demonstrably quiet before a pull may resume.
BLOCK_END: Final[time] = time(15, 35)

#: Env kill switch name (fail-loud). Unset / on-token ⇒ ENABLED (the default —
#: protecting the shared account is the safe posture); off-token ⇒ DISABLED.
MARKET_HOURS_GUARD_ENV: Final[str] = "BRUTEX_MARKET_HOURS_GUARD"

#: ENABLE tokens (case-insensitive, whitespace-stripped). ``""`` (unset) is ENABLED.
_ENV_ON_TOKENS: Final[frozenset[str]] = frozenset({"", "1", "on", "true", "yes"})

#: DISABLE tokens (the operator escape hatch — e.g. a deliberate off-hours-only
#: manual pull override, or a test host). Anything else raises (junk is never
#: silently coerced — the DEC-RAM-BUDGETS-001 fail-loud env convention).
_ENV_OFF_TOKENS: Final[frozenset[str]] = frozenset({"0", "off", "false", "no"})


def pull_allowed_now(ist_dt: datetime, is_trading_day_flag: bool) -> bool:
    """Return ``True`` iff a VENDOR pull is allowed at *ist_dt* — the PURE predicate.

    A pull is allowed unless *is_trading_day_flag* is ``True`` AND *ist_dt*'s IST
    wall-clock time is within ``[BLOCK_START, BLOCK_END)``. On a non-trading day
    (weekend / holiday) a pull is ALWAYS allowed regardless of the time. Pure,
    O(1), no I/O — the sole place the window boundary law lives.

    Parameters
    ----------
    ist_dt
        The instant to test, as an IST wall-clock ``datetime`` (only its
        ``.time()`` is read — the caller resolves the date's trading-day status).
    is_trading_day_flag
        Whether *ist_dt*'s calendar date is an NSE trading day (the caller decides
        via :func:`~brutex.calendars.nse.is_trading_day`, so this predicate stays
        pure + clockless).

    Returns
    -------
    bool
        ``True`` when a pull may proceed; ``False`` when it is inside the
        market-hours window on a trading day.
    """
    if not is_trading_day_flag:
        return True
    return not (BLOCK_START <= ist_dt.time() < BLOCK_END)


def market_hours_blocked_now(ist_dt: datetime, is_trading_day_flag: bool) -> bool:
    """Convenience negation of :func:`pull_allowed_now` — ``True`` iff pulls are BLOCKED."""
    return not pull_allowed_now(ist_dt, is_trading_day_flag)


def pull_allowed_default(*, now: datetime | None = None) -> bool:
    """Bind the live IST clock + NSE calendar to :func:`pull_allowed_now`.

    Resolves *now* (default :func:`~brutex.calendars.nse.now_ist`) and its
    trading-day status via the NON-raising
    :func:`~brutex.calendars.nse.is_trading_day` — so an out-of-holiday-coverage
    weekday reads as a trading day and a pull is BLOCKED during market hours (the
    fail-safe posture; a weekend reads non-trading → allowed). *now* is injectable
    for tests (a frozen IST instant).

    Parameters
    ----------
    now
        Optional IST-aware ``datetime`` override (tests); ``None`` reads the live
        :func:`~brutex.calendars.nse.now_ist`.

    Returns
    -------
    bool
        ``True`` when a vendor pull may proceed at *now*.
    """
    ist_dt = now_ist() if now is None else now
    return pull_allowed_now(ist_dt, is_trading_day(ist_dt.date()))


def resolve_market_hours_guard_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the ``BRUTEX_MARKET_HOURS_GUARD`` kill switch — fail-loud.

    unset / ``""`` / ``"1"`` / ``"on"`` / ``"true"`` / ``"yes"`` (case-insensitive,
    whitespace-stripped) → ``True`` (default-ENABLED: protecting the shared Groww
    account from a market-hours 429 storm is the safe posture); ``"0"`` / ``"off"``
    / ``"false"`` / ``"no"`` → ``False`` (the operator escape hatch). Anything else
    raises — junk is never silently coerced to a default (the DEC-RAM-BUDGETS-001
    fail-loud env convention), so a typo can never SILENTLY disable the guard.

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    bool
        ``True`` when the guard is enabled.

    Raises
    ------
    ValueError
        If the value is set to anything outside the two token sets.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(MARKET_HOURS_GUARD_ENV, "")
    token = raw.strip().lower()
    if token in _ENV_ON_TOKENS:
        return True
    if token in _ENV_OFF_TOKENS:
        return False
    raise ValueError(
        f"{MARKET_HOURS_GUARD_ENV}={raw!r} is not a recognised switch value; "
        f"expected one of '1', 'on', 'true', 'yes' (enable — the default when unset) "
        f"or '0', 'off', 'false', 'no' (disable)"
    )


def guard_active_default(
    *, now: datetime | None = None, env: Mapping[str, str] | None = None
) -> bool:
    """Return ``True`` iff the guard is ENABLED AND a pull is BLOCKED right now.

    The ONE convenience the start gates + the production spine-default wiring
    consult: ``resolve_market_hours_guard_enabled(env) and not
    pull_allowed_default(now=now)``. When the guard is disabled it returns
    ``False`` (pulls always proceed); when enabled it returns ``True`` exactly
    inside the market-hours window on a trading day. Both *now* and *env* are
    injectable for tests.
    """
    if not resolve_market_hours_guard_enabled(env):
        return False
    return not pull_allowed_default(now=now)


def _market_never_blocked() -> bool:
    """A bound 'never blocked' predicate — the guard is disabled."""
    return False


def _market_clock_blocked() -> bool:
    """A bound 'blocked?' predicate reading ONLY the live clock (no env re-parse)."""
    return not pull_allowed_default()


def resolve_market_closed(*, env: Mapping[str, str] | None = None) -> Callable[[], bool]:
    """Resolve the env kill switch ONCE (fail-loud) → a clock-only ``blocked?`` predicate.

    The MID-RUN wiring seam (DEC-GROWW-MARKET-HOURS-GUARD-001): every production pull
    that threads a per-window / per-cell / between-rounds guard binds THIS at pull
    START — the ``BRUTEX_MARKET_HOURS_GUARD`` env is parsed exactly ONCE, here (still
    fail-LOUD on junk, at start, before any vendor call), and the returned callable
    reads ONLY the live clock + NSE calendar per call (:func:`pull_allowed_default`).
    So a junk env value set MID-pull can NEVER crash a long-running pull with a
    per-call ``ValueError`` — the only parse happened at start (contrast
    :func:`guard_active_default`, which re-reads the env on every call and is used
    ONLY for the one-shot START gates).

    Disabled → :func:`_market_never_blocked` (always ``False``); enabled →
    :func:`_market_clock_blocked` (``True`` iff currently in-window on a trading day).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ`` ONCE.

    Returns
    -------
    Callable[[], bool]
        A clock-only ``blocked?`` predicate (env already resolved).

    Raises
    ------
    ValueError
        If the env value is junk — raised HERE, at pull start, never mid-pull.
    """
    if resolve_market_hours_guard_enabled(env):
        return _market_clock_blocked
    return _market_never_blocked


@dataclass(frozen=True, slots=True)
class MarketHoursGuard:
    """A tiny ``now``-injectable guard consulted at the mid-run per-window seams.

    Wraps a zero-arg :attr:`clock` (default :func:`~brutex.calendars.nse.now_ist`)
    and a resolved :attr:`enabled` flag so a long-running pull can cheaply re-check
    "has the market opened?" per window without re-reading the env each time.
    Dependency-light by design (it imports only the calendar leaf) so both ingest
    spines can consult it without pulling in the dashboard / adapter chain.

    :meth:`blocked` is the whole surface — ``True`` iff the guard is enabled AND the
    live (or injected) clock is inside the market-hours window on a trading day.
    """

    #: Whether the guard is active at all (default resolves the env kill switch
    #: ONCE at construction — the per-window :meth:`blocked` call is then a pure
    #: clock read, never a repeated env parse).
    enabled: bool = True
    #: The IST clock source (default the live leaf); tests inject a frozen /
    #: advancing clock to drive the boundary.
    clock: Callable[[], datetime] = now_ist

    @classmethod
    def from_env(
        cls, *, clock: Callable[[], datetime] | None = None, env: Mapping[str, str] | None = None
    ) -> MarketHoursGuard:
        """Build a guard whose :attr:`enabled` is resolved from the env kill switch.

        *clock* defaults to the live :func:`~brutex.calendars.nse.now_ist`; tests
        inject their own. *env* is forwarded to
        :func:`resolve_market_hours_guard_enabled` (fail-loud on junk).
        """
        return cls(
            enabled=resolve_market_hours_guard_enabled(env),
            clock=now_ist if clock is None else clock,
        )

    def blocked(self) -> bool:
        """Return ``True`` iff pulls are currently BLOCKED (enabled + in-window)."""
        if not self.enabled:
            return False
        ist_dt = self.clock()
        return market_hours_blocked_now(ist_dt, is_trading_day(ist_dt.date()))


# DEC-GROWW-MARKET-HOURS-GUARD-001 — re-snapshot marker (PR-body event-snapshot gate).
