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

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from brutex.calendars.auto_pull_gate import (
    MUHURAT_POST_MARGIN,
    MUHURAT_PRE_MARGIN,
    PULL_ALL_SETTLE_ENV,
    auto_pull_allowed,
    compose_market_closed,
    muhurat_session_active,
    resolve_settle_resolution,
)
from brutex.calendars.market_hours import (
    BLOCK_END,
    BLOCK_START,
    MARKET_HOURS_GUARD_ENV,
    pull_allowed_now,
)
from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    IST,
    MUHURAT_SESSIONS,
    has_muhurat_session,
    is_holiday_data_available,
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
        # Wave-2D EXTENSIONS (append-only keys — every pre-existing key above
        # stays byte-identical; captured on the UNMODIFIED Python module):
        "belt_coherence": _belt_coherence_cases(),
        "cases_settle_1535": _settle_1535_cases(),
        "settle_tokens": [_settle_token_case(token) for token in SETTLE_TOKEN_MATRIX],
    }


# ── Wave-2D extensions (settle-token tiers · floor-exact settle · belt) ──────

#: The settle-token verdict matrix (tier-1 env values): the ok arm (incl. the
#: floor-exact ``15:35`` boundary + the Python ``str.strip`` law), the
#: below-floor refusals, the out-of-range wall-clock refusals, and the
#: bad-shape refusals (single-digit / missing-colon / Unicode-digit — the
#: ``re.ASCII`` law). NO whitespace-only token (blank never reaches tier 1)
#: and no tab/newline (the TSV projection is line-oriented).
SETTLE_TOKEN_MATRIX: tuple[str, ...] = (
    "17:30",
    "15:35",
    "23:59",
    " 17:30 ",
    "15:34",
    "08:45",
    "00:00",
    "24:00",
    "17:60",
    "99:99",
    "7:30",
    "17:3",
    "1730",
    "17-30",
    "0x:30",
    "+7:30",
    "١٧:٣٠",  # "١٧:٣٠" — Unicode digits must be refused
    "17:30 IST",
)

#: The message → machine-kind classification of the UNMODIFIED module's three
#: fail-loud ``ValueError`` arms (the golden freezes the KIND, not the byte
#: text — detail wording stays Python-owned, never crosses the FFI).
_SETTLE_ERROR_KINDS: tuple[tuple[str, str], ...] = (
    ("not a strict HH:MM", "bad_shape"),
    ("not a real wall-clock time", "not_wall_clock"),
    ("before the market-hours guard's", "below_floor"),
)


def _settle_token_case(token: str) -> dict[str, Any]:
    """One frozen settle-token verdict row (tier-1 env → parse outcome)."""
    try:
        res = resolve_settle_resolution({PULL_ALL_SETTLE_ENV: token})
    except ValueError as exc:
        msg = str(exc)
        kind = next((k for needle, k in _SETTLE_ERROR_KINDS if needle in msg), "unclassified")
        return {"token": token, "verdict": kind, "minute": None}
    if res.source != "env":  # pragma: no cover — matrix tokens are all tier-1
        raise AssertionError(f"settle token {token!r} did not resolve at tier 1")
    return {
        "token": token,
        "verdict": "ok",
        "minute": res.value.hour * 60 + res.value.minute,
    }


#: The floor-exact settle env (``15:35`` == the guard's BLOCK_END — the
#: boundary the market-hours belt's structural unreachability rests on).
SETTLE_1535_ENV: dict[str, str] = {PULL_ALL_SETTLE_ENV: "15:35"}

#: Probe times for the floor-exact grid: in-window minutes (every one is
#: ``before_settle`` — the belt stays unreachable), the 15:34:59/15:35:00
#: settle boundary, and post-settle instants.
SETTLE_1535_TIMES: tuple[time, ...] = (
    time(8, 44, 0),
    time(8, 45, 0),
    time(12, 0, 0),
    time(15, 34, 59),
    time(15, 35, 0),
    time(15, 36, 0),
    time(17, 30, 0),
    time(23, 59, 59),
)

#: Floor-exact grid dates: a regular trading day + the out-of-coverage
#: weekday (proceeds post-settle with the coverage WARN — ruling 10).
SETTLE_1535_DATES: tuple[date, ...] = (
    date(2024, 1, 2),  # Tuesday — regular trading day
    date(2027, 1, 4),  # Monday, out-of-coverage weekday
)


def _settle_1535_cases() -> list[dict[str, Any]]:
    """The gate grid under the FLOOR-EXACT ``15:35`` settle env."""
    cases: list[dict[str, Any]] = []
    for d in SETTLE_1535_DATES:
        for t in SETTLE_1535_TIMES:
            verdict = auto_pull_allowed(
                now=datetime.combine(d, t, tzinfo=IST), env=dict(SETTLE_1535_ENV)
            )
            cases.append(
                {
                    "date": d.isoformat(),
                    "time": t.isoformat(),
                    "allowed": verdict.allowed,
                    "reason": verdict.reason,
                }
            )
    return cases


