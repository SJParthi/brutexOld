"""Unit tests for the bounded bad-tick screen (``brutex.data.bad_tick``).

100% line + branch coverage of the spike / fat-finger / vendor-scaling guard at
the engine read boundary (DEC-SWEEP-DATA-EDGE-GUARDS-001, D-MAJ-2) with the
EVIDENCE-CONFIRMED session-head seed (DEC-BADTICK-HEAD-SEED-001 — a poison
FIRST bar is itself dropped by the median-of-first-3 head vote instead of
becoming an unhealable cascade baseline). The end-to-end wiring through
:func:`arrow_table_to_bars` is exercised in
``tests/integration/test_feed_replay_bad_tick_screen.py`` (stem-named
``test_feed_replay*`` so the coverage-gate discovery maps it to
``brutex/feed/replay.py``).
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.data.bad_tick import (
    BAD_TICK_HEAD_UNCONFIRMED_EVENT,
    BAD_TICK_MAX_RATIO,
    BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT,
    BAD_TICK_SKIP_EVENT,
    HEAD_CONFIRM_BARS,
    BadTickScreen,
    bad_tick_screen_enabled,
    is_bad_tick,
)
from structlog.testing import capture_logs

# ────────────────────────────────────────────────────────────────────────
# bad_tick_screen_enabled — default ON, falsy kill switch
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", " no ", "Off", "off"])
def test_disabled_only_for_explicit_falsy(raw: str) -> None:
    assert bad_tick_screen_enabled(raw) is False


@pytest.mark.parametrize("raw", ["", "1", "true", "yes", "on", "anything", "  "])
def test_enabled_for_everything_else(raw: str) -> None:
    assert bad_tick_screen_enabled(raw) is True


def test_enabled_default_true_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
    assert bad_tick_screen_enabled() is True


def test_enabled_reads_env_when_raw_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
    assert bad_tick_screen_enabled() is False


# ────────────────────────────────────────────────────────────────────────
# is_bad_tick — the pure bounded-ratio predicate
# ────────────────────────────────────────────────────────────────────────


def test_scaled_down_by_ten_is_bad() -> None:
    # 27940 -> 2794 (÷10): ratio 0.1 < 1/4 → bad.
    assert is_bad_tick(27940.0, 2794.0) is True


def test_scaled_up_by_ten_is_bad() -> None:
    # 27940 -> 279400 (×10): ratio 10 >= 4 → bad.
    assert is_bad_tick(27940.0, 279400.0) is True


def test_normal_minute_move_is_good() -> None:
    # A ~0.04% minute move is nowhere near the 4x band.
    assert is_bad_tick(27940.0, 27950.0) is False


def test_exact_boundary_is_bad_both_directions() -> None:
    # Exactly max_ratio (>= / <=) counts as bad.
    assert is_bad_tick(100.0, 400.0) is True  # up: 400 >= 100*4
    assert is_bad_tick(400.0, 100.0) is True  # down: 100*4 <= 400


def test_just_inside_boundary_is_good() -> None:
    assert is_bad_tick(100.0, 399.99) is False
    assert is_bad_tick(400.0, 100.01) is False


def test_custom_max_ratio() -> None:
    assert is_bad_tick(100.0, 250.0, max_ratio=2.0) is True
    assert is_bad_tick(100.0, 250.0, max_ratio=3.0) is False


def test_symmetric_in_the_ratio_sense() -> None:
    # The 2-bar unconfirmable-head rationale: the pair identifies no offender.
    assert is_bad_tick(100.0, 1000.0) == is_bad_tick(1000.0, 100.0) is True
    assert is_bad_tick(100.0, 101.0) == is_bad_tick(101.0, 100.0) is False


def test_default_constants() -> None:
    assert BAD_TICK_MAX_RATIO == 4.0
    assert HEAD_CONFIRM_BARS == 3


# ────────────────────────────────────────────────────────────────────────
# BadTickScreen.screen_mask — single-pass in-session screen
# ────────────────────────────────────────────────────────────────────────

_D1 = date(2024, 1, 2)
_D2 = date(2024, 1, 3)


def _mask(rows: list[tuple[date, float]], *, screen: BadTickScreen | None = None) -> list[bool]:
    """Run one table's (ist_date, close) rows through the screen."""
    scr = screen if screen is not None else BadTickScreen()
    return scr.screen_mask([r[0] for r in rows], [r[1] for r in rows])


