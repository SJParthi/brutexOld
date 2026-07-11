"""Cascade-wire G7 replay-guard coverage (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6).

Stem-matching supplement so the touched-module discovery gate proves
``brutex.engine.cascade_wire`` at 100% over its OWN ``test_engine_cascade_
wire*`` suite: the WP-5 G7 additions — the replay's stamp-validate-before-
consume ladder (unstamped WARN / torn refusal / changed-lake refusal) and the
two fail-soft stamping seams — were previously exercised only by the run-core
suite (a foreign stem the gate cannot map).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import brutex.engine.cascade_wire as cw
import pytest
from structlog.testing import capture_logs

from tests.unit.test_engine_cascade_wire import _bars_for, _session_bars, _spec

pytest.importorskip("polars")

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g7_replay_over_valid_stamp_is_byte_identical": ("G7", "R42"),
    "test_g7_unstamped_manifest_replays_with_one_warn": ("G7",),
    "test_g7_torn_manifest_stamp_refuses_the_replay": ("G7", "R42"),
    "test_g7_changed_lake_refuses_the_replay_naming_both_hashes": ("G7",),
    "test_g7_auto_path_stamp_failure_is_fail_soft": ("G7",),
    "test_g7_replay_restamp_failure_is_fail_soft": ("G7",),
}


def _auto_run(tmp_path: Path) -> tuple[Any, Any]:
    """One stamped AUTOMATIC cascade run over the sibling harness's fixture."""
    bars_for = _bars_for(_session_bars())
    report = cw.run_and_write_from_specs(
        tmp_path,
        sweep_id="a" * 64,
        specs=[_spec()],
        bars_for=bars_for,
        bars_context=cw.BarsContext(
            exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
        ),
        cascade_config=cw.cascade_config_for_sweep("a" * 64),
    )
    return bars_for, report


def test_g7_replay_over_valid_stamp_is_byte_identical(tmp_path: Path) -> None:
    bars_for, _report = _auto_run(tmp_path)
    before = cw.report_path(tmp_path).read_bytes()
    replayed = cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    assert cw.report_path(tmp_path).read_bytes() == before  # the golden contract
    assert replayed.n_input >= 1


def test_g7_unstamped_manifest_replays_with_one_warn(tmp_path: Path) -> None:
    """A pre-G7 (unstamped) manifest replays on the honest back-compat path."""
    bars_for, _report = _auto_run(tmp_path)
    stamp_sidecar = Path(str(cw.manifest_path(tmp_path)) + ".stamp.json")
    stamp_sidecar.unlink()
    with capture_logs() as logs:
        replayed = cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    assert replayed.n_input >= 1
    warns = [e for e in logs if e["event"] == "cascade_wire.replay_unstamped"]
    assert len(warns) == 1


def test_g7_torn_manifest_stamp_refuses_the_replay(tmp_path: Path) -> None:
    """A torn (content-drifted) manifest REFUSES — earlier artifacts untouched."""
    bars_for, _report = _auto_run(tmp_path)
    manifest = cw.manifest_path(tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b"\n")  # valid JSON, drifted hash
    report_before = cw.report_path(tmp_path).read_bytes()
    with pytest.raises(cw.CascadeWireError, match="refusing the replay"):
        cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    assert cw.report_path(tmp_path).read_bytes() == report_before  # R42 one-line law


def test_g7_changed_lake_refuses_the_replay_naming_both_hashes(tmp_path: Path) -> None:
    """A silently-changed lake can never re-mint a wrong-provenance report."""
    _auto_run(tmp_path)
    session = _session_bars()
    drifted = session[:-1]  # one bar vanished from the lake
    with pytest.raises(cw.CascadeWireError, match="data_snapshot_hash"):
        cw.run_and_write_from_manifest(tmp_path, bars_for=_bars_for(drifted))


def test_g7_auto_path_stamp_failure_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead disk at the AUTOMATIC path's stamp site never loses the run."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("stamp disk broke")

    monkeypatch.setattr(cw, "stamp_cascade_artifacts", _boom)
    with capture_logs() as logs:
        _bars, report = _auto_run(tmp_path)
    assert report.n_input >= 1  # the cascade run itself completed
    warns = [e for e in logs if e["event"] == "cascade_wire.stamp_failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "OSError"


def test_g7_replay_restamp_failure_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay's RE-stamp of the rewritten report is fail-soft too."""
    bars_for, _report = _auto_run(tmp_path)
    real = cw.stamp_cascade_artifacts
    calls = {"n": 0}

    def _boom_on_restamp(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        raise OSError("re-stamp disk broke")

    monkeypatch.setattr(cw, "stamp_cascade_artifacts", _boom_on_restamp)
    with capture_logs() as logs:
        replayed = cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    monkeypatch.setattr(cw, "stamp_cascade_artifacts", real)
    assert replayed.n_input >= 1  # the replay completed despite the stamp fault
    assert calls["n"] == 1
    warns = [e for e in logs if e["event"] == "cascade_wire.stamp_failed"]
    assert len(warns) == 1
