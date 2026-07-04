"""SLA-honesty launch guard for the full grid sweep (DEC-SLA-LAUNCH-GUARD-001).

Before ``brutex sweep-all`` launches a grid, this module PROJECTS the sweep's
wall-clock as ``tuple_count × per-tuple seconds`` and compares it against the
CLAUDE.md §8 hardware-envelope SLA (**2-4 days** wall time on the M4 Pro
reference envelope). A projection past the envelope REFUSES the launch unless
the operator passes ``--acknowledge-sla-overrun`` — then the launch proceeds
with a LOUD warning. The decision (allowed / refused / acknowledged) is always
logged as one structured ``sweep.sla_projection`` event.

## Honest envelope (Golden Rule #10)

* :data:`SWEEP_PER_TRIAL_SECONDS` is an **UNBENCHMARKED, conservative
  placeholder ESTIMATE** — it has NOT been measured end-to-end on the §8
  reference envelope (Apple M4 Pro, 14-core, 48 GB) and MUST be re-measured
  there before the projection can be treated as calibrated. Until then the
  guard's verdict is a coarse plausibility screen, NOT a certification: the
  §8 "~33B trials in 2-4 days" SLA itself remains UNMEASURED, and this guard
  never claims it met. The operator can override the estimate per environment
  via :data:`ENV_PER_TRIAL_SECONDS` (``BRUTEX_SWEEP_PER_TRIAL_SECONDS``).
* The projection unit is one grid TUPLE (one
  ``instrument × timeframe × direction × K × SL × TP × fib-anchor`` cell —
  ``count_sweep_tuples``); a tuple's true cost varies with its bar count and
  mask population, which a single constant cannot capture. Linear-in-tuples is
  the honest first-order model, not an oracle.
* The guard costs O(1) — one multiply + one compare; it never touches the
  lake, the grid, or the sweep hot path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import structlog

logger = structlog.get_logger("brutex.engine.sweep_sla_guard")

#: The CLAUDE.md §8 production-sweep SLA envelope UPPER bound: "2-4 days wall
#: time" on the M4 Pro reference envelope — the guard projects against the
#: 4-day ceiling (a projection inside 4 days is within the stated envelope).
SLA_ENVELOPE_DAYS: Final[float] = 4.0

#: :data:`SLA_ENVELOPE_DAYS` in seconds — the comparison unit of the guard.
SLA_ENVELOPE_SECONDS: Final[float] = SLA_ENVELOPE_DAYS * 86_400.0

#: UNBENCHMARKED per-tuple wall-clock ESTIMATE (seconds) used to project a
#: grid's total wall-clock (Golden Rule #10 honesty): this value has NOT been
#: measured end-to-end on the §8 reference envelope and MUST be re-measured
#: there (a production-scale tuple evaluates its full mask population over the
#: swept window — its cost depends on bar count + mask count, not a constant).
#: 30 s/tuple is a deliberately conservative first-order placeholder so the
#: guard refuses implausibly large grids rather than blessing them. Override
#: per environment via :data:`ENV_PER_TRIAL_SECONDS`.
SWEEP_PER_TRIAL_SECONDS: Final[float] = 30.0

#: Environment variable overriding :data:`SWEEP_PER_TRIAL_SECONDS` — the
#: operator's calibration knob once a real per-tuple measurement exists for
#: their hardware. Must parse as a float ``> 0``.
ENV_PER_TRIAL_SECONDS: Final[str] = "BRUTEX_SWEEP_PER_TRIAL_SECONDS"


class SlaDecision(StrEnum):
    """The launch guard's verdict for one projected grid."""

    #: The projection fits the §8 envelope — launch proceeds silently.
    ALLOWED = "allowed"
    #: The projection exceeds the envelope and no acknowledgement was given —
    #: the launch is REFUSED.
    REFUSED = "refused"
    #: The projection exceeds the envelope but the operator passed
    #: ``--acknowledge-sla-overrun`` — the launch proceeds with a LOUD warning.
    ACKNOWLEDGED = "acknowledged"


