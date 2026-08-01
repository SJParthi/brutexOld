"""The BRUTEX_BACKUP_RUST seam legs for :mod:`brutex.backup.deadman`.

DEC-BACKUP-RUST-OPS-001. Covers the routed :func:`evaluate_deadman` wrapper,
the decision→report materialiser (every arm + every refuse-poison gate) and
the bit-packing burn-in key — with FAKE crates (deterministic decision
tuples) and, when the wheel is installed, the REAL ``brutex_ops`` kernel
(flag-ON parity vs the retained Python oracle across the verdict matrix).
The oracle itself stays pinned by the UNMODIFIED ``test_backup_deadman.py``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from brutex.backup import backup_rust, deadman
from brutex.backup.deadman import DeadmanReport, DeadmanVerdict, evaluate_deadman
from brutex.backup.state import BackupState, DeadmanState, empty_backup_state
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)

THRESHOLDS = {
    "threshold_hours": 26.0,
    "realert_hours": 6.0,
    "skew_tolerance_s": 300.0,
    "max_runtime_hours": 48.0,
}


@pytest.fixture(autouse=True)
def _fresh_seam() -> Iterator[None]:
    backup_rust.reset_backup_rust_for_tests()
    yield
    backup_rust.reset_backup_rust_for_tests()


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")


@pytest.fixture
def strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the degrade-to-oracle contract to the mode that still HAS one.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` default-ON,
    where a Class-A (crate absent/stale/errored) or Class-B (crate answered
    but the RESULT is structurally refused) fall-back RAISES
    :class:`RustFallbackRefused` at the seam's choke point instead of serving
    the Python oracle.  The degrade machinery is not deleted — it stays
    reachable through the documented ``BRUTEX_RUST_STRICT=0`` kill switch,
    and THAT is the mode the tests requesting this fixture describe.  The
    strict-ON refusal for the SAME induced fault is pinned positively by
    :class:`TestStrictModeRefusesTheFallback`, so no coverage is traded away.

    Uses the SHARED ``monkeypatch`` (never a module-scoped
    ``MonkeyPatch.context()``) so set/unset ordering with sibling env pins in
    this file stays deterministic.
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")


def _clock() -> datetime:
    return NOW


def _install_fake(monkeypatch: pytest.MonkeyPatch, decision: object) -> None:
    """Install a fake crate whose deadman decision is the fixed *decision*."""
    module = types.ModuleType("brutex_ops")
    for name in backup_rust._CRATE_API:
        setattr(module, name, lambda *a, **k: None)
    # setattr (not `module.attr = ...`): pyright's reportAttributeAccessIssue
    # refuses direct unknown-attribute assignment on ModuleType — the loop
    # idiom above is the structural, ignore-free spelling (CI review-round fix).
    setattr(module, "evaluate_deadman_decision", lambda *a, **k: decision)  # noqa: B010
    monkeypatch.setitem(sys.modules, "brutex_ops", module)


def _oracle(
    state: BackupState | None, sidecar: DeadmanState, state_error: str | None = None
) -> DeadmanReport:
    return deadman._evaluate_deadman_python(
        state, sidecar, clock=_clock, state_error=state_error, **THRESHOLDS
    )


def _routed(
    state: BackupState | None, sidecar: DeadmanState, state_error: str | None = None
) -> DeadmanReport:
    return evaluate_deadman(state, sidecar, clock=_clock, state_error=state_error, **THRESHOLDS)


def _mk_state(
    *,
    running_since: datetime | None = None,
    last_success_at: datetime | None = None,
) -> BackupState:
    base = empty_backup_state()
    return BackupState(
        schema=base.schema,
        running_since=running_since,
        last_attempt_at=None,
        last_success_at=last_success_at,
        last_snapshot_id=None,
        last_failure_at=None,
        last_failure_reason=None,
        consecutive_failures=0,
        consecutive_partials=0,
        maintenance_ok=True,
        verify_subset_cursor=0,
        lake_device=None,
    )


#: The verdict-matrix scenarios (state, sidecar, state_error) — one per arm.
SCENARIOS: dict[str, tuple[BackupState | None, DeadmanState, str | None]] = {
    "state_corrupt": (None, DeadmanState(), "bad json"),
    "never_no_file": (None, DeadmanState(), None),
    "clock_skew_running": (
        _mk_state(running_since=NOW + timedelta(seconds=301)),
        DeadmanState(),
        None,
    ),
    "running": (
        _mk_state(running_since=NOW - timedelta(hours=1), last_success_at=NOW - timedelta(hours=2)),
        DeadmanState(),
        None,
    ),
    "hung": (
        _mk_state(running_since=NOW - timedelta(hours=49)),
        DeadmanState(),
        None,
    ),
    "never_no_stamp": (_mk_state(), DeadmanState(), None),
    "clock_skew_success": (
        _mk_state(last_success_at=NOW + timedelta(seconds=301)),
        DeadmanState(),
        None,
    ),
    "overdue": (
        _mk_state(last_success_at=NOW - timedelta(hours=27)),
        DeadmanState(),
        None,
    ),
    "fresh": (
        _mk_state(last_success_at=NOW - timedelta(hours=1)),
        DeadmanState(),
        None,
    ),
    "throttled_alert": (
        None,
        DeadmanState(last_overdue_alert_at=NOW - timedelta(hours=1)),
        None,
    ),
    "future_alert_stamp": (
        None,
        DeadmanState(last_overdue_alert_at=NOW + timedelta(hours=1)),
        None,
    ),
}


class TestOffArmIdentity:
    """Kill switch (``"0"``): the wrapper serves the oracle byte-identically, crate untouched.

    Retargeted from the pre-flip unset arm onto the explicit falsy kill
    switch (DEC-RUST-DEFAULT-ON-WAVE-001) — assertions unchanged.
    """

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_off_equals_oracle(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_ops", None)  # the bomb: import → ImportError
        state, sidecar, state_error = SCENARIOS[name]
        assert _routed(state, sidecar, state_error) == _oracle(state, sidecar, state_error)
        assert backup_rust.disabled_reason() is None, "OFF must never touch the crate"

    def test_wrapper_calls_the_clock_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(backup_rust.BACKUP_RUST_ENV, raising=False)
        calls: list[int] = []

        def _counting_clock() -> datetime:
            calls.append(1)
            return NOW

        # `state_error=None` explicit (the _oracle/_routed call shape): without
        # it pyright reads `**THRESHOLDS` (dict[str, float]) as possibly
        # supplying the `state_error: str | None` keyword (CI review-round fix).
        evaluate_deadman(
            None, DeadmanState(), clock=_counting_clock, state_error=None, **THRESHOLDS
        )
        assert len(calls) == 1


class TestFakeCrateRouting:
    def test_correct_fake_decision_burn_in_matches(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        # NEVER_SUCCEEDED (no file): arm 2, alert True, no hours.
        _install_fake(monkeypatch, (2, "never_succeeded", True, None, 0.0))
        report = _routed(None, DeadmanState())
        assert report == _oracle(None, DeadmanState())
        assert backup_rust.disabled_reason() is None
        assert backup_rust.burn_in_remaining("evaluate_deadman") == backup_rust.BURN_IN_SAMPLES - 1

    def test_drifted_fake_decision_autoreverts_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, strict_off: None
    ) -> None:
        # A WRONG verdict (fresh where the oracle says never_succeeded).
        _install_fake(monkeypatch, (9, "fresh", False, None, 0.0))
        report = _routed(None, DeadmanState())
        assert report == _oracle(None, DeadmanState())
        assert backup_rust.disabled_reason() == "burn_in_mismatch"

    def test_poisoned_decision_shape_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, strict_off: None
    ) -> None:
        _install_fake(monkeypatch, (2, "never_succeeded", 1, None, 0.0))  # int, not bool
        report = _routed(None, DeadmanState())
        assert report == _oracle(None, DeadmanState())
        assert backup_rust.disabled_reason() is None, "refused, not latched"


class TestStrictModeRefusesTheFallback:
    """The no-fallback guarantee, pinned POSITIVELY on this facade.

    DEC-RUST-STRICT-NO-FALLBACK-001.  Each test induces the SAME fault its
    ``strict_off`` twin in :class:`TestFakeCrateRouting` induces, but leaves
    ``BRUTEX_RUST_STRICT`` at its shipped default (ON) and asserts the seam
    RAISES rather than quietly serving the Python oracle.  Without these the
    ``strict_off`` pins above would be a coverage trade, not a relocation.
    """

    def test_drifted_decision_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        _install_fake(monkeypatch, (9, "fresh", False, None, 0.0))
        with pytest.raises(RustFallbackRefused, match="burn_in_mismatch"):
            _routed(None, DeadmanState())

    def test_poisoned_decision_shape_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        _install_fake(monkeypatch, (2, "never_succeeded", 1, None, 0.0))
        with pytest.raises(RustFallbackRefused):
            _routed(None, DeadmanState())


class TestReportFromDecision:
    """Direct materialiser matrix — every arm + every refuse-poison gate."""

    def _materialise(
        self,
        decision: object,
        *,
        state: BackupState | None = None,
        state_error: str | None = None,
    ) -> DeadmanReport:
        return deadman._report_from_decision(
            decision,
            state=state,
            now=NOW,
            threshold_hours=26.0,
            max_runtime_hours=48.0,
            state_error=state_error,
        )

    def test_every_arm_matches_the_oracle_report(self) -> None:
        """For each scenario: oracle-derived decision → identical report."""
        for name, (state, sidecar, state_error) in sorted(SCENARIOS.items()):
            oracle = _oracle(state, sidecar, state_error)
            decision = _decision_from_oracle(oracle, state, state_error)
            report = self._materialise(decision, state=state, state_error=state_error)
            assert report == oracle, name

    def test_state_corrupt_without_error_is_refused(self) -> None:
        with pytest.raises(ValueError, match="STATE_CORRUPT arm"):
            self._materialise((1, "state_corrupt", True, None, 0.0), state_error=None)

    def test_running_family_without_stamp_is_refused(self) -> None:
        with pytest.raises(ValueError, match="running-family arm"):
            self._materialise((4, "running", False, None, 0.0), state=_mk_state())

    def test_success_skew_without_stamp_is_refused(self) -> None:
        with pytest.raises(ValueError, match="success-skew arm"):
            self._materialise((7, "clock_skew", True, -0.1, 0.0), state=_mk_state())

    def test_unknown_arm_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown deadman decision arm"):
            self._materialise((42, "fresh", False, None, 0.0))

    def test_unknown_verdict_token_is_refused(self) -> None:
        with pytest.raises(ValueError):
            self._materialise((9, "not-a-verdict", False, None, 0.0))

    @pytest.mark.parametrize(
        "decision",
        [
            "not-a-tuple-of-five",
            (True, "fresh", False, None, 0.0),  # bool arm
            (9.0, "fresh", False, None, 0.0),  # float arm
            (9, 42, False, None, 0.0),  # non-str verdict
            (9, "fresh", "yes", None, 0.0),  # non-bool alert
            (9, "fresh", False, "1.0", 0.0),  # non-float hours
            (9, "fresh", False, None, "0.0"),  # non-float elapsed
        ],
    )
    def test_poisoned_decision_types_are_refused(self, decision: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            self._materialise(decision)


class TestOutcomeKey:
    def test_key_bit_compares_floats(self) -> None:
        a = DeadmanReport(
            verdict=DeadmanVerdict.FRESH,
            hours_since_success=0.0,
            threshold_hours=26.0,
            should_alert=False,
            detail="d",
        )
        b = DeadmanReport(
            verdict=DeadmanVerdict.FRESH,
            hours_since_success=-0.0,
            threshold_hours=26.0,
            should_alert=False,
            detail="d",
        )
        assert a == b, "dataclass equality cannot see the sign"
        assert deadman._deadman_outcome_key(("ok", a)) != deadman._deadman_outcome_key(("ok", b))

    def test_key_none_hours(self) -> None:
        report = DeadmanReport(
            verdict=DeadmanVerdict.NEVER_SUCCEEDED,
            hours_since_success=None,
            threshold_hours=26.0,
            should_alert=True,
            detail="d",
        )
        key = deadman._deadman_outcome_key(("ok", report))
        assert isinstance(key, tuple)

    def test_key_delegates_for_non_report_payloads(self) -> None:
        outcome: tuple[str, object] = ("raise", deadman.BackupError("x"))
        assert deadman._deadman_outcome_key(outcome) == backup_rust.default_outcome_key(outcome)

    def test_now_minus_seconds_none(self) -> None:
        assert deadman._now_minus_seconds(NOW, None) is None
        assert deadman._now_minus_seconds(NOW, NOW - timedelta(hours=1)) == 3600.0

    def test_pack_float_none(self) -> None:
        assert deadman._pack_float(None) is None
        assert deadman._pack_float(0.0) != deadman._pack_float(-0.0)


def _decision_from_oracle(
    report: DeadmanReport, state: BackupState | None, state_error: str | None
) -> tuple[int, str, bool, float | None, float]:
    """Derive the primitive decision tuple an honest crate would return."""
    verdict = report.verdict
    if state_error is not None:
        arm = 1
    elif state is None:
        arm = 2
    elif state.running_since is not None:
        arm = {
            DeadmanVerdict.CLOCK_SKEW: 3,
            DeadmanVerdict.RUNNING: 4,
            DeadmanVerdict.HUNG: 5,
        }[verdict]
    elif verdict is DeadmanVerdict.NEVER_SUCCEEDED:
        arm = 6
    elif verdict is DeadmanVerdict.CLOCK_SKEW:
        arm = 7
    elif verdict is DeadmanVerdict.OVERDUE:
        arm = 8
    else:
        arm = 9
    elapsed = 0.0
    if arm in (8, 9):
        hours = report.hours_since_success
        elapsed = max(0.0, hours if hours is not None else 0.0)
    return (arm, verdict.value, report.should_alert, report.hours_since_success, elapsed)


# ── REAL-wheel legs (skipped when the crate is not installed) ──────────────


class TestRealWheelParity:
    @pytest.fixture(autouse=True)
    def _require_wheel(self) -> None:
        pytest.importorskip("brutex_ops")

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_flag_on_matches_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, name: str
    ) -> None:
        state, sidecar, state_error = SCENARIOS[name]
        routed = _routed(state, sidecar, state_error)
        assert routed == _oracle(state, sidecar, state_error)
        assert backup_rust.disabled_reason() is None, "real kernel must burn in clean"

    def test_steady_state_serves_the_kernel_decision(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        backup_rust.reset_backup_rust_for_tests(remaining=0)
        state, sidecar, state_error = SCENARIOS["overdue"]
        routed = _routed(state, sidecar, state_error)
        assert routed == _oracle(state, sidecar, state_error)
        assert backup_rust.disabled_reason() is None
