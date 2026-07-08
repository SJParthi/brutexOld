"""Tests for `brutex.engine.sweep_calibration` (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001).

The medians-over-qualifying calibration store — BUILD-SPEC §2.3/§6, extreme
test 13 (§10.1 row 13): load fail-SOFT (absent/malformed/foreign → the label
degrades, never a block), record best-effort post-run (ring-20, qualifying
thresholds, ``sweep.estimate_vs_actual`` EVERY run), golden-pinned JSON
schema, bounded ``measure_store_bytes``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from brutex.config.hardware_envelope import HardwareEnvelope
from brutex.engine.sweep_calibration import (
    CALIBRATION_DIRNAME,
    CALIBRATION_FILENAME,
    CALIBRATION_MIN_TRIALS,
    CALIBRATION_MIN_WALL_SECONDS,
    CALIBRATION_RING_CAP,
    CALIBRATION_SCHEMA_VERSION,
    CalibrationSnapshot,
    RunActuals,
    calibration_path,
    hardware_fingerprint,
    load_calibration,
    measure_store_bytes,
    record_run_calibration,
)
from brutex.engine.sweep_estimate import (
    SweepEstimate,
    estimate_selection,
    selection_from_raw,
)
from structlog.testing import capture_logs

_FP = "cores=10;ram=51539607552"
_NOW = datetime(2026, 7, 7, 10, 0, 0, tzinfo=UTC)


def _small_estimate() -> SweepEstimate:
    """A tiny real estimate (2 tuples, 1,830 trials) feeding the record hook."""
    sel = selection_from_raw(
        symbols=["NIFTY"],
        intervals=["1minute"],
        directions=["BUY"],
        k_values=[1, 2],
        stop_losses=[None],
        take_profits=[None],
        fib_anchors=["prev_day"],
        top_k=5,
        start=datetime(2020, 1, 1, tzinfo=UTC).date(),
        end=datetime(2026, 7, 3, tzinfo=UTC).date(),
    )
    return estimate_selection(sel, calibration=None)


def _zero_estimate() -> SweepEstimate:
    """A degenerate all-zero estimate for the undefined-ratio arms."""
    return SweepEstimate(
        trials=0,
        tuples=0,
        masks=0,
        n_conditions=60,
        rows_upper_bound=0,
        est_result_bytes=0,
        store_bytes=0,
        sidecar_bytes=0,
        audit_bytes=0,
        dedup_map_bytes=0,
        est_seconds=0.0,
        est_seconds_fallback=0.0,
        time_model="per_tuple_sla_fallback",
        bytes_model="projection_fallback",
        provisional=True,
        calibration_samples=0,
        calibration_last_ts=None,
        absurd_k=False,
        warnings=(),
        notes=(),
    )


def _actuals(
    *,
    trials: int = 200_000,
    wall: float = 100.0,
    rows: int = 100,
    result_bytes: int = 98_720,
) -> RunActuals:
    """A QUALIFYING run by default (trials/s = 2,000; bytes/row = 987.2)."""
    return RunActuals(
        run_identity="run-1",
        wall_seconds=wall,
        trials_enumerated=trials,
        tuples_run=2,
        rows_persisted=rows,
        result_bytes=result_bytes,
    )


def _store_payload(fingerprint: str = _FP) -> dict[str, Any]:
    """One valid §2.3-shaped sidecar payload."""
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "updated_at": "2026-07-07T12:00:00Z",
        "derived": {
            "trials_per_second": 1210.0,
            "bytes_per_row": 987.2,
            "samples": 1,
            "qualifying_samples": 1,
            "last_run_ts": "2026-07-07T10:00:00Z",
        },
        "runs": [
            {
                "ts": "2026-07-07T10:00:00Z",
                "run_identity": "prior",
                "trials_est": 100,
                "trials_actual": 121_000,
                "seconds_est": 60.0,
                "seconds_actual": 100.0,
                "bytes_est": 10,
                "bytes_actual": 98_720,
                "tuples": 2,
                "rows_persisted": 100,
                "time_model": "per_tuple_sla_fallback",
                "qualifying": True,
            }
        ],
    }


def _write_store(out_root: Path, payload: dict[str, Any]) -> Path:
    """Write a sidecar body verbatim for the load-side tests."""
    path = calibration_path(out_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestHardwareFingerprint:
    def test_cores_plus_ram_no_hostname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The §2.3 key: physical cores + RAM, deliberately NO platform.node()."""
        monkeypatch.setattr(
            "brutex.engine.sweep_calibration.detect_total_ram_bytes", lambda: 51_539_607_552
        )
        envelope = HardwareEnvelope(
            logical_cores=14,
            physical_cores=10,
            performance_cores=10,
            efficiency_cores=4,
            source="test",
        )
        assert hardware_fingerprint(envelope) == "cores=10;ram=51539607552"
        # Excision #10 negative pin: no hostname enters the key.
        import inspect

        from brutex.engine import sweep_calibration as module

        assert "import platform" not in inspect.getsource(module)

    def test_default_envelope_is_process_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        envelope = HardwareEnvelope(
            logical_cores=8,
            physical_cores=8,
            performance_cores=8,
            efficiency_cores=0,
            source="test",
        )
        monkeypatch.setattr("brutex.engine.sweep_calibration.cached_envelope", lambda: envelope)
        monkeypatch.setattr("brutex.engine.sweep_calibration.detect_total_ram_bytes", lambda: 0)
        assert hardware_fingerprint() == "cores=8;ram=0"


