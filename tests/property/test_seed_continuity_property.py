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


# ── DEC-SEED-OBSERVED-OVERLAY-001: seed/fetch parity + overlay monotonicity ──────────


def _seed_verdicts(session_dates: frozenset, observed: frozenset) -> dict[str, bool]:
    """Run the seed over the three windows; map ``window_start -> seeded?``."""
    ledger = WindowLedger()
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
    seed_enclosed_windows(
        ledger,
        span_index,
        [(w, _SPAN_KEY) for w in _WINDOWS],
        now=_NOW,
        today_ist=date(2026, 6, 8),
        observed=observed,
    )
    return {w.start.isoformat(): ledger.has_window(w) for w in _WINDOWS}


@given(
    covered_idx=st.sets(
        st.integers(min_value=0, max_value=len(_SESSIONS) - 1), min_size=0, max_size=len(_SESSIONS)
    ),
    overlay_idx=st.sets(
        st.integers(min_value=0, max_value=len(_SESSIONS) - 1), min_size=0, max_size=6
    ),
)
def test_seed_gate_matches_fetch_classifier_and_overlay_is_monotone(
    covered_idx: set[int], overlay_idx: set[int]
) -> None:
    """(1) SINGLE-ARITHMETIC-SOURCE parity: the seed's refusal verdict equals an
    INDEPENDENT window-intersection over the fetch-time ``interior_gap_runs``
    classification with the SAME overlay. (2) MONOTONICITY: the overlay only
    REMOVES trading days, so ``refused(observed) ⊆ refused(∅)`` — threading it
    can make the seed strictly MORE permissive, never newly refusing — for ANY
    overlay shape, stale entries over covered days included."""
    from datetime import timedelta

    from brutex.data.interior_gap_detect import interior_gap_runs

    session_dates = frozenset(_SESSIONS[i] for i in covered_idx)
    # ANY overlay shape — deliberately INCLUDING ``observed ∩ covered != ∅``
    # (a STALE overlay entry over a since-BACKFILLED day: an entry self-heals
    # only when ``brutex detect-session-drift`` next re-runs, NOT the moment
    # bars appear, so the non-disjoint shape IS reachable in production). The
    # run-walker breaks a run on every COVERED static session BEFORE
    # consulting the overlay (DEC-SEED-OBSERVED-OVERLAY-001 review fold), so
    # the monotonicity law below is STRUCTURAL — a stale entry can never MERGE
    # two flanking runs into a new refusal.
    observed = frozenset(_SESSIONS[i] for i in overlay_idx)

    with_overlay = _seed_verdicts(session_dates, observed)
    without_overlay = _seed_verdicts(session_dates, frozenset())

    # (2) Monotonicity: every window seeded WITHOUT the overlay also seeds WITH it.
    for start_iso, seeded_plain in without_overlay.items():
        if seeded_plain:
            assert with_overlay[start_iso], "the overlay newly REFUSED a window"

    # (1) Parity: refused iff a fetch-time confirmed run INTERSECTS the window
    # (an abstain — None — never refuses; both sides share the abstain rule).
    runs = interior_gap_runs(covered_dates=session_dates, observed=observed)
    for window in _WINDOWS:
        last_covered = window.end - timedelta(days=1)
        expected_refusal = runs is not None and any(
            run_first <= last_covered and window.start <= run_last for run_first, run_last in runs
        )
        assert with_overlay[window.start.isoformat()] == (not expected_refusal)


# ── DEC-ALLFILTERED-MASS-BREAKER-001: no-trip ⇒ ledger outcomes byte-identical ───────


@given(
    outcomes=st.lists(
        st.tuples(st.integers(min_value=0, max_value=1), st.booleans()),
        max_size=20,
    )
)
def test_below_conjunct_breaker_never_alters_ledger_outcomes(
    outcomes: list[tuple[int, bool]],
) -> None:
    """For ANY window-outcome interleaving confined to ≤ 2 series (below the
    SPREAD floor and the FRACTION series floor), the breaker never trips, so the
    record-time ``allow_degraded_settle`` re-check (the production tail shape
    since the review-round-1 TOCTOU fix) always allows and the ledger rows are
    BYTE-IDENTICAL to a breaker-absent run."""
    from brutex.adapters.all_filtered_breaker import AllFilteredBreaker

    breaker = AllFilteredBreaker(
        spread_series_limit=3, fraction_limit=0.5, min_observations=5, fraction_min_series=3
    )
    with_breaker = WindowLedger()
    without_breaker = WindowLedger()
    for i, (series_idx, all_filtered) in enumerate(outcomes):
        key = WindowKey(
            market="spot",
            security_id=f"SYM{series_idx}",
            interval="1minute",
            start=date(2024, 3, 1) + timedelta(days=i),
            end=date(2024, 3, 2) + timedelta(days=i),
        )
        breaker.observe(
            f"SYM{series_idx}:1minute",
            all_filtered=all_filtered,
            window_start=key.start.isoformat(),
            window_end=key.end.isoformat(),
        )
        # The EXACT tail wiring: the breaker's allow_settle is threaded as the
        # record-time re-check for an all-filtered record, never a pre-computed bool.
        allow = breaker.allow_settle if all_filtered else None
        common = {
            "raw_bars": 3,
            "error_type": None,
            "bars_appended": 0 if all_filtered else 3,
            "now": _NOW,
            "degraded": all_filtered,
            "degraded_reason": "ALL_FILTERED" if all_filtered else None,
            "pull_id": f"p{i}",
        }
        with_breaker.record(key, allow_degraded_settle=allow, **common)  # type: ignore[arg-type]
        without_breaker.record(key, **common)  # type: ignore[arg-type]
    assert not breaker.tripped
    assert with_breaker.to_rows() == without_breaker.to_rows()
