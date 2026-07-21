"""NSE-calendar verdict-table parity golden (DEC-CALENDAR-VERDICT-GOLDEN-001).

The frozen, machine-readable ANSWER KEY for the Rust port of the
``brutex/calendars/nse.py`` verdict LOGIC (ADR-0019): every date
2020-01-01..2026-12-31 (2,557 records) × the full verdict surface
(``is_trading_day`` / the ``is_trading_day_checked`` raise-vs-False-vs-True
triage / ``get_session`` incl. its documented out-of-coverage FABRICATION /
``get_session_checked`` / ``has_muhurat_session`` /
``is_special_weekend_session`` / ``previous_trading_session`` / ``is_holiday``
/ the ``is_holiday_checked`` triage / ``is_weekend``), the two
out-of-coverage abstain-triage strips, and every scalar family (expected-bars
ceiling law, forced-exit time+seconds for one regular day + EVERY Muhurat +
special-weekend session, the ``is_forced_exit_bar`` strict-``>`` boundary, the
``daily_derive_cutoff`` strict-``>`` at-close law, ``session_last_bar_complete``,
the ``previous_trading_session`` observed-overlay remove-only vectors, the
Muhurat-precedence law, and the ``trading_days_between[_checked]`` vectors
incl. the checked variant's OOC-raise partial-discard law). The port flips ON
only when it reproduces this fixture BIT-FOR-BIT (the goldens-first law);
calendar DATA itself stays in ``brutex.calendars`` per ADR-0012 D8 and
crosses any FFI as config/day-ordinals — this key freezes the VERDICTS.

Slice-9 review-round ADDITIVE extension (calendar MINOR; the ledgered regen
record is MIGRATION.md #1727a): the answer key had omitted 5 of the 17
dispatch heads — the ``h``/``hc``/``we`` per-date columns + the
``trading_days_between`` scalar family were APPENDED, verified additive-only
(every pre-extension record/family byte-identical under key projection).

The test asserts fixture-vs-live EQUALITY through the shared thin projection
(``tests/golden/_calendar_verdict_harness``) — it re-derives no semantics and
duplicates no unit assertion (the 189-test ``test_calendar_nse.py`` gate owns
the semantics; this golden owns the frozen answer key, the
``vocab_golden_vectors.json`` / #1695 layer).

REGENERATION LAW: regenerating this fixture is a calendar-DATA act — it
REQUIRES its own ledgered calendar-data DEC in the same commit (e.g.
``HOLIDAYS_2027``, the pending 2026-11-08 Muhurat timings, a Jan-2026 circular
resolution). The generator refuses to overwrite without
``BRUTEX_REGEN_CALENDAR_VERDICT_GOLDEN=1``, and the ``_EXPECTED_SHA256``
manifest constant below forces any regen through a reviewed test edit. The
exchange-law and coverage-span tripwires below FAIL BY DESIGN on the first
legitimately divergent NSE/BSE circular or on adding ``HOLIDAYS_2027`` —
forcing that conscious regen.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Final

import pytest
from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    HolidayDataUnavailableError,
    is_holiday_checked,
    previous_trading_session,
)
from brutex.data.timeframes import ALL_TF_MINUTES
from structlog.testing import capture_logs

from tests.golden._calendar_verdict_harness import (
    CALENDAR_FIXTURE,
    SPAN_END,
    SPAN_START,
    STRIP_AFTER,
    STRIP_BEFORE,
    TF_MINUTES,
    build_scalars,
    build_strips,
    verdict_record,
)

#: SHA-256 manifest over the RAW fixture bytes (fixtures/README.md convention).
#: Updating this constant is the reviewed-edit chokepoint every regeneration
#: must pass through — together with its own ledgered calendar-data DEC.
_EXPECTED_SHA256: Final[str] = "8d6b65793fabef2bd7bd7222ce43a79ac5c33ad5bbc77bb6a951b72e41822e0c"

_EXPECTED_DAY_COUNT: Final[int] = 2557
_YEARS: Final[tuple[int, ...]] = (2020, 2021, 2022, 2023, 2024, 2025, 2026)


def _doc() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(CALENDAR_FIXTURE.read_text(encoding="utf-8"))
    return doc


def _days() -> list[dict[str, Any]]:
    days: list[dict[str, Any]] = _doc()["days"]
    return days


# ── Manifest + shape laws ────────────────────────────────────────────────────


def test_fixture_sha256_manifest() -> None:
    """The fixture's raw bytes match the frozen manifest exactly."""
    assert hashlib.sha256(CALENDAR_FIXTURE.read_bytes()).hexdigest() == _EXPECTED_SHA256