class TestCalibrationPath:
    def test_sidecar_location(self, tmp_path: Path) -> None:
        assert calibration_path(tmp_path) == tmp_path / CALIBRATION_DIRNAME / CALIBRATION_FILENAME


class TestLoadCalibration:
    def test_absent_store_is_none(self, tmp_path: Path) -> None:
        assert load_calibration(tmp_path, fingerprint=_FP) is None

    def test_valid_matching_store_loads_calibrated(self, tmp_path: Path) -> None:
        _write_store(tmp_path, _store_payload())
        snapshot = load_calibration(tmp_path, fingerprint=_FP)
        assert snapshot is not None
        assert snapshot.trials_per_second == 1210.0
        assert snapshot.bytes_per_row == 987.2
        assert snapshot.samples == 1
        assert snapshot.qualifying_samples == 1
        assert snapshot.fingerprint == _FP
        assert snapshot.hardware_match is True
        assert snapshot.last_run_ts == "2026-07-07T10:00:00Z"
        assert snapshot.calibrated is True

    def test_null_rates_load_as_none(self, tmp_path: Path) -> None:
        payload = _store_payload()
        payload["derived"]["trials_per_second"] = None
        payload["derived"]["bytes_per_row"] = None
        payload["derived"]["last_run_ts"] = None
        _write_store(tmp_path, payload)
        snapshot = load_calibration(tmp_path, fingerprint=_FP)
        assert snapshot is not None
        assert snapshot.trials_per_second is None
        assert snapshot.bytes_per_row is None
        assert snapshot.last_run_ts is None

    def test_foreign_fingerprint_stays_provisional_with_warn(self, tmp_path: Path) -> None:
        """R23: a store copied from other hardware NEVER calibrates this one."""
        _write_store(tmp_path, _store_payload(fingerprint="cores=64;ram=1"))
        with capture_logs() as logs:
            snapshot = load_calibration(tmp_path, fingerprint=_FP)
        assert snapshot is not None
        assert snapshot.hardware_match is False
        assert snapshot.calibrated is False
        assert snapshot.fingerprint == "cores=64;ram=1"
        assert snapshot.trials_per_second == 1210.0  # figures kept, label degraded
        assert [entry["event"] for entry in logs] == ["sweep.calibration.hardware_mismatch"]

    def test_malformed_json_degrades_the_label_never_blocks(self, tmp_path: Path) -> None:
        path = calibration_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        with capture_logs() as logs:
            snapshot = load_calibration(tmp_path, fingerprint=_FP)
        assert snapshot is not None
        assert snapshot.calibrated is False
        assert snapshot.samples == 0
        assert snapshot.trials_per_second is None
        assert [entry["event"] for entry in logs] == ["sweep.calibration.load_failed"]

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda p: p.update(schema_version=2), id="wrong-schema-version"),
            pytest.param(lambda p: p.update(fingerprint=5), id="non-string-fingerprint"),
            pytest.param(lambda p: p.update(derived="x"), id="derived-not-object"),
            pytest.param(lambda p: p.update(runs="x"), id="runs-not-list"),
            pytest.param(lambda p: p.update(runs=[1]), id="run-entry-not-object"),
            pytest.param(
                lambda p: p["derived"].update(trials_per_second="fast"),
                id="garbage-rate",
            ),
            pytest.param(lambda p: p["derived"].update(bytes_per_row=True), id="bool-rate"),
            pytest.param(lambda p: p["derived"].update(samples="3"), id="garbage-samples"),
            pytest.param(lambda p: p["derived"].update(qualifying_samples=True), id="bool-samples"),
            pytest.param(lambda p: p["derived"].update(last_run_ts=7), id="garbage-ts"),
        ],
    )
    def test_shape_defects_degrade_with_one_warn(self, tmp_path: Path, mutate: object) -> None:
        payload = _store_payload()
        mutate(payload)  # type: ignore[operator]
        _write_store(tmp_path, payload)
        with capture_logs() as logs:
            snapshot = load_calibration(tmp_path, fingerprint=_FP)
        assert snapshot is not None
        assert snapshot.calibrated is False
        assert snapshot.samples == 0
        assert [entry["event"] for entry in logs] == ["sweep.calibration.load_failed"]

    def test_non_object_top_level_degrades(self, tmp_path: Path) -> None:
        path = calibration_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("[]", encoding="utf-8")
        with capture_logs() as logs:
            snapshot = load_calibration(tmp_path, fingerprint=_FP)
        assert snapshot is not None
        assert snapshot.calibrated is False
        assert [entry["event"] for entry in logs] == ["sweep.calibration.load_failed"]

    def test_unreadable_store_degrades(self, tmp_path: Path) -> None:
        # estimator.json as a DIRECTORY → read_text raises IsADirectoryError (OSError).
        calibration_path(tmp_path).mkdir(parents=True)
        with capture_logs() as logs:
            snapshot = load_calibration(tmp_path, fingerprint=_FP)
        assert snapshot is not None
        assert snapshot.calibrated is False
        assert [entry["event"] for entry in logs] == ["sweep.calibration.load_failed"]

    def test_load_is_write_free(self, tmp_path: Path) -> None:
        """Preflight discipline: an absent store is NOT created by loading."""
        assert load_calibration(tmp_path, fingerprint=_FP) is None
        assert list(tmp_path.iterdir()) == []


