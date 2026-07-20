"""Unit tests for the bounded bad-tick screen (``brutex.data.bad_tick``).

100% line + branch coverage of the spike / fat-finger / vendor-scaling guard at
the engine read boundary (DEC-SWEEP-DATA-EDGE-GUARDS-001, re-derived for the
DEC-BADTICK-HEAD-SEED-001 session-head median-confirm law + the
DEC-BADTICK-HILO-SCREEN-001 high/low arm). The end-to-end wiring through
:func:`arrow_table_to_bars` is exercised in
``tests/integration/test_feed_replay_bad_tick_screen.py`` (stem-named
``test_feed_replay*`` so the coverage-gate discovery maps it to
``brutex/feed/replay.py``).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from brutex.data.bad_tick import (
    BAD_TICK_HEAD_UNCONFIRMED_EVENT,
    BAD_TICK_MAX_RATIO,
    BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT,
    BAD_TICK_SKIP_EVENT,
    EVENT_KIND_HEAD_UNCONFIRMED,
    EVENT_KIND_MAJORITY_DROPPED,
    EVENT_KIND_SKIP,
    HEAD_CONFIRM_BARS,
    REASON_CLOSE,
    REASON_HIGH,
    REASON_LOW,
    SEED_REF_ROW,
    SKIP_REASON_TOKENS,
    BadTickScreen,
    bad_tick_screen_enabled,
    emit_bad_tick_warn,
    emit_head_unconfirmed_warn,
    emit_majority_dropped_warn,
    emit_screen_warn,
    is_bad_tick,
    resample_screen_applies,
    screen_frame_bad_ticks,
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
# resample_screen_applies — the ONE COMMON instrument-class gate
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("segment", "symbol"),
    [
        ("INDEX", "NSE-NIFTY"),
        ("CASH", "NSE-RELIANCE"),
        ("FNO", "NSE-NIFTY-30Sep25-FUT"),  # a decoded FUT contract is screened
    ],
)
def test_resample_screen_applies_spot_and_futures(segment: str, symbol: str) -> None:
    assert resample_screen_applies(segment, symbol) is True


@pytest.mark.parametrize(
    "symbol",
    [
        "NSE-NIFTY-30Sep25-24650-CE",  # option premium — legitimately > 4x
        "NSE-NIFTY-30Sep25-24650-PE",
        "NSE-JUNK",  # undecodable FNO symbol — cannot prove futures
        "not-a-contract",
    ],
)
def test_resample_screen_applies_refuses_options_and_undecodable(symbol: str) -> None:
    assert resample_screen_applies("FNO", symbol) is False


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


def test_default_max_ratio_constant() -> None:
    assert BAD_TICK_MAX_RATIO == 4.0


def test_symmetric_in_the_ratio_sense() -> None:
    # The docstring's symmetry law — why a lone 2-bar pair names no offender.
    assert is_bad_tick(100.0, 400.0) == is_bad_tick(400.0, 100.0)
    assert is_bad_tick(100.0, 399.0) == is_bad_tick(399.0, 100.0)


# ────────────────────────────────────────────────────────────────────────
# BadTickScreen.screen_mask — the decision plane (mask + events, no logging)
# ────────────────────────────────────────────────────────────────────────

_D1 = date(2024, 1, 2)
_D2 = date(2024, 1, 3)


def _mask(
    closes: list[float],
    *,
    dates: list[date] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    seed: float | None = None,
    max_ratio: float = BAD_TICK_MAX_RATIO,
) -> tuple[list[bool], list[tuple[int, ...]]]:
    days = dates if dates is not None else [_D1] * len(closes)
    screen = BadTickScreen(max_ratio=max_ratio)
    keep, events = screen.screen_mask(days, closes, highs=highs, lows=lows, seed_prev_close=seed)
    return keep, [tuple(e) for e in events]


class TestScreenMaskHeadLaw:
    """DEC-BADTICK-HEAD-SEED-001: the session-head median-confirm vote."""

    def test_clean_head_all_kept_no_events(self) -> None:
        keep, events = _mask([100.0, 100.5, 101.0, 101.5])
        assert keep == [True, True, True, True]
        assert events == []

    def test_poison_first_bar_dropped_by_median_vote(self) -> None:
        # The #1625 headline law: a ×10 poison FIRST bar loses the median
        # vote (median = 101 @ row 2) instead of seating the baseline and
        # cascading the whole session away. (The RED-first R-twin of the
        # rust scan_tests poison-head case — identical event bytes.)
        keep, events = _mask([1000.0, 100.0, 101.0, 102.0])
        assert keep == [False, True, True, True]
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 2, 0, REASON_CLOSE, 0, 0)]

    def test_escape_band_head_spike_kept_but_never_seats_baseline(self) -> None:
        # The #1811 phase-1 blindness probe [4, 4.05, 15.9, 16.5]: 15.9 is
        # INSIDE the band vs the median 4.05 (15.9 < 16.2) so it is KEPT —
        # but the post-head baseline SEEDS from the median, so 16.5 is
        # judged vs 4.05 (not 15.9) and drops.
        keep, events = _mask([4.0, 4.05, 15.9, 16.5])
        assert keep == [True, True, True, False]
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]

    def test_two_bar_session_mutually_inconsistent_both_kept(self) -> None:
        # No third witness → no offender identified → keep BOTH + disclose.
        keep, events = _mask([100.0, 400.0])
        assert keep == [True, True]
        assert events == [(EVENT_KIND_HEAD_UNCONFIRMED, 2, 0, 0, 1, 0, 0, 0, 0)]

    def test_two_bar_session_consistent_silent(self) -> None:
        keep, events = _mask([100.0, 101.0])
        assert keep == [True, True]
        assert events == []

    def test_one_bar_session_silent(self) -> None:
        keep, events = _mask([100.0])
        assert keep == [True]
        assert events == []

    def test_corrupt_head_majority_wins_documented_tradeoff(self) -> None:
        # HONEST LIMIT (module docstring): 2-of-3 corrupt head bars WIN the
        # median vote — the sane minority is dropped. The trade-off is
        # conscious: a majority-vote screen cannot tell "2 corrupt of 3"
        # from "1 corrupt of 3" without external evidence.
        keep, events = _mask([100.0, 1000.0, 1005.0, 101.0])
        assert keep == [False, True, True, False]
        assert events == [(EVENT_KIND_SKIP, 4, 0, 2, 1, 0, REASON_CLOSE, 0, 0)]

    def test_majority_dropped_event_follows_skip_at_same_anchor(self) -> None:
        keep, events = _mask([100.0, 100.5, 101.0, 1000.0, 1001.0, 1002.0, 1003.0])
        assert keep == [True, True, True, False, False, False, False]
        assert events == [
            (EVENT_KIND_SKIP, 7, 0, 4, 1, 3, REASON_CLOSE, 0, 0),
            (EVENT_KIND_MAJORITY_DROPPED, 7, 0, 4, 3, 0, 0, 0, 0),
        ]

    def test_head_median_tie_breaks_by_row(self) -> None:
        # Duplicate closes: the (value, row) sort picks row 1 as the median
        # (the EXACT scan.rs tie rule) — pinned via the SKIP's first_ref_row.
        keep, events = _mask([100.0, 100.0, 100.0, 1000.0])
        assert keep == [True, True, True, False]
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]

    def test_cross_session_gap_never_trips(self) -> None:
        # A ×10 reopen gap across sessions: head buffers restart EMPTY.
        keep, events = _mask(
            [100.0, 101.0, 1000.0, 1001.0],
            dates=[_D1, _D1, _D2, _D2],
        )
        assert keep == [True, True, True, True]
        assert events == []

    def test_session_flush_emits_at_next_session_first_row(self) -> None:
        keep, events = _mask(
            [100.0, 100.5, 101.0, 1000.0, 50.0],
            dates=[_D1, _D1, _D1, _D1, _D2],
        )
        assert keep == [True, True, True, False, True]
        # The D1 SKIP anchors at row 4 (the first D2 row); D2's single bar
        # is silent.
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]

    def test_head_unconfirmed_pair_rows_pinned_second_session(self) -> None:
        # The head-pair rows are ABSOLUTE row indices of the 2-bar session.
        keep, events = _mask(
            [100.0, 100.5, 101.0, 200.0, 900.0],
            dates=[_D1, _D1, _D1, _D2, _D2],
        )
        assert keep == [True] * 5
        assert events == [(EVENT_KIND_HEAD_UNCONFIRMED, 5, 3, 3, 4, 0, 0, 0, 0)]


class TestScreenMaskPostHeadLaw:
    """The post-head running-baseline band (last KEPT close, median-seeded)."""

    def test_skip_never_advances_baseline(self) -> None:
        # The spike's successor is judged against the last KEPT close (100),
        # never the skipped 1000 — a lone spike cannot cascade-flag its
        # healthy successor.
        keep, events = _mask([100.0, 100.0, 100.0, 1000.0, 102.0])
        assert keep == [True, True, True, False, True]
        assert events == [(EVENT_KIND_SKIP, 5, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]

    def test_kept_bar_advances_baseline(self) -> None:
        # A gradual walk (each step < 4x) keeps everything even when the
        # session's total drift exceeds 4x from the head.
        keep, events = _mask([100.0, 100.0, 100.0, 300.0, 900.0, 2700.0])
        assert keep == [True] * 6
        assert events == []

    def test_exact_up_boundary_dropped(self) -> None:
        keep, _ = _mask([100.0, 100.0, 100.0, 400.0])
        assert keep == [True, True, True, False]

    def test_exact_down_boundary_dropped(self) -> None:
        keep, _ = _mask([400.0, 400.0, 400.0, 100.0])
        assert keep == [True, True, True, False]

    def test_just_inside_boundary_kept(self) -> None:
        keep_up, events_up = _mask([100.0, 100.0, 100.0, 399.99])
        keep_dn, events_dn = _mask([400.0, 400.0, 400.0, 100.01])
        assert keep_up == [True] * 4
        assert keep_dn == [True] * 4
        assert events_up == events_dn == []

    def test_custom_max_ratio_post_head(self) -> None:
        keep_default, _ = _mask([100.0, 100.0, 100.0, 250.0])
        keep_tight, _ = _mask([100.0, 100.0, 100.0, 250.0], max_ratio=2.0)
        assert keep_default == [True] * 4
        assert keep_tight == [True, True, True, False]

    def test_two_consecutive_spikes_one_skip_event(self) -> None:
        keep, events = _mask([100.0, 100.5, 101.0, 1000.0, 1005.0, 101.5])
        assert keep == [True, True, True, False, False, True]
        # first_ref_row = 1: the median-seeded baseline (100.5 @ row 1) is
        # still the reference at the first skip (no post-head keep yet).
        assert events == [(EVENT_KIND_SKIP, 6, 0, 2, 1, 3, REASON_CLOSE, 0, 0)]


class TestScreenMaskHiLoArm:
    """DEC-BADTICK-HILO-SCREEN-001: highs/lows judged, never the baseline."""

    @staticmethod
    def _tight(closes: list[float]) -> tuple[list[float], list[float]]:
        return [c + 0.01 for c in closes], [c - 0.01 for c in closes]

    def test_high_out_of_band_drops_bar_reason_high(self) -> None:
        closes = [100.0, 100.0, 100.0, 101.0]
        highs, lows = self._tight(closes)
        highs[3] = 1020.0  # a ×10 HIGH with a sane close
        keep, events = _mask(closes, highs=highs, lows=lows)
        assert keep == [True, True, True, False]
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_HIGH, 1, 0)]

    def test_low_out_of_band_drops_bar_reason_low(self) -> None:
        closes = [100.0, 100.0, 100.0, 101.0]
        highs, lows = self._tight(closes)
        lows[3] = 10.0  # 10 * 4 <= 100 → low out-of-band
        keep, events = _mask(closes, highs=highs, lows=lows)
        assert keep == [True, True, True, False]
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_LOW, 0, 1)]

    def test_close_precedence_over_high_and_low(self) -> None:
        # A bar failing ALL THREE fields counts ONCE as `close` (the pinned
        # precedence order close → high → low).
        closes = [100.0, 100.0, 100.0, 1000.0]
        highs, lows = self._tight(closes)
        highs[3] = 5000.0
        lows[3] = 1.0
        keep, events = _mask(closes, highs=highs, lows=lows)
        assert keep == [True, True, True, False]
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]

    def test_high_precedence_over_low(self) -> None:
        closes = [100.0, 100.0, 100.0, 101.0]
        highs, lows = self._tight(closes)
        highs[3] = 1020.0
        lows[3] = 1.0
        keep, events = _mask(closes, highs=highs, lows=lows)
        assert keep == [True, True, True, False]
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_HIGH, 1, 0)]

    def test_hilo_omitted_is_close_only(self) -> None:
        # The same bar with the poison HIGH sails through a close-only call.
        keep, events = _mask([100.0, 100.0, 100.0, 101.0])
        assert keep == [True] * 4
        assert events == []

    def test_head_bar_high_judged_vs_median_close(self) -> None:
        # The median bar ITSELF drops for its own poison high — yet its
        # CLOSE still seeds the baseline (a close-derived quantity, never a
        # kept-bar identity).
        closes = [100.0, 100.5, 101.0, 100.6]
        highs, lows = self._tight(closes)
        highs[1] = 5000.0  # the median bar (row 1) carries the poison high
        keep, events = _mask(closes, highs=highs, lows=lows)
        assert keep == [True, False, True, True]
        # ref_row == skip_row == 1: the median bar judged against itself.
        assert events == [(EVENT_KIND_SKIP, 4, 0, 1, 1, 1, REASON_HIGH, 1, 0)]

    def test_exactly_three_bar_session_fully_emptied_by_hilo(self) -> None:
        # HONEST LIMIT: with every head bar carrying an out-of-band high,
        # the hi/lo arm can empty an exactly-3-bar session (the close vote
        # alone never can — the median always survives it).
        closes = [100.0, 100.0, 100.0]
        highs = [1000.0, 1000.0, 1000.0]
        lows = [99.9, 99.9, 99.9]
        keep, events = _mask(closes, highs=highs, lows=lows)
        assert keep == [False, False, False]
        assert events == [
            (EVENT_KIND_SKIP, 3, 0, 3, 1, 0, REASON_HIGH, 3, 0),
            (EVENT_KIND_MAJORITY_DROPPED, 3, 0, 3, 0, 0, 0, 0, 0),
        ]

    def test_two_bar_session_hilo_not_extended(self) -> None:
        # The unconfirmable 2-bar shape keeps its highs/lows UNSCREENED
        # (disclosed limit — no confirmed reference exists).
        closes = [100.0, 101.0]
        highs = [100.5, 2000.0]
        lows = [99.5, 100.5]
        keep, events = _mask(closes, highs=highs, lows=lows)
        assert keep == [True, True]
        assert events == []

    def test_mixed_reason_tally(self) -> None:
        # close + high + low skips in ONE session: the counts land in the
        # (skipped, n_high, n_low) fields; first_reason names the FIRST.
        # 3 skipped vs 3 kept is NOT a strict majority — no MAJORITY event.
        closes = [100.0, 100.0, 100.0, 1000.0, 101.0, 102.0]
        highs, lows = self._tight(closes)
        highs[4] = 1020.0
        lows[5] = 10.0
        keep, events = _mask(closes, highs=highs, lows=lows)
        assert keep == [True, True, True, False, False, False]
        assert events == [(EVENT_KIND_SKIP, 6, 0, 3, 1, 3, REASON_CLOSE, 1, 1)]


class TestScreenMaskNonFinite:
    """Non-finite robustness: the #1897 raise law composes UPSTREAM of the
    screen at raise-gated seams; the screen itself must stay total + sane."""

    def test_nonfinite_close_kept_unjudged_and_excluded_from_head(self) -> None:
        keep, events = _mask([math.nan, 100.0, 100.5, 101.0, 1000.0])
        # Row 0 kept UNJUDGED; the head is rows 1-3; row 4 drops vs 100.5.
        assert keep == [True, True, True, True, False]
        assert events == [(EVENT_KIND_SKIP, 5, 0, 1, 2, 4, REASON_CLOSE, 0, 0)]

    def test_nonfinite_close_mid_session_excluded_from_reference(self) -> None:
        keep, events = _mask([100.0, 100.0, 100.0, math.inf, 102.0])
        # The inf close is kept-unjudged; 102 is judged vs the REAL baseline
        # (100), not the inf — a NaN/inf reference would blind the screen.
        assert keep == [True] * 5
        assert events == []

    def test_nonfinite_rows_outside_session_tallies(self) -> None:
        # MAJORITY counts FINITE-close rows only: 3 finite (2 skipped, 1
        # kept) + 2 non-finite kept-unjudged → skipped(2) > kept(1) fires.
        keep, events = _mask([100.0, 100.0, 100.0, math.nan, 1000.0, 1001.0, math.inf][:7])
        assert keep == [True, True, True, True, False, False, True]
        assert events == [
            (EVENT_KIND_SKIP, 7, 0, 2, 1, 4, REASON_CLOSE, 0, 0),
        ]

    @pytest.mark.parametrize(
        ("high", "kept"),
        [
            (math.nan, True),  # NaN compares False against every bound
            (math.inf, False),  # inf >= reference * 4 → out-of-band
            (-math.inf, False),  # -inf * 4 <= reference → out-of-band
        ],
    )
    def test_nonfinite_high_judged_native_ieee(self, high: float, kept: bool) -> None:
        # The DOCUMENTED asymmetry (module docstring): non-finite CLOSES are
        # kept-unjudged (a poisoned REFERENCE blinds the screen), while
        # non-finite highs/lows are judged natively by the IEEE comparisons
        # (they are never references). Unreachable at every raise-gated seam.
        closes = [100.0, 100.0, 100.0, 101.0]
        highs = [100.5, 100.5, 100.5, high]
        lows = [99.5, 99.5, 99.5, 100.5]
        keep, _ = _mask(closes, highs=highs, lows=lows)
        assert keep == [True, True, True, kept]


class TestScreenMaskSeed:
    """The /db page-seed law: the FIRST session's head resolved upstream."""

    def test_seed_replaces_first_session_head_vote(self) -> None:
        keep, events = _mask([1000.0, 101.0], seed=100.0)
        assert keep == [False, True]
        assert events == [(EVENT_KIND_SKIP, 2, 0, 1, SEED_REF_ROW, 0, REASON_CLOSE, 0, 0)]

    def test_seed_first_session_only(self) -> None:
        keep, events = _mask(
            [101.0, 1000.0, 100.5, 101.5],
            dates=[_D1, _D2, _D2, _D2],
            seed=100.0,
        )
        # D1 judged vs the seed; D2 head-votes NORMALLY (median 101.5 @ row
        # 3 of (1000, 100.5, 101.5) → sorted [(100.5,2),(101.5,3),(1000,1)]).
        assert keep == [True, False, True, True]
        assert events == [(EVENT_KIND_SKIP, 4, 1, 1, 3, 1, REASON_CLOSE, 0, 0)]

    def test_in_band_seed_keeps_everything(self) -> None:
        keep, events = _mask([101.0, 102.0], seed=100.0)
        assert keep == [True, True]
        assert events == []


