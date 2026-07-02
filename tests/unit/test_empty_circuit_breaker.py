"""Unit tests for :mod:`brutex.adapters.empty_circuit_breaker` (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001).

100% line+branch on the pure per-pull breaker: the consecutive + fraction trip
rules (boundaries included), the no-trip permutations (one empty; mixed
real+empty below threshold), the provable pre-listing exclusion, the
leading-empty retro-clear (first-bar knowledge), the disabled kill switch, the
latched raise-on-every-further-empty behaviour, ``allow_settle`` flipping on a
trip, the halt's count-naming message, per-instance state isolation (no leak
across pulls), env-config resolution incl. every fail-safe fallback, and
thread-safety under a concurrent fan-out.
"""

from __future__ import annotations

import threading
from datetime import date

import pytest
from brutex.adapters.empty_circuit_breaker import (
    EMPTY_BREAKER_CONSECUTIVE_ENV,
    EMPTY_BREAKER_CONSECUTIVE_LIMIT,
    EMPTY_BREAKER_ENABLED_ENV,
    EMPTY_BREAKER_FRACTION_ENV,
    EMPTY_BREAKER_FRACTION_LIMIT,
    EMPTY_BREAKER_MIN_OBSERVATIONS,
    EMPTY_BREAKER_MIN_OBSERVATIONS_ENV,
    EmptySettleBreaker,
    MassEmptySettleHalt,
    resolve_empty_breaker,
)

_END = date(2021, 4, 1)


def _observe_empties(breaker: EmptySettleBreaker, n: int, *, series: str = "NIFTY:1minute") -> None:
    """Feed *n* confirmed-empty windows of one series into *breaker*."""
    for _ in range(n):
        breaker.observe_empty(series, symbol=series.split(":", maxsplit=1)[0], window_end=_END)


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
            min_series=1, consecutive_limit=3, fraction_limit=0.8, min_observations=4
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
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=0, fraction_limit=1.0, min_observations=100
        )
        _observe_empties(breaker, 25)
        assert not breaker.tripped

    def test_fraction_not_armed_below_min_observations(self) -> None:
        # 100% empty but the sample is below min_observations — the single-dead-symbol
        # guard: a lone symbol's ~27 windows never meet a 50-observation gate.
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=0, fraction_limit=0.8, min_observations=10
        )
        _observe_empties(breaker, 9)
        assert not breaker.tripped


class TestConsecutiveTrip:
    """The consecutive rule: trip exactly at the limit, halt loudly, stay latched."""

    def test_trips_at_exact_consecutive_limit(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=3, fraction_limit=1.0, min_observations=999
        )
        _observe_empties(breaker, 2)
        assert not breaker.tripped
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1)
        assert breaker.tripped
        assert excinfo.value.trip_rule == "consecutive"
        assert excinfo.value.consecutive_empties == 3
        assert excinfo.value.empty_windows == 3
        assert excinfo.value.observed_windows == 3

    def test_halt_message_names_counts_and_causes(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 2)
        message = str(excinfo.value)
        assert "2 of 2" in message  # the counts
        assert "soft-throttle" in message and "[R2]" in message
        assert "mis-addressed" in message and "[R3]" in message
        assert "pre-listing" in message
        assert "reopen-skipped" in message  # the re-verify path

    def test_already_tripped_keeps_raising_on_further_empties(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 2)
        # A sibling in-flight worker observing another empty must also halt promptly.
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1, series="SENSEX:1minute")
        assert excinfo.value.trip_rule == "consecutive"

    def test_allow_settle_flips_false_on_trip(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=2, fraction_limit=1.0, min_observations=999
        )
        assert breaker.allow_settle()
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 2)
        assert not breaker.allow_settle()


class TestFractionTrip:
    """The fraction rule: arms at min_observations, trips at the ratio boundary."""

    def test_hundred_percent_empty_trips_at_min_observations(self) -> None:
        # "100% empty → trip immediately" (once the sample arms — every window empty).
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=0, fraction_limit=1.0, min_observations=5
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
            min_series=1, consecutive_limit=0, fraction_limit=0.8, min_observations=5
        )
        breaker.observe_data("NIFTY:1minute")
        _observe_empties(breaker, 3, series="A:1minute")
        # Break the leading-prefix association so the empties stay counted: series A
        # never lands data, series B's data window resets nothing of A's tally.
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1, series="A:1minute")
        assert excinfo.value.trip_rule == "fraction"
        assert excinfo.value.empty_windows == 4
        assert excinfo.value.observed_windows == 5

    def test_below_fraction_never_trips(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=0, fraction_limit=0.8, min_observations=4
        )
        breaker.observe_data("NIFTY:1minute")
        breaker.observe_data("BANKNIFTY:1minute")
        _observe_empties(breaker, 2)  # 2/4 = 0.5 < 0.8
        assert not breaker.tripped


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
        # Post-listing empties DO count: two below the limit, the third trips.
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
        with pytest.raises(MassEmptySettleHalt):
            _observe_empties(breaker, 2, series="NEWIPO:1minute")