def test_empty_input_returns_empty_mask() -> None:
    with capture_logs() as logs:
        assert BadTickScreen().screen_mask([], []) == []
    assert not logs


def test_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        BadTickScreen().screen_mask([_D1], [100.0, 101.0])


def test_clean_session_all_kept_no_warn() -> None:
    rows = [(_D1, 27940.0 + i) for i in range(6)]
    with capture_logs() as logs:
        assert _mask(rows) == [True] * 6
    assert not logs


# ── the head-seed regression (DEC-BADTICK-HEAD-SEED-001) ────────────────
# Poison-position permutations over a ≥ 3-bar session: the corrupt head bar is
# itself dropped by the median vote at EVERY head position, and the healthy
# remainder of the session survives (the pre-fix screen kept a poison FIRST bar
# as the baseline and cascade-dropped every healthy successor — these FAIL with
# the fix reverted).


def test_poison_first_bar_dropped_healthy_session_survives() -> None:
    # THE bug-1 shape: ×10 poison head, 5 healthy successors.
    rows = [(_D1, 279400.0)] + [(_D1, 27940.0 + i) for i in range(1, 6)]
    with capture_logs() as logs:
        assert _mask(rows) == [False, True, True, True, True, True]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    # The head skip's reference is the head-window MEDIAN (27942), not a
    # "previous close" — the field is named accordingly.
    assert warns[0]["first_ref_close"] == 27942.0
    assert warns[0]["first_close"] == 279400.0


def test_poison_second_bar_dropped() -> None:
    rows = [(_D1, 27940.0), (_D1, 2794.0), (_D1, 27942.0), (_D1, 27943.0)]
    assert _mask(rows) == [True, False, True, True]


def test_poison_third_bar_dropped_baseline_is_second() -> None:
    rows = [(_D1, 27940.0), (_D1, 27941.0), (_D1, 279400.0), (_D1, 27942.0)]
    # Head median = 27941 → bar 3 dropped; baseline = 27941 (last KEPT head
    # bar) so bar 4 is judged against it and kept.
    assert _mask(rows) == [True, True, False, True]


def test_poison_never_advances_baseline_after_head() -> None:
    # Mid-session spike AFTER the head resolved: dropped, and the NEXT bar is
    # judged against the last KEPT close (no cascade).
    rows = [
        (_D1, 27940.0),
        (_D1, 27941.0),
        (_D1, 27942.0),
        (_D1, 2794.0),  # ÷10 spike — dropped vs baseline 27942
        (_D1, 27943.0),  # judged vs 27942 (NOT the spike) — kept
    ]
    assert _mask(rows) == [True, True, True, False, True]


def test_two_consecutive_mid_session_spikes_one_warn() -> None:
    with capture_logs() as logs:
        rows = [
            (_D1, 27940.0),
            (_D1, 27941.0),
            (_D1, 27942.0),
            (_D1, 2794.0),
            (_D1, 2795.0),
        ]
        screen = BadTickScreen(source="NSE/INDEX/NSE-NIFTY/1minute")
        assert _mask(rows, screen=screen) == [True, True, True, False, False]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 2
    assert warns[0]["session"] == "2024-01-02"
    assert warns[0]["source"] == "NSE/INDEX/NSE-NIFTY/1minute"
    assert warns[0]["first_ref_close"] == 27942.0
    assert warns[0]["first_close"] == 2794.0
    assert warns[0]["max_ratio"] == BAD_TICK_MAX_RATIO


def test_head_and_mid_session_skips_fold_into_one_warn() -> None:
    rows = [
        (_D1, 279400.0),  # poison head (dropped by the median vote)
        (_D1, 27940.0),
        (_D1, 27941.0),
        (_D1, 2794.0),  # mid-session spike
        (_D1, 27942.0),
    ]
    with capture_logs() as logs:
        assert _mask(rows) == [False, True, True, False, True]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 2