class TestScreenMaskErrors:
    def test_ist_dates_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="ist_dates/closes length mismatch"):
            BadTickScreen().screen_mask([_D1], [100.0, 101.0])

    def test_highs_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="highs/closes length mismatch"):
            BadTickScreen().screen_mask([_D1], [100.0], highs=[100.0, 101.0])

    def test_lows_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="lows/closes length mismatch"):
            BadTickScreen().screen_mask([_D1], [100.0], lows=[])

    def test_empty_input(self) -> None:
        keep, events = BadTickScreen().screen_mask([], [])
        assert keep == []
        assert events == []

    def test_screen_never_logs(self) -> None:
        # The decision plane is silent — emission is the caller's drain.
        with capture_logs() as logs:
            _mask([100.0, 100.0, 100.0, 1000.0])
        assert logs == []

    def test_one_instance_screens_multiple_tables(self) -> None:
        # The instance carries only configuration — no per-session state
        # leaks across calls.
        screen = BadTickScreen()
        first = screen.screen_mask([_D1] * 4, [100.0, 100.0, 100.0, 1000.0])
        second = screen.screen_mask([_D1] * 4, [100.0, 100.0, 100.0, 1000.0])
        assert first == second


# ────────────────────────────────────────────────────────────────────────
# The emission seams (DEC-FEED-RUST-CORE-001 — ONE seam per event kind)
# ────────────────────────────────────────────────────────────────────────


