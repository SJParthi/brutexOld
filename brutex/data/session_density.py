"""WARN-only per-session BAR-DENSITY census (DEC-SESSION-DENSITY-CENSUS-001).

Why this module exists
----------------------
The existing completeness detectors are all SESSION-set-granular: the
interior-gap detector (:mod:`brutex.data.interior_gap_detect`) flags only a
``> 5``-session contiguous run of bar-LESS sessions, and the partial-window
detector (:mod:`brutex.data.partial_window_detect`) watches only the trailing
edge's temporal coverage. NEITHER counts bars WITHIN a session — so a session
that is present-but-hollow (a 45-minute .. 4-hour intraday hole, the exact
shape the 2021-02-24 NSE outage day leaves behind, or a 1-5-session missing
run below interior-gap's tolerance) is invisible today. This module is the
DENSITY twin: it compares each interior session's observed bar count against
the session-AWARE expectation
(:func:`brutex.calendars.nse.expected_bars_for_session` — 375 on a regular
day, 465 on the 2021-02-24 extended day, 60 on a Muhurat hour) and WARNs on a
material deficit. It is OBSERVE-ONLY by decree: it never touches
``WindowLedger.record`` (no demotion, no re-pull action — spy-pinned), never
mutates a bar, and any internal failure is swallowed + logged so it can NEVER
block / slow / crash a pull.

False-positive safety (read before touching a threshold)
---------------------------------------------------------
* **Deficiency law** — a session flags iff BOTH
  ``expected - observed >= SESSION_DENSITY_ABS_FLOOR`` (never flag fewer than
  30 missing 1-minute bars — the noise floor) AND
  ``observed < ceil(expected * SESSION_DENSITY_MIN_FRACTION)`` (default 0.90).
  On a regular 375-bar day a 45-minute hole (330 < 338) flags while a
  30-minute hole (345 >= 338) deliberately passes — the target class is the
  45-min .. 4-hour hole, not minute-level jitter.
* **Abstains** (each counted, never silently dropped): ``out_of_coverage``
  (the NSE holiday list cannot vouch for the date — checked FIRST, the
  interior-gap precedent), ``non_trading`` (no session / observed-overlay
  day), ``muhurat`` (a ceremonial hour's density is not comparable),
  ``forming_session`` (today's session is still printing), and ``boundary``
  (the window's first/last covered date — a mid-day-clamped or chunk-tiled
  window edge must never false-flag as a hole).
* **Judgment set** — every date STRICTLY interior to the covered span,
  INCLUDING interior zero-bar trading days (a fully-missing single session
  flags at fraction 0.0 — closing the 1-5-session hole invisible to
  interior-gap's tolerance-5; a >= 6-session hole double-flags with
  ``window.interior_gap``, an accepted disclosed overlap).
* **Known-irregular INFO arm** — a deficit on an annotated date
  (:func:`brutex.calendars.nse.is_known_irregular_session` — the 2020
  circuit-halt days + the 2021-02-24 extended day) reads INFO, never WARN:
  on 2021-02-24 this is the standing "375/465 — re-pull this window" line
  telling the operator the extended bars are absent; on a halt day the
  deficit is the documented market fact (no bars print during a halt).

Cost honesty (Golden Rule #10)
------------------------------
O(kept bars in the window) + O(days in the covered span) per window at the
ingest tail, with ZERO additional lake I/O — the bars are already in memory
when the census runs; the sweep-preflight note is O(already-loaded slice
bars). The #1614 lake stats manifest is deliberately NOT consumed — its
per-month-file granularity cannot answer per-session counts.

Everything here is pure modulo the documented fail-soft env reads (the kill
switch inside :func:`session_density_census_enabled` — never the classifier —
plus the two threshold tunables the classifier reads only when the matching
override is not injected), stdlib + structlog + :mod:`brutex.calendars.nse`
only (the observed-sessions layering law).
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Final

from brutex.calendars.nse import (
    CIRCUIT_HALT_REASON,
    IST,
    TradingSession,
    expected_bars_for_session,
    get_session,
    has_muhurat_session,
    is_holiday_data_available,
    is_known_irregular_session,
    known_irregular_reason,
    now_ist,
)

__all__ = [
    "SESSION_DENSITY_ABS_FLOOR",
    "SESSION_DENSITY_ERROR_EVENT",
    "SESSION_DENSITY_KILL_ENV",
    "SESSION_DENSITY_KNOWN_EVENT",
    "SESSION_DENSITY_LOW_EVENT",
    "SESSION_DENSITY_MIN_FRACTION",
    "SESSION_DENSITY_SUPPRESSED_EVENT",
    "SESSION_DENSITY_WARN_CAP",
    "SHAPE_INTRADAY_HOLE",
    "SHAPE_MISSING_SESSION",
    "SHAPE_SPARSE",
    "SessionDensityFlag",
    "SessionDensityVerdict",
    "classify_session_density",
    "observe_session_density",
    "session_density_census_enabled",
]

#: A session flags only when its observed bar count is BELOW this fraction of
#: the session-aware expectation (``observed < ceil(expected * fraction)``).
#: Env-tunable via ``BRUTEX_SESSION_DENSITY_MIN_FRACTION`` (fail-soft: a
#: missing / malformed / out-of-(0, 1] value falls back to this default — the
#: diagnostic never crashes a pull over a bad env var).
SESSION_DENSITY_MIN_FRACTION: Final[float] = 0.90

#: Absolute noise floor: never flag a session missing FEWER than this many
#: bars, whatever the fraction says. Env-tunable via
#: ``BRUTEX_SESSION_DENSITY_ABS_FLOOR`` (same fail-soft law; non-positive /
#: malformed → default).
SESSION_DENSITY_ABS_FLOOR: Final[int] = 30

#: Environment kill switch. Default ON; a falsy value ("0"/"false"/"no"/
#: "off") disables ALL census output (the ``bad_tick`` kill-switch pattern):
#: BOTH the ingest-tail emitter (:func:`observe_session_density` checks it
#: FIRST) and the sweep-preflight note consumption (``cmd_sweep_all`` gates
#: its ``preflight_session_density`` call on
#: :func:`session_density_census_enabled`). The classifier
#: (:func:`classify_session_density`) itself stays un-gated BY DESIGN —
#: callers gate, so NO kill-switch read hides inside it (its two threshold
#: tunables remain documented fail-soft internal reads when not injected).
SESSION_DENSITY_KILL_ENV: Final[str] = "BRUTEX_SESSION_DENSITY_CENSUS"

#: Per-emit cap on ``window.session_density_low`` WARN lines (house cap 20 —
#: the #919 / preopen / movers precedent) + ONE suppressed summary carrying
#: the TRUE count, so the cap never lies (Golden Rule #1).
SESSION_DENSITY_WARN_CAP: Final[int] = 20

#: One WARNING per flagged low-density session (capped).
SESSION_DENSITY_LOW_EVENT: Final[str] = "window.session_density_low"
#: One INFO per deficient KNOWN-irregular session (halt / extended days).
SESSION_DENSITY_KNOWN_EVENT: Final[str] = "window.session_density_known_irregular"
#: One summary when an arm's per-session lines were capped (true count carried).
SESSION_DENSITY_SUPPRESSED_EVENT: Final[str] = "window.session_density_suppressed"
#: Emitted if the observe-only scan itself raises — it NEVER breaks the pull.
SESSION_DENSITY_ERROR_EVENT: Final[str] = "window.session_density_error"

#: Flag shape: an interior trading session with ZERO bars.
SHAPE_MISSING_SESSION: Final[str] = "missing_session"
#: Flag shape: the largest contiguous in-session gap is >= 30 minutes.
SHAPE_INTRADAY_HOLE: Final[str] = "intraday_hole"
#: Flag shape: a material deficit with no single dominant gap (scattered).
SHAPE_SPARSE: Final[str] = "sparse"

#: A single contiguous gap at/above this many minutes classifies the flag as
#: :data:`SHAPE_INTRADAY_HOLE` (below it, a flagged deficit reads ``sparse``).
_INTRADAY_HOLE_MIN_MINUTES: Final[int] = 30

#: Falsy literals that DISABLE the (default-ON) census — the ``bad_tick`` /
#: ``volume_keepalive`` kill-switch vocabulary (CLAUDE.md §11 COMMON pattern).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Shape-matched operator remediation for the WARN arm (Golden Rule #1: the
#: remedy must match the evidence — a genuine hole re-pulls; scattered
#: sparsity on an index is a vendor-depth question, not a re-pull).
_SHAPE_REMEDIATION: Final[dict[str, str]] = {
    SHAPE_MISSING_SESSION: "re-pull this window (brutex reopen-skipped if settled)",
    SHAPE_INTRADAY_HOLE: "re-pull this window (brutex reopen-skipped if settled)",
    SHAPE_SPARSE: (
        "inspect — possible vendor 1m depth limit "
        "(CLAUDE.md §6: 2020-boundary granularity UNVERIFIED)"
    ),
}


def session_density_census_enabled(raw: str | None = None) -> bool:
    """Return whether the census is ENABLED (default ``True``).

    Parameters
    ----------
    raw
        Explicit value to interpret (tests). ``None`` reads
        :data:`SESSION_DENSITY_KILL_ENV` from the environment.

    Returns
    -------
    bool
        ``False`` only for an explicit falsy literal (``0``/``false``/``no``/
        ``off``, case-insensitive, whitespace-stripped); ``True`` for anything
        else — the census is ON by default (the ``bad_tick`` pattern).
    """
    value = os.environ.get(SESSION_DENSITY_KILL_ENV) if raw is None else raw
    return not (value is not None and value.strip().lower() in _FALSY)


def _tunable_fraction(env_var: str, default: float) -> float:
    """Read a ``(0, 1]``-bounded float tunable from *env_var*, else *default*.

    A missing, empty, non-float, non-finite, or out-of-``(0, 1]`` value falls
    back to *default* — the diagnostic must NEVER crash a pull over a
    malformed env var (the ``interior_gap_detect._tunable_sessions`` law).
    Pure modulo the one ``os.environ`` read.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or not 0.0 < value <= 1.0:
        return default
    return value


