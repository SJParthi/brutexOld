"""Unit tests for brutex.orchestrator.retry.

Validates the exception-to-Outcome classifier, the exponential backoff
schedule, and the severity helper. No the Groww API imports at module load
time — tests use synthetic exception classes with the same names.
"""

from __future__ import annotations

import random

import pytest
from brutex.orchestrator.retry import (
    BACKOFF_SCHEDULE_SECONDS,
    BROKER_EXCEPTION_CLASSIFICATION,
    JITTER_FRACTION,
    REQUIRED_EXCEPTION_NAMES,
    UnknownExceptionError,
    backoff_delay,
    classify_exception,
    classify_exception_name,
    outcome_severity,
)
from brutex.orchestrator.state import Outcome

# ────────────────────────────────────────────────────────────────────────
# Synthetic Groww API exception classes (name-matched to the real SDK)
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
        """Groww uses British 'Authorisation' — locked."""
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
        with pytest.raises(UnknownExceptionError, match="Unknown Groww API exception"):
            classify_exception_name("VendorAPITotallyNewException")

    def test_unknown_error_mentions_remediation(self) -> None:
        try:
            classify_exception_name("NotAVendorException")
        except UnknownExceptionError as e:
            msg = str(e)
            assert "BROKER_EXCEPTION_CLASSIFICATION" in msg
            assert "test_orchestrator_retry.py" in msg


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
        """Exceptions from outside the Groww exception hierarchy are a bug in
        the caller — should not reach classify_exception."""
        with pytest.raises(UnknownExceptionError, match="should not have reached"):
            classify_exception(ValueError("unrelated"))

    def test_os_error_raises(self) -> None:
        with pytest.raises(UnknownExceptionError):
            classify_exception(OSError("oops"))


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
