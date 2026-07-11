"""Unit tests for brutex.orchestrator.rate_limiter — adaptive token bucket.

Every test uses an **injected fake clock** so the whole suite runs in
milliseconds (no real ``time.sleep`` ever called). This proves the
determinism guarantee — the limiter's behaviour is purely a function
of the timing the caller reports.
"""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Callable, Iterator

import pytest
from brutex.orchestrator import rate_limiter as rate_limiter_module
from brutex.orchestrator.rate_limiter import (
    _ACQUIRE_LOOP_CAP,
    _LEGACY_HARD_CAP_RPS,
    DEFAULT_BURST,
    DEFAULT_DOWNSHIFT_RPS,
    DEFAULT_DOWNSHIFT_TRIGGER,
    DEFAULT_DOWNSHIFT_WINDOW,
    DEFAULT_INITIAL_RPS,
    DEFAULT_UPSHIFT_RECOVERY,
    AdaptiveTokenBucket,
    RateLimiterError,
    RateLimiterEvent,
)
from structlog.testing import capture_logs

# ────────────────────────────────────────────────────────────────────────
# Fake clock & sleep helpers — deterministic test infrastructure
# ────────────────────────────────────────────────────────────────────────


class FakeClock:
    """Monotonic clock controlled by tests.

    Exposes ``.now`` as the injected ``clock`` callable. Calling
    :meth:`advance` moves it forward. Calling ``sleep(s)`` also
    advances by ``s``.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def sleep(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bucket(clock: FakeClock) -> AdaptiveTokenBucket:
    return AdaptiveTokenBucket(
        initial_rps=15.0,
        downshift_rps=10.0,
        downshift_trigger=3,
        downshift_window=60.0,
        upshift_recovery=120.0,
        burst=5,
        clock=clock.now,
        sleep=clock.sleep,
    )


# ────────────────────────────────────────────────────────────────────────
# Construction / validation
# ────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_defaults_are_sane(self) -> None:
        b = AdaptiveTokenBucket()
        assert b.initial_rps == DEFAULT_INITIAL_RPS == 3.0
        assert b.downshift_rps == DEFAULT_DOWNSHIFT_RPS == 1.5
        assert b.burst >= 1

    def test_zero_initial_rps_rejected(self) -> None:
        with pytest.raises(RateLimiterError):
            AdaptiveTokenBucket(initial_rps=0.0)

    def test_negative_rps_rejected(self) -> None:
        with pytest.raises(RateLimiterError):
            AdaptiveTokenBucket(initial_rps=-1.0)

    def test_downshift_not_below_initial_rejected(self) -> None:
        with pytest.raises(RateLimiterError, match="must be <"):
            AdaptiveTokenBucket(initial_rps=15.0, downshift_rps=20.0)

    def test_equal_rps_rejected(self) -> None:
        with pytest.raises(RateLimiterError, match="must be <"):
            AdaptiveTokenBucket(initial_rps=15.0, downshift_rps=15.0)

    def test_zero_trigger_rejected(self) -> None:
        with pytest.raises(RateLimiterError):
            AdaptiveTokenBucket(downshift_trigger=0)

    def test_zero_downshift_rps_rejected(self) -> None:
        """``downshift_rps <= 0`` fails fast (rate_limiter.py:227).

        Distinct from the downshift-vs-initial ordering check — a zero
        downshift rate would stall every post-429 acquire forever
        (DEC-ORCH-STRICT-GATES-001 standalone-gap closure).
        """
        with pytest.raises(RateLimiterError, match="downshift_rps must be > 0"):
            AdaptiveTokenBucket(downshift_rps=0.0)

    def test_zero_burst_rejected(self) -> None:
        with pytest.raises(RateLimiterError):
            AdaptiveTokenBucket(burst=0)

    # ── Byte-exact gate pins (DEC-MUTATION-RUNTIME-001) ────────────
    # `match=` is a substring search, so it cannot kill mutmut's
    # XX-wrapped message mutants; each guard message is pinned with
    # `==` on str(exc.value), and each `<`/`<=` boundary is pinned by
    # constructing AT the boundary (house style: test_ranking.py).

    def test_zero_initial_rps_message_byte_exact(self) -> None:
        """Kills the `<= 0` → `< 0` flip too: at 0.0 the mutant falls
        through to the ordering guard, whose message differs."""
        with pytest.raises(RateLimiterError) as exc:
            AdaptiveTokenBucket(initial_rps=0.0)
        assert str(exc.value) == "initial_rps must be > 0, got 0.0"

    def test_initial_rps_of_exactly_one_accepted(self) -> None:
        """Boundary pin: `initial_rps <= 0` must NOT drift to `<= 1`."""
        b = AdaptiveTokenBucket(initial_rps=1.0, downshift_rps=0.5)
        assert b.initial_rps == 1.0

    def test_zero_downshift_rps_message_byte_exact(self) -> None:
        with pytest.raises(RateLimiterError) as exc:
            AdaptiveTokenBucket(downshift_rps=0.0)
        assert str(exc.value) == "downshift_rps must be > 0, got 0.0"

    def test_downshift_rps_of_exactly_one_accepted(self) -> None:
        """Boundary pin: `downshift_rps <= 0` must NOT drift to `<= 1`."""
        b = AdaptiveTokenBucket(initial_rps=3.0, downshift_rps=1.0)
        assert b.downshift_rps == 1.0

    def test_downshift_ordering_message_byte_exact(self) -> None:
        with pytest.raises(RateLimiterError) as exc:
            AdaptiveTokenBucket(initial_rps=15.0, downshift_rps=20.0)
        assert str(exc.value) == "downshift_rps (20.0) must be < initial_rps (15.0)"

    def test_trigger_of_exactly_one_accepted(self) -> None:
        """Boundary pin: `downshift_trigger < 1` must NOT drift to
        `<= 1` or `< 2` — a single-429 trigger stays constructible."""
        b = AdaptiveTokenBucket(downshift_trigger=1)
        assert b.downshift_trigger == 1

    def test_zero_trigger_message_byte_exact(self) -> None:
        with pytest.raises(RateLimiterError) as exc:
            AdaptiveTokenBucket(downshift_trigger=0)
        assert str(exc.value) == "downshift_trigger must be ≥ 1, got 0"

    def test_zero_burst_message_byte_exact(self) -> None:
        with pytest.raises(RateLimiterError) as exc:
            AdaptiveTokenBucket(burst=0)
        assert str(exc.value) == "burst must be ≥ 1, got 0"


# ────────────────────────────────────────────────────────────────────────
# Byte-exact constant + default pins (DEC-MUTATION-RUNTIME-001)
# ────────────────────────────────────────────────────────────────────────


class TestConstantPins:
    """Pin every module constant byte-exact.

    The rate-limit constants are SAFETY-CRITICAL (CLAUDE.md §2 rule 6:
    Dhan 5 r/s cap, adaptive [3..5] r/s band, never raise) — a silent
    constant drift is exactly the mutant class these pins kill.
    """

    def test_module_constants_byte_exact(self) -> None:
        assert DEFAULT_INITIAL_RPS == 3.0
        assert DEFAULT_DOWNSHIFT_RPS == 1.5
        assert DEFAULT_DOWNSHIFT_TRIGGER == 3
        assert DEFAULT_DOWNSHIFT_WINDOW == 60.0
        assert DEFAULT_UPSHIFT_RECOVERY == 120.0
        assert DEFAULT_BURST == 5
        assert _LEGACY_HARD_CAP_RPS == 5.0
        assert _ACQUIRE_LOOP_CAP == 16

    def test_field_defaults_byte_exact(self) -> None:
        """The dataclass field defaults (not just the constants) are pinned —
        kills the `DEFAULT_* → None` field-default mutants and the
        `start_empty: False → None` flip (None is falsy, so only an
        identity pin can tell them apart)."""
        b = AdaptiveTokenBucket()
        assert b.downshift_window == 60.0
        assert b.upshift_recovery == 120.0
        assert b.burst == 5
        assert b.start_empty is False
        assert b.quota_tracker is None

    def test_last_refill_stamped_from_injected_clock(self) -> None:
        """__post_init__ stamps `_last_refill` from the injected clock
        (rate_limiter.py:245) — pinned via the fake clock's start value
        so a `None` stamp fails HERE, fast, not as a TypeError deep
        inside a later refill."""
        clock = FakeClock()
        b = AdaptiveTokenBucket(
            initial_rps=15.0,
            downshift_rps=10.0,
            clock=clock.now,
            sleep=clock.sleep,
        )
        assert b._last_refill == 1000.0


# ────────────────────────────────────────────────────────────────────────
# Dataclass discipline (DEC-MUTATION-RUNTIME-001; house style:
# test_ranking.py TestRankedComboDataclass)
# ────────────────────────────────────────────────────────────────────────


class TestEventDataclass:
    def _ev(self) -> RateLimiterEvent:
        return RateLimiterEvent(kind="rate_limit_hit", at=1000.0)

    def test_frozen(self) -> None:
        # frozen=True — a published event is immutable once emitted.
        with pytest.raises(dataclasses.FrozenInstanceError):
            self._ev().kind = "upshift"  # type: ignore[misc]

    def test_slots(self) -> None:
        # slots=True — a slotted instance carries no per-instance __dict__.
        assert not hasattr(self._ev(), "__dict__")
        assert hasattr(RateLimiterEvent, "__slots__")

    def test_optional_fields_default_byte_exact(self) -> None:
        # Pins the defaults whose lines carry a line-scoped
        # `pragma: no mutate` (equivalent PEP 563 annotation flips) —
        # None (NOT "" or any sentinel) and exactly 0.
        ev = self._ev()
        assert ev.from_rps is None
        assert ev.to_rps is None
        assert ev.recent_429_count == 0


class TestBucketDataclassDiscipline:
    def test_slots(self, bucket: AdaptiveTokenBucket) -> None:
        # slots=True — O(1) attribute access, no stray per-instance dict.
        assert not hasattr(bucket, "__dict__")
        assert hasattr(AdaptiveTokenBucket, "__slots__")

    def test_runtime_state_fields_not_in_init(self) -> None:
        """Every runtime-state field is `field(init=False)` with NO
        default — callers can never smuggle state through the
        constructor (kills both the `init=True` flips and the
        `field(...) → None` plain-default mutants)."""
        by_name = {f.name: f for f in dataclasses.fields(AdaptiveTokenBucket)}
        for name in (
            "_current_rps",
            "_tokens",
            "_last_refill",
            "_recent_429s",
            "_last_success_at",
            "_lock",
        ):
            f = by_name[name]
            assert f.init is False, name
            assert f.default is dataclasses.MISSING, name
            assert f.default_factory is dataclasses.MISSING, name
        pending = by_name["_pending_events"]
        assert pending.init is False
        assert pending.default_factory is list


# ────────────────────────────────────────────────────────────────────────
# Happy-path acquire
# ────────────────────────────────────────────────────────────────────────


class TestAcquireBurst:
    def test_initial_burst_is_free(self, bucket: AdaptiveTokenBucket) -> None:
        """First BURST calls should be instant (bucket starts full)."""
        for _ in range(5):  # burst = 5
            wait = bucket.acquire()
            assert wait == 0.0

    def test_sixth_call_waits(self, bucket: AdaptiveTokenBucket, clock: FakeClock) -> None:
        # Drain burst
        for _ in range(5):
            bucket.acquire()
        # Next call needs to wait 1/15 s
        before = clock.now()
        wait = bucket.acquire()
        after = clock.now()
        assert wait == pytest.approx(1.0 / 15.0)
        assert after - before == pytest.approx(1.0 / 15.0)

    def test_steady_state_throughput(self, bucket: AdaptiveTokenBucket, clock: FakeClock) -> None:
        """After burst, sustained throughput = initial_rps."""
        # Drain burst
        for _ in range(5):
            bucket.acquire()
        # Next 15 calls should take exactly 1 second total
        start = clock.now()
        for _ in range(15):
            bucket.acquire()
        elapsed = clock.now() - start
        assert elapsed == pytest.approx(1.0, abs=0.05)

    def test_partial_token_wait_is_remaining_fraction(self, clock: FakeClock) -> None:
        """A HALF-refilled bucket waits only for the missing half.

        2 r/s, burst 1: drain the burst, refill 0.25 s → 0.5 token; the
        wait must be exactly (1.0 − 0.5) / 2 = 0.25 s (all exact binary
        floats). Kills the `needed = 1.0 - tokens` → `1.0 + tokens`
        mutant, which is INVISIBLE at tokens == 0 where every other
        test sits (DEC-MUTATION-RUNTIME-001).
        """
        b = AdaptiveTokenBucket(
            initial_rps=2.0,
            downshift_rps=1.0,
            burst=1,
            clock=clock.now,
            sleep=clock.sleep,
        )
        assert b.acquire() == 0.0  # the free burst token
        clock.advance(0.25)  # 0.25 s × 2 r/s = exactly 0.5 token
        assert b.acquire() == 0.25


# ────────────────────────────────────────────────────────────────────────
# Micro-wait loop arithmetic (DEC-MUTATION-RUNTIME-001)
#
# Each test injects a sleep that UNDER-delivers wall-clock time, so the
# post-sleep refill lands a hair short of one token and the micro-wait
# loop becomes observable. All clocks are fake (zero real sleeping) and
# every path is bounded by _ACQUIRE_LOOP_CAP, so a mutated loop can
# never hang the suite — the observable (return value / sleep count /
# leftover tokens) is asserted immediately after the bounded acquire.
# ────────────────────────────────────────────────────────────────────────


class TestAcquireMicroWaitLoop:
    def _bucket(
        self,
        clock: FakeClock,
        sleep: Callable[[float], None],
    ) -> AdaptiveTokenBucket:
        return AdaptiveTokenBucket(
            initial_rps=1.0,
            downshift_rps=0.5,
            burst=1,
            start_empty=True,
            clock=clock.now,
            sleep=sleep,
        )

    def test_exact_eps_boundary_consumes_without_micro_wait(self) -> None:
        """tokens == 1.0 − eps EXACTLY must consume, not micro-wait.

        The clock starts at 0.0 and the sleep undershoots by exactly
        1e-9, so the refill leaves tokens at the very float the loop
        guard compares against (1 r/s ⇒ tokens ≡ elapsed). Pins the
        strict `<` in `while tokens < 1.0 - eps` against the `<=` flip,
        which would burn ≥ 1 extra micro-wait and return > 1.0.
        """
        clock = FakeClock(start=0.0)

        def undershoot_sleep(seconds: float) -> None:
            clock.advance(seconds - 1e-9)

        b = self._bucket(clock, undershoot_sleep)
        assert b.acquire() == 1.0

    def test_loop_counter_increments_by_exactly_one(self) -> None:
        """The micro-wait counter advances by exactly +1 per iteration.

        The sleep callable HARD-STOPS after cap+3 micro-waits, so this is
        the one cap-path test that cannot hang even when the loop bound
        itself is broken: under the ``iterations += 1`` → ``= 1`` / ``-= 1``
        mutants the safety net is unreachable and the guard trips at call
        cap+4 in microseconds (previously those two mutants could only die
        by mutmut TIMEOUT — a red, slow verdict — because every cap test
        relied on the cap for termination, DEC-MUTATION-RUNTIME-001). On
        real code the cap fires first: exactly 1 + _ACQUIRE_LOOP_CAP sleeps.
        MUST run before test_eps_drift_floor_pinned_at_1e_9 (the first
        hang-capable test) so the `-x` mutation runner fails fast.
        """
        clock = FakeClock(start=0.0)
        sleeps: list[float] = []

        def bounded_undershoot_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) > 3 + _ACQUIRE_LOOP_CAP:
                raise AssertionError(
                    "micro-wait loop escaped _ACQUIRE_LOOP_CAP — the "
                    "iteration counter is not incrementing by +1"
                )
            clock.advance(seconds - 1.5e-9)

        b = self._bucket(clock, bounded_undershoot_sleep)
        b.acquire()
        assert len(sleeps) == 1 + _ACQUIRE_LOOP_CAP

    def test_eps_drift_floor_pinned_at_1e_9(self) -> None:
        """eps == 1e-9 byte-exact, via a deficit BETWEEN 1e-9 and 2e-9.

        The sleep undershoots by 1.5e-9, so tokens land strictly inside
        (1.0 − 2e-9, 1.0 − 1e-9): with eps = 1e-9 the micro-wait loop
        runs to the cap (clock is frozen at nanoscale) and the wait
        exceeds the ideal 1.0 s; under the `eps → 2e-09` mutant the
        guard is already satisfied and acquire returns exactly 1.0
        after a single sleep.
        """
        clock = FakeClock(start=0.0)
        sleeps: list[float] = []

        def undershoot_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds - 1.5e-9)

        b = self._bucket(clock, undershoot_sleep)
        wait = b.acquire()
        assert wait > 1.0
        assert len(sleeps) == 1 + _ACQUIRE_LOOP_CAP

    def test_micro_wait_tops_up_to_exactly_one_token(self, clock: FakeClock) -> None:
        """One micro-wait iteration, byte-exact wait accounting.

        2 r/s, burst 1, start empty: the first sleep (0.5 s requested)
        delivers only half, leaving 0.5 token; ONE micro-wait of
        exactly (1.0 − 0.5) / 2 = 0.25 s tops the bucket up. Pins
        `more_wait = (1.0 - tokens) / rps` against the `2.0 - tokens` /
        `1.0 + tokens` mutants (both would request 0.75 s) and
        `wait_seconds += more_wait` against the `=` overwrite (which
        would report 0.25 instead of 0.5 + 0.25).
        """
        calls: list[float] = []

        def first_sleep_short(seconds: float) -> None:
            calls.append(seconds)
            # First sleep under-delivers half; later sleeps deliver fully.
            clock.advance(seconds / 2.0 if len(calls) == 1 else seconds)

        b = AdaptiveTokenBucket(
            initial_rps=2.0,
            downshift_rps=1.0,
            burst=1,
            start_empty=True,
            clock=clock.now,
            sleep=first_sleep_short,
        )
        assert b.acquire() == 0.75
        assert calls == [0.5, 0.25]

    def test_loop_cap_force_grant_leaves_zero_tokens(self) -> None:
        """The cap's force-grant sets tokens to exactly 1.0 — not more.

        On a frozen clock the capped acquire consumes its force-granted
        token and must leave the bucket at exactly 0.0 (a `1.0 → 2.0`
        force-grant would leak a free extra token over the rate cap,
        violating CLAUDE.md §2 rule 6).
        """
        clock = FakeClock()
        sleeps: list[float] = []
        b = AdaptiveTokenBucket(
            initial_rps=15.0,
            downshift_rps=10.0,
            clock=clock.now,
            sleep=sleeps.append,  # records but NEVER advances the clock
            start_empty=True,
        )
        b.acquire()
        assert b.available_tokens() == 0.0


# ────────────────────────────────────────────────────────────────────────
# Downshift on consecutive 429s
# ────────────────────────────────────────────────────────────────────────


class TestDownshift:
    def test_single_429_does_not_downshift(self, bucket: AdaptiveTokenBucket) -> None:
        event = bucket.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "rate_limit_hit"
        assert event.from_rps == 15.0
        assert bucket.current_rps() == 15.0

    def test_two_429s_do_not_downshift(self, bucket: AdaptiveTokenBucket) -> None:
        for _ in range(2):
            event = bucket.record_rate_limit_hit()
            assert event is not None
            assert event.kind == "rate_limit_hit"
        assert bucket.current_rps() == 15.0

    def test_three_429s_trigger_downshift(self, bucket: AdaptiveTokenBucket) -> None:
        events = [bucket.record_rate_limit_hit() for _ in range(3)]
        assert events[2] is not None
        assert events[2].kind == "downshift"
        assert events[2].from_rps == 15.0
        assert events[2].to_rps == 10.0
        assert events[2].recent_429_count == 3
        assert bucket.current_rps() == 10.0

    def test_fourth_429_after_downshift_stays_downshifted(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        for _ in range(3):
            bucket.record_rate_limit_hit()
        assert bucket.current_rps() == 10.0
        # A 4th hit — we're already downshifted, no new downshift event
        event = bucket.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "rate_limit_hit"
        assert bucket.current_rps() == 10.0

    def test_old_429s_evicted_from_window(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Two 429s → wait past the 60s window → two more 429s: no trigger."""
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        clock.advance(61.0)  # Past the window
        # The first two are now out of window
        event1 = bucket.record_rate_limit_hit()
        event2 = bucket.record_rate_limit_hit()
        assert event1 is not None
        assert event1.kind == "rate_limit_hit"
        assert event2 is not None
        assert event2.kind == "rate_limit_hit"
        assert bucket.current_rps() == 15.0

    def test_three_within_window_still_triggers_after_eviction(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Two at t=0, advance 30s (still in window), one more → trigger."""
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        clock.advance(30.0)
        event = bucket.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "downshift"

    def test_hit_exactly_at_window_edge_still_counts(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """A 429 EXACTLY `downshift_window` seconds old is still in-window.

        Eviction is strict (`hit < cutoff`, rate_limiter.py): two hits at
        t=1000 plus one at t=1060 (cutoff exactly 1000.0 — all exact
        floats) make THREE in-window hits → downshift. The `<` → `<=`
        eviction flip would evict both edge hits and see only one
        (DEC-MUTATION-RUNTIME-001 boundary pin).
        """
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        clock.advance(60.0)
        event = bucket.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "downshift"
        assert event.recent_429_count == 3

    def test_downshift_gate_tolerance_pinned_at_1e_6(self, clock: FakeClock) -> None:
        """The at-full-rate gate's rel_tol is exactly 1e-6.

        A bucket whose two rates differ by a RELATIVE 1.5e-6 sits between
        rel_tol 1e-6 (not close → no re-downshift) and the 2e-06 mutant
        (close → spurious second downshift event): after downshifting,
        a 4th 429 must report plain `rate_limit_hit`
        (DEC-MUTATION-RUNTIME-001).
        """
        b = AdaptiveTokenBucket(
            initial_rps=1.0,
            downshift_rps=1.0 - 1.5e-6,
            downshift_trigger=3,
            downshift_window=60.0,
            upshift_recovery=120.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
        )
        for _ in range(3):
            b.record_rate_limit_hit()
        assert b.current_rps() == 1.0 - 1.5e-6  # downshifted
        event = b.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "rate_limit_hit"  # NOT a second downshift


# ────────────────────────────────────────────────────────────────────────
# Effective rate after downshift
# ────────────────────────────────────────────────────────────────────────


class TestDownshiftedThroughput:
    def test_effective_rate_drops_to_10_rps(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        # Drain burst first
        for _ in range(5):
            bucket.acquire()
        # Downshift
        for _ in range(3):
            bucket.record_rate_limit_hit()
        assert bucket.current_rps() == 10.0

        # Wait for burst to refill at the new rate
        clock.advance(10.0)  # 10s * 10 rps = 100 tokens, clamped at burst=5
        start = clock.now()
        for _ in range(10):  # Drain the refilled burst + a few steady
            bucket.acquire()
        elapsed = clock.now() - start
        # Burst (5) free + 5 at 10 rps = 0.5s
        assert elapsed == pytest.approx(0.5, abs=0.05)


# ────────────────────────────────────────────────────────────────────────
# Upshift — recovery back to initial_rps after a clean window
# (DEC-ORCH-STRICT-GATES-001 standalone-gap closure: the upshift arm
# of _maybe_upshift_locked + the pending_events drain were exercised
# only by the aggregate --cov=brutex run before this class.)
# ────────────────────────────────────────────────────────────────────────


class TestUpshift:
    def _downshift(self, bucket: AdaptiveTokenBucket) -> None:
        """Drive the bucket into the downshifted state via 3 quick 429s."""
        for _ in range(3):
            bucket.record_rate_limit_hit()
        assert bucket.current_rps() == 10.0

    def test_upshift_gated_on_upshift_recovery_not_downshift_window(self, clock: FakeClock) -> None:
        """The upshift gate is :attr:`upshift_recovery`, NOT
        :attr:`downshift_window` (DEC-ORCH-AUDIT-RATE-HARDEN-001 regression).

        With ``downshift_window=60`` and ``upshift_recovery=120``, the
        previously-shipped (defective) code gated the upshift on the 60 s
        window — so it recovered ~2x faster than the documented 120 s. This
        pins the corrected contract: after a downshift the bucket must STILL
        be downshifted at 61 s (past the 60 s window, short of recovery) and
        only restore initial_rps once the full 120 s recovery has elapsed.
        """
        bucket = AdaptiveTokenBucket(
            initial_rps=15.0,
            downshift_rps=10.0,
            downshift_trigger=3,
            downshift_window=60.0,
            upshift_recovery=120.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
        )
        self._downshift(bucket)
        # 61 s: past the 60 s downshift_window but well short of the 120 s
        # recovery — the OLD code upshifted here; the fixed code must HOLD.
        clock.advance(61.0)
        bucket.acquire()
        assert bucket.current_rps() == 10.0
        assert bucket.pending_events() == []
        # Cross the 120 s recovery threshold (61 + 60 = 121 total) — now it
        # upshifts back to initial_rps.
        clock.advance(60.0)
        bucket.acquire()
        assert bucket.current_rps() == 15.0

    def test_clean_window_upshifts_back_to_initial(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Once a full ``upshift_recovery`` window is clean, acquire
        restores initial_rps.

        Also covers the upshift event construction in
        ``_maybe_upshift_locked``. The gate is keyed on
        :attr:`upshift_recovery` (=120 s), NOT the shorter
        :attr:`downshift_window` (=60 s) used only to count 429s for the
        downshift trigger (Golden Rule #6: never recover faster than
        specified).
        """
        self._downshift(bucket)
        # Move past the 120s recovery window so the last 429 ages out.
        clock.advance(121.0)
        bucket.acquire()
        assert bucket.current_rps() == 15.0

    def test_upshift_event_drained_via_pending_events(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """The upshift event lands in the batch queue and drains ONCE.

        Covers the pending_events drain (rate_limiter.py:428-431) — the
        upshift arm is the only producer of batched events today.
        """
        self._downshift(bucket)
        clock.advance(121.0)  # past the 120s upshift_recovery window
        bucket.acquire()

        events = bucket.pending_events()
        assert len(events) == 1
        event = events[0]
        assert event.kind == "upshift"
        assert event.from_rps == 10.0
        assert event.to_rps == 15.0
        assert event.recent_429_count == 0
        # Drained — a second read returns nothing.
        assert bucket.pending_events() == []

    def test_429_still_in_window_blocks_upshift(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """A 429 still inside the recovery window keeps the lower rate."""
        self._downshift(bucket)
        clock.advance(10.0)  # last 429 only 10s ago — well within 120s recovery
        bucket.acquire()
        assert bucket.current_rps() == 10.0
        assert bucket.pending_events() == []

    def test_clock_anomaly_now_before_last_success_blocks_upshift(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Defensive guard: ``now < last_success_at`` defers the upshift.

        Unreachable under a true monotonic clock — pins the anomaly guard
        in ``_maybe_upshift_locked`` so a backwards-stepping injected clock
        can never upshift early, even AFTER the recovery window has
        nominally elapsed.
        """
        self._downshift(bucket)
        clock.advance(300.0)  # past the 120s recovery window (last 429 ages out)
        bucket.record_success()  # last_success_at = t+300
        clock.advance(-90.0)  # rewind: now (t+210) < last_success (t+300)
        bucket.acquire()  # recovery satisfied but the anomaly guard holds
        assert bucket.current_rps() == 10.0
        assert bucket.pending_events() == []

    def test_429_just_before_recovery_edge_blocks_upshift(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """A 429 just SHORT of ``upshift_recovery`` still blocks.

        The recovery gate is strict-less-than (`now - last_429 < recovery`):
        the last 429 at t=1000 with the acquire at t=1119 (119 s elapsed,
        recovery 120 s) is still inside the window, so the rate must HOLD
        at 10 — the gate must NOT recover one tick early. Pins the gate on
        :attr:`upshift_recovery` (Golden Rule #6: never recover faster than
        specified) and kills the `< upshift_recovery` → `< downshift_window`
        regression that would upshift at 61 s.
        """
        self._downshift(bucket)
        clock.advance(119.0)  # 119 < 120 recovery → still blocked
        bucket.acquire()
        assert bucket.current_rps() == 10.0
        assert bucket.pending_events() == []

    def test_429_exactly_at_recovery_edge_upshifts(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """At EXACTLY ``upshift_recovery`` seconds the bucket upshifts.

        The recovery gate is strict-less-than (`now - last_429 < recovery`):
        the last 429 at t=1000 with the acquire at t=1120 gives elapsed
        exactly 120.0, so `120.0 < 120.0` is False → upshift fires. Pins
        the boundary so the `<` → `<=` flip (which would defer the
        on-the-dot recovery one tick) is killed.
        """
        self._downshift(bucket)
        clock.advance(120.0)  # exactly at the recovery edge → upshift
        bucket.acquire()
        assert bucket.current_rps() == 15.0

    def test_upshift_when_now_equals_last_success(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """`now == last_success_at` exactly must still upshift.

        The anomaly guard is strict (`now - last_success_at < 0`): a
        success recorded at the very same fake-clock instant as the
        acquire gives a 0.0 difference, which is NOT an anomaly. Pins
        the rate_limiter.py guard against both the `< 0` → `<= 0` and
        `< 0` → `< 1` flips (each would defer this upshift).
        """
        self._downshift(bucket)
        clock.advance(121.0)  # past the 120s upshift_recovery window
        bucket.record_success()  # last_success_at == now exactly
        bucket.acquire()
        assert bucket.current_rps() == 15.0

    def test_small_positive_last_success_stamp_allows_upshift(self) -> None:
        """`last_success_at` in (0, 1] is a VALID stamp — upshift runs.

        Mirror of the 0.0-stamp guard test: a monotonic clock starting
        at 0.5 stamps last_success_at = 0.5 at construction, which must
        NOT be treated as "never succeeded" — pins `last_success_at <= 0`
        against the `<= 1` drift.
        """
        clock = FakeClock(start=0.5)
        bucket = AdaptiveTokenBucket(
            initial_rps=15.0,
            downshift_rps=10.0,
            downshift_trigger=3,
            downshift_window=60.0,
            upshift_recovery=120.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
        )
        for _ in range(3):
            bucket.record_rate_limit_hit()
        assert bucket.current_rps() == 10.0
        clock.advance(121.0)  # past the 120s upshift_recovery window
        bucket.acquire()
        assert bucket.current_rps() == 15.0

    def test_upshift_gate_tolerance_pinned_at_1e_6(self, clock: FakeClock) -> None:
        """The already-at-full-rate early-return rel_tol is exactly 1e-6.

        With a relative 1.5e-6 gap between the two rates the gate must
        see "NOT at full rate" and upshift after a clean window; the
        2e-06 mutant deems the rates equal and silently never recovers
        (DEC-MUTATION-RUNTIME-001).
        """
        b = AdaptiveTokenBucket(
            initial_rps=1.0,
            downshift_rps=1.0 - 1.5e-6,
            downshift_trigger=3,
            downshift_window=60.0,
            upshift_recovery=120.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
        )
        for _ in range(3):
            b.record_rate_limit_hit()
        assert b.current_rps() == 1.0 - 1.5e-6
        clock.advance(121.0)  # past the 120s upshift_recovery window
        b.acquire()
        assert b.current_rps() == 1.0  # upshifted: the gap exceeds 1e-6

    def test_nonpositive_last_success_stamp_blocks_upshift(self) -> None:
        """Defensive guard: ``last_success_at <= 0`` defers the upshift.

        A monotonic clock CAN legitimately start at 0.0 — the
        ``_maybe_upshift_locked`` ``last_success_at <= 0`` guard then waits
        for one recorded success before trusting the recovery window, even
        once the recovery window has elapsed.
        """
        clock = FakeClock(start=0.0)  # construction stamps last_success_at=0.0
        bucket = AdaptiveTokenBucket(
            initial_rps=15.0,
            downshift_rps=10.0,
            downshift_trigger=3,
            downshift_window=60.0,
            upshift_recovery=120.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
        )
        for _ in range(3):
            bucket.record_rate_limit_hit()
        assert bucket.current_rps() == 10.0
        clock.advance(121.0)  # recovery satisfied, but last_success_at == 0.0 blocks
        bucket.acquire()
        assert bucket.current_rps() == 10.0
        assert bucket.pending_events() == []


# ────────────────────────────────────────────────────────────────────────
# Stats — dashboard hooks
# ────────────────────────────────────────────────────────────────────────


class TestStats:
    def test_current_rps_after_construction(self, bucket: AdaptiveTokenBucket) -> None:
        assert bucket.current_rps() == 15.0

    def test_available_tokens_starts_at_burst(self, bucket: AdaptiveTokenBucket) -> None:
        assert bucket.available_tokens() == 5.0

    def test_available_tokens_after_acquire(self, bucket: AdaptiveTokenBucket) -> None:
        bucket.acquire()
        # 5 - 1 = 4 (no time passed, no refill)
        # But the refill helper runs on every check, so a micro refill may occur.
        remaining = bucket.available_tokens()
        assert 3.9 <= remaining <= 5.0

    def test_available_tokens_clamped_to_burst(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Tokens should never exceed burst capacity even with long waits."""
        clock.advance(1000.0)
        assert bucket.available_tokens() == 5.0

    def test_recent_429_count_starts_at_zero(self, bucket: AdaptiveTokenBucket) -> None:
        assert bucket.recent_429_count() == 0

    def test_recent_429_count_tracks_hits(self, bucket: AdaptiveTokenBucket) -> None:
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        assert bucket.recent_429_count() == 2

    def test_recent_429_count_evicts(self, bucket: AdaptiveTokenBucket, clock: FakeClock) -> None:
        bucket.record_rate_limit_hit()
        clock.advance(61.0)
        assert bucket.recent_429_count() == 0

    def test_recent_429_count_keeps_hit_exactly_at_window_edge(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """A hit EXACTLY `downshift_window` seconds old still counts.

        The dashboard counter evicts strictly (`hit < cutoff`): hit at
        t=1000, read at t=1060 → cutoff exactly 1000.0 → kept. Pins the
        eviction `<` against the `<=` flip
        (DEC-MUTATION-RUNTIME-001 boundary pin).
        """
        bucket.record_rate_limit_hit()
        clock.advance(60.0)
        assert bucket.recent_429_count() == 1

    def test_subsecond_elapsed_refill_pinned(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """A 0.25 s elapsed refills exactly 0.25 × 15 = 3.75 tokens.

        Pins the `_refill_locked` early-return threshold (`elapsed <= 0`)
        against the `0 → 1` flip, which would skip every sub-second
        refill — the dominant case at 3-15 r/s. Named in the line-scoped
        `pragma: no mutate` on that guard (DEC-MUTATION-RUNTIME-001).
        """
        for _ in range(5):
            bucket.acquire()  # drain the burst; tokens == 0.0 exactly
        clock.advance(0.25)
        assert bucket.available_tokens() == 3.75


# ────────────────────────────────────────────────────────────────────────
# Never-drop guarantee
# ────────────────────────────────────────────────────────────────────────


class TestNeverDropGuarantee:
    """Per FR-ING-017: the limiter NEVER drops a request silently.

    Every acquire() call eventually returns. The wait time may be
    long under extreme rate limiting but it's never infinite and
    never raises an exception.
    """

    def test_acquire_always_returns_non_negative(self, bucket: AdaptiveTokenBucket) -> None:
        for _ in range(20):
            wait = bucket.acquire()
            assert wait >= 0.0

    def test_acquire_after_downshift_still_works(self, bucket: AdaptiveTokenBucket) -> None:
        for _ in range(3):
            bucket.record_rate_limit_hit()
        for _ in range(10):
            wait = bucket.acquire()
            assert wait >= 0.0

    def test_rate_limit_hits_never_raise(self, bucket: AdaptiveTokenBucket) -> None:
        """Recording 429s should never raise, regardless of count."""
        for _ in range(100):
            event = bucket.record_rate_limit_hit()
            assert event is not None  # Always returns an event

    def test_record_success_never_raises(self, bucket: AdaptiveTokenBucket) -> None:
        bucket.record_success()
        bucket.record_success()
        # Still at initial rps
        assert bucket.current_rps() == 15.0

    def test_acquire_terminates_even_on_frozen_clock(self) -> None:
        """The _ACQUIRE_LOOP_CAP safety net guarantees termination.

        A clock whose sleep never advances time (the worst-case
        injected-fake round-off scenario the cap defends against) would
        otherwise spin the refill micro-wait loop forever; the cap
        force-grants the token after 16 iterations
        (rate_limiter.py:286-287, DEC-ORCH-STRICT-GATES-001 gap closure).
        """
        clock = FakeClock()
        sleeps: list[float] = []
        bucket = AdaptiveTokenBucket(
            initial_rps=15.0,
            downshift_rps=10.0,
            clock=clock.now,
            sleep=sleeps.append,  # records but NEVER advances the clock
            start_empty=True,
        )
        wait = bucket.acquire()  # must return, not hang
        assert wait > 0.0
        # One initial wait + one micro-wait per capped loop iteration.
        assert len(sleeps) == 1 + _ACQUIRE_LOOP_CAP


# ────────────────────────────────────────────────────────────────────────
# Thread safety sanity check (no deadlock, correct final state)
# ────────────────────────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_acquires_serialize(self) -> None:
        """Multiple threads calling acquire() should not deadlock.

        Uses REAL time.monotonic and real sleep here because we want
        to exercise the lock under actual contention.
        """
        import threading
        import time

        bucket = AdaptiveTokenBucket(
            initial_rps=100.0,  # Fast for test
            downshift_rps=50.0,
            burst=10,
        )

        results: list[float] = []

        def worker() -> None:
            for _ in range(3):
                wait = bucket.acquire()
                results.append(wait)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        elapsed = time.monotonic() - start

        # Should complete in well under 5 seconds
        assert elapsed < 5.0
        # 15 acquires total, burst=10 free, 5 at 100 rps = ~50ms
        assert len(results) == 15


# ────────────────────────────────────────────────────────────────────────
# Bucket ↔ QuotaGate integration (DEC-P0-030, PR #50c; retyped onto the
# structural QuotaGate Protocol by DEC-ORCH-DEAD-SCAFFOLD-DELETE-001 —
# the deleted DailyQuotaTracker is replaced by an in-memory fake gate)
# ────────────────────────────────────────────────────────────────────────


class _FakeQuotaGate:
    """A minimal in-memory :class:`QuotaGate` double.

    Exactly the deleted ``DailyQuotaTracker``'s consumed surface: the
    bucket calls ONLY ``acquire_or_raise()`` (rate_limiter.py's quota-gate
    branch); the count/remaining reads let the tests assert increments.
    """

    def __init__(self, *, daily_limit: int) -> None:
        self.daily_limit = daily_limit
        self._used = 0

    def acquire_or_raise(self) -> int:
        from brutex.orchestrator.quota_tracker import DailyQuotaExceeded

        if self._used >= self.daily_limit:
            raise DailyQuotaExceeded(f"daily_limit={self.daily_limit} already reached")
        self._used += 1
        return self._used

    def current_count(self) -> int:
        return self._used

    def remaining(self) -> int:
        return max(0, self.daily_limit - self._used)


class TestQuotaTrackerIntegration:
    """Verify the optional ``quota_tracker`` field composes correctly.

    Closes Agent 1 §G None-safety, Agent 3 A1/A2/B1/B2 ordering, Agent 2
    §B no-silent-bypass (via explicit test that None is the production
    bucket-only path).
    """

    def test_quota_tracker_none_is_legacy_path(self) -> None:
        """When quota_tracker is None, bucket behaves identically to
        pre-PR-#50c (no quota call). This is the test-fixture path.
        """
        clock = FakeClock()
        sleep = clock.sleep
        bucket = AdaptiveTokenBucket(
            initial_rps=10.0,
            downshift_rps=5.0,
            burst=1,
            clock=clock.now,
            sleep=sleep,
            # quota_tracker omitted — defaults to None
        )
        assert bucket.quota_tracker is None
        wait = bucket.acquire()
        assert wait == 0.0  # first token from burst, no wait

    def test_quota_tracker_set_increments_on_acquire(self) -> None:
        """When quota_tracker is set, every successful bucket acquire
        triggers exactly one quota increment.
        """
        clock = FakeClock()
        gate = _FakeQuotaGate(daily_limit=10)
        bucket = AdaptiveTokenBucket(
            initial_rps=10.0,
            downshift_rps=5.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
            quota_tracker=gate,
        )
        assert gate.current_count() == 0
        bucket.acquire()
        assert gate.current_count() == 1
        bucket.acquire()
        bucket.acquire()
        assert gate.current_count() == 3

    def test_quota_exceeded_bubbles_up_from_acquire(self) -> None:
        """When the quota gate raises DailyQuotaExceeded, it bubbles
        up from bucket.acquire() — the halt chain maps this to a
        pull-wide stop (no auto-retry).
        """
        from brutex.orchestrator.quota_tracker import DailyQuotaExceeded

        clock = FakeClock()
        bucket = AdaptiveTokenBucket(
            initial_rps=10.0,
            downshift_rps=5.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
            quota_tracker=_FakeQuotaGate(daily_limit=3),
        )
        bucket.acquire()
        bucket.acquire()
        bucket.acquire()
        with pytest.raises(DailyQuotaExceeded):
            bucket.acquire()

    def test_record_success_unchanged_on_quota_raise(self) -> None:
        """Agent 3 A2/I1: bucket's _last_success_at MUST remain
        unchanged when quota raises. The adapter caller controls
        record_success(); the bucket itself does not call it inside
        acquire().
        """
        from brutex.orchestrator.quota_tracker import DailyQuotaExceeded

        clock = FakeClock()
        bucket = AdaptiveTokenBucket(
            initial_rps=10.0,
            downshift_rps=5.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
            quota_tracker=_FakeQuotaGate(daily_limit=1),
        )
        bucket.acquire()
        success_at_before = bucket._last_success_at
        with pytest.raises(DailyQuotaExceeded):
            bucket.acquire()
        # _last_success_at MUST be unchanged — bucket.acquire() does not
        # update it on quota failure (adapter caller controls success
        # recording via the separate record_success() method).
        assert bucket._last_success_at == success_at_before


class TestStartEmpty:
    """``start_empty`` ease-in (DEC-DHAN-RATE-HARD-CAP-001): the bucket holds 0
    tokens at t=0 instead of ``burst``, so the FIRST acquire waits one full
    ``1/rate`` interval — no opening burst over the rate."""

    def test_default_starts_full_first_call_immediate(self, clock: FakeClock) -> None:
        """Legacy default (start_empty=False): a full bucket → first token is free."""
        b = AdaptiveTokenBucket(
            initial_rps=5.0,
            downshift_rps=2.5,
            burst=1,
            clock=clock.now,
            sleep=clock.sleep,
        )
        assert b.acquire() == 0.0

    def test_start_empty_first_call_waits_one_interval(self, clock: FakeClock) -> None:
        """start_empty=True: a 0-token bucket → the first call is paced at 1/rate."""
        b = AdaptiveTokenBucket(
            initial_rps=5.0,
            downshift_rps=2.5,
            burst=1,
            start_empty=True,
            clock=clock.now,
            sleep=clock.sleep,
        )
        assert b.acquire() == 1.0 / 5.0


class TestSetRate:
    """``set_rate`` in-place rate change (DEC-DHAN-RATE-SINGLE-BUCKET-001): the
    Dhan controller mutates its ONE shared bucket instead of swapping in a fresh
    one, so a 5→4→3 step can never create a second concurrent token stream (the
    ``rate.breach rps_1s=11`` opening transient)."""

    def _bucket(self, clock: FakeClock) -> AdaptiveTokenBucket:
        return AdaptiveTokenBucket(
            initial_rps=5.0,
            downshift_rps=2.5,
            burst=1,
            start_empty=True,
            clock=clock.now,
            sleep=clock.sleep,
        )

    def test_changes_current_rps(self, clock: FakeClock) -> None:
        b = self._bucket(clock)
        b.set_rate(4.0)
        assert b.current_rps() == 4.0

    def test_rejects_non_positive(self, clock: FakeClock) -> None:
        b = self._bucket(clock)
        with pytest.raises(RateLimiterError, match="must be > 0"):
            b.set_rate(0.0)

    def test_drains_tokens_to_empty(self, clock: FakeClock) -> None:
        # A FULL bucket (start_empty=False, burst=2) holds 2 tokens; set_rate must
        # drain it to 0 so a stepdown can't release a stale higher-rate token.
        b = AdaptiveTokenBucket(
            initial_rps=5.0, downshift_rps=2.5, burst=2, clock=clock.now, sleep=clock.sleep
        )
        assert b.available_tokens() == 2.0
        b.set_rate(4.0)
        assert b.available_tokens() == 0.0  # drained — no free token at the new rate

    def test_first_call_after_downshift_waits_full_new_interval(self, clock: FakeClock) -> None:
        # The drained bucket + 0 elapsed since the change → the next acquire waits a
        # full 1/new_rps, exactly mirroring the prior start_empty rebuild.
        b = self._bucket(clock)
        b.set_rate(4.0)
        assert b.acquire() == 1.0 / 4.0

    def test_set_rate_of_exactly_one_accepted(self, clock: FakeClock) -> None:
        """Boundary pin: `new_rps <= 0` must NOT drift to `<= 1` — the
        controller's lowest legal step stays settable
        (DEC-MUTATION-RUNTIME-001)."""
        b = self._bucket(clock)
        b.set_rate(1.0)
        assert b.current_rps() == 1.0

    def test_set_rate_zero_message_byte_exact(self, clock: FakeClock) -> None:
        """`match=` is substring-only; the guard message is pinned with
        `==` so an XX-wrapped mutant cannot hide."""
        b = self._bucket(clock)
        with pytest.raises(RateLimiterError) as exc:
            b.set_rate(0.0)
        assert str(exc.value) == "new_rps must be > 0, got 0.0"

    def test_set_rate_rederives_downshift_rps_as_half(self, clock: FakeClock) -> None:
        # set_rate moves BOTH thresholds with the new rate: initial_rps
        # becomes the rate itself and the (dormant) downshift_rps is
        # re-derived as exactly new_rps / 2.0 — pinned byte-exact so the
        # `/ 2.0` → `* 2.0` / `/ 3.0` / `None` mutants all surface.
        b = self._bucket(clock)
        b.set_rate(4.0)
        assert b.initial_rps == 4.0
        assert b.downshift_rps == 2.0

    def test_internal_upshift_stays_dormant(self, clock: FakeClock) -> None:
        # set_rate moves initial_rps WITH the rate, so the bucket's own
        # _maybe_upshift_locked (which never sees a controller 429) cannot bump the
        # rate back up on the next acquire — the controller stays the sole authority.
        b = self._bucket(clock)
        b.set_rate(2.0)
        b.acquire()
        clock.advance(1000.0)  # well past upshift_recovery
        b.acquire()
        assert b.current_rps() == 2.0  # did NOT auto-recover to the old initial 5.0


# ────────────────────────────────────────────────────────────────────────
# `_last_429_at` sentinel — CI mutation-rehearsal survivor kills
# ────────────────────────────────────────────────────────────────────────


class TestLast429SentinelInvariants:
    """Kill the two CI mutation-rehearsal survivors on the ``_last_429_at``
    no-429-yet sentinel (DEC-MUTATION-RUNTIME-003 rehearsal; both were
    near-equivalent because ``__post_init__`` re-stamps the field and the
    upshift recovery gate is normally unreachable before a downshift):

    * mutant 43 — ``_last_429_at: float = field(init=False)`` → ``= None``
      silently promotes internal runtime state to a constructor parameter
      with a ``None`` default;
    * mutant 70 — ``self._last_429_at = float("-inf")`` → ``= None`` in
      ``__post_init__`` replaces the documented open-gate sentinel with a
      value that would CRASH the recovery gate (``now - None``) on the
      defended never-downshifted path.
    """

    def test_last_429_at_is_runtime_state_not_a_constructor_param(self) -> None:
        """``_last_429_at`` is ``field(init=False)`` with NO default.

        It is internal runtime state stamped only by ``__post_init__`` /
        ``record_rate_limit_hit`` — never caller-settable: a
        caller-preseeded 429 stamp could hold the upshift recovery gate
        open (or shut) independently of real vendor 429s (Golden Rule #6:
        never recover faster than specified).
        """
        by_name = {f.name: f for f in dataclasses.fields(AdaptiveTokenBucket)}
        f = by_name["_last_429_at"]
        assert f.init is False  # no constructor parameter exists
        assert f.default is dataclasses.MISSING
        assert f.default_factory is dataclasses.MISSING

    def test_post_init_stamps_minus_inf_sentinel(self, clock: FakeClock) -> None:
        """After ``__post_init__`` the sentinel is EXACTLY ``float("-inf")``.

        ``-inf`` is the one value that keeps the recovery gate subtraction
        ``now - _last_429_at`` well-defined AND trivially open
        (``>= upshift_recovery``) for every finite monotonic clock — a
        ``None`` sentinel would TypeError, a finite/``+inf`` sentinel
        would wrongly hold the gate shut with no 429 ever seen.
        """
        b = AdaptiveTokenBucket(clock=clock.now, sleep=clock.sleep)
        assert b._last_429_at == float("-inf")
        assert clock.now() - b._last_429_at >= b.upshift_recovery  # gate open, no crash

    def test_no_429_yet_recovery_gate_trivially_open(self, clock: FakeClock) -> None:
        """SAFE direction pinned: below full rate with NO 429 ever recorded,
        ``_maybe_upshift_locked`` must upshift back to ``initial_rps``
        (there is nothing to recover from) — never crash, never wedge
        sub-rate forever. The public API cannot reach this state (every
        downshift stamps a real timestamp; ``set_rate`` moves
        ``initial_rps`` with the rate), so the test forces the defended
        desync directly — the exact scenario the ``-inf`` construction
        sentinel exists for.
        """
        b = AdaptiveTokenBucket(
            initial_rps=15.0,
            downshift_rps=10.0,
            burst=5,
            clock=clock.now,
            sleep=clock.sleep,
        )
        b.record_success()
        b._current_rps = b.downshift_rps  # the "reached without a downshift" desync
        clock.advance(1.0)
        b.acquire()  # recovery gate: now - (-inf) = +inf >= upshift_recovery → open
        assert b.current_rps() == b.initial_rps


# ────────────────────────────────────────────────────────────────────────
# Rust rate-math kernel seam (DEC-RATE-MATH-RUST-001, ADR-0019)
# ────────────────────────────────────────────────────────────────────────


class TestRustRateMathSeam:
    """The flag-gated Rust dispatch: flag arms, kernel resolution, burn-in +
    autorevert latch, and both-arms f64-byte identity."""

    @pytest.fixture(autouse=True)
    def _fresh_seam(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Every test starts and ends with a fresh-process seam view."""
        monkeypatch.delenv(rate_limiter_module.RUST_RATE_MATH_ENV, raising=False)
        rate_limiter_module._reset_rust_rate_math_for_tests()
        yield
        rate_limiter_module._reset_rust_rate_math_for_tests()

    def _arm(self, monkeypatch: pytest.MonkeyPatch, *, burn_in_remaining: int = 16) -> None:
        monkeypatch.setenv(rate_limiter_module.RUST_RATE_MATH_ENV, "1")
        rate_limiter_module._reset_rust_rate_math_for_tests(burn_in_remaining=burn_in_remaining)

    @staticmethod
    def _fake_kernel(
        refill: float = 999.0,
        consume: tuple[float, float] = (999.0, 999.0),
        more_wait: float = 999.0,
        set_rate: tuple[float, float] = (999.0, 999.0),
    ) -> rate_limiter_module._RustRateKernel:
        return rate_limiter_module._RustRateKernel(
            refill_fn=lambda *a: refill,
            consume_fn=lambda *a: consume,
            more_wait_fn=lambda *a: more_wait,
            set_rate_fn=lambda *a: set_rate,
        )

    # ── flag arms ────────────────────────────────────────────────────────

    def test_flag_default_off_resolves_no_kernel(self) -> None:
        assert rate_limiter_module._rust_rate_kernel() is None

    def test_flag_falsy_off_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rate_limiter_module.RUST_RATE_MATH_ENV, "off")
        rate_limiter_module._reset_rust_rate_math_for_tests()
        with capture_logs() as logs:
            assert rate_limiter_module._rust_rate_kernel() is None
        assert logs == []

    def test_flag_junk_warns_once_and_stays_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(rate_limiter_module.RUST_RATE_MATH_ENV, "flase")
        rate_limiter_module._reset_rust_rate_math_for_tests()
        with capture_logs() as logs:
            assert rate_limiter_module._rust_flag_enabled() is False
            assert rate_limiter_module._rust_flag_enabled() is False  # memo: warn once
        assert [r["event"] for r in logs] == ["rate_math.rust_flag_unrecognised"]

    # ── kernel resolution arms ───────────────────────────────────────────

    def test_flag_on_resolves_the_real_kernel_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("fno_math")
        self._arm(monkeypatch)
        kernel = rate_limiter_module._rust_rate_kernel()
        assert kernel is not None
        # Memoized: a second call returns the SAME resolved object.
        assert rate_limiter_module._rust_rate_kernel() is kernel

    def test_wheel_missing_warns_once_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        self._arm(monkeypatch)

        def _no_wheel() -> object:
            raise ImportError("no fno_math")

        monkeypatch.setattr(rate_limiter_module, "_import_fno_math", _no_wheel)
        with capture_logs() as logs:
            assert rate_limiter_module._rust_rate_kernel() is None
        assert [r["event"] for r in logs] == ["rate_math.rust_kernel_unavailable"]
        assert logs[0]["reason"] == "wheel_missing"
        # The bucket still paces from the Python oracle.
        b = AdaptiveTokenBucket(clock=clock.now, sleep=clock.sleep)
        assert b.acquire() == 0.0

    @pytest.mark.parametrize(
        "stale_ns",
        [
            {},
            {"bucket_refill": lambda *a: 0.0},
            {
                "bucket_refill": lambda *a: 0.0,
                "bucket_consume": lambda *a: (0.0, 0.0),
            },
            {
                "bucket_refill": lambda *a: 0.0,
                "bucket_consume": lambda *a: (0.0, 0.0),
                "bucket_more_wait": lambda *a: 0.0,
            },
            {
                "bucket_refill": lambda *a: 0.0,
                "bucket_consume": lambda *a: (0.0, 0.0),
                "bucket_more_wait": lambda *a: 0.0,
                "bucket_set_rate_state": "not-callable",
            },
        ],
        ids=["empty", "refill-only", "no-more-wait", "no-set-rate", "set-rate-not-callable"],
    )
    def test_wheel_stale_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, stale_ns: dict[str, object]
    ) -> None:
        self._arm(monkeypatch)
        monkeypatch.setattr(
            rate_limiter_module, "_import_fno_math", lambda: types.SimpleNamespace(**stale_ns)
        )
        with capture_logs() as logs:
            assert rate_limiter_module._rust_rate_kernel() is None
        assert [r["event"] for r in logs] == ["rate_math.rust_kernel_unavailable"]
        assert logs[0]["reason"] == "wheel_stale"

    # ── dispatch parity (the real kernel) ────────────────────────────────

    def test_flag_on_all_four_surfaces_match_the_python_oracle_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every dispatch surface serves f64-byte-identical values to the
        retained Python expressions — probed DIRECTLY so the retry-wait
        surface (naturally reachable only under a concurrent downshift) is
        exercised too."""
        pytest.importorskip("fno_math")
        self._arm(monkeypatch)
        vectors: list[tuple[tuple[float, ...], tuple[float, ...]]] = []
        for tokens, burst, rps, elapsed in [
            (0.0, 5.0, 3.0, 0.5),
            (4.0, 5.0, 3.0, 1.0),
            (2.0, 5.0, 8.0, 1e-9),
            (0.9999999999999999, 5.0, 1.5, 0.25),
        ]:
            vectors.append(
                (
                    (rate_limiter_module._bucket_refill(tokens, burst, rps, elapsed),),
                    (rate_limiter_module._py_bucket_refill(tokens, burst, rps, elapsed),),
                )
            )
        for tokens, rps in [(1.0, 3.0), (0.25, 3.0), (5.0, 8.0), (1.0 - 1e-12, 1.5)]:
            vectors.append(
                (
                    rate_limiter_module._bucket_consume(tokens, rps),
                    rate_limiter_module._py_bucket_consume(tokens, rps),
                )
            )
        for tokens, rps, eps in [(0.25, 3.0, 1e-9), (1.0 - 1e-12, 3.0, 1e-9), (0.5, 2.0, 0.25)]:
            vectors.append(
                (
                    (rate_limiter_module._bucket_more_wait(tokens, rps, eps),),
                    (rate_limiter_module._py_bucket_more_wait(tokens, rps, eps),),
                )
            )
        for new_rps in [8.0, 3.0, 1.5, 5e-324]:
            vectors.append(
                (
                    rate_limiter_module._bucket_set_rate_state(new_rps),
                    rate_limiter_module._py_bucket_set_rate_state(new_rps),
                )
            )
        for served, oracle in vectors:
            assert rate_limiter_module._f64_bytes(served) == rate_limiter_module._f64_bytes(oracle)
        # None of the double-computes diverged — the kernel is still live.
        assert rate_limiter_module._RUST_DISABLED_REASON[0] is None

    def test_flag_on_bucket_end_to_end_matches_flag_off_twin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full acquire/set_rate/refill sequence produces IDENTICAL waits,
        tokens and rates under both arms (the AIMD ladder untouched)."""
        pytest.importorskip("fno_math")

        def _run() -> list[float]:
            c = FakeClock()
            b = AdaptiveTokenBucket(
                initial_rps=8.0, downshift_rps=4.0, start_empty=True, clock=c.now, sleep=c.sleep
            )
            out: list[float] = []
            out.append(b.acquire())  # empty bucket → real wait
            b.record_success()
            b.set_rate(3.0)  # drain-to-empty + downshift moves
            out.append(b.downshift_rps)
            out.append(b.acquire())
            c.advance(2.0)
            out.append(b.acquire())  # refilled — free token
            out.append(b.available_tokens())
            out.append(b.current_rps())
            return out

        self._arm(monkeypatch)
        rust_arm = _run()
        assert rate_limiter_module._RUST_DISABLED_REASON[0] is None
        monkeypatch.delenv(rate_limiter_module.RUST_RATE_MATH_ENV)
        rate_limiter_module._reset_rust_rate_math_for_tests()
        python_arm = _run()
        assert rate_limiter_module._f64_bytes(tuple(rust_arm)) == rate_limiter_module._f64_bytes(
            tuple(python_arm)
        )

    # ── burn-in + autorevert ─────────────────────────────────────────────

    def test_burn_in_consumes_one_shared_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("fno_math")
        self._arm(monkeypatch, burn_in_remaining=3)
        rate_limiter_module._bucket_refill(0.0, 5.0, 3.0, 0.5)
        rate_limiter_module._bucket_consume(1.0, 3.0)
        assert rate_limiter_module._RUST_BURN_IN_REMAINING[0] == 1
        rate_limiter_module._bucket_more_wait(0.25, 3.0, 1e-9)
        rate_limiter_module._bucket_set_rate_state(8.0)  # exhausted — no decrement below 0
        assert rate_limiter_module._RUST_BURN_IN_REMAINING[0] == 0

    def test_burn_in_mismatch_ships_python_latches_and_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm(monkeypatch)
        rate_limiter_module._set_rust_kernel_for_tests(self._fake_kernel())
        with capture_logs() as logs:
            served = rate_limiter_module._bucket_refill(0.0, 5.0, 3.0, 0.5)
        assert served == rate_limiter_module._py_bucket_refill(0.0, 5.0, 3.0, 0.5)
        assert [r["event"] for r in logs] == ["rate_math.kernel_autoreverted"]
        assert logs[0]["reason"] == "burn_in_mismatch"
        assert logs[0]["surface"] == "bucket_refill"
        assert rate_limiter_module._RUST_DISABLED_REASON[0] == "burn_in_mismatch"
        # Latched: every later call short-circuits to the oracle (no kernel).
        assert rate_limiter_module._rust_rate_kernel() is None
        assert rate_limiter_module._bucket_consume(0.25, 3.0) == (
            rate_limiter_module._py_bucket_consume(0.25, 3.0)
        )
        # The latch logs ONCE — a second disable call is a no-op.
        with capture_logs() as logs:
            rate_limiter_module._disable_rust_kernel("again")
        assert logs == []

    def test_burn_in_signed_zero_mismatch_is_caught_by_the_byte_comparator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-0.0 == 0.0`` under ``==`` — the byte comparator must still latch."""
        self._arm(monkeypatch)
        rate_limiter_module._set_rust_kernel_for_tests(
            self._fake_kernel(set_rate=(-0.0, 4.0))  # oracle drains to +0.0
        )
        with capture_logs() as logs:
            served = rate_limiter_module._bucket_set_rate_state(8.0)
        assert served == (0.0, 4.0)
        assert rate_limiter_module._f64_bytes((served[0],)) == rate_limiter_module._f64_bytes(
            (0.0,)
        )
        assert [r["event"] for r in logs] == ["rate_math.kernel_autoreverted"]

    def test_post_burn_in_rust_value_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the budget exhausted the kernel values serve directly —
        proven with a fake kernel whose values are distinguishable."""
        self._arm(monkeypatch, burn_in_remaining=0)
        rate_limiter_module._set_rust_kernel_for_tests(
            self._fake_kernel(refill=7.0, consume=(6.0, 0.5), more_wait=0.75, set_rate=(0.0, 2.5))
        )
        assert rate_limiter_module._bucket_refill(0.0, 5.0, 3.0, 0.5) == 7.0
        assert rate_limiter_module._bucket_consume(1.0, 3.0) == (6.0, 0.5)
        assert rate_limiter_module._bucket_more_wait(0.25, 3.0, 1e-9) == 0.75
        assert rate_limiter_module._bucket_set_rate_state(8.0) == (0.0, 2.5)

    # ── seam plumbing ────────────────────────────────────────────────────

    def test_reset_seam_restores_a_fresh_view(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._arm(monkeypatch, burn_in_remaining=0)
        rate_limiter_module._set_rust_kernel_for_tests(self._fake_kernel())
        rate_limiter_module._disable_rust_kernel("test")
        rate_limiter_module._reset_rust_rate_math_for_tests()
        assert rate_limiter_module._RUST_DISABLED_REASON[0] is None
        assert (
            rate_limiter_module._RUST_BURN_IN_REMAINING[0]
            == rate_limiter_module.RATE_MATH_BURN_IN_SAMPLES
        )
        assert rate_limiter_module._RUST_KERNEL_RESOLVED[0] is False
