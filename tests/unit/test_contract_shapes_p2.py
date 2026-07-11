"""Cross-phase contract-fixture gate (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6, graft C).

Pins the SIX cross-phase contract shapes in ONE every-push file that imports
ONLY Phase-2 modules (plus stdlib) — it must pass with ZERO other-phase code
present, so a P1/P3/P4 session can code against these fixtures from minute
zero and a P2 refactor can never silently drift a shared shape:

* IN  — P1 → P2: the preflight-verdict fixture through ``fold_preflight``.
* OUT — P2 → P3: the stage-stamp sidecar shape (field names EXACT).
* OUT — P2 → P3/P4/P6: the run-manifest top-level contract keys.
* OUT — P2 → P4: the flight-recorder ``contract_view`` record shape.
* OUT — P2 → P4: the exit-status artifact + chain-map artifact shapes.
* LAW — per_door_kills ABSENT ≠ ZERO (D-8): consumers must distinguish an
  unobservable door (absent) from a door that genuinely killed nothing (0).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Final

from brutex.engine.run_verdicts import load_run_verdicts
from brutex.engine.sweep_exit_status import (
    RunState,
    read_exit_status,
    write_exit_status,
)
from brutex.observability.chain_map import LINKS, build_chain_map, write_chain_map
from brutex.observability.flight_recorder import FlightRecorder, iter_run_records
from brutex.observability.run_manifest import (
    ManifestAccumulator,
    fold_preflight,
    load_manifest,
    manifest_path,
)
from brutex.observability.stage_stamp import (
    StampVerdict,
    validate_stage_stamp,
    write_stage_stamp,
)

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r28_manifest_contract_top_level_keys_exact": ("R28",),
    "test_r28_per_door_kills_absent_is_not_zero": ("R28",),
    "test_r42_stage_stamp_sidecar_field_names_exact": ("R42",),
    "test_r41_events_contract_view_shape_exact": ("R41",),
    "test_r16_preflight_verdict_fixture_folds_full_contract": ("R16",),
    "test_r16_preflight_missing_estimator_is_honest_null": ("R16",),
    "test_g5_exit_status_artifact_shape_exact": ("G5",),
    "test_r41_chain_map_artifact_shape_exact": ("R41",),
    "test_g8_verdicts_reader_shape_and_mask_hex_law": ("G8",),
    "test_module_imports_zero_other_phase_code": ("R16",),
}

_RUN_ID: Final[str] = "ab" * 32

#: The P1 → P2 preflight-verdict contract fixture (contracts file #1) — field
#: names EXACT; selection echoes ride any other key.
PREFLIGHT_VERDICT_FIXTURE: Final[dict[str, object]] = {
    "ok": True,
    "refusals": [
        {
            "symbol": "NIFTY",
            "tf": "5minute",
            "missing_spans": [["2024-01-02", "2024-01-05"]],
            "reason": "derived TF stale vs 1m",
        }
    ],
    "clamps": [{"field": "start", "requested": "2019-01-01", "clamped_to": "2020-01-01"}],
    "estimator": {"trials": 1770, "est_seconds_provisional": 42, "est_bytes": 1048576},
    "symbols": ["NIFTY"],
    "intervals": ["1minute"],
}


def test_r28_manifest_contract_top_level_keys_exact(tmp_path: Path) -> None:
    acc = ManifestAccumulator(
        results_root=tmp_path,
        run_id=_RUN_ID,
        selection={"symbols": ["NIFTY"]},
        monotonic=time.monotonic,
        wall=lambda: 1_700_000_000.0,
    )
    acc.add_door_kills("min_hits", 3)
    acc.add_dedup_skip("resume", 2)
    path = acc.flush()
    assert path == manifest_path(tmp_path, _RUN_ID)
    doc = json.loads(Path(str(path)).read_text())
    # Contracts file #3: the nine contract keys, plus the documented additive set.
    assert set(doc) == {
        "run_id",
        "selection",
        "estimate",
        "actual",
        "per_stage_timings",
        "per_door_kills",
        "dedup_skips",
        "tripwires",
        "suspect",
        "schema_version",
        "attempt",
        "exit_code",
        "updated_utc",
        "suspect_reasons",
        "dedup_skips_detail",
        "preflight",
        "recorder_degraded",
    }
    assert doc["run_id"] == _RUN_ID
    assert doc["estimate"] is None  # honest null until P1 wires (D-17)
    assert doc["dedup_skips"] == 2  # the scalar total (D-7)
    assert doc["dedup_skips_detail"] == {"cache": 0, "resume": 2, "sentinel": 0}
    assert doc["schema_version"] == 1
    assert doc["updated_utc"] == 1_700_000_000.0


def test_r28_per_door_kills_absent_is_not_zero(tmp_path: Path) -> None:
    """D-8 consumer law: an UNOBSERVABLE door is ABSENT — never null, never a
    fabricated 0 — and a consumer can tell the two apart."""
    acc = ManifestAccumulator(
        results_root=tmp_path,
        run_id=_RUN_ID,
        selection={},
    )
    acc.add_door_kills("min_hits", 0)  # observed: genuinely killed nothing
    acc.flush()
    manifest = load_manifest(tmp_path, _RUN_ID)
    doors = manifest.per_door_kills
    assert doors["min_hits"] == 0  # observed zero IS present
    assert "dsr" not in doors  # unobserved door is ABSENT (never fabricated)
    # The consumer-side distinction (the contract assertion, D-8):
    assert doors.get("dsr") is None
    assert doors.get("min_hits") == 0
    raw = json.loads(manifest_path(tmp_path, _RUN_ID).read_text())
    assert {row["door"] for row in raw["per_door_kills"]} == {"min_hits"}


def test_r42_stage_stamp_sidecar_field_names_exact(tmp_path: Path) -> None:
    artifact = tmp_path / "cascade-report.json"
    artifact.write_bytes(b'{"golden": true}')
    sidecar = write_stage_stamp(
        artifact,
        stage="cascade_report",
        run_id=_RUN_ID,
        extra={"data_snapshot_hash": "cd" * 32},
    )
    doc = json.loads(sidecar.read_text())
    # Contracts file #2: field names EXACT; extras additive.
    assert set(doc) == {
        "stage",
        "run_id",
        "content_hash",
        "produced_at_utc",
        "schema_version",
        "extra",
    }
    assert doc["stage"] == "cascade_report"
    assert doc["run_id"] == _RUN_ID
    assert len(doc["content_hash"]) == 64  # full sha256 hex, never truncated
    assert doc["extra"] == {"data_snapshot_hash": "cd" * 32}
    verdict, stamp = validate_stage_stamp(artifact, expected_stage="cascade_report")
    assert verdict is StampVerdict.OK
    assert stamp is not None
    assert stamp.content_hash == doc["content_hash"]


def test_r41_events_contract_view_shape_exact(tmp_path: Path) -> None:
    recorder = FlightRecorder.open(tmp_path, run_id=_RUN_ID, attempt=1)
    assert recorder.record("RUN_START", {"workers": 2}) is True
    recorder.close()
    records = list(iter_run_records(tmp_path, _RUN_ID))
    assert len(records) == 1
    # Contracts file #6: one record = {ts_utc, run_id, kind, payload}.
    assert set(records[0]) == {"ts_utc", "run_id", "kind", "payload"}
    assert records[0]["run_id"] == _RUN_ID
    assert records[0]["kind"] == "RUN_START"
    payload = records[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["workers"] == 2
    assert payload["attempt"] == 1  # every payload carries the attempt ordinal
    assert isinstance(records[0]["ts_utc"], float)


def test_r16_preflight_verdict_fixture_folds_full_contract() -> None:
    selection, estimate, preflight = fold_preflight(PREFLIGHT_VERDICT_FIXTURE)
    assert selection == {"symbols": ["NIFTY"], "intervals": ["1minute"]}
    assert estimate == {"trials": 1770, "est_seconds_provisional": 42, "est_bytes": 1048576}
    assert preflight == {
        "refusals": PREFLIGHT_VERDICT_FIXTURE["refusals"],
        "clamps": PREFLIGHT_VERDICT_FIXTURE["clamps"],
    }


def test_r16_preflight_missing_estimator_is_honest_null() -> None:
    verdict = {k: v for k, v in PREFLIGHT_VERDICT_FIXTURE.items() if k != "estimator"}
    _selection, estimate, preflight = fold_preflight(verdict)
    assert estimate is None  # honest null — never a fabricated estimate
    assert preflight["refusals"] == PREFLIGHT_VERDICT_FIXTURE["refusals"]


def test_g5_exit_status_artifact_shape_exact(tmp_path: Path) -> None:
    write_exit_status(
        tmp_path,
        run_id=_RUN_ID,
        state=RunState.COMPLETE,
        exit_code=0,
        attempt=1,
        pid=4242,
        suspect=False,
        clock=lambda: 200.0,
    )
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert set(doc) == {
        "run_id",
        "state",
        "exit_code",
        "refusal_text",
        "error_type",
        "attempt",
        "pid",
        "wall_started_utc",
        "wall_ended_utc",
        "suspect",
    }
    assert doc["state"] == "complete"
    assert doc["exit_code"] == 0
    assert doc["refusal_text"] is None
    assert doc["pid"] == 4242


def test_r41_chain_map_artifact_shape_exact(tmp_path: Path) -> None:
    chain = build_chain_map(
        progress=None,
        stamps={},
        suspect=False,
        exit_status={"state": "complete", "exit_code": 0},
    )
    write_chain_map(tmp_path, _RUN_ID, chain, wall=lambda: 1_700_000_000.0)
    doc = json.loads((tmp_path / "runs" / _RUN_ID / "chain-map.json").read_text())
    assert set(doc) == {"run_id", "links", "suspect", "schema_version", "updated_utc"}
    assert doc["run_id"] == _RUN_ID
    assert tuple(doc["links"]) == LINKS  # preflight→sweep→stage3→cascade→artifacts
    for link_doc in doc["links"].values():
        assert set(link_doc) == {"state", "detail"}
        assert link_doc["state"] in {"pending", "running", "green", "amber", "red"}
    assert doc["updated_utc"] == 1_700_000_000.0


def test_g8_verdicts_reader_shape_and_mask_hex_law(tmp_path: Path) -> None:
    """The verdicts artifact P3 joins at /results: mask_hex keys are lowercase,
    un-prefixed, un-padded; values carry the two verdict axes."""
    run_dir = tmp_path / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "verdicts.json").write_text(
        json.dumps(
            {
                "run_id": _RUN_ID,
                "by_mask": {"ff": {"stage3": "refined", "cascade": "passed"}},
                "schema_version": 1,
            }
        )
    )
    by_mask = load_run_verdicts(tmp_path, _RUN_ID)
    assert by_mask == {"ff": {"stage3": "refined", "cascade": "passed"}}
    assert load_run_verdicts(tmp_path, "cd" * 32) == {}  # missing ⇒ {} silently


def test_module_imports_zero_other_phase_code() -> None:
    """The gate's own guarantee: only Phase-2 modules (+ stdlib/pytest) are
    imported, so the file passes with ZERO other-phase code present."""
    import ast as ast_mod

    allowed_prefixes = (
        "brutex.engine.run_verdicts",
        "brutex.engine.sweep_exit_status",
        "brutex.observability.chain_map",
        "brutex.observability.flight_recorder",
        "brutex.observability.run_manifest",
        "brutex.observability.stage_stamp",
    )
    tree = ast_mod.parse(Path(__file__).read_text(encoding="utf-8"))
    brutex_imports = [
        node.module
        for node in ast_mod.walk(tree)
        if isinstance(node, ast_mod.ImportFrom)
        and node.module is not None
        and node.module.startswith("brutex")
    ]
    assert brutex_imports  # the pin is non-vacuous
    for module in brutex_imports:
        assert module.startswith(allowed_prefixes), f"other-phase import: {module}"