def _tunable_floor(env_var: str, default: int) -> int:
    """Read a positive-int bar floor from *env_var*, else *default* (fail-soft)."""
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True, slots=True)
class SessionDensityFlag:
    """One deficient session's facts — pure data.

    Attributes
    ----------
    session
        The IST trading date judged deficient.
    expected
        The session-aware expected bar count
        (:func:`~brutex.calendars.nse.expected_bars_for_session` — 465 on the
        2021-02-24 extended day, 375 on a regular day).
    observed
        Distinct in-session bar buckets present (duplicate timestamps /
        off-session strays never inflate density).
    fraction
        ``observed / expected`` (0.0 for a fully-missing session).
    largest_gap_minutes
        The longest contiguous run of missing in-session minutes (capped at
        the session duration).
    gap_start_ist / gap_end_ist
        IST wall-clock bounds of that largest gap (``[start, end)``), or
        ``None`` when no bucket is missing (unreachable on a flagged session
        — kept total for the frozen record's honesty).
    shape
        :data:`SHAPE_MISSING_SESSION` / :data:`SHAPE_INTRADAY_HOLE` /
        :data:`SHAPE_SPARSE`.
    known_irregular
        ``True`` iff the date is an annotated halt/extended session
        (:func:`~brutex.calendars.nse.is_known_irregular_session`) — routed to
        the INFO arm, never the WARN arm.
    """

    session: date
    expected: int
    observed: int
    fraction: float
    largest_gap_minutes: int
    gap_start_ist: time | None
    gap_end_ist: time | None
    shape: str
    known_irregular: bool


