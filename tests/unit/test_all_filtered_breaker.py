"""Unit tests for the mass ALL-FILTERED circuit breaker (DEC-ALLFILTERED-MASS-BREAKER-001).

Covers both trip rules (SPREAD / FRACTION) with their floors as a truth table,
the env resolvers' fail-safe fallbacks + the kill switch, the observe-returns-
True-EXACTLY-ONCE latch (incl. a 16-thread ``Barrier`` race straddling the trip
— the DEC-GROWW-GOVERNOR-ATOMIC-001 pattern), the ``counts`` /
``trip_snapshot`` / ``allow_settle`` read surfaces, the disabled no-op, and a
hypothesis property that the breaker NEVER trips while below every rule's
conjuncts.
"""

from __future__ import annotations

import threading

import pytest
from brutex.adapters.all_filtered_breaker import (
    ALLFILTERED_BREAKER_ENABLED_ENV,
    ALLFILTERED_BREAKER_FRACTION,
    ALLFILTERED_BREAKER_FRACTION_ENV,
    ALLFILTERED_BREAKER_FRACTION_MIN_SERIES,
    ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV,
    ALLFILTERED_BREAKER_MIN_OBSERVATIONS,
    ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV,
    ALLFILTERED_BREAKER_SPREAD_SERIES,
    ALLFILTERED_BREAKER_SPREAD_SERIES_ENV,
    AllFilteredBreaker,
    AllFilteredTrip,
    resolve_all_filtered_breaker,
)
from hypothesis import given
from hypothesis import strategies as st


def _breaker(
    *,
    spread: int = 10,
    fraction: float = 0.5,
    min_obs: int = 20,
    fraction_min_series: int = 3,
    enabled: bool = True,
) -> AllFilteredBreaker:
    return AllFilteredBreaker(
        spread_series_limit=spread,
        fraction_limit=fraction,
        min_observations=min_obs,
        fraction_min_series=fraction_min_series,
        enabled=enabled,
    )


