"""Unit tests for :mod:`brutex.adapters.empty_circuit_breaker` (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001).

100% line+branch on the pure per-pull breaker: the consecutive rule (post-data
signature + run-composition min-series + exact-limit boundary), the fraction rule
(min-observations arming + the fraction-series floor + the inclusive boundary),
the reviewer's THREE false-trigger scenarios as regression tests at PRODUCTION
defaults (worklist tail of late-listing prefixes / empty-dominated resume /
small targeted pull of recent listings — each tripped the pre-redesign breaker),
the must-trip throttle signatures (mid-pull sequential + parallel; from-pull-start
on a fresh universe), the provable expected-empty exclusions (predicate + the
per-call ledger hint), the leading-empty retro-clear, the disabled kill switch,
the latched raise-on-every-further-empty behaviour, ``allow_settle`` flipping on
a trip, the halt's count-naming message + degenerate-construction guard,
per-instance state isolation, env-config resolution incl. every fail-safe
fallback, and thread-safety under a concurrent fan-out.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date

import pytest
from brutex.adapters.empty_circuit_breaker import (
    EMPTY_BREAKER_CONSECUTIVE_ENV,
    EMPTY_BREAKER_CONSECUTIVE_LIMIT,
    EMPTY_BREAKER_ENABLED_ENV,
    EMPTY_BREAKER_FRACTION_ENV,
    EMPTY_BREAKER_FRACTION_LIMIT,
    EMPTY_BREAKER_FRACTION_MIN_SERIES,
    EMPTY_BREAKER_FRACTION_MIN_SERIES_ENV,
    EMPTY_BREAKER_MIN_OBSERVATIONS,
    EMPTY_BREAKER_MIN_OBSERVATIONS_ENV,
    EmptySettleBreaker,
    MassEmptySettleHalt,
    resolve_empty_breaker,
)
from structlog.testing import capture_logs

_END = date(2021, 4, 1)


def _observe_empties(
    breaker: EmptySettleBreaker,
    n: int,
    *,
    series: str = "NIFTY:1minute",
    expected: bool = False,
) -> None:
    """Feed *n* confirmed-empty windows of one series into *breaker*."""
    for _ in range(n):
        breaker.observe_empty(
            series, symbol=series.split(":", maxsplit=1)[0], window_end=_END, expected=expected
        )


class TestNoTrip:
    """Permutations that must NOT trip the breaker."""

    def test_single_empty_window_no_trip(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=3, fraction_limit=0.8, min_observations=5
        )
        _observe_empties(breaker, 1)
        assert not breaker.tripped
        assert breaker.allow_settle()

    def test_mixed_real_and_empty_below_thresholds_no_trip(self) -> None:
        # Empties interleaved with data windows: the consecutive run keeps resetting
        # and the fraction stays below the limit — the pull settles (loud verdict is
        # the verdict layer's job, not the breaker's).
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=3,
            fraction_limit=0.8,
            min_observations=4,
            fraction_min_series=1,
        )
        for i in range(10):
            if i % 2 == 0:
                breaker.observe_data("NIFTY:1minute")
            else:
                _observe_empties(breaker, 1)
        assert not breaker.tripped
        assert breaker.allow_settle()

    def test_consecutive_run_broken_by_data_never_trips(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=3, fraction_limit=1.0, min_observations=100
        )
        _observe_empties(breaker, 2)
        breaker.observe_data("BANKNIFTY:1minute")
        _observe_empties(breaker, 2)
        assert not breaker.tripped

    def test_consecutive_rule_disabled_by_nonpositive_limit(self) -> None:
        # Even a post-data all-empty run cannot trip a disabled (<= 0) consecutive rule.
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=0, fraction_limit=1.0, min_observations=100
        )
        breaker.observe_data("NIFTY:1minute")
        _observe_empties(breaker, 25)
        assert not breaker.tripped

    def test_fraction_not_armed_below_min_observations(self) -> None:
        # 100% empty but the sample is below min_observations — the fraction rule
        # never fires on a statistically-meaningless sample.
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.8,
            min_observations=10,
            fraction_min_series=1,
        )
        _observe_empties(breaker, 9)
        assert not breaker.tripped


class TestConsecutiveTrip:
    """The consecutive rule: trip exactly at the limit, halt loudly, stay latched.

    Every trip here seeds a data window FIRST — the consecutive rule requires the
    run to contain a POST-DATA empty (the throttle signature: a series that was
    producing bars went quiet). A pure pre-data run can never trip it (pinned in
    :class:`TestPostDataSignature`).
    """

    def test_trips_at_exact_consecutive_limit(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=3, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("NIFTY:1minute")  # the series produced data this pull
        _observe_empties(breaker, 2)
        assert not breaker.tripped
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1)
        assert breaker.tripped
        assert excinfo.value.trip_rule == "consecutive"
        assert excinfo.value.consecutive_empties == 3
        assert excinfo.value.empty_windows == 3
        assert excinfo.value.observed_windows == 4  # 1 data + 3 empties

    def test_halt_message_names_counts_and_causes(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("NIFTY:1minute")
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 2)
        message = str(excinfo.value)
        assert "2 of 3" in message  # the counts
        assert "soft-throttle" in message and "[R2]" in message
        assert "mis-addressed" in message and "[R3]" in message
        assert "pre-listing" in message
        assert "reopen-skipped" in message  # the re-verify path

    def test_already_tripped_keeps_raising_on_further_empties(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("NIFTY:1minute")
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 2)
        # A sibling in-flight worker observing another empty must also halt promptly.
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1, series="SENSEX:1minute")
        assert excinfo.value.trip_rule == "consecutive"

    def test_tripped_breaker_raises_even_on_expected_empty(self) -> None:
        # Post-trip, EVERY empty observation halts the worker — an expected-empty
        # (REOPENED / prior-EMPTY) window must not let its worker keep fetching
        # into the episode.
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=1, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("NIFTY:1minute")
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 1)
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 1, series="SENSEX:1minute", expected=True)

    def test_allow_settle_flips_false_on_trip(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("NIFTY:1minute")
        assert breaker.allow_settle()
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 2)
        assert not breaker.allow_settle()


class TestPostDataSignature:
    """The consecutive rule's throttle signature: the run must hold a post-data empty."""

    def test_pure_pre_data_run_never_trips_consecutive(self) -> None:
        # However long the all-empty run and however many series it spans, if NO
        # series in it has produced data this pull it is indistinguishable from a
        # cluster of pre-listing prefixes — the consecutive rule must stay silent.
        breaker = EmptySettleBreaker(
            min_series=3, consecutive_limit=5, fraction_limit=1.0, min_observations=9999
        )
        for i in range(60):
            _observe_empties(breaker, 1, series=f"S{i % 6}:1minute")
        assert not breaker.tripped

    def test_one_post_data_empty_arms_the_run(self) -> None:
        # The SAME shape with one series having produced data first → the run now
        # carries the throttle signature and trips at the limit.
        breaker = EmptySettleBreaker(
            min_series=3, consecutive_limit=5, fraction_limit=1.0, min_observations=9999
        )
        breaker.observe_data("S0:1minute")

        def _feed_run() -> None:
            for i in range(60):
                _observe_empties(breaker, 1, series=f"S{i % 6}:1minute")

        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _feed_run()
        assert excinfo.value.trip_rule == "consecutive"
        assert excinfo.value.consecutive_empties == 5  # exactly at the limit

    def test_data_window_resets_the_post_data_evidence(self) -> None:
        # A data window anywhere ends the run — series composition AND the
        # post-data tally restart from zero.
        breaker = EmptySettleBreaker(
            min_series=2, consecutive_limit=3, fraction_limit=1.0, min_observations=9999
        )
        breaker.observe_data("A:1minute")
        _observe_empties(breaker, 2, series="A:1minute")  # post-data run building…
        breaker.observe_data("B:1minute")  # …reset (data landed somewhere)
        # New run: 3 empties from B only (post-data) — 1 series < min_series 2.
        _observe_empties(breaker, 3, series="B:1minute")
        assert not breaker.tripped


