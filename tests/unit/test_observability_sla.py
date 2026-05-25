"""Coverage tests for :mod:`brutex.observability.sla` — the SLA
breach detector closing 2026-05-25 audit row 19 (SLA alerting was
MISSING).

Per DEC-SLA-DETECTOR-001 (MIGRATION entry #239), this module is the
pure detector — caller-supplied callbacks decide the sink (Telegram,
audit log, dashboard, etc.). Coverage discipline: 100% line + 100%
branch + only synchronous in-process tests (no scheduler / no
threads).
"""

from __future__ import annotations

import logging
from typing import Final

import pytest
from brutex.observability.sla import (
    SLADetector,
    SLAEvent,
    SLAPredicate,
    SLARule,
    SLASeverity,
    SLAState,
    _predicate_holds,
)

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


_BREACH_LOG: Final[list[SLAEvent]] = []
_RECOVERY_LOG: Final[list[SLAEvent]] = []


def _on_breach(event: SLAEvent) -> None:
    _BREACH_LOG.append(event)


def _on_recovery(event: SLAEvent) -> None:
    _RECOVERY_LOG.append(event)


@pytest.fixture(autouse=True)
def _clear_logs() -> None:
    _BREACH_LOG.clear()
    _RECOVERY_LOG.clear()


def _make_rule(
    *,
    name: str = "test_rule",
    provider_returns: float | None = 0.0,
    predicate: SLAPredicate = SLAPredicate.GREATER_THAN,
    threshold: float = 100.0,
    severity: SLASeverity = SLASeverity.WARN,
    description: str = "synthetic test rule",
) -> SLARule:
    return SLARule(
        name=name,
        value_provider=lambda: provider_returns,
        predicate=predicate,
        threshold=threshold,
        severity=severity,
        description=description,
    )


def _make_detector(*rules: SLARule) -> SLADetector:
    return SLADetector(
        rules=rules,
        on_breach=_on_breach,
        on_recovery=_on_recovery,
    )


# ────────────────────────────────────────────────────────────────────────
# Predicate helper
# ────────────────────────────────────────────────────────────────────────


class TestPredicateHolds:
    @pytest.mark.parametrize(
        ("predicate", "value", "threshold", "expected"),
        [
            (SLAPredicate.GREATER_THAN, 101.0, 100.0, True),
            (SLAPredicate.GREATER_THAN, 99.0, 100.0, False),
            (SLAPredicate.GREATER_THAN, 100.0, 100.0, False),
            (SLAPredicate.LESS_THAN, 99.0, 100.0, True),
            (SLAPredicate.LESS_THAN, 101.0, 100.0, False),
            (SLAPredicate.LESS_THAN, 100.0, 100.0, False),
            (SLAPredicate.EQUALS, 100.0, 100.0, True),
            (SLAPredicate.EQUALS, 100.0 + 1e-12, 100.0, True),
            (SLAPredicate.EQUALS, 100.5, 100.0, False),
        ],
    )
    def test_predicate_combinations(
        self,
        predicate: SLAPredicate,
        value: float,
        threshold: float,
        *,
        expected: bool,
    ) -> None:
        assert _predicate_holds(predicate, value, threshold) is expected


# ────────────────────────────────────────────────────────────────────────
# Detector construction + state
# ────────────────────────────────────────────────────────────────────────


class TestConstructionAndState:
    def test_initial_state_for_all_rules_is_ok(self) -> None:
        rule_a = _make_rule(name="a")
        rule_b = _make_rule(name="b")
        det = _make_detector(rule_a, rule_b)
        assert det.state_of("a") == SLAState.OK
        assert det.state_of("b") == SLAState.OK

    def test_state_of_unknown_rule_raises(self) -> None:
        det = _make_detector()
        with pytest.raises(KeyError, match="No rule"):
            det.state_of("ghost")

    def test_add_rule_registers_state(self) -> None:
        det = _make_detector()
        rule = _make_rule(name="dynamic")
        det.add_rule(rule)
        assert det.state_of("dynamic") == SLAState.OK
        assert rule in det.rules

    def test_add_rule_rejects_duplicate(self) -> None:
        det = _make_detector(_make_rule(name="taken"))
        with pytest.raises(ValueError, match="already registered"):
            det.add_rule(_make_rule(name="taken"))


# ────────────────────────────────────────────────────────────────────────
# check_once — transitions + hysteresis + edge cases
# ────────────────────────────────────────────────────────────────────────