class TestTripRules:
    """The SPREAD + FRACTION rules and their floors — the truth table."""

    def test_spread_trips_at_the_distinct_series_limit(self) -> None:
        b = _breaker(spread=3, min_obs=1000)  # fraction rule armed far away
        assert b.observe("A:1minute", all_filtered=True) is False
        assert b.observe("B:1minute", all_filtered=True) is False
        assert b.observe("C:1minute", all_filtered=True) is True
        snap = b.trip_snapshot()
        assert snap is not None and snap.trip_rule == "spread"

    def test_spread_low_rate_across_many_series_trips(self) -> None:
        # The one-bad-vendor-day-inside-long-backfills shape: ~1 AF window per
        # series across >= spread series trips even at a LOW overall rate.
        b = _breaker(spread=10, min_obs=1000)
        tripped = False
        for i in range(10):
            for j in range(9):  # 9 clean windows per series — rate ~10%
                b.observe(f"S{i}:{j}", all_filtered=False)
            tripped = b.observe(f"S{i}:1minute", all_filtered=True) or tripped
        assert tripped and b.tripped
        snap = b.trip_snapshot()
        assert snap is not None and snap.trip_rule == "spread"

    def test_spread_repeat_series_never_trips(self) -> None:
        # ONE genuinely-weird symbol (1-2 series) is structurally below the
        # floor, however many DISTINCT windows it empties.
        b = _breaker(spread=3, min_obs=1000)
        for i in range(50):
            assert b.observe("A:1minute", all_filtered=True, window_start=f"w{i}") is False
            assert b.observe("A:1day", all_filtered=True, window_start=f"w{i}") is False
        assert not b.tripped
        assert b.counts() == (100, 100, 2)

    def test_spread_zero_disables_the_rule(self) -> None:
        b = _breaker(spread=0, min_obs=1000)
        for i in range(40):
            assert b.observe(f"S{i}:1minute", all_filtered=True) is False
        assert not b.tripped

    def test_fraction_trips_the_three_index_engine_pull(self) -> None:
        # THE engine-surface case IN ITS BACKFILL SHAPE: exactly 3 series
        # (3 indexes x 1minute) with >= min_obs eligible windows (7 per index)
        # under a vendor-day bug MUST be trippable — the fraction_min_series=3
        # floor. The ~3-window DAILY INCREMENTAL shape stays below the
        # observation floor (the small-pull blind spot pinned below).
        b = _breaker(spread=10, min_obs=20, fraction_min_series=3)
        trips = [
            b.observe(f"{sym}:1minute", all_filtered=True, window_start=f"w{i}")
            for i in range(7)
            for sym in ("NIFTY", "BANKNIFTY", "SENSEX")
        ]
        assert trips.count(True) == 1
        assert trips.index(True) == 19  # the 20th observation crossed min_obs
        snap = b.trip_snapshot()
        assert snap is not None and snap.trip_rule == "fraction"
        assert snap.distinct_series == 3

    def test_fraction_exactly_below_each_conjunct_never_trips(self) -> None:
        # 19 distinct windows (below min_obs=20) at 100% AF over 3 series → no trip.
        b = _breaker(spread=100, min_obs=20, fraction_min_series=3)
        for i in range(19):
            assert b.observe(f"S{i % 3}:1minute", all_filtered=True, window_start=f"w{i}") is False
        assert not b.tripped

    def test_fraction_below_rate_never_trips(self) -> None:
        # A steady ~33% AF rate over >= min_obs across >= 3 series stays below
        # the 0.5 limit at EVERY observation (the rules re-evaluate per observe,
        # so the interleaving keeps the running ratio under the rate throughout).
        # Every observation is a DISTINCT window (per-window dedupe, round 6).
        b = _breaker(spread=100, min_obs=20, fraction_min_series=3)
        for i in range(33):
            assert b.observe(f"S{i % 3}:1minute", all_filtered=True, window_start=f"w{i}") is False
            assert b.observe(f"D{i}:a", all_filtered=False) is False
            assert b.observe(f"D{i}:b", all_filtered=False) is False
        assert not b.tripped
        assert b.counts() == (99, 33, 3)

    def test_fraction_below_series_floor_never_trips(self) -> None:
        # 100% AF but only 2 distinct series (< 3) → structurally below.
        b = _breaker(spread=100, min_obs=20, fraction_min_series=3)
        for i in range(40):
            assert b.observe(f"S{i % 2}:1minute", all_filtered=True) is False
        assert not b.tripped

    def test_fraction_can_cross_on_a_data_observation(self) -> None:
        # min_obs crossed BY a clean window while the rate already sits at the
        # limit — the rules are evaluated on EVERY observation after counting.
        b = _breaker(spread=100, min_obs=20, fraction_min_series=3)
        for i in range(10):
            b.observe(f"S{i % 3}:1minute", all_filtered=True, window_start=f"w{i}")
        for i in range(9):
            b.observe("D:1minute", all_filtered=False, window_start=f"d{i}")
        assert b.observe("D:1minute", all_filtered=False, window_start="d9") is True  # 10/20
        snap = b.trip_snapshot()
        assert snap is not None and snap.trip_rule == "fraction"

    def test_fraction_min_series_floor_clamps_to_one(self) -> None:
        # fraction_min_series <= 1 arms on any series (the max(1, ...) clamp).
        b = _breaker(spread=100, min_obs=1, fraction_min_series=0)
        assert b.observe("A:1minute", all_filtered=True) is True

    def test_default_config_daily_incremental_engine_pull_cannot_trip(self) -> None:
        # HONEST-ENVELOPE PIN (review round 2, Golden Rule #10): the ENGINE's
        # DAILY INCREMENTAL pull observes ~3 eligible windows (3 indexes x
        # 1minute x one open window) — under the DEFAULTS that is below BOTH
        # floors (3 series < SPREAD 10; 3 observed < min_observations 20), so
        # a persisting vendor-day bug at this scale trips NOTHING and the
        # windows settle SKIPPED after EMPTY_TERMINAL_THRESHOLD pulls; the
        # per-window WARNs + streak provenance + `brutex reopen-skipped` are
        # the backstops. The "engine pull trippable" claim holds for the
        # BACKFILL shape only (the test above). Deliberately pinned so the
        # documented blind spot can never silently drift.
        for _ in range(3):  # EMPTY_TERMINAL_THRESHOLD pulls of the same shape
            b = _breaker()  # the documented defaults; fresh per pull
            for sym in ("NIFTY", "BANKNIFTY", "SENSEX"):
                assert b.observe(f"{sym}:1minute", all_filtered=True) is False
            assert not b.tripped
            assert b.counts() == (3, 3, 3)

    def test_default_config_diluted_late_mass_event_escapes(self) -> None:
        # HONEST-ENVELOPE PIN (review round 2): a LATE mass event spanning
        # 3-9 series (below SPREAD's 10) inside a LARGE pull whose whole-pull
        # all-filtered fraction stays < 0.5 escapes BOTH rules — the fraction
        # is whole-pull over UNIQUE windows, never windowed, so 91 clean
        # windows dilute an 18-window burst across 9 series to 18/109 < 0.5.
        # Broader than the documented <= 2-series hole; pinned honest.
        b = _breaker()  # the documented defaults
        for i in range(91):
            assert b.observe(f"CLEAN{i}:1minute", all_filtered=False) is False
        for i in range(9):
            for j in range(2):  # 2 DISTINCT windows per burst series
                assert b.observe(f"BAD{i}:1minute", all_filtered=True, window_start=f"w{j}") is (
                    False
                )
        assert not b.tripped
        assert b.counts() == (109, 18, 9)


