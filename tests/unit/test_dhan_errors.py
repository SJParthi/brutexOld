"""Unit tests for ``brutex.orchestrator.dhan_errors`` (DEC-P0-031, PR #50d).

Covers the 4-agent pre-impl review's mandatory blockers:
- 22-code → Outcome mapping correctness (Agent 3 §A)
- safe_message discipline enforcement on every subclass (Agent 2 §B)
- Whitelist semantics — unknown code raises (Agent 1 §A + Agent 3 §D)
- HTTP 5xx / empty / None code synthesis to DH-908 (Agent 3 §K)
- Token-leak regression: subclass __str__ never leaks payload contents (Agent 2 §G)
- Frozen 22-code vocabulary test (Agent 3 §I)
"""

from __future__ import annotations

import pytest
from brutex.orchestrator.dhan_errors import (
    DHAN_CODE_TO_EXCEPTION,
    DHAN_ERROR_CLASSIFICATION,
    HTTP_5XX_FALLBACK_CODE,
    DhanError,
    DhanError800,
    DhanError805,
    DhanErrorDH907,
    UnknownDhanCodeError,
    classify_dhan_code,
)
from brutex.orchestrator.state import Outcome

# ────────────────────────────────────────────────────────────────────────
# Frozen 22-code vocabulary
# ────────────────────────────────────────────────────────────────────────

_EXPECTED_22_CODES: frozenset[str] = frozenset(
    {
        # Data API (12)
        "800",
        "804",
        "805",
        "806",
        "807",
        "808",
        "809",
        "810",
        "811",
        "812",
        "813",
        "814",
        # Trading API (10)
        "DH-901",
        "DH-902",
        "DH-903",
        "DH-904",
        "DH-905",
        "DH-906",
        "DH-907",
        "DH-908",
        "DH-909",
        "DH-910",
    }
)


# ────────────────────────────────────────────────────────────────────────
# Frozen-set tests — protect against silent vocabulary drift
# ────────────────────────────────────────────────────────────────────────


class TestFrozenVocabulary:
    """Closes Agent 3 §I — frozen-set guard against silent code-list drift."""

    def test_classification_dict_has_exactly_22_codes(self) -> None:
        assert set(DHAN_ERROR_CLASSIFICATION.keys()) == _EXPECTED_22_CODES
        assert len(DHAN_ERROR_CLASSIFICATION) == 22

    def test_exception_dict_has_exactly_22_codes(self) -> None:
        assert set(DHAN_CODE_TO_EXCEPTION.keys()) == _EXPECTED_22_CODES
        assert len(DHAN_CODE_TO_EXCEPTION) == 22

    def test_classification_and_exception_dicts_have_same_keys(self) -> None:
        """Every classified code must have a corresponding exception subclass."""
        assert set(DHAN_ERROR_CLASSIFICATION.keys()) == set(DHAN_CODE_TO_EXCEPTION.keys())


# ────────────────────────────────────────────────────────────────────────
# safe_message_default enforcement (Agent 2 §B)
# ────────────────────────────────────────────────────────────────────────


class TestSafeMessageDiscipline:
    """Every DhanError subclass MUST set safe_message_default at class level."""

    def test_every_subclass_has_safe_message_default(self) -> None:
        # Introspect all DhanError subclasses (lives in DHAN_CODE_TO_EXCEPTION)
        for exc_class in DHAN_CODE_TO_EXCEPTION.values():
            assert hasattr(exc_class, "safe_message_default"), (
                f"{exc_class.__name__} missing safe_message_default"
            )
            assert isinstance(exc_class.safe_message_default, str)
            assert len(exc_class.safe_message_default) > 0

    def test_every_subclass_has_dhan_error_code(self) -> None:
        for code, exc_class in DHAN_CODE_TO_EXCEPTION.items():
            assert hasattr(exc_class, "dhan_error_code"), (
                f"{exc_class.__name__} missing dhan_error_code"
            )
            assert exc_class.dhan_error_code == code, (
                f"{exc_class.__name__}.dhan_error_code = {exc_class.dhan_error_code!r}; "
                f"dict key = {code!r}"
            )

    def test_subclass_missing_safe_message_default_raises(self) -> None:
        """Fail-loud regression oracle: forgetting safe_message_default
        raises AttributeError at construction (NOT silent string leak).
        """

        class _BadDhanError(DhanError):
            dhan_error_code = "TEST-999"
            # NO safe_message_default

        with pytest.raises(AttributeError, match="safe_message_default"):
            _BadDhanError("some detail")

    def test_str_returns_safe_message_not_detailed(self) -> None:
        """str(exc) MUST return the categorical safe_message, NOT the
        detailed message (which may contain token/URL/payload).
        """
        exc = DhanError805("token=eyJ0XXX url=https://api.dhan.co/v2/charts/intraday")
        assert str(exc) == "Dhan Data API rate limit hit (805)"
        # The detailed message is stored in args[0] for local debugging
        # but MUST NOT appear in str(exc).
        assert "eyJ0XXX" not in str(exc)
        assert "api.dhan.co" not in str(exc)


