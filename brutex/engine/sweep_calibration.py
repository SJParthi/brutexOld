"""Sweep-estimator CALIBRATION store — medians over qualifying runs
(DEC-SWEEP-PREFLIGHT-ESTIMATOR-001, BUILD-SPEC §2.3/§6).

One JSON sidecar per results volume — ``<out_root>/calibration/estimator.json``
via the guard-FREE ``atomic_write_simple`` (the engine owns ``out_root``
sidecars; a disk error here must never mis-escalate to ``LakeWriteHalt``) —
keyed INSIDE the file by the hardware fingerprint (physical cores + total RAM,
deliberately NO hostname: a hostname change must never silently invalidate
calibration — BUILD-SPEC excision #10).

Lifecycle (§6): :func:`load_calibration` at every preflight (READ-ONLY,
fail-SOFT — absent → ``None``; malformed / foreign-hardware → one structlog
WARN + an uncalibrated snapshot: a corrupt calibration degrades the LABEL,
never blocks). :func:`record_run_calibration` post-run ONLY (the proceed path,
never on refusal): appends one ring entry (cap
:data:`CALIBRATION_RING_CAP` = 20, oldest evicted), re-derives MEDIANS over
QUALIFYING entries (``trials ≥ 100k ∧ wall ≥ 60 s`` — a toy smoke run records
its entry but can NEVER flip PROVISIONAL), writes best-effort (failure WARNs
``sweep.calibration.record_failed``; the completed run still exits green) and
emits ONE ``sweep.estimate_vs_actual`` event EVERY run (R20/O-j/R28).

Honest envelope (Golden Rule #10): medians are robust but ``N ≤ 20`` — the
estimate label carries provenance ("calibrated from N runs, last <date>"),
never a bare adjective; a store copied from other hardware stays PROVISIONAL;
:func:`measure_store_bytes` sums ONLY this run's ``<out_root>/<instrument>/
<tf>/`` store directories — never the lake tree (the 2.35M-file / 29.7-min
whole-walk trap, ``lake_manifest.py``).
"""

from __future__ import annotations

import json
import stat as stat_module
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from brutex.config.hardware_envelope import cached_envelope, detect_total_ram_bytes
from brutex.data.atomic_io import atomic_write_simple

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from brutex.config.hardware_envelope import HardwareEnvelope

logger = structlog.get_logger("brutex.engine.sweep_calibration")

#: The calibration sidecar's JSON schema version (golden-pinned key-set).
CALIBRATION_SCHEMA_VERSION: Final[int] = 1
#: Sidecar directory under the results volume root: ``<out_root>/calibration/``.
CALIBRATION_DIRNAME: Final[str] = "calibration"
#: Sidecar filename: ``estimator.json``.
CALIBRATION_FILENAME: Final[str] = "estimator.json"
#: Bounded run ring — the oldest entry is evicted past this cap.
CALIBRATION_RING_CAP: Final[int] = 20
#: A run must have enumerated at least this many trials to QUALIFY for the
#: derived medians (a toy smoke run can never flip PROVISIONAL).
CALIBRATION_MIN_TRIALS: Final[int] = 100_000
#: ... and must have run at least this long (wall seconds) to QUALIFY.
CALIBRATION_MIN_WALL_SECONDS: Final[float] = 60.0


def calibration_path(out_root: Path) -> Path:
    """Return the calibration sidecar path — ``<out_root>/calibration/estimator.json``."""
    return out_root / CALIBRATION_DIRNAME / CALIBRATION_FILENAME


def hardware_fingerprint(envelope: HardwareEnvelope | None = None) -> str:
    """Return the calibration hardware key — ``"cores=<physical>;ram=<bytes>"``.

    Physical cores + total physical RAM from the COMMON
    ``brutex.config.hardware_envelope`` leaf. Deliberately NO
    ``platform.node()`` (BUILD-SPEC excision #10 — a hostname change must
    never silently invalidate calibration); undetected RAM keys at the honest
    ``ram=0`` sentinel (still a stable per-machine key).

    Parameters
    ----------
    envelope
        Pre-detected envelope (defaults to the process-cached one) — injected
        in tests.
    """
    hw = cached_envelope() if envelope is None else envelope
    return f"cores={hw.physical_cores};ram={detect_total_ram_bytes()}"