class TestCheckOnceTransitions:
    def test_no_transition_when_under_threshold(self) -> None:
        det = _make_detector(
            _make_rule(provider_returns=50.0, threshold=100.0),
        )
        emitted = det.check_once()
        assert emitted == ()
        assert _BREACH_LOG == []
        assert _RECOVERY_LOG == []

    def test_breach_fires_on_first_violation(self) -> None:
        det = _make_detector(
            _make_rule(name="cpu", provider_returns=150.0, threshold=100.0),
        )
        emitted = det.check_once()
        assert len(emitted) == 1
        assert emitted[0].new_state == SLAState.BREACH
        assert emitted[0].rule_name == "cpu"
        assert emitted[0].observed_value == 150.0
        assert emitted[0].threshold == 100.0
        assert len(_BREACH_LOG) == 1
        assert _RECOVERY_LOG == []

    def test_hysteresis_steady_breach_fires_only_once(self) -> None:
        det = _make_detector(
            _make_rule(provider_returns=150.0, threshold=100.0),
        )
        det.check_once()
        # Same value next pass → no new event
        emitted = det.check_once()
        assert emitted == ()
        assert len(_BREACH_LOG) == 1

    def test_recovery_fires_on_breach_to_ok_transition(self) -> None:
        """Inject a mutable provider so we can flip the value mid-test."""
        live_value: list[float] = [150.0]
        rule = SLARule(
            name="cpu",
            value_provider=lambda: live_value[0],
            predicate=SLAPredicate.GREATER_THAN,
            threshold=100.0,
        )
        det = _make_detector(rule)
        det.check_once()
        assert len(_BREACH_LOG) == 1
        live_value[0] = 50.0
        emitted = det.check_once()
        assert len(emitted) == 1
        assert emitted[0].new_state == SLAState.OK
        assert len(_RECOVERY_LOG) == 1

    def test_oscillation_emits_alternating_edges(self) -> None:
        live_value: list[float] = [150.0]
        rule = SLARule(
            name="cpu",
            value_provider=lambda: live_value[0],
            predicate=SLAPredicate.GREATER_THAN,
            threshold=100.0,
        )
        det = _make_detector(rule)
        for _cycle in range(3):
            live_value[0] = 150.0
            det.check_once()
            live_value[0] = 50.0
            det.check_once()
        assert len(_BREACH_LOG) == 3
        assert len(_RECOVERY_LOG) == 3

    def test_value_provider_none_skips_rule(self) -> None:
        det = _make_detector(_make_rule(provider_returns=None, threshold=100.0))
        emitted = det.check_once()
        assert emitted == ()
        assert _BREACH_LOG == []

    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError])
    def test_value_provider_exception_is_swallowed(
        self,
        exc_cls: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _raises() -> float:
            raise exc_cls("synthetic provider failure")

        rule = SLARule(
            name="bad",
            value_provider=_raises,
            predicate=SLAPredicate.GREATER_THAN,
            threshold=100.0,
        )
        det = _make_detector(rule)
        with caplog.at_level(logging.ERROR, logger="brutex.observability.sla"):
            emitted = det.check_once()
        assert emitted == ()
        assert "value_provider" in caplog.text


# ────────────────────────────────────────────────────────────────────────
# Callback failure isolation
# ────────────────────────────────────────────────────────────────────────


class TestCallbackFailuresIsolated:
    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError])
    def test_on_breach_callback_exception_does_not_propagate(
        self,
        exc_cls: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _bad(_event: SLAEvent) -> None:
            raise exc_cls("synthetic breach handler failure")

        det = SLADetector(
            rules=(_make_rule(provider_returns=150.0, threshold=100.0),),
            on_breach=_bad,
            on_recovery=_on_recovery,
        )
        with caplog.at_level(logging.ERROR, logger="brutex.observability.sla"):
            emitted = det.check_once()
        assert len(emitted) == 1  # state still transitioned
        assert "callback raised" in caplog.text

    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError])
    def test_on_recovery_callback_exception_does_not_propagate(
        self,
        exc_cls: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _bad(_event: SLAEvent) -> None:
            raise exc_cls("synthetic recovery handler failure")

        live_value: list[float] = [150.0]
        det = SLADetector(
            rules=(
                SLARule(
                    name="cpu",
                    value_provider=lambda: live_value[0],
                    predicate=SLAPredicate.GREATER_THAN,
                    threshold=100.0,
                ),
            ),
            on_breach=_on_breach,
            on_recovery=_bad,
        )
        det.check_once()  # breach
        live_value[0] = 50.0
        with caplog.at_level(logging.ERROR, logger="brutex.observability.sla"):
            det.check_once()  # recovery — bad cb raises but is caught
        assert "callback raised" in caplog.text


# ────────────────────────────────────────────────────────────────────────
# Mixed rule predicates + severities
# ────────────────────────────────────────────────────────────────────────


class TestMixedRules:
    def test_independent_rules_track_state_independently(self) -> None:
        rules = (
            _make_rule(name="a", provider_returns=150.0, threshold=100.0),
            _make_rule(name="b", provider_returns=50.0, threshold=100.0),
        )
        det = _make_detector(*rules)
        det.check_once()
        assert det.state_of("a") == SLAState.BREACH
        assert det.state_of("b") == SLAState.OK
        assert len(_BREACH_LOG) == 1
        assert _BREACH_LOG[0].rule_name == "a"

    def test_event_carries_severity_from_rule(self) -> None:
        rule = _make_rule(provider_returns=150.0, threshold=100.0, severity=SLASeverity.CRITICAL)
        det = _make_detector(rule)
        det.check_once()
        assert _BREACH_LOG[0].severity == SLASeverity.CRITICAL

    def test_less_than_predicate_breach(self) -> None:
        rule = _make_rule(provider_returns=5.0, threshold=10.0, predicate=SLAPredicate.LESS_THAN)
        det = _make_detector(rule)
        emitted = det.check_once()
        assert len(emitted) == 1
        assert emitted[0].new_state == SLAState.BREACH

    def test_equals_predicate_breach(self) -> None:
        rule = _make_rule(provider_returns=42.0, threshold=42.0, predicate=SLAPredicate.EQUALS)
        det = _make_detector(rule)
        emitted = det.check_once()
        assert len(emitted) == 1
        assert emitted[0].new_state == SLAState.BREACH
        assert emitted[0].predicate == SLAPredicate.EQUALS
