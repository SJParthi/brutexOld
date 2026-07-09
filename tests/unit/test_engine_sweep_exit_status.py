"""Unit suite for :mod:`brutex.engine.sweep_exit_status` (G5 — DEC-SWEEP-UNKILLABLE-CORE-001).

Pins the exit-status state machine (running/complete/failed/refused/halted/
suspect_complete + exit_code + refusal text), the atomic write, the
corrupt-read ⇒ ``None`` honesty, the fail-soft ``finally`` twin, and the
``enrich_progress_payload`` ADDITIVE-merge law (every pre-existing progress
key byte-unchanged; the dashboard route needs zero edits).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.engine import sweep_exit_status
from brutex.engine.sweep_exit_status import (
    EXIT_STATUS_FILENAME,
    RunState,
    enrich_progress_payload,
    exit_status_path,
    read_exit_status,
    write_exit_status,
    write_exit_status_soft,
)

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g5_state_machine_wire_values_pinned": ("G5",),
    "test_g5_running_schema_exact": ("G5",),
    "test_g5_terminal_preserves_wall_started": ("G5",),
    "test_g5_terminal_without_prior_stamps_now": ("G5",),
    "test_g5_terminal_with_corrupt_prior_started_stamps_now": ("G5",),
    "test_g5_terminal_with_nonfinite_prior_started_stamps_now": ("G5",),
    "test_g5_suspect_complete_forces_suspect_true": ("G5",),
    "test_g5_suspect_explicit_wins_and_none_preserves": ("G5",),
    "test_g5_refusal_text_carried_exact": ("G5",),
    "test_g5_failed_carries_exit_code_and_error_type": ("G5",),
    "test_g5_validation_fail_loud_matrix": ("G5",),
    "test_g5_atomic_write_leaves_no_temp": ("G5",),
    "test_g5_read_missing_is_none": ("G5",),
    "test_g5_read_corrupt_is_none": ("G5",),
    "test_g5_read_non_object_is_none": ("G5",),
    "test_g5_soft_twin_oserror_false_success_true": ("G5",),
    "test_g5_soft_twin_validation_still_raises": ("G5",),
    "test_g5_enrich_additive_merge_preexisting_bytes_unchanged": ("G5",),
    "test_g5_enrich_no_run_id_serves_honest_null": ("G5",),
    "test_g5_enrich_suspect_folded_but_never_overwritten": ("G5",),
    "test_g5_enrich_never_mutates_input": ("G5",),
}

_RUN_ID: Final[str] = "b" * 64


def _write(
    results_root: Path,
    state: RunState | str,
    *,
    now: float,
    exit_code: int | None = None,
    refusal_text: str | None = None,
    error_type: str | None = None,
    suspect: bool | None = None,
) -> None:
    write_exit_status(
        results_root,
        run_id=_RUN_ID,
        state=state,
        exit_code=exit_code,
        refusal_text=refusal_text,
        error_type=error_type,
        attempt=1,
        pid=4321,
        suspect=suspect,
        clock=lambda: now,
    )


def test_g5_state_machine_wire_values_pinned() -> None:
    """The six wire states are frozen (dashboard consumers key on them)."""
    assert [s.value for s in RunState] == [
        "running",
        "complete",
        "failed",
        "refused",
        "halted",
        "suspect_complete",
    ]


def test_g5_running_schema_exact(tmp_path: Path) -> None:
    """state=running writes the EXACT 10-key schema with wall_ended_utc=null."""
    _write(tmp_path, RunState.RUNNING, now=100.0)
    path = exit_status_path(tmp_path, _RUN_ID)
    assert path == tmp_path / "runs" / _RUN_ID / EXIT_STATUS_FILENAME
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc == {
        "run_id": _RUN_ID,
        "state": "running",
        "exit_code": None,
        "refusal_text": None,
        "error_type": None,
        "attempt": 1,
        "pid": 4321,
        "wall_started_utc": 100.0,
        "wall_ended_utc": None,
        "suspect": False,
    }


def test_g5_running_write_preserves_prior_wall_started(tmp_path: Path) -> None:
    """A mid-run RUNNING write preserves the prior launch start (round-3 fix, #1594).

    ``mark_suspect``'s never-launder belt writes ``state=RUNNING, suspect=True``
    mid-run; that write must NOT reset ``wall_started_utc`` to the suspect-latch
    instant (which would truncate a SUSPECT run's reported forensic duration).
    The enter write stamps launch; the later RUNNING write keeps it and only
    flips ``suspect``.
    """
    _write(tmp_path, RunState.RUNNING, now=100.0)
    _write(tmp_path, RunState.RUNNING, now=180.0, suspect=True)
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert doc["state"] == "running"
    assert doc["wall_started_utc"] == 100.0  # NOT reset to 180.0
    assert doc["wall_ended_utc"] is None
    assert doc["suspect"] is True
    # A terminal write after the suspect latch still reports launch → duration.
    _write(tmp_path, RunState.SUSPECT_COMPLETE, now=260.0, exit_code=0)
    final = read_exit_status(tmp_path, _RUN_ID)
    assert final is not None
    assert final["wall_started_utc"] == 100.0
    assert final["wall_ended_utc"] == 260.0


def test_g5_terminal_preserves_wall_started(tmp_path: Path) -> None:
    """A terminal write preserves the prior artifact's wall_started_utc."""
    _write(tmp_path, RunState.RUNNING, now=100.0)
    _write(tmp_path, RunState.COMPLETE, now=250.0, exit_code=0)
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert doc["state"] == "complete"
    assert doc["exit_code"] == 0
    assert doc["wall_started_utc"] == 100.0
    assert doc["wall_ended_utc"] == 250.0


def test_g5_terminal_without_prior_stamps_now(tmp_path: Path) -> None:
    """A terminal write with no prior artifact stamps started == ended == now."""
    _write(tmp_path, RunState.HALTED, now=77.0, error_type="LakeWriteHalt")
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert doc["state"] == "halted"
    assert doc["error_type"] == "LakeWriteHalt"
    assert doc["wall_started_utc"] == 77.0
    assert doc["wall_ended_utc"] == 77.0


def test_g5_terminal_with_corrupt_prior_started_stamps_now(tmp_path: Path) -> None:
    """A prior artifact whose wall_started_utc is junk degrades to now (honest)."""
    _write(tmp_path, RunState.RUNNING, now=100.0)
    path = exit_status_path(tmp_path, _RUN_ID)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["wall_started_utc"] = "not-a-number"
    path.write_text(json.dumps(doc), encoding="utf-8")
    _write(tmp_path, RunState.COMPLETE, now=200.0, exit_code=0)
    after = read_exit_status(tmp_path, _RUN_ID)
    assert after is not None
    assert after["wall_started_utc"] == 200.0


def test_g5_suspect_complete_forces_suspect_true(tmp_path: Path) -> None:
    """SUSPECT_COMPLETE always persists suspect=true (D-12), even unset."""
    _write(tmp_path, RunState.SUSPECT_COMPLETE, now=10.0, exit_code=0)
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert doc["state"] == "suspect_complete"
    assert doc["suspect"] is True


def test_g5_suspect_explicit_wins_and_none_preserves(tmp_path: Path) -> None:
    """suspect=True latches; a later suspect=None write PRESERVES the latch."""
    _write(tmp_path, RunState.RUNNING, now=1.0, suspect=True)
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None and doc["suspect"] is True
    _write(tmp_path, RunState.COMPLETE, now=2.0, exit_code=0, suspect=None)
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert doc["suspect"] is True  # preserved across the terminal rewrite
    _write(tmp_path, RunState.COMPLETE, now=3.0, exit_code=0, suspect=False)
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert doc["suspect"] is False  # an EXPLICIT flag always wins


def test_g5_refusal_text_carried_exact(tmp_path: Path) -> None:
    """state=refused carries the loud refusal text verbatim to the page."""
    refusal = "preflight refused: NIFTY 5m derived partition is stale — pull on /universe first"
    _write(tmp_path, RunState.REFUSED, now=5.0, exit_code=2, refusal_text=refusal)
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert doc["state"] == "refused"
    assert doc["refusal_text"] == refusal
    assert doc["exit_code"] == 2


def test_g5_failed_carries_exit_code_and_error_type(tmp_path: Path) -> None:
    """state=failed surfaces the exit code + exception class (the G5 blindness fix)."""
    _write(tmp_path, RunState.FAILED, now=5.0, exit_code=3, error_type="SweepComboError")
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None
    assert doc["state"] == "failed"
    assert doc["exit_code"] == 3
    assert doc["error_type"] == "SweepComboError"


def test_g5_validation_fail_loud_matrix(tmp_path: Path) -> None:
    """Empty run_id / junk state / attempt<1 / pid<1 / bool exit_code all raise."""
    with pytest.raises(ValueError, match="run_id"):
        write_exit_status(tmp_path, run_id="", state=RunState.RUNNING, attempt=1, pid=1)
    with pytest.raises(ValueError):
        write_exit_status(tmp_path, run_id=_RUN_ID, state="exploded", attempt=1, pid=1)
    with pytest.raises(ValueError, match="attempt"):
        write_exit_status(tmp_path, run_id=_RUN_ID, state=RunState.RUNNING, attempt=0, pid=1)
    with pytest.raises(ValueError, match="pid"):
        write_exit_status(tmp_path, run_id=_RUN_ID, state=RunState.RUNNING, attempt=1, pid=0)
    with pytest.raises(ValueError, match="exit_code"):
        write_exit_status(
            tmp_path,
            run_id=_RUN_ID,
            state=RunState.COMPLETE,
            exit_code=True,
            attempt=1,
            pid=1,
        )


def test_g5_atomic_write_leaves_no_temp(tmp_path: Path) -> None:
    """The write is tmp→replace atomic: only the artifact remains in the run dir."""
    _write(tmp_path, RunState.RUNNING, now=1.0)
    entries = sorted(p.name for p in (tmp_path / "runs" / _RUN_ID).iterdir())
    assert entries == [EXIT_STATUS_FILENAME]


def test_g5_read_missing_is_none(tmp_path: Path) -> None:
    """No artifact ⇒ honest None (never a raise)."""
    assert read_exit_status(tmp_path, _RUN_ID) is None


def test_g5_read_corrupt_is_none(tmp_path: Path) -> None:
    """Garbage bytes ⇒ honest None (failure-matrix A13/F3 — never a 500)."""
    path = exit_status_path(tmp_path, _RUN_ID)
    path.parent.mkdir(parents=True)
    path.write_text("{{{torn", encoding="utf-8")
    assert read_exit_status(tmp_path, _RUN_ID) is None


def test_g5_read_non_object_is_none(tmp_path: Path) -> None:
    """A JSON body that is not an object ⇒ honest None."""
    path = exit_status_path(tmp_path, _RUN_ID)
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_exit_status(tmp_path, _RUN_ID) is None


def test_g5_soft_twin_oserror_false_success_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finally twin maps OSError → False (one WARN); a clean write → True."""
    ok = write_exit_status_soft(
        tmp_path, run_id=_RUN_ID, state=RunState.COMPLETE, exit_code=0, attempt=1, pid=1
    )
    assert ok is True
    doc = read_exit_status(tmp_path, _RUN_ID)
    assert doc is not None and doc["state"] == "complete"

    def _boom(dest: Path, write_to: object, *, chmod: object = None) -> None:
        raise OSError("dead disk")

    monkeypatch.setattr(sweep_exit_status, "atomic_write_simple", _boom)
    ok = write_exit_status_soft(
        tmp_path, run_id=_RUN_ID, state=RunState.FAILED, exit_code=1, attempt=1, pid=1
    )
    assert ok is False


def test_g5_soft_twin_validation_still_raises(tmp_path: Path) -> None:
    """A code-bug ValueError is NOT swallowed by the soft twin (fail-loud)."""
    with pytest.raises(ValueError, match="attempt"):
        write_exit_status_soft(tmp_path, run_id=_RUN_ID, state=RunState.COMPLETE, attempt=0, pid=1)


def test_g5_enrich_additive_merge_preexisting_bytes_unchanged(tmp_path: Path) -> None:
    """Enrichment ADDS exit_status + suspect; every pre-existing key is unchanged."""
    _write(tmp_path, RunState.FAILED, now=9.0, exit_code=3, error_type="RuntimeError")
    progress: dict[str, Any] = {
        "state": "error",
        "enumerated": 10,
        "evaluated": 4,
        "percent": 40.0,
        "run_id": _RUN_ID,
    }
    out = enrich_progress_payload(progress, results_root=tmp_path)
    for key, value in progress.items():
        assert out[key] == value  # pre-existing keys byte-unchanged
    assert sorted(set(out) - set(progress)) == ["exit_status", "suspect"]
    status = out["exit_status"]
    assert status is not None
    assert status["state"] == "failed"
    assert status["exit_code"] == 3
    assert status["error_type"] == "RuntimeError"
    assert out["suspect"] is False


def test_g5_enrich_no_run_id_serves_honest_null(tmp_path: Path) -> None:
    """No/blank/non-string run_id (the idle payload) ⇒ exit_status=None, suspect=False."""
    for payload in ({}, {"run_id": ""}, {"run_id": 7}):
        out = enrich_progress_payload(dict(payload), results_root=tmp_path)
        assert out["exit_status"] is None
        assert out["suspect"] is False
    # A named run whose artifact is missing is honest-null too.
    out = enrich_progress_payload({"run_id": _RUN_ID}, results_root=tmp_path)
    assert out["exit_status"] is None


def test_g5_enrich_suspect_folded_but_never_overwritten(tmp_path: Path) -> None:
    """The artifact's suspect flag is folded in ONLY when progress lacks the key."""
    _write(tmp_path, RunState.SUSPECT_COMPLETE, now=9.0, exit_code=0)
    folded = enrich_progress_payload({"run_id": _RUN_ID}, results_root=tmp_path)
    assert folded["suspect"] is True
    authoritative = enrich_progress_payload(
        {"run_id": _RUN_ID, "suspect": False}, results_root=tmp_path
    )
    assert authoritative["suspect"] is False  # the progress writer stays authoritative


def test_g5_enrich_never_mutates_input(tmp_path: Path) -> None:
    """The input payload object is never mutated (a fresh dict is returned)."""
    progress: dict[str, Any] = {"run_id": _RUN_ID, "percent": 10.0}
    snapshot = dict(progress)
    out = enrich_progress_payload(progress, results_root=tmp_path)
    assert progress == snapshot
    assert out is not progress


def test_g5_terminal_with_nonfinite_prior_started_stamps_now(tmp_path: Path) -> None:
    """A prior wall_started_utc of Infinity/NaN (a float, but junk) degrades to now."""
    _write(tmp_path, RunState.RUNNING, now=100.0)
    path = exit_status_path(tmp_path, _RUN_ID)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace("100.0", "Infinity"), encoding="utf-8")
    _write(tmp_path, RunState.COMPLETE, now=300.0, exit_code=0)
    after = read_exit_status(tmp_path, _RUN_ID)
    assert after is not None
    assert after["wall_started_utc"] == 300.0
    assert after["wall_ended_utc"] == 300.0
