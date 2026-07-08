"""Estimator calibration store — schema + reader for ``estimator_calibration.json``.

Gap-hunt ruling #13 (`docs/MIGRATION.md`, [[sweep-gap-register-jul8]] finding
#13): the R20 live pre-run estimator's estimated-vs-actual self-correction
loop needs a PERSISTED calibration store both sides agree on. P1 (this
module) owns the schema + the read path + the ``calibrated`` discriminator;
the WRITER (stamping real measured seconds/bytes-per-trial at run-end) is
P2's orchestrator-seam job — it does not exist on ``main`` yet, so
:func:`load_estimator_calibration` always returns the honest
:data:`UNCALIBRATED` fallback today (verified 2026-07-08: zero references to
``estimator_calibration`` anywhere in the tree before this module). That is
not a bug — it is R23's MEASURED-TIMING-ONLY law: no ETA is trusted until a
real smoke run calibrates it, so every estimate stays honestly labelled
``"provisional"`` until a real file appears.

## Honest envelope (Golden Rule #10)

The fallback ``seconds_per_trial``/``bytes_per_row`` constants below are
**UNBENCHMARKED placeholders** in the same spirit as
:data:`brutex.engine.sweep_sla_guard.SWEEP_PER_TUPLE_SECONDS` — deliberately
conservative so a bad estimate over-warns rather than under-warns. They are
NEVER labelled ``"calibrated"``; only a real, schema-valid, on-disk
``estimator_calibration.json`` (P2's future writer) can flip
:attr:`EstimatorCalibration.calibrated` to ``True``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "CALIBRATION_FILENAME",
    "SCHEMA_VERSION",
    "UNCALIBRATED",
    "EstimatorCalibration",
    "estimator_calibration_path",
    "load_estimator_calibration",
]

#: The one on-disk filename, always a direct child of the sweep results root
#: (``<results_root>/estimator_calibration.json`` — gap-hunt ruling #13).
CALIBRATION_FILENAME: Final[str] = "estimator_calibration.json"

#: The calibration file's schema version. A file whose ``schema_version``
#: does not match is treated as absent (fail-closed to the uncalibrated
#: fallback, never a crash on a future/older schema).
SCHEMA_VERSION: Final[int] = 1

#: UNBENCHMARKED placeholder — conservative first-order estimate, honestly
#: never claimed "calibrated". Mirrors the per-trial cost model implied by
#: CLAUDE.md §8's architecture arithmetic (a mask-AND bench-gated at
#: 50M+/s ⇒ ~2e-8s/op at the single-condition-evaluation grain; a full
#: *trial* here is one combo evaluated over one tuple's whole bar series,
#: several orders of magnitude coarser than one mask-AND, so this constant
#: is deliberately far more conservative than the raw kernel throughput).
_FALLBACK_SECONDS_PER_TRIAL: Final[float] = 5e-5

#: UNBENCHMARKED placeholder average persisted-row size in bytes (a
#: :class:`~brutex.engine.results_store.ResultsStore` row: metrics + regime +
#: VIX band + trade-list pointer, full precision floats). Conservative
#: over-estimate so the byte projection over-warns rather than under-warns.
_FALLBACK_BYTES_PER_ROW: Final[int] = 512

#: Sanity ceiling on the on-disk file size (bytes) before it is even read —
#: this is a small fixed-shape JSON object; anything past this is refused as
#: malformed rather than fully read into memory (a hostile review found an
#: unbounded ``read_text()`` could raise an uncaught ``MemoryError`` for an
#: adversarially large file, breaking the "NEVER raises" contract).
_MAX_FILE_BYTES: Final[int] = 64 * 1024

#: Sanity ceiling on ``seconds_per_trial`` — anything past this is refused
#: (a hostile review found an unbounded value lets ``estimate_sweep_cost``
#: overflow ``trials * seconds_per_trial`` to ``inf``, which then raises
#: inside FastAPI's ``JSONResponse`` at serialize time, an unhandled 500).
#: A real trial has never measured anywhere near one hour each.
_MAX_SECONDS_PER_TRIAL: Final[float] = 3_600.0

#: Sanity ceiling on ``bytes_per_row`` — same overflow rationale; no real
#: persisted-row shape is anywhere near 1 GiB.
_MAX_BYTES_PER_ROW: Final[int] = 1024**3


@dataclass(frozen=True, slots=True)
class EstimatorCalibration:
    """One calibration reading for the R20 estimator's seconds/bytes projection.

    Attributes
    ----------
    calibrated
        ``True`` iff this reading came from a real, schema-valid, on-disk
        calibration file written by a completed run (P2's future writer).
        ``False`` for the built-in :data:`UNCALIBRATED` fallback.
    seconds_per_trial
        Wall-clock seconds per combo-trial (one combo mask evaluated over one
        tuple's full swept window) — the projection unit R22/R23 describe.
    bytes_per_row
        Average persisted-row size in bytes (one stored winner row).
    sample_trials
        How many trials the calibration reading was measured over (``0`` for
        the fallback — never a fabricated sample size).
    measured_at_utc
        ISO-8601 UTC timestamp of the real measurement, or ``None`` for the
        fallback.
    source
        A short human-readable provenance label (``"unbenchmarked_default"``
        or, once a real writer lands, e.g. ``"smoke_run"``).
    """

    calibrated: bool
    seconds_per_trial: float
    bytes_per_row: int
    sample_trials: int
    measured_at_utc: str | None
    source: str


#: The honest fallback every read resolves to until a real calibration file
#: exists — never labelled calibrated, never a fabricated sample size.
UNCALIBRATED: Final[EstimatorCalibration] = EstimatorCalibration(
    calibrated=False,
    seconds_per_trial=_FALLBACK_SECONDS_PER_TRIAL,
    bytes_per_row=_FALLBACK_BYTES_PER_ROW,
    sample_trials=0,
    measured_at_utc=None,
    source="unbenchmarked_default",
)


def estimator_calibration_path(results_root: Path) -> Path:
    """Return ``<results_root>/estimator_calibration.json`` (never created here)."""
    return results_root / CALIBRATION_FILENAME


def _is_positive_finite(value: int | float) -> bool:
    """``True`` iff *value* is finite and ``> 0``.

    Callers MUST exclude ``bool``/non-numeric types first (Python's ``bool``
    is an ``int`` subclass) — this helper only checks magnitude/finiteness,
    kept separate so the type-narrowing ``isinstance`` guard lives once, at
    each call site, where mypy can actually narrow the argument type.
    """
    return value > 0 and value not in (float("inf"), float("-inf")) and value == value  # noqa: PLR0124 — nan check


def load_estimator_calibration(results_root: Path) -> EstimatorCalibration:
    """Read ``<results_root>/estimator_calibration.json``; fail-closed to :data:`UNCALIBRATED`.

    NEVER raises: a missing file, a non-regular file (directory, FIFO,
    socket, device — anything that could block forever or isn't a plain
    file), an oversized file, unreadable permissions, non-UTF-8 content,
    malformed JSON, a non-object top level, a schema-version mismatch, or
    any required field that is missing / wrong-typed / non-positive /
    non-finite / absurdly large all resolve to :data:`UNCALIBRATED` — an
    unbenchmarked estimate must never crash the preflight click (Golden Rule
    #10: honest fallback, not a fabricated hard failure). The upper-bound
    sanity caps (:data:`_MAX_SECONDS_PER_TRIAL`/:data:`_MAX_BYTES_PER_ROW`)
    exist because a hostile review found an unbounded value lets
    :func:`~brutex.engine.sweep_estimator.estimate_sweep_cost` overflow its
    ``trials * seconds_per_trial`` multiply to ``inf`` — which then raises
    inside FastAPI's JSON encoder at serialize time (Starlette's
    ``JSONResponse`` calls ``json.dumps(..., allow_nan=False)``), an
    unhandled 500 one layer downstream of this "never raises" promise. O(1):
    one small fixed-shape JSON file, no lake I/O.

    Parameters
    ----------
    results_root
        The sweep results root (``<lake>/sweeps`` for the dashboard path).

    Returns
    -------
    EstimatorCalibration
        A real calibrated reading, or :data:`UNCALIBRATED`.
    """
    path = estimator_calibration_path(results_root)
    try:
        if not path.is_file():
            return UNCALIBRATED
        if path.stat().st_size > _MAX_FILE_BYTES:
            return UNCALIBRATED
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return UNCALIBRATED
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return UNCALIBRATED
    if not isinstance(payload, dict):
        return UNCALIBRATED
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        return UNCALIBRATED

    seconds_per_trial = payload.get("seconds_per_trial")
    bytes_per_row = payload.get("bytes_per_row")
    sample_trials = payload.get("sample_trials")
    measured_at_utc = payload.get("measured_at_utc")
    source = payload.get("source")

    if (
        not isinstance(seconds_per_trial, (int, float))
        or isinstance(seconds_per_trial, bool)
        or not _is_positive_finite(seconds_per_trial)
        or seconds_per_trial > _MAX_SECONDS_PER_TRIAL
    ):
        return UNCALIBRATED
    if not (
        isinstance(bytes_per_row, int)
        and not isinstance(bytes_per_row, bool)
        and 0 < bytes_per_row <= _MAX_BYTES_PER_ROW
    ):
        return UNCALIBRATED
    if not (
        isinstance(sample_trials, int) and not isinstance(sample_trials, bool) and sample_trials > 0
    ):
        return UNCALIBRATED
    if measured_at_utc is not None and not isinstance(measured_at_utc, str):
        return UNCALIBRATED
    if not isinstance(source, str) or not source:
        return UNCALIBRATED

    return EstimatorCalibration(
        calibrated=True,
        seconds_per_trial=float(seconds_per_trial),
        bytes_per_row=bytes_per_row,
        sample_trials=sample_trials,
        measured_at_utc=measured_at_utc,
        source=source,
    )
