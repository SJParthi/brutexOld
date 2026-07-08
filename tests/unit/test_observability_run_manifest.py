"""Unit tests for :mod:`brutex.observability.run_manifest` — the run-core manifest.

DEC-SWEEP-UNKILLABLE-CORE-001 (MIGRATION.md #1605). 100% line + branch, with the
two binding laws pinned:

* **D-7** — ``dedup_skips`` is a SCALAR int + the additive ``dedup_skips_detail``
  ``{cache, resume, sentinel}`` breakdown.
* **D-8** — ``per_door_kills`` ABSENT-key law: an unobservable door is ABSENT
  (no row), never ``null``/fabricated-``0``; on disk it is the ordered
  ``[{door, killed}]`` list in :data:`CANONICAL_DOOR_ORDER`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from brutex.engine.run_layout import RunLayout
from brutex.observability.run_manifest import (
    CANONICAL_DOOR_ORDER,
    DEDUP_DETAIL_KEYS,
    MANIFEST_SCHEMA_VERSION,
    RunManifest,
    RunManifestError,
    funnel_rows,
)

_RUN_ID = "d" * 64


def _layout(tmp_path: Path) -> RunLayout:
    return RunLayout(results_root=tmp_path / "sweeps", run_id=_RUN_ID)


# ─────────────────────────── construction + defaults ───────────────────────────


def test_defaults_are_honest_and_empty() -> None:
    m = RunManifest(run_id=_RUN_ID)
    assert m.estimate is None  # honest null until P1 wires
    assert m.selection == {}
    assert m.per_door_kills == {}  # ABSENT-key law: nothing observed yet
    assert m.dedup_skips == 0
    assert m.dedup_skips_detail == dict.fromkeys(DEDUP_DETAIL_KEYS, 0)
    assert m.preflight == {"refusals": [], "clamps": []}
    assert m.suspect is False
    assert m.schema_version == MANIFEST_SCHEMA_VERSION


def test_to_payload_round_trip_full() -> None:
    m = RunManifest(
        run_id=_RUN_ID,
        selection={"instruments": ["NIFTY"]},
        estimate={"trials": 10, "est_seconds_provisional": 5, "est_bytes": 1024},
        actual={"trials": 10, "funnel": funnel_rows([("enumerated", 10), ("passed", 3)])},
        per_stage_timings={"stage1a": 1.5, "cascade": 2},
        per_door_kills={"min_hits": 4, "dsr": 2},
        dedup_skips=7,
        tripwires=({"name": "rate_storm", "detail": "x"},),
        suspect=True,
        attempt=2,
        exit_code=0,
        suspect_reasons=("funnel_mismatch",),
        dedup_skips_detail={"cache": 5, "resume": 2},
        preflight={"refusals": [{"symbol": "SENSEX"}], "clamps": []},
        recorder_degraded=True,
    )
    payload = m.to_payload()
    back = RunManifest.from_payload(payload)
    assert back.to_payload() == payload
    assert back.per_door_kills == {"min_hits": 4, "dsr": 2}
    assert back.dedup_skips == 7
    assert back.suspect is True


def test_estimate_none_serialises_null() -> None:
    payload = RunManifest(run_id=_RUN_ID).to_payload()
    assert payload["estimate"] is None


# ─────────────────────────── D-8 per_door_kills ordering ───────────────────────────


def test_per_door_kills_serialises_in_canonical_order_absent_stays_absent() -> None:
    # Out-of-order + one UNLISTED door; absent doors must have NO row.
    m = RunManifest(
        run_id=_RUN_ID,
        per_door_kills={"dsr": 2, "min_hits": 4, "zzz_future": 1, "cache_skip": 3},
    )
    rows = m.to_payload()["per_door_kills"]
    doors = [r["door"] for r in rows]
    # canonical doors first in CANONICAL_DOOR_ORDER, then unlisted name-sorted.
    assert doors == ["min_hits", "cache_skip", "dsr", "zzz_future"]
    # ABSENT-key law: no_bars / spa etc. were never observed → NOT present.
    assert "no_bars" not in doors
    assert all(door in CANONICAL_DOOR_ORDER[:10] or door == "zzz_future" for door in doors)


def test_per_door_kills_empty_serialises_empty_list() -> None:
    assert RunManifest(run_id=_RUN_ID).to_payload()["per_door_kills"] == []


# ─────────────────────────── D-7 dedup scalar + detail ───────────────────────────


def test_dedup_skips_scalar_and_detail_filled() -> None:
    payload = RunManifest(
        run_id=_RUN_ID, dedup_skips=9, dedup_skips_detail={"cache": 9}
    ).to_payload()
    assert payload["dedup_skips"] == 9  # scalar
    assert payload["dedup_skips_detail"] == {"cache": 9, "resume": 0, "sentinel": 0}


# ─────────────────────────── validation (every raise branch) ───────────────────────────


def test_bad_schema_version_raises() -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, schema_version=2)


@pytest.mark.parametrize("bad", ["", 123])
def test_bad_run_id_raises(bad: Any) -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=bad)


@pytest.mark.parametrize("bad", [True, "x", 1.5])
def test_dedup_skips_non_int_raises(bad: Any) -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, dedup_skips=bad)


def test_dedup_skips_negative_raises() -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, dedup_skips=-1)


@pytest.mark.parametrize("killed", [True, "x", -1, 1.0])
def test_per_door_kills_bad_value_raises(killed: Any) -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, per_door_kills={"min_hits": killed})


@pytest.mark.parametrize("seconds", [True, "x", float("inf"), float("nan"), -0.5])
def test_per_stage_timings_bad_raises(seconds: Any) -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, per_stage_timings={"s": seconds})


def test_dedup_detail_unknown_key_raises() -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, dedup_skips_detail={"bogus": 1})


@pytest.mark.parametrize("value", [True, "x", -1])
def test_dedup_detail_bad_value_raises(value: Any) -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, dedup_skips_detail={"cache": value})


@pytest.mark.parametrize("attempt", [True, 0, "x"])
def test_bad_attempt_raises(attempt: Any) -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, attempt=attempt)


@pytest.mark.parametrize("exit_code", [True, "x", 1.5])
def test_bad_exit_code_raises(exit_code: Any) -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, exit_code=exit_code)


def test_exit_code_none_ok() -> None:
    assert RunManifest(run_id=_RUN_ID, exit_code=None).exit_code is None


def test_tripwire_not_mapping_raises() -> None:
    with pytest.raises(RunManifestError):
        RunManifest(run_id=_RUN_ID, tripwires=("not-a-mapping",))  # type: ignore[arg-type]


# ─────────────────────────── from_payload edges ───────────────────────────


def test_from_payload_non_mapping_raises() -> None:
    with pytest.raises(RunManifestError):
        RunManifest.from_payload([1, 2, 3])  # type: ignore[arg-type]


def test_from_payload_per_door_kills_not_list_raises() -> None:
    with pytest.raises(RunManifestError):
        RunManifest.from_payload({"run_id": _RUN_ID, "per_door_kills": {"a": 1}})


def test_from_payload_per_door_kills_bad_row_raises() -> None:
    with pytest.raises(RunManifestError):
        RunManifest.from_payload({"run_id": _RUN_ID, "per_door_kills": [{"door": "x"}]})


def test_from_payload_defaults_missing_fields() -> None:
    m = RunManifest.from_payload({"run_id": _RUN_ID})
    assert m.dedup_skips == 0
    assert m.per_door_kills == {}
    assert m.estimate is None
    assert m.updated_utc  # a fresh timestamp is minted when absent


def test_funnel_rows_shape() -> None:
    assert funnel_rows([("a", 1), ("b", 2)]) == [
        {"stage": "a", "count": 1},
        {"stage": "b", "count": 2},
    ]


# ─────────────────────────── write / load ───────────────────────────


def test_write_and_load_round_trip(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    m = RunManifest(run_id=_RUN_ID, dedup_skips=3, per_door_kills={"dsr": 1})
    path_str = m.write(layout)
    assert Path(path_str) == layout.manifest_path
    loaded = RunManifest.load(layout)
    assert loaded is not None
    assert loaded.dedup_skips == 3
    assert loaded.per_door_kills == {"dsr": 1}


def test_load_none_when_missing(tmp_path: Path) -> None:
    assert RunManifest.load(_layout(tmp_path)) is None


def test_load_none_when_stamp_mismatch(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    RunManifest(run_id=_RUN_ID).write(layout)
    # Mutate the artifact so its stamp no longer verifies.
    layout.manifest_path.write_text('{"tampered":true}', encoding="utf-8")
    assert RunManifest.load(layout) is None


def _write_raw_with_matching_stamp(layout: RunLayout, body: bytes) -> None:
    """Write *body* to manifest.json + a VALID stamp (matching sha256) beside it.

    Lets a test drive :meth:`RunManifest.load` PAST ``verify_stamp`` with a body
    the manifest loader still rejects (bad JSON / law violation) — the only way
    to exercise load's inner ``except`` arcs.
    """
    layout.ensure_dir()
    layout.manifest_path.write_bytes(body)
    stamp = {
        "stage": "manifest",
        "run_id": layout.run_id,
        "content_hash": hashlib.sha256(body).hexdigest(),
        "produced_at_utc": "2026-07-08T00:00:00.000000Z",
        "schema_version": 1,
        "extra": {},
    }
    RunLayout.stamp_path(layout.manifest_path).write_text(json.dumps(stamp), encoding="utf-8")


def test_load_none_on_bad_json_with_valid_stamp(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_raw_with_matching_stamp(layout, b"{not valid json")
    assert RunManifest.load(layout) is None


def test_load_none_on_law_violation_with_valid_stamp(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    # Valid JSON, valid stamp, but a NEGATIVE dedup_skips → from_payload raises.
    body = json.dumps({"run_id": _RUN_ID, "dedup_skips": -5}).encode("utf-8")
    _write_raw_with_matching_stamp(layout, body)
    assert RunManifest.load(layout) is None