class _FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))


def test_emit_bad_tick_warn_exact_kwargs_injected_logger() -> None:
    log = _FakeLog()
    emit_bad_tick_warn(
        source="NSE/INDEX/NSE-NIFTY/1minute",
        session_iso="2024-01-02",
        skipped=2,
        max_ratio=BAD_TICK_MAX_RATIO,
        first_ref_close=27945.0,
        first_close=2794.5,
        skip_reasons={"close": 2},
        first_reason="close",
        log=log,  # type: ignore[arg-type]
    )
    assert log.events == [
        (
            BAD_TICK_SKIP_EVENT,
            {
                "source": "NSE/INDEX/NSE-NIFTY/1minute",
                "session": "2024-01-02",
                "skipped": 2,
                "max_ratio": BAD_TICK_MAX_RATIO,
                "first_ref_close": 27945.0,
                "first_close": 2794.5,
                "skip_reasons": {"close": 2},
                "first_reason": "close",
            },
        )
    ]


def test_emit_bad_tick_warn_default_logger_record_shape() -> None:
    with capture_logs() as logs:
        emit_bad_tick_warn(
            source=None,
            session_iso=None,
            skipped=1,
            max_ratio=2.0,
            first_ref_close=100.0,
            first_close=10.0,
            skip_reasons={"low": 1},
            first_reason="low",
        )
    assert logs == [
        {
            "event": BAD_TICK_SKIP_EVENT,
            "log_level": "warning",
            "source": None,
            "session": None,
            "skipped": 1,
            "max_ratio": 2.0,
            "first_ref_close": 100.0,
            "first_close": 10.0,
            "skip_reasons": {"low": 1},
            "first_reason": "low",
        }
    ]