def test_one_warn_per_session_flushed_at_session_boundary() -> None:
    with capture_logs() as logs:
        rows = [
            (_D1, 27940.0),
            (_D1, 27941.0),
            (_D1, 27942.0),
            (_D1, 2794.0),  # D1 spike
            (_D2, 27000.0),
            (_D2, 27001.0),
            (_D2, 27002.0),
            (_D2, 2700.0),  # D2 spike
        ]
        assert _mask(rows) == [True, True, True, False, True, True, True, False]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert [w["session"] for w in warns] == ["2024-01-02", "2024-01-03"]
    assert all(w["skipped"] == 1 for w in warns)


def test_reopen_gap_across_sessions_never_trips() -> None:
    # Cross-session comparison is FORBIDDEN (a stock split must never
    # false-trip): a ÷10 overnight gap between two internally-consistent
    # sessions keeps every bar with no warn.
    rows = [(_D1, 27940.0 + i) for i in range(4)] + [(_D2, 2794.0 + i) for i in range(4)]
    with capture_logs() as logs:
        assert _mask(rows) == [True] * 8
    assert not logs


def test_genuine_head_gap_agreeing_bars_never_false_trip() -> None:
    # A large reopen gap is legitimate when the head bars AGREE with each other
    # (the median is one of them) — all kept, no warn.
    rows = [(_D1, 100.0), (_D2, 6000.0), (_D2, 6001.0), (_D2, 6002.0), (_D2, 6003.0)]
    with capture_logs() as logs:
        assert _mask(rows) == [True] * 5
    assert not logs


# ── head-window permutation corners ─────────────────────────────────────


def test_one_bar_session_kept_unconfirmable_silent() -> None:
    # No second witness at all → kept, no warn (documented honest limit).
    with capture_logs() as logs:
        assert _mask([(_D1, 999999.0)]) == [True]
    assert not logs


def test_two_bar_session_consistent_both_kept_no_warn() -> None:
    with capture_logs() as logs:
        assert _mask([(_D1, 100.0), (_D1, 101.0)]) == [True, True]
    assert not logs


def test_two_bar_session_inconsistent_both_kept_one_unconfirmed_warn() -> None:
    # The symmetric ratio test identifies no offender → keep BOTH, disclose.
    with capture_logs() as logs:
        screen = BadTickScreen(source="src")
        assert _mask([(_D1, 100.0), (_D1, 1000.0)], screen=screen) == [True, True]
    unconfirmed = [e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]
    assert len(unconfirmed) == 1
    assert unconfirmed[0]["session"] == "2024-01-02"
    assert unconfirmed[0]["source"] == "src"
    assert unconfirmed[0]["first_close"] == 100.0
    assert unconfirmed[0]["second_close"] == 1000.0
    assert unconfirmed[0]["max_ratio"] == BAD_TICK_MAX_RATIO
    # No skip warn — nothing was skipped.
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_two_bar_session_flushed_by_next_session_boundary() -> None:
    # The unconfirmable 2-bar head resolves at the SESSION boundary too, not
    # only at stream end; the next session screens independently.
    rows = [
        (_D1, 100.0),
        (_D1, 1000.0),  # inconsistent pair — kept + disclosed
        (_D2, 200.0),
        (_D2, 201.0),
        (_D2, 202.0),
        (_D2, 2000.0),  # a REAL post-head spike in D2 — dropped
    ]
    with capture_logs() as logs:
        assert _mask(rows) == [True, True, True, True, True, False]
    unconfirmed = [e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]
    assert [e["session"] for e in unconfirmed] == ["2024-01-02"]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert [w["session"] for w in warns] == ["2024-01-03"]


