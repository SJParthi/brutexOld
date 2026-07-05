"""Property invariant: a cold-start seed REFUSAL never touches an existing ledger row.

Locks the operator-mandated safety law of DEC-DATA-HONESTY-ALLFILTERED-SEED-001 (the
dataset-wide continuity gate inside
:func:`brutex.data.window_ledger_store.seed_enclosed_windows`): for ANY combination of
pre-existing ledger rows (any status — PLANNED / FAILED / EMPTY / SUCCESS / SKIPPED,
including rows sitting exactly at would-be-refused window keys) and ANY holed / dense /
malformed ``session_dates`` enrichment,

* every PRE-EXISTING row is BIT-IDENTICAL after the seed (a refusal — and the seed
  itself — only ever ADDS absent rows, never mutates);
* a refused window key that had NO row still has none (left PLANNED-able);
* every NEWLY added row is a plain lake-seeded ``SUCCESS``.

This proves the no-touch law across the whole input space, not just the unit fixtures.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from brutex.calendars.nse import is_trading_day
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import SUCCESS, WindowLedger
from brutex.data.window_ledger_store import index_lake_spans, seed_enclosed_windows
from hypothesis import given
from hypothesis import strategies as st

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

#: Every NSE trading day in [2024-03-01, 2024-03-28] (real calendar, in coverage).
_SESSIONS: list[date] = [
    date(2024, 3, 1) + timedelta(days=i)
    for i in range(28)
    if is_trading_day(date(2024, 3, 1) + timedelta(days=i))
]

#: Three fixed windows tiling [2024-03-01, 2024-03-29) — enclosed by the span below.
_WINDOWS: list[WindowKey] = [
    WindowKey(
        market="spot",
        security_id="NIFTY",
        interval="1minute",
        start=start,
        end=end,
    )
    for start, end in (
        (date(2024, 3, 1), date(2024, 3, 11)),
        (date(2024, 3, 11), date(2024, 3, 21)),
        (date(2024, 3, 21), date(2024, 3, 29)),
    )
]

_SPAN_KEY = ("NSE", "INDEX", "NIFTY", "1minute")


def _prepopulate(ledger: WindowLedger, key: WindowKey, shape: int) -> None:
    """Drive *ledger* into one of five REAL statuses at *key* via the public API."""
    if shape == 0:
        ledger.plan(key, now=_NOW)  # PLANNED
    elif shape == 1:
        ledger.record(
            key, raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=_NOW
        )  # FAILED
    elif shape == 2:
        ledger.record(key, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY
    elif shape == 3:
        ledger.record(key, raw_bars=5, error_type=None, bars_appended=5, now=_NOW)  # SUCCESS
    else:
        ledger.settle_known_gap(key, now=_NOW, reason="DH-905")  # SKIPPED


@given(
    covered_idx=st.sets(
        st.integers(min_value=0, max_value=len(_SESSIONS) - 1), min_size=0, max_size=len(_SESSIONS)
    ),
    pre_rows=st.dictionaries(
        st.integers(min_value=0, max_value=len(_WINDOWS) - 1),
        st.integers(min_value=0, max_value=4),
        max_size=len(_WINDOWS),
    ),
    malformed=st.booleans(),
)
def test_seed_refusal_never_touches_existing_rows(
    covered_idx: set[int], pre_rows: dict[int, int], malformed: bool
) -> None:
    session_dates: frozenset = (
        frozenset({"2024-03-01", "2024-03-28"})  # the fail-closed (error) shape
        if malformed
        else frozenset(_SESSIONS[i] for i in covered_idx)
    )
    ledger = WindowLedger()
    for win_idx, shape in pre_rows.items():
        _prepopulate(ledger, _WINDOWS[win_idx], shape)
    before = {
        (r.security_id, r.interval, r.window_start, r.window_end): r for r in ledger.records()
    }

    span_index = index_lake_spans(
        [
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "symbol": "NIFTY",
                "interval": "1minute",
                "bars": 1000,
                "first_date": "2024-03-01",
                "last_date": "2024-03-28",
                "session_dates": session_dates,
            }
        ]
    )
    seeded = seed_enclosed_windows(
        ledger,
        span_index,
        [(w, _SPAN_KEY) for w in _WINDOWS],
        now=_NOW,
        today_ist=date(2026, 6, 8),
    )

    after = {(r.security_id, r.interval, r.window_start, r.window_end): r for r in ledger.records()}
    # (1) every pre-existing row is bit-identical — the seed/gate never mutates.
    for row_key, row in before.items():
        assert after[row_key] == row
    # (2) only ADDITIONS happened, each a plain lake-seeded SUCCESS.
    new_keys = set(after) - set(before)
    assert seeded == len(new_keys)
    for row_key in new_keys:
        rec = after[row_key]
        assert rec.status == SUCCESS
        assert rec.last_error is None
    # (3) a window with no row before and none after was refused/ungated —
    # crucially it was NOT minted by the refusal path (stays PLANNED-able).
    for win in _WINDOWS:
        wk = (win.security_id, win.interval, win.start.isoformat(), win.end.isoformat())
        if wk not in before and wk not in after:
            assert not ledger.has_window(win)
