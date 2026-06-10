"""Unit tests for brutex.orchestrator.retry.

Validates the exception-to-Outcome classifier, the exponential backoff
schedule, and the severity helper. No Dhan API imports at module load
time — tests use synthetic exception classes with the same names.
"""

from __future__ import annotations

import random

import pytest
from brutex.orchestrator.retry import (
    BACKOFF_SCHEDULE_SECONDS,
    BROKER_EXCEPTION_CLASSIFICATION,
    DEFAULT_MAX_ATTEMPTS,
    JITTER_FRACTION,
    REQUIRED_EXCEPTION_NAMES,
    RETRYABLE_OUTCOMES,
    UnknownExceptionError,
    backoff_delay,
    classify_exception,
    classify_exception_name,
    outcome_severity,
    retry_call,
)
from brutex.orchestrator.state import Outcome

# ────────────────────────────────────────────────────────────────────────
# Synthetic Dhan API exception classes (name-matched to the real SDK)
# ────────────────────────────────────────────────────────────────────────


class VendorBaseException(Exception):
    pass


class VendorAPIException(VendorBaseException):
    pass


class VendorAPIRateLimitException(VendorAPIException):
    pass


class VendorAPITimeoutException(VendorAPIException):
    pass


class VendorAPINotFoundException(VendorAPIException):
    pass


class VendorAPIBadRequestException(VendorAPIException):
    pass


class VendorAPIAuthenticationException(VendorAPIException):
    pass


class VendorAPIAuthorisationException(VendorAPIException):
    pass


class VendorFeedException(VendorBaseException):
    pass


class VendorFeedConnectionException(VendorFeedException):
    pass


class VendorFeedNotSubscribedException(VendorFeedException):
    pass


# ────────────────────────────────────────────────────────────────────────
# Classification table shape
# ────────────────────────────────────────────────────────────────────────


class TestClassificationTable:
    def test_table_has_all_required_exception_names(self) -> None:
        expected = {
            "VendorBaseException",
            "VendorAPIException",
            "VendorAPIRateLimitException",
            "VendorAPITimeoutException",
            "VendorAPINotFoundException",
            "VendorAPIBadRequestException",
            "VendorAPIAuthenticationException",
            "VendorAPIAuthorisationException",
            "VendorFeedException",
            "VendorFeedConnectionException",
            "VendorFeedNotSubscribedException",
        }
        assert set(BROKER_EXCEPTION_CLASSIFICATION.keys()) == expected
        assert expected == REQUIRED_EXCEPTION_NAMES

    def test_british_authorisation_spelling(self) -> None:
        """The vendor uses British 'Authorisation' — locked."""
        assert "VendorAPIAuthorisationException" in BROKER_EXCEPTION_CLASSIFICATION
        assert "VendorAPIAuthorizationException" not in BROKER_EXCEPTION_CLASSIFICATION

    def test_every_outcome_has_at_least_one_mapping(self) -> None:
        """Every Outcome that can come from an exception must be
        reachable from at least one class name. OK_WITH_DATA and
        OK_EMPTY are not exception outcomes, so they are excluded.
        """
        exception_outcomes = set(BROKER_EXCEPTION_CLASSIFICATION.values())
        # Outcomes that should be in the map
        expected = {
            Outcome.RATE_LIMIT,
            Outcome.TIMEOUT,
            Outcome.GENERIC_API_ERROR,
            Outcome.NOT_FOUND,
            Outcome.BAD_REQUEST,
            Outcome.AUTH_FAILED,
            Outcome.AUTHZ_FAILED,
        }
        assert expected <= exception_outcomes

    def test_ok_outcomes_not_in_map(self) -> None:
        """OK_WITH_DATA and OK_EMPTY are set by the happy path, not
        by exception classification."""
        assert Outcome.OK_WITH_DATA not in BROKER_EXCEPTION_CLASSIFICATION.values()
        assert Outcome.OK_EMPTY not in BROKER_EXCEPTION_CLASSIFICATION.values()


# ────────────────────────────────────────────────────────────────────────
# classify_exception_name
# ────────────────────────────────────────────────────────────────────────