# ────────────────────────────────────────────────────────────────────────
# Token-leak regression (Agent 2 §G)
# ────────────────────────────────────────────────────────────────────────


class TestTokenLeakRegression:
    """Critical security oracle: subclass __str__ must never leak
    token / URL / Bearer / response-body contents.
    """

    @pytest.mark.parametrize("exc_class", list(DHAN_CODE_TO_EXCEPTION.values()))
    def test_no_subclass_str_leaks_token(self, exc_class: type[DhanError]) -> None:
        """Exercise each of the 22 subclasses with payload containing
        token/URL/Bearer markers and assert str(exc) contains NONE of
        them.
        """
        leaky_payload = (
            "token=eyJ0eXAiOJ1bGcixxxxxxxxx "
            "Authorization: Bearer xxxxxxxxxxxxxxxxx "
            "url=https://api.dhan.co/v2/charts/intraday?token=eyJ"
        )
        exc = exc_class(leaky_payload)
        rendered = str(exc)
        assert "eyJ" not in rendered, f"{exc_class.__name__} leaked JWT token"
        assert "Bearer" not in rendered, f"{exc_class.__name__} leaked Bearer header"
        assert "api.dhan.co" not in rendered, f"{exc_class.__name__} leaked URL host"
        assert "Authorization" not in rendered, f"{exc_class.__name__} leaked auth header"


# ────────────────────────────────────────────────────────────────────────
# Code → Outcome dispatch (Agent 3 §A) — table-driven
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # Data API (12 codes)
        ("800", Outcome.GENERIC_API_ERROR),
        ("804", Outcome.FATAL),
        ("805", Outcome.RATE_LIMIT),
        ("806", Outcome.FATAL),
        ("807", Outcome.AUTH_FAILED),
        ("808", Outcome.AUTH_FAILED),
        ("809", Outcome.AUTH_FAILED),
        ("810", Outcome.AUTH_FAILED),
        ("811", Outcome.BAD_REQUEST),
        ("812", Outcome.BAD_REQUEST),
        ("813", Outcome.BAD_REQUEST),
        ("814", Outcome.BAD_REQUEST),
        # Trading API (10 codes)
        ("DH-901", Outcome.AUTH_FAILED),
        ("DH-902", Outcome.FATAL),
        ("DH-903", Outcome.AUTH_FAILED),
        ("DH-904", Outcome.RATE_LIMIT),
        ("DH-905", Outcome.BAD_REQUEST),
        ("DH-906", Outcome.BAD_REQUEST),
        ("DH-907", Outcome.NO_DATA),
        ("DH-908", Outcome.GENERIC_API_ERROR),
        ("DH-909", Outcome.TIMEOUT),
        ("DH-910", Outcome.GENERIC_API_ERROR),
    ],
)
def test_classify_dhan_code_returns_expected_outcome(code: str, expected: Outcome) -> None:
    """Each of the 22 codes dispatches to the documented Outcome per
    docs/TRACK1_DATA_INGEST_DESIGN.md:678-705.
    """
    assert classify_dhan_code(code) == expected


# ────────────────────────────────────────────────────────────────────────
# Whitelist semantics (Agent 1 §A + Agent 3 §D + §H)
# ────────────────────────────────────────────────────────────────────────