class TestRunComposition:
    """The consecutive min-series gate reads the CURRENT run, never cumulative history."""

    def test_single_series_never_trips_below_min_series(self) -> None:
        # 100% empty on ONE series (a dead / delisted-mid-range symbol): below the
        # 3-series default gate the consecutive rule may not trip, however extreme
        # the counts — even with the post-data signature present.
        breaker = EmptySettleBreaker(consecutive_limit=2, fraction_limit=0.5, min_observations=1)
        breaker.observe_data("NIFTY:1minute")
        _observe_empties(breaker, 100)
        assert not breaker.tripped

    def test_two_series_still_below_default_gate(self) -> None:
        breaker = EmptySettleBreaker(consecutive_limit=2, fraction_limit=0.5, min_observations=1)
        breaker.observe_data("A:1minute")
        _observe_empties(breaker, 10, series="A:1minute")
        _observe_empties(breaker, 10, series="A:1day")
        assert not breaker.tripped

    def test_third_series_in_the_run_arms_the_trip(self) -> None:
        breaker = EmptySettleBreaker(consecutive_limit=2, fraction_limit=1.0, min_observations=999)
        breaker.observe_data("A:1minute")
        _observe_empties(breaker, 1, series="A:1minute")
        _observe_empties(breaker, 1, series="B:1minute")
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1, series="C:1minute")
        assert excinfo.value.trip_rule == "consecutive"

    def test_stale_counted_series_cannot_arm_a_later_run(self) -> None:
        # The pre-redesign bug shape: two series counted long ago (their runs ended)
        # must NOT arm a later single-series run — the gate reads the run itself.
        breaker = EmptySettleBreaker(
            min_series=3, consecutive_limit=3, fraction_limit=1.0, min_observations=9999
        )
        breaker.observe_data("A:1minute")
        _observe_empties(breaker, 1, series="A:1minute")
        _observe_empties(breaker, 1, series="B:1minute")
        breaker.observe_data("C:1minute")  # run ends — A/B evidence is history
        _observe_empties(breaker, 20, series="C:1minute")  # post-data, but 1 series
        assert not breaker.tripped