class TestClassifyExceptionName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("VendorAPIRateLimitException", Outcome.RATE_LIMIT),
            ("VendorAPITimeoutException", Outcome.TIMEOUT),
            ("VendorAPIException", Outcome.GENERIC_API_ERROR),
            ("VendorBaseException", Outcome.GENERIC_API_ERROR),
            ("VendorAPINotFoundException", Outcome.NOT_FOUND),
            ("VendorAPIBadRequestException", Outcome.BAD_REQUEST),
            ("VendorAPIAuthenticationException", Outcome.AUTH_FAILED),
            ("VendorAPIAuthorisationException", Outcome.AUTHZ_FAILED),
        ],
    )
    def test_known_names_classify_correctly(self, name: str, expected: Outcome) -> None:
        assert classify_exception_name(name) == expected

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(UnknownExceptionError, match="Unknown Dhan API exception"):
            classify_exception_name("VendorAPITotallyNewException")

    def test_unknown_error_mentions_remediation(self) -> None:
        try:
            classify_exception_name("NotAVendorException")
        except UnknownExceptionError as e:
            msg = str(e)
            assert "BROKER_EXCEPTION_CLASSIFICATION" in msg
            assert "test_orchestrator_retry.py" in msg

    def test_unknown_name_error_message_is_byte_exact(self) -> None:
        """The remediation message is operator-facing — pinned byte-for-byte
        with ``==`` (a substring ``match=`` cannot kill the per-segment
        string mutants; DEC-MUTATION-RUNTIME-001)."""
        with pytest.raises(UnknownExceptionError) as excinfo:
            classify_exception_name("VendorAPITotallyNewException")
        assert str(excinfo.value) == (
            "Unknown Dhan API exception class: 'VendorAPITotallyNewException'. "
            "Add it to BROKER_EXCEPTION_CLASSIFICATION in brutex/orchestrator/retry.py "
            "and re-run tests/unit/test_orchestrator_retry.py."
        )


# ────────────────────────────────────────────────────────────────────────
# classify_exception (instance dispatch via MRO)
# ────────────────────────────────────────────────────────────────────────


class TestClassifyException:
    def test_direct_rate_limit(self) -> None:
        exc = VendorAPIRateLimitException("too many requests")
        assert classify_exception(exc) == Outcome.RATE_LIMIT

    def test_direct_timeout(self) -> None:
        exc = VendorAPITimeoutException("slow")
        assert classify_exception(exc) == Outcome.TIMEOUT

    def test_direct_not_found(self) -> None:
        exc = VendorAPINotFoundException("no such symbol")
        assert classify_exception(exc) == Outcome.NOT_FOUND

    def test_direct_bad_request(self) -> None:
        exc = VendorAPIBadRequestException("bad params")
        assert classify_exception(exc) == Outcome.BAD_REQUEST

    def test_direct_auth_failed(self) -> None:
        exc = VendorAPIAuthenticationException("bad token")
        assert classify_exception(exc) == Outcome.AUTH_FAILED

    def test_direct_authz_failed(self) -> None:
        exc = VendorAPIAuthorisationException("missing scope")
        assert classify_exception(exc) == Outcome.AUTHZ_FAILED

    def test_generic_api_error(self) -> None:
        exc = VendorAPIException("unknown API issue")
        assert classify_exception(exc) == Outcome.GENERIC_API_ERROR

    def test_base_exception_fallback(self) -> None:
        exc = VendorBaseException("something bad")
        assert classify_exception(exc) == Outcome.GENERIC_API_ERROR

    def test_subclass_falls_through_mro(self) -> None:
        """A future subclass of VendorAPIRateLimitException should
        still classify as RATE_LIMIT via MRO walk."""

        class BurstRateLimit(VendorAPIRateLimitException):
            pass

        exc = BurstRateLimit("burst")
        assert classify_exception(exc) == Outcome.RATE_LIMIT

    def test_feed_exceptions(self) -> None:
        assert classify_exception(VendorFeedException("x")) == Outcome.GENERIC_API_ERROR
        assert classify_exception(VendorFeedConnectionException("x")) == Outcome.GENERIC_API_ERROR
        assert (
            classify_exception(VendorFeedNotSubscribedException("x")) == Outcome.GENERIC_API_ERROR
        )

    def test_non_vendor_exception_raises(self) -> None:
        """Exceptions from outside the Dhan exception hierarchy are a bug in
        the caller — should not reach classify_exception."""
        with pytest.raises(UnknownExceptionError, match="should not have reached"):
            classify_exception(ValueError("unrelated"))

    def test_os_error_raises(self) -> None:
        with pytest.raises(UnknownExceptionError):
            classify_exception(OSError("oops"))

    def test_no_match_error_message_is_byte_exact(self) -> None:
        """The classifier-bug message (name + full MRO + remediation) is
        pinned byte-for-byte with ``==`` — a substring ``match=`` cannot
        kill the per-segment string mutants (DEC-MUTATION-RUNTIME-001)."""
        with pytest.raises(UnknownExceptionError) as excinfo:
            classify_exception(ValueError("unrelated"))
        assert str(excinfo.value) == (
            "Exception 'ValueError' (MRO: "
            "['ValueError', 'Exception', 'BaseException', 'object']) does not match "
            "any entry in BROKER_EXCEPTION_CLASSIFICATION. "
            "This exception should not have reached the retry classifier — "
            "verify the adapter only catches Dhan API exceptions."
        )


