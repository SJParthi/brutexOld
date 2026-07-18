"""SLA breach detector — closes the 2026-05-25 honest-audit row 19
(SLA breach alerting was MISSING).

Per the 5-agent audit (``docs/STANDARDS_100PCT_HONEST_AUDIT.md``) the
existing observability stack has metrics (`brutex.observability.metrics`)
and a Telegram channel (`brutex.notifications.telegram`) wired now via
DEC-TELEGRAM-WIRING-001 (entry #238), but there is **no detector** that
watches SLA thresholds and fires alerts on breach. The watchdog
promised in CLAUDE.md §11 AUTOMATED (Release Please + auto-merge +
watchdog + Telegram + auto-resume) lands in a separate PR — this
module is the SLA-watch half of that promise.

## Design — rule-driven, hysteresis-aware, value-provider-agnostic

Each :class:`SLARule` carries:

* ``name`` — operator-facing identifier (also the dedupe key for
  Telegram).
* ``value_provider`` — ``Callable[[], float]`` (NOT a metric-name
  string). Decouples the detector from any specific value source —
  tests pass a closure, production callers pass
  ``lambda: registry.gauge_value(name)`` or a queue-depth probe.
* ``predicate`` — ``GREATER_THAN`` / ``LESS_THAN`` / ``EQUALS``.
  Value-vs-threshold comparison.
* ``threshold`` — numeric trigger level.
* ``severity`` — :class:`SLASeverity` (info, warn, error, critical) for
  downstream routing.

:class:`SLADetector` holds the rules + per-rule state (OK vs BREACH)
and exposes a single :meth:`check_once` method. Each invocation:

1. Calls every rule's ``value_provider``.
2. Evaluates the predicate.
3. On state TRANSITION (OK→BREACH or BREACH→OK), fires the relevant
   callback. **Hysteresis is built in** — a stable BREACH state
   does NOT spam alerts; only the edge fires.

This isolates the WHEN (caller's polling cadence) from the WHAT
(detector logic) from the WHERE (callback sinks — Telegram, stdout,
audit log, etc.). The detector itself is pure + deterministic, which
makes unit testing exhaustive.

## Why hysteresis matters

Without it, a metric oscillating around the threshold (e.g., queue
depth at 99.9 vs 100.1) would emit a constant stream of breach +
recovery events — Telegram would block on the 100-msg/sec rate
limit, and the audit log would balloon. Hysteresis collapses this
into a single edge transition per real change.

## Decision: DEC-SLA-DETECTOR-001 (MIGRATION entry #239).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from brutex.observability import observability_rust

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Enums + dataclasses
# ────────────────────────────────────────────────────────────────────────


class SLAState(StrEnum):
    """Per-rule state. Hysteresis means the detector tracks this
    explicitly so we only fire callbacks on state TRANSITIONS, not on
    every steady-state check."""

    OK = "ok"
    BREACH = "breach"


class SLAPredicate(StrEnum):
    """How the rule compares the observed value against the threshold."""

    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    EQUALS = "equals"


class SLASeverity(StrEnum):
    """Severity for downstream routing.

    Mirrors :class:`brutex.notifications.telegram.TelegramSeverity` so
    callers can pass through unchanged. Distinct enum is preferred
    over reusing TelegramSeverity because the SLA detector must not
    depend on the notification layer (one-way dependency).
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


#: A function that returns the current observed value for a rule.
#: Returning ``None`` indicates "value not available yet" — the
#: detector skips the rule on that pass without changing state.
ValueProvider = Callable[[], float | None]


@dataclass(frozen=True, slots=True)
class SLARule:
    """One SLA threshold rule.

    Attributes
    ----------
    name
        Operator-facing identifier. Used as the Telegram dedupe key
        and the breach-event ``rule_name``.
    value_provider
        Callable returning the current observed value (or ``None``
        to skip this check pass).
    predicate
        Comparison operator (greater-than / less-than / equals).
    threshold
        Numeric trigger value.
    severity
        :class:`SLASeverity` label for downstream routing.
    description
        Optional human-readable description — included in the
        breach event for the chat message.
    """

    name: str
    value_provider: ValueProvider
    predicate: SLAPredicate
    threshold: float
    severity: SLASeverity = SLASeverity.WARN
    description: str = ""


@dataclass(frozen=True, slots=True)
class SLAEvent:
    """Emitted to the callbacks on every state transition.

    Carries both the rule + observed value + severity so the callback
    can build whatever payload it needs (Telegram message, audit log
    entry, dashboard signal, etc.) without re-reading the rule
    registry.
    """

    rule_name: str
    severity: SLASeverity
    description: str
    observed_value: float
    threshold: float
    predicate: SLAPredicate
    new_state: SLAState


#: Callback signature for breach + recovery events.
SLAEventCallback = Callable[[SLAEvent], None]


# ────────────────────────────────────────────────────────────────────────
# Detector
# ────────────────────────────────────────────────────────────────────────


_EPS: Final[float] = 1e-9