@dataclass(frozen=True, slots=True)
class CalibrationSnapshot:
    """The derived calibration state one estimate consumes (immutable).

    Attributes
    ----------
    trials_per_second
        MEDIAN over qualifying ring entries; ``None`` = uncalibrated.
    bytes_per_row
        MEDIAN observed bytes per persisted row over qualifying entries;
        ``None`` when no qualifying entry carried a usable ratio.
    samples
        Total ring entries (including non-qualifying toy runs).
    qualifying_samples
        Ring entries past BOTH qualifying thresholds.
    fingerprint
        The STORED hardware fingerprint the snapshot derives from.
    hardware_match
        Stored fingerprint == the current machine's fingerprint.
    last_run_ts
        The newest ring entry's timestamp (ISO, ``Z``-suffixed), or ``None``.
    """

    trials_per_second: float | None
    bytes_per_row: float | None
    samples: int
    qualifying_samples: int
    fingerprint: str
    hardware_match: bool
    last_run_ts: str | None

    @property
    def calibrated(self) -> bool:
        """The R23 PROVISIONAL flip — hardware-matched AND ≥1 qualifying entry."""
        return self.hardware_match and self.qualifying_samples >= 1


class _StoreFormatError(ValueError):
    """Internal: the calibration sidecar JSON has the wrong shape/schema."""


def _opt_rate(value: object, *, key: str) -> float | None:
    """Narrow an optional stored rate to ``float | None`` (fail-loud on garbage)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _StoreFormatError(f"calibration {key} must be a number or null; got {value!r}")
    return float(value)


def _req_int(value: object, *, key: str) -> int:
    """Narrow a required stored counter to ``int`` (fail-loud on garbage)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _StoreFormatError(f"calibration {key} must be an integer; got {value!r}")
    return value


def _req_str(value: object, *, key: str) -> str:
    """Narrow a required stored string (fail-loud on garbage)."""
    if not isinstance(value, str):
        raise _StoreFormatError(f"calibration {key} must be a string; got {value!r}")
    return value


def _opt_str(value: object, *, key: str) -> str | None:
    """Narrow an optional stored string to ``str | None`` (fail-loud on garbage)."""
    if value is None:
        return None
    return _req_str(value, key=key)


@dataclass(frozen=True, slots=True)
class _ParsedStore:
    """Internal: one validated calibration sidecar (derived block + run ring)."""

    fingerprint: str
    trials_per_second: float | None
    bytes_per_row: float | None
    samples: int
    qualifying_samples: int
    last_run_ts: str | None
    runs: tuple[dict[str, object], ...]