class TestFractionTrip:
    """The fraction rule: arms at min_observations, gated by the series floor."""

    def test_hundred_percent_empty_trips_at_min_observations(self) -> None:
        # "100% empty → trip the instant the sample arms" (the throttle-from-start
        # owner — no post-data ever exists there, so the fraction rule must fire).
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=1.0,
            min_observations=5,
            fraction_min_series=1,
        )
        _observe_empties(breaker, 4)
        assert not breaker.tripped
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1)
        assert excinfo.value.trip_rule == "fraction"
        assert excinfo.value.empty_windows == 5
        assert excinfo.value.observed_windows == 5

    def test_fraction_boundary_is_inclusive(self) -> None:
        # 4 empties / 5 observed == 0.8 — exactly the limit trips (>=).
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.8,
            min_observations=5,
            fraction_min_series=1,
        )
        breaker.observe_data("NIFTY:1minute")
        _observe_empties(breaker, 3, series="A:1minute")
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1, series="A:1minute")
        assert excinfo.value.trip_rule == "fraction"
        assert excinfo.value.empty_windows == 4
        assert excinfo.value.observed_windows == 5

    def test_below_fraction_never_trips(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.8,
            min_observations=4,
            fraction_min_series=1,
        )
        breaker.observe_data("NIFTY:1minute")
        breaker.observe_data("BANKNIFTY:1minute")
        _observe_empties(breaker, 2)  # 2/4 = 0.5 < 0.8
        assert not breaker.tripped

    def test_series_floor_blocks_small_pulls(self) -> None:
        # 100% empty over an armed sample, but counted empties span fewer distinct
        # series than the floor — a small targeted pull, not a throttled universe.
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.8,
            min_observations=10,
            fraction_min_series=4,
        )
        for i in range(30):
            _observe_empties(breaker, 1, series=f"S{i % 3}:1minute")  # 3 series only
        assert not breaker.tripped

    def test_series_floor_crossing_arms_the_trip(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.8,
            min_observations=10,
            fraction_min_series=4,
        )
        for i in range(9):
            _observe_empties(breaker, 1, series=f"S{i % 3}:1minute")
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1, series="S3:1minute")  # the 4th distinct series
        assert excinfo.value.trip_rule == "fraction"

    def test_retro_clear_removes_series_from_the_floor(self) -> None:
        # A retro-cleared series (its empties were a genuine pre-listing prefix)
        # leaves the fraction floor's evidence set with its prefix.
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.1,
            min_observations=1,
            fraction_min_series=3,
        )
        _observe_empties(breaker, 1, series="A:1minute")
        _observe_empties(breaker, 1, series="B:1minute")
        breaker.observe_data("A:1minute")  # retro-clear: A leaves the evidence set
        _observe_empties(breaker, 1, series="C:1minute")  # counted series: B + C = 2 < 3
        assert not breaker.tripped
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 1, series="D:1minute")  # 3rd distinct series


