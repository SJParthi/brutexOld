"""Tunable + scaled sweep stall-timeout policy (G2/G15 — DEC-SWEEP-UNKILLABLE-CORE-001).

Both stall guards today pin a fixed 300 s constant
(:data:`brutex.engine.sweep_watchdog.DEFAULT_TIMEOUT_SECONDS` and
``brutex.engine.sweep_parallel.DEFAULT_STALL_SECONDS``), which a big grid's
legitimately-slow tuple can exceed → a FALSE abort of a healthy multi-day run
(G2). This module resolves ONE :class:`StallPolicy` at launch, fed to BOTH
guards by the run-core wiring:

1. ``BRUTEX_SWEEP_STALL_SECONDS`` — the operator override — WINS outright
   (fail-loud parse: a set-but-junk value raises, never silently defaults).
2. Otherwise the timeout SCALES with the per-tuple estimate:
   ``max(DEFAULT_STALL_BASE_SECONDS, factor × estimate)`` where *factor* is
   ``BRUTEX_SWEEP_STALL_FACTOR`` (default :data:`DEFAULT_STALL_FACTOR`,
   fail-loud) and *estimate* is the caller's ``per_tuple_estimate_s`` or —
   when the caller has none — the SLA guard's read-only
   ``BRUTEX_SWEEP_PER_TUPLE_SECONDS`` seam
   (:func:`brutex.engine.sweep_sla_guard.resolve_per_tuple_seconds`).

The I-1 identity law (byte-identity of the unscaled default): with no env
overrides and no estimate, the resolution is EXACTLY
:data:`DEFAULT_STALL_BASE_SECONDS` == 300.0 — the value both existing guards
already use (test-pinned against BOTH constants). Scaling only ever RAISES the
timeout above the base — it can NEVER lower it (a longer timeout only converts
a false abort into a completed run; the explicit env override is the sole way
to go below 300, an operator decision).

Honest envelope (Golden Rule #10): the *workers* argument is VALIDATED but
deliberately NOT used to shrink the bound — with W workers, result arrivals
interleave and the mean gap shrinks, but the WORST-case gap (all workers on
equally-slow tuples started together) is still one full tuple duration, so
dividing by W would re-open the false-abort class this module closes. The
scaled bound removes the false-abort class only as well as the estimate is
honest; real per-tuple durations vs the constant remain observation-closed in
the first supervised run (R16 honest limit).
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from brutex.engine.sweep_sla_guard import resolve_per_tuple_seconds
from brutex.engine.sweep_watchdog import DEFAULT_TIMEOUT_SECONDS

__all__ = [
    "DEFAULT_STALL_BASE_SECONDS",
    "DEFAULT_STALL_FACTOR",
    "ENV_STALL_FACTOR",
    "ENV_STALL_SECONDS",
    "StallPolicy",
    "resolve_stall_policy",
]

#: The unscaled stall-timeout floor — imported from the watchdog constant (ONE
#: source of truth, CLAUDE.md §11 COMMON) so the default resolution is
#: byte-identical to today's fixed guard (I-1). 300.0.
DEFAULT_STALL_BASE_SECONDS: Final[float] = DEFAULT_TIMEOUT_SECONDS

#: Default multiple of the per-tuple estimate the scaled timeout allows before
#: declaring a stall. 10× is a generous "something is genuinely wedged" bound,
#: not a latency SLA; with the SLA guard's conservative 30 s default estimate
#: it lands EXACTLY on the 300 s base (identity when unscaled).
DEFAULT_STALL_FACTOR: Final[float] = 10.0

#: Operator override for the absolute stall timeout (seconds). Fail-loud.
ENV_STALL_SECONDS: Final[str] = "BRUTEX_SWEEP_STALL_SECONDS"

#: Operator override for the scaling factor. Fail-loud.
ENV_STALL_FACTOR: Final[str] = "BRUTEX_SWEEP_STALL_FACTOR"

#: The three provenance labels a resolved policy may carry.
_SOURCES: Final[frozenset[str]] = frozenset({"default", "env", "scaled"})


@dataclass(frozen=True, slots=True)
class StallPolicy:
    """One resolved stall policy (immutable).

    Attributes
    ----------
    timeout_seconds
        The stall timeout both guards consume; positive and finite.
    source
        Provenance of the value: ``"default"`` (the unscaled 300 s base),
        ``"env"`` (:data:`ENV_STALL_SECONDS` override), or ``"scaled"``
        (``factor × estimate`` raised it above the base).

    Raises
    ------
    ValueError
        On a non-positive/non-finite ``timeout_seconds`` or an unknown
        ``source`` label (a construction defect must fail loudly at launch).
    """

    timeout_seconds: float
    source: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError(
                "StallPolicy.timeout_seconds must be a positive, finite number of "
                f"seconds; got {self.timeout_seconds!r}"
            )
        if self.source not in _SOURCES:
            raise ValueError(
                f"StallPolicy.source must be one of {sorted(_SOURCES)}; got {self.source!r}"
            )


def _parse_positive_finite(raw: str, *, name: str) -> float:
    """Parse *raw* as a positive, finite float — fail-loud on any junk.

    The house env-parse shape (mirrors
    :func:`brutex.engine.sweep_sla_guard.resolve_per_tuple_seconds`): a SET
    value must parse; blank / non-numeric / non-positive / ``nan`` / ``inf``
    all raise ``ValueError`` naming the variable — a mistyped stall knob must
    be a clean user-config error, never a silently-wrong guard.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive, finite number; got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive, finite number; got {raw!r}")
    return value