class TestWhitelistSemantics:
    """Unknown codes raise UnknownDhanCodeError (NOT fall-through to a
    safe default). Closes MITM retry-storm vector.
    """

    def test_unknown_code_raises(self) -> None:
        with pytest.raises(UnknownDhanCodeError):
            classify_dhan_code("DH-999")

    def test_unknown_code_error_has_safe_message(self) -> None:
        with pytest.raises(UnknownDhanCodeError) as exc_info:
            classify_dhan_code("DH-999")
        assert str(exc_info.value) == (
            "Unknown Dhan API error code; halting for operator review."
        )
        # Detailed message (in exc.args[0]) DOES contain the code
        # for local debugging — but str(exc) returns the safe form.
        assert "DH-999" not in str(exc_info.value)

    def test_arbitrary_garbage_code_raises(self) -> None:
        with pytest.raises(UnknownDhanCodeError):
            classify_dhan_code("not-a-real-code")

    def test_numeric_unknown_code_raises(self) -> None:
        with pytest.raises(UnknownDhanCodeError):
            classify_dhan_code("999")


# ────────────────────────────────────────────────────────────────────────
# HTTP 5xx / empty / None code synthesis (Agent 3 §K)
# ────────────────────────────────────────────────────────────────────────


class TestHttp5xxFallback:
    """Empty/None code synthesizes to DH-908 (Internal Server Error)
    so transient HTTP 5xx classifies as retryable, not as
    UnknownDhanCodeError → FATAL halt.
    """

    def test_none_code_synthesizes_to_dh908(self) -> None:
        assert classify_dhan_code(None) == Outcome.GENERIC_API_ERROR

    def test_empty_string_synthesizes_to_dh908(self) -> None:
        assert classify_dhan_code("") == Outcome.GENERIC_API_ERROR

    def test_fallback_code_constant_is_dh908(self) -> None:
        assert HTTP_5XX_FALLBACK_CODE == "DH-908"

    def test_fallback_code_is_in_classification_dict(self) -> None:
        """Sanity: the fallback code must itself be classified."""
        assert HTTP_5XX_FALLBACK_CODE in DHAN_ERROR_CLASSIFICATION


# ────────────────────────────────────────────────────────────────────────
# Exception class construction sanity
# ────────────────────────────────────────────────────────────────────────


class TestExceptionConstruction:
    """Smoke tests for each subclass — construct + check attributes."""

    def test_dhan_error_800_has_code(self) -> None:
        assert DhanError800.dhan_error_code == "800"
        assert DhanError800().safe_message_default == "Dhan Data API internal server error (800)"

    def test_dhan_error_dh907_has_code(self) -> None:
        assert DhanErrorDH907.dhan_error_code == "DH-907"

    def test_all_22_subclasses_instantiable_with_empty_detail(self) -> None:
        for exc_class in DHAN_CODE_TO_EXCEPTION.values():
            exc = exc_class()
            assert isinstance(exc, DhanError)
            assert exc.safe_message is not None

    def test_all_22_subclasses_instantiable_with_detail_string(self) -> None:
        for exc_class in DHAN_CODE_TO_EXCEPTION.values():
            exc = exc_class("debug payload")
            assert isinstance(exc, DhanError)


# ────────────────────────────────────────────────────────────────────────
# Outcome exhaustiveness (Agent 3 §J)
# ────────────────────────────────────────────────────────────────────────


class TestOutcomeExhaustiveness:
    """Every Outcome value the classifier emits must exist in the enum."""

    def test_all_emitted_outcomes_are_valid_enum_members(self) -> None:
        emitted = set(DHAN_ERROR_CLASSIFICATION.values())
        for outcome in emitted:
            assert outcome in Outcome

    def test_classifier_emits_only_known_outcomes(self) -> None:
        emitted = set(DHAN_ERROR_CLASSIFICATION.values())
        expected_subset = {
            Outcome.GENERIC_API_ERROR,
            Outcome.FATAL,
            Outcome.RATE_LIMIT,
            Outcome.AUTH_FAILED,
            Outcome.BAD_REQUEST,
            Outcome.NO_DATA,
            Outcome.TIMEOUT,
        }
        # Every emitted outcome must be in our expected set; we should
        # NOT see OK_WITH_DATA, OK_EMPTY, NOT_FOUND, AUTHZ_FAILED here.
        assert emitted.issubset(expected_subset)