class TestFalseTripScenarios:
    """The hostile review's three false-halt scenarios — regression-pinned at PRODUCTION defaults.

    Each of these tripped the pre-redesign breaker (pull-wide consecutive counter +
    CUMULATIVE min-series gate); none may trip now. All three use a DEFAULT
    ``EmptySettleBreaker()`` — the exact production configuration.
    """

    def test_worklist_tail_of_late_listing_prefixes_does_not_trip(self) -> None:
        # Scenario 1 — the operational killer: a 2020→today bulk backfill whose
        # worklist TAIL holds >= 3 clustered late-listed NTM names. The healthy head
        # lands data across many series; the tail then contributes ~35 leading
        # empties per name, back-to-back (105 consecutive counted empties across 3
        # series — the pre-redesign trip: 105 >= 40 with a cumulative 3-series gate).
        # No tail series has produced data, so the run has no post-data empty →
        # the consecutive rule stays silent; the healthy head dilutes the fraction.
        breaker = EmptySettleBreaker()
        for i in range(60):  # the bulk head — 60 series land data twice each
            breaker.observe_data(f"HEAD{i}:1minute")
            breaker.observe_data(f"HEAD{i}:1minute")
        for tail in ("NTM_A", "NTM_B", "NTM_C"):  # the late-listed tail prefixes
            _observe_empties(breaker, 35, series=f"{tail}:1minute")
        assert not breaker.tripped
        observed, empties, consecutive = breaker.counts()
        assert (observed, empties, consecutive) == (225, 105, 105)

    def test_empty_dominated_resume_does_not_trip(self) -> None:
        # Scenario 2 — a resume whose worklist is dominated by prior pulls'
        # pre-listing empties: their ledger rows already say EMPTY, so the spine
        # passes expected=True (re-confirmation, not throttle evidence). 150
        # expected re-confirmations across 30 series + 2 genuinely-new counted
        # empties + a little fresh data → nothing trips.
        breaker = EmptySettleBreaker()
        for i in range(30):
            _observe_empties(breaker, 5, series=f"RESUME{i}:1minute", expected=True)
        breaker.observe_data("LIVE:1minute")
        _observe_empties(breaker, 2, series="NEW:1minute")
        assert not breaker.tripped
        observed, empties, _consecutive = breaker.counts()
        assert observed == 153
        assert empties == 2  # only the genuinely-new windows count

    def test_targeted_pull_of_recent_listings_does_not_trip(self) -> None:
        # Scenario 3 — a small targeted pull of 3 recently-listed F&O names, both
        # intervals, fanned out in parallel: their pre-listing prefixes interleave
        # into one long all-empty run spanning 6 series (the pre-redesign trip shape)
        # BEFORE any data lands. No post-data empty → no consecutive trip; 6 counted
        # series < the 10-series fraction floor → no fraction trip; once each series
        # reaches its listing date and lands data, the prefixes retro-clear.
        breaker = EmptySettleBreaker()
        series = [f"RECENT{i}:{iv}" for i in range(3) for iv in ("1minute", "1day")]
        for round_ in range(20):  # 120 interleaved leading empties, all-empty so far
            del round_
            for s in series:
                _observe_empties(breaker, 1, series=s)
        assert not breaker.tripped
        for s in series:  # each name reaches its listing date — prefixes self-clear
            breaker.observe_data(s)
        assert not breaker.tripped
        _observed, empties, consecutive = breaker.counts()
        assert empties == 0  # every prefix retro-cleared
        assert consecutive == 0


class TestThrottleSignatures:
    """The must-trip cases (hostile-review requirements (a) + (b)) at PRODUCTION defaults."""

    def test_mid_pull_throttle_parallel_trips_consecutive_fast(self) -> None:
        # (a) parallel bulk pull mid-throttle: many in-flight series were producing
        # data; the throttle empties them all → the run is post-data-rich, spans
        # many series, and trips at exactly the 40-window limit.
        breaker = EmptySettleBreaker()
        for i in range(8):
            breaker.observe_data(f"S{i}:1minute")

        def _throttle() -> None:
            for i in range(60):
                _observe_empties(breaker, 1, series=f"S{i % 8}:1minute")

        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _throttle()
        assert excinfo.value.trip_rule == "consecutive"
        assert excinfo.value.consecutive_empties == EMPTY_BREAKER_CONSECUTIVE_LIMIT

    def test_mid_pull_throttle_sequential_trips_within_a_few_symbols(self) -> None:
        # (a) sequential bulk pull mid-throttle: the current symbol's post-data
        # empties arm the run; the next symbols' (pre-data) empties extend it and
        # widen its series composition until the trip.
        breaker = EmptySettleBreaker()
        for _ in range(20):
            breaker.observe_data("K:1minute")  # symbol K was producing…

        def _boundary_throttle() -> None:
            _observe_empties(breaker, 30, series="K:1minute")  # …then goes quiet
            _observe_empties(breaker, 30, series="L:1minute")
            _observe_empties(breaker, 30, series="M:1minute")

        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _boundary_throttle()
        assert excinfo.value.trip_rule == "consecutive"

    def test_throttle_from_pull_start_on_fresh_universe_trips_fraction(self) -> None:
        # (b) a throttle active FROM PULL START on a fresh bulk universe: no series
        # ever lands data (the consecutive rule is structurally silent), but counted
        # empties quickly span >= the fraction-series floor at 100% empty — the
        # fraction rule owns this case and trips the moment the sample arms.
        breaker = EmptySettleBreaker()

        def _fresh_universe_throttle() -> None:
            for i in range(60):
                _observe_empties(breaker, 1, series=f"U{i % 12}:1minute")

        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _fresh_universe_throttle()
        assert excinfo.value.trip_rule == "fraction"
        assert excinfo.value.observed_windows == EMPTY_BREAKER_MIN_OBSERVATIONS

    def test_boundary_timed_sequential_throttle_is_an_honest_blind_spot(self) -> None:
        # HONEST ENVELOPE pin (module docstring blind spot (b)): a throttle starting
        # exactly at a symbol boundary of a sequential pull with a healthy head
        # empties only never-yet-producing series — shape-identical to a late-listing
        # tail, so the breaker does NOT halt; the settled-count verdict +
        # needs_operator threshold are the documented nets.
        breaker = EmptySettleBreaker()
        for i in range(10):
            breaker.observe_data(f"HEAD{i}:1minute")
        for i in range(5):
            _observe_empties(breaker, 20, series=f"COLD{i}:1minute")
        assert not breaker.tripped  # documented, deliberate — not a detection claim


