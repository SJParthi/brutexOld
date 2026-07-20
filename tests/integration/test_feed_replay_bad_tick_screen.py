"""Integration: the bounded bad-tick screen wired into ``arrow_table_to_bars``.

The engine read boundary (DEC-SWEEP-DATA-EDGE-GUARDS-001, D-MAJ-2 — re-derived
for the DEC-BADTICK-HEAD-SEED-001 session-head median-confirm law + the
DEC-BADTICK-HILO-SCREEN-001 high/low arm): a self-consistent SCALED bar —
internally ordered + positive, so it passes the
DEC-STORED-DATA-VALIDATION-001 positivity screen — is SKIPPED-and-COUNTED
before it can reach Stage-1A / run identity, a poison session HEAD loses the
median vote instead of seating the cascade baseline, and an out-of-band
high/low drops its whole bar; every clean regular-day table is returned
byte-identically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from brutex.data.bad_tick import BAD_TICK_HEAD_UNCONFIRMED_EVENT, BAD_TICK_SKIP_EVENT
from brutex.feed.replay import arrow_table_to_bars
from structlog.testing import capture_logs

_BASE = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)  # 09:15 IST


def _table(rows: list[tuple[datetime, float, float, float, float]]) -> pa.Table:
    """Build the canonical 7-column bar table from (ts, o, h, l, c) rows."""
    return pa.table(
        {
            "timestamp": pa.array([r[0] for r in rows], type=pa.timestamp("us", tz="UTC")),
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [10] * len(rows),
            "open_interest": [None] * len(rows),
        }
    )


def _bar(minute: int, close: float, *, high: float | None = None) -> tuple:
    """One tight (±0.01 unless *high* overrides) bar at 09:15 IST + *minute*."""
    ts = _BASE + timedelta(minutes=minute)
    h = high if high is not None else close + 0.01
    return (ts, close, h, close - 0.01, close)


def test_scaled_bar_skipped_with_one_warn() -> None:
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5),  # ÷10 spike
        (_BASE + timedelta(minutes=2), 27946.0, 27960.0, 27940.0, 27955.0),  # good vs 27945
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="NSE/INDEX/NSE-NIFTY/1minute")
    # The 3-bar head votes vs its median (27945 @ row 0): the ÷10 spike loses.
    assert [float(b.close) for b in bars] == [27945.0, 27955.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    assert warns[0]["source"] == "NSE/INDEX/NSE-NIFTY/1minute"
    assert warns[0]["first_ref_close"] == 27945.0
    assert warns[0]["first_reason"] == "close"


def test_poison_head_bar_no_longer_collapses_its_session() -> None:
    """RED-first R1 (#1625): a ×10 poison FIRST bar must be dropped, not seat
    the baseline — the pre-port first-bar-exempt law kept it and cascaded the
    session's REAL bars away."""
    rows = [_bar(0, 279400.0), _bar(1, 27940.0), _bar(2, 27941.0), _bar(3, 27942.0)]
    bars = arrow_table_to_bars(_table(rows), source="repro")
    assert [float(b.close) for b in bars] == [27940.0, 27941.0, 27942.0]


def test_x10_high_with_sane_close_is_dropped() -> None:
    """RED-first R2 (#1687 DATA-3b): a ×10 HIGH with a sane close must drop
    the whole bar (the hi/lo arm) — pre-port the bar was served and the
    poisoned high laundered into every derived bucket."""
    rows = [_bar(0, 100.0), _bar(1, 101.0), _bar(2, 102.0, high=1020.0), _bar(3, 103.0)]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="repro")
    assert [float(b.close) for b in bars] == [100.0, 101.0, 103.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skip_reasons"] == {"high": 1}
    assert warns[0]["first_reason"] == "high"


def test_reopen_gap_first_bar_of_session_not_tripped() -> None:
    # A large overnight gap opening a new session is legitimate — the head
    # buffer restarts per session (single-bar sessions have no witness and
    # stay silent).
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0),  # new session, big gap
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert [float(b.close) for b in bars] == [27945.0, 6005.0]
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_two_bar_session_head_unconfirmed_disclosed_kept() -> None:
    # DEC-BADTICK-HEAD-SEED-001 honest limit: a mutually-inconsistent 2-bar
    # session names no offender — BOTH bars serve, one disclosure WARN.
    rows = [_bar(0, 100.0), _bar(1, 1000.0)]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="src")
    assert [float(b.close) for b in bars] == [100.0, 1000.0]
    heads = [e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]
    assert len(heads) == 1
    assert heads[0]["first_close"] == 100.0
    assert heads[0]["second_close"] == 1000.0
    assert heads[0]["source"] == "src"


def test_clean_table_byte_identical() -> None:
    rows = [(_BASE + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5) for i in range(5)]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert len(bars) == 5
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_kill_switch_disables_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(minutes=1), 27941.0, 27951.0, 27931.0, 27946.0),
        (_BASE + timedelta(minutes=2), 27942.0, 27952.0, 27932.0, 27947.0),
        (_BASE + timedelta(minutes=3), 2794.5, 2795.0, 2793.0, 2794.5),  # spike kept when off
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert len(bars) == 4  # nothing skipped
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_source_none_still_screens() -> None:
    rows = [
        _bar(0, 100.0),
        _bar(1, 100.0),
        _bar(2, 100.0),
        _bar(3, 10.0),  # ÷10 post-head spike
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))  # no source
    assert [float(b.close) for b in bars] == [100.0, 100.0, 100.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["source"] is None