def test_two_of_three_corrupt_head_beats_the_median() -> None:
    # DOCUMENTED LIMIT (full blast radius pinned — round-1 review): when 2 of
    # the first 3 bars are corrupt, the median IS the corruption — the healthy
    # head bar is dropped, the corrupt pair SEEDS the post-head baseline, and
    # EVERY healthy successor in the session is cascade-dropped: the session
    # collapses to the corrupt pair (the bug-1 session-destroying shape at a
    # 2-corrupt-head-bars threshold; Stage-1A would absorb the corrupt pair as
    # _today_ohlc and poison the NEXT day's anchors). Majority evidence is the
    # best a bounded in-session screen can do; the skip WARN fires AND — since
    # skipped > kept — the majority-dropped collapse WARN fires too.
    rows = [(_D1, 279400.0), (_D1, 27940.0), (_D1, 279410.0)] + [
        (_D1, 27941.0 + i) for i in range(19)
    ]
    with capture_logs() as logs:
        assert _mask(rows) == [True, False, True] + [False] * 19
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 20  # 1 healthy head bar + all 19 successors
    collapse = [e for e in logs if e.get("event") == BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT]
    assert len(collapse) == 1
    assert collapse[0]["skipped"] == 20
    assert collapse[0]["kept"] == 2  # ONLY the corrupt pair survives
    assert collapse[0]["session"] == "2024-01-02"


def test_two_of_three_differently_scaled_corrupt_head_also_beats_the_median() -> None:
    # DOCUMENTED LIMIT (round-1 review — the "SAME scaling" qualifier was
    # WRONG): two DIFFERENTLY-scaled corrupt head bars (×10 and ×100) also put
    # the median ON a corrupt bar. Here the session collapses to exactly ONE
    # poison bar (the ×10 median; the ×100 bar is itself ≥4x from the median so
    # it drops too) — the precise bug-1 catastrophic shape surviving behind a
    # 2-corrupt-head-bars precondition. Loudly disclosed via BOTH WARNs.
    rows = [(_D1, 279400.0), (_D1, 27940.0), (_D1, 2794000.0)] + [
        (_D1, 27941.0 + i) for i in range(10)
    ]
    with capture_logs() as logs:
        assert _mask(rows) == [True, False, False] + [False] * 10
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 12
    collapse = [e for e in logs if e.get("event") == BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT]
    assert len(collapse) == 1
    assert collapse[0]["kept"] == 1  # the session collapsed to ONE poison bar


def test_corrupt_run_from_head_position_two_is_the_disclosed_regression_envelope() -> None:
    # DOCUMENTED LIMIT (round-1 review): a corrupt run of length >= 2 STARTING
    # at head position 2 is a strict REGRESSION versus the #1616 screen —
    # pre-fix, healthy bar 1 was the baseline and the whole corrupt run was
    # dropped; post-fix, the corrupt pair wins the median vote and the healthy
    # bars (head bar 1 + every successor) are ALL dropped. The vendor-scaling
    # threat model makes a contiguous corrupt run of opening minutes realistic,
    # so this envelope is pinned + disclosed (module docstring, honest limits)
    # rather than silently accepted; the collapse WARN makes it loud.
    rows = [(_D1, 27940.0), (_D1, 279410.0), (_D1, 279420.0)] + [
        (_D1, 27941.0 + i) for i in range(10)
    ]
    with capture_logs() as logs:
        assert _mask(rows) == [False, True, True] + [False] * 10
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 11
    collapse = [e for e in logs if e.get("event") == BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT]
    assert len(collapse) == 1
    assert collapse[0]["kept"] == 2


def test_majority_dropped_warn_not_fired_at_or_below_equal_split() -> None:
    # The collapse WARN is strictly majority (skipped > kept): a 4-bar session
    # with 2 skipped / 2 kept fires the skip WARN only.
    rows = [(_D1, 100.0), (_D1, 101.0), (_D1, 1000.0), (_D1, 10000.0)]
    with capture_logs() as logs:
        assert _mask(rows) == [True, True, False, False]
    assert len([e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]) == 1
    assert not [e for e in logs if e.get("event") == BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT]