@dataclass(frozen=True, slots=True)
class SessionDensityVerdict:
    """The immutable result of one census pass.

    Attributes
    ----------
    flags
        Deficient sessions on UN-annotated dates (the WARN arm), ascending by
        date. The emit cap is applied at EMIT time, never here.
    known_irregular
        Deficient sessions on annotated halt/extended dates (the INFO arm).
    sessions_checked
        Interior sessions actually JUDGED (expected/observed computed).
    abstained
        ``((reason, count), ...)`` sorted by reason — every walked date that
        was NOT judged, so the judged + abstained sets partition the walk.
    """

    flags: tuple[SessionDensityFlag, ...]
    known_irregular: tuple[SessionDensityFlag, ...]
    sessions_checked: int
    abstained: tuple[tuple[str, int], ...]


def _largest_missing_run(present: AbstractSet[int], expected: int) -> tuple[int, int]:
    """The longest run of missing bucket slots in ``[0, expected)``.

    Returns ``(start_slot, end_slot)`` INCLUSIVE of the longest contiguous run
    of slots absent from *present* (ties: the earliest run wins —
    deterministic). O(expected). The caller must pre-check that at least one
    slot is missing (a deficient session always has one — ``deficit >= 1``);
    a gap-free input raises :class:`ValueError` loudly instead of fabricating
    a run.
    """
    best_len = 0
    best: tuple[int, int] | None = None
    run_start: int | None = None
    for slot in range(expected):
        if slot not in present:
            if run_start is None:
                run_start = slot
        elif run_start is not None:
            if slot - run_start > best_len:
                best_len = slot - run_start
                best = (run_start, slot - 1)
            run_start = None
    if run_start is not None and expected - run_start > best_len:
        best = (run_start, expected - 1)
    if best is None:
        raise ValueError("no missing bucket slot — caller must pre-check observed < expected")
    return best


