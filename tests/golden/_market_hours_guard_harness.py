"""Shared harness for the market-hours guard goldens (DEC-RUST-PULL-STATE-001).

The Rust pull-state conversion lane freezes the vendor-account SAFETY guard's
verdicts (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692 — no Groww
vendor pulls 08:45-15:35 IST on a trading day; the shared-TOTP protection) as
the CONTRACT the Rust port must satisfy bit-for-bit. This module is imported
by BOTH the generator (``generate_market_hours_guard_golden.py``) and the
consuming test (``test_market_hours_guard_golden.py``) — the shared-harness
shape of ``_preopen_prevday_harness.py`` (never generator-imports-test, the
CodeQL ``py/cyclic-import`` break).

Two verdict tables are computed from the LIVE Python oracle:

* :func:`compute_guard_table` — ``pull_allowed_now`` over the FULL covered
  NSE-calendar span (every date in
  ``[HOLIDAY_COVERAGE_FIRST_YEAR-01-01, HOLIDAY_COVERAGE_LAST_YEAR-12-31]``)
  × the boundary + representative probe times, PLUS the out-of-coverage
  fail-safe cases (an out-of-coverage WEEKDAY reads as a trading day via the
  non-raising ``is_trading_day`` and BLOCKS in-window — the deliberately
  fail-safe posture; a weekend stays allowed).
* :func:`compute_auto_gate_table` — ``auto_pull_allowed`` (allowed + machine
  reason; the human ``detail`` is deliberately NOT frozen — it carries
  tier-provenance wording) over a curated date × time grid under the
  HERMETIC tier-1 settle env (``BRUTEX_PULL_ALL_SETTLE_IST=17:30`` — the
  tier-1 short-circuit performs ZERO filesystem work, so the golden never
  reads the committed evidence channel), plus the ``muhurat_session_active``
  margined-window boundaries over every listed Muhurat session.

Fixture freshness law: the fixture records the coverage span it was captured
over; the test asserts the LIVE span equals the recorded span, so adding
``HOLIDAYS_2027`` (or editing any holiday set) fails the golden LOUDLY and
forces a deliberate, ledger-worthy regeneration — never a silent drift.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from brutex.calendars.auto_pull_gate import (
    MUHURAT_POST_MARGIN,
    MUHURAT_PRE_MARGIN,
    PULL_ALL_SETTLE_ENV,
    auto_pull_allowed,
    muhurat_session_active,
)
from brutex.calendars.market_hours import BLOCK_END, BLOCK_START, pull_allowed_now
from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    IST,
    MUHURAT_SESSIONS,
    is_trading_day,
)

#: Fixture schema version (bump ONLY with a deliberate regeneration).
SCHEMA_VERSION = 1

#: Probe times per covered date — the window boundaries (08:44:59 / 08:45:00 /
#: 15:34:59 / 15:35:00) + representative in/out-of-window instants (midnight,
#: the 03:30 nightly-pull slot, session open, mid-session, session close,
#: evening). Order is LOAD-BEARING: the per-day verdict bitstring indexes it.
GUARD_PROBE_TIMES: tuple[time, ...] = (
    time(0, 0, 0),
    time(3, 30, 0),
    time(8, 44, 59),
    time(8, 45, 0),
    time(9, 15, 0),
    time(12, 0, 0),
    time(15, 30, 0),
    time(15, 34, 59),
    time(15, 35, 0),
    time(20, 0, 0),
)

#: Out-of-coverage fail-safe probes: pre-coverage + post-coverage WEEKDAYS
#: (non-raising ``is_trading_day`` reads True → BLOCKED in-window — the
#: fail-safe posture protecting the shared account) and a post-coverage
#: WEEKEND (reads non-trading → always allowed).
OUT_OF_COVERAGE_DATES: tuple[date, ...] = (
    date(2019, 6, 12),  # Wednesday, pre-coverage → fail-safe BLOCKED in-window
    date(2027, 1, 4),  # Monday, post-coverage → fail-safe BLOCKED in-window
    date(2027, 1, 2),  # Saturday, post-coverage → weekend, always allowed
    date(2030, 7, 18),  # Thursday, far post-coverage → fail-safe BLOCKED in-window
)

#: The hermetic settle env for the auto-gate golden (tier-1 short-circuit —
#: zero filesystem work; the VALUE equals the tier-3 conservative default so
#: the frozen decision grid matches the shipped default law).
AUTO_GATE_ENV: dict[str, str] = {PULL_ALL_SETTLE_ENV: "17:30"}

#: Auto-gate probe times (IST wall clock) — around the market-hours window,
#: the settle boundary (17:29:59 / 17:30:00) and the day edges.
AUTO_GATE_TIMES: tuple[time, ...] = (
    time(0, 0, 0),
    time(8, 44, 0),
    time(8, 45, 0),
    time(12, 0, 0),
    time(15, 34, 0),
    time(15, 35, 0),
    time(17, 29, 59),
    time(17, 30, 0),
    time(18, 0, 0),
    time(23, 59, 59),
)

#: Curated auto-gate dates: a regular trading weekday, a weekend, a listed
#: holiday, every listed Muhurat-session date (each is a HOLIDAY, so the gate
#: defers at step 1 — pinned), the announced-but-timing-less 2026 Muhurat
#: date, the three Budget special-weekend sessions, and the out-of-coverage
#: pair (weekday proceeds post-settle with ONE coverage WARN — ruling 10;
#: weekend defers).
AUTO_GATE_DATES: tuple[date, ...] = (
    date(2024, 1, 2),  # Tuesday — regular trading day
    date(2024, 1, 6),  # Saturday — weekend
    date(2024, 1, 26),  # Friday — Republic Day holiday
    date(2020, 11, 14),  # Muhurat 2020 (Sat, holiday-listed)
    date(2021, 11, 4),  # Muhurat 2021
    date(2022, 10, 24),  # Muhurat 2022
    date(2023, 11, 12),  # Muhurat 2023 (Sun)
    date(2024, 11, 1),  # Muhurat 2024
    date(2025, 10, 21),  # Muhurat 2025 (afternoon session)
    date(2026, 11, 8),  # Diwali 2026 — announced, timings NOT notified (Sun, holiday)
    date(2020, 2, 1),  # Budget Saturday 2020 — special weekend TRADING session
    date(2025, 2, 1),  # Budget Saturday 2025
    date(2026, 2, 1),  # Budget Sunday 2026
    date(2027, 1, 4),  # Monday, out-of-coverage → proceeds post-settle (ruling 10)
    date(2027, 1, 2),  # Saturday, out-of-coverage → non_trading_day
)


def _iter_covered_dates() -> list[date]:
    """Every calendar date of the covered holiday span, ascending."""
    first = date(HOLIDAY_COVERAGE_FIRST_YEAR, 1, 1)
    last = date(HOLIDAY_COVERAGE_LAST_YEAR, 12, 31)
    out: list[date] = []
    d = first
    one = timedelta(days=1)
    while d <= last:
        out.append(d)
        d += one
    return out


def _day_row(d: date) -> list[Any]:
    """One fixture row: ``[is_trading (0/1), allowed-bitstring]`` for date *d*."""
    trading = is_trading_day(d)
    bits = "".join(
        "1" if pull_allowed_now(datetime.combine(d, probe, tzinfo=IST), trading) else "0"
        for probe in GUARD_PROBE_TIMES
    )
    return [1 if trading else 0, bits]


def compute_guard_table() -> dict[str, Any]:
    """The full market-hours guard verdict table (the frozen contract)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "dec": "DEC-RUST-PULL-STATE-001",
        "source": "brutex.calendars.market_hours.pull_allowed_now"
        " + brutex.calendars.nse.is_trading_day",
        "block_start": BLOCK_START.isoformat(),
        "block_end": BLOCK_END.isoformat(),
        "probe_times": [t.isoformat() for t in GUARD_PROBE_TIMES],
        "coverage": {
            "first": date(HOLIDAY_COVERAGE_FIRST_YEAR, 1, 1).isoformat(),
            "last": date(HOLIDAY_COVERAGE_LAST_YEAR, 12, 31).isoformat(),
        },
        "days": {d.isoformat(): _day_row(d) for d in _iter_covered_dates()},
        "out_of_coverage": {d.isoformat(): _day_row(d) for d in OUT_OF_COVERAGE_DATES},
    }