def test_emit_head_unconfirmed_warn_record_shape() -> None:
    with capture_logs() as logs:
        emit_head_unconfirmed_warn(
            source="src",
            session_iso="2024-01-02",
            first_close=100.0,
            second_close=400.0,
            max_ratio=BAD_TICK_MAX_RATIO,
        )
    assert logs == [
        {
            "event": BAD_TICK_HEAD_UNCONFIRMED_EVENT,
            "log_level": "warning",
            "source": "src",
            "session": "2024-01-02",
            "first_close": 100.0,
            "second_close": 400.0,
            "max_ratio": BAD_TICK_MAX_RATIO,
        }
    ]


def test_emit_majority_dropped_warn_record_shape() -> None:
    log = _FakeLog()
    emit_majority_dropped_warn(
        source=None,
        session_iso="2024-01-02",
        skipped=3,
        kept=1,
        max_ratio=BAD_TICK_MAX_RATIO,
        log=log,  # type: ignore[arg-type]
    )
    assert log.events == [
        (
            BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT,
            {
                "source": None,
                "session": "2024-01-02",
                "skipped": 3,
                "kept": 1,
                "max_ratio": BAD_TICK_MAX_RATIO,
            },
        )
    ]


class TestEmitScreenWarn:
    """The per-kind dispatcher — integer coordinates → the caller's floats."""

    def test_skip_resolves_rows_and_reason_tallies(self) -> None:
        log = _FakeLog()
        closes = [100.0, 101.0, 102.0, 103.0]
        emit_screen_warn(
            (EVENT_KIND_SKIP, 4, 0, 2, 1, 3, REASON_HIGH, 1, 1),
            source="s",
            session_iso="2024-01-02",
            closes=closes,
            max_ratio=4.0,
            log=log,  # type: ignore[arg-type]
        )
        # n_close = skipped - n_high - n_low = 0 → the `close` key is OMITTED
        # (only non-zero reasons render, in the pinned precedence order).
        assert log.events == [
            (
                BAD_TICK_SKIP_EVENT,
                {
                    "source": "s",
                    "session": "2024-01-02",
                    "skipped": 2,
                    "max_ratio": 4.0,
                    "first_ref_close": 101.0,
                    "first_close": 103.0,
                    "skip_reasons": {"high": 1, "low": 1},
                    "first_reason": "high",
                },
            )
        ]

    def test_skip_all_three_reasons_render_in_precedence_order(self) -> None:
        log = _FakeLog()
        emit_screen_warn(
            (EVENT_KIND_SKIP, 6, 0, 4, 0, 1, REASON_CLOSE, 1, 1),
            source=None,
            session_iso=None,
            closes=[100.0, 1000.0],
            max_ratio=4.0,
            log=log,  # type: ignore[arg-type]
        )
        (_, kw) = log.events[0]
        assert kw["skip_reasons"] == {"close": 2, "high": 1, "low": 1}
        assert list(kw["skip_reasons"]) == ["close", "high", "low"]  # type: ignore[call-overload]
        assert kw["first_reason"] == SKIP_REASON_TOKENS[REASON_CLOSE]

    def test_skip_seed_ref_row_resolves_seed(self) -> None:
        log = _FakeLog()
        emit_screen_warn(
            (EVENT_KIND_SKIP, 1, 0, 1, SEED_REF_ROW, 0, REASON_CLOSE, 0, 0),
            source=None,
            session_iso="2024-01-02",
            closes=[1000.0],
            max_ratio=4.0,
            seed_prev_close=99.5,
            log=log,  # type: ignore[arg-type]
        )
        (_, kw) = log.events[0]
        assert kw["first_ref_close"] == 99.5
        assert kw["first_close"] == 1000.0

    def test_skip_seed_ref_row_without_seed_raises(self) -> None:
        with pytest.raises(ValueError, match="SEED_REF_ROW"):
            emit_screen_warn(
                (EVENT_KIND_SKIP, 1, 0, 1, SEED_REF_ROW, 0, REASON_CLOSE, 0, 0),
                source=None,
                session_iso=None,
                closes=[1000.0],
                max_ratio=4.0,
            )

    def test_head_unconfirmed_dispatch(self) -> None:
        log = _FakeLog()
        emit_screen_warn(
            (EVENT_KIND_HEAD_UNCONFIRMED, 2, 0, 0, 1, 0, 0, 0, 0),
            source="s",
            session_iso="2024-01-02",
            closes=[100.0, 400.0],
            max_ratio=4.0,
            log=log,  # type: ignore[arg-type]
        )
        assert log.events == [
            (
                BAD_TICK_HEAD_UNCONFIRMED_EVENT,
                {
                    "source": "s",
                    "session": "2024-01-02",
                    "first_close": 100.0,
                    "second_close": 400.0,
                    "max_ratio": 4.0,
                },
            )
        ]

    def test_majority_dropped_dispatch(self) -> None:
        log = _FakeLog()
        emit_screen_warn(
            (EVENT_KIND_MAJORITY_DROPPED, 3, 0, 3, 0, 0, 0, 0, 0),
            source=None,
            session_iso="2024-01-02",
            closes=[100.0, 100.0, 100.0],
            max_ratio=4.0,
            log=log,  # type: ignore[arg-type]
        )
        assert log.events == [
            (
                BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT,
                {
                    "source": None,
                    "session": "2024-01-02",
                    "skipped": 3,
                    "kept": 0,
                    "max_ratio": 4.0,
                },
            )
        ]


