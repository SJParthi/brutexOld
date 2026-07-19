"""A faithful stand-in ``brutex_lake`` crate for the T24 detector laws.

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (SLICE T24 — PR-1 laws 1-3, PR-2
adds law 4, PR-3a adds the audit pair 5-6). The ONE fake-crate builder the facade suite
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


def fake_economics_lot_fold(
    group_ids: Sequence[int], under_ids: Sequence[int], lots: Sequence[int]
) -> tuple[list[int], list[list[int]], list[int], list[list[int]], int]:
    """The economics lot-fold law (PR-3a) — the crate mirror in Python.

    Flagged ids in ascending-id order; each flagged id's distinct lots
    sorted ascending; the observed distinct-group count last (the facade's
    handshake field).
    """
    n = len(group_ids)
    if len(under_ids) != n or len(lots) != n:
        raise ValueError("column lengths diverge")
    by_group: dict[int, set[int]] = {}
    by_under: dict[int, set[int]] = {}
    for group_id, under_id, lot in zip(group_ids, under_ids, lots, strict=True):
        by_group.setdefault(group_id, set()).add(lot)
        by_under.setdefault(under_id, set()).add(lot)
    mismatch_ids = sorted(g for g, s in by_group.items() if len(s) > 1)
    mismatch_lots = [sorted(by_group[g]) for g in mismatch_ids]
    revision_ids = sorted(u for u, s in by_under.items() if len(s) > 1)
    revision_lots = [sorted(by_under[u]) for u in revision_ids]
    return (mismatch_ids, mismatch_lots, revision_ids, revision_lots, len(by_group))


def fake_master_integrity_fold(
    spot_sym_key_ids: Sequence[int],
    spot_identity_ids: Sequence[int],
    spot_token_key_ids: Sequence[int],
    leg_sym_key_ids: Sequence[int],
    leg_tok_key_ids: Sequence[int],
) -> tuple[list[int], int]:
    """The master-integrity fold law (PR-3a) — the crate mirror in Python.

    Bucket codes mirror the crate's ``BUCKET_*`` order: 0 resolved_both ·
    1 symbol_only · 2 token_only · 3 orphan · 4 ambiguous ·
    5 disagreement; ``-1`` is the absent-key sentinel. Pass 1 replays the
    oracle's dict-op sequence exactly (the stored symbol mapping keeps the
    FIRST identity; a later distinct identity marks the key ambiguous;
    ``sym_ambiguous`` wins the classification).
    """
    n_spots = len(spot_sym_key_ids)
    if len(spot_identity_ids) != n_spots or len(spot_token_key_ids) != n_spots:
        raise ValueError("spot column lengths diverge")
    if len(leg_tok_key_ids) != len(leg_sym_key_ids):
        raise ValueError("leg column lengths diverge")
    spot_by_symbol: dict[int, int] = {}
    ambiguous_symbol_keys: set[int] = set()
    spot_by_token: dict[int, int] = {}
    spot_identities: set[int] = set()
    for sym_key, identity, tok_key in zip(
        spot_sym_key_ids, spot_identity_ids, spot_token_key_ids, strict=True
    ):
        spot_identities.add(identity)
        existing = spot_by_symbol.get(sym_key)
        if existing is not None and existing != identity:
            ambiguous_symbol_keys.add(sym_key)
        else:
            spot_by_symbol[sym_key] = identity
        if tok_key >= 0:
            spot_by_token[tok_key] = identity
    codes: list[int] = []
    referenced: set[int] = set()
    for sym_key, tok_key in zip(leg_sym_key_ids, leg_tok_key_ids, strict=True):
        sym_ambiguous = sym_key >= 0 and sym_key in ambiguous_symbol_keys
        sym_hit = spot_by_symbol.get(sym_key) if sym_key >= 0 else None
        tok_hit = spot_by_token.get(tok_key) if tok_key >= 0 else None
        if sym_ambiguous:
            codes.append(4)
        elif sym_hit is not None and tok_hit is not None and sym_hit != tok_hit:
            codes.append(5)
        elif sym_hit is not None and tok_hit is not None:
            codes.append(0)
            referenced.add(sym_hit)
        elif sym_hit is not None:
            codes.append(1)
            referenced.add(sym_hit)
        elif tok_hit is not None:
            codes.append(2)
            referenced.add(tok_hit)
        else:
            codes.append(3)
    return (codes, len(spot_identities - referenced))


def fake_detectors_crate(
    *,
    blake2b_hex: Callable[..., object] | None = None,
    interior_runs: Callable[..., object] | None = None,
    classify_partial: Callable[..., object] | None = None,
    classify_today: Callable[..., object] | None = None,
    economics_fold: Callable[..., object] | None = None,
    master_fold: Callable[..., object] | None = None,
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
        economics_lot_fold=(
            economics_fold if economics_fold is not None else fake_economics_lot_fold
        ),
        master_integrity_fold=(
            master_fold if master_fold is not None else fake_master_integrity_fold
        ),
        __version__=version,
    )