class TestPreListing:
    """Provable pre-listing windows are observed but never counted."""

    def test_pre_listing_predicate_excludes_from_trip(self) -> None:
        listing = date(2023, 1, 1)
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=3,
            fraction_limit=1.0,
            min_observations=999,
            expected_empty=lambda _symbol, window_end: window_end < listing,
        )
        # Every empty window PROVABLY predates listing — no trip however many.
        for _ in range(10):
            breaker.observe_empty("NEWIPO:1minute", symbol="NEWIPO", window_end=date(2022, 6, 1))
        assert not breaker.tripped
        # Post-listing, post-data empties DO count: two below the limit, the third trips.
        breaker.observe_data("NEWIPO:1minute")
        for _ in range(2):
            breaker.observe_empty("NEWIPO:1minute", symbol="NEWIPO", window_end=date(2024, 6, 1))
        with pytest.raises(MassEmptySettleHalt):
            breaker.observe_empty("NEWIPO:1minute", symbol="NEWIPO", window_end=date(2024, 6, 1))
        assert breaker.tripped

    def test_default_no_predicate_counts_everything(self) -> None:
        # The honest default: no reliable listing-date source exists, so every empty
        # counts (register rule: "exclude only what the code can PROVE").
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("NEWIPO:1minute")
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 2, series="NEWIPO:1minute")


class TestExpectedHint:
    """The per-call ``expected`` hint (prior-EMPTY / REOPENED ledger rows)."""

    def test_expected_empties_observed_but_never_counted(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("A:1minute")
        _observe_empties(breaker, 10, series="A:1minute", expected=True)
        assert not breaker.tripped
        observed, empties, consecutive = breaker.counts()
        assert (observed, empties, consecutive) == (11, 0, 0)

    def test_expected_empty_is_neutral_to_the_run(self) -> None:
        # An expected empty neither extends NOR resets the consecutive run — the
        # counted empties around it still accumulate to the trip.
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("A:1minute")
        _observe_empties(breaker, 1, series="A:1minute")  # counted (run: 1)
        _observe_empties(breaker, 1, series="A:1minute", expected=True)  # neutral
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1, series="A:1minute")  # counted (run: 2 → trip)
        assert excinfo.value.consecutive_empties == 2


class TestLeadingEmptyRetroClear:
    """First-bar knowledge: a series' pre-listing prefix un-counts once data lands."""

    def test_leading_empties_uncounted_when_series_lands_data(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.8,
            min_observations=6,
            fraction_min_series=1,
        )
        # 4 leading empties on one late-listed series…
        _observe_empties(breaker, 4, series="SMALLCAP:1minute")
        # …then the series lands data: the prefix retro-clears (they were that
        # symbol's genuine pre-history, not throttle evidence).
        breaker.observe_data("SMALLCAP:1minute")
        # A 5th empty elsewhere: 1 counted empty / 6 observed = 0.17 < 0.8 — no trip.
        _observe_empties(breaker, 1, series="OTHER:1minute")
        assert not breaker.tripped

    def test_post_data_empties_still_count(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=3, fraction_limit=1.0, min_observations=999
        )
        breaker.observe_data("NIFTY:1minute")
        # Empties AFTER the series has data are mid-range holes — throttle-shaped.
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 3)
        assert breaker.tripped

    def test_retro_clear_fires_once_per_series(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.5,
            min_observations=4,
            fraction_min_series=1,
        )
        _observe_empties(breaker, 2, series="S:1minute")
        breaker.observe_data("S:1minute")  # clears the 2 leading empties
        breaker.observe_data("S:1minute")  # second data window: nothing more to clear
        # 2 more empties on the SAME series now count (post-data): 2/6 = 0.33 < 0.5.
        _observe_empties(breaker, 2, series="S:1minute")
        assert not breaker.tripped