class TestLatchAndSurfaces:
    """observe-once latch + counts/trip_snapshot/allow_settle + disabled no-op."""

    def test_observe_returns_true_exactly_once_then_keeps_counting(self) -> None:
        b = _breaker(spread=2, min_obs=1000)
        results = [b.observe(f"S{i}:1minute", all_filtered=True) for i in range(10)]
        assert results.count(True) == 1
        # Post-trip observations keep counting (telemetry) — never re-return True.
        assert b.counts() == (10, 10, 10)
        assert not b.allow_settle()

    def test_allow_settle_true_until_trip(self) -> None:
        b = _breaker(spread=2, min_obs=1000)
        assert b.allow_settle()
        b.observe("A:1m", all_filtered=True)
        assert b.allow_settle()
        b.observe("B:1m", all_filtered=True)
        assert not b.allow_settle()

    def test_trip_snapshot_none_until_tripped_then_counts(self) -> None:
        b = _breaker(spread=2, min_obs=1000)
        assert b.trip_snapshot() is None
        b.observe("B:1m", all_filtered=True, window_start="2026-01-01", window_end="2026-02-01")
        b.observe("A:1m", all_filtered=True, window_start="2026-02-01", window_end="2026-03-01")
        snap = b.trip_snapshot()
        assert snap == AllFilteredTrip(
            trip_rule="spread",
            all_filtered_windows=2,
            observed_windows=2,
            distinct_series=2,
            series_sample=("A:1m", "B:1m"),  # sorted — deterministic
            window_samples=(
                "B:1m 2026-01-01→2026-02-01",
                "A:1m 2026-02-01→2026-03-01",
            ),
        )

    def test_samples_are_capped_at_ten(self) -> None:
        b = _breaker(spread=1000, min_obs=10_000)
        for i in range(25):
            b.observe(f"S{i}:1m", all_filtered=True, window_start="s", window_end="e")
        # Force a trip via a tiny fraction config on a second breaker is not
        # needed — read the internals through a trip on a fresh breaker instead.
        b2 = _breaker(spread=12, min_obs=10_000)
        for i in range(12):
            b2.observe(f"S{i:02d}:1m", all_filtered=True, window_start="s", window_end="e")
        snap = b2.trip_snapshot()
        assert snap is not None
        assert len(snap.series_sample) == 10
        assert len(snap.window_samples) == 10

    def test_disabled_breaker_counts_nothing_and_always_allows(self) -> None:
        b = _breaker(spread=1, min_obs=0, fraction_min_series=0, enabled=False)
        for i in range(30):
            assert b.observe(f"S{i}:1m", all_filtered=True) is False
        assert b.counts() == (0, 0, 0)
        assert not b.tripped
        assert b.allow_settle()

    def test_sixteen_thread_barrier_trips_exactly_once(self) -> None:
        # The DEC-GROWW-GOVERNOR-ATOMIC-001 pattern: 16 threads straddle the trip
        # boundary simultaneously — the latch transition is returned EXACTLY once.
        b = _breaker(spread=8, min_obs=10_000)
        barrier = threading.Barrier(16)
        trips: list[bool] = []
        lock = threading.Lock()

        def _worker(i: int) -> None:
            barrier.wait()
            got = b.observe(f"S{i}:1m", all_filtered=True)
            with lock:
                trips.append(got)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert trips.count(True) == 1
        assert b.tripped
        assert b.counts() == (16, 16, 16)