def test_emit_events_drains_all_with_session_resolution() -> None:
    # screen_mask + emit_events == the byte-equivalent direct emits (the
    # delegation-equivalence law both arms of the replay seam rely on).
    dates = [_D1, _D1, _D1, _D1, _D2, _D2]
    closes = [100.0, 100.5, 101.0, 1000.0, 200.0, 2000.0]
    screen = BadTickScreen(source="src")
    keep, events = screen.screen_mask(dates, closes)
    assert keep == [True, True, True, False, True, True]
    with capture_logs() as via_events:
        screen.emit_events(events, session_dates=dates, closes=closes)
    with capture_logs() as direct:
        emit_bad_tick_warn(
            source="src",
            session_iso="2024-01-02",
            skipped=1,
            max_ratio=BAD_TICK_MAX_RATIO,
            first_ref_close=100.5,
            first_close=1000.0,
            skip_reasons={"close": 1},
            first_reason="close",
        )
        emit_head_unconfirmed_warn(
            source="src",
            session_iso="2024-01-03",
            first_close=200.0,
            second_close=2000.0,
            max_ratio=BAD_TICK_MAX_RATIO,
        )
    assert via_events == direct


def test_emit_events_uses_injected_logger_and_config() -> None:
    log = _FakeLog()
    dates = [_D1, _D1, _D1, _D1]
    closes = [100.0, 100.0, 100.0, 260.0]
    screen = BadTickScreen(source="cfg", max_ratio=2.5, log=log)  # type: ignore[arg-type]
    keep, events = screen.screen_mask(dates, closes)
    assert keep == [True, True, True, False]
    screen.emit_events(events, session_dates=dates, closes=closes)
    assert log.events == [
        (
            BAD_TICK_SKIP_EVENT,
            {
                "source": "cfg",
                "session": "2024-01-02",
                "skipped": 1,
                "max_ratio": 2.5,
                "first_ref_close": 100.0,
                "first_close": 260.0,
                "skip_reasons": {"close": 1},
                "first_reason": "close",
            },
        )
    ]


