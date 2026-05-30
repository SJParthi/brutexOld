"""Unit tests for :mod:`brutex.adapters.groww.remint_guard`.

Pure concurrency logic — no SDK, no network. Covers the lock-free happy path,
the one-shot latch-bounded re-mint, and auth-error classification at 100% line
+ branch.
"""

from __future__ import annotations

import pytest
from brutex.adapters.groww.remint_guard import RemintGuard, is_auth_error


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Authentication failed", True),
        ("Your API token has either expired or is invalid", True),
        ("401 Unauthorized", True),
        ("invalid token", True),
        ("unauthorized access", True),
        ("bad request: invalid expiry_date", False),  # "invalid" but NOT "invalid token"
        ("rate limit exceeded", False),
        ("", False),
    ],
)
def test_is_auth_error(message: str, expected: bool) -> None:
    """Auth fingerprints match (case-insensitive); unrelated errors don't."""
    assert is_auth_error(RuntimeError(message)) is expected


def test_is_auth_error_custom_fingerprints() -> None:
    """A caller-supplied fingerprint tuple is honoured."""
    assert is_auth_error(RuntimeError("CUSTOM boom"), ("custom",)) is True
    assert is_auth_error(RuntimeError("other"), ("custom",)) is False


def test_call_success_runs_without_rebind() -> None:
    """A successful call returns its value and never re-mints."""
    rebinds: list[int] = []
    guard = RemintGuard(lambda: rebinds.append(1))
    assert guard.call(lambda: "ok") == "ok"
    assert rebinds == []


def test_call_non_auth_error_propagates_without_rebind() -> None:
    """A non-auth error propagates immediately, with no re-mint."""
    rebinds: list[int] = []
    guard = RemintGuard(lambda: rebinds.append(1))

    def _fn() -> str:
        raise RuntimeError("bad request — malformed payload")

    with pytest.raises(RuntimeError, match="bad request"):
        guard.call(_fn)
    assert rebinds == []


def test_call_auth_error_remints_once_then_retries() -> None:
    """An auth error triggers exactly one re-mint, then the retry succeeds."""
    rebinds: list[int] = []
    guard = RemintGuard(lambda: rebinds.append(1))
    attempts: list[int] = []

    def _fn() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Authentication failed")
        return "ok-after-remint"

    assert guard.call(_fn) == "ok-after-remint"
    assert rebinds == [1]  # rebound exactly once
    assert len(attempts) == 2  # original + retry


def test_call_auth_error_latched_no_second_remint() -> None:
    """A second auth error reuses the latch — it never re-mints twice."""
    rebinds: list[int] = []
    guard = RemintGuard(lambda: rebinds.append(1))

    seq1: list[object] = [RuntimeError("401 unauthorized"), "ok1"]
    seq2: list[object] = [RuntimeError("invalid token"), "ok2"]

    def _drain(seq: list[object]) -> object:
        value = seq.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    assert guard.call(lambda: _drain(seq1)) == "ok1"
    assert guard.call(lambda: _drain(seq2)) == "ok2"
    assert rebinds == [1]  # only ONE re-mint across both auth failures


def test_call_auth_error_still_failing_propagates() -> None:
    """If the call still auth-fails after the single re-mint, it propagates."""
    guard = RemintGuard(lambda: None)

    def _fn() -> str:
        raise RuntimeError("Authentication failed")

    with pytest.raises(RuntimeError, match="Authentication failed"):
        guard.call(_fn)
