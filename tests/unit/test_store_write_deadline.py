"""Unit tests for :mod:`brutex.data.store_write_deadline` (DEC-STORE-WRITE-DEADLINE-001).

The registry/coverage store-write freeze guard — the metadata twin of
DEC-FETCH-HUNG-WRITE-DEADLINE-001 (#999). Every path is exercised deterministically
with NO real hang risk (the #999 recipe: a fn blocking on a :class:`threading.Event`
with a 30 s safety, released in ``finally``): the healthy byte-identical path, the
breach (deadline fires near 0.2 s, transient raise, one WARN), the wedge latch
(instant ``latched=True`` fail-fast, fn never invoked, zero new threads, no guard
log), the token-matched self-recovery (one INFO, store resumes), the defensive
token-mismatch arm, the thread-spawn-failure no-stuck-latch regression (a
``Thread.start`` RuntimeError leaves NO zombie — the orphaned token must be
cleared, never a permanent false wedge), un-retyped fn errors (incl. a REAL
``TimeoutError``), the
env-tunable resolver, and the transient error class (message / attrs / pickle /
never-a-``LakeWriteHalt``). 100% line+branch on
``brutex/data/store_write_deadline.py``.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest
from brutex.data import store_write_deadline as swd
from brutex.data.errors import DataLakeError, LakeWriteHalt, StoreWriteDeadlineExceeded
from brutex.data.store_write_deadline import (
    STORE_WRITE_DEADLINE_ENV,
    STORE_WRITE_DEADLINE_EVENT,
    STORE_WRITE_DEADLINE_S,
    STORE_WRITE_RECOVERED_EVENT,
    StoreWriteDeadlineGuard,
    resolve_store_write_deadline_seconds,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """Poll *predicate* until true or fail loudly (never hangs the suite)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:  # pragma: no cover - only on a real test hang
            pytest.fail("condition not reached within the watchdog bound")
        time.sleep(0.005)


class _HungWrite:
    """A write closure that BLOCKS on a gate — the deterministic wedge.

    ``entered`` proves the hang really started; ``gate`` is released by the test's
    ``finally`` (30 s safety so a crashed test never leaves a forever-daemon);
    ``calls`` counts invocations so the latch's fn-never-invoked pin is provable.
    """

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        self.entered.set()
        self.gate.wait(timeout=30.0)
        return "late-value"