def test_meta_matches_the_coverage_constants() -> None:
    """Coverage-span law: adding ``HOLIDAYS_2027`` without a regen fails HERE."""
    meta = _doc()["meta"]
    assert meta["coverage_first_year"] == HOLIDAY_COVERAGE_FIRST_YEAR == 2020
    assert meta["coverage_last_year"] == HOLIDAY_COVERAGE_LAST_YEAR == 2026
    assert meta["span"] == [SPAN_START.isoformat(), SPAN_END.isoformat()]
    assert meta["strips"] == [
        [STRIP_BEFORE[0].isoformat(), STRIP_BEFORE[1].isoformat()],
        [STRIP_AFTER[0].isoformat(), STRIP_AFTER[1].isoformat()],
    ]


def test_day_table_covers_the_whole_span_once() -> None:
    days = _days()
    assert len(days) == _EXPECTED_DAY_COUNT
    assert days[0]["d"] == SPAN_START.isoformat()
    assert days[-1]["d"] == SPAN_END.isoformat()
    assert len({r["d"] for r in days}) == _EXPECTED_DAY_COUNT


def test_tf_mirror_matches_the_one_timeframe_source() -> None:
    """The harness TF mirror equals ``brutex.data.timeframes.ALL_TF_MINUTES``."""
    assert TF_MINUTES == ALL_TF_MINUTES


# ── Day-table replay (chunked per year) ──────────────────────────────────────


@pytest.mark.parametrize("year", _YEARS)
def test_days_replay_against_live_module(year: int) -> None:
    """Every frozen per-date record equals the live module's verdicts."""
    records = [r for r in _days() if int(r["d"][:4]) == year]
    assert records, f"no fixture records for {year}"
    for record in records:
        d = date.fromisoformat(record["d"])
        assert verdict_record(d) == record, f"verdict drift at {record['d']}"


@pytest.mark.parametrize("strip_index", [0, 1])
def test_strips_replay_against_live_module(strip_index: int) -> None:
    """The out-of-coverage abstain TRIAGE (False vs raise vs fabricate) is frozen."""
    strips: list[dict[str, Any]] = _doc()["strips"]
    live = build_strips()
    assert len(strips) == len(live) == 20
    chunk = slice(strip_index * 10, strip_index * 10 + 10)
    assert live[chunk] == strips[chunk]


def test_strip_triage_shapes_are_the_documented_ones() -> None:
    """Spot-pin the triage arms: checked raises on a weekday, unguarded fabricates."""
    strips: list[dict[str, Any]] = _doc()["strips"]
    by_date = {r["d"]: r for r in strips}
    weekday_2027 = by_date["2027-01-01"]  # Friday, out of coverage
    assert weekday_2027["td"] is True  # unguarded: silent trading-day
    assert weekday_2027["tdc"] == "raises"  # checked: refuses to guess
    assert weekday_2027["s"] is not None  # unguarded get_session FABRICATES
    assert weekday_2027["sc"] == "raises"  # checked refuses
    weekend_2027 = by_date["2027-01-02"]  # Saturday — data-complete either way
    assert weekend_2027["td"] is False
    assert weekend_2027["tdc"] == "false"
    assert weekend_2027["s"] is None
    assert weekend_2027["sc"] is None
    # The back-walk abstain: Monday 2027-01-04's previous session walks through
    # the weekend into the out-of-coverage Friday and honestly abstains (null).
    assert by_date["2027-01-04"]["p"] is None
    # …while 2027-01-01 itself finds the in-coverage 2026-12-31 directly.
    assert weekday_2027["p"] == "2026-12-31"


