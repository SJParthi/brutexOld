"""Integration: the bounded bad-tick screen wired into ``arrow_table_to_bars``.

The engine read boundary (DEC-SWEEP-DATA-EDGE-GUARDS-001, D-MAJ-2): a
self-consistent SCALED bar — internally ordered + positive, so it passes the
DEC-STORED-DATA-VALIDATION-001 positivity screen — is SKIPPED-and-COUNTED before
it can reach Stage-1A / run identity, while every clean regular-day table is
returned byte-identically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from brutex.data.bad_tick import BAD_TICK_SKIP_EVENT
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


def test_scaled_bar_skipped_with_one_warn() -> None:
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5),  # ÷10 spike
        (_BASE + timedelta(minutes=2), 27946.0, 27960.0, 27940.0, 27955.0),  # good vs 27945
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="NSE/INDEX/NSE-NIFTY/1minute")
    assert [float(b.close) for b in bars] == [27945.0, 27955.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    assert warns[0]["source"] == "NSE/INDEX/NSE-NIFTY/1minute"


def test_reopen_gap_first_bar_of_session_not_tripped() -> None:
    # A large overnight gap opening a new session is legitimate — the first bar
    # of the second session is exempt.
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0),  # new session, big gap
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert [float(b.close) for b in bars] == [27945.0, 6005.0]
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


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
        (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5),  # spike kept when off
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert len(bars) == 2  # nothing skipped
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_source_none_still_screens() -> None:
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 10.0, 10.1, 9.9, 10.0),  # ÷10 spike
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))  # no source
    assert [float(b.close) for b in bars] == [100.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["source"] is None
