"""Live run tripwires — the R39 real-time self-audit engine (DEC-SWEEP-UNKILLABLE-CORE-001).

Watches a running brute-force sweep through three fail-soft observation
seams and latches the run SUSPECT the moment any tripwire fires (design
D-12), so a bad run is caught at minute ~12, never discovered at hour 40
(requirement R39):

* :meth:`TripwireMonitor.on_progress` — interval-gated **count
  reconciliation** over the EXACT :class:`~brutex.engine.sweep_progress.SweepProgressSnapshot`
  counters (design D-9 — never flight-recorder record counts, which are
  decimated), plus the **throughput-floor** and **memory** (RSS budget +
  per-tuple growth slope) trend watches.
* :meth:`TripwireMonitor.on_tuple_summary` — **NaN / non-finite** and
  **absurd-magnitude** detectors over per-tuple metric-summary floats.
* :meth:`TripwireMonitor.on_stage` — **stage-transition invariants**: no
  stage entry without its prerequisite stamps
  (:data:`STAGE_PREREQUISITES`; "no gate-2 entry without gate-1 stamp").

## Fail-soft law (design I-3)

Every observation method body runs under ONE lock + a blanket
``try/except`` (the :meth:`~brutex.observability.pull_health.PullHealthMonitor.observe`
idiom) — a tripwire-engine failure NEVER changes sweep results or kills
the run. The single deliberate exception is the SUSPECT latch itself: a
plain set-once ``bool`` write that cannot fail (design D-12). The injected
``on_trip`` callable carries the recorder / manifest / alerter fan-out and
is guarded, so a failing sink can never re-enter the sweep.

## Edge-triggered, dwell-gated firing

A tripwire fires on the breach EDGE (once per distinct breach key), never
per sample — the trend watches additionally require the breach to hold for
a configured number of consecutive reconciliation samples (``dwell``)
before firing, so heterogeneous tuple durations do not false-positive
(design §10 risk 5). A recovered breach re-arms its key, so a genuine
recurrence fires again. Fired events land in a bounded ring
(:data:`EVENTS_RING_CAP`) read via :attr:`TripwireMonitor.events` — the
manifest's ``tripwires[]`` source.

## Memory probe honesty (design D-16)

The default RSS probe reads ``resource.getrusage(...).ru_maxrss`` and
normalizes the platform unit divergence (macOS reports BYTES, Linux
reports KiB — :func:`normalized_ru_maxrss_bytes`). A probe failure
disables the memory tripwires for the run with ONE WARN — a reading is
never fabricated (Golden Rule #1). ``ru_maxrss`` is a monotone
high-water mark: the watch sees leak-style growth, not leak-then-free
(honest envelope, design §10).

## O(1) discipline

Every observation is O(1) in the run size (a fixed set of counter
comparisons per tick; ``on_tuple_summary`` is O(keys) in its own small
mapping) — bench-pinned flat vs ``enumerated_total`` by
``tests/bench/test_run_tripwires_o1.py``.

## Rust-routed decision laws (DEC-RUST-OBSERVABILITY-TRIPWIRES-002)

The count-reconciliation, throughput-floor/dwell, memory-budget,
memory-slope, NaN/absurd summary-screen, stage-prerequisite and
``ru_maxrss``-normalization DECISION laws route
through the default-OFF :mod:`brutex.observability.observability_rust`
facade (``BRUTEX_OBSERVABILITY_RUST``), with the module-level ``_*_python``
oracles below as the LIVE flag-OFF implementations — flag OFF (the
default) is byte-identical, and every fire/clear application, detail
template, ``values`` dict, enable/interval/state-existence gate and state
write stays Python. The facade is TOTAL over ``Exception`` (it returns a
value — crate or oracle — for every ``Exception``-class fault), so the I-3
blanket ``except`` above never observes a facade-internal failure; a
``MemoryError`` re-raised RAW by the facade IS caught by the blanket and
folds to ``observe_failed`` — the documented OOM-landing INVERSION vs the
loop-watchdog lane, identical on both arms.
"""

from __future__ import annotations

import contextlib
import math
import os
import resource
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import structlog

from brutex.config.hardware_envelope import detect_total_ram_bytes
from brutex.engine.sweep_progress import STATE_DONE
from brutex.observability import observability_rust

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.engine.sweep_progress import SweepProgressSnapshot

_LOG = structlog.get_logger(__name__)

#: Capacity of the fired-events ring (:attr:`TripwireMonitor.events`) — the
#: last 64 events are retained for the manifest / diagnostic bundle; the
#: SUSPECT latch itself is never lost regardless of ring churn.
EVENTS_RING_CAP: Final[int] = 64

#: Event severities (string vocabulary — mirrors the alerting tiers).
SEVERITY_WARNING: Final[str] = "warning"
SEVERITY_CRITICAL: Final[str] = "critical"

#: Env knobs consumed by :meth:`TripwireConfig.from_env` (fail-loud parse —
#: junk raises ``ValueError`` BEFORE the run starts, never mid-run).
ENV_RECONCILE_INTERVAL_S: Final[str] = "BRUTEX_SWEEP_TRIPWIRE_RECONCILE_INTERVAL_S"
ENV_THROUGHPUT_FLOOR: Final[str] = "BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_FLOOR"
ENV_THROUGHPUT_DWELL: Final[str] = "BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_DWELL"
ENV_RSS_BUDGET_FRACTION: Final[str] = "BRUTEX_SWEEP_TRIPWIRE_RSS_BUDGET_FRACTION"
ENV_MEMORY_SLOPE_BYTES: Final[str] = "BRUTEX_SWEEP_TRIPWIRE_MEMORY_SLOPE_BYTES"
ENV_MEMORY_DWELL: Final[str] = "BRUTEX_SWEEP_TRIPWIRE_MEMORY_DWELL"
ENV_ABSURD_PNL_PAISA: Final[str] = "BRUTEX_SWEEP_TRIPWIRE_ABSURD_PNL_PAISA"

