"""Shared record builders for the NSE-calendar verdict-table golden.

DEC-CALENDAR-VERDICT-GOLDEN-001. Imported by BOTH the generator
(``generate_calendar_verdict_table_golden.py``) and the consuming test
(``test_calendar_verdict_table_golden.py``) — the ``_preopen_prevday_harness``
pattern, so the generator never imports the test module (the CodeQL
``py/cyclic-import`` break precedent).

Every builder here is a THIN projection over live ``brutex.calendars.nse``
calls — no value is hand-authored, no semantics are re-derived. The golden
therefore freezes "what the live module answers today" as a machine-readable,
language-neutral ANSWER KEY (the ``vocab_golden_vectors.json`` /
DEC-BXSNAP2-PARITY-GOLDEN-001 #1695 layer) that a non-Python port replays
natively; it duplicates no unit assertion.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

from brutex.calendars.nse import (
    MUHURAT_SESSIONS,
    SPECIAL_WEEKEND_TRADING_SESSIONS,
    HolidayDataUnavailableError,
    TradingSession,
    daily_derive_cutoff,
    expected_bars_per_day_for_tf,
    forced_exit_seconds_for_session,
    forced_exit_time_for_session,
    get_session,
    get_session_checked,
    has_muhurat_session,
    is_forced_exit_bar,
    is_special_weekend_session,
    is_trading_day,
    is_trading_day_checked,
    previous_trading_session,
    session_last_bar_complete,
)

FIXTURES: Final[Path] = Path(__file__).resolve().parent / "fixtures"
CALENDAR_FIXTURE: Final[Path] = FIXTURES / "calendar_verdict_table_golden.json"

#: The full covered span — one day record per date (2,557 records).
SPAN_START: Final[date] = date(2020, 1, 1)
SPAN_END: Final[date] = date(2026, 12, 31)

#: Out-of-coverage strips freezing the three-way abstain TRIAGE
#: (silent-False vs raise vs fabricate) on both sides of the covered span.
STRIP_BEFORE: Final[tuple[date, date]] = (date(2019, 12, 22), date(2019, 12, 31))
STRIP_AFTER: Final[tuple[date, date]] = (date(2027, 1, 1), date(2027, 1, 10))

#: The 21 Track-1 timeframes in minutes — MIRRORED from
#: ``brutex.data.timeframes.ALL_TF_MINUTES`` (1m base + 19 derived intraday +
#: the 375-minute daily) and equality-pinned against it in the consuming test —
#: plus the boundary vectors the ceiling law pins (374/376 around the session
#: length; ``tf < 1`` raises).
TF_MINUTES: Final[tuple[int, ...]] = (
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    30,
    60,
    120,
    180,
    240,
    375,
)
TF_BOUNDARY_MINUTES: Final[tuple[int, ...]] = (374, 376, 0, -5)

#: ``is_forced_exit_bar`` strict-``>`` boundary probes: (open_time, tf_minutes,
#: use_muhurat_2025_boundary). The Muhurat rows resolve the 2025-10-21
#: afternoon session's own forced-exit seconds via
#: :func:`forced_exit_seconds_for_session` — computed, never hand-authored.
FORCED_EXIT_BAR_PROBES: Final[tuple[tuple[time, int, bool], ...]] = (
    (time(15, 18, 0), 1, False),
    (time(15, 19, 0), 1, False),  # close lands exactly ON 15:20 — strict > says False
    (time(15, 19, 1), 1, False),
    (time(15, 20, 0), 1, False),
    (time(15, 15, 0), 5, False),  # close exactly ON the boundary
    (time(15, 16, 0), 5, False),
    (time(14, 20, 0), 60, False),  # close exactly ON the boundary
    (time(14, 20, 1), 60, False),
    (time(14, 21, 0), 60, False),
    (time(14, 34, 0), 1, True),  # Muhurat-2025 boundary (14:35): close ON it
    (time(14, 35, 0), 1, True),
    (time(14, 30, 0), 5, True),  # close ON the Muhurat boundary
    (time(14, 31, 0), 5, True),
)

#: ``daily_derive_cutoff`` probes: the strict-``>`` at-close law (15:30:00 is
#: still "forming"), a holiday, a weekend, and the Muhurat afternoon close.
DAILY_CUTOFF_PROBES: Final[tuple[datetime, ...]] = (
    datetime(2026, 7, 6, 15, 29, 59),
    datetime(2026, 7, 6, 15, 30, 0),
    datetime(2026, 7, 6, 15, 30, 1),
    datetime(2026, 1, 26, 12, 0, 0),  # Republic Day holiday
    datetime(2026, 7, 5, 12, 0, 0),  # Sunday
    datetime(2025, 10, 21, 14, 0, 0),  # Muhurat afternoon, pre-close
    datetime(2025, 10, 21, 14, 46, 0),  # Muhurat afternoon, post-close
)

#: ``session_last_bar_complete`` probes: (session_date, last_bar naive-IST).
LAST_BAR_PROBES: Final[tuple[tuple[date, datetime], ...]] = (
    (date(2026, 7, 6), datetime(2026, 7, 6, 15, 29, 0)),  # the +1-minute >= law
    (date(2026, 7, 6), datetime(2026, 7, 6, 15, 28, 0)),
    (date(2025, 10, 21), datetime(2025, 10, 21, 14, 44, 0)),  # Muhurat close 14:45
    (date(2025, 10, 21), datetime(2025, 10, 21, 14, 43, 0)),
)

#: ``previous_trading_session`` observed-overlay probes: (d, observed set) —
#: empty-set identity, a single removal across the Republic-Day cluster, and
#: the Jan-2026-drift shape (an overlay member that is itself a SPECIAL
#: weekend session — the remove-only law must step over it too).
OBSERVED_OVERLAY_PROBES: Final[tuple[tuple[date, frozenset[date]], ...]] = (
    (date(2026, 1, 28), frozenset()),
    (date(2026, 1, 28), frozenset({date(2026, 1, 27)})),
    (date(2026, 2, 2), frozenset({date(2026, 2, 1)})),
)


def _session_json(session: TradingSession | None) -> dict[str, str] | None:
    """Project a session (or ``None``) into the compact fixture shape."""
    if session is None:
        return None
    return {
        "t": session.session_type.value,
        "o": session.open_time_ist.isoformat(),
        "c": session.close_time_ist.isoformat(),
    }


def verdict_record(d: date, *, exchange: str | None = None) -> dict[str, Any]:
    """One compact per-date verdict record over the live module.

    Field key → source:
    ``d`` ISO date · ``td`` :func:`is_trading_day` · ``tdc``
    :func:`is_trading_day_checked` outcome (``"true"``/``"false"``/``"raises"``)
    · ``s`` :func:`get_session` (out-of-coverage weekday FABRICATION included —
    it is documented load-bearing) · ``sc`` :func:`get_session_checked` (or
    ``"raises"``) · ``m`` :func:`has_muhurat_session` · ``w``
    :func:`is_special_weekend_session` · ``p``
    :func:`previous_trading_session` (ISO or ``None`` — the coverage-floor
    abstain freezes as ``None``).
    """
    tdc: str
    try:
        tdc = "true" if is_trading_day_checked(d, exchange=exchange) else "false"
    except HolidayDataUnavailableError:
        tdc = "raises"
    sc: dict[str, str] | str | None
    try:
        sc = _session_json(get_session_checked(d, exchange=exchange))
    except HolidayDataUnavailableError:
        sc = "raises"
    prev = previous_trading_session(d, exchange=exchange)
    return {
        "d": d.isoformat(),
        "td": is_trading_day(d, exchange=exchange),
        "tdc": tdc,
        "s": _session_json(get_session(d, exchange=exchange)),
        "sc": sc,
        "m": has_muhurat_session(d),
        "w": is_special_weekend_session(d),
        "p": None if prev is None else prev.isoformat(),
    }


def _dates_between(start: date, end: date) -> list[date]:
    days: list[date] = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def build_days(*, exchange: str | None = None) -> list[dict[str, Any]]:
    """The full-span per-date verdict table (2,557 records)."""
    return [verdict_record(d, exchange=exchange) for d in _dates_between(SPAN_START, SPAN_END)]


def build_strips(*, exchange: str | None = None) -> list[dict[str, Any]]:
    """The two out-of-coverage strips (abstain-triage freeze)."""
    dates = _dates_between(*STRIP_BEFORE) + _dates_between(*STRIP_AFTER)
    return [verdict_record(d, exchange=exchange) for d in dates]


def _expected_bars_outcome(tf: int) -> int | str:
    try:
        return expected_bars_per_day_for_tf(tf)
    except ValueError:
        return "raises"


def _forced_exit_row(d: date) -> dict[str, Any]:
    session = get_session(d)
    assert session is not None, f"no session on {d} — fixture probe date must trade"
    return {
        "d": d.isoformat(),
        "t": session.session_type.value,
        "o": session.open_time_ist.isoformat(),
        "c": session.close_time_ist.isoformat(),
        "exit_time": forced_exit_time_for_session(session).isoformat(),
        "exit_seconds": forced_exit_seconds_for_session(session),
    }


def _muhurat_2025_boundary_seconds() -> int:
    session = get_session(date(2025, 10, 21))
    assert session is not None
    return forced_exit_seconds_for_session(session)


def _previous_session_observed(d: date, observed: AbstractSet[date]) -> str | None:
    prev = previous_trading_session(d, observed=observed)
    return None if prev is None else prev.isoformat()


def build_scalars() -> dict[str, Any]:
    """Every scalar-vector family, computed from the live module."""
    muhurat_boundary = _muhurat_2025_boundary_seconds()
    return {
        "expected_bars_per_day_for_tf": {
            str(tf): _expected_bars_outcome(tf) for tf in (*TF_MINUTES, *TF_BOUNDARY_MINUTES)
        },
        "forced_exit": [
            _forced_exit_row(date(2024, 7, 1)),  # one regular Monday
            *(_forced_exit_row(d) for d in sorted(MUHURAT_SESSIONS)),
            *(_forced_exit_row(d) for d in sorted(SPECIAL_WEEKEND_TRADING_SESSIONS)),
        ],
        "is_forced_exit_bar": [
            {
                "t": t.isoformat(),
                "tf": tf,
                "forced_exit_seconds": muhurat_boundary if muhurat else None,
                "result": is_forced_exit_bar(t, tf, muhurat_boundary if muhurat else None),
            }
            for t, tf, muhurat in FORCED_EXIT_BAR_PROBES
        ],
        "daily_derive_cutoff": [
            {"now_ist": now.isoformat(), "cutoff": daily_derive_cutoff(now).isoformat()}
            for now in DAILY_CUTOFF_PROBES
        ],
        "session_last_bar_complete": [
            {
                "session_date": d.isoformat(),
                "last_bar_ist": last_bar.isoformat(),
                "result": session_last_bar_complete(_require_session(d), last_bar),
            }
            for d, last_bar in LAST_BAR_PROBES
        ],
        "previous_session_observed": [
            {
                "d": d.isoformat(),
                "observed": sorted(o.isoformat() for o in observed),
                "result": _previous_session_observed(d, observed),
            }
            for d, observed in OBSERVED_OVERLAY_PROBES
        ],
        "muhurat_precedence": [
            {
                "d": d.isoformat(),
                "td": is_trading_day(d),
                "session_type": _require_session(d).session_type.value,
            }
            for d in sorted(MUHURAT_SESSIONS)
        ],
    }


def _require_session(d: date) -> TradingSession:
    session = get_session(d)
    assert session is not None, f"no session on {d} — fixture probe date must trade"
    return session


def render_fixture(
    meta: dict[str, Any],
    days: list[dict[str, Any]],
    strips: list[dict[str, Any]],
    scalars: dict[str, Any],
) -> bytes:
    """Deterministic one-record-per-line JSON rendering (reviewable diffs)."""
    import json

    def _compact(record: dict[str, Any]) -> str:
        return json.dumps(record, separators=(",", ":"), ensure_ascii=False)

    parts = [
        "{",
        '"meta": ' + json.dumps(meta, sort_keys=True, ensure_ascii=False) + ",",
        '"days": [',
        ",\n".join(_compact(r) for r in days),
        "],",
        '"strips": [',
        ",\n".join(_compact(r) for r in strips),
        "],",
        '"scalars": ' + json.dumps(scalars, indent=1, sort_keys=True, ensure_ascii=False),
        "}",
    ]
    return ("\n".join(parts) + "\n").encode("utf-8")