def resolve_stall_policy(
    *,
    per_tuple_estimate_s: float | None,
    workers: int,
    env: Mapping[str, str] | None = None,
) -> StallPolicy:
    """Resolve the launch stall policy — env override, else the raise-only scaled bound.

    Parameters
    ----------
    per_tuple_estimate_s
        The caller's per-tuple duration estimate (seconds; positive finite),
        or ``None`` to read the SLA guard's
        ``BRUTEX_SWEEP_PER_TUPLE_SECONDS`` seam
        (:func:`~brutex.engine.sweep_sla_guard.resolve_per_tuple_seconds`,
        read-only — its conservative 30 s default keeps the unscaled
        resolution at exactly the 300 s base).
    workers
        The launch worker count (``>= 1``). Validated only — deliberately
        NEVER used to LOWER the bound (module docstring; G2's law is that
        scaling only raises).
    env
        Environment mapping to consult (default ``os.environ``); injected so
        tests never mutate the process environment.

    Returns
    -------
    StallPolicy
        The resolved policy. With no env overrides and no estimate this is
        EXACTLY ``StallPolicy(300.0, "default")`` (I-1 identity).

    Raises
    ------
    ValueError
        On a set-but-junk :data:`ENV_STALL_SECONDS` /
        :data:`ENV_STALL_FACTOR` / ``BRUTEX_SWEEP_PER_TUPLE_SECONDS``, a
        non-positive/non-finite *per_tuple_estimate_s*, or ``workers < 1``.
    """
    source_env = os.environ if env is None else env
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    raw_override = source_env.get(ENV_STALL_SECONDS)
    if raw_override is not None:
        return StallPolicy(
            timeout_seconds=_parse_positive_finite(raw_override, name=ENV_STALL_SECONDS),
            source="env",
        )
    raw_factor = source_env.get(ENV_STALL_FACTOR)
    factor = (
        DEFAULT_STALL_FACTOR
        if raw_factor is None
        else _parse_positive_finite(raw_factor, name=ENV_STALL_FACTOR)
    )
    if per_tuple_estimate_s is None:
        estimate = resolve_per_tuple_seconds(source_env)
    else:
        if not math.isfinite(per_tuple_estimate_s) or per_tuple_estimate_s <= 0:
            raise ValueError(
                "per_tuple_estimate_s must be a positive, finite number of seconds; "
                f"got {per_tuple_estimate_s!r}"
            )
        estimate = per_tuple_estimate_s
    scaled = factor * estimate
    if scaled > DEFAULT_STALL_BASE_SECONDS:
        return StallPolicy(timeout_seconds=scaled, source="scaled")
    return StallPolicy(timeout_seconds=DEFAULT_STALL_BASE_SECONDS, source="default")