def _parse_store(raw: str) -> _ParsedStore:
    """Parse + validate one sidecar body; any shape defect raises :class:`ValueError`."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise _StoreFormatError("calibration store must be a JSON object")
    if data.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
        raise _StoreFormatError(
            f"unsupported calibration schema_version {data.get('schema_version')!r} "
            f"(expected {CALIBRATION_SCHEMA_VERSION})"
        )
    fingerprint = _req_str(data.get("fingerprint"), key="fingerprint")
    derived = data.get("derived")
    if not isinstance(derived, dict):
        raise _StoreFormatError("calibration derived block must be a JSON object")
    runs_raw = data.get("runs")
    if not isinstance(runs_raw, list) or not all(isinstance(run, dict) for run in runs_raw):
        raise _StoreFormatError("calibration runs must be a list of JSON objects")
    return _ParsedStore(
        fingerprint=fingerprint,
        trials_per_second=_opt_rate(
            derived.get("trials_per_second"), key="derived.trials_per_second"
        ),
        bytes_per_row=_opt_rate(derived.get("bytes_per_row"), key="derived.bytes_per_row"),
        samples=_req_int(derived.get("samples"), key="derived.samples"),
        qualifying_samples=_req_int(
            derived.get("qualifying_samples"), key="derived.qualifying_samples"
        ),
        last_run_ts=_opt_str(derived.get("last_run_ts"), key="derived.last_run_ts"),
        runs=tuple(runs_raw),
    )


def _uncalibrated(fingerprint: str) -> CalibrationSnapshot:
    """The honest zero snapshot for a corrupt/unreadable store (degrades the LABEL)."""
    return CalibrationSnapshot(
        trials_per_second=None,
        bytes_per_row=None,
        samples=0,
        qualifying_samples=0,
        fingerprint=fingerprint,
        hardware_match=False,
        last_run_ts=None,
    )


def load_calibration(out_root: Path, *, fingerprint: str) -> CalibrationSnapshot | None:
    """Load the calibration snapshot — READ-ONLY, fail-SOFT, never creates anything.

    * Absent sidecar → ``None`` (PROVISIONAL — the normal first-boot state).
    * Malformed JSON / wrong ``schema_version`` / unreadable file → one
      structlog WARN ``sweep.calibration.load_failed`` + an UNCALIBRATED
      snapshot — a corrupt calibration degrades the LABEL, never blocks.
    * Foreign fingerprint → one WARN ``sweep.calibration.hardware_mismatch``
      + the stored figures with ``hardware_match=False`` (stays PROVISIONAL —
      a store copied from other hardware never calibrates this one).

    Write-free by construction (the preflight discipline): one ``read_text``,
    zero creates, zero mutations.

    Parameters
    ----------
    out_root
        The results volume root the sidecar lives under.
    fingerprint
        The CURRENT machine's :func:`hardware_fingerprint`.
    """
    path = calibration_path(out_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "sweep.calibration.load_failed",
            path=str(path),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _uncalibrated(fingerprint)
    try:
        store = _parse_store(raw)
    except ValueError as exc:  # _StoreFormatError + json.JSONDecodeError
        logger.warning(
            "sweep.calibration.load_failed",
            path=str(path),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _uncalibrated(fingerprint)
    if store.fingerprint != fingerprint:
        logger.warning(
            "sweep.calibration.hardware_mismatch",
            path=str(path),
            stored_fingerprint=store.fingerprint,
            current_fingerprint=fingerprint,
        )
        return CalibrationSnapshot(
            trials_per_second=store.trials_per_second,
            bytes_per_row=store.bytes_per_row,
            samples=store.samples,
            qualifying_samples=store.qualifying_samples,
            fingerprint=store.fingerprint,
            hardware_match=False,
            last_run_ts=store.last_run_ts,
        )
    return CalibrationSnapshot(
        trials_per_second=store.trials_per_second,
        bytes_per_row=store.bytes_per_row,
        samples=store.samples,
        qualifying_samples=store.qualifying_samples,
        fingerprint=store.fingerprint,
        hardware_match=True,
        last_run_ts=store.last_run_ts,
    )


@dataclass(frozen=True, slots=True)
class RunActuals:
    """One finished sweep run's measured actuals (immutable).

    Attributes
    ----------
    run_identity
        ``grid_identity(grid)`` — the run's reproducibility key.
    wall_seconds
        Measured wall-clock of the run span.
    trials_enumerated
        The ``SweepGridSummary`` enumerated fold (the actual trial count).
    tuples_run
        Tuples executed this run.
    rows_persisted
        Result rows persisted this run (the bytes/row denominator).
    result_bytes
        :func:`measure_store_bytes` before/after delta for this run's stores.
    """

    run_identity: str
    wall_seconds: float
    trials_enumerated: int
    tuples_run: int
    rows_persisted: int
    result_bytes: int


def measure_store_bytes(
    out_root: Path, *, instruments: Sequence[str], tf_labels: Sequence[str]
) -> int:
    """Sum on-disk bytes of THIS run's result-store directories — bounded, best-effort.

    Walks ONLY ``<out_root>/<instrument>/<tf>/`` for the given axes (the
    results-store layout: one Parquet + sidecar per tuple inside each such
    directory) — NEVER the lake tree and never a recursive ``out_root`` walk
    (the 2.35M-file / 29.7-min whole-walk trap). Cost is O(files in the named
    store dirs). A file that vanishes mid-walk (or a dangling symlink) is
    skipped — the tally is a best-effort delta feed, not an audit.

    Parameters
    ----------
    out_root
        The results volume root.
    instruments, tf_labels
        THIS run's swept instrument symbols and timeframe labels (duplicates
        deduped; missing directories contribute 0).
    """
    total = 0
    for instrument in dict.fromkeys(instruments):
        for tf_label in dict.fromkeys(tf_labels):
            store_dir = out_root / instrument / tf_label
            if not store_dir.is_dir():
                continue
            for entry in store_dir.iterdir():
                try:
                    entry_stat = entry.stat()
                except OSError:
                    continue  # raced-away file / dangling symlink — best-effort tally
                if stat_module.S_ISREG(entry_stat.st_mode):
                    total += entry_stat.st_size
    return total


def _utc_stamp(now: datetime | None) -> str:
    """Render *now* (default: the real clock) as a ``Z``-suffixed UTC ISO stamp."""
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ratio(actual: float, est: float) -> float | None:
    """``actual / est`` guarded against a non-positive estimate (``None`` = undefined)."""
    if est <= 0:
        return None
    return actual / est


def _as_float(value: object) -> float | None:
    """Narrow a stored ring number to ``float`` — ``None`` for garbage (skip, never crash)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _existing_ring(out_root: Path, *, fingerprint: str) -> tuple[dict[str, object], ...]:
    """Read the prior run ring for *fingerprint* — absent/corrupt/foreign ⇒ fresh ring.

    A malformed sidecar WARNs ``sweep.calibration.load_failed`` and resets; a
    FOREIGN-fingerprint sidecar WARNs ``sweep.calibration.hardware_mismatch``
    and resets (other hardware's timings must never poison this machine's
    medians).
    """
    path = calibration_path(out_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        logger.warning(
            "sweep.calibration.load_failed",
            path=str(path),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return ()
    try:
        store = _parse_store(raw)
    except ValueError as exc:  # _StoreFormatError + json.JSONDecodeError
        logger.warning(
            "sweep.calibration.load_failed",
            path=str(path),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return ()
    if store.fingerprint != fingerprint:
        logger.warning(
            "sweep.calibration.hardware_mismatch",
            path=str(path),
            stored_fingerprint=store.fingerprint,
            current_fingerprint=fingerprint,
            reset=True,
        )
        return ()
    return store.runs


def _derive_snapshot(ring: Sequence[dict[str, object]], *, fingerprint: str) -> CalibrationSnapshot:
    """Derive the MEDIAN rates over the QUALIFYING entries of a non-empty *ring*.

    A qualifying entry with an unusable ratio (zero/garbage denominator —
    e.g. ``rows_persisted == 0``) still counts toward ``qualifying_samples``
    but is excluded from that rate's median (the other rate may still derive).
    """
    qualifying = [run for run in ring if run.get("qualifying") is True]
    tps_samples: list[float] = []
    bpr_samples: list[float] = []
    for run in qualifying:
        seconds = _as_float(run.get("seconds_actual"))
        trials = _as_float(run.get("trials_actual"))
        if seconds is not None and seconds > 0 and trials is not None and trials > 0:
            tps_samples.append(trials / seconds)
        rows = _as_float(run.get("rows_persisted"))
        bytes_actual = _as_float(run.get("bytes_actual"))
        if rows is not None and rows > 0 and bytes_actual is not None and bytes_actual > 0:
            bpr_samples.append(bytes_actual / rows)
    last_ts = ring[-1].get("ts")
    return CalibrationSnapshot(
        trials_per_second=median(tps_samples) if tps_samples else None,
        bytes_per_row=median(bpr_samples) if bpr_samples else None,
        samples=len(ring),
        qualifying_samples=len(qualifying),
        fingerprint=fingerprint,
        hardware_match=True,
        last_run_ts=last_ts if isinstance(last_ts, str) else None,
    )


class _EstimateLike(Protocol):
    """The (trials, est_seconds, est_result_bytes, time_model) slice of one
    pre-run estimate (structural typing).

    ``sweep_estimate.SweepEstimate`` satisfies this; sweep_calibration cannot
    import it — even under ``TYPE_CHECKING`` — without closing the two-way
    module cycle CodeQL ``py/cyclic-import`` flags (``sweep_estimate`` imports
    THIS module for :class:`CalibrationSnapshot`, the one allowed direction —
    the ``cascade_wire._RefinedCellLike`` precedent).

    Stub bodies use ``raise NotImplementedError`` (the typing-spec protocol
    placeholder) instead of the docstring+``...`` stub: CodeQL
    ``py/ineffectual-statement`` flags an ellipsis ExprStmt AFTER a docstring
    (alerts #858-#861 here; the ``tests/browser/conftest.py``
    ``NewPageFactory`` precedent, alert #688) and pyright rejects a
    docstring-only body (reportReturnType).
    """

    @property
    def trials(self) -> int:
        """Estimated trial count (the ``bloom_capacity_hint`` twin)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def est_seconds(self) -> float:
        """The DISPLAYED time-model estimate, in seconds."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def est_result_bytes(self) -> int:
        """The disk-gate NEED basis, in bytes."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def time_model(self) -> str:
        """``"calibrated_trials"`` | ``"per_tuple_sla_fallback"``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def record_run_calibration(
    out_root: Path,
    *,
    estimate: _EstimateLike,
    actuals: RunActuals,
    fingerprint: str,
    now: datetime | None = None,
) -> CalibrationSnapshot:
    """Record one finished run's estimated-vs-actual and re-derive the medians.

    Appends one ring entry ``{estimate snapshot, actuals, qualifying flag}``
    (cap :data:`CALIBRATION_RING_CAP`, oldest evicted), recomputes the MEDIANS
    over qualifying entries, and writes the sidecar via the guard-FREE
    ``atomic_write_simple`` — BEST-EFFORT: a persist failure WARNs
    ``sweep.calibration.record_failed`` and the completed run still exits
    green (the returned snapshot is the in-memory derivation either way;
    ``MemoryError`` re-raises raw — only ``OSError`` is best-effort). Emits
    ONE ``sweep.estimate_vs_actual`` structlog event with both sides + ratios
    EVERY run, qualifying or not (R20/O-j/R28).

    Parameters
    ----------
    out_root
        The results volume root the sidecar lives under.
    estimate
        The pre-run :class:`~brutex.engine.sweep_estimate.SweepEstimate`
        (structurally, via :class:`_EstimateLike` — this module never imports
        ``sweep_estimate``, keeping the pair acyclic).
    actuals
        The measured :class:`RunActuals`.
    fingerprint
        The CURRENT machine's :func:`hardware_fingerprint` (stored verbatim).
    now
        Injectable clock (tests); ``None`` = the real UTC clock.
    """
    ts = _utc_stamp(now)
    qualifying = (
        actuals.trials_enumerated >= CALIBRATION_MIN_TRIALS
        and actuals.wall_seconds >= CALIBRATION_MIN_WALL_SECONDS
    )
    entry: dict[str, object] = {
        "ts": ts,
        "run_identity": actuals.run_identity,
        "trials_est": estimate.trials,
        "trials_actual": actuals.trials_enumerated,
        "seconds_est": estimate.est_seconds,
        "seconds_actual": actuals.wall_seconds,
        "bytes_est": estimate.est_result_bytes,
        "bytes_actual": actuals.result_bytes,
        "tuples": actuals.tuples_run,
        "rows_persisted": actuals.rows_persisted,
        "time_model": estimate.time_model,
        "qualifying": qualifying,
    }
    logger.info(
        "sweep.estimate_vs_actual",
        run_identity=actuals.run_identity,
        trials_est=estimate.trials,
        trials_actual=actuals.trials_enumerated,
        seconds_est=estimate.est_seconds,
        seconds_actual=actuals.wall_seconds,
        bytes_est=estimate.est_result_bytes,
        bytes_actual=actuals.result_bytes,
        trials_ratio=_ratio(actuals.trials_enumerated, estimate.trials),
        seconds_ratio=_ratio(actuals.wall_seconds, estimate.est_seconds),
        bytes_ratio=_ratio(actuals.result_bytes, estimate.est_result_bytes),
        time_model=estimate.time_model,
        qualifying=qualifying,
    )
    ring = [*_existing_ring(out_root, fingerprint=fingerprint), entry][-CALIBRATION_RING_CAP:]
    snapshot = _derive_snapshot(ring, fingerprint=fingerprint)
    payload: dict[str, object] = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "updated_at": ts,
        "derived": {
            "trials_per_second": snapshot.trials_per_second,
            "bytes_per_row": snapshot.bytes_per_row,
            "samples": snapshot.samples,
            "qualifying_samples": snapshot.qualifying_samples,
            "last_run_ts": snapshot.last_run_ts,
        },
        "runs": ring,
    }
    path = calibration_path(out_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(
            path,
            lambda tmp: tmp.write_text(
                json.dumps(payload, sort_keys=False) + "\n", encoding="utf-8"
            ),
        )
    except OSError as exc:
        logger.warning(
            "sweep.calibration.record_failed",
            path=str(path),
            error=str(exc),
            error_type=type(exc).__name__,
        )
    return snapshot


__all__ = [
    "CALIBRATION_DIRNAME",
    "CALIBRATION_FILENAME",
    "CALIBRATION_MIN_TRIALS",
    "CALIBRATION_MIN_WALL_SECONDS",
    "CALIBRATION_RING_CAP",
    "CALIBRATION_SCHEMA_VERSION",
    "CalibrationSnapshot",
    "RunActuals",
    "calibration_path",
    "hardware_fingerprint",
    "load_calibration",
    "measure_store_bytes",
    "record_run_calibration",
]
