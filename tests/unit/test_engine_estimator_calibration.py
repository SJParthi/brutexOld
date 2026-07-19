"""100% line + branch coverage of :mod:`brutex.engine.estimator_calibration`.

Gap-hunt ruling #13: P1 owns the ``estimator_calibration.json`` schema + the
read path; no writer exists on ``main`` yet (P2's future job), so every
production read resolves to :data:`UNCALIBRATED` until a real file lands —
this suite proves that fail-closed contract for every absent/corrupt/
malformed shape, plus the one happy path via a hand-written fixture file.
"""

from __future__ import annotations

import json
from pathlib import Path

from brutex.engine.estimator_calibration import (
    CALIBRATION_FILENAME,
    SCHEMA_VERSION,
    UNCALIBRATED,
    EstimatorCalibration,
    estimator_calibration_path,
    load_estimator_calibration,
)

_VALID_PAYLOAD: dict[str, object] = {
    "schema_version": SCHEMA_VERSION,
    "seconds_per_trial": 2.1e-5,
    "bytes_per_row": 480,
    "sample_trials": 50_000_000,
    "measured_at_utc": "2026-07-08T00:00:00Z",
    "source": "smoke_run",
}


def _write(results_root: Path, payload: object) -> None:
    results_root.mkdir(parents=True, exist_ok=True)
    (results_root / CALIBRATION_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def test_estimator_calibration_path_joins_filename(tmp_path: Path) -> None:
    assert estimator_calibration_path(tmp_path) == tmp_path / "estimator_calibration.json"


def test_uncalibrated_is_never_labelled_calibrated() -> None:
    assert UNCALIBRATED.calibrated is False
    assert UNCALIBRATED.sample_trials == 0
    assert UNCALIBRATED.measured_at_utc is None
    assert UNCALIBRATED.source == "unbenchmarked_default"
    assert UNCALIBRATED.seconds_per_trial > 0
    assert UNCALIBRATED.bytes_per_row > 0


def test_missing_file_resolves_to_uncalibrated(tmp_path: Path) -> None:
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_directory_instead_of_file_resolves_to_uncalibrated(tmp_path: Path) -> None:
    (tmp_path / CALIBRATION_FILENAME).mkdir(parents=True)
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_malformed_json_resolves_to_uncalibrated(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / CALIBRATION_FILENAME).write_text("{not json", encoding="utf-8")
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_deeply_nested_json_resolves_to_uncalibrated_not_recursion_error(tmp_path: Path) -> None:
    """Round-3 adversarial-refuter finding: a deeply-nested JSON array, well
    under the file-size cap, blows Python's ``json.loads`` parser recursion
    limit and raises ``RecursionError`` — a DIFFERENT exception than
    ``JSONDecodeError`` — which the reader must also catch to honor its
    "never raises" contract."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    nested = "[" * 20_000 + "]" * 20_000
    (tmp_path / CALIBRATION_FILENAME).write_text(nested, encoding="utf-8")
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_non_object_json_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, [1, 2, 3])
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_oversized_file_resolves_to_uncalibrated(tmp_path: Path) -> None:
    """A hostile review found an unbounded ``read_text()`` could raise
    ``MemoryError`` for an adversarially large file — the size cap must
    refuse before ever reading the bytes."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    oversized_payload = json.dumps({**_VALID_PAYLOAD, "source": "x" * (64 * 1024 + 1)})
    (tmp_path / CALIBRATION_FILENAME).write_text(oversized_payload, encoding="utf-8")
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_non_utf8_file_resolves_to_uncalibrated(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / CALIBRATION_FILENAME).write_bytes(b"\xff\xfe\x00not utf-8")
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_wrong_schema_version_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "schema_version": 999})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_missing_schema_version_resolves_to_uncalibrated(tmp_path: Path) -> None:
    payload = dict(_VALID_PAYLOAD)
    del payload["schema_version"]
    _write(tmp_path, payload)
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_non_positive_seconds_per_trial_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "seconds_per_trial": 0})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_negative_seconds_per_trial_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "seconds_per_trial": -1.0})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_nan_seconds_per_trial_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "seconds_per_trial": float("nan")})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_infinite_seconds_per_trial_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "seconds_per_trial": float("inf")})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_non_numeric_seconds_per_trial_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "seconds_per_trial": "fast"})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_bool_seconds_per_trial_resolves_to_uncalibrated(tmp_path: Path) -> None:
    """``bool`` is an ``int`` subclass in Python — must be explicitly rejected."""
    _write(tmp_path, {**_VALID_PAYLOAD, "seconds_per_trial": True})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_absurdly_large_seconds_per_trial_resolves_to_uncalibrated(tmp_path: Path) -> None:
    """A hostile review found an unbounded value overflows
    ``estimate_sweep_cost``'s multiply to ``inf``, which then breaks
    ``JSONResponse``'s ``allow_nan=False`` encoder — refuse before that."""
    _write(tmp_path, {**_VALID_PAYLOAD, "seconds_per_trial": 1e302})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_non_int_bytes_per_row_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "bytes_per_row": 480.5})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_zero_bytes_per_row_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "bytes_per_row": 0})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_absurdly_large_bytes_per_row_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "bytes_per_row": 1024**3 + 1})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_bool_bytes_per_row_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "bytes_per_row": True})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_negative_sample_trials_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "sample_trials": -1})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_zero_sample_trials_resolves_to_uncalibrated(tmp_path: Path) -> None:
    """A real smoke run cannot measure over zero trials (R23) — reject, don't fabricate."""
    _write(tmp_path, {**_VALID_PAYLOAD, "sample_trials": 0})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_non_int_sample_trials_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "sample_trials": "many"})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_bool_sample_trials_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "sample_trials": True})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_non_string_measured_at_utc_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "measured_at_utc": 12345})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_missing_source_resolves_to_uncalibrated(tmp_path: Path) -> None:
    payload = dict(_VALID_PAYLOAD)
    del payload["source"]
    _write(tmp_path, payload)
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_empty_source_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "source": ""})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_non_string_source_resolves_to_uncalibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "source": 42})
    assert load_estimator_calibration(tmp_path) == UNCALIBRATED


def test_valid_file_is_read_as_calibrated(tmp_path: Path) -> None:
    _write(tmp_path, _VALID_PAYLOAD)
    result = load_estimator_calibration(tmp_path)
    assert result == EstimatorCalibration(
        calibrated=True,
        seconds_per_trial=2.1e-5,
        bytes_per_row=480,
        sample_trials=50_000_000,
        measured_at_utc="2026-07-08T00:00:00Z",
        source="smoke_run",
    )


def test_valid_file_with_null_measured_at_is_read_as_calibrated(tmp_path: Path) -> None:
    _write(tmp_path, {**_VALID_PAYLOAD, "measured_at_utc": None})
    result = load_estimator_calibration(tmp_path)
    assert result.calibrated is True
    assert result.measured_at_utc is None
