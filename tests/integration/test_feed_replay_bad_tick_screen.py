"""Integration: the bounded bad-tick screen wired into ``arrow_table_to_bars``.

The engine read boundary (DEC-SWEEP-DATA-EDGE-GUARDS-001, D-MAJ-2 + the
head-seed fix DEC-BADTICK-HEAD-SEED-001): a self-consistent SCALED bar —
internally ordered + positive, so it passes the DEC-STORED-DATA-VALIDATION-001
positivity screen — is SKIPPED-and-COUNTED before it can reach Stage-1A / run
identity, INCLUDING a poison FIRST bar of a session (the head-seed regression:
pre-fix, a kept ×10 head became the baseline and cascade-dropped every healthy
successor, collapsing the session to the single poison bar), while every clean
regular-day table is returned byte-identically.
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


def test_poison_first_bar_dropped_session_survives() -> None:
    # THE DEC-BADTICK-HEAD-SEED-001 regression (fails with the fix reverted):
    # a ×10 poison FIRST bar must be dropped by the median-of-first-3 head
    # vote, and every healthy successor KEPT — never the pre-fix collapse
    # where the poison head became the baseline, the 5 healthy bars were
    # cascade-dropped, and Stage-1A absorbed the lone poison bar as the whole
    # day's OHLC (poisoning the NEXT day's pivot/PDH-PDL/Fib anchor).
    rows = [(_BASE, 279400.0, 279500.0, 279300.0, 279400.0)] + [
        (
            _BASE + timedelta(minutes=i),
            27940.0 + i,
            27945.0 + i,
            27935.0 + i,
            27940.0 + i,
        )
        for i in range(1, 6)
    ]
    # A healthy control session the next day.
    rows += [
        (
            _BASE + timedelta(days=1, minutes=i),
            27950.0 + i,
            27955.0 + i,
            27945.0 + i,
            27950.0 + i,
        )
        for i in range(3)
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="NSE/INDEX/NSE-NIFTY/1minute")
    closes = [float(b.close) for b in bars]
    assert 279400.0 not in closes  # the poison head is GONE
    assert closes == [27941.0, 27942.0, 27943.0, 27944.0, 27945.0, 27950.0, 27951.0, 27952.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    assert warns[0]["session"] == "2024-01-02"


def test_reopen_gap_first_bars_of_session_not_tripped() -> None:
    # A large overnight gap opening a new session is legitimate — the second
    # session's head bars agree with each other, so none is dropped (cross-
    # session comparison is forbidden).
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0),  # new session, big gap
        (_BASE + timedelta(days=1, minutes=1), 6001.0, 6011.0, 5991.0, 6006.0),
        (_BASE + timedelta(days=1, minutes=2), 6002.0, 6012.0, 5992.0, 6007.0),
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert [float(b.close) for b in bars] == [27945.0, 6005.0, 6006.0, 6007.0]
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
    assert not [e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]


def test_source_none_still_screens() -> None:
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, 99.5, 100.5),
        (_BASE + timedelta(minutes=2), 101.0, 102.0, 100.0, 101.0),
        (_BASE + timedelta(minutes=3), 10.0, 10.1, 9.9, 10.0),  # ÷10 spike
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))  # no source
    assert [float(b.close) for b in bars] == [100.0, 100.5, 101.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["source"] is None


def test_two_bar_session_inconsistent_kept_and_disclosed() -> None:
    # The disclosed unconfirmable-head limit end-to-end: a 2-bar session whose
    # bars are mutually ≥ 4x apart keeps BOTH bars (no offender identifiable)
    # and emits ONE head-unconfirmed WARN — never a silent poison keep, never a
    # 50/50 data destruction.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 1000.0, 1001.0, 999.0, 1000.0),
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="src")
    assert [float(b.close) for b in bars] == [100.0, 1000.0]
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    unconfirmed = [e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]
    assert len(unconfirmed) == 1
    assert unconfirmed[0]["source"] == "src"


def test_non_positive_price_still_raises_before_the_screen_drop() -> None:
    # The positivity guard (DEC-STORED-DATA-VALIDATION-001) stays FIRST: a
    # non-positive stored bar raises loudly even when the bad-tick mask would
    # have dropped that row.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, 99.5, 100.5),
        (_BASE + timedelta(minutes=2), 101.0, 102.0, 100.0, 101.0),
        (_BASE + timedelta(minutes=3), -1000.0, -999.0, -1001.0, -1000.0),
    ]
    with pytest.raises(ValueError, match="non-positive price"):
        arrow_table_to_bars(_table(rows))


def test_refused_table_emits_bad_tick_warns_before_the_positivity_raise() -> None:
    # SCOPE PIN for the "byte-identical to #1616's ordering" claim (round-1
    # review): the whole-table mask pre-pass runs BEFORE the row loop, so on a
    # table the positivity guard REFUSES the pre-pass has already emitted the
    # table's bad-tick WARNs — the pre-#1625 streaming screen emitted none past
    # the raising row. Strictly MORE disclosure on a refused table, never less;
    # the table still raises identically and no bars are served either way.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, 99.5, 100.5),
        (_BASE + timedelta(minutes=2), 101.0, 102.0, 100.0, 101.0),
        (_BASE + timedelta(minutes=3), 10.0, 10.1, 9.9, 10.0),  # ÷10 spike (masked)
        (_BASE + timedelta(days=1), -5.0, -4.0, -6.0, -5.0),  # next session, refused
    ]
    with capture_logs() as logs, pytest.raises(ValueError, match="non-positive price"):
        arrow_table_to_bars(_table(rows), source="src")
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1  # the D1 spike's WARN landed before the raise
    assert warns[0]["skipped"] == 1
    assert warns[0]["session"] == "2024-01-02"