class TestPerWindowDedup:
    """Review round 6 — counters are per-WINDOW deduped, latest verdict wins.

    Pre-fix ``observe()`` counted ATTEMPTS: retry passes re-attempt ONLY
    still-FAILED windows, so a persistent event's all-filtered windows were
    re-counted up to ``EMPTY_TERMINAL_THRESHOLD`` times while clean windows
    counted once — FRACTION tripped at ~25% of unique windows (below the
    documented 0.5 floor) and the CRITICAL alarm sold attempt counts as
    windows (up to 3× inflation).
    """

    def test_failure_biased_retry_reobservation_cannot_trip_fraction(self) -> None:
        # THE round-6 regression, at the PRODUCTION defaults: 30 persistent
        # all-filtered windows of 100 unique across 3 series (30% < 0.5 —
        # below the FRACTION floor; 3 series < SPREAD 10). Two retry passes
        # re-attempt ONLY the 30 still-FAILED windows. FAIL-BEFORE: cumulative
        # attempt counting read (160, 90, 3) after pass 3 — 90/160 = 0.5625
        # TRIPPED at an effective ~0.25 unique-window floor and the alarm
        # reported "90 of 160 windows" for a 30-of-100 event.
        b = _breaker()  # the documented defaults
        for i in range(70):
            assert b.observe(f"CLEAN{i}:1minute", all_filtered=False) is False
        for _pass in range(3):  # pass 1 + 2 failure-biased retry passes
            for i in range(30):
                assert (
                    b.observe(f"BAD{i % 3}:1minute", all_filtered=True, window_start=f"w{i}")
                    is False
                )
        assert not b.tripped
        assert b.allow_settle()
        assert b.counts() == (100, 30, 3)  # the unique-window truth, all passes

    def test_same_window_reobservation_is_a_counter_noop(self) -> None:
        b = _breaker(spread=100, min_obs=1000)
        for _ in range(5):  # same verdict, same window — one count
            b.observe("A:1m", all_filtered=True, window_start="s", window_end="e")
        for _ in range(5):
            b.observe("B:1m", all_filtered=False, window_start="s", window_end="e")
        assert b.counts() == (2, 1, 1)

    def test_verdict_flip_to_clean_decrements(self) -> None:
        # A window all-filtered on pass 1 that comes back CLEAN on a retry
        # (vendor recovered mid-pull) stops counting as evidence — latest wins.
        b = _breaker(spread=100, min_obs=1000)
        b.observe("A:1m", all_filtered=True, window_start="s")
        assert b.counts() == (1, 1, 1)
        b.observe("A:1m", all_filtered=False, window_start="s")
        assert b.counts() == (1, 0, 0)

    def test_verdict_flip_to_all_filtered_counts(self) -> None:
        # The reverse flip (a mass event starting BETWEEN passes) is seen —
        # first-verdict-wins would have been blind to it.
        b = _breaker(spread=100, min_obs=1000)
        b.observe("A:1m", all_filtered=False, window_start="s")
        assert b.counts() == (1, 0, 0)
        b.observe("A:1m", all_filtered=True, window_start="s")
        assert b.counts() == (1, 1, 1)

    def test_flip_keeps_series_while_other_af_windows_remain(self) -> None:
        # Two AF windows share one series; flipping ONE clean keeps the series
        # counted for the other (per-series tallies, not a bare set).
        b = _breaker(spread=100, min_obs=1000)
        b.observe("A:1m", all_filtered=True, window_start="w1")
        b.observe("A:1m", all_filtered=True, window_start="w2")
        assert b.counts() == (2, 2, 1)
        b.observe("A:1m", all_filtered=False, window_start="w1")
        assert b.counts() == (2, 1, 1)
        b.observe("A:1m", all_filtered=False, window_start="w2")
        assert b.counts() == (2, 0, 0)

    def test_explicit_window_key_distinguishes_same_series_windows(self) -> None:
        # The F&O tail's shape: the series is the UNDERLYING, shared by a whole
        # CE/PE chain whose contracts are DISTINCT windows over the SAME span —
        # the explicit contract-scoped window_key keeps each counted once.
        b = _breaker(spread=100, min_obs=1000)
        for strike in (100, 200):
            b.observe(
                "NIFTY:1minute",
                all_filtered=True,
                window_key=f"NSE-NIFTY-02Jan25-{strike}-CE:1minute|s|e",
                window_start="s",
                window_end="e",
            )
        assert b.counts() == (2, 2, 1)

    def test_sample_not_duplicated_on_reflip(self) -> None:
        # AF → clean → AF re-flip of one window keeps ONE sample (a flip never
        # retracts a sample; a re-flip never duplicates one).
        b = _breaker(spread=100, min_obs=1000)
        for verdict in (True, False, True):
            b.observe("A:1m", all_filtered=verdict, window_start="s", window_end="e")
        snap_b = _breaker(spread=1, min_obs=1000)  # trip a sibling to read samples
        for verdict in (True, False, True):
            snap_b.observe("A:1m", all_filtered=verdict, window_start="s", window_end="e")
        snap = snap_b.trip_snapshot()
        assert snap is not None
        assert snap.window_samples == ("A:1m s→e",)

    def test_trip_latch_survives_a_flip_below_the_rule(self) -> None:
        # The latch is one-way: flipping every AF window clean AFTER the trip
        # keeps settles suppressed (counters honestly reflect the flips).
        b = _breaker(spread=2, min_obs=1000)
        b.observe("A:1m", all_filtered=True, window_start="s")
        assert b.observe("B:1m", all_filtered=True, window_start="s") is True
        b.observe("A:1m", all_filtered=False, window_start="s")
        b.observe("B:1m", all_filtered=False, window_start="s")
        assert b.tripped
        assert not b.allow_settle()
        assert b.counts() == (2, 0, 0)

    @given(
        observations=st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=4),
                st.integers(min_value=0, max_value=7),
                st.booleans(),
            ),
            max_size=80,
        )
    )
    def test_counts_equal_the_unique_window_fold(
        self, observations: list[tuple[int, int, bool]]
    ) -> None:
        # For ANY interleaving of (series, window, verdict) observations —
        # re-observations and flips in both directions included — the counters
        # equal the naive latest-verdict-per-unique-window fold.
        b = _breaker(spread=10**6, min_obs=10**6, fraction_min_series=3)
        latest: dict[tuple[int, int], bool] = {}
        for series_idx, window_idx, all_filtered in observations:
            b.observe(
                f"S{series_idx}:1m",
                all_filtered=all_filtered,
                window_start=f"w{window_idx}",
            )
            latest[(series_idx, window_idx)] = all_filtered
        af_windows = {key for key, verdict in latest.items() if verdict}
        assert b.counts() == (
            len(latest),
            len(af_windows),
            len({series_idx for series_idx, _ in af_windows}),
        )
        assert not b.tripped