def test_head_escape_band_threshold_adjacent_spike_kept_median_reference_pin() -> None:
    # DOCUMENTED PRECISION (round-2 review): a HEAD bar is judged against the
    # head MEDIAN, not its immediate predecessor — so a threshold-adjacent
    # spike that is >= 4x its NEIGHBOR yet < 4x the median rides the escape
    # band [4*min(other head closes), 4*median) and is KEPT, silently (the
    # pre-fix pairwise #1616 screen dropped it: 400.5 >= 4*100). The band is
    # exactly as wide as the head-bar dispersion (~1% on real minute data), so
    # only a threshold-adjacent ~4.00-4.04x spike can escape; the x10
    # threat-model class sits far outside the band and is always caught.
    rows = [(_D1, c) for c in (100.0, 400.5, 101.0, 100.5, 100.8)]
    with capture_logs() as logs:
        assert _mask(rows) == [True] * 5
    assert not logs
    # The pairwise predicate WOULD have flagged it (the pre-fix reference) …
    assert is_bad_tick(100.0, 400.5) is True
    # … but the head median (101) does not: 400.5 < 4 * 101 = 404.
    assert is_bad_tick(101.0, 400.5) is False
    # The x10 class is still always caught (far outside the escape band).
    x10 = [(_D1, c) for c in (100.0, 1000.0, 101.0, 100.5, 100.8)]
    with capture_logs() as logs:
        assert _mask(x10) == [True, False, True, True, True]
    assert len([e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]) == 1


def test_minimal_corrupt_majority_shapes_emit_only_the_skip_warn() -> None:
    # DOCUMENTED PRECISION (round-2 review): on the MINIMAL corrupt-majority
    # shapes the SURVIVORS are the corruption, yet the strict skipped > kept
    # gate cannot fire — a 3-bar session with a 2-of-3 corrupt head skips
    # 1 < keeps 2 (the 4-bar 2v2 split is pinned above at the equal-split
    # boundary) — so only the ordinary skip WARN discloses. Real NSE sessions
    # carry 60-375 bars, so a real corrupt-majority head cascades far past
    # the gate: the companion 6-bar shape below fires BOTH warns.
    rows = [(_D1, c) for c in (100.0, 1000.0, 1000.0)]
    with capture_logs() as logs:
        # Median = 1000 (the corruption): the healthy bar is dropped, the two
        # corrupt survivors are kept — and NO collapse warn can fire.
        assert _mask(rows) == [False, True, True]
    assert len([e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]) == 1
    assert not [e for e in logs if e.get("event") == BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT]
    # The SAME corrupt head with healthy successors cascades past the gate.
    longer = [(_D1, c) for c in (100.0, 1000.0, 1000.0, 100.0, 101.0, 100.0)]
    with capture_logs() as logs:
        assert _mask(longer) == [False, True, True, False, False, False]
    collapse = [e for e in logs if e.get("event") == BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT]
    assert len(collapse) == 1
    assert collapse[0]["skipped"] == 4
    assert collapse[0]["kept"] == 2


def test_healthy_median_short_session_collapse_warn_after_perfect_screen() -> None:
    # DOCUMENTED PRECISION (round-2 review): the collapse WARN is a coarse
    # "most of this session was screened away — inspect the partition"
    # signal, NOT proof the survivors are corrupt — on a short session with a
    # HEALTHY median and two differently-corrupt head bars the screen works
    # PERFECTLY (both corruptions dropped, the healthy bar kept), yet
    # skipped(2) > kept(1) still fires it. Mask untouched; disclosure only.
    rows = [(_D1, 400.0), (_D1, 100.0), (_D1, 20.0)]
    with capture_logs() as logs:
        assert _mask(rows) == [False, True, False]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 2
    assert warns[0]["first_ref_close"] == 100.0
    assert warns[0]["first_close"] == 400.0
    collapse = [e for e in logs if e.get("event") == BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT]
    assert len(collapse) == 1
    assert collapse[0]["skipped"] == 2
    assert collapse[0]["kept"] == 1


def test_head_verdict_is_bounded_forward_looking_truncation_pin() -> None:
    # DOCUMENTED LIMIT (round-1 review): the head vote reads up to 2 FOLLOWING
    # same-session bars, so the SAME leading bar's verdict legitimately differs
    # across loads truncated inside the session's first 3 bars — a 1- or 2-bar
    # truncated load keeps the poison head (unconfirmable — never destroy real
    # data on no/50-50 evidence) that the 3-bar load drops. The pre-#1625
    # screen was strictly prefix-causal; this forward dependence is the
    # recorded price of the evidence-confirmed head seed, disclosed in the
    # module docstring (and the sweep suffix-independence gates deliberately
    # do NOT route through arrow_table_to_bars, so they do not certify this).
    screen = BadTickScreen()
    assert screen.screen_mask([_D1], [279400.0]) == [True]
    with capture_logs() as logs:
        assert screen.screen_mask([_D1] * 2, [279400.0, 27940.0]) == [True, True]
    assert len([e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]) == 1
    assert screen.screen_mask([_D1] * 3, [279400.0, 27940.0, 27941.0]) == [False, True, True]


