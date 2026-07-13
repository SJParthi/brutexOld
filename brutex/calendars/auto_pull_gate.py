"""The ``brutex pull-all-spot --auto`` gate — settle time + Muhurat deferral.

DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779). The pure decision layer
an UNATTENDED (cron/launchd) ``brutex pull-all-spot --auto`` invocation runs
BEFORE any lock/vendor work: pull only on an NSE trading day, only AFTER the
post-close vendor settle time, and never on a Muhurat-session day. Plus the
launcher-level ``compose_market_closed`` mid-run Muhurat belt — the
trading-day-keyed market-hours guard is structurally BLIND to a Muhurat
session (a Muhurat date is a listed holiday, so :func:`~brutex.calendars.nse.
is_trading_day` reads False and the guard never blocks), so the spot-only arm
COMPOSES a session-window predicate over the guard's own resolved callable.
The guard module (:mod:`brutex.calendars.market_hours`) is byte-UNTOUCHED —
composed, never edited.

Honest envelope (Golden Rule #10): the settle default is a NAMED CONSERVATIVE
GUESS — the #1775 post-close settle probe is UNRUN, so the vendor's true
settle time is UNMEASURED; 17:30 IST is the probe's LAST slot
(``docs/groww/probe/postclose_settle_probe.py`` ``_SLOTS``). Re-derive once
the probe matrix lands (``docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md``). A
mis-set settle time costs bounded deferral churn, never wrong data — the
today-completeness gate + the prev-day end bound are the data safety net.
``allowed`` NEVER bypasses the market-hours guard: the launcher still binds
``resolve_market_closed()``; this gate is strictly ADDITIONAL for ``--auto``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Final, overload

import structlog

from brutex.calendars.market_hours import BLOCK_END, pull_allowed_default
from brutex.calendars.nse import (
    MUHURAT_SESSIONS,
    has_muhurat_session,
    is_holiday_data_available,
    is_trading_day,
    now_ist,
)

_LOG = structlog.get_logger("brutex.calendars.auto_pull_gate")

__all__ = [
    "DEFAULT_PULL_ALL_SETTLE_IST",
    "MUHURAT_POST_MARGIN",
    "MUHURAT_PRE_MARGIN",
    "PULL_ALL_SETTLE_ENV",
    "AutoGateVerdict",
    "auto_pull_allowed",
    "compose_market_closed",
    "muhurat_session_active",
    "resolve_settle_time",
]

#: CONSERVATIVE default post-close settle time (IST wall clock) — the #1775
#: settle probe is UNRUN; 17:30 = the probe's LAST slot
#: (docs/groww/probe/postclose_settle_probe.py ``_SLOTS``). Re-derive once the
#: probe matrix lands (docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md).
DEFAULT_PULL_ALL_SETTLE_IST: Final[time] = time(17, 30)

#: The feature-family env knob (B's naming — survives a Phase-2
#: ``pull-all-fno`` sibling unchanged). Strict ``HH:MM``; fail-loud on junk.
PULL_ALL_SETTLE_ENV: Final[str] = "BRUTEX_PULL_ALL_SETTLE_IST"

#: Strict ``HH:MM`` shape — exactly two digits each side (never a lenient
#: parse; a junk knob must fail loud, DEC-RAM-BUDGETS-001 env convention).
_SETTLE_SHAPE: Final[re.Pattern[str]] = re.compile(r"^(\d{2}):(\d{2})$")


def resolve_settle_time(env: Mapping[str, str] | None = None) -> time:
    """Resolve the post-close settle time ONCE — strict, fail-loud, floored.

    Unset / blank → :data:`DEFAULT_PULL_ALL_SETTLE_IST`. A set value must be
    strict ``HH:MM`` (two digits each, a real wall-clock time) — anything else
    raises :class:`ValueError` (junk is never silently coerced). STRUCTURAL
    refusal: a value BEFORE the market-hours guard's ``BLOCK_END`` (15:35 IST)
    is refused naming the guard — a pre-close "settle" would just bounce off
    the untouched market-hours gate; exactly 15:35 is ALLOWED (boundary
    pinned). Resolved once per invocation — never mid-run.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(PULL_ALL_SETTLE_ENV, "")
    token = raw.strip()
    if not token:
        return DEFAULT_PULL_ALL_SETTLE_IST
    match = _SETTLE_SHAPE.match(token)
    if match is None:
        raise ValueError(
            f"{PULL_ALL_SETTLE_ENV}={raw!r} is not a strict HH:MM IST wall-clock "
            f"time (e.g. '17:30')"
        )
    try:
        value = time(int(match.group(1)), int(match.group(2)))
    except ValueError as exc:  # out-of-range hour/minute (e.g. "25:99")
        raise ValueError(
            f"{PULL_ALL_SETTLE_ENV}={raw!r} is not a real wall-clock time "
            f"(hour 00-23, minute 00-59)"
        ) from exc
    if value < BLOCK_END:
        raise ValueError(
            f"{PULL_ALL_SETTLE_ENV}={raw!r} is before the market-hours guard's "
            f"BLOCK_END {BLOCK_END:%H:%M} IST — a pre-close settle time would "
            f"just bounce off the untouched market-hours gate "
            f"(brutex.calendars.market_hours); use {BLOCK_END:%H:%M} or later"
        )
    return value