def test_r20_oj_estimated_vs_actual_logged_and_self_corrects(tmp_path: Path) -> None:
    """R20/O-j/R28 / extreme test 13 — logged EVERY run; the NEXT estimate self-corrects.

    One qualifying run (200k trials / 100 s ⇒ 2,000 trials/s) is recorded:
    the ``sweep.estimate_vs_actual`` event carries both sides + ratios, the
    sidecar persists, and a second ``estimate_selection`` against the
    reloaded snapshot moves the displayed hours onto the MEASURED rate
    (``trials / 2000``) instead of the 30 s/tuple placeholder.
    """
    estimate = _small_estimate()
    assert estimate.provisional is True
    assert estimate.est_seconds == 2 * 30.0
    with capture_logs() as logs:
        snapshot = record_run_calibration(
            tmp_path, estimate=estimate, actuals=_actuals(), fingerprint=_FP, now=_NOW
        )
    events = {entry["event"]: entry for entry in logs}
    assert "sweep.estimate_vs_actual" in events
    event = events["sweep.estimate_vs_actual"]
    assert event["trials_est"] == estimate.trials
    assert event["trials_actual"] == 200_000
    assert event["seconds_ratio"] == pytest.approx(100.0 / 60.0)
    assert event["qualifying"] is True
    assert snapshot.calibrated is True
    assert snapshot.trials_per_second == pytest.approx(2_000.0)
    assert snapshot.bytes_per_row == pytest.approx(987.2)
    # Persisted + reloadable (the §6 lifecycle).
    reloaded = load_calibration(tmp_path, fingerprint=_FP)
    assert reloaded == snapshot
    # The NEXT estimate self-corrects onto the measured rate (numeric pin).
    sel = selection_from_raw(
        symbols=["NIFTY"],
        intervals=["1minute"],
        directions=["BUY"],
        k_values=[1, 2],
        stop_losses=[None],
        take_profits=[None],
        fib_anchors=["prev_day"],
        top_k=5,
        start=_NOW.date(),
        end=_NOW.date(),
    )
    corrected = estimate_selection(sel, calibration=reloaded)
    assert corrected.provisional is False
    assert corrected.est_seconds == pytest.approx(corrected.trials / 2_000.0)
    assert corrected.est_seconds != estimate.est_seconds
    # A sub-threshold TOY run records its entry but NEVER flips PROVISIONAL.
    toy_root = tmp_path / "toy"
    with capture_logs() as toy_logs:
        toy = record_run_calibration(
            toy_root,
            estimate=_small_estimate(),
            actuals=_actuals(trials=500, wall=2.0),
            fingerprint=_FP,
            now=_NOW,
        )
    assert any(entry["event"] == "sweep.estimate_vs_actual" for entry in toy_logs)
    assert toy.samples == 1
    assert toy.qualifying_samples == 0
    assert toy.calibrated is False
    assert toy.trials_per_second is None
    toy_estimate = estimate_selection(sel, calibration=load_calibration(toy_root, fingerprint=_FP))
    assert toy_estimate.provisional is True