class TestDisabled:
    """The kill switch: a disabled breaker observes nothing and never trips."""

    def test_disabled_never_trips_or_blocks_settles(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=1,
            fraction_limit=0.1,
            min_observations=1,
            fraction_min_series=1,
            enabled=False,
        )
        breaker.observe_data("NIFTY:1minute")
        _observe_empties(breaker, 5)
        breaker.observe_data("NIFTY:1minute")
        assert not breaker.tripped
        assert breaker.allow_settle()


class TestStateIsolation:
    """Breaker state never leaks across pulls — a fresh instance starts clean."""

    def test_fresh_instance_per_pull_starts_clean(self) -> None:
        first = EmptySettleBreaker(
            min_series=1, consecutive_limit=3, fraction_limit=1.0, min_observations=999
        )
        first.observe_data("NIFTY:1minute")
        _observe_empties(first, 2)
        # The "next pull" constructs a NEW breaker: the 2 prior post-data empties are
        # gone — 2 more (cumulative 4 > limit) still do not trip.
        second = EmptySettleBreaker(
            min_series=1, consecutive_limit=3, fraction_limit=1.0, min_observations=999
        )
        second.observe_data("NIFTY:1minute")
        _observe_empties(second, 2)
        assert not second.tripped

    def test_resolve_returns_new_instance_each_call(self) -> None:
        one = resolve_empty_breaker()
        two = resolve_empty_breaker()
        assert one is not two


class TestThreadSafety:
    """Concurrent observations never tear the counters (one lock, O(1) ops)."""

    def test_concurrent_observations_consistent(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=0, fraction_limit=1.0, min_observations=10_000
        )
        n_threads, per_thread = 8, 50

        def _work(idx: int) -> None:
            series = f"S{idx}:1minute"
            for _ in range(per_thread):
                breaker.observe_empty(series, symbol=f"S{idx}", window_end=_END)

        threads = [threading.Thread(target=_work, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 400 observed < 10_000 min_observations → never tripped; the exact tally
        # proves no lost update under the fan-out.
        assert not breaker.tripped
        observed, empties, consecutive = breaker.counts()
        assert observed == n_threads * per_thread
        assert empties == n_threads * per_thread
        assert consecutive == n_threads * per_thread


class TestResolveEnvConfig:
    """`resolve_empty_breaker` — env overrides with fail-safe fallbacks (DYNAMIC)."""

    def test_defaults_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in (
            EMPTY_BREAKER_CONSECUTIVE_ENV,
            EMPTY_BREAKER_FRACTION_ENV,
            EMPTY_BREAKER_MIN_OBSERVATIONS_ENV,
            EMPTY_BREAKER_FRACTION_MIN_SERIES_ENV,
            EMPTY_BREAKER_ENABLED_ENV,
        ):
            monkeypatch.delenv(env, raising=False)
        breaker = resolve_empty_breaker()
        assert breaker.consecutive_limit == EMPTY_BREAKER_CONSECUTIVE_LIMIT
        assert breaker.fraction_limit == EMPTY_BREAKER_FRACTION_LIMIT
        assert breaker.min_observations == EMPTY_BREAKER_MIN_OBSERVATIONS
        assert breaker.fraction_min_series == EMPTY_BREAKER_FRACTION_MIN_SERIES
        assert breaker.enabled

    def test_env_overrides_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_CONSECUTIVE_ENV, "7")
        monkeypatch.setenv(EMPTY_BREAKER_FRACTION_ENV, "0.5")
        monkeypatch.setenv(EMPTY_BREAKER_MIN_OBSERVATIONS_ENV, "12")
        monkeypatch.setenv(EMPTY_BREAKER_FRACTION_MIN_SERIES_ENV, "4")
        breaker = resolve_empty_breaker()
        assert breaker.consecutive_limit == 7
        assert breaker.fraction_limit == 0.5
        assert breaker.min_observations == 12
        assert breaker.fraction_min_series == 4

    def test_bad_int_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_CONSECUTIVE_ENV, "not-a-number")
        assert resolve_empty_breaker().consecutive_limit == EMPTY_BREAKER_CONSECUTIVE_LIMIT

    def test_blank_int_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_MIN_OBSERVATIONS_ENV, "   ")
        assert resolve_empty_breaker().min_observations == EMPTY_BREAKER_MIN_OBSERVATIONS

    def test_bad_fraction_min_series_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_FRACTION_MIN_SERIES_ENV, "junk")
        assert resolve_empty_breaker().fraction_min_series == EMPTY_BREAKER_FRACTION_MIN_SERIES

    def test_bad_fraction_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_FRACTION_ENV, "nope")
        assert resolve_empty_breaker().fraction_limit == EMPTY_BREAKER_FRACTION_LIMIT

    def test_blank_fraction_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_FRACTION_ENV, " ")
        assert resolve_empty_breaker().fraction_limit == EMPTY_BREAKER_FRACTION_LIMIT

    def test_out_of_range_fraction_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_FRACTION_ENV, "1.5")
        assert resolve_empty_breaker().fraction_limit == EMPTY_BREAKER_FRACTION_LIMIT
        monkeypatch.setenv(EMPTY_BREAKER_FRACTION_ENV, "0")
        assert resolve_empty_breaker().fraction_limit == EMPTY_BREAKER_FRACTION_LIMIT

    def test_kill_switch_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_ENABLED_ENV, "off")
        assert not resolve_empty_breaker().enabled
        monkeypatch.setenv(EMPTY_BREAKER_ENABLED_ENV, "FALSE")
        assert not resolve_empty_breaker().enabled

    def test_other_enabled_values_stay_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_ENABLED_ENV, "on")
        assert resolve_empty_breaker().enabled
        monkeypatch.setenv(EMPTY_BREAKER_ENABLED_ENV, "weird")
        assert resolve_empty_breaker().enabled

    def test_min_series_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.adapters.empty_circuit_breaker import (
            EMPTY_BREAKER_MIN_SERIES,
            EMPTY_BREAKER_MIN_SERIES_ENV,
        )

        monkeypatch.setenv(EMPTY_BREAKER_MIN_SERIES_ENV, "7")
        assert resolve_empty_breaker().min_series == 7
        monkeypatch.setenv(EMPTY_BREAKER_MIN_SERIES_ENV, "junk")
        assert resolve_empty_breaker().min_series == EMPTY_BREAKER_MIN_SERIES

    def test_resolver_threads_expected_empty(self) -> None:
        predicate = lambda _s, _e: True  # noqa: E731 — a tiny test predicate
        assert resolve_empty_breaker(expected_empty=predicate).expected_empty is predicate


