"""Live pre-run sweep ESTIMATOR (R20) — pure O(1)-ish trial/seconds/bytes arithmetic.

On click, BEFORE any compute, the one-click institutional sweep button must
show "this selection = X trials, est. Y hours, Z GB — proceed?" computed
LIVE from the actual selection — never a stale hardcoded figure (R20; the
"33B trials" talk is RETIRED as a default, R20). This module is the pure
arithmetic core; :mod:`brutex.engine.sweep_oneclick_preflight` composes it
into the full preflight verdict.

## The trials-vs-tuples distinction (gap-hunt finding, load-bearing)

:func:`brutex.engine.sweep_orchestrator.count_sweep_tuples` counts GRID
TUPLES — one ``(instrument, timeframe, direction, k, sl, tp, proximity,
fib_anchor)`` cell, treating each requested ``k`` as ONE axis value. A tuple
whose ``k`` is a specific integer does not itself enumerate the ``C(60, k)``
condition-combination masks the engine actually brute-forces at that tuple —
so ``count_sweep_tuples`` UNDER-COUNTS the true number of combo evaluations
("trials", R19/R20's unit) by orders of magnitude. This module's
:func:`count_true_trials` is deliberately a SEPARATE function — it does not
edit or wrap ``count_sweep_tuples`` (a different, still-correct concept used
by :mod:`brutex.engine.sweep_sla_guard`'s tuple-grain projection) — so the
two units can never be silently conflated.

## Honest O() (Golden Rule #10 — never falsely claim O(1))

:func:`count_true_trials` is ``O(|k_values|)`` — a tiny sum (``|k_values|``
is at most a handful of small integers, e.g. ``{1, 2, 3}``), not a literal
O(1) constant-time operation, though it is *effectively* O(1) at the scale
this project ever sees. :func:`brutex.engine.combo_mask.count_combos` itself
is a closed-form ``C(n, k)`` — true O(1) per call.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, prod
from typing import TYPE_CHECKING, Final

from brutex.engine.combo_mask import count_combos
from brutex.engine.estimator_calibration import EstimatorCalibration
from brutex.vocabulary.bits import IMPLEMENTED_MASK

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CALIBRATED_BASIS",
    "PROVISIONAL_BASIS",
    "SWEPT_CONDITIONS",
    "VWAP_DEAD_BITS",
    "EstimatorEstimate",
    "count_true_trials",
    "effective_condition_count",
    "estimate_sweep_cost",
]

#: :attr:`EstimatorEstimate.estimate_basis` when a real calibration file was read.
CALIBRATED_BASIS: Final[str] = "calibrated"
#: :attr:`EstimatorEstimate.estimate_basis` when the estimate rides the
#: unbenchmarked fallback constants (R23: every ETA is PROVISIONAL until a
#: real smoke run calibrates it).
PROVISIONAL_BASIS: Final[str] = "provisional"

#: The number of IMPLEMENTED (sweepable) conditions — the estimator's default
#: vocabulary size. A one-click launch always passes ``conditions=None``,
#: which the engine resolves to ``default_condition_bits()`` == every
#: IMPLEMENTED bit — 64 on the 66-declared layout
#: (DEC-VOCAB-66-RESHAPE-IMPL-001): tombstoned/layout-only bits are NEVER
#: swept, so sizing ``C(n, k)`` off the declared width would overstate every
#: trial/ETA/byte projection. ``IMPLEMENTED_MASK`` is the one source of
#: truth, so vocab growth moves this automatically
#: (DEC-VOCAB-U128-CODEGEN-001).
SWEPT_CONDITIONS: Final[int] = IMPLEMENTED_MASK.bit_count()

#: The two VWAP-position vocabulary bits (`brutex.vocabulary.bits` 52/53) that
#: are structurally DEAD (can never fire) on zero-volume INDEX-segment spot
#: candles — gap-hunt finding #8 (VWAP dead-bits, the original "60 vs 58"
#: effective condition count). EXCLUDE-vs-FLAG was an operator decision still
#: queued (ruling #8) — this module DEFAULTS to flag-and-run: the nominal
#: vocabulary is NEVER reduced here (:func:`count_true_trials` always sweeps
#: the FULL requested condition set), :func:`effective_condition_count`
#: merely surfaces the honest effective-vs-nominal count for display.
#: RETENTION NOTE (DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206): the
#: bits-52/53 wipe is REVERSED — the pair is IMPLEMENTED again, so the
#: ∩-implemented subtraction SELF-REVIVED exactly as designed: on a
#: zero-volume INDEX segment the annotation reads effective == nominal - 2
#: (the pre-wipe "60 vs 58" law at the 74/74 width); every other segment
#: reads effective == nominal.
VWAP_DEAD_BITS: Final[frozenset[int]] = frozenset({52, 53})

#: The implemented bit POSITIONS (for the dead-bit ∩ implemented law above).
_IMPLEMENTED_BIT_SET: Final[frozenset[int]] = frozenset(
    i for i in range(IMPLEMENTED_MASK.bit_length()) if IMPLEMENTED_MASK >> i & 1
)

#: Segments whose spot candles are volume=0 by construction (index segment —
#: gap-hunt finding #8's "zero-volume (INDEX-segment) selection").
_ZERO_VOLUME_SEGMENTS: Final[frozenset[str]] = frozenset({"INDEX"})

#: Sanity ceiling on ``est_seconds_provisional`` — an absurd but FINITE,
#: JSON-safe value (~31.7 billion years). ``trials`` (an arbitrary-precision
#: Python int, unbounded by :func:`count_true_trials`) times even a
#: :data:`~brutex.engine.estimator_calibration._MAX_SECONDS_PER_TRIAL`-capped
#: ``seconds_per_trial`` can still overflow the ``int * float`` multiply to
#: ``inf`` or raise ``OverflowError`` outright converting a huge int to
#: float (an adversarial-refuter review found the calibration file's own
#: caps only bound one operand, not the product) — clamping the PRODUCT
#: here closes that gap regardless of which operand made it large.
_MAX_EST_SECONDS: Final[float] = 1e18

#: Sanity ceiling on ``est_bytes`` — a round-3 adversarial-refuter review
#: found ``tuple_count * top * bytes_per_row`` (plain Python ints, so this
#: never raises ``OverflowError``/produces ``inf`` INSIDE this function) can
#: still grow an arbitrary-precision int past Python 3.11+'s
#: ``sys.set_int_max_str_digits`` guard (default 4300 digits) — reproduced
#: via a real, unauthenticated dashboard request: a large-but-otherwise-valid
#: ``top`` query value (no upper bound existed on either one-click route)
#: feeds through the UNCALIBRATED default (``bytes_per_row=512``) into an
#: ``est_bytes`` whose decimal string blows that guard, raising
#: ``ValueError: Exceeds the limit ... for integer string conversion`` from
#: deep inside FastAPI's JSON encoder — an unhandled 500 on a single GET.
#: Clamping the byte projection to an absurd-but-finite, JSON-and-string-safe
#: ceiling (~1 yottabyte) closes this the same way :data:`_MAX_EST_SECONDS`
#: closes the seconds-side gap.
_MAX_EST_BYTES: Final[int] = 10**24


@dataclass(frozen=True, slots=True)
class EstimatorEstimate:
    """The R20 estimator's verdict for one selection (the ``estimator`` contract sub-shape).

    Attributes
    ----------
    trials
        The TRUE trial count: ``sum(C(60, k) for k in k_values) *
        other_axes_product`` — see :func:`count_true_trials`.
    tuple_count
        The grid-tuple count (:func:`brutex.engine.sweep_orchestrator.count_sweep_tuples`'s
        unit) — echoed here (not re-derived) purely for the byte projection,
        which sizes PERSISTED rows (top-N per tuple), not raw trials.
    est_seconds_provisional
        ``trials * calibration.seconds_per_trial`` — labelled "provisional"
        in the field name itself per R23 (MEASURED-TIMING-ONLY) regardless of
        :attr:`estimate_basis`, since even a "calibrated" reading is a linear
        extrapolation, never a certification.
    est_bytes
        ``tuple_count * top * calibration.bytes_per_row`` — the projected
        on-disk footprint of the run's persisted winners.
    estimate_basis
        :data:`CALIBRATED_BASIS` or :data:`PROVISIONAL_BASIS` — ruling #14's
        calibrated/provisional discriminator.
    nominal_condition_count
        The full vocabulary size the selection nominally sweeps (before any
        dead-bit accounting) — normally :data:`SWEPT_CONDITIONS`.
    effective_condition_count
        The count of conditions that can ever actually FIRE for this
        selection — less than *nominal_condition_count* when the selection is
        a zero-volume INDEX segment (VWAP bits 52/53 dead, gap-hunt #8).
        Flag-and-run: this NEVER shrinks *trials* — it is purely an honest
        annotation surfaced alongside the estimate.
    """

    trials: int
    tuple_count: int
    est_seconds_provisional: float
    est_bytes: int
    estimate_basis: str
    nominal_condition_count: int
    effective_condition_count: int

    def to_dict(self) -> dict[str, object]:
        """Render the exact ``estimator`` contract sub-shape (JSON-safe)."""
        return {
            "trials": self.trials,
            "est_seconds_provisional": self.est_seconds_provisional,
            "est_bytes": self.est_bytes,
            "estimate_basis": self.estimate_basis,
            "nominal_condition_count": self.nominal_condition_count,
            "effective_condition_count": self.effective_condition_count,
        }


def count_true_trials(
    k_values: Sequence[int],
    *,
    other_axes_product: int,
    n: int = SWEPT_CONDITIONS,
) -> int:
    """Return the TRUE trial count for a selection — see the module docstring.

    ``sum(count_combos(k, n) for k in k_values) * other_axes_product``.

    Parameters
    ----------
    k_values
        The swept condition-depth values (e.g. ``[1, 2, 3]``).
    other_axes_product
        The product of every OTHER axis cardinality: instruments ×
        timeframes × directions × stop-losses × take-profits × proximities ×
        fib-anchors (i.e. :func:`~brutex.engine.sweep_orchestrator.count_sweep_tuples`'s
        full product with the ``k`` axis divided back out).
    n
        The condition vocabulary size (default :data:`SWEPT_CONDITIONS` —
        the IMPLEMENTED count; tombstoned bits are never swept).

    Raises
    ------
    ValueError
        If *k_values* is empty, any ``k`` is outside ``[0, n]``, or
        *other_axes_product* is negative.
    """
    if not k_values:
        raise ValueError("k_values must be non-empty")
    if other_axes_product < 0:
        raise ValueError(f"other_axes_product must be >= 0; got {other_axes_product!r}")
    return sum(count_combos(k, n) for k in k_values) * other_axes_product


def effective_condition_count(*, segment: str, n: int = SWEPT_CONDITIONS) -> tuple[int, int]:
    """Return ``(nominal, effective)`` condition counts for *segment* — gap-hunt #8.

    ``effective < nominal`` iff *segment* is a zero-volume INDEX segment
    AND some :data:`VWAP_DEAD_BITS` member is still IMPLEMENTED (sweepable):
    such a bit can never fire on zero-volume data and is honestly reported
    as dead — flag-and-run (this module never excludes it from the swept
    vocabulary, only annotates). On the 74/74 retention layout the VWAP bits
    are IMPLEMENTED again (DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206 —
    the wipe reversed), so the INDEX-segment subtraction is TWO: the pair is
    live-but-volumeless on zero-volume spot indices and can never fire there.

    Parameters
    ----------
    segment
        The lake segment of the launch (e.g. ``"INDEX"``, ``"FNO"``).
    n
        The nominal vocabulary size (default :data:`SWEPT_CONDITIONS`).
    """
    if segment.upper() in _ZERO_VOLUME_SEGMENTS:
        return n, n - len(VWAP_DEAD_BITS & _IMPLEMENTED_BIT_SET)
    return n, n


def estimate_sweep_cost(
    *,
    k_values: Sequence[int],
    other_axes_product: int,
    tuple_count: int,
    top: int,
    segment: str,
    calibration: EstimatorCalibration,
    n: int = SWEPT_CONDITIONS,
) -> EstimatorEstimate:
    """Compose the full R20 estimator verdict for one selection.

    Pure given its inputs (no I/O, no clock) — the caller resolves
    *calibration* via :func:`~brutex.engine.estimator_calibration.load_estimator_calibration`
    once per preflight call.

    Parameters
    ----------
    k_values, other_axes_product
        Forwarded to :func:`count_true_trials`.
    tuple_count
        The grid's tuple count (sizes the byte projection — top-N PERSISTED
        rows are stored per tuple, not per trial).
    top
        Top-N combos persisted per tuple (R21).
    segment
        The lake segment — feeds :func:`effective_condition_count`.
    calibration
        The resolved calibration reading.
    n
        The condition vocabulary size (default :data:`SWEPT_CONDITIONS` —
        the IMPLEMENTED count; tombstoned bits are never swept).

    Raises
    ------
    ValueError
        If *tuple_count* or *top* is negative, propagated from
        :func:`count_true_trials`, or *tuple_count*/*top* multiplication
        would be negative.
    """
    if tuple_count < 0:
        raise ValueError(f"tuple_count must be >= 0; got {tuple_count!r}")
    if top < 0:
        raise ValueError(f"top must be >= 0; got {top!r}")
    trials = count_true_trials(k_values, other_axes_product=other_axes_product, n=n)
    nominal, effective = effective_condition_count(segment=segment, n=n)
    try:
        est_seconds = trials * calibration.seconds_per_trial
    except OverflowError:
        est_seconds = _MAX_EST_SECONDS
    if not isfinite(est_seconds) or est_seconds > _MAX_EST_SECONDS:
        est_seconds = _MAX_EST_SECONDS
    est_bytes = min(tuple_count * top * calibration.bytes_per_row, _MAX_EST_BYTES)
    basis = CALIBRATED_BASIS if calibration.calibrated else PROVISIONAL_BASIS
    return EstimatorEstimate(
        trials=trials,
        tuple_count=tuple_count,
        est_seconds_provisional=est_seconds,
        est_bytes=est_bytes,
        estimate_basis=basis,
        nominal_condition_count=nominal,
        effective_condition_count=effective,
    )


def axes_product(*axis_lengths: int) -> int:
    """Return the product of every axis length (``math.prod`` with a clear name).

    A tiny named wrapper so callers building *other_axes_product* read as
    ``axes_product(len(instruments), len(timeframes), ...)`` rather than a
    bare ``prod(...)`` call whose intent is only clear from context.
    """
    return prod(axis_lengths)
