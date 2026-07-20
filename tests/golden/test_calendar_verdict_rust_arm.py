"""Flag-ON Rust-arm replay of the calendar verdict-table golden.

DEC-CALENDAR-VERDICT-RUST-001 (slice 9, ADR-0019 / MIGRATION.md #1727): the
SAME frozen fixture + the SAME harness projection as
``test_calendar_verdict_table_golden.py`` (which keeps pinning the flag-OFF
Python arm), replayed with ``BRUTEX_RUST_CALENDAR=1`` — so the Rust
``CalendarVerdictEngine``, built from the REAL ``nse.py`` module constants,
must reproduce every frozen verdict BIT-FOR-BIT through the public dispatch
seam. Two modules · one fixture · one harness — no CI matrix, no job
duplication (the golden job discovers this module by placement).

Skips VISIBLY when the installed ``fno_math`` wheel predates the slice-9
``CalendarVerdictEngine`` symbol (a stale local wheel), never silently.

The slice-9 review-round additive fixture extension (the ``h``/``hc``/``we``
per-date columns — ``is_holiday`` / ``is_holiday_checked`` / ``is_weekend`` —
plus the ``trading_days_between[_checked]`` scalar family; ledgered in
MIGRATION.md #1727a) rides the SAME shared harness, so this module replays
all 5 previously-unpinned dispatch heads flag-ON automatically — the Rust
arm now reproduces the COMPLETE 17-head answer key bit-for-bit.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from brutex.calendars import nse

from tests.golden._calendar_verdict_harness import (
    CALENDAR_FIXTURE,
    build_scalars,
    build_strips,
    verdict_record,
)

fno_math = pytest.importorskip("fno_math")
if not hasattr(fno_math, "CalendarVerdictEngine"):  # pragma: no cover - stale-wheel guard
    pytest.skip(
        "installed fno_math wheel predates CalendarVerdictEngine (slice 9) — rebuild the wheel",
        allow_module_level=True,
    )

_YEARS = (2020, 2021, 2022, 2023, 2024, 2025, 2026)


@pytest.fixture(autouse=True, scope="module")
def _rust_calendar_arm() -> Iterator[None]:
    """Arm the Rust calendar for the WHOLE module; restore the seam after."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(nse.RUST_CALENDAR_ENV, "1")
        nse._reset_rust_calendar_for_tests()  # pyright: ignore[reportPrivateUsage]
        kernel = nse._rust_calendar()  # pyright: ignore[reportPrivateUsage]
        assert kernel is not None, "the Rust calendar kernel must engage (vacuous-gate trap)"
        yield
    nse._reset_rust_calendar_for_tests()  # pyright: ignore[reportPrivateUsage]


def _doc() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(CALENDAR_FIXTURE.read_text(encoding="utf-8"))
    return doc


@pytest.mark.parametrize("year", _YEARS)
def test_days_replay_rust_arm(year: int) -> None:
    """Every frozen per-date record replays identically through the Rust arm."""
    records = [r for r in _doc()["days"] if int(r["d"][:4]) == year]
    assert records, f"no fixture records for {year}"
    for record in records:
        d = date.fromisoformat(record["d"])
        assert verdict_record(d) == record, f"rust-arm verdict drift at {record['d']}"


def test_strips_replay_rust_arm() -> None:
    """The out-of-coverage abstain TRIAGE replays identically (both strips)."""
    assert build_strips() == _doc()["strips"]


def test_scalars_replay_rust_arm() -> None:
    """Every scalar family replays identically through the Rust arm."""
    frozen: dict[str, Any] = _doc()["scalars"]
    live = build_scalars()
    assert sorted(live.keys()) == sorted(frozen.keys())
    for family, vectors in frozen.items():
        assert live[family] == vectors, f"rust-arm scalar drift in family {family!r}"


def test_kernel_never_autoreverted_by_the_replay() -> None:
    """The full-span replay left the kernel engaged — zero burn-in mismatches.

    Runs LAST in the module (test order is file order): a single divergent
    verdict anywhere in the 2,557-day + strips + scalar replay would have
    latched the autorevert, so this is the aggregated no-mismatch proof.
    """
    assert nse._CAL_DISABLED_REASON[0] is None  # pyright: ignore[reportPrivateUsage]
    assert nse._rust_calendar() is not None  # pyright: ignore[reportPrivateUsage]