def _muhurat_boundary_cases() -> list[dict[str, Any]]:
    """``muhurat_session_active`` at every listed session's margined boundaries."""
    cases: list[dict[str, Any]] = []
    second = timedelta(seconds=1)
    for d, (open_t, close_t) in sorted(MUHURAT_SESSIONS.items()):
        start = datetime.combine(d, open_t, tzinfo=IST) - MUHURAT_PRE_MARGIN
        end = datetime.combine(d, close_t, tzinfo=IST) + MUHURAT_POST_MARGIN
        mid = (
            datetime.combine(d, open_t, tzinfo=IST)
            + (datetime.combine(d, close_t, tzinfo=IST) - datetime.combine(d, open_t, tzinfo=IST))
            / 2
        )
        for instant in (start - second, start, mid, end - second, end):
            cases.append(
                {
                    "date": d.isoformat(),
                    "time": instant.time().isoformat(),
                    "active": muhurat_session_active(instant),
                }
            )
    # The announced-but-timing-less 2026 date HONESTLY reads inactive.
    probe_2026 = datetime.combine(date(2026, 11, 8), time(18, 30), tzinfo=IST)
    cases.append(
        {
            "date": "2026-11-08",
            "time": probe_2026.time().isoformat(),
            "active": muhurat_session_active(probe_2026),
        }
    )
    return cases


def compute_auto_gate_table() -> dict[str, Any]:
    """The auto-pull gate deferral-verdict table (allowed + machine reason)."""
    cases: list[dict[str, Any]] = []
    for d in AUTO_GATE_DATES:
        for t in AUTO_GATE_TIMES:
            verdict = auto_pull_allowed(
                now=datetime.combine(d, t, tzinfo=IST), env=dict(AUTO_GATE_ENV)
            )
            cases.append(
                {
                    "date": d.isoformat(),
                    "time": t.isoformat(),
                    "allowed": verdict.allowed,
                    "reason": verdict.reason,
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "dec": "DEC-RUST-PULL-STATE-001",
        "source": "brutex.calendars.auto_pull_gate.auto_pull_allowed + muhurat_session_active",
        "settle_env": dict(AUTO_GATE_ENV),
        "cases": cases,
        "muhurat_active": _muhurat_boundary_cases(),
    }