def _belt_coherence_cases() -> list[dict[str, Any]]:
    """``compose_market_closed`` kill-switch coherence (guard off ⇒ belt off).

    Per listed Muhurat session: one instant INSIDE the margined window + one
    just BEFORE it, composed over an always-False base under the guard switch
    unset / explicitly-on / explicitly-off — the frozen law: the belt closes
    the in-window instant ONLY while the guard switch is enabled.
    """
    rows: list[dict[str, Any]] = []
    second = timedelta(seconds=1)

    def _frozen_clock(instant: datetime) -> Callable[[], datetime]:
        return lambda: instant

    for d, (open_t, _close_t) in sorted(MUHURAT_SESSIONS.items()):
        inside = datetime.combine(d, open_t, tzinfo=IST) + timedelta(minutes=1)
        outside = datetime.combine(d, open_t, tzinfo=IST) - MUHURAT_PRE_MARGIN - second
        for guard in ("unset", "1", "0"):
            env: dict[str, str] = {} if guard == "unset" else {MARKET_HOURS_GUARD_ENV: guard}
            for instant in (inside, outside):
                composed = compose_market_closed(
                    lambda: False, clock=_frozen_clock(instant), env=env
                )
                rows.append(
                    {
                        "date": d.isoformat(),
                        "time": instant.time().isoformat(),
                        "guard": guard,
                        "closed": composed(),
                    }
                )
    return rows


# ── The serde-free TSV projection (cargo consumes this; sync-pinned) ─────────


def _us_of_time(t: time) -> int:
    """Microsecond-of-day of an IST wall-clock time (order-isomorphic)."""
    return ((t.hour * 60 + t.minute) * 60 + t.second) * 1_000_000 + t.microsecond


def _gate_tsv_row(row: dict[str, Any], settle: time) -> str:
    """One ``gate`` TSV row: the RESOLVED predicate inputs + expected verdict.

    The Rust kernel is clockless/calendar-free, so the projection carries the
    Python-resolved predicates (trading-day / coverage / muhurat-listed) the
    facade will pass at runtime — recomputed here from the one Python
    calendar source of truth at projection-build time.
    """
    d = date.fromisoformat(row["date"])
    t = time.fromisoformat(row["time"])
    return "\t".join(
        [
            "gate",
            row["date"],
            row["time"],
            str(int(is_trading_day(d))),
            str(int(is_holiday_data_available(d))),
            str(int(has_muhurat_session(d))),
            str(_us_of_time(t)),
            str(_us_of_time(settle)),
            row["reason"],
            str(int(row["allowed"])),
        ]
    )


def _mwin_tsv_row(row: dict[str, Any]) -> str | None:
    """One ``mwin`` TSV row, or ``None`` for a session-less date (the 2026
    announced-but-timing-less probe — the session LOOKUP stays Python; the
    kernel only ever sees a listed session's interval)."""
    d = date.fromisoformat(row["date"])
    session = MUHURAT_SESSIONS.get(d)
    if session is None:
        return None
    open_t, close_t = session
    return "\t".join(
        [
            "mwin",
            row["date"],
            str(_us_of_time(time.fromisoformat(row["time"]))),
            str(_us_of_time(open_t)),
            str(_us_of_time(close_t)),
            str(MUHURAT_PRE_MARGIN // timedelta(microseconds=1)),
            str(MUHURAT_POST_MARGIN // timedelta(microseconds=1)),
            str(int(row["active"])),
        ]
    )


def _settle_tsv_row(row: dict[str, Any]) -> str:
    """One ``settle`` TSV row: the PRE-STRIPPED token + floor + verdict kind.

    ``str.strip`` stays Python (its whitespace set is CPython's ``isspace``
    law, not Unicode ``White_Space``) — the kernel receives the stripped
    token exactly as the facade will pass it.
    """
    token = str(row["token"]).strip()
    if "\t" in token or "\n" in token:  # pragma: no cover — matrix is line-safe
        raise AssertionError(f"TSV-unsafe settle token {row['token']!r}")
    minute = row["minute"]
    minute_us = -1 if minute is None else minute * 60_000_000
    return "\t".join(["settle", token, str(_us_of_time(BLOCK_END)), row["verdict"], str(minute_us)])


def compute_auto_gate_tsv(payload: dict[str, Any]) -> str:
    """The whole TSV projection of the auto-gate *payload* (one truth, two
    encodings — the ``pull_stall_verdicts.tsv`` pattern): the consuming
    Python golden test pins the committed ``.tsv`` byte-equal to this
    projection of the committed ``.json``, and the cargo fixture test
    replays it through the Rust kernels serde-free."""
    settle_default = time(17, 30)
    settle_floor = time(15, 35)
    lines = [_gate_tsv_row(r, settle_default) for r in payload["cases"]]
    lines.extend(_gate_tsv_row(r, settle_floor) for r in payload["cases_settle_1535"])
    lines.extend(row for r in payload["muhurat_active"] if (row := _mwin_tsv_row(r)) is not None)
    lines.extend(_settle_tsv_row(r) for r in payload["settle_tokens"])
    return "\n".join(lines) + "\n"
