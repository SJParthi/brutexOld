"""A faithful stand-in ``brutex_lake`` crate for the T24 detector laws.

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (SLICE T24 — PR-1 laws 1-3, PR-2
adds law 4). The ONE fake-crate builder the facade suite
(``test_detectors_rust.py``) and the target seam suites share (CLAUDE.md
§11 COMMON — no per-file copy): a ``SimpleNamespace`` exposing the exact
``_CRATE_API`` surface with law-faithful pure-Python implementations, so
ON-arm wiring/coverage tests run WITHOUT the built wheel (the real-wheel
differentials are separate, ``importorskip``-gated). Each law callable is
overridable for poison/degrade/panic scenarios (the
``test_window_ledger_rust.py`` ``_fake_crate`` convention, hoisted because
FIVE suites need it).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from types import SimpleNamespace


def fake_blake2b_hex(data: bytes, digest_size: int) -> str:
    """The digest law — delegated straight to the hashlib oracle."""
    return hashlib.blake2b(data, digest_size=digest_size).hexdigest()


def fake_interior_barless_runs(
    covered: Sequence[int], interior_sessions: Sequence[int]
) -> list[tuple[int, int, int]]:
    """The run-walker law over ordinals — the crate mirror in Python."""
    if len(covered) < 2:
        return []
    have = set(covered)
    runs: list[tuple[int, int, int]] = []
    run_first: int | None = None
    run_last: int | None = None
    run = 0
    for session in interior_sessions:
        if session in have:
            if run_first is not None and run_last is not None:
                runs.append((run_first, run_last, run))
            run_first = None
            run_last = None
            run = 0
        else:
            if run_first is None:
                run_first = session
            run_last = session
            run += 1
    if run_first is not None and run_last is not None:
        runs.append((run_first, run_last, run))
    return runs


def fake_classify_partial_window_verdict(
    last_bar_ordinal: int | None,
    window_end_ordinal: int,
    today_ordinal: int,
    expiry_ordinal: int | None,
    coverage_ok: bool,
    tolerance: int,
    live_edge: int,
    span_start_ordinal: int,
    trading_flags: Sequence[bool],
) -> tuple[str, int | None, int | None, int] | None:
    """The partial-window verdict law over the session grid — the crate mirror.

    ``None`` = a session walk stepped off the grid (the ABSTAIN contract).
    """

    def is_session(day: int) -> bool | None:
        idx = day - span_start_ordinal
        if idx < 0 or idx >= len(trading_flags):
            return None
        return trading_flags[idx]

    def last_session_on_or_before(day: int) -> int | None:
        cursor = day
        while True:
            flag = is_session(cursor)
            if flag is None:
                return None
            if flag:
                return cursor
            cursor -= 1

    def sessions_between(start: int, end: int) -> int | None:
        if end <= start:
            return 0
        count = 0
        for cursor in range(start + 1, end + 1):
            flag = is_session(cursor)
            if flag is None:
                return None
            if flag:
                count += 1
        return count

    if last_bar_ordinal is None:
        return ("empty", None, None, 0)
    if not coverage_ok:
        return ("out_of_calendar_coverage", last_bar_ordinal, None, 0)
    end_exclusive = window_end_ordinal - 1
    if expiry_ordinal is not None and expiry_ordinal < end_exclusive:
        end_exclusive = expiry_ordinal
    end_exclusive = min(end_exclusive, today_ordinal)
    expected_end = last_session_on_or_before(end_exclusive)
    if expected_end is None:
        return None
    if expiry_ordinal is not None:
        expiry_session = last_session_on_or_before(expiry_ordinal)
        if expiry_session is None:
            return None
        if expected_end >= expiry_session:
            expected_end = expiry_session
            if last_bar_ordinal >= expiry_session:
                return ("at_expiry", last_bar_ordinal, expected_end, 0)
    today_session = last_session_on_or_before(today_ordinal)
    if today_session is None:
        return None
    horizon = sessions_between(expected_end, today_session)
    if horizon is None:
        return None
    if horizon < live_edge:
        return ("live_edge", last_bar_ordinal, expected_end, 0)
    missing = sessions_between(last_bar_ordinal, expected_end)
    if missing is None:
        return None
    if missing == 0:
        return ("complete", last_bar_ordinal, expected_end, 0)
    if missing > tolerance:
        return ("truncated", last_bar_ordinal, expected_end, missing)
    return ("sparse_complete", last_bar_ordinal, expected_end, missing)


def fake_classify_today_completeness_verdict(
    instrument_class: int,
    expected_minutes: int,
    minute_ordinals: Sequence[int],
    zero_volume: Sequence[bool],
    oi_null: Sequence[bool],
    last_bar_complete: bool,
) -> tuple[bool, int, int, int, int, bool]:
    """The today-completeness criteria law (PR-2) — the crate mirror in Python.

    Reason indexes mirror the crate's ``TODAY_REASON_*`` order (= the
    facade's ``_TODAY_REASONS`` tuple): 0 complete · 1 no_today_bars ·
    2 count_short · 3 count_excess · 4 last_bar_incomplete ·
    5 oi_null_settling · 6 volume_all_zero.
    """
    n = len(minute_ordinals)
    if len(zero_volume) != n or len(oi_null) != n:
        raise ValueError("column lengths diverge")
    if not 0 <= instrument_class <= 3:
        raise ValueError(f"unknown instrument-class code {instrument_class}")
    distinct = len(set(minute_ordinals))
    oi_null_count = sum(1 for flag in oi_null if flag)
    zero_volume_count = sum(1 for flag in zero_volume if flag)
    reason = 0
    incomplete = False
    count_short_observed = False
    if instrument_class == 0:  # INDEX_SPOT
        if distinct < expected_minutes:
            reason, incomplete = 2, True
        elif distinct > expected_minutes:
            reason, incomplete = 3, True
        elif n > 0 and not last_bar_complete:
            reason, incomplete = 4, True
    elif n == 0:
        reason = 1
    elif instrument_class == 1:  # CASH_SPOT
        if zero_volume_count == n:
            reason, incomplete = 6, True
    elif instrument_class == 2:  # FUT
        count_short_observed = distinct < expected_minutes
        if oi_null_count > 0:
            reason, incomplete = 5, True
        elif zero_volume_count == n:
            reason, incomplete = 6, True
    elif oi_null_count > 0:  # OPTION
        reason, incomplete = 5, True
    return (incomplete, reason, distinct, oi_null_count, zero_volume_count, count_short_observed)


def fake_detectors_crate(
    *,
    blake2b_hex: Callable[..., object] | None = None,
    interior_runs: Callable[..., object] | None = None,
    classify_partial: Callable[..., object] | None = None,
    classify_today: Callable[..., object] | None = None,
    version: str = "0.1.0-fake",
) -> SimpleNamespace:
    """A stand-in ``brutex_lake`` module exposing the T24 ``_CRATE_API`` surface."""
    return SimpleNamespace(
        raw_integrity_blake2b_hex=(blake2b_hex if blake2b_hex is not None else fake_blake2b_hex),
        interior_barless_runs=(
            interior_runs if interior_runs is not None else fake_interior_barless_runs
        ),
        classify_partial_window_verdict=(
            classify_partial
            if classify_partial is not None
            else fake_classify_partial_window_verdict
        ),
        classify_today_completeness_verdict=(
            classify_today
            if classify_today is not None
            else fake_classify_today_completeness_verdict
        ),
        __version__=version,
    )
