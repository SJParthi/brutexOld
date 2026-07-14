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
composed, never edited. The belt honors the guard's OWN
``BRUTEX_MARKET_HOURS_GUARD`` kill switch (guard off ⇒ belt off — one switch
governs the shared-account protection family; hostile-review r1 ruling).

Settle resolution is the THREE-TIER law (DEC-SETTLE-EVIDENCE-ADOPTION-001,
MIGRATION.md #1793): explicit ``BRUTEX_PULL_ALL_SETTLE_IST`` (fail-loud,
strict HH:MM — the tier-1 short-circuit, evidence never read) > VALIDATED
committed probe evidence (:mod:`brutex.calendars.settle_evidence` — the
#1775 probe's ``--emit-evidence`` artifact) > the conservative 17:30 default.
Honest envelope (Golden Rule #10): the DEFAULT tier is a NAMED CONSERVATIVE
GUESS — until a measured evidence file is committed, the vendor's true
settle time is UNMEASURED; 17:30 IST is the probe's LAST slot
(``docs/groww/probe/postclose_settle_probe.py`` ``_SLOTS``). The evidence
tier replaces the guess ONLY with a validated ≥5-regular-day probe matrix
(``docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md``). A
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

from brutex.calendars.market_hours import (
    BLOCK_END,
    pull_allowed_default,
    resolve_market_hours_guard_enabled,
)
from brutex.calendars.nse import (
    MUHURAT_SESSIONS,
    has_muhurat_session,
    is_holiday_data_available,
    is_trading_day,
    now_ist,
)
from brutex.calendars.settle_evidence import evidence_settle

_LOG = structlog.get_logger("brutex.calendars.auto_pull_gate")

__all__ = [
    "DEFAULT_PULL_ALL_SETTLE_IST",
    "MUHURAT_POST_MARGIN",
    "MUHURAT_PRE_MARGIN",
    "PULL_ALL_SETTLE_ENV",
    "AutoGateVerdict",
    "SettleResolution",
    "auto_pull_allowed",
    "compose_market_closed",
    "muhurat_session_active",
    "resolve_settle_resolution",
    "resolve_settle_time",
]

#: CONSERVATIVE default post-close settle time (IST wall clock) — tier 3 of
#: the DEC-SETTLE-EVIDENCE-ADOPTION-001 resolution law (env > evidence >
#: this default): while no measured evidence file is committed the vendor's
#: settle time stays UNMEASURED and 17:30 = the #1775 probe's LAST slot
#: (docs/groww/probe/postclose_settle_probe.py ``_SLOTS``); the probe's
#: ``--emit-evidence`` matrix is the measured replacement channel
#: (docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md).
DEFAULT_PULL_ALL_SETTLE_IST: Final[time] = time(17, 30)

#: The feature-family env knob (B's naming — survives a Phase-2
#: ``pull-all-fno`` sibling unchanged). Strict ``HH:MM``; fail-loud on junk.
PULL_ALL_SETTLE_ENV: Final[str] = "BRUTEX_PULL_ALL_SETTLE_IST"

#: Strict ``HH:MM`` shape — exactly two ASCII digits each side (never a
#: lenient parse; a junk knob must fail loud, DEC-RAM-BUDGETS-001 env
#: convention). ``re.ASCII`` per the DEC-LAKE-STATS-MANIFEST-001 round-3
#: precedent: Unicode ``\d`` would silently accept e.g. ``"١٧:٣٠"``.
_SETTLE_SHAPE: Final[re.Pattern[str]] = re.compile(r"^(\d{2}):(\d{2})$", re.ASCII)


def _parse_settle_token(raw: str) -> time:
    """The strict ``HH:MM`` parse + the ≥ ``BLOCK_END`` floor — fail-loud.

    The VERBATIM tier-1 parse body (identical error messages — every junk pin
    stays green): shape, real-wall-clock range, then the STRUCTURAL floor —
    a value BEFORE the market-hours guard's ``BLOCK_END`` (15:35 IST) is
    refused naming the guard; exactly 15:35 is ALLOWED (boundary pinned).
    """
    token = raw.strip()
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
class SettleResolution:
    """One settle resolution: the value + WHICH tier decided + honest provenance.

    ``source`` ∈ ``{"env", "evidence", "default"}``; ``provenance`` is the
    fully-formed parenthetical clause the ``before_settle`` deferral detail
    renders (the DEFAULT arm's clause is byte-identical to the pre-#1793
    wording — the verbatim UNMEASURED honesty label; Golden Rule #10 both
    directions: a measured value never reads UNMEASURED, an override never
    claims conservatism the operator replaced).
    """

    value: time
    source: str
    provenance: str


def resolve_settle_resolution(env: Mapping[str, str] | None = None) -> SettleResolution:
    """Resolve the settle time ONCE via the three-tier law — fail-loud, floored.

    Tier 1 — a set (non-blank) :data:`PULL_ALL_SETTLE_ENV` parses strictly
    (:func:`_parse_settle_token`; junk raises) and SHORT-CIRCUITS: the
    evidence channel is never read (the ``--settle-ist`` flag rides this tier
    via the CLI's env_map merge, so the flag ALWAYS wins over a present
    evidence file). Tier 2 — a validated committed evidence artifact
    (:func:`~brutex.calendars.settle_evidence.evidence_settle`; an explicit
    junk ``BRUTEX_SETTLE_EVIDENCE_PATH`` raises out — the resolve_* law;
    default-path corruption WARNs once and falls through). Tier 3 —
    :data:`DEFAULT_PULL_ALL_SETTLE_IST` with the verbatim UNMEASURED
    provenance. Resolved once per invocation — never mid-run. Cost honesty
    (r1 MINOR-2): tiers 2-3 perform ONE bounded dentry-cached ``os.stat``
    probe of the evidence path per resolution (memoized on the stat token —
    the 60 s scheduler tick + status renders pay one probe each); the tier-1
    env short-circuit performs ZERO filesystem work.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(PULL_ALL_SETTLE_ENV, "")
    if raw.strip():
        return SettleResolution(
            value=_parse_settle_token(raw),
            source="env",
            provenance=f"operator override {PULL_ALL_SETTLE_ENV}",
        )
    evidence = evidence_settle(mapping)
    if evidence is not None:
        return SettleResolution(
            value=evidence.adopted,
            source="evidence",
            provenance=(
                f"measured — probe evidence: earliest all-pass "
                f"{evidence.earliest_all_pass} IST + {evidence.margin_minutes} min "
                f"margin over {evidence.distinct_trading_days} trading days; "
                f"override {PULL_ALL_SETTLE_ENV}"
            ),
        )
    return SettleResolution(
        value=DEFAULT_PULL_ALL_SETTLE_IST,
        source="default",
        provenance=(
            f"conservative — the vendor's true settle time is "
            f"UNMEASURED; see docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md; "
            f"override {PULL_ALL_SETTLE_ENV}"
        ),
    )


def resolve_settle_time(env: Mapping[str, str] | None = None) -> time:
    """The settle VALUE — a thin wrapper over :func:`resolve_settle_resolution`.

    Signature + name unchanged (every consumer — the scheduler, both cli.py
    call sites — rides the three-tier law with zero mechanical churn).
    """
    return resolve_settle_resolution(env).value


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
    parsed ONCE via :func:`resolve_settle_resolution` (junk RAISES out — the
    caller fails loud BEFORE any lock/vendor work). Decision order:

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
    3. ``now.time() < resolve_settle_resolution(env).value`` →
       ``before_settle`` (the three-tier law: env > validated committed
       evidence > the conservative 17:30 default; the deferral detail carries
       the resolved tier's honest provenance);
    4. not :func:`~brutex.calendars.market_hours.pull_allowed_default` →
       ``market_hours_blocked`` (a BELT — structurally unreachable while the
       settle floor is ≥ 15:35 = ``BLOCK_END``, since every in-window minute
       is already ``before_settle``; kept so a future settle-law change can
       never open a hole);
    5. else ``ok``.
    """
    res = resolve_settle_resolution(env)
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
    if ist_now.time() < res.value:
        # The provenance clause is per-tier (Golden Rule #10): the DEFAULT
        # arm's rendered detail is BYTE-IDENTICAL to the pre-#1793 wording.
        return AutoGateVerdict(
            allowed=False,
            reason="before_settle",
            detail=(f"waiting for post-close settle time {res.value:%H:%M} IST ({res.provenance})"),
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
    base: None,
    *,
    clock: Callable[[], datetime] | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Overload stub: a ``None`` base (dry-run) composes to ``None``."""


@overload
def compose_market_closed(  # pragma: no cover
    base: Callable[[], bool],
    *,
    clock: Callable[[], datetime] | None = None,
    env: Mapping[str, str] | None = None,
) -> Callable[[], bool]:
    """Overload stub: a resolved predicate composes to a predicate."""


def compose_market_closed(
    base: Callable[[], bool] | None,
    *,
    clock: Callable[[], datetime] | None = None,
    env: Mapping[str, str] | None = None,
) -> Callable[[], bool] | None:
    """Compose the Muhurat session belt OVER the resolved market-hours predicate.

    ``None`` → ``None`` (dry-run parity — no vendor calls to gate). The belt
    honors the SAME ``BRUTEX_MARKET_HOURS_GUARD`` kill switch as the guard it
    composes over (hostile-review r1 ruling: ONE switch governs the whole
    shared-account protection family — guard off ⇒ belt off): when the switch
    is disabled, *base* is returned UNCHANGED (no Muhurat term). The switch is
    resolved ONCE here at composition time (the ``resolve_market_closed``
    parse-once posture — junk fails loud at pull start, never mid-run; a
    junk value written AFTER composition is never re-read — no-re-read-pinned;
    *env* ``None`` reads ``os.environ``). Otherwise returns the semantic
    ``base() or muhurat_session_active(clock())`` — the launcher-level
    belt the trading-day-keyed guard is structurally blind to (a Muhurat date
    is a listed holiday ⇒ the guard reads non-trading ⇒ inert). COMPOSES the
    guard's own resolved callable; never edits the guard module. *clock*
    defaults to :func:`~brutex.calendars.nse.now_ist`.

    MACHINE ATTRIBUTION (refuter round 1, r2 MINOR-1): when the MUHURAT arm
    fires (base False, session window True) the composed predicate emits ONE
    ``auto_pull_gate.muhurat_belt_halt`` WARN per composed instance — the
    downstream deferral machinery (``MarketHoursHalt.reason`` /
    ``stopped_after="market_hours"``) is a SHARED contract and stays
    byte-untouched, so this event is the log-side tell-apart from a genuine
    market-hours deferral.
    """
    if base is None:
        return None
    if not resolve_market_hours_guard_enabled(env):
        # Kill switch off ⇒ the composed predicate defers ENTIRELY to base
        # (which the guard resolved to always-False) — the belt never arms.
        return base

    belt_logged = False

    def _composed() -> bool:
        # ``now_ist`` resolved at CALL time (module-global lookup) so the live
        # clock stays current and tests can freeze it after composition.
        nonlocal belt_logged
        tick = now_ist if clock is None else clock
        if base():
            return True  # the guard's own arm — attribution is already honest
        instant = tick()
        if not muhurat_session_active(instant):
            return False
        if not belt_logged:
            # Capped ONCE per composed instance (a mid-run halt consults the
            # predicate once per window — one loud line, never a storm).
            belt_logged = True
            _LOG.warning(
                "auto_pull_gate.muhurat_belt_halt",
                instant=instant.isoformat(),
                detail=(
                    "the Muhurat session belt (NOT the 08:45-15:35 market-hours "
                    "window) is deferring this spot-only pull"
                ),
            )
        return True

    return _composed