# ────────────────────────────────────────────────────────────────────────
# backoff_delay
# ────────────────────────────────────────────────────────────────────────


class TestBackoffDelay:
    def test_first_retry(self) -> None:
        # With deterministic RNG for reproducibility
        rng = random.Random(42)
        delay = backoff_delay(1, rng=rng)
        base = BACKOFF_SCHEDULE_SECONDS[0]  # 2.0
        assert base * (1 - JITTER_FRACTION) <= delay <= base * (1 + JITTER_FRACTION)

    def test_no_jitter_returns_exact_base(self) -> None:
        for i, base in enumerate(BACKOFF_SCHEDULE_SECONDS, start=1):
            assert backoff_delay(i, jitter_fraction=0.0) == base

    def test_schedule_is_exponential(self) -> None:
        """Each slot should be at least double the previous (up to
        the 60s plateau)."""
        schedule = BACKOFF_SCHEDULE_SECONDS
        # First few slots double
        assert schedule[1] / schedule[0] == 2.0
        assert schedule[2] / schedule[1] == 2.0
        assert schedule[3] / schedule[2] == 2.0
        # Then grow (faster than doubling past 16s due to 32-60-120)
        assert schedule[-1] >= schedule[0] * 100  # 900s >= 200s ✓

    def test_schedule_has_ten_slots(self) -> None:
        """FR-ING-008 specifies 10 soft retries max."""
        assert len(BACKOFF_SCHEDULE_SECONDS) == 10

    def test_clamps_at_last_slot(self) -> None:
        """Calling with retry_count past the schedule returns the last
        slot's value (the FSM should have escalated by then)."""
        last = BACKOFF_SCHEDULE_SECONDS[-1]
        assert backoff_delay(100, jitter_fraction=0.0) == last
        assert backoff_delay(1000, jitter_fraction=0.0) == last

    def test_zero_retry_count_raises(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            backoff_delay(0)

    def test_negative_retry_count_raises(self) -> None:
        with pytest.raises(ValueError):
            backoff_delay(-5)

    def test_jitter_is_bounded(self) -> None:
        """Over 1000 deterministic trials, every delay stays within
        [base * (1 - jitter), base * (1 + jitter)]."""
        rng = random.Random(0)
        base = BACKOFF_SCHEDULE_SECONDS[2]  # 8.0
        for _ in range(1000):
            d = backoff_delay(3, rng=rng)
            assert base * 0.8 <= d <= base * 1.2

    def test_jitter_is_never_negative(self) -> None:
        """Even with a weird jitter fraction, delay never goes below 0."""
        rng = random.Random(0)
        for _ in range(100):
            d = backoff_delay(1, jitter_fraction=5.0, rng=rng)
            assert d >= 0.0

    def test_deterministic_with_seeded_rng(self) -> None:
        """Same seed → same delay, across runs."""
        rng1 = random.Random(123)
        rng2 = random.Random(123)
        for retry in range(1, 11):
            assert backoff_delay(retry, rng=rng1) == backoff_delay(retry, rng=rng2)

    def test_schedule_slots_are_pinned_byte_exact(self) -> None:
        """Every slot of the FR-ING-008 ladder pinned by value — kills the
        per-slot ``+1.0`` constant mutants (32→33 … 900→901) that the
        ratio/length asserts cannot see (DEC-MUTATION-RUNTIME-001)."""
        assert BACKOFF_SCHEDULE_SECONDS == (
            2.0,
            4.0,
            8.0,
            16.0,
            32.0,
            60.0,
            120.0,
            240.0,
            480.0,
            900.0,
        )

    def test_retry_count_error_message_is_byte_exact(self) -> None:
        """Byte-exact ``==`` pin on the guard message — a substring
        ``match=`` cannot kill the string mutant (DEC-MUTATION-RUNTIME-001)."""
        with pytest.raises(ValueError) as excinfo:
            backoff_delay(0)
        assert str(excinfo.value) == "retry_count must be >= 1, got 0"

    def test_seeded_jitter_value_is_byte_exact(self) -> None:
        """Pin the exact jittered delay for a seeded RNG (DEC-MUTATION-RUNTIME-001).

        Byte-exact ``==`` on ``base + uniform(-f, +f) * base`` kills the
        ``== 0.0`` → ``!= 0.0`` early-return flip (returns the bare base), the
        ``uniform(+f, +f)`` sign flip (the constant ``+f`` jitter), the
        ``* base`` → ``/ base`` flip, and the ``base - jitter`` flip — all of
        which stay inside the ±20% RANGE asserts above.
        """
        base = BACKOFF_SCHEDULE_SECONDS[0]  # 2.0
        expected = base + random.Random(42).uniform(-JITTER_FRACTION, JITTER_FRACTION) * base
        assert expected != base  # guard: seed 42 draws a non-zero jitter
        assert expected != base * (1 + JITTER_FRACTION)  # guard: …and not the +f constant
        assert backoff_delay(1, rng=random.Random(42)) == expected

    def test_jitter_fraction_one_still_jitters(self) -> None:
        """``jitter_fraction=1.0`` must take the jitter path — pins the
        ``0.0`` sentinel in ``jitter_fraction == 0.0`` against the
        ``0.0 → 1.0`` constant mutant, which would silently return the
        un-jittered base for ±100% jitter (DEC-MUTATION-RUNTIME-001)."""
        base = BACKOFF_SCHEDULE_SECONDS[0]
        expected = base + random.Random(11).uniform(-1.0, 1.0) * base
        assert expected != base  # guard: seed 11 draws a non-zero jitter
        assert backoff_delay(1, jitter_fraction=1.0, rng=random.Random(11)) == expected

    def test_unseeded_path_uses_module_random_uniform(self) -> None:
        """The ``rng=None`` production path must draw
        ``random.uniform(-f, +f) * base`` from the module RNG.

        Seeds the module RNG (state restored) so the expected delay is exact —
        kills the production-branch sign flip, ``* base`` → ``/ base`` flip,
        and ``jitter = None`` mutants that the rng-injected tests can never
        reach (DEC-MUTATION-RUNTIME-001).
        """
        base = BACKOFF_SCHEDULE_SECONDS[0]
        state = random.getstate()
        try:
            random.seed(1234)
            expected = base + random.uniform(-JITTER_FRACTION, JITTER_FRACTION) * base
            random.seed(1234)
            actual = backoff_delay(1)
        finally:
            random.setstate(state)
        assert expected != base  # guard: seed 1234 draws a non-zero jitter
        assert expected != base * (1 + JITTER_FRACTION)  # guard: …and not the +f constant
        assert actual == expected

    def test_sub_second_delay_is_not_clamped_upward(self) -> None:
        """A jittered delay below 1.0s must pass through unchanged — pins the
        ``max(0.0, delay)`` floor against the ``0.0 → 1.0`` mutant, which
        would silently round sub-second delays up to a full second
        (DEC-MUTATION-RUNTIME-001)."""
        expected = 0.5 + random.Random(7).uniform(-JITTER_FRACTION, JITTER_FRACTION) * 0.5
        assert 0.0 < expected < 1.0  # guard: meaningful for the floor pin
        assert backoff_delay(1, schedule=(0.5,), rng=random.Random(7)) == expected


# ────────────────────────────────────────────────────────────────────────
# outcome_severity
# ────────────────────────────────────────────────────────────────────────


class TestOutcomeSeverity:
    def test_ok_is_info(self) -> None:
        assert outcome_severity(Outcome.OK_WITH_DATA) == "info"
        assert outcome_severity(Outcome.OK_EMPTY) == "info"

    def test_not_found_is_info(self) -> None:
        """NOT_FOUND is a recorded terminal, not a failure."""
        assert outcome_severity(Outcome.NOT_FOUND) == "info"

    def test_transient_is_warn(self) -> None:
        assert outcome_severity(Outcome.RATE_LIMIT) == "warn"
        assert outcome_severity(Outcome.TIMEOUT) == "warn"
        assert outcome_severity(Outcome.GENERIC_API_ERROR) == "warn"

    def test_permanent_is_error(self) -> None:
        assert outcome_severity(Outcome.BAD_REQUEST) == "error"
        assert outcome_severity(Outcome.AUTH_FAILED) == "error"
        assert outcome_severity(Outcome.AUTHZ_FAILED) == "error"

    def test_every_outcome_has_a_severity(self) -> None:
        """Exhaustive: every Outcome value must return one of
        info/warn/error, never crash."""
        for outcome in Outcome:
            sev = outcome_severity(outcome)
            assert sev in ("info", "warn", "error"), (
                f"{outcome} returned unexpected severity {sev!r}"
            )


# ────────────────────────────────────────────────────────────────────────
# retry_call — bounded "never give up on a recoverable error" driver
# ────────────────────────────────────────────────────────────────────────


class TestRetryCall:
    """Transient → backoff + retry; permanent → surface at once; non-vendor
    → never caught. Deterministic via injected ``sleep`` recorder + seeded RNG.
    """

    # NOTE: this test is deliberately FIRST in the class. The ``attempts = 1``
    # mutant of ``attempts += 1`` makes every always-failing retry_call test
    # below spin forever (the counter never reaches max_attempts), so under
    # mutmut's ``pytest -x`` runner the kill must land BEFORE any of them run
    # — converting that hang into a fast deterministic kill
    # (DEC-MUTATION-RUNTIME-001).
    def test_attempt_counter_increments_by_exactly_one(self) -> None:
        """``attempts += 1`` pinned: two transient failures then success must
        sleep exactly ``backoff_delay(1)`` then ``backoff_delay(2)``.

        ``attempts = 1`` repeats the slot-1 delay, which the exact sleep pin
        rejects (slot-1 max 2.4s < slot-2 min 3.2s, the ranges never overlap);
        ``attempts -= 1`` feeds ``backoff_delay(-1)`` → ValueError on the
        first retry. The thunk is bounded — it returns on call 3 — so no
        mutant variant can hang this test.
        """
        seed = 5
        calls = {"n": 0}

        def _flaky() -> str:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise VendorAPIRateLimitException("429")
            return "done"

        sleeps: list[float] = []
        result = retry_call(
            _flaky,
            retry_on=VendorBaseException,
            max_attempts=3,
            sleep=sleeps.append,
            rng=random.Random(seed),
        )
        assert result == "done"
        assert calls["n"] == 3
        replay = random.Random(seed)
        expected = [backoff_delay(1, rng=replay), backoff_delay(2, rng=replay)]
        assert expected[0] != expected[1]  # guard: slot-1 vs slot-2 delays differ
        assert sleeps == expected

    def test_retryable_outcomes_are_the_warn_tier(self) -> None:
        assert (
            frozenset({Outcome.RATE_LIMIT, Outcome.TIMEOUT, Outcome.GENERIC_API_ERROR})
            == RETRYABLE_OUTCOMES
        )

    def test_default_max_attempts_matches_backoff_ladder(self) -> None:
        assert len(BACKOFF_SCHEDULE_SECONDS) == DEFAULT_MAX_ATTEMPTS

    def test_returns_first_success_without_sleeping(self) -> None:
        sleeps: list[float] = []
        result = retry_call(lambda: "ok", retry_on=VendorBaseException, sleep=sleeps.append)
        assert result == "ok"
        assert sleeps == []

    @pytest.mark.parametrize("bad", [0, -1])
    def test_max_attempts_below_one_raises(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            retry_call(lambda: "ok", retry_on=VendorBaseException, max_attempts=bad)

    def test_permanent_error_propagates_without_retry(self) -> None:
        sleeps: list[float] = []

        def _boom() -> str:
            raise VendorAPIBadRequestException("nope")

        with pytest.raises(VendorAPIBadRequestException):
            retry_call(_boom, retry_on=VendorBaseException, sleep=sleeps.append)
        assert sleeps == []  # BAD_REQUEST is not retryable — surfaces at once

    def test_not_found_propagates_without_retry(self) -> None:
        sleeps: list[float] = []

        def _missing() -> str:
            raise VendorAPINotFoundException("gone")

        with pytest.raises(VendorAPINotFoundException):
            retry_call(_missing, retry_on=VendorBaseException, sleep=sleeps.append)
        assert sleeps == []

    def test_transient_then_success(self) -> None:
        calls = {"n": 0}

        def _flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise VendorAPIRateLimitException("429")
            return "recovered"

        sleeps: list[float] = []
        result = retry_call(
            _flaky, retry_on=VendorBaseException, sleep=sleeps.append, rng=random.Random(7)
        )
        assert result == "recovered"
        assert calls["n"] == 2
        assert len(sleeps) == 1

    def test_generic_api_error_is_retryable(self) -> None:
        calls = {"n": 0}

        def _flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise VendorAPIException("blip")
            return "ok"

        sleeps: list[float] = []
        result = retry_call(
            _flaky, retry_on=VendorBaseException, sleep=sleeps.append, rng=random.Random(1)
        )
        assert result == "ok"
        assert len(sleeps) == 1

    def test_transient_backoff_schedule_is_deterministic(self) -> None:
        """Sleeps follow backoff_delay(1), backoff_delay(2), … with the injected
        RNG — exact + replayable (no reliance on real timing)."""
        seed = 42

        def _always() -> str:
            raise VendorAPITimeoutException("slow")

        sleeps: list[float] = []
        with pytest.raises(VendorAPITimeoutException):
            retry_call(
                _always,
                retry_on=VendorBaseException,
                max_attempts=3,
                sleep=sleeps.append,
                rng=random.Random(seed),
            )
        replay = random.Random(seed)
        expected = [backoff_delay(1, rng=replay), backoff_delay(2, rng=replay)]
        assert sleeps == expected  # 2 sleeps (before attempts 2 and 3); none on the final raise

    def test_transient_exhausts_attempts_and_raises(self) -> None:
        calls = {"n": 0}

        def _always() -> str:
            calls["n"] += 1
            raise VendorAPIRateLimitException("429")

        sleeps: list[float] = []
        with pytest.raises(VendorAPIRateLimitException):
            retry_call(
                _always,
                retry_on=VendorBaseException,
                max_attempts=4,
                sleep=sleeps.append,
                rng=random.Random(3),
            )
        assert calls["n"] == 4  # tried exactly max_attempts times
        assert len(sleeps) == 3  # slept between attempts, never after the last

    def test_non_vendor_exception_is_never_caught(self) -> None:
        sleeps: list[float] = []

        def _bug() -> str:
            raise KeyError("not a vendor error")

        with pytest.raises(KeyError):
            retry_call(_bug, retry_on=VendorBaseException, sleep=sleeps.append)
        assert sleeps == []  # a non-vendor bug must surface, never spin

    def test_max_attempts_of_exactly_one_is_accepted(self) -> None:
        """Boundary pin for ``max_attempts < 1``: exactly 1 is VALID (one
        attempt, no retry) — kills the ``< 1`` → ``<= 1`` / ``< 2`` flips
        that would reject the legal single-attempt mode
        (DEC-MUTATION-RUNTIME-001)."""
        sleeps: list[float] = []
        result = retry_call(
            lambda: "ok", retry_on=VendorBaseException, max_attempts=1, sleep=sleeps.append
        )
        assert result == "ok"
        assert sleeps == []

    def test_max_attempts_error_message_is_byte_exact(self) -> None:
        """Byte-exact ``==`` pin on the guard message — a substring
        ``match=`` cannot kill the string mutant (DEC-MUTATION-RUNTIME-001)."""
        with pytest.raises(ValueError) as excinfo:
            retry_call(lambda: "ok", retry_on=VendorBaseException, max_attempts=0)
        assert str(excinfo.value) == "max_attempts must be >= 1, got 0"
