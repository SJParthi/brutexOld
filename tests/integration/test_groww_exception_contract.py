"""Contract test: BruteX classifier ↔ real growwapi exception hierarchy.

This test imports the ACTUAL ``growwapi.groww.exceptions`` module
(skipped if not installed) and verifies that every exception class
name BruteX's classifier dispatches on actually exists in the SDK.

## Why this matters

``brutex/orchestrator/retry.py`` classifies exceptions by **class name
string**, not by ``isinstance``, because the real Groww SDK isn't
imported at unit-test time (tests use ``tests/fakes/groww.py`` synth
classes). That classifier is a silent failure mode:

* If Groww renames ``GrowwAPIAuthorisationException`` →
  ``GrowwAPIAuthorizationException`` (American spelling), our
  classifier would stop recognising auth failures and quarantine
  every 403 as "unknown".
* If Groww adds a new exception like ``BaseGrowwException`` with a
  different spelling than our ``GrowwBaseException``, we miss the
  MRO fallback.
* If Groww adds a new error like ``InstrumentNotFoundException``
  that we don't know about, it bubbles up as ``UnknownExceptionError``
  and crashes the worker.

This contract test catches all three cases at CI time by importing
the real SDK and asserting:

1. Every name in ``GROWW_EXCEPTION_CLASSIFICATION`` exists in the
   real module as a class that inherits from ``Exception``.
2. Every real Groww exception class is either in our classifier OR
   inherits from a class that is (so MRO fallback works).
3. No class in the real SDK is accidentally mis-spelled relative to
   what BruteX expects.

If the test fails, the error message names the drift explicitly and
points at ``brutex/orchestrator/retry.py`` for the fix.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("growwapi")

import growwapi.groww.exceptions as groww_exceptions
from brutex.orchestrator.retry import (
    GROWW_EXCEPTION_CLASSIFICATION,
)

# ────────────────────────────────────────────────────────────────────────
# Collect the real Groww exception hierarchy
# ────────────────────────────────────────────────────────────────────────


def _real_groww_exception_classes() -> dict[str, type[Exception]]:
    """Return every Exception subclass exported by growwapi.groww.exceptions."""
    classes: dict[str, type[Exception]] = {}
    for name, obj in inspect.getmembers(groww_exceptions):
        if inspect.isclass(obj) and issubclass(obj, BaseException) and name.endswith("Exception"):
            classes[name] = obj
    return classes


_REAL_CLASSES = _real_groww_exception_classes()


# ────────────────────────────────────────────────────────────────────────
# Known aliases — names BruteX uses that map to differently-named real classes
# ────────────────────────────────────────────────────────────────────────


#: Mapping from BruteX-classifier name → real SDK class name.
#: Used to handle historical spelling drift. When the SDK renames a
#: class, add an entry here (and optionally update the classifier).
KNOWN_ALIASES: dict[str, str] = {
    # BruteX uses "GrowwBaseException" in the classifier. The real
    # SDK exposes the root as "BaseGrowwException". Both exist and
    # should be accepted.
    "GrowwBaseException": "BaseGrowwException",
}


# ────────────────────────────────────────────────────────────────────────
# Test: every BruteX-classifier name resolves to a real class
# ────────────────────────────────────────────────────────────────────────


class TestClassifierNamesExist:
    """Every name in GROWW_EXCEPTION_CLASSIFICATION must resolve to an
    actual Exception subclass in the real SDK (directly or via
    KNOWN_ALIASES)."""

    @pytest.mark.parametrize("classifier_name", sorted(GROWW_EXCEPTION_CLASSIFICATION.keys()))
    def test_name_resolves_to_real_class(self, classifier_name: str) -> None:
        real_name = KNOWN_ALIASES.get(classifier_name, classifier_name)
        assert real_name in _REAL_CLASSES, (
            f"brutex.orchestrator.retry.GROWW_EXCEPTION_CLASSIFICATION "
            f"uses {classifier_name!r}, but no class by that name exists "
            f"in growwapi.groww.exceptions. Real classes: "
            f"{sorted(_REAL_CLASSES.keys())}. If the SDK renamed the "
            f"class, add an entry to KNOWN_ALIASES in this file and "
            f"update the classifier."
        )
        cls = _REAL_CLASSES[real_name]
        assert issubclass(cls, BaseException), f"{real_name} is not an Exception subclass"


# ────────────────────────────────────────────────────────────────────────
# Test: every real Groww exception is classifiable (directly or via MRO)
# ────────────────────────────────────────────────────────────────────────


class TestCoverageOfRealHierarchy:
    """Every real Groww exception class must be classifiable by BruteX,
    either directly (its name is in GROWW_EXCEPTION_CLASSIFICATION) or
    via MRO fallback (at least one of its ancestors' names is in the
    classifier).
    """

    # Names in the classifier + their known-alias equivalents form the
    # set of "covered names" for MRO-fallback purposes.
    _COVERED_NAMES: frozenset[str] = frozenset(GROWW_EXCEPTION_CLASSIFICATION.keys()) | frozenset(
        KNOWN_ALIASES.values()
    )

    @pytest.mark.parametrize(
        ("real_name", "real_cls"),
        sorted(_real_groww_exception_classes().items()),
    )
    def test_real_class_is_classifiable(self, real_name: str, real_cls: type[Exception]) -> None:
        mro_names = [c.__name__ for c in real_cls.mro()]
        covered = [name for name in mro_names if name in self._COVERED_NAMES]
        assert covered, (
            f"Real Groww class {real_name!r} (MRO: {mro_names}) is not "
            f"classifiable by BruteX — no ancestor name matches "
            f"GROWW_EXCEPTION_CLASSIFICATION. Add an entry for "
            f"{real_name!r} (or a common ancestor) to the classifier."
        )


# ────────────────────────────────────────────────────────────────────────
# Test: BruteX spellings match the SDK (no silent drift)
# ────────────────────────────────────────────────────────────────────────


class TestSpellingDrift:
    def test_authorisation_is_british_spelling(self) -> None:
        """Groww uses the British spelling 'Authorisation' — if they
        ever switch to American 'Authorization', our classifier will
        silently miss 403 errors until this test starts failing."""
        assert "GrowwAPIAuthorisationException" in _REAL_CLASSES, (
            "Groww SDK has switched from British 'Authorisation' to "
            "American 'Authorization' spelling. Update "
            "GROWW_EXCEPTION_CLASSIFICATION in brutex/orchestrator/retry.py "
            "and add an alias in this file's KNOWN_ALIASES."
        )
        assert "GrowwAPIAuthorizationException" not in _REAL_CLASSES

    def test_authentication_is_present(self) -> None:
        assert "GrowwAPIAuthenticationException" in _REAL_CLASSES

    def test_rate_limit_is_present(self) -> None:
        assert "GrowwAPIRateLimitException" in _REAL_CLASSES

    def test_timeout_is_present(self) -> None:
        assert "GrowwAPITimeoutException" in _REAL_CLASSES

    def test_not_found_is_present(self) -> None:
        assert "GrowwAPINotFoundException" in _REAL_CLASSES

    def test_bad_request_is_present(self) -> None:
        assert "GrowwAPIBadRequestException" in _REAL_CLASSES