class TestHaltGenericConstruction:
    """The halt is constructible like any exception (the global-stop pin contract)."""

    def test_plain_message_construction(self) -> None:
        # The pipeline drift-pin test builds every _GLOBAL_STOP_ERRORS member as
        # ``cls("x")`` — the halt must honour standard Exception semantics there.
        halt = MassEmptySettleHalt("x")
        assert str(halt) == "x"
        assert halt.empty_windows == 0
        assert halt.observed_windows == 0
        assert halt.consecutive_empties == 0
        assert halt.trip_rule == ""

    def test_keyword_construction_builds_the_counting_message(self) -> None:
        halt = MassEmptySettleHalt(
            empty_windows=4, observed_windows=5, consecutive_empties=3, trip_rule="fraction"
        )
        assert "4 of 5" in str(halt)
        assert "(fraction rule)" in str(halt)

    def test_zero_arg_construction_has_no_degenerate_hole(self) -> None:
        # ``MassEmptySettleHalt()`` must not render "( rule)" — the empty trip_rule
        # is labelled honestly.
        halt = MassEmptySettleHalt()
        assert "(unspecified rule)" in str(halt)
        assert "( rule)" not in str(halt)


# ── vendor-health canary (DEC-EMPTY-BREAKER-CANARY-001) ──────────────────────


def _canary_trip_breaker(probe: Callable[[], bool] | None) -> EmptySettleBreaker:
    """A breaker whose FRACTION rule trips on the 3rd counted empty (min floors 1)."""
    return EmptySettleBreaker(
        consecutive_limit=0,  # isolate the fraction rule
        fraction_limit=0.8,
        min_observations=3,
        min_series=1,
        fraction_min_series=1,
        health_probe=probe,
    )