class TestLeadingEmptyRetroClear:
    """First-bar knowledge: a series' pre-listing prefix un-counts once data lands."""

    def test_leading_empties_uncounted_when_series_lands_data(self) -> None:
        breaker = EmptySettleBreaker(
            min_series=1, consecutive_limit=0, fraction_limit=0.8, min_observations=6
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
            min_series=1, consecutive_limit=0, fraction_limit=0.5, min_observations=4
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
            min_series=1, consecutive_limit=1, fraction_limit=0.1, min_observations=1, enabled=False
        )
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
        _observe_empties(first, 2)
        # The "next pull" constructs a NEW breaker: the 2 prior empties are gone —
        # 2 more empties (cumulative 4 > limit) still do not trip.
        second = EmptySettleBreaker(
            min_series=1, consecutive_limit=3, fraction_limit=1.0, min_observations=999
        )
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
            EMPTY_BREAKER_ENABLED_ENV,
        ):
            monkeypatch.delenv(env, raising=False)
        breaker = resolve_empty_breaker()
        assert breaker.consecutive_limit == EMPTY_BREAKER_CONSECUTIVE_LIMIT
        assert breaker.fraction_limit == EMPTY_BREAKER_FRACTION_LIMIT
        assert breaker.min_observations == EMPTY_BREAKER_MIN_OBSERVATIONS
        assert breaker.enabled

    def test_env_overrides_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_CONSECUTIVE_ENV, "7")
        monkeypatch.setenv(EMPTY_BREAKER_FRACTION_ENV, "0.5")
        monkeypatch.setenv(EMPTY_BREAKER_MIN_OBSERVATIONS_ENV, "12")
        breaker = resolve_empty_breaker()
        assert breaker.consecutive_limit == 7
        assert breaker.fraction_limit == 0.5
        assert breaker.min_observations == 12

    def test_bad_int_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_CONSECUTIVE_ENV, "not-a-number")
        assert resolve_empty_breaker().consecutive_limit == EMPTY_BREAKER_CONSECUTIVE_LIMIT

    def test_blank_int_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(EMPTY_BREAKER_MIN_OBSERVATIONS_ENV, "   ")
        assert resolve_empty_breaker().min_observations == EMPTY_BREAKER_MIN_OBSERVATIONS

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


class TestMinSeriesGate:
    """One dead symbol is not a throttle — trips need min_series distinct series."""

    def test_single_series_never_trips_below_min_series(self) -> None:
        # 100% empty on ONE series (a dead / reference-only symbol): below the
        # 3-series default gate NEITHER rule may trip, however extreme the counts —
        # the pull settles honestly (loud settled-count verdict + reopen-skipped).
        breaker = EmptySettleBreaker(consecutive_limit=2, fraction_limit=0.5, min_observations=1)
        _observe_empties(breaker, 100)
        assert not breaker.tripped

    def test_two_series_still_below_default_gate(self) -> None:
        breaker = EmptySettleBreaker(consecutive_limit=2, fraction_limit=0.5, min_observations=1)
        _observe_empties(breaker, 10, series="A:1minute")
        _observe_empties(breaker, 10, series="A:1day")
        assert not breaker.tripped

    def test_third_series_arms_the_trip(self) -> None:
        breaker = EmptySettleBreaker(consecutive_limit=2, fraction_limit=1.0, min_observations=999)
        _observe_empties(breaker, 1, series="A:1minute")
        _observe_empties(breaker, 1, series="B:1minute")
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            _observe_empties(breaker, 1, series="C:1minute")
        assert excinfo.value.trip_rule == "consecutive"

    def test_retro_clear_removes_series_from_the_gate(self) -> None:
        # Two series' leading empties + a third — but series A lands data (its
        # empties were its genuine pre-history) BEFORE the third series' empty, so
        # only 2 series remain counted → still below the 3-series gate.
        breaker = EmptySettleBreaker(consecutive_limit=1, fraction_limit=1.0, min_observations=999)
        _observe_empties(breaker, 1, series="A:1minute")
        _observe_empties(breaker, 1, series="B:1minute")
        breaker.observe_data("A:1minute")  # retro-clear: A leaves the evidence set
        _observe_empties(breaker, 1, series="C:1minute")  # counted series: B + C = 2 < 3
        assert not breaker.tripped

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
