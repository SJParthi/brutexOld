"""Unit tests for :mod:`brutex.engine.run_layout` — the sweep RUN-CORE layout leaf.

DEC-SWEEP-UNKILLABLE-CORE-001 (MIGRATION.md #1605). 100% line + branch: the
sibling-root path mechanics, the path-safe ``run_id`` allowlist (no ``..`` /
separator escape), the atomic JSON writer, the stamped-artifact writer + its
``<artifact>.stamp.json`` sidecar, and the stamp verify tier (every ``False``
rejection path + the ``True`` acceptance).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from brutex.engine.run_layout import (
    STAMP_SCHEMA_VERSION,
    SWEEP_RUNS_DIRNAME,
    RunLayout,
    RunLayoutError,
    run_dir,
    run_level_run_id,
    sweep_runs_root,
    validate_run_id,
)

_RUN_ID = "a" * 64  # a well-formed 64-hex grid-identity-shaped run id


# ─────────────────────────── validate_run_id ───────────────────────────


def test_validate_run_id_accepts_hex_identity() -> None:
    assert validate_run_id(_RUN_ID) == _RUN_ID


def test_validate_run_id_accepts_logical_form() -> None:
    assert validate_run_id("multi-" + "b" * 32) == "multi-" + "b" * 32


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "short",  # < 8 chars
        "a" * 129,  # > 128 chars
        "has/slash" + "x" * 4,  # path separator
        "has\\back" + "x" * 4,  # windows separator
        "..dotdot" + "x" * 4,  # leading dots (traversal shape)
        "has space" + "x",  # whitespace
        "-leadingdash",  # must start alphanumeric
    ],
)
def test_validate_run_id_rejects_unsafe(bad: str) -> None:
    with pytest.raises(RunLayoutError):
        validate_run_id(bad)


def test_run_level_run_id_passthrough_and_reject() -> None:
    assert run_level_run_id(_RUN_ID) == _RUN_ID
    with pytest.raises(RunLayoutError):
        run_level_run_id("../escape")


# ─────────────────────────── root/dir helpers ───────────────────────────


def test_sweep_runs_root_is_sibling_of_results_root() -> None:
    results_root = Path("/lake/sweeps")
    assert sweep_runs_root(results_root) == Path("/lake") / SWEEP_RUNS_DIRNAME
    # NEVER inside the .parquet-globbed sweeps tree.
    assert SWEEP_RUNS_DIRNAME not in results_root.parts


def test_run_dir_composition_and_validation() -> None:
    results_root = Path("/lake/sweeps")
    assert run_dir(results_root, _RUN_ID) == Path("/lake") / SWEEP_RUNS_DIRNAME / _RUN_ID
    with pytest.raises(RunLayoutError):
        run_dir(results_root, "..")


# ─────────────────────────── RunLayout ───────────────────────────


def _layout(tmp_path: Path) -> RunLayout:
    return RunLayout(results_root=tmp_path / "sweeps", run_id=_RUN_ID)


def test_run_layout_rejects_bad_run_id(tmp_path: Path) -> None:
    with pytest.raises(RunLayoutError):
        RunLayout(results_root=tmp_path / "sweeps", run_id="bad/id")


def test_run_layout_run_dir_and_named_paths(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert layout.run_dir == tmp_path / SWEEP_RUNS_DIRNAME / _RUN_ID
    assert layout.manifest_path.name == "manifest.json"
    assert layout.exit_status_path.name == "exit-status.json"
    assert layout.chain_map_path.name == "chain-map.json"
    assert layout.recorder_path.name == "flight-recorder.jsonl"
    assert layout.manifest_path.parent == layout.run_dir


@pytest.mark.parametrize("bad", ["", "a/b", "a\\b", ".", ".."])
def test_artifact_path_rejects_non_bare_name(tmp_path: Path, bad: str) -> None:
    with pytest.raises(RunLayoutError):
        _layout(tmp_path).artifact_path(bad)


def test_artifact_path_accepts_bare_name(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert layout.artifact_path("top100.json") == layout.run_dir / "top100.json"


def test_stamp_path_is_sidecar(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    art = layout.manifest_path
    assert RunLayout.stamp_path(art) == art.with_name("manifest.json.stamp.json")


def test_ensure_dir_creates_idempotently(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    d1 = layout.ensure_dir()
    assert d1.is_dir()
    d2 = layout.ensure_dir()  # second call is a no-op
    assert d2 == d1


def test_write_json_creates_dir_and_deterministic_bytes(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_json("selection.json", {"b": 2, "a": 1})
    assert dest.exists()
    # sorted keys + compact separators (deterministic).
    assert dest.read_text(encoding="utf-8") == '{"a":1,"b":2}'


def test_write_json_rejects_nan(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(ValueError):
        layout.write_json("bad.json", {"x": float("nan")})


def test_write_stamped_json_writes_artifact_and_stamp(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json(
        "manifest.json", {"k": "v"}, stage="manifest", extra={"data_snapshot_hash": "snap"}
    )
    stamp = json.loads(RunLayout.stamp_path(dest).read_text(encoding="utf-8"))
    assert stamp["stage"] == "manifest"
    assert stamp["run_id"] == _RUN_ID
    assert stamp["schema_version"] == STAMP_SCHEMA_VERSION
    assert stamp["extra"] == {"data_snapshot_hash": "snap"}
    assert stamp["content_hash"] == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert "produced_at_utc" in stamp


def test_write_stamped_json_extra_none_defaults_empty(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("a.json", {"k": 1}, stage="s")
    stamp = json.loads(RunLayout.stamp_path(dest).read_text(encoding="utf-8"))
    assert stamp["extra"] == {}


# ─────────────────────────── verify_stamp ───────────────────────────


def test_verify_stamp_true_for_fresh_stamped_artifact(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("m.json", {"k": 1}, stage="s")
    assert layout.verify_stamp(dest) is True


def test_verify_stamp_false_when_artifact_missing(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert layout.verify_stamp(layout.run_dir / "nope.json") is False


def test_verify_stamp_false_when_stamp_missing(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_json("m.json", {"k": 1})  # no stamp written
    assert layout.verify_stamp(dest) is False


def test_verify_stamp_false_on_bad_stamp_json(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("m.json", {"k": 1}, stage="s")
    RunLayout.stamp_path(dest).write_text("{not json", encoding="utf-8")
    assert layout.verify_stamp(dest) is False


def test_verify_stamp_false_when_stamp_read_raises_oserror(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_json("m.json", {"k": 1})
    # A DIRECTORY at the stamp path exists() but read_text() raises IsADirectoryError.
    RunLayout.stamp_path(dest).mkdir(parents=True)
    assert layout.verify_stamp(dest) is False


def test_verify_stamp_false_when_stamp_not_dict(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("m.json", {"k": 1}, stage="s")
    RunLayout.stamp_path(dest).write_text("[1,2,3]", encoding="utf-8")
    assert layout.verify_stamp(dest) is False


def test_verify_stamp_false_on_wrong_schema_version(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("m.json", {"k": 1}, stage="s")
    stamp = json.loads(RunLayout.stamp_path(dest).read_text(encoding="utf-8"))
    stamp["schema_version"] = 999
    RunLayout.stamp_path(dest).write_text(json.dumps(stamp), encoding="utf-8")
    assert layout.verify_stamp(dest) is False


def test_verify_stamp_false_on_wrong_run_id(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("m.json", {"k": 1}, stage="s")
    stamp = json.loads(RunLayout.stamp_path(dest).read_text(encoding="utf-8"))
    stamp["run_id"] = "c" * 64
    RunLayout.stamp_path(dest).write_text(json.dumps(stamp), encoding="utf-8")
    assert layout.verify_stamp(dest) is False


def test_verify_stamp_false_when_content_hash_not_str(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("m.json", {"k": 1}, stage="s")
    stamp = json.loads(RunLayout.stamp_path(dest).read_text(encoding="utf-8"))
    stamp["content_hash"] = 123
    RunLayout.stamp_path(dest).write_text(json.dumps(stamp), encoding="utf-8")
    assert layout.verify_stamp(dest) is False


def test_verify_stamp_false_on_hash_mismatch(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("m.json", {"k": 1}, stage="s")
    dest.write_text('{"k":2}', encoding="utf-8")  # artifact mutated after stamp
    assert layout.verify_stamp(dest) is False


def test_verify_stamp_false_when_artifact_read_raises_oserror(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    dest = layout.write_stamped_json("m.json", {"k": 1}, stage="s")
    # Replace the artifact FILE with a directory: exists() True, read_bytes() raises.
    dest.unlink()
    dest.mkdir()
    assert layout.verify_stamp(dest) is False