#: Default metric-summary keys screened for ABSURD magnitude (paisa scale).
#: Keys ABSENT from a summary are ignored — absent is never fabricated as 0
#: (the D-8 absent-key law's sibling). Every value is ALSO screened for
#: NaN/inf regardless of name.
DEFAULT_ABSURD_VALUE_NAMES: Final[tuple[str, ...]] = ("net_pnl_paisa", "max_dd_paisa")


class TripwireKind(StrEnum):
    """The tripwire vocabulary — one member per R39 detector."""

    #: SweepProgress counter identity violated (D-9 exact reconciliation).
    COUNT_RECONCILIATION = "count_reconciliation"
    #: A metric-summary float is NaN / ±inf.
    NAN_METRIC = "nan_metric"
    #: A screened metric-summary magnitude exceeds ``absurd_pnl_paisa``.
    ABSURD_VALUE = "absurd_value"
    #: Throughput held below ``floor_fraction × peak`` for ``dwell`` samples.
    THROUGHPUT_FLOOR = "throughput_floor"
    #: RSS exceeded ``rss_budget_fraction × total RAM``.
    MEMORY_BUDGET = "memory_budget"
    #: RSS grew faster than ``memory_slope_bytes_per_tuple`` for ``dwell`` samples.
    MEMORY_SLOPE = "memory_slope"
    #: A stage was entered without its prerequisite stamps.
    STAGE_INVARIANT = "stage_invariant"


#: Stage → prerequisite stamp names required BEFORE that stage may start
#: (the R39 "no gate-2 entry without gate-1 stamp" invariant; stamp names
#: follow the design §3 stamped-artifact vocabulary). Stages absent from
#: this map have no prerequisite and are tolerated silently (forward-compat
#: for new link names — an unknown stage must never false-trip).
STAGE_PREREQUISITES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "preflight": frozenset(),
        "sweep": frozenset(),
        "stage3": frozenset({"stage2"}),
        "cascade": frozenset({"stage3"}),
        "artifacts": frozenset({"cascade_manifest", "cascade_report"}),
    }
)


@dataclass(frozen=True, slots=True)
class TripwireEvent:
    """One fired tripwire (immutable record — the ring / manifest / fan-out unit).

    Attributes
    ----------
    ts_utc
        Wall-clock UNIX timestamp at firing (observability only — never part
        of any identity, design I-2).
    name
        The :class:`TripwireKind` value that fired.
    severity
        :data:`SEVERITY_WARNING` or :data:`SEVERITY_CRITICAL`.
    detail
        Human-readable one-line diagnosis (exact numbers included).
    values
        The numeric observations behind the trip (floats; NaN preserved).
    """

    ts_utc: float
    name: str
    severity: str
    detail: str
    values: dict[str, float]


def _parse_env_float(src: Mapping[str, str], key: str) -> float | None:
    """Parse an optional float env knob — junk raises ``ValueError`` (fail-loud)."""
    raw = src.get(key, "")
    if raw.strip() == "":
        return None
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{key}={raw!r} is not a valid number") from exc


def _parse_env_int(src: Mapping[str, str], key: str) -> int | None:
    """Parse an optional int env knob — junk raises ``ValueError`` (fail-loud)."""
    raw = src.get(key, "")
    if raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{key}={raw!r} is not a valid integer") from exc