# ── Scalar families ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "family",
    [
        "expected_bars_per_day_for_tf",
        "forced_exit",
        "is_forced_exit_bar",
        "daily_derive_cutoff",
        "session_last_bar_complete",
        "previous_session_observed",
        "muhurat_precedence",
        "trading_days_between",
    ],
)
def test_scalar_family_replays_against_live_module(family: str) -> None:
    """Each frozen scalar family equals the live module's recomputed vectors."""
    frozen: dict[str, Any] = _doc()["scalars"]
    live = build_scalars()
    assert sorted(frozen.keys()) == sorted(live.keys())
    assert live[family] == frozen[family], f"scalar drift in family {family!r}"


def test_expected_bars_boundary_laws() -> None:
    """The ceiling law's landmark cells: 60→7, 375→1, 376→1, tf<1 raises."""
    bars: dict[str, Any] = _doc()["scalars"]["expected_bars_per_day_for_tf"]
    assert bars["1"] == 375
    assert bars["60"] == 7
    assert bars["375"] == 1
    assert bars["376"] == 1
    assert bars["0"] == "raises"
    assert bars["-5"] == "raises"


def test_daily_cutoff_strict_gt_at_close_divergence_is_frozen() -> None:
    """At exactly 15:30:00 the cutoff stays TODAY (strict ``>`` — the pinned law)."""
    vectors: list[dict[str, Any]] = _doc()["scalars"]["daily_derive_cutoff"]
    by_now = {v["now_ist"]: v["cutoff"] for v in vectors}
    assert by_now["2026-07-06T15:29:59"] == "2026-07-06"
    assert by_now["2026-07-06T15:30:00"] == "2026-07-06"  # AT close = still forming
    assert by_now["2026-07-06T15:30:01"] == "2026-07-07"


def test_holiday_and_weekend_columns_spot_pins() -> None:
    """Spot-pin the slice-9 additive columns: ``h``/``hc``/``we`` shapes.

    Republic Day 2026 (Mon) is a holiday both variants agree on; a regular
    Monday is neither; and — unlike ``tdc`` — ``hc`` raises on ANY
    out-of-coverage date, weekends included (frozen in the strips).
    """
    by_date = {r["d"]: r for r in _days()}
    republic = by_date["2026-01-26"]
    assert republic["h"] is True
    assert republic["hc"] == "true"
    assert republic["we"] is False
    monday = by_date["2026-07-06"]
    assert monday["h"] is False
    assert monday["hc"] == "false"
    assert monday["we"] is False
    saturday = by_date["2026-07-04"]
    assert saturday["we"] is True
    strips: list[dict[str, Any]] = _doc()["strips"]
    strip_by_date = {r["d"]: r for r in strips}
    ooc_weekend = strip_by_date["2027-01-02"]  # Saturday
    assert ooc_weekend["tdc"] == "false"  # trading-day triage answers weekends
    assert ooc_weekend["hc"] == "raises"  # holiday triage refuses ANY OOC date
    assert ooc_weekend["h"] is False  # unguarded: silent set-membership


