"""Unit tests for :mod:`brutex.config.deadline` (the COMMON wall-clock freeze guard).

DEC-STORE-WRITE-DEADLINE-001 (the layering move): the #999 primitive
(:func:`run_with_deadline` + :class:`DeadlineExceeded`) now lives at its ONE COMMON
home in the config layer — reused by the Groww SDK call bound, the token re-mint,
the durable lake write, AND the registry/coverage store-write guard — plus the ONE
env-seconds validation shape (:func:`resolve_env_seconds`) the three deadline
resolvers delegate to. 100% line+branch on ``brutex/config/deadline.py``:

* the fast success / inner-error / timeout branches of :func:`run_with_deadline`;
* the DEFAULT ``what`` / ``thread_name`` reproduce the pre-move Groww message +
  thread name BYTE-FOR-BYTE (the ``"deadline exceeded"`` substring is load-bearing
  for the ``from_groww_exception`` hint classification) and custom values are
  honored;
* a real ``fn``-raised :class:`TimeoutError` is NEVER re-typed;
* the daemon storm drains back to the thread-count baseline;
* every :func:`resolve_env_seconds` branch (unset / blank / non-numeric / nan /
  inf / zero / negative → default; valid env; explicit ``raw``).
"""

from __future__ import annotations

import threading
import time

import pytest
from brutex.config.deadline import DeadlineExceeded, resolve_env_seconds, run_with_deadline

_ENV = "BRUTEX_TEST_DEADLINE_SECONDS"


class TestRunWithDeadline:
    def test_returns_the_value_when_fast(self) -> None:
        """A fn that returns within the deadline yields its value verbatim."""
        assert run_with_deadline(lambda: 41 + 1, 5.0) == 42

    def test_propagates_the_underlying_error_unchanged(self) -> None:
        """An inner exception (not a timeout) is re-raised on the caller thread."""

        def _boom() -> int:
            raise ValueError("real failure, not a timeout")

        with pytest.raises(ValueError, match="real failure"):
            run_with_deadline(_boom, 5.0)

    def test_timeout_raises_deadline_exceeded_with_default_groww_message(self) -> None:
        """The DEFAULT ``what`` reproduces the pre-move Groww message byte-for-byte.

        The ``"deadline exceeded"`` substring is load-bearing (the
        ``from_groww_exception`` message-hint classification), so the default
        message must never drift (DEC-STORE-WRITE-DEADLINE-001 relocation pin).
        """
        release = threading.Event()

        def _hang() -> int:
            release.wait(timeout=5.0)
            return 0

        try:
            with pytest.raises(DeadlineExceeded) as excinfo:
                run_with_deadline(_hang, 0.05)
        finally:
            release.set()
        assert str(excinfo.value) == "Groww call deadline exceeded (0.05s)"
        assert isinstance(excinfo.value, TimeoutError)

    def test_custom_what_and_thread_name_are_honored(self) -> None:
        """A store-guard caller labels the message + the daemon thread per call."""
        release = threading.Event()
        seen_names: list[str] = []

        def _hang() -> int:
            seen_names.append(threading.current_thread().name)
            release.wait(timeout=5.0)
            return 0

        try:
            with pytest.raises(
                DeadlineExceeded, match=r"registry write deadline exceeded \(0\.05s\)"
            ):
                run_with_deadline(
                    _hang, 0.05, what="registry write", thread_name="registry-write-deadline"
                )
        finally:
            release.set()
        assert seen_names == ["registry-write-deadline"]

    def test_default_thread_name_is_the_groww_call_daemon(self) -> None:
        """The DEFAULT thread name reproduces the pre-move name byte-for-byte."""
        seen_names: list[str] = []

        def _fast() -> int:
            seen_names.append(threading.current_thread().name)
            return 7

        assert run_with_deadline(_fast, 5.0) == 7
        assert seen_names == ["groww-deadline-call"]

    def test_fn_raised_timeout_error_is_not_retyped(self) -> None:
        """A real ``TimeoutError`` from *fn* propagates UNCHANGED — never re-typed.

        The guard's own expiry is the :class:`DeadlineExceeded` SUBCLASS; a caller
        matching that subclass must never see a fn-raised ``TimeoutError`` there.
        """
        marker = TimeoutError("fn's own socket timeout")

        def _boom() -> int:
            raise marker

        with pytest.raises(TimeoutError) as excinfo:
            run_with_deadline(_boom, 5.0)
        assert excinfo.value is marker
        assert not isinstance(excinfo.value, DeadlineExceeded)

    def test_daemon_storm_drains_to_baseline(self) -> None:
        """N timed-out calls leave N daemons; releasing them drains the count.

        The guard's honest bound: abandoned daemons die with the call they wrap,
        so the live-thread count returns to its pre-storm baseline once every
        simulated hang releases.
        """
        release = threading.Event()

        def _hang() -> int:
            release.wait(timeout=30.0)
            return 0

        baseline = threading.active_count()
        for _ in range(4):
            with pytest.raises(DeadlineExceeded):
                run_with_deadline(_hang, 0.02)
        assert threading.active_count() >= baseline + 1  # abandoned daemons live
        release.set()
        deadline = time.monotonic() + 10.0
        while threading.active_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.01)
        assert threading.active_count() <= baseline


class TestResolveEnvSeconds:
    def test_unset_env_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        assert resolve_env_seconds(_ENV, 90.0) == 90.0

    def test_valid_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, "12.5")
        assert resolve_env_seconds(_ENV, 90.0) == 12.5

    def test_explicit_raw_bypasses_the_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, "12.5")
        assert resolve_env_seconds(_ENV, 90.0, "3.25") == 3.25

    @pytest.mark.parametrize("bad", ["", "   ", "ninety", "nan", "inf", "-inf", "0", "-5"])
    def test_bad_values_fail_safe_to_default(self, bad: str) -> None:
        """A bad value can NEVER disable a bound — it falls back to the default."""
        assert resolve_env_seconds(_ENV, 90.0, bad) == 90.0