def _predicate_holds(predicate: SLAPredicate, value: float, threshold: float) -> bool:
    """Return True if (value, threshold) violates the predicate."""
    if predicate == SLAPredicate.GREATER_THAN:
        return value > threshold
    if predicate == SLAPredicate.LESS_THAN:
        return value < threshold
    # EQUALS — float-safe via tiny epsilon
    return abs(value - threshold) < _EPS


def _sla_decide_python(
    predicate: SLAPredicate,
    value: float,
    threshold: float,
    prev_breach: bool,
) -> tuple[bool, bool]:
    """The SLA decision — the LIVE parity oracle over :func:`_predicate_holds`.

    A disclosed COMPOSITION of the loop-inlined lines of
    :meth:`SLADetector.check_once` (DEC-RUST-OBSERVABILITY-001 — the
    Slice-0 exemption): ``breached`` per the untouched
    :func:`_predicate_holds` component, ``edge = breached != prev_breach``
    (≡ ``new_state != prev_state`` over the 2-member :class:`SLAState`).
    Flag-OFF the seam returns this function's result DIRECTLY — the default
    path is byte-identical.
    """
    breached = _predicate_holds(predicate, value, threshold)
    return breached, breached != prev_breach


@dataclass(slots=True)
class SLADetector:
    """Watches a set of SLA rules; fires callbacks on state transitions.

    Designed for synchronous polling — the caller invokes
    :meth:`check_once` on whatever cadence makes sense (every N
    seconds via a scheduler, every K phase transitions via the
    pipeline subscriber, etc.). The detector is NOT a thread / not a
    timer / not a daemon.

    Parameters
    ----------
    rules
        Initial :class:`SLARule` set. Pass an empty tuple + use
        :meth:`add_rule` if rules are discovered dynamically.
    on_breach
        Callback fired when a rule transitions OK → BREACH.
    on_recovery
        Callback fired when a rule transitions BREACH → OK.

    Notes
    -----
    Callback exceptions are caught + logged. A buggy callback never
    crashes the detector — Golden Rule #10.
    """

    rules: tuple[SLARule, ...]
    on_breach: SLAEventCallback
    on_recovery: SLAEventCallback
    _state: dict[str, SLAState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Every rule starts in OK state.
        for rule in self.rules:
            self._state.setdefault(rule.name, SLAState.OK)

    def add_rule(self, rule: SLARule) -> None:
        """Append a rule and initialise its state to OK."""
        if rule.name in self._state:
            raise ValueError(f"SLARule with name {rule.name!r} is already registered")
        # Frozen-tuple rules sequence is rebuilt; cheap for small N.
        self.rules = (*self.rules, rule)
        self._state[rule.name] = SLAState.OK

    def state_of(self, rule_name: str) -> SLAState:
        """Return the current per-rule state (OK or BREACH)."""
        if rule_name not in self._state:
            raise KeyError(f"No rule named {rule_name!r}")
        return self._state[rule_name]

    def check_once(self) -> tuple[SLAEvent, ...]:
        """Evaluate every rule and fire callbacks on state transitions.

        Returns
        -------
        tuple[SLAEvent, ...]
            The events fired this pass (may be empty if nothing
            transitioned). Useful for tests + audit logging.
        """
        emitted: list[SLAEvent] = []
        for rule in self.rules:
            try:
                value = rule.value_provider()
            except (OSError, ValueError, RuntimeError):
                logger.exception(
                    "SLADetector.check_once: value_provider for rule %r raised; skipping this pass",
                    rule.name,
                )
                continue
            if value is None:
                continue  # provider says "not available yet"
            # The decision routes through the default-OFF observability_rust
            # facade with `_sla_decide_python` as the live oracle
            # (DEC-RUST-OBSERVABILITY-001; branch-free here — all
            # flag/guard/burn-in branching lives facade-side). prev_state is
            # read BEFORE the decision (a side-effect-free dict read; the
            # ONE disclosed re-ordering vs the pre-seam loop).
            prev_state = self._state[rule.name]
            breached, edge = observability_rust.routed_sla_decide(
                rule.predicate,
                value,
                rule.threshold,
                prev_state == SLAState.BREACH,
                python_law=_sla_decide_python,
            )
            new_state = SLAState.BREACH if breached else SLAState.OK
            if not edge:
                continue  # hysteresis: steady state, no edge
            self._state[rule.name] = new_state
            event = SLAEvent(
                rule_name=rule.name,
                severity=rule.severity,
                description=rule.description,
                observed_value=value,
                threshold=rule.threshold,
                predicate=rule.predicate,
                new_state=new_state,
            )
            emitted.append(event)
            try:
                cb = self.on_breach if new_state == SLAState.BREACH else self.on_recovery
                cb(event)
            except (OSError, ValueError, RuntimeError):
                logger.exception(
                    "SLADetector.check_once: callback raised for rule %r → %s; detector continues",
                    rule.name,
                    new_state.value,
                )
        return tuple(emitted)


__all__ = (
    "SLADetector",
    "SLAEvent",
    "SLAEventCallback",
    "SLAPredicate",
    "SLARule",
    "SLASeverity",
    "SLAState",
    "ValueProvider",
)