def test_all_three_head_bars_mutually_inconsistent_median_survives() -> None:
    # Pathological head where every pair is ≥ 4x apart: the median bar always
    # survives (is_bad_tick(m, m) is False) so a session can never be emptied
    # by its own head vote.
    rows = [(_D1, 10.0), (_D1, 100.0), (_D1, 1000.0)]
    with capture_logs() as logs:
        assert _mask(rows) == [False, True, False]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 2


def test_head_median_with_duplicate_closes_tie() -> None:
    # Ties: median of (100, 100, 1000) = 100 → the odd one out is dropped.
    rows = [(_D1, 100.0), (_D1, 100.0), (_D1, 1000.0), (_D1, 101.0)]
    assert _mask(rows) == [True, True, False, True]


def test_exactly_three_bar_session_resolves_head_at_third_bar() -> None:
    rows = [(_D1, 100.0), (_D1, 101.0), (_D1, 1000.0)]
    with capture_logs() as logs:
        assert _mask(rows) == [True, True, False]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1


def test_custom_max_ratio_applies_to_head_and_stream() -> None:
    screen = BadTickScreen(max_ratio=2.0)
    rows = [(_D1, 100.0), (_D1, 250.0), (_D1, 101.0), (_D1, 150.0)]
    # Head median = 101 → 250 is >= 2x → dropped; stream: 150 vs baseline
    # 101 is < 2x → kept.
    assert _mask(rows, screen=screen) == [True, False, True, True]


def test_screen_instance_reusable_across_tables() -> None:
    # Config-only instance: a second screen_mask call starts fresh (no state
    # bleeds across tables).
    screen = BadTickScreen()
    rows = [(_D1, 100.0), (_D1, 101.0), (_D1, 102.0), (_D1, 1000.0)]
    assert _mask(rows, screen=screen) == [True, True, True, False]
    assert _mask(rows, screen=screen) == [True, True, True, False]


def test_injected_logger_is_used_for_all_three_events() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class _FakeLog:
        def warning(self, event: str, **kw: object) -> None:
            events.append((event, kw))

    screen = BadTickScreen(source="src", log=_FakeLog())  # type: ignore[arg-type]
    # Table 1: a post-head ÷10 spike (skip warn only — 1 skipped vs 3 kept).
    screen.screen_mask([_D1] * 4, [100.0, 101.0, 102.0, 10.0])
    # Table 2: an inconsistent 2-bar session (unconfirmed warn).
    screen.screen_mask([_D2] * 2, [100.0, 1000.0])
    # Table 3: a mutually-inconsistent head (2 skipped vs 1 kept) — the skip
    # warn AND the majority-dropped collapse warn.
    screen.screen_mask([_D1] * 3, [10.0, 100.0, 1000.0])
    assert events == [
        (
            BAD_TICK_SKIP_EVENT,
            {
                "source": "src",
                "session": "2024-01-02",
                "skipped": 1,
                "max_ratio": BAD_TICK_MAX_RATIO,
                "first_ref_close": 102.0,
                "first_close": 10.0,
            },
        ),
        (
            BAD_TICK_HEAD_UNCONFIRMED_EVENT,
            {
                "source": "src",
                "session": "2024-01-03",
                "first_close": 100.0,
                "second_close": 1000.0,
                "max_ratio": BAD_TICK_MAX_RATIO,
            },
        ),
        (
            BAD_TICK_SKIP_EVENT,
            {
                "source": "src",
                "session": "2024-01-02",
                "skipped": 2,
                "max_ratio": BAD_TICK_MAX_RATIO,
                "first_ref_close": 100.0,
                "first_close": 10.0,
            },
        ),
        (
            BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT,
            {
                "source": "src",
                "session": "2024-01-02",
                "skipped": 2,
                "kept": 1,
                "max_ratio": BAD_TICK_MAX_RATIO,
            },
        ),
    ]