def test_trading_days_between_vectors_spot_pins() -> None:
    """Spot-pin the slice-9 additive scalar family — both variants.

    The coverage-crossing range shows the divergence the family exists to
    freeze: the unguarded walk silently INCLUDES out-of-coverage weekdays,
    the checked walk raises (and discards everything — partial results are
    never returned)."""
    rows: list[dict[str, Any]] = _doc()["scalars"]["trading_days_between"]
    by_range = {(r["start"], r["end"]): r for r in rows}
    republic_week = by_range[("2026-01-23", "2026-01-30")]
    assert "2026-01-26" not in republic_week["days"]  # Republic Day elided
    assert republic_week["days"] == republic_week["checked"]  # in-coverage: identical
    budget = by_range[("2025-01-31", "2025-02-02")]
    assert "2025-02-01" in budget["days"]  # the Budget SATURDAY trades
    muhurat = by_range[("2020-11-13", "2020-11-16")]
    assert muhurat["days"] == ["2020-11-13"]  # the Muhurat Saturday does NOT
    crossing = by_range[("2026-12-28", "2027-01-05")]
    assert "2027-01-01" in crossing["days"]  # silent OOC-weekday inclusion
    assert crossing["checked"] == "raises"  # the strict walk refuses instead
    empty = by_range[("2026-07-06", "2026-07-05")]
    assert empty["days"] == [] and empty["checked"] == []


def test_muhurat_precedence_law_holds_on_every_muhurat_date() -> None:
    """Every Muhurat date: NOT a trading day AND a MUHURAT session — in data form."""
    rows: list[dict[str, Any]] = _doc()["scalars"]["muhurat_precedence"]
    assert len(rows) == 6  # 2020-2025; 2026 timings not yet notified (honest absence)
    for row in rows:
        assert row["td"] is False
        assert row["session_type"] == "muhurat"


def test_forced_exit_covers_every_muhurat_and_special_weekend_session() -> None:
    """One regular row + all 6 Muhurat rows (incl. the 2025 14:35 afternoon exit)
    + all 3 Budget-weekend rows."""
    rows: list[dict[str, Any]] = _doc()["scalars"]["forced_exit"]
    assert len(rows) == 1 + 6 + 3
    by_date = {r["d"]: r for r in rows}
    assert by_date["2024-07-01"]["exit_time"] == "15:20:00"
    assert by_date["2024-07-01"]["exit_seconds"] == 55200
    assert by_date["2025-10-21"]["exit_time"] == "14:35:00"  # afternoon Muhurat
    assert by_date["2025-10-21"]["exit_seconds"] == 52500
    assert by_date["2024-11-01"]["exit_time"] == "18:50:00"  # evening Muhurat
    assert by_date["2021-11-04"]["exit_time"] == "19:05:00"
    assert by_date["2025-02-01"]["exit_time"] == "15:20:00"  # Budget Saturday


# ── Exchange-law tripwire ────────────────────────────────────────────────────


@pytest.mark.parametrize("exchange", ["NSE", "BSE"])
def test_exchange_law_every_verdict_identical_to_default(exchange: str) -> None:
    """NSE == BSE == default for EVERY frozen verdict — itself frozen behavior.

    Both holiday sets are identical for 2020-2026 (``NSE_ONLY``/``BSE_ONLY``
    empty by verified fact). The first legitimately divergent circular fails
    HERE and forces a conscious regeneration — by design, cheaper than
    tripling the fixture.
    """
    doc = _doc()
    for record in [*doc["days"], *doc["strips"]]:
        d = date.fromisoformat(record["d"])
        assert verdict_record(d, exchange=exchange) == record, (
            f"exchange divergence at {record['d']} for {exchange}"
        )


# ── Structlog WARN event names (the port-shim observability contract) ────────


def test_out_of_coverage_warn_event_names_are_frozen() -> None:
    """The two abstain WARN event NAMES any port shim must keep emitting."""
    with capture_logs() as logs:
        with pytest.raises(HolidayDataUnavailableError):
            is_holiday_checked(date(2027, 1, 4))
        assert previous_trading_session(date(2019, 12, 23)) is None
    events = [e.get("event") for e in logs]
    assert "calendar.holiday_data_unavailable" in events
    assert "calendar.previous_session_coverage_exhausted" in events