@dataclass(frozen=True, slots=True)
class TripwireConfig:
    """Tripwire thresholds (validated at construction — an invalid config fails fast).

    Attributes
    ----------
    reconcile_interval_s
        Minimum seconds (on the injected monotonic clock) between two
        reconciliation samples; must be ``> 0``. The first
        :meth:`TripwireMonitor.on_progress` call always samples.
    throughput_floor_fraction
        Trip when the observed rate stays below this fraction of the peak
        observed rate; in ``[0, 1)`` — ``0`` disables the throughput watch.
    throughput_dwell
        Consecutive below-floor samples required before the throughput
        tripwire fires (``>= 1``).
    rss_budget_fraction
        Trip when RSS exceeds this fraction of total physical RAM; in
        ``[0, 1]`` — ``0`` disables the budget watch.
    memory_slope_bytes_per_tuple
        Trip when RSS grows faster than this many bytes per evaluated combo
        for ``memory_dwell`` consecutive samples; ``>= 0`` — ``0`` disables
        the slope watch.
    memory_dwell
        Consecutive over-slope samples required before the memory-slope
        tripwire fires (``>= 1``).
    absurd_pnl_paisa
        Absolute paisa magnitude STRICTLY above which a screened summary
        value is absurd (``> 0``).
    absurd_value_names
        The metric-summary keys screened for absurd magnitude (every value
        is screened for NaN/inf regardless).
    """

    reconcile_interval_s: float = 60.0
    throughput_floor_fraction: float = 0.2
    throughput_dwell: int = 5
    rss_budget_fraction: float = 0.85
    memory_slope_bytes_per_tuple: int = 8 * 2**20
    memory_dwell: int = 5
    absurd_pnl_paisa: int = 10**15
    absurd_value_names: tuple[str, ...] = DEFAULT_ABSURD_VALUE_NAMES

    def __post_init__(self) -> None:
        """Validate every threshold — an impossible config raises immediately."""
        if not self.reconcile_interval_s > 0:
            raise ValueError(f"reconcile_interval_s must be > 0, got {self.reconcile_interval_s!r}")
        if not 0 <= self.throughput_floor_fraction < 1:
            raise ValueError(
                "throughput_floor_fraction must be in [0, 1) (0 disables), "
                f"got {self.throughput_floor_fraction!r}"
            )
        if self.throughput_dwell < 1:
            raise ValueError(f"throughput_dwell must be >= 1, got {self.throughput_dwell!r}")
        if not 0 <= self.rss_budget_fraction <= 1:
            raise ValueError(
                "rss_budget_fraction must be in [0, 1] (0 disables), "
                f"got {self.rss_budget_fraction!r}"
            )
        if self.memory_slope_bytes_per_tuple < 0:
            raise ValueError(
                "memory_slope_bytes_per_tuple must be >= 0 (0 disables), "
                f"got {self.memory_slope_bytes_per_tuple!r}"
            )
        if self.memory_dwell < 1:
            raise ValueError(f"memory_dwell must be >= 1, got {self.memory_dwell!r}")
        if self.absurd_pnl_paisa <= 0:
            raise ValueError(f"absurd_pnl_paisa must be > 0, got {self.absurd_pnl_paisa!r}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TripwireConfig:
        """Build a config from ``BRUTEX_SWEEP_TRIPWIRE_*`` env knobs (fail-loud).

        Unset / empty knobs keep the class defaults; a malformed value raises
        ``ValueError`` naming the variable (the DEC-RAM-BUDGETS-001 fail-loud
        convention — resolved at launch, so junk can never kill a running
        sweep). Range violations raise through the constructor's own
        validation.

        Parameters
        ----------
        env
            Environment mapping; defaults to ``os.environ`` (injectable for
            tests — no global mutable state).
        """
        src: Mapping[str, str] = os.environ if env is None else env
        base = cls()
        reconcile = _parse_env_float(src, ENV_RECONCILE_INTERVAL_S)
        floor = _parse_env_float(src, ENV_THROUGHPUT_FLOOR)
        t_dwell = _parse_env_int(src, ENV_THROUGHPUT_DWELL)
        rss_fraction = _parse_env_float(src, ENV_RSS_BUDGET_FRACTION)
        slope = _parse_env_int(src, ENV_MEMORY_SLOPE_BYTES)
        m_dwell = _parse_env_int(src, ENV_MEMORY_DWELL)
        absurd = _parse_env_int(src, ENV_ABSURD_PNL_PAISA)
        return cls(
            reconcile_interval_s=base.reconcile_interval_s if reconcile is None else reconcile,
            throughput_floor_fraction=base.throughput_floor_fraction if floor is None else floor,
            throughput_dwell=base.throughput_dwell if t_dwell is None else t_dwell,
            rss_budget_fraction=base.rss_budget_fraction if rss_fraction is None else rss_fraction,
            memory_slope_bytes_per_tuple=base.memory_slope_bytes_per_tuple
            if slope is None
            else slope,
            memory_dwell=base.memory_dwell if m_dwell is None else m_dwell,
            absurd_pnl_paisa=base.absurd_pnl_paisa if absurd is None else absurd,
        )


def normalized_ru_maxrss_bytes(*, ru_maxrss: int, platform: str) -> int:
    """Normalize a raw ``ru_maxrss`` reading to BYTES per platform (design D-16).

    macOS (``darwin``) reports ``ru_maxrss`` in BYTES; Linux (and every
    other POSIX in scope) reports KiB — the divergence that inverted an
    earlier stress-test memory assertion (see
    ``tests/stress/test_audit_chain_sustained.py``'s note). Pure and
    injectable so both branches are unit-pinned on any host.

    Parameters
    ----------
    ru_maxrss
        The raw ``resource.getrusage(...).ru_maxrss`` value.
    platform
        ``sys.platform`` (``"darwin"`` → bytes verbatim; anything else →
        KiB × 1024).

    Returns
    -------
    int
        The reading in bytes.

    The decision routes through the default-OFF observability_rust facade
    with :func:`_ru_maxrss_python` as the live oracle
    (DEC-RUST-OBSERVABILITY-TRIPWIRES-002; branch-free here — all
    flag/guard/burn-in branching lives facade-side).
    """
    return observability_rust.routed_ru_maxrss_bytes(
        ru_maxrss, platform, python_law=_ru_maxrss_python
    )


def _default_rss_probe() -> int:
    """Read the process high-water RSS in bytes (platform-normalized ``ru_maxrss``)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return normalized_ru_maxrss_bytes(ru_maxrss=raw, platform=sys.platform)


# ── Routed decision-law oracles (DEC-RUST-OBSERVABILITY-TRIPWIRES-002) ─────
#
# Each ``_*_python`` function below is the VERBATIM (scalarized) decision
# law its seam routes through the observability_rust facade — the LIVE
# parity oracle AND the flag-OFF implementation. The comparison/arithmetic
# expressions moved token-for-token from the pre-seam method bodies (the
# disclosed Slice-0 composition: the ``.append(f"…")`` sites became the
# bitmask/code vocabulary and the detail templates stayed at the fire
# sites). The code vocabulary is the facade's ``TW_NONE``/``TW_CLEAR``/
# ``TW_FIRE`` — defined ONCE facade-side, mirrored crate-side.

#: T2 violation-bitmask bits — bit i == predicate i in the pre-seam
#: ``_reconcile_counts`` append order (mirrored by the crate).
_RECONCILE_BIT_NEGATIVE_IN_FLIGHT: Final[int] = 1 << 0
_RECONCILE_BIT_ENUMERATED_DRIFT: Final[int] = 1 << 1
_RECONCILE_BIT_SKIPPED_OVERFLOW: Final[int] = 1 << 2
_RECONCILE_BIT_SURVIVORS_OVERFLOW: Final[int] = 1 << 3
_RECONCILE_BIT_IN_FLIGHT_OVERFLOW: Final[int] = 1 << 4
_RECONCILE_BIT_BACKWARDS: Final[int] = 1 << 5
_RECONCILE_BIT_DONE_INCOMPLETE: Final[int] = 1 << 6


def _reconcile_decide_python(
    in_flight: int,
    enumerated: int,
    evaluated: int,
    skipped: int,
    survivors: int,
    total: int,
    prev_evaluated: int | None,
    is_done: bool,
) -> int:
    """The T2 count-reconciliation decision — the D-9 identity predicates.

    Returns the violation BITMASK (bit i == predicate i in the pre-seam
    append order); the consumer materializes the detail templates from the
    SAME scalars via :func:`_reconcile_details`. ``is_done`` is computed by
    the consumer (``snapshot.state == STATE_DONE`` — the str compare stays
    with its import).
    """
    mask = 0
    if in_flight < 0:
        mask |= _RECONCILE_BIT_NEGATIVE_IN_FLIGHT
    if enumerated != total:
        mask |= _RECONCILE_BIT_ENUMERATED_DRIFT
    if skipped > evaluated:
        mask |= _RECONCILE_BIT_SKIPPED_OVERFLOW
    if survivors > evaluated:
        mask |= _RECONCILE_BIT_SURVIVORS_OVERFLOW
    if evaluated + in_flight > total:
        mask |= _RECONCILE_BIT_IN_FLIGHT_OVERFLOW
    if prev_evaluated is not None and evaluated < prev_evaluated:
        mask |= _RECONCILE_BIT_BACKWARDS
    if is_done and evaluated != total:
        mask |= _RECONCILE_BIT_DONE_INCOMPLETE
    return mask


def _reconcile_details(
    mask: int,
    *,
    in_flight: int,
    enumerated: int,
    evaluated: int,
    skipped: int,
    survivors: int,
    total: int,
    prev_evaluated: int | None,
) -> str:
    """Materialize the fired T2 violation templates, joined in BIT order.

    Bit order == the pre-seam append order, so the joined detail string is
    byte-identical to the pre-seam ``"; ".join(violations)``. The templates
    stay Python (CPython f-string formatting is the byte contract — the
    exact-substring unit asserts are the pins).
    """
    violations: list[str] = []
    if mask & _RECONCILE_BIT_NEGATIVE_IN_FLIGHT:
        violations.append(f"in_flight {in_flight} < 0")
    if mask & _RECONCILE_BIT_ENUMERATED_DRIFT:
        violations.append(f"snapshot enumerated {enumerated} != armed total {total}")
    if mask & _RECONCILE_BIT_SKIPPED_OVERFLOW:
        violations.append(f"skipped {skipped} > evaluated {evaluated} (skipped ⊆ evaluated)")
    if mask & _RECONCILE_BIT_SURVIVORS_OVERFLOW:
        violations.append(f"survivors {survivors} > evaluated {evaluated}")
    if mask & _RECONCILE_BIT_IN_FLIGHT_OVERFLOW:
        violations.append(f"evaluated {evaluated} + in_flight {in_flight} > enumerated {total}")
    if mask & _RECONCILE_BIT_BACKWARDS:
        violations.append(f"evaluated went backwards: {evaluated} < {prev_evaluated}")
    if mask & _RECONCILE_BIT_DONE_INCOMPLETE:
        violations.append(f"run done but evaluated {evaluated} != enumerated {total}")
    return "; ".join(violations)


def _throughput_decide_python(
    rate: float, peak: float, below: int, fraction: float, dwell: int
) -> tuple[float, int, float, int]:
    """The T3 throughput floor/dwell decision — ``(new_peak, new_below, floor, code)``.

    Called only when ``fraction != 0`` (the consumer's Python pre-gate,
    which also preserves the ``snapshot.combos_per_sec`` read order).
    ``max(peak, rate)`` is the pre-seam argument-order mirror (a NaN rate
    keeps the peak); the fire code lands on the dwell EDGE (``==`` — a
    past-dwell sample is ``TW_NONE``, never a re-fire).
    """
    new_peak = max(peak, rate)
    if new_peak <= 0.0:
        return (new_peak, below, 0.0, observability_rust.TW_NONE)
    floor = fraction * new_peak
    if rate < floor:
        new_below = below + 1
        if new_below == dwell:
            return (new_peak, new_below, floor, observability_rust.TW_FIRE)
        return (new_peak, new_below, floor, observability_rust.TW_NONE)
    return (new_peak, 0, floor, observability_rust.TW_CLEAR)


def _memory_budget_decide_python(rss: int, ram_total: int, fraction: float) -> tuple[float, int]:
    """The T4a RSS-budget decision — ``(budget, code)``.

    Called only when the budget watch is enabled, RAM is known and the
    Python-side RSS probe succeeded (the consumer's gates). Always a
    fire-or-clear decision — strictly-over-budget trips, exactly-AT is
    legal.
    """
    budget = fraction * float(ram_total)
    if float(rss) > budget:
        return (budget, observability_rust.TW_FIRE)
    return (budget, observability_rust.TW_CLEAR)


def _memory_slope_decide_python(
    rss: int,
    evaluated: int,
    prev_rss: int,
    prev_evaluated: int,
    slope_threshold: float,
    slope_high: int,
    dwell: int,
) -> tuple[float, int, int]:
    """The T4b RSS-slope decision — ``(slope, new_high, code)``.

    Called only when the slope watch is enabled, the probe succeeded and
    ``prev is not None and evaluated > prev_evaluated`` (the consumer's
    state-existence + progress gates — the divisor is strictly positive by
    construction). The over-slope arm NEVER clears (the pre-seam
    ``_slope_breach`` law); the fire code lands on the dwell EDGE (``==``);
    recovery resets the dwell counter AND clears.
    """
    slope = (rss - prev_rss) / (evaluated - prev_evaluated)
    if slope > slope_threshold:
        new_high = slope_high + 1
        if new_high == dwell:
            return (slope, new_high, observability_rust.TW_FIRE)
        return (slope, new_high, observability_rust.TW_NONE)
    return (slope, 0, observability_rust.TW_CLEAR)


def _summary_classify_python(
    values: list[float], screened: list[bool], absurd_threshold: float
) -> tuple[list[int], list[int]]:
    """The T5 NaN/absurd per-element classification — ``(nan_codes, absurd_codes)``.

    The consumer owns the ``sorted(tallies)`` iteration, the ``float()``
    coercion (whose raise IS the I-3 fail-soft input), the screened-name
    membership, the breach-key strings and the detail templates; the code
    arrays drive its fire/clear walk in input order. A non-finite value
    fires the NaN key and leaves the absurd key UNTOUCHED (the pre-seam
    ``continue``); a finite screened value trips strictly-over-threshold
    (exactly-AT is sane); an unscreened finite value decides nothing on the
    absurd key.
    """
    nan_codes: list[int] = []
    absurd_codes: list[int] = []
    for value, is_screened in zip(values, screened, strict=True):
        if not math.isfinite(value):
            nan_codes.append(observability_rust.TW_FIRE)
            absurd_codes.append(observability_rust.TW_NONE)
            continue
        nan_codes.append(observability_rust.TW_CLEAR)
        if is_screened:
            if abs(value) > absurd_threshold:
                absurd_codes.append(observability_rust.TW_FIRE)
            else:
                absurd_codes.append(observability_rust.TW_CLEAR)
        else:
            absurd_codes.append(observability_rust.TW_NONE)
    return (nan_codes, absurd_codes)


def _stage_missing_python(required_sorted: list[str], present: list[str]) -> list[bool]:
    """The T6 stage-prerequisite set law — the per-token MISSING mask.

    The consumer owns the :data:`STAGE_PREREQUISITES` map get + the
    unknown-stage early return, the ``sorted(required)`` (codepoint sort ≡
    the crate's UTF-8 byte order), the missing fold + the list-repr detail,
    and fire/clear. Membership is str equality — a junk (non-str) member is
    INERT exactly as in the pre-seam set diff (the facade's exact-str gate
    routes such inputs here).
    """
    return [token not in present for token in required_sorted]


def _ru_maxrss_python(ru_maxrss: int, platform: str) -> int:
    """The T9 ``ru_maxrss`` platform unit law — the verbatim pre-seam body.

    ``darwin`` reports BYTES verbatim; anything else reports KiB × 1024.
    The LIVE parity oracle behind :func:`normalized_ru_maxrss_bytes` (the
    public router keeps its signature, docstring and ``__all__`` entry).
    """
    if platform == "darwin":
        return int(ru_maxrss)
    return int(ru_maxrss) * 1024


class TripwireMonitor:
    """The R39 tripwire engine for ONE sweep run (thread-safe, fail-soft, O(1)/tick).

    Parameters
    ----------
    config
        The validated :class:`TripwireConfig`.
    enumerated_total
        The run's enumerated combo total (``>= 0``) — the reconciliation
        anchor every progress snapshot is checked against.
    clock
        Monotonic time source for the reconciliation interval gate
        (injected for deterministic tests; defaults to
        :func:`time.monotonic`). Never stamped on events —
        :attr:`TripwireEvent.ts_utc` is wall-clock, observability-only.
    on_trip
        The injected fan-out (flight-recorder TRIPWIRE record + Telegram +
        manifest — composed by the run core, design D-12). Called ONCE per
        fired event, under the monitor lock, guarded: a raising sink is
        WARNed and swallowed, never re-raised into the sweep.
    ram_total_bytes
        Total physical RAM in bytes; ``None`` resolves via
        :func:`brutex.config.hardware_envelope.detect_total_ram_bytes`
        (``0`` = honestly unknown → the RSS-budget watch stands down with
        ONE WARN, never a fabricated budget).
    rss_probe
        Zero-arg callable returning the process RSS in bytes; ``None`` uses
        the platform-normalized ``ru_maxrss`` default. The FIRST probe
        failure disables the memory tripwires for the run with ONE WARN
        (design D-16).

    Raises
    ------
    ValueError
        If ``enumerated_total`` is negative.
    """

    def __init__(
        self,
        *,
        config: TripwireConfig,
        enumerated_total: int,
        clock: Callable[[], float] = time.monotonic,
        on_trip: Callable[[TripwireEvent], None],
        ram_total_bytes: int | None = None,
        rss_probe: Callable[[], int] | None = None,
    ) -> None:
        if enumerated_total < 0:
            raise ValueError(f"enumerated_total must be >= 0, got {enumerated_total!r}")
        self._config = config
        self._total = enumerated_total
        self._clock = clock
        self._on_trip = on_trip
        self._ram_total = detect_total_ram_bytes() if ram_total_bytes is None else ram_total_bytes
        self._rss_probe: Callable[[], int] = _default_rss_probe if rss_probe is None else rss_probe
        self._lock = threading.Lock()
        # D-12: plain set-once bool — the one state whose write cannot fail.
        self._suspect = False
        self._events: deque[TripwireEvent] = deque(maxlen=EVENTS_RING_CAP)
        self._in_breach: set[str] = set()
        self._last_reconcile: float | None = None
        self._prev_evaluated: int | None = None
        self._peak_rate = 0.0
        self._throughput_below = 0
        self._memory_probe_disabled = False
        self._ram_unknown_warned = False
        self._slope_high = 0
        # Last (rss_bytes, evaluated) pair for the slope watch.
        self._prev_mem: tuple[int, int] | None = None

    # ── Public read surface ───────────────────────────────────────────────

    @property
    def suspect(self) -> bool:
        """``True`` iff any tripwire has EVER fired this run (set-once, D-12)."""
        return self._suspect

    @property
    def events(self) -> tuple[TripwireEvent, ...]:
        """The last :data:`EVENTS_RING_CAP` fired events, oldest first."""
        with self._lock:
            return tuple(self._events)

    @property
    def memory_probe_disabled(self) -> bool:
        """``True`` iff the RSS probe failed and the memory watches stood down."""
        return self._memory_probe_disabled

    @property
    def ram_total_bytes(self) -> int:
        """The resolved total-RAM figure (``0`` = honestly unknown)."""
        return self._ram_total

    # ── Observation seams (fail-soft — I-3) ───────────────────────────────

    def on_progress(self, snapshot: SweepProgressSnapshot, *, in_flight: int = 0) -> None:
        """Feed one progress snapshot — reconcile counts + trend watches, O(1).

        Interval-gated: samples at most once per
        :attr:`TripwireConfig.reconcile_interval_s` on the injected clock
        (the first call always samples). Count reconciliation reads the
        snapshot's counters EXACTLY (design D-9). Fail-soft: any internal
        error is WARNed and swallowed.

        Parameters
        ----------
        snapshot
            The live :class:`~brutex.engine.sweep_progress.SweepProgressSnapshot`.
        in_flight
            Combos currently issued but not yet folded into ``evaluated``
            (``0`` on the sequential spine).
        """
        try:
            with self._lock:
                now = self._clock()
                if (
                    self._last_reconcile is not None
                    and now - self._last_reconcile < self._config.reconcile_interval_s
                ):
                    return
                self._last_reconcile = now
                self._reconcile_counts(snapshot, in_flight)
                self._watch_throughput(snapshot)
                self._watch_memory(snapshot)
        except Exception as exc:  # fail-soft law (I-3) — never into the sweep
            self._observe_failed("on_progress", exc)

    def on_tuple_summary(self, tallies: Mapping[str, float]) -> None:
        """Screen one tuple's metric-summary floats — NaN/inf + absurd magnitude.

        O(keys). Every value is screened non-finite; keys named in
        :attr:`TripwireConfig.absurd_value_names` are additionally screened
        for ``abs(value) > absurd_pnl_paisa`` (strict — a value exactly AT
        the threshold is sane). Absent keys are ignored, never fabricated.
        Fail-soft.
        """
        try:
            with self._lock:
                # The per-element CLASSIFICATION routes through the
                # default-OFF observability_rust facade with
                # `_summary_classify_python` as the live oracle
                # (DEC-RUST-OBSERVABILITY-TRIPWIRES-002). The sorted
                # iteration + the float() coercion (whose raise IS the I-3
                # fail-soft input) + the key strings + the detail templates
                # stay Python, driven by the returned code arrays.
                names = sorted(tallies)
                values = [float(tallies[name]) for name in names]
                screened = [name in self._config.absurd_value_names for name in names]
                nan_codes, absurd_codes = observability_rust.routed_tripwire_summary(
                    values,
                    screened,
                    float(self._config.absurd_pnl_paisa),
                    python_law=_summary_classify_python,
                )
                for name, value, nan_code, absurd_code in zip(
                    names, values, nan_codes, absurd_codes, strict=True
                ):
                    if nan_code == observability_rust.TW_FIRE:
                        self._edge_fire(
                            f"nan_metric:{name}",
                            TripwireKind.NAN_METRIC,
                            SEVERITY_CRITICAL,
                            f"non-finite metric-summary value: {name}={value!r}",
                            {name: value},
                        )
                        continue
                    self._clear(f"nan_metric:{name}")
                    if absurd_code == observability_rust.TW_FIRE:
                        self._edge_fire(
                            f"absurd_value:{name}",
                            TripwireKind.ABSURD_VALUE,
                            SEVERITY_CRITICAL,
                            f"absurd metric-summary magnitude: {name}={value!r} "
                            f"(|value| > {self._config.absurd_pnl_paisa} paisa)",
                            {name: value},
                        )
                    elif absurd_code == observability_rust.TW_CLEAR:
                        self._clear(f"absurd_value:{name}")
        except Exception as exc:  # fail-soft law (I-3)
            self._observe_failed("on_tuple_summary", exc)

    def on_stage(self, stage: str, *, stamps_present: frozenset[str]) -> None:
        """Check a stage entry against its prerequisite stamps — O(1).

        Fires :data:`TripwireKind.STAGE_INVARIANT` when any stamp required
        by :data:`STAGE_PREREQUISITES` for *stage* is missing from
        *stamps_present*; a stage not in the map is tolerated silently.
        Fail-soft.
        """
        try:
            with self._lock:
                required = STAGE_PREREQUISITES.get(stage)
                if required is None:
                    return
                # The set-law DECISION routes through the default-OFF
                # observability_rust facade with `_stage_missing_python` as
                # the live oracle (DEC-RUST-OBSERVABILITY-TRIPWIRES-002).
                # required is pre-sorted Python-side, so the mask fold below
                # IS `sorted(required - stamps_present)`; the map get, the
                # `sorted(missing)` list-repr detail and fire/clear stay
                # Python.
                required_sorted = sorted(required)
                missing_mask = observability_rust.routed_tripwire_stage(
                    required_sorted,
                    list(stamps_present),
                    python_law=_stage_missing_python,
                )
                missing = [
                    token
                    for token, is_missing in zip(required_sorted, missing_mask, strict=True)
                    if is_missing
                ]
                if missing:
                    self._edge_fire(
                        f"stage_invariant:{stage}",
                        TripwireKind.STAGE_INVARIANT,
                        SEVERITY_CRITICAL,
                        f"stage {stage!r} entered without prerequisite stamp(s): {sorted(missing)}",
                        {"missing": float(len(missing))},
                    )
                else:
                    self._clear(f"stage_invariant:{stage}")
        except Exception as exc:  # fail-soft law (I-3)
            self._observe_failed("on_stage", exc)

    # ── Internals (called under the lock) ─────────────────────────────────

    def _reconcile_counts(self, snapshot: SweepProgressSnapshot, in_flight: int) -> None:
        """Check the D-9 exact counter identities; edge-fire on any violation.

        The DECISION routes through the default-OFF observability_rust
        facade with :func:`_reconcile_decide_python` as the live oracle
        (DEC-RUST-OBSERVABILITY-TRIPWIRES-002; branch-free here — all
        flag/guard/burn-in branching lives facade-side). The snapshot
        counters are extracted ONCE in the pre-seam first-read order (a
        raising duck attribute surfaces identically); the detail templates
        + the ``values`` dict are built from the SAME scalars.
        """
        enumerated = snapshot.enumerated
        skipped = snapshot.skipped
        evaluated = snapshot.evaluated
        survivors = snapshot.survivors
        prev_evaluated = self._prev_evaluated
        is_done = snapshot.state == STATE_DONE
        mask = observability_rust.routed_tripwire_reconcile(
            in_flight,
            enumerated,
            evaluated,
            skipped,
            survivors,
            self._total,
            prev_evaluated,
            is_done,
            python_law=_reconcile_decide_python,
        )
        self._prev_evaluated = evaluated
        if mask:
            self._edge_fire(
                "count_reconciliation",
                TripwireKind.COUNT_RECONCILIATION,
                SEVERITY_CRITICAL,
                _reconcile_details(
                    mask,
                    in_flight=in_flight,
                    enumerated=enumerated,
                    evaluated=evaluated,
                    skipped=skipped,
                    survivors=survivors,
                    total=self._total,
                    prev_evaluated=prev_evaluated,
                ),
                {
                    "enumerated": float(self._total),
                    "evaluated": float(evaluated),
                    "skipped": float(skipped),
                    "survivors": float(survivors),
                    "in_flight": float(in_flight),
                },
            )
        else:
            self._clear("count_reconciliation")

    def _watch_throughput(self, snapshot: SweepProgressSnapshot) -> None:
        """Dwell-gated floor watch over the snapshot's own observed rate.

        The DECISION routes through the default-OFF observability_rust
        facade with :func:`_throughput_decide_python` as the live oracle
        (DEC-RUST-OBSERVABILITY-TRIPWIRES-002). The ``fraction == 0``
        pre-gate stays Python BEFORE the ``combos_per_sec`` property read
        (the pre-seam raise surface); state writes + fire/clear + the
        detail template stay Python, driven by the returned code.
        """
        fraction = self._config.throughput_floor_fraction
        if fraction == 0:
            return
        rate = snapshot.combos_per_sec
        new_peak, new_below, floor, code = observability_rust.routed_tripwire_throughput(
            rate,
            self._peak_rate,
            self._throughput_below,
            fraction,
            self._config.throughput_dwell,
            python_law=_throughput_decide_python,
        )
        self._peak_rate = new_peak
        self._throughput_below = new_below
        if code == observability_rust.TW_FIRE:
            self._edge_fire(
                "throughput_floor",
                TripwireKind.THROUGHPUT_FLOOR,
                SEVERITY_WARNING,
                f"throughput {rate:.3f}/s below floor {floor:.3f}/s "
                f"(peak {self._peak_rate:.3f}/s × {fraction}) for "
                f"{self._throughput_below} consecutive samples",
                {"rate": rate, "floor": floor, "peak": self._peak_rate},
            )
        elif code == observability_rust.TW_CLEAR:
            self._clear("throughput_floor")

    def _watch_memory(self, snapshot: SweepProgressSnapshot) -> None:
        """RSS budget + growth-slope watches (probe-failure ⇒ stand down, D-16)."""
        budget_enabled = self._config.rss_budget_fraction > 0
        slope_enabled = self._config.memory_slope_bytes_per_tuple > 0
        if (not budget_enabled and not slope_enabled) or self._memory_probe_disabled:
            return
        try:
            rss = int(self._rss_probe())
        except Exception as exc:  # probe failure ⇒ disabled + ONE WARN (D-16)
            self._memory_probe_disabled = True
            self._warn(
                "run_tripwires.rss_probe_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                detail="memory tripwires disabled for this run",
            )
            return
        # The budget + slope DECISIONS route through the default-OFF
        # observability_rust facade with `_memory_budget_decide_python` /
        # `_memory_slope_decide_python` as the live oracles
        # (DEC-RUST-OBSERVABILITY-TRIPWIRES-002). The enable gates, the
        # probe + stand-down, the ram-unknown warn-once, the prev/progress
        # gates and the `_prev_mem` stamp stay Python.
        if budget_enabled:
            if self._ram_total <= 0:
                if not self._ram_unknown_warned:
                    self._ram_unknown_warned = True
                    self._warn(
                        "run_tripwires.ram_total_unknown",
                        detail="total RAM undetected — RSS-budget tripwire stands down",
                    )
            else:
                budget, budget_code = observability_rust.routed_tripwire_memory_budget(
                    rss,
                    self._ram_total,
                    self._config.rss_budget_fraction,
                    python_law=_memory_budget_decide_python,
                )
                if budget_code == observability_rust.TW_FIRE:
                    self._edge_fire(
                        "memory_budget",
                        TripwireKind.MEMORY_BUDGET,
                        SEVERITY_CRITICAL,
                        f"RSS {rss} bytes exceeds budget {budget:.0f} bytes "
                        f"({self._config.rss_budget_fraction} × {self._ram_total})",
                        {"rss_bytes": float(rss), "budget_bytes": budget},
                    )
                else:
                    self._clear("memory_budget")
        if slope_enabled:
            prev = self._prev_mem
            if prev is not None and snapshot.evaluated > prev[1]:
                slope, new_high, slope_code = observability_rust.routed_tripwire_memory_slope(
                    rss,
                    snapshot.evaluated,
                    prev[0],
                    prev[1],
                    float(self._config.memory_slope_bytes_per_tuple),
                    self._slope_high,
                    self._config.memory_dwell,
                    python_law=_memory_slope_decide_python,
                )
                self._slope_high = new_high
                if slope_code == observability_rust.TW_FIRE:
                    self._edge_fire(
                        "memory_slope",
                        TripwireKind.MEMORY_SLOPE,
                        SEVERITY_WARNING,
                        f"RSS growing {slope:.0f} bytes/tuple > "
                        f"{self._config.memory_slope_bytes_per_tuple} bytes/tuple for "
                        f"{self._slope_high} consecutive samples",
                        {"slope_bytes_per_tuple": slope, "rss_bytes": float(rss)},
                    )
                elif slope_code == observability_rust.TW_CLEAR:
                    self._clear("memory_slope")
            self._prev_mem = (rss, snapshot.evaluated)

    def _edge_fire(
        self,
        key: str,
        kind: TripwireKind,
        severity: str,
        detail: str,
        values: Mapping[str, float],
    ) -> None:
        """Fire once per breach edge — a still-breached key never re-fires."""
        if key in self._in_breach:
            return
        self._in_breach.add(key)
        self._fire(kind, severity, detail, values)

    def _clear(self, key: str) -> None:
        """Re-arm a recovered breach key so a recurrence fires again."""
        self._in_breach.discard(key)

    def _fire(
        self, kind: TripwireKind, severity: str, detail: str, values: Mapping[str, float]
    ) -> None:
        """Latch SUSPECT (plain bool — cannot fail) then fan out, guarded (D-12)."""
        self._suspect = True
        event = TripwireEvent(
            ts_utc=time.time(),
            name=kind.value,
            severity=severity,
            detail=detail,
            values=dict(values),
        )
        self._events.append(event)
        try:
            _LOG.error(
                "run_tripwires.tripped",
                name=event.name,
                severity=event.severity,
                detail=event.detail,
                values=event.values,
            )
            self._on_trip(event)
        except Exception as exc:  # a failing sink must never re-enter the sweep
            self._warn(
                "run_tripwires.on_trip_failed",
                name=event.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _observe_failed(self, method: str, exc: Exception) -> None:
        """WARN (best-effort) that an observation body failed — fail-soft (I-3)."""
        self._warn(
            "run_tripwires.observe_failed",
            method=method,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    @staticmethod
    def _warn(event: str, **kwargs: object) -> None:
        """Best-effort structlog WARN — even a broken logger never propagates."""
        with contextlib.suppress(Exception):
            _LOG.warning(event, **kwargs)


__all__ = [
    "DEFAULT_ABSURD_VALUE_NAMES",
    "ENV_ABSURD_PNL_PAISA",
    "ENV_MEMORY_DWELL",
    "ENV_MEMORY_SLOPE_BYTES",
    "ENV_RECONCILE_INTERVAL_S",
    "ENV_RSS_BUDGET_FRACTION",
    "ENV_THROUGHPUT_DWELL",
    "ENV_THROUGHPUT_FLOOR",
    "EVENTS_RING_CAP",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "STAGE_PREREQUISITES",
    "TripwireConfig",
    "TripwireEvent",
    "TripwireKind",
    "TripwireMonitor",
    "normalized_ru_maxrss_bytes",
]