class TestHealthyPath:
    def test_returns_value_not_wedged_zero_logs(self) -> None:
        guard = StoreWriteDeadlineGuard(store_label="registry")
        with capture_logs() as logs:
            assert guard.run(lambda: "written", op="upsert_expiries") == "written"
        assert guard.wedged is False
        assert logs == []  # zero new log events on the healthy path

    def test_explicit_deadline_skips_the_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit ``deadline_seconds`` never reads the env (the #999 shape)."""
        monkeypatch.setenv(STORE_WRITE_DEADLINE_ENV, "not-a-number-would-still-be-safe")
        guard = StoreWriteDeadlineGuard(store_label="registry")
        assert guard.run(lambda: 7, op="op", deadline_seconds=5.0) == 7


class TestBreach:
    def test_hung_write_trips_the_deadline(self) -> None:
        """The deterministic hang: deadline fires near 0.2 s, transient raised, WARN."""
        guard = StoreWriteDeadlineGuard(store_label="registry")
        hung = _HungWrite()
        start = time.monotonic()
        try:
            with capture_logs() as logs, pytest.raises(StoreWriteDeadlineExceeded) as excinfo:
                guard.run(hung, op="upsert_contracts", deadline_seconds=0.2)
            elapsed = time.monotonic() - start
            assert hung.entered.is_set()  # the hang genuinely started
            assert elapsed < 5.0  # freed near the deadline, far below the 30 s safety
            assert guard.wedged is True
            exc = excinfo.value
            assert exc.deadline_seconds == 0.2
            assert exc.store == "registry"
            assert exc.op == "upsert_contracts"
            assert exc.latched is False
            warns = [r for r in logs if r["event"] == STORE_WRITE_DEADLINE_EVENT]
            assert len(warns) == 1
            assert warns[0]["store"] == "registry"
            assert warns[0]["op"] == "upsert_contracts"
            assert warns[0]["deadline_seconds"] == 0.2
        finally:
            hung.gate.set()
        _wait_until(lambda: not guard.wedged)

    def test_breach_uses_env_resolved_default_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``deadline_seconds=None`` resolves the env override (the DYNAMIC knob)."""
        monkeypatch.setenv(STORE_WRITE_DEADLINE_ENV, "0.2")
        guard = StoreWriteDeadlineGuard(store_label="coverage")
        hung = _HungWrite()
        try:
            with pytest.raises(StoreWriteDeadlineExceeded) as excinfo:
                guard.run(hung, op="upsert_coverage")
            assert excinfo.value.deadline_seconds == 0.2
        finally:
            hung.gate.set()
        _wait_until(lambda: not guard.wedged)


class TestWedgeLatch:
    def test_latched_fail_fast_spawns_no_thread_and_never_calls_fn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = StoreWriteDeadlineGuard(store_label="registry")
        hung = _HungWrite()
        try:
            with pytest.raises(StoreWriteDeadlineExceeded):
                guard.run(hung, op="upsert_expiries", deadline_seconds=0.2)
            assert guard.wedged is True

            def _never(*_a: object, **_k: object) -> object:  # pragma: no cover - the pin
                raise AssertionError("run_with_deadline must NOT run while latched")

            monkeypatch.setattr(swd, "run_with_deadline", _never)
            second = _HungWrite()
            start = time.monotonic()
            with capture_logs() as logs, pytest.raises(StoreWriteDeadlineExceeded) as excinfo:
                guard.run(second, op="upsert_contracts", deadline_seconds=0.2)
            assert time.monotonic() - start < 0.1  # instant — no deadline wait
            assert second.calls == 0  # fn never invoked
            assert excinfo.value.latched is True
            assert "[latched fail-fast]" in str(excinfo.value)
            assert logs == []  # NO guard log on a latched raise (storm avoidance)
        finally:
            hung.gate.set()
        _wait_until(lambda: not guard.wedged)


class TestSpawnFailureNeverSticksTheLatch:
    def test_thread_spawn_failure_clears_the_token_and_the_store_is_not_bricked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILS-BEFORE regression: a ``Thread.start()`` RuntimeError inside
        ``run_with_deadline`` (thread exhaustion — realistic under the very zombie
        storm this guard targets) used to leave ``_active`` set FOREVER with NO
        zombie in existence to self-clear it: every later write instantly
        ``latched=True``, every read the terminal wedged error — a false 'wedged
        volume' diagnosis bricking the store for the process lifetime. The
        BaseException arm now clears the orphaned token (token-identity-safe)."""
        guard = StoreWriteDeadlineGuard(store_label="registry")
        calls = 0

        def _spawn_fails(*_a: object, **_k: object) -> object:
            nonlocal calls
            calls += 1
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(swd, "run_with_deadline", _spawn_fails)
        with capture_logs() as logs, pytest.raises(RuntimeError, match="can't start new thread"):
            guard.run(lambda: "never-runs", op="upsert_expiries", deadline_seconds=0.2)
        assert calls == 1  # the failure came from the guard's own run_with_deadline call
        assert guard.wedged is False  # NO false wedge — the orphaned token was cleared
        assert logs == []  # not a breach: no deadline WARN, no recovery INFO
        # The store is NOT bricked: with the primitive healthy again, the very
        # next guarded write runs normally (no latched fail-fast).
        monkeypatch.undo()
        assert guard.run(lambda: "ok", op="upsert_expiries", deadline_seconds=5.0) == "ok"
        assert guard.wedged is False


class TestSelfRecovery:
    def test_zombie_completion_clears_the_latch_and_logs_recovery(self) -> None:
        guard = StoreWriteDeadlineGuard(store_label="coverage")
        hung = _HungWrite()
        with pytest.raises(StoreWriteDeadlineExceeded):
            guard.run(hung, op="upsert_coverage", deadline_seconds=0.2)
        assert guard.wedged is True
        with capture_logs() as logs:
            hung.gate.set()  # the volume "recovers" — the zombie completes
            _wait_until(lambda: not guard.wedged)
            # The INFO is emitted AFTER the token clear (same zombie thread) —
            # wait for the log itself, not just the state flip.
            _wait_until(lambda: any(r["event"] == STORE_WRITE_RECOVERED_EVENT for r in logs))
        recovered = [r for r in logs if r["event"] == STORE_WRITE_RECOVERED_EVENT]
        assert len(recovered) == 1
        assert recovered[0]["store"] == "coverage"
        assert recovered[0]["op"] == "upsert_coverage"
        # The store resumes: the next guarded write succeeds.
        assert guard.run(lambda: "ok", op="upsert_coverage", deadline_seconds=5.0) == "ok"

    def test_token_mismatch_defensive_arm_never_clobbers(self) -> None:
        """White-box: a stale zombie whose token was superseded must not clear state."""
        guard = StoreWriteDeadlineGuard(store_label="registry")
        hung = _HungWrite()
        with pytest.raises(StoreWriteDeadlineExceeded):
            guard.run(hung, op="upsert_expiries", deadline_seconds=0.2)
        # Simulate the token having been superseded (identity mismatch) before the
        # zombie's finally runs — its arm must SKIP both the clear and the INFO.
        sentinel = object()
        guard._active = sentinel
        with capture_logs() as logs:
            hung.gate.set()
            _wait_until(lambda: hung.calls == 1)
            time.sleep(0.05)  # give the zombie's finally time to run
        assert guard._active is sentinel  # not clobbered by the stale zombie
        assert guard._abandoned_op == "upsert_expiries"  # recovery NOT logged
        assert [r for r in logs if r["event"] == STORE_WRITE_RECOVERED_EVENT] == []
        guard._active = None  # restore for a clean teardown


class TestFnErrorsPropagateUnchanged:
    def test_fn_value_error_propagates_and_clears_the_token(self) -> None:
        guard = StoreWriteDeadlineGuard(store_label="registry")

        def _boom() -> None:
            raise ValueError("a real validation failure")

        with pytest.raises(ValueError, match="real validation"):
            guard.run(_boom, op="upsert_contracts", deadline_seconds=5.0)
        assert guard.wedged is False  # the finally cleared the token on the error path

    def test_fn_real_timeout_error_is_not_retyped(self) -> None:
        """A fn-raised ``TimeoutError`` is NOT a guard breach — never re-typed."""
        guard = StoreWriteDeadlineGuard(store_label="registry")
        marker = TimeoutError("duckdb's own timeout")

        def _boom() -> None:
            raise marker

        with pytest.raises(TimeoutError) as excinfo:
            guard.run(_boom, op="upsert_expiries", deadline_seconds=5.0)
        assert excinfo.value is marker
        assert not isinstance(excinfo.value, StoreWriteDeadlineExceeded)
        assert guard.wedged is False


class TestResolver:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(STORE_WRITE_DEADLINE_ENV, raising=False)
        assert resolve_store_write_deadline_seconds() == STORE_WRITE_DEADLINE_S == 90.0

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STORE_WRITE_DEADLINE_ENV, "7.5")
        assert resolve_store_write_deadline_seconds() == 7.5

    @pytest.mark.parametrize("bad", ["", "  ", "abc", "nan", "inf", "0", "-1"])
    def test_bad_values_fail_safe(self, bad: str) -> None:
        assert resolve_store_write_deadline_seconds(bad) == STORE_WRITE_DEADLINE_S


class TestErrorClass:
    def test_message_and_attrs(self) -> None:
        exc = StoreWriteDeadlineExceeded(0.2, "registry", "upsert_contracts")
        assert str(exc) == (
            "registry DuckDB write 'upsert_contracts' not completed within 0.2s "
            "(wedged store volume); deferred to the next pull"
        )
        assert exc.deadline_seconds == 0.2
        assert exc.store == "registry"
        assert exc.op == "upsert_contracts"
        assert exc.latched is False

    def test_latched_suffix(self) -> None:
        exc = StoreWriteDeadlineExceeded(90.0, "coverage", "upsert_coverage", latched=True)
        assert str(exc).endswith("[latched fail-fast]")
        assert exc.latched is True

    def test_transient_contract_never_a_pull_wide_stop(self) -> None:
        exc = StoreWriteDeadlineExceeded(0.2, "registry", "op")
        assert isinstance(exc, DataLakeError)
        assert not isinstance(exc, LakeWriteHalt)