def _judge_session(
    d: date,
    session: TradingSession,
    minutes: AbstractSet[int],
    *,
    tf_minutes: int,
    min_fraction: float,
    abs_floor: int,
) -> SessionDensityFlag | None:
    """Judge ONE interior session's density; return a flag iff deficient.

    *minutes* is the set of IST minute-of-day values with a bar on ``d``;
    off-session strays (pre-open / post-close) and duplicates never inflate
    the density (each in-session BUCKET counts once).
    """
    expected = expected_bars_for_session(session, tf_minutes)
    open_min = session.open_time_ist.hour * 60 + session.open_time_ist.minute
    duration = session.duration_minutes
    present = {(m - open_min) // tf_minutes for m in minutes if open_min <= m < open_min + duration}
    observed = len(present)
    deficit = expected - observed
    if deficit < abs_floor or observed >= math.ceil(expected * min_fraction):
        return None
    # A deficient session has ``deficit >= abs_floor >= 1`` missing buckets,
    # so the run finder is total here (its no-gap ValueError is unreachable).
    start_slot, end_slot = _largest_missing_run(present, expected)
    gap_minutes = min((end_slot - start_slot + 1) * tf_minutes, duration)
    open_dt = datetime.combine(d, session.open_time_ist)
    gap_start = (open_dt + timedelta(minutes=start_slot * tf_minutes)).time()
    gap_end_minutes = min((end_slot + 1) * tf_minutes, duration)
    gap_end = (open_dt + timedelta(minutes=gap_end_minutes)).time()
    if observed == 0:
        shape = SHAPE_MISSING_SESSION
    elif gap_minutes >= _INTRADAY_HOLE_MIN_MINUTES:
        shape = SHAPE_INTRADAY_HOLE
    else:
        shape = SHAPE_SPARSE
    return SessionDensityFlag(
        session=d,
        expected=expected,
        observed=observed,
        fraction=observed / expected,
        largest_gap_minutes=gap_minutes,
        gap_start_ist=gap_start,
        gap_end_ist=gap_end,
        shape=shape,
        known_irregular=is_known_irregular_session(d),
    )


def classify_session_density(
    bar_timestamps: Sequence[datetime],
    *,
    tf_minutes: int = 1,
    min_fraction: float | None = None,
    abs_floor: int | None = None,
    today_ist: date | None = None,
    observed: AbstractSet[date] = frozenset(),
) -> SessionDensityVerdict:
    """Classify one window's per-session bar density — pure.

    Walks every calendar date in the covered span ``[first_bar_date,
    last_bar_date]``, abstains per the module-docstring rules (in order:
    ``out_of_coverage`` → ``non_trading`` → ``muhurat`` → ``forming_session``
    → ``boundary``), and judges each remaining INTERIOR session — including
    interior zero-bar trading days — against the deficiency law. O(bars) to
    group + O(days in span) to walk + O(expected buckets) per judged session;
    zero I/O beyond the two fail-soft env reads, each taken only when its
    override parameter is not injected.

    Parameters
    ----------
    bar_timestamps
        UTC-aware timestamps of the window's kept bars (any order; naive
        timestamps are treated per ``astimezone`` semantics — the lake stores
        UTC-aware). Empty → an all-zero verdict.
    tf_minutes
        The bars' timeframe in whole minutes (``>= 1``; the production wiring
        is ``1minute``-only).
    min_fraction
        Override the deficiency fraction. ``None`` reads
        ``BRUTEX_SESSION_DENSITY_MIN_FRACTION`` (fail-soft) falling back to
        :data:`SESSION_DENSITY_MIN_FRACTION`.
    abs_floor
        Override the absolute missing-bars noise floor. ``None`` reads
        ``BRUTEX_SESSION_DENSITY_ABS_FLOOR`` (fail-soft) falling back to
        :data:`SESSION_DENSITY_ABS_FLOOR`.
    today_ist
        The forming-session cutoff (dates ``>= today_ist`` abstain).
        ``None`` reads the live IST clock (:func:`brutex.calendars.nse.now_ist`).
    observed
        Observed-holiday overlay days (DEC-CALENDAR-OBSERVED-SESSIONS-001,
        remove-only): an overlaid date is NOT a session, so it can never
        false-flag as a missing session.
    """
    fraction = (
        min_fraction
        if min_fraction is not None
        else _tunable_fraction("BRUTEX_SESSION_DENSITY_MIN_FRACTION", SESSION_DENSITY_MIN_FRACTION)
    )
    floor = (
        abs_floor
        if abs_floor is not None
        else _tunable_floor("BRUTEX_SESSION_DENSITY_ABS_FLOOR", SESSION_DENSITY_ABS_FLOOR)
    )
    by_date: dict[date, set[int]] = {}
    for ts in bar_timestamps:
        ist = ts.astimezone(IST)
        by_date.setdefault(ist.date(), set()).add(ist.hour * 60 + ist.minute)
    if not by_date:
        return SessionDensityVerdict(flags=(), known_irregular=(), sessions_checked=0, abstained=())
    today = today_ist if today_ist is not None else now_ist().date()
    first = min(by_date)
    last = max(by_date)
    abstains: dict[str, int] = {}
    flags: list[SessionDensityFlag] = []
    known: list[SessionDensityFlag] = []
    checked = 0
    cursor = first
    while cursor <= last:
        d = cursor
        cursor += timedelta(days=1)
        if not is_holiday_data_available(d):
            abstains["out_of_coverage"] = abstains.get("out_of_coverage", 0) + 1
            continue
        session = get_session(d)
        if session is None or d in observed:
            abstains["non_trading"] = abstains.get("non_trading", 0) + 1
            continue
        if has_muhurat_session(d):
            abstains["muhurat"] = abstains.get("muhurat", 0) + 1
            continue
        if d >= today:
            abstains["forming_session"] = abstains.get("forming_session", 0) + 1
            continue
        if d in (first, last):
            abstains["boundary"] = abstains.get("boundary", 0) + 1
            continue
        checked += 1
        flag = _judge_session(
            d,
            session,
            by_date.get(d, frozenset()),
            tf_minutes=tf_minutes,
            min_fraction=fraction,
            abs_floor=floor,
        )
        if flag is None:
            continue
        if flag.known_irregular:
            known.append(flag)
        else:
            flags.append(flag)
    return SessionDensityVerdict(
        flags=tuple(flags),
        known_irregular=tuple(known),
        sessions_checked=checked,
        abstained=tuple(sorted(abstains.items())),
    )


def _known_remediation(reason: str | None) -> str:
    """The reason-matched operator remediation for the known-irregular INFO arm."""
    if reason == CIRCUIT_HALT_REASON:
        return (
            "expected — no bars print during a market-wide circuit halt "
            "(DEC-HALT-DAY-TREATMENT-001); no action needed"
        )
    return (
        "re-pull this window — the extended-session bars "
        "(DEC-EXTENDED-SESSION-OUTAGE-001) may be absent from a pre-fix pull"
    )


def _emit_arm(
    log: object,
    flags: Sequence[SessionDensityFlag],
    *,
    event: str,
    arm: str,
    info: bool,
    symbol: str,
    interval: str,
    pull_id: str | None,
) -> None:
    """Emit one arm's per-session lines (capped 20) + one suppressed summary."""
    emit = log.info if info else log.warning  # type: ignore[attr-defined]
    for flag in flags[:SESSION_DENSITY_WARN_CAP]:
        fields: dict[str, object] = {
            "pull_id": pull_id,
            "symbol": repr(symbol),
            "interval": interval,
            "session": flag.session.isoformat(),
            "expected": flag.expected,
            "observed": flag.observed,
            "fraction": flag.fraction,
            "largest_gap_minutes": flag.largest_gap_minutes,
            "gap_start": (
                flag.gap_start_ist.strftime("%H:%M") if flag.gap_start_ist is not None else None
            ),
            "gap_end": (
                flag.gap_end_ist.strftime("%H:%M") if flag.gap_end_ist is not None else None
            ),
            "shape": flag.shape,
        }
        if info:
            reason = known_irregular_reason(flag.session)
            fields["reason"] = reason
            fields["remediation"] = _known_remediation(reason)
        else:
            fields["remediation"] = _SHAPE_REMEDIATION[flag.shape]
        emit(event, **fields)
    if len(flags) > SESSION_DENSITY_WARN_CAP:
        log.warning(  # type: ignore[attr-defined]
            SESSION_DENSITY_SUPPRESSED_EVENT,
            pull_id=pull_id,
            symbol=repr(symbol),
            interval=interval,
            arm=arm,
            cap=SESSION_DENSITY_WARN_CAP,
            total=len(flags),
        )


def observe_session_density(
    log: object,
    *,
    bar_timestamps: Sequence[datetime],
    symbol: str,
    interval: str,
    pull_id: str | None = None,
    observed: AbstractSet[date] = frozenset(),
    today_ist: date | None = None,
) -> SessionDensityVerdict | None:
    """Run the census over one window's kept bars; emit; return the verdict.

    The ONE shared wiring (CLAUDE.md §11 COMMON) for both ingest spines:
    kill-switch checked FIRST (:data:`SESSION_DENSITY_KILL_ENV`, default ON →
    a disabled census returns ``None`` with zero work); the classifier runs at
    ``tf_minutes=1`` (the wiring gates to the fetched ``1minute`` interval);
    per-flag :data:`SESSION_DENSITY_LOW_EVENT` WARNs capped at
    :data:`SESSION_DENSITY_WARN_CAP` + ONE suppressed summary with the TRUE
    count; per-known-irregular :data:`SESSION_DENSITY_KNOWN_EVENT` INFOs under
    the same cap discipline. Fully wrapped: ANY internal failure logs ONE
    :data:`SESSION_DENSITY_ERROR_EVENT` and returns ``None`` — the census can
    NEVER block / slow / crash a pull, and it NEVER touches
    ``WindowLedger.record`` (observe-only by construction; spy-pinned).

    *log* is the caller's structlog logger (duck-typed for
    ``.warning``/``.info``). The caller owns the SCOPE gates (spot index
    ``1minute`` real-vendor windows; F&O ``FUT`` contracts — see the wiring
    sites).
    """
    if not session_density_census_enabled():
        return None
    try:
        verdict = classify_session_density(bar_timestamps, observed=observed, today_ist=today_ist)
        _emit_arm(
            log,
            verdict.flags,
            event=SESSION_DENSITY_LOW_EVENT,
            arm="low",
            info=False,
            symbol=symbol,
            interval=interval,
            pull_id=pull_id,
        )
        _emit_arm(
            log,
            verdict.known_irregular,
            event=SESSION_DENSITY_KNOWN_EVENT,
            arm="known_irregular",
            info=True,
            symbol=symbol,
            interval=interval,
            pull_id=pull_id,
        )
        return verdict
    except Exception as exc:  # observe-only diagnostic must never break the pull
        log.warning(  # type: ignore[attr-defined]
            SESSION_DENSITY_ERROR_EVENT, pull_id=pull_id, error=str(exc)
        )
        return None