class TestQualifyingThresholds:
    def test_exact_thresholds_qualify(self, tmp_path: Path) -> None:
        snapshot = record_run_calibration(
            tmp_path,
            estimate=_small_estimate(),
            actuals=_actuals(trials=CALIBRATION_MIN_TRIALS, wall=CALIBRATION_MIN_WALL_SECONDS),
            fingerprint=_FP,
            now=_NOW,
        )
        assert snapshot.qualifying_samples == 1

    def test_trials_below_floor_do_not_qualify(self, tmp_path: Path) -> None:
        snapshot = record_run_calibration(
            tmp_path,
            estimate=_small_estimate(),
            actuals=_actuals(trials=CALIBRATION_MIN_TRIALS - 1, wall=100.0),
            fingerprint=_FP,
            now=_NOW,
        )
        assert snapshot.qualifying_samples == 0

    def test_wall_below_floor_does_not_qualify(self, tmp_path: Path) -> None:
        snapshot = record_run_calibration(
            tmp_path,
            estimate=_small_estimate(),
            actuals=_actuals(trials=200_000, wall=59.9),
            fingerprint=_FP,
            now=_NOW,
        )
        assert snapshot.qualifying_samples == 0


class TestRecordRunCalibration:
    def test_sidecar_golden_key_set(self, tmp_path: Path) -> None:
        """The §2.3 golden-pinned JSON schema (top-level / derived / run key-sets)."""
        record_run_calibration(
            tmp_path, estimate=_small_estimate(), actuals=_actuals(), fingerprint=_FP, now=_NOW
        )
        data = json.loads(calibration_path(tmp_path).read_text(encoding="utf-8"))
        assert set(data) == {"schema_version", "fingerprint", "updated_at", "derived", "runs"}
        assert data["schema_version"] == 1
        assert data["fingerprint"] == _FP
        assert data["updated_at"] == "2026-07-07T10:00:00Z"
        assert set(data["derived"]) == {
            "trials_per_second",
            "bytes_per_row",
            "samples",
            "qualifying_samples",
            "last_run_ts",
        }
        assert len(data["runs"]) == 1
        assert set(data["runs"][0]) == {
            "ts",
            "run_identity",
            "trials_est",
            "trials_actual",
            "seconds_est",
            "seconds_actual",
            "bytes_est",
            "bytes_actual",
            "tuples",
            "rows_persisted",
            "time_model",
            "qualifying",
        }
        assert data["runs"][0]["ts"] == "2026-07-07T10:00:00Z"
        assert data["derived"]["last_run_ts"] == "2026-07-07T10:00:00Z"

    def test_ring_evicts_oldest_past_cap(self, tmp_path: Path) -> None:
        for i in range(CALIBRATION_RING_CAP + 1):
            record_run_calibration(
                tmp_path,
                estimate=_small_estimate(),
                actuals=_actuals(),
                fingerprint=_FP,
                now=_NOW + timedelta(minutes=i),
            )
        data = json.loads(calibration_path(tmp_path).read_text(encoding="utf-8"))
        assert len(data["runs"]) == CALIBRATION_RING_CAP
        # The FIRST stamp (10:00:00Z) was evicted; the ring starts at 10:01:00Z.
        assert data["runs"][0]["ts"] == "2026-07-07T10:01:00Z"
        assert data["runs"][-1]["ts"] == "2026-07-07T10:20:00Z"

    def test_median_law_odd_and_even(self, tmp_path: Path) -> None:
        """MEDIANS over qualifying entries (D7) — pinned numerically, odd + even N."""
        rates = [(200_000, 200.0), (500_000, 100.0), (200_000, 100.0)]  # 1,000 / 5,000 / 2,000 t/s
        snapshot = CalibrationSnapshot(
            trials_per_second=None,
            bytes_per_row=None,
            samples=0,
            qualifying_samples=0,
            fingerprint=_FP,
            hardware_match=True,
            last_run_ts=None,
        )
        for i, (trials, wall) in enumerate(rates):
            snapshot = record_run_calibration(
                tmp_path,
                estimate=_small_estimate(),
                actuals=_actuals(trials=trials, wall=wall),
                fingerprint=_FP,
                now=_NOW + timedelta(minutes=i),
            )
        assert snapshot.trials_per_second == pytest.approx(2_000.0)  # odd N → middle
        snapshot = record_run_calibration(
            tmp_path,
            estimate=_small_estimate(),
            actuals=_actuals(trials=400_000, wall=100.0),  # 4,000 t/s
            fingerprint=_FP,
            now=_NOW + timedelta(minutes=3),
        )
        assert snapshot.trials_per_second == pytest.approx(3_000.0)  # even N → mid-mean

    def test_qualifying_run_with_zero_rows_still_counts_but_skips_bytes_rate(
        self, tmp_path: Path
    ) -> None:
        snapshot = record_run_calibration(
            tmp_path,
            estimate=_small_estimate(),
            actuals=_actuals(rows=0, result_bytes=0),
            fingerprint=_FP,
            now=_NOW,
        )
        assert snapshot.qualifying_samples == 1
        assert snapshot.trials_per_second == pytest.approx(2_000.0)
        assert snapshot.bytes_per_row is None

    def test_foreign_existing_store_resets_ring_with_warn(self, tmp_path: Path) -> None:
        _write_store(tmp_path, _store_payload(fingerprint="cores=64;ram=1"))
        with capture_logs() as logs:
            snapshot = record_run_calibration(
                tmp_path, estimate=_small_estimate(), actuals=_actuals(), fingerprint=_FP, now=_NOW
            )
        assert snapshot.samples == 1  # the foreign ring was discarded
        events = [entry["event"] for entry in logs]
        assert "sweep.calibration.hardware_mismatch" in events
        data = json.loads(calibration_path(tmp_path).read_text(encoding="utf-8"))
        assert data["fingerprint"] == _FP

    def test_malformed_existing_store_resets_ring_with_warn(self, tmp_path: Path) -> None:
        path = calibration_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        with capture_logs() as logs:
            snapshot = record_run_calibration(
                tmp_path, estimate=_small_estimate(), actuals=_actuals(), fingerprint=_FP, now=_NOW
            )
        assert snapshot.samples == 1
        assert "sweep.calibration.load_failed" in [entry["event"] for entry in logs]

    def test_garbage_ring_numbers_are_skipped_never_crash(self, tmp_path: Path) -> None:
        payload = _store_payload()
        payload["runs"] = [
            {
                "ts": "2026-07-07T09:00:00Z",
                "qualifying": True,
                "seconds_actual": "wedged",  # garbage → skipped from trials/s
                "trials_actual": 200_000,
                "rows_persisted": 0,  # zero denominator → skipped from bytes/row
                "bytes_actual": 5,
            },
            {
                "ts": "2026-07-07T09:30:00Z",
                "qualifying": True,
                "seconds_actual": 100.0,
                "trials_actual": True,  # bool masquerading as a count → skipped
                "rows_persisted": 10,
                "bytes_actual": "x",  # garbage → skipped from bytes/row
            },
        ]
        _write_store(tmp_path, payload)
        snapshot = record_run_calibration(
            tmp_path, estimate=_small_estimate(), actuals=_actuals(), fingerprint=_FP, now=_NOW
        )
        # 2 garbage-qualifying priors + the new run: only the new run's rates derive.
        assert snapshot.samples == 3
        assert snapshot.qualifying_samples == 3
        assert snapshot.trials_per_second == pytest.approx(2_000.0)
        assert snapshot.bytes_per_row == pytest.approx(987.2)

    def test_record_failure_warns_and_still_returns_snapshot(self, tmp_path: Path) -> None:
        """BEST-EFFORT persist: a blocked sidecar dir WARNs; the run still exits green."""
        (tmp_path / CALIBRATION_DIRNAME).write_text("i am a file", encoding="utf-8")
        with capture_logs() as logs:
            snapshot = record_run_calibration(
                tmp_path, estimate=_small_estimate(), actuals=_actuals(), fingerprint=_FP, now=_NOW
            )
        assert snapshot.calibrated is True  # in-memory derivation unaffected
        events = [entry["event"] for entry in logs]
        assert "sweep.calibration.record_failed" in events
        assert "sweep.estimate_vs_actual" in events  # still logged EVERY run

    def test_unreadable_prior_store_and_blocked_replace_both_warn(self, tmp_path: Path) -> None:
        # estimator.json as a DIRECTORY: the prior-ring read hits the OSError arm
        # AND the atomic replace onto a directory hits the record_failed arm.
        calibration_path(tmp_path).mkdir(parents=True)
        with capture_logs() as logs:
            snapshot = record_run_calibration(
                tmp_path, estimate=_small_estimate(), actuals=_actuals(), fingerprint=_FP, now=_NOW
            )
        assert snapshot.samples == 1
        events = [entry["event"] for entry in logs]
        assert events.count("sweep.calibration.load_failed") == 1
        assert events.count("sweep.calibration.record_failed") == 1

    def test_zero_estimates_yield_none_ratios(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            record_run_calibration(
                tmp_path,
                estimate=_zero_estimate(),
                actuals=_actuals(trials=500, wall=2.0),
                fingerprint=_FP,
                now=_NOW,
            )
        event = next(e for e in logs if e["event"] == "sweep.estimate_vs_actual")
        assert event["trials_ratio"] is None
        assert event["seconds_ratio"] is None
        assert event["bytes_ratio"] is None

    def test_clock_arms(self, tmp_path: Path) -> None:
        # Aware non-UTC clock converts to Z; a naive clock is assumed UTC.
        ist = timezone(timedelta(hours=5, minutes=30))
        record_run_calibration(
            tmp_path,
            estimate=_small_estimate(),
            actuals=_actuals(),
            fingerprint=_FP,
            now=datetime(2026, 7, 7, 15, 30, 0, tzinfo=ist),
        )
        data = json.loads(calibration_path(tmp_path).read_text(encoding="utf-8"))
        assert data["updated_at"] == "2026-07-07T10:00:00Z"
        record_run_calibration(
            tmp_path,
            estimate=_small_estimate(),
            actuals=_actuals(),
            fingerprint=_FP,
            now=datetime(2026, 7, 7, 11, 0, 0),  # naive → assumed UTC
        )
        data = json.loads(calibration_path(tmp_path).read_text(encoding="utf-8"))
        assert data["updated_at"] == "2026-07-07T11:00:00Z"

    def test_real_clock_default(self, tmp_path: Path) -> None:
        record_run_calibration(
            tmp_path, estimate=_small_estimate(), actuals=_actuals(), fingerprint=_FP
        )
        data = json.loads(calibration_path(tmp_path).read_text(encoding="utf-8"))
        assert data["updated_at"].endswith("Z")


class TestMeasureStoreBytes:
    def test_sums_only_the_named_store_dirs(self, tmp_path: Path) -> None:
        store = tmp_path / "NIFTY" / "1minute"
        store.mkdir(parents=True)
        (store / "a.parquet").write_bytes(b"x" * 10)
        (store / "a.parquet.trials.json").write_bytes(b"y" * 20)
        (store / "subdir").mkdir()  # directories are never counted
        (tmp_path / "audit").mkdir()
        (tmp_path / "audit" / "chain.jsonl").write_bytes(b"z" * 1000)  # not a store dir
        other = tmp_path / "BANKNIFTY" / "5minute"
        other.mkdir(parents=True)
        (other / "b.parquet").write_bytes(b"w" * 5)  # not in THIS run's axes
        total = measure_store_bytes(
            tmp_path, instruments=["NIFTY", "NIFTY"], tf_labels=["1minute", "1minute", "5minute"]
        )
        assert total == 30  # dedup: each dir counted once; missing NIFTY/5minute → 0

    def test_dangling_symlink_is_skipped(self, tmp_path: Path) -> None:
        store = tmp_path / "NIFTY" / "1minute"
        store.mkdir(parents=True)
        (store / "real.parquet").write_bytes(b"x" * 7)
        os.symlink(store / "vanished.parquet", store / "dangling.parquet")
        assert measure_store_bytes(tmp_path, instruments=["NIFTY"], tf_labels=["1minute"]) == 7

    def test_empty_axes_and_missing_root(self, tmp_path: Path) -> None:
        assert measure_store_bytes(tmp_path / "absent", instruments=[], tf_labels=[]) == 0
        assert (
            measure_store_bytes(tmp_path / "absent", instruments=["NIFTY"], tf_labels=["1day"]) == 0
        )