class TestResolver:
    """resolve_all_filtered_breaker — env config, fail-safe fallbacks, kill switch."""

    def test_defaults_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in (
            ALLFILTERED_BREAKER_ENABLED_ENV,
            ALLFILTERED_BREAKER_SPREAD_SERIES_ENV,
            ALLFILTERED_BREAKER_FRACTION_ENV,
            ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV,
            ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV,
        ):
            monkeypatch.delenv(env, raising=False)
        b = resolve_all_filtered_breaker()
        assert b.enabled
        assert b.spread_series_limit == ALLFILTERED_BREAKER_SPREAD_SERIES
        assert b.fraction_limit == ALLFILTERED_BREAKER_FRACTION
        assert b.min_observations == ALLFILTERED_BREAKER_MIN_OBSERVATIONS
        assert b.fraction_min_series == ALLFILTERED_BREAKER_FRACTION_MIN_SERIES

    def test_env_overrides_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALLFILTERED_BREAKER_SPREAD_SERIES_ENV, "4")
        monkeypatch.setenv(ALLFILTERED_BREAKER_FRACTION_ENV, "0.9")
        monkeypatch.setenv(ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV, "5")
        monkeypatch.setenv(ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV, "2")
        b = resolve_all_filtered_breaker()
        assert (b.spread_series_limit, b.fraction_limit) == (4, 0.9)
        assert (b.min_observations, b.fraction_min_series) == (5, 2)

    def test_bad_env_values_fail_safe_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALLFILTERED_BREAKER_SPREAD_SERIES_ENV, "not-an-int")
        monkeypatch.setenv(ALLFILTERED_BREAKER_FRACTION_ENV, "1.5")  # out of (0, 1]
        monkeypatch.setenv(ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV, "")
        monkeypatch.setenv(ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV, "x")
        b = resolve_all_filtered_breaker()
        assert b.spread_series_limit == ALLFILTERED_BREAKER_SPREAD_SERIES
        assert b.fraction_limit == ALLFILTERED_BREAKER_FRACTION
        assert b.min_observations == ALLFILTERED_BREAKER_MIN_OBSERVATIONS
        assert b.fraction_min_series == ALLFILTERED_BREAKER_FRACTION_MIN_SERIES
        assert b.enabled  # a mis-typed value can never disable the protection

    @pytest.mark.parametrize("off", ["off", "0", "false", "no", "OFF"])
    def test_kill_switch_disables(self, monkeypatch: pytest.MonkeyPatch, off: str) -> None:
        monkeypatch.setenv(ALLFILTERED_BREAKER_ENABLED_ENV, off)
        b = resolve_all_filtered_breaker()
        assert not b.enabled
        assert b.observe("A:1m", all_filtered=True) is False
        assert b.allow_settle()

    def test_non_off_enabled_value_keeps_it_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALLFILTERED_BREAKER_ENABLED_ENV, "on")
        assert resolve_all_filtered_breaker().enabled


class TestNeverTripsBelowConjunctsProperty:
    """Hypothesis: below EVERY rule's conjuncts the breaker never trips."""

    @given(
        observations=st.lists(
            st.tuples(st.integers(min_value=0, max_value=1), st.booleans()),
            max_size=60,
        )
    )
    def test_never_trips_while_below_every_conjunct(
        self, observations: list[tuple[int, bool]]
    ) -> None:
        # spread=3 / fraction over >=20 obs across >=3 series: feeding at most
        # TWO distinct series keeps BOTH rules structurally below their floors
        # regardless of rate, count, or interleaving (every observation a
        # DISTINCT window, so the count genuinely grows past the floors).
        b = _breaker(spread=3, min_obs=20, fraction_min_series=3)
        for n, (series_idx, all_filtered) in enumerate(observations):
            assert (
                b.observe(f"S{series_idx}:1m", all_filtered=all_filtered, window_start=f"w{n}")
                is False
            )
        assert not b.tripped
        assert b.allow_settle()