# ────────────────────────────────────────────────────────────────────────
# screen_frame_bad_ticks — the frame form (DEC-READ-GUARD-PARITY-001)
# ────────────────────────────────────────────────────────────────────────


class TestScreenFrameBadTicks:
    """Frame twin of :class:`BadTickScreen` at the resample-input seams.

    The sequential :meth:`BadTickScreen.screen_mask` arm is the single source
    of truth for WHICH rows drop + the WARN semantics; the vectorized
    candidate trigger only decides whether the sequential arm runs at all — a
    clean frame must come back as the INPUT OBJECT (zero-copy). Requires
    polars (skipped wheel-free — the scalar tests above stay
    dependency-free).
    """

    @staticmethod
    def _frame(
        rows: list[tuple[datetime, float]],
        *,
        oi: bool = False,
        naive: bool = False,
        tight: bool = False,
        highs: list[float] | None = None,
        lows: list[float] | None = None,
    ) -> Any:
        """Build a sorted ts_utc/OHLCV(+open_interest) frame from (ts, close) rows.

        ``tight=True`` pins high/low to close±0.01 (so ONLY the close arm can
        judge); the default keeps the historical close±1.0 shape; explicit
        *highs*/*lows* override either.
        """
        pl = pytest.importorskip("polars")
        stamps = [ts.replace(tzinfo=None) if naive else ts for ts, _ in rows]
        closes = [close for _, close in rows]
        pad = 0.01 if tight else 1.0
        data: dict[str, object] = {
            "ts_utc": stamps,
            "open": closes,
            "high": highs if highs is not None else [close + pad for close in closes],
            "low": lows if lows is not None else [close - pad for close in closes],
            "close": closes,
            "volume": list(range(1, len(rows) + 1)),
        }
        if oi:
            data["open_interest"] = [10 * i for i in range(len(rows))]
        schema = {
            "ts_utc": pl.Datetime("us") if naive else pl.Datetime("us", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            **({"open_interest": pl.Int64} if oi else {}),
        }
        return pl.DataFrame(data, schema=schema)  # type: ignore[arg-type]

    @staticmethod
    def _ts(minute: int, *, day: int = 0) -> datetime:
        """09:15 IST (03:45 UTC) + *minute* on session day 2024-01-02 + *day*."""
        return datetime(2024, 1, 2, 3, 45, tzinfo=UTC) + timedelta(days=day, minutes=minute)

    def test_spike_row_dropped_mid_session(self) -> None:
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(2), 1000.0),
                (self._ts(3), 102.0),
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 101.0, 102.0]

    def test_head_vote_drops_out_of_band_third_bar(self) -> None:
        # DEC-BADTICK-HEAD-SEED-001: the head votes vs its median (999990) —
        # the far-out 240 loses the vote even though it IS a head bar.
        frame = self._frame(
            [(self._ts(0), 999999.0), (self._ts(1), 999990.0), (self._ts(2), 240.0)]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [999999.0, 999990.0]

    def test_poison_head_bar_no_longer_collapses_its_session(self) -> None:
        # The RED-first R1 twin at the frame seam: a ×10 poison FIRST bar is
        # dropped by the median vote instead of seating the baseline (the
        # pre-port law kept it and cascaded the rest of the session away).
        frame = self._frame(
            [
                (self._ts(0), 279400.0),
                (self._ts(1), 27940.0),
                (self._ts(2), 27941.0),
                (self._ts(3), 27942.0),
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [27940.0, 27941.0, 27942.0]

    def test_escape_band_seated_baseline_probe_is_caught(self) -> None:
        # The RED-first R4 probe [4, 4.05, 15.9, 16.5]: the escape-band
        # spike (15.9 < 4x the median 4.05... strictly, 15.9 < 16.2) is KEPT
        # but never seats the baseline — 16.5 drops vs 4.05, and the frame
        # TRIGGER catches it (no adjacent violation exists: the head-median
        # arm is load-bearing — a silent identity return was the pre-port
        # phase-1 blindness).
        frame = self._frame(
            [
                (self._ts(0), 4.0),
                (self._ts(1), 4.05),
                (self._ts(2), 15.9),
                (self._ts(3), 16.5),
            ],
            tight=True,
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [4.0, 4.05, 15.9]

    def test_high_only_poison_dropped_matches_engine_read(self) -> None:
        # The RED-first R3 twin: a ×10 HIGH with a sane close drops the
        # whole bar at the frame seam — the SAME row the engine read drops
        # (the parity that stops a poisoned high laundering into derived
        # buckets); the engine-vs-frame equality itself is pinned in the
        # replay integration suite.
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(2), 102.0),
                (self._ts(3), 103.0),
            ],
            highs=[100.5, 101.5, 1020.0, 103.5],
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 101.0, 103.0]
        with capture_logs() as logs:
            screen_frame_bad_ticks(frame)
        warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
        assert len(warns) == 1
        assert warns[0]["skip_reasons"] == {"high": 1}
        assert warns[0]["first_reason"] == "high"

    def test_reopen_gap_across_sessions_never_trips(self) -> None:
        # Day-1 closes at ~100; day-2 reopens ×10 away — the head buffer
        # restarts per session, so the whole frame is clean → identity.
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(0, day=1), 1000.0),
                (self._ts(1, day=1), 1001.0),
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result is frame

    def test_session_boundary_resets_baseline_mid_frame(self) -> None:
        # Three sessions in ONE frame: a bad ratio ACROSS each boundary never
        # drops (session reset), while a bad ratio WITHIN session 2 drops.
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(0, day=1), 500.0),  # ratio 4.95 vs day-1 close: KEPT (new session)
                (self._ts(1, day=1), 505.0),
                (self._ts(2, day=1), 5050.0),  # ×10 within session 2: DROPPED (head vote)
                (self._ts(3, day=1), 506.0),
                (self._ts(0, day=2), 2100.0),  # ratio 4.15 vs day-2 close: KEPT (new session)
                (self._ts(1, day=2), 2110.0),
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [
            100.0,
            101.0,
            500.0,
            505.0,
            506.0,
            2100.0,
            2110.0,
        ]

    def test_skipped_spike_never_advances_baseline(self) -> None:
        # The M1 mutant killer: the spike's successor is judged against the
        # kept reference (the head median 102 here), never the skipped 1000.
        frame = self._frame([(self._ts(0), 100.0), (self._ts(1), 1000.0), (self._ts(2), 102.0)])
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 102.0]

    def test_two_consecutive_spikes_both_dropped_one_warn(self) -> None:
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 100.5),
                (self._ts(2), 101.0),
                (self._ts(3), 1000.0),
                (self._ts(4), 1005.0),
                (self._ts(5), 101.5),
            ]
        )
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 100.5, 101.0, 101.5]
        warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
        assert len(warns) == 1
        assert warns[0]["skipped"] == 2
        # The reference is the median-seeded baseline (100.5 @ row 1).
        assert warns[0]["first_ref_close"] == 100.5
        assert warns[0]["first_close"] == 1000.0

    def test_two_bar_session_head_unconfirmed_kept_identity_with_warn(self) -> None:
        # A mutually-inconsistent 2-bar session: BOTH bars kept (identity
        # object — nothing dropped) with the honest disclosure WARN.
        frame = self._frame([(self._ts(0), 100.0), (self._ts(1), 1000.0)])
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame)
        assert result is frame
        heads = [e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]
        assert len(heads) == 1
        assert heads[0]["first_close"] == 100.0
        assert heads[0]["second_close"] == 1000.0
        assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]

    def test_majority_dropped_disclosure(self) -> None:
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 100.5),
                (self._ts(2), 101.0),
                (self._ts(3), 1000.0),
                (self._ts(4), 1001.0),
                (self._ts(5), 1002.0),
                (self._ts(6), 1003.0),
            ]
        )
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 100.5, 101.0]
        majority = [e for e in logs if e.get("event") == BAD_TICK_SESSION_MAJORITY_DROPPED_EVENT]
        assert len(majority) == 1
        assert majority[0]["skipped"] == 4
        assert majority[0]["kept"] == 3

    def test_exact_ratio_boundary_dropped_both_directions(self) -> None:
        # `>=` / `<=` — exactly max_ratio-x counts as bad, in BOTH directions
        # (post-head fixtures — a 2-bar boundary pair is the unconfirmable
        # head shape and keeps both by design).
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 100.0),
                (self._ts(2), 100.0),
                (self._ts(3), 400.0),  # up: 400 >= 100 * 4 → dropped
                (self._ts(0, day=1), 400.0),
                (self._ts(1, day=1), 400.0),
                (self._ts(2, day=1), 400.0),
                (self._ts(3, day=1), 100.0),  # down: 100 * 4 <= 400 → dropped
            ]
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [
            100.0,
            100.0,
            100.0,
            400.0,
            400.0,
            400.0,
        ]

    def test_exact_up_boundary_sole_candidate_dropped(self) -> None:
        # The UP boundary as the frame's ONLY violation: 400.0 == 100.0 * 4.0
        # exactly in floats. Kills the single-direction trigger mutant
        # `>=` -> `>` ALONE (with no other candidate in the frame, a drifted
        # up-trigger finds nothing and would return the frame unscreened —
        # the two-boundary test cannot catch it because its other session's
        # intact direction still routes the frame to the sequential arm).
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 100.0),
                (self._ts(2), 100.0),
                (self._ts(3), 400.0),
            ],
            tight=True,
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [100.0, 100.0, 100.0]

    def test_exact_down_boundary_sole_candidate_dropped(self) -> None:
        # The DOWN boundary as the frame's ONLY violation: 100.0 * 4.0 ==
        # 400.0 exactly. Kills the `<=` -> `<` single-direction trigger
        # mutant, symmetrically to the up-boundary killer above.
        frame = self._frame(
            [
                (self._ts(0), 400.0),
                (self._ts(1), 400.0),
                (self._ts(2), 400.0),
                (self._ts(3), 100.0),
            ],
            tight=True,
        )
        result = screen_frame_bad_ticks(frame)
        assert result.get_column("close").to_list() == [400.0, 400.0, 400.0]

    def test_just_inside_boundary_kept(self) -> None:
        # Closes 399.9 / 100.1 keep the ±0.01 tight highs/lows strictly
        # INSIDE the band too (399.91 < 400; 100.09 * 4 > 400) — the hi/lo
        # arm judges the extremes, so a close exactly 0.01 inside the bound
        # would still drop on its own high/low.
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 100.0),
                (self._ts(2), 100.0),
                (self._ts(3), 399.9),
                (self._ts(0, day=1), 400.0),
                (self._ts(1, day=1), 400.0),
                (self._ts(2, day=1), 400.0),
                (self._ts(3, day=1), 100.1),
            ],
            tight=True,
        )
        result = screen_frame_bad_ticks(frame)
        assert result is frame  # nothing dropped → the input object, zero-copy

    def test_one_warn_per_session_with_source_fields(self) -> None:
        # Post-head spikes in TWO sessions → two WARNs; the SECOND sits in
        # the FINAL session, so it only flushes at end-of-input (the M6
        # mutant killer).
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 100.0),
                (self._ts(2), 100.0),
                (self._ts(3), 1000.0),
                (self._ts(0, day=1), 200.0),
                (self._ts(1, day=1), 200.0),
                (self._ts(2, day=1), 200.0),
                (self._ts(3, day=1), 2000.0),
            ]
        )
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame, source="prebuild:NSE/INDEX/NSE-NIFTY/1minute")
        assert result.get_column("close").to_list() == [
            100.0,
            100.0,
            100.0,
            200.0,
            200.0,
            200.0,
        ]
        warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
        assert [w["session"] for w in warns] == ["2024-01-02", "2024-01-03"]
        for warn, (ref_close, close) in zip(warns, [(100.0, 1000.0), (200.0, 2000.0)], strict=True):
            assert warn["source"] == "prebuild:NSE/INDEX/NSE-NIFTY/1minute"
            assert warn["skipped"] == 1
            assert warn["first_ref_close"] == ref_close
            assert warn["first_close"] == close
            assert warn["first_reason"] == "close"

    def test_clean_frame_returns_input_object_identity(self) -> None:
        frame = self._frame([(self._ts(0), 100.0), (self._ts(1), 100.5), (self._ts(2), 101.0)])
        assert screen_frame_bad_ticks(frame) is frame

    def test_clean_frame_no_warn(self) -> None:
        frame = self._frame([(self._ts(0), 100.0), (self._ts(1), 100.5)])
        with capture_logs() as logs:
            screen_frame_bad_ticks(frame)
        assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]

    def test_empty_frame_returned_unchanged(self) -> None:
        frame = self._frame([])
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame)
        assert result is frame
        assert result.height == 0
        assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]

    def test_single_row_frame_kept(self) -> None:
        frame = self._frame([(self._ts(0), 100.0)])
        assert screen_frame_bad_ticks(frame) is frame

    def test_nonfinite_close_routes_sequential_but_keeps_identity(self) -> None:
        # ARM 3: a NaN close routes the frame to the sequential arm (the
        # vectorized arms are finite-only sound) — which keeps the row
        # UNJUDGED; with nothing dropped the INPUT OBJECT comes back.
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), math.nan),
                (self._ts(2), 100.5),
                (self._ts(3), 101.0),
            ],
            tight=True,
        )
        with capture_logs() as logs:
            result = screen_frame_bad_ticks(frame)
        assert result is frame
        assert logs == []

    def test_month_straddling_sessions_reset_on_ist_date(self) -> None:
        # Jan-31 + Feb-01 sessions concatenated by the whole-span glob: the ×10
        # reopen gap across the month boundary never trips (session reset on
        # the IST calendar date, not on frame position).
        jan = datetime(2024, 1, 31, 3, 45, tzinfo=UTC)
        feb = datetime(2024, 2, 1, 3, 45, tzinfo=UTC)
        frame = self._frame(
            [
                (jan, 100.0),
                (jan + timedelta(minutes=1), 101.0),
                (feb, 1010.0),
                (feb + timedelta(minutes=1), 1011.0),
            ]
        )
        assert screen_frame_bad_ticks(frame) is frame

    def test_open_interest_column_preserved_and_row_aligned(self) -> None:
        pl = pytest.importorskip("polars")
        frame = self._frame(
            [
                (self._ts(0), 100.0),
                (self._ts(1), 101.0),
                (self._ts(2), 1000.0),
                (self._ts(3), 102.0),
            ],
            oi=True,
        )
        result = screen_frame_bad_ticks(frame)
        assert result.columns == frame.columns
        # The dropped spike removes ITS OWN row wholesale — OI included,
        # row-aligned with every other column (the hand-filtered oracle).
        expected = frame.filter(pl.Series("keep", [True, True, False, True]))
        assert result.equals(expected)
        assert result.get_column("open_interest").to_list() == [0, 10, 30]

    def test_naive_ts_treated_as_utc(self) -> None:
        rows = [(self._ts(0), 100.0), (self._ts(1), 1000.0), (self._ts(2), 102.0)]
        aware = screen_frame_bad_ticks(self._frame(rows))
        naive = screen_frame_bad_ticks(self._frame(rows, naive=True))
        assert naive.get_column("close").to_list() == aware.get_column("close").to_list()
        assert naive.get_column("close").to_list() == [100.0, 102.0]

    def test_custom_max_ratio(self) -> None:
        rows = [
            (self._ts(0), 100.0),
            (self._ts(1), 100.0),
            (self._ts(2), 100.0),
            (self._ts(3), 250.0),
        ]
        # The default 4.0 keeps a 2.5x move (identity return) …
        default_frame = self._frame(rows, tight=True)
        assert screen_frame_bad_ticks(default_frame) is default_frame
        # … while an explicit max_ratio=2.0 drops it (threaded to BOTH the
        # trigger and the sequential arm).
        tightened = screen_frame_bad_ticks(self._frame(rows, tight=True), max_ratio=2.0)
        assert tightened.get_column("close").to_list() == [100.0, 100.0, 100.0]

    def test_custom_ts_column(self) -> None:
        pl = pytest.importorskip("polars")
        frame = self._frame(
            [(self._ts(0), 100.0), (self._ts(1), 1000.0), (self._ts(2), 102.0)]
        ).rename({"ts_utc": "timestamp"})
        result = screen_frame_bad_ticks(frame, ts_column="timestamp")
        assert isinstance(result, pl.DataFrame)
        assert result.get_column("close").to_list() == [100.0, 102.0]

    def test_head_confirm_bars_constant(self) -> None:
        assert HEAD_CONFIRM_BARS == 3