@dataclass(frozen=True, slots=True)
class AutoGateVerdict:
    """One ``--auto`` gate decision: allowed + the machine reason + human text."""

    allowed: bool
    #: ``"ok"`` | ``"non_trading_day"`` | ``"muhurat_deferral"`` |
    #: ``"market_hours_blocked"`` | ``"before_settle"``
    reason: str
    detail: str


def auto_pull_allowed(
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> AutoGateVerdict:
    """The whole ``--auto`` decision — pure given an injected *now*.

    *now* defaults to the live :func:`~brutex.calendars.nse.now_ist`; *env* is
    parsed ONCE via :func:`resolve_settle_time` (junk RAISES out — the caller
    fails loud BEFORE any lock/vendor work). Decision order:

    1. not a trading day → ``non_trading_day`` (the NON-raising
       :func:`~brutex.calendars.nse.is_trading_day`: every listed Muhurat
       date is a holiday, so it defers HERE; a weekend defers; an
       OUT-OF-COVERAGE weekday (≥2027) reads as trading and PROCEEDS with ONE
       ``auto_pull_gate.calendar_coverage_exhausted`` WARN — ruling 10: the
       zero-touch product stays alive past the calendar horizon, account
       safety preserved by the untouched in-window market-hours guard; a true
       unlisted holiday costs bounded EMPTY churn, never wrong data);
    2. :func:`~brutex.calendars.nse.has_muhurat_session` → ``muhurat_deferral``
       (the belt for a future TRADING-day Muhurat listing);
    3. ``now.time() < resolve_settle_time(env)`` → ``before_settle``;
    4. not :func:`~brutex.calendars.market_hours.pull_allowed_default` →
       ``market_hours_blocked`` (a BELT — structurally unreachable while the
       settle floor is ≥ 15:35 = ``BLOCK_END``, since every in-window minute
       is already ``before_settle``; kept so a future settle-law change can
       never open a hole);
    5. else ``ok``.
    """
    settle = resolve_settle_time(env)
    ist_now = now_ist() if now is None else now
    d = ist_now.date()
    if not is_trading_day(d):
        return AutoGateVerdict(
            allowed=False,
            reason="non_trading_day",
            detail=f"{d.isoformat()} is not an NSE trading day — nothing new to pull",
        )
    if not is_holiday_data_available(d):
        # Ruling 10: coverage-exhausted weekday PROCEEDS post-settle, loudly.
        _LOG.warning(
            "auto_pull_gate.calendar_coverage_exhausted",
            date=d.isoformat(),
            detail=(
                "holiday list does not cover this year — treating the weekday as "
                "a trading day (add the new NSE holiday circular; a true holiday "
                "costs bounded EMPTY churn, never wrong data)"
            ),
        )
    if has_muhurat_session(d):
        return AutoGateVerdict(
            allowed=False,
            reason="muhurat_deferral",
            detail=(
                f"{d.isoformat()} has a Muhurat session — deferred to the next "
                f"regular trading day (evening-session settle behavior unverified)"
            ),
        )
    if ist_now.time() < settle:
        return AutoGateVerdict(
            allowed=False,
            reason="before_settle",
            detail=(
                f"waiting for post-close settle time {settle:%H:%M} IST "
                f"(conservative default 17:30 — the vendor's true settle time is "
                f"UNMEASURED; see docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md; "
                f"override {PULL_ALL_SETTLE_ENV})"
            ),
        )
    if not pull_allowed_default(now=ist_now):
        return AutoGateVerdict(
            allowed=False,
            reason="market_hours_blocked",
            detail="inside the NSE market-hours pull window — deferred",
        )
    return AutoGateVerdict(allowed=True, reason="ok", detail="ok")


# ── Muhurat mid-run arm (COMPOSITION — the guard module is untouched) ────────

#: Session-window margins mirroring the guard's own envelope: the block opens
#: 30 min before the regular session (08:45 = 09:15 − 30) and closes 5 min
#: after it (15:35 = 15:30 + 5) — the SAME margins around a Muhurat session.
MUHURAT_PRE_MARGIN: Final[timedelta] = timedelta(minutes=30)
MUHURAT_POST_MARGIN: Final[timedelta] = timedelta(minutes=5)


def muhurat_session_active(ist_dt: datetime) -> bool:
    """True iff *ist_dt* sits inside a LISTED Muhurat session's margined window.

    Pure/clockless: ``open − 30 min <= t < close + 5 min`` over the date's
    :data:`~brutex.calendars.nse.MUHURAT_SESSIONS` entry. A holiday-listed
    Diwali date with NO session entry (2026 — timings not yet notified)
    HONESTLY returns ``False`` — timings are never fabricated (Golden Rule #1;
    the disclosed residual: a long run crossing an UNLISTED evening session is
    not belted until the calendar entry lands).
    """
    session = MUHURAT_SESSIONS.get(ist_dt.date())
    if session is None:
        return False
    open_t, close_t = session
    day = ist_dt.date()
    start = datetime.combine(day, open_t, tzinfo=ist_dt.tzinfo) - MUHURAT_PRE_MARGIN
    end = datetime.combine(day, close_t, tzinfo=ist_dt.tzinfo) + MUHURAT_POST_MARGIN
    return start <= ist_dt < end


# The two ``@overload`` stubs are typing-only declarations (the
# sweep_lookahead idiom): their def lines execute once at import but the stub
# bodies can never run — docstring bodies + a def-line pragma keep them
# outside the measured surface; the real implementation follows.
@overload
def compose_market_closed(  # pragma: no cover
    base: None, *, clock: Callable[[], datetime] | None = None
) -> None:
    """Overload stub: a ``None`` base (dry-run) composes to ``None``."""


@overload
def compose_market_closed(  # pragma: no cover
    base: Callable[[], bool], *, clock: Callable[[], datetime] | None = None
) -> Callable[[], bool]:
    """Overload stub: a resolved predicate composes to a predicate."""


def compose_market_closed(
    base: Callable[[], bool] | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[], bool] | None:
    """Compose the Muhurat session belt OVER the resolved market-hours predicate.

    ``None`` → ``None`` (dry-run parity — no vendor calls to gate). Otherwise
    returns ``lambda: base() or muhurat_session_active(clock())`` — the
    launcher-level belt the trading-day-keyed guard is structurally blind to
    (a Muhurat date is a listed holiday ⇒ the guard reads non-trading ⇒
    inert). COMPOSES the guard's own resolved callable; never edits the guard
    module. *clock* defaults to :func:`~brutex.calendars.nse.now_ist`.
    """
    if base is None:
        return None
    tick = now_ist if clock is None else clock

    def _composed() -> bool:
        return base() or muhurat_session_active(tick())

    return _composed