class TestBreakerCanary:
    """The trip-time vendor-health canary seam."""

    def test_healthy_canary_suppresses_the_halt_and_logs(self) -> None:
        """Canary returns DATA ⇒ the empties are genuine ⇒ NO halt, one log.

        THE register-#6 false-halt: a genuinely-empty 2020-era chain at the
        bucket head tripped the breaker and halted the whole F&O phase; a
        healthy canary now proves the vendor fine and the pull continues (the
        settle law governs the empties).
        """
        probes: list[int] = []

        def _healthy() -> bool:
            probes.append(1)
            return True

        breaker = _canary_trip_breaker(_healthy)
        with capture_logs() as logs:
            _observe_empties(breaker, 3)  # crosses the fraction rule — no raise
        assert probes == [1], "exactly ONE canary call at the trip moment"
        assert not breaker.tripped
        assert breaker.allow_settle()
        (event,) = [e for e in logs if e["event"] == "empty_breaker.canary_vendor_healthy"]
        assert event["trip_rule"] == "fraction"
        assert event["empty_windows"] == 3

    def test_healthy_canary_resets_evidence_and_rearms(self) -> None:
        """After a healthy canary the evidence RESETS; a later storm re-probes.

        Bounded cost: ≤ 1 canary call per evidence cycle — the trip condition
        must re-accumulate from zero before the probe fires again; a REAL
        throttle beginning later is still caught (the second, unhealthy probe
        halts)."""
        verdicts = [True, False]
        probes: list[bool] = []

        def _probe() -> bool:
            verdict = verdicts[len(probes)]
            probes.append(verdict)
            return verdict

        breaker = _canary_trip_breaker(_probe)
        _observe_empties(breaker, 3)  # first trip → healthy canary → reset
        assert breaker.counts() == (0, 0, 0), "evidence resets after a healthy canary"
        _observe_empties(breaker, 2)  # rebuild BELOW the trip — no second probe yet
        assert probes == [True]
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 1)  # third counted empty re-trips → unhealthy → HALT
        assert probes == [True, False]
        assert breaker.tripped

    def test_unhealthy_canary_halts_exactly_as_today(self) -> None:
        """Canary ALSO empty ⇒ the trip latches + raises with the counts."""
        breaker = _canary_trip_breaker(lambda: False)
        with pytest.raises(MassEmptySettleHalt) as exc:
            _observe_empties(breaker, 3)
        assert breaker.tripped
        assert not breaker.allow_settle()
        assert exc.value.trip_rule == "fraction"
        assert exc.value.empty_windows == 3

    def test_raising_canary_is_unhealthy_and_halts(self) -> None:
        """A RAISING probe is NOT health evidence — halt, with the error logged."""

        def _boom() -> bool:
            raise RuntimeError("canary transport died")

        breaker = _canary_trip_breaker(_boom)
        with capture_logs() as logs, pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 3)
        (event,) = [e for e in logs if e["event"] == "empty_breaker.canary_probe_error"]
        assert event["error_type"] == "RuntimeError"

    def test_no_probe_is_byte_identical_trip_now(self) -> None:
        """DEFAULT-PRESERVATION pin: ``health_probe=None`` halts on the trip
        observation itself — today's behaviour, byte-identical."""
        breaker = _canary_trip_breaker(None)
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 3)
        assert breaker.tripped

    def test_settles_paused_while_probe_in_flight(self) -> None:
        """``allow_settle`` reads False DURING the canary (a trip candidate
        exists; an unhealthy verdict would make those settles poison)."""
        seen: list[bool] = []
        holder: list[EmptySettleBreaker] = []

        def _probe() -> bool:
            seen.append(holder[0].allow_settle())
            return True

        breaker = _canary_trip_breaker(_probe)
        holder.append(breaker)
        _observe_empties(breaker, 3)
        assert seen == [False], "settling must be suppressed while the canary is in flight"
        assert breaker.allow_settle(), "released after the healthy verdict"

    def test_probe_single_flight_under_concurrent_observers(self) -> None:
        """16 threads crossing the trip together fire EXACTLY one canary call."""
        started = threading.Barrier(16)
        release = threading.Event()
        probes: list[int] = []

        def _slow_probe() -> bool:
            probes.append(1)
            release.wait(timeout=10)
            return True

        breaker = EmptySettleBreaker(
            consecutive_limit=0,
            fraction_limit=0.5,
            min_observations=4,
            min_series=1,
            fraction_min_series=1,
            health_probe=_slow_probe,
        )
        errors: list[Exception] = []

        def _worker(i: int) -> None:
            started.wait(timeout=10)
            try:
                breaker.observe_empty(f"S{i}:1minute", symbol=f"S{i}", window_end=_END)
            except MassEmptySettleHalt:  # pragma: no cover — healthy canary, no halt expected
                pass
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        # Let the prober reach the probe, then release it.
        for _ in range(1000):
            if probes:
                break
            threading.Event().wait(0.005)
        release.set()
        for t in threads:
            t.join(timeout=10)
        assert errors == []
        assert probes == [1], "the canary is single-flight — exactly one probe"
        assert not breaker.tripped

    def test_resolver_threads_health_probe(self) -> None:
        """``resolve_empty_breaker(health_probe=…)`` lands on the instance;
        the default stays None (byte-identical construction)."""

        def _probe() -> bool:  # pragma: no cover — identity only
            return True

        assert resolve_empty_breaker(health_probe=_probe).health_probe is _probe
        assert resolve_empty_breaker().health_probe is None