def resolve_per_trial_seconds(env: Mapping[str, str] | None = None) -> float:
    """Return the per-tuple seconds estimate — env override, else the module constant.

    Parameters
    ----------
    env
        The environment mapping to consult (default ``os.environ``); injected
        so tests never mutate the process environment.

    Raises
    ------
    ValueError
        If :data:`ENV_PER_TRIAL_SECONDS` is set but is not a float ``> 0`` —
        a mistyped calibration knob must be a clean user-config error, never a
        silently-wrong projection.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_PER_TRIAL_SECONDS)
    if raw is None:
        return SWEEP_PER_TRIAL_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ENV_PER_TRIAL_SECONDS} must be a positive number of seconds; got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError(
            f"{ENV_PER_TRIAL_SECONDS} must be a positive number of seconds; got {raw!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class SlaProjection:
    """One launch-guard projection + verdict (immutable).

    Attributes
    ----------
    tuple_count
        The grid's tuple count (``count_sweep_tuples``).
    per_trial_seconds
        The per-tuple estimate the projection used (env-resolved).
    projected_seconds
        ``tuple_count × per_trial_seconds``.
    envelope_seconds
        The §8 envelope ceiling compared against.
    decision
        The verdict — see :class:`SlaDecision`.
    """

    tuple_count: int
    per_trial_seconds: float
    projected_seconds: float
    envelope_seconds: float
    decision: SlaDecision

    @property
    def projected_days(self) -> float:
        """The projection in days (display convenience)."""
        return self.projected_seconds / 86_400.0

    def _projection_clause(self) -> str:
        """The shared honest projection sentence both messages carry."""
        return (
            f"projected wall-clock ≈ {self.projected_days:.1f} days "
            f"({self.tuple_count} tuples × {self.per_trial_seconds:g} s/tuple, an "
            f"UNBENCHMARKED estimate — override via {ENV_PER_TRIAL_SECONDS}) exceeds "
            f"the CLAUDE.md §8 envelope of {self.envelope_seconds / 86_400.0:.0f} days"
        )

    def refusal_message(self) -> str:
        """The clear operator-facing refusal (honest: an estimate, not a certification)."""
        return (
            f"REFUSED: {self._projection_clause()}. Shrink the grid "
            "(--max-tuples / fewer symbols, intervals, K, SL/TP legs) or re-run "
            "with --acknowledge-sla-overrun to proceed deliberately. This guard "
            "projects from an estimate; it does NOT certify the §8 SLA either way."
        )

    def acknowledged_warning(self) -> str:
        """The loud proceed-anyway warning for an acknowledged overrun."""
        return (
            f"SLA OVERRUN ACKNOWLEDGED: {self._projection_clause()}. Proceeding "
            "because --acknowledge-sla-overrun was passed — expect the sweep to "
            "run past the §8 envelope (the projection is an estimate, not a "
            "certification)."
        )


def check_sweep_sla(
    tuple_count: int,
    *,
    acknowledge: bool,
    per_trial_seconds: float | None = None,
    envelope_seconds: float = SLA_ENVELOPE_SECONDS,
    env: Mapping[str, str] | None = None,
) -> SlaProjection:
    """Project a grid's wall-clock and decide allowed / refused / acknowledged.

    Emits ONE loud ``sweep.sla_projection`` structlog event (INFO when allowed,
    WARNING otherwise) carrying the tuple count, the per-tuple estimate, the
    projection, the envelope, and the decision — so every launch verdict is
    observable (Golden Rule #8 spirit; the caller additionally audit-stamps the
    launch).

    Parameters
    ----------
    tuple_count
        The grid's tuple count. Must be ``>= 0``.
    acknowledge
        ``True`` when the operator passed ``--acknowledge-sla-overrun``.
    per_trial_seconds
        Explicit per-tuple estimate; ``None`` resolves via
        :func:`resolve_per_trial_seconds` (env override → module constant).
    envelope_seconds
        The SLA ceiling (default the §8 4-day bound). Must be ``> 0``.
    env
        Environment mapping forwarded to :func:`resolve_per_trial_seconds`.

    Raises
    ------
    ValueError
        On a negative *tuple_count*, a non-positive *envelope_seconds* /
        *per_trial_seconds*, or a malformed env override.
    """
    if tuple_count < 0:
        raise ValueError(f"tuple_count must be >= 0; got {tuple_count!r}")
    if envelope_seconds <= 0:
        raise ValueError(f"envelope_seconds must be > 0; got {envelope_seconds!r}")
    if per_trial_seconds is None:
        per_trial_seconds = resolve_per_trial_seconds(env)
    elif per_trial_seconds <= 0:
        raise ValueError(f"per_trial_seconds must be > 0; got {per_trial_seconds!r}")

    projected = tuple_count * per_trial_seconds
    if projected <= envelope_seconds:
        decision = SlaDecision.ALLOWED
    elif acknowledge:
        decision = SlaDecision.ACKNOWLEDGED
    else:
        decision = SlaDecision.REFUSED

    projection = SlaProjection(
        tuple_count=tuple_count,
        per_trial_seconds=per_trial_seconds,
        projected_seconds=projected,
        envelope_seconds=envelope_seconds,
        decision=decision,
    )
    log = logger.info if decision is SlaDecision.ALLOWED else logger.warning
    log(
        "sweep.sla_projection",
        tuple_count=tuple_count,
        per_trial_seconds=per_trial_seconds,
        projected_wall_clock_seconds=projected,
        envelope_seconds=envelope_seconds,
        decision=decision.value,
        per_trial_estimate_benchmarked=False,
    )
    return projection


__all__ = [
    "ENV_PER_TRIAL_SECONDS",
    "SLA_ENVELOPE_DAYS",
    "SLA_ENVELOPE_SECONDS",
    "SWEEP_PER_TRIAL_SECONDS",
    "SlaDecision",
    "SlaProjection",
    "check_sweep_sla",
    "resolve_per_trial_seconds",
]
