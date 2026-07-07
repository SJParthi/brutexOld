# pyright: reportPrivateUsage=none
"""Tests for `brutex.nightly.sweep_binding` (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The kwargs golden for a representative Namespace, the signature-drift pin
against ``inspect.signature(cli.cmd_sweep_all)``, the structural absence of
the UNATTENDED_ESCAPE_FLAGS (argv fuzz), the date defaults, the audit-tail
counts reader (happy / torn / missing → None — never a ``-1`` sentinel), the
exit→LinkResult mapping, and both arms of the P2/P3 contract-artifact probe.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
from pathlib import Path
from typing import Any

from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.nightly.chain import EXIT_SWEEP_TUPLE_FAILED, LinkStatus
from brutex.nightly.sweep_binding import (
    SWEEP_FORWARDED_KEYS,
    UNATTENDED_ESCAPE_FLAGS,
    nightly_sweep_kwargs,
    probe_contract_artifacts,
    read_sweep_tail_counts,
    sweep_link_result,
)
from hypothesis import given
from hypothesis import strategies as st


def _namespace(**overrides: Any) -> argparse.Namespace:
    """A representative parsed-args Namespace carrying every grid knob."""
    base: dict[str, Any] = {
        "catalog_path": None,
        "results_root": None,
        "exchange": "NSE",
        "segment": "INDEX",
        "symbols": "NIFTY,BANKNIFTY",
        "intervals": "1minute,5minute,15minute",
        "start": None,
        "end": None,
        "directions": "BUY,SELL",
        "k": "1,2",
        "fib_anchors": "prev_day",
        "conditions": None,
        "sl": "none",
        "tp": "none",
        "top": 20,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "overfit_gate": False,
        "dsr_min": 0.95,
        "options_sim": True,
        "options_expiry": "weekly",
        "options_broker": "groww",
        "stage3_enable": False,
        "stage3_top_k": None,
        "stage3_fevals": None,
        "stage3_restarts": None,
        "lake_root": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_kwargs_golden_for_representative_namespace(tmp_path: Path) -> None:
    kwargs = nightly_sweep_kwargs(
        _namespace(top=50, mode="B"),
        lake_root=tmp_path,
        start_default="2020-01-01",
        end_default="2026-07-06",
    )
    assert kwargs == {
        "lake_root": tmp_path,
        "catalog_path": None,
        "results_root": None,
        "exchange": "NSE",
        "segment": "INDEX",
        "symbols": "NIFTY,BANKNIFTY",
        "intervals": "1minute,5minute,15minute",
        "start": "2020-01-01",
        "end": "2026-07-06",
        "directions": "BUY,SELL",
        "k": "1,2",
        "fib_anchors": "prev_day",
        "conditions": None,
        "sl": "none",
        "tp": "none",
        "top": 50,
        "mode": "B",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "overfit_gate": False,
        "dsr_min": 0.95,
        "options_sim": True,
        "options_expiry": "weekly",
        "options_broker": "groww",
        "stage3_enable": False,
        "stage3_top_k": None,
        "stage3_fevals": None,
        "stage3_restarts": None,
    }


def test_every_forwarded_key_exists_on_cmd_sweep_all() -> None:
    """The signature-drift pin: the daemon key-list law can never rot."""
    from brutex import cli

    parameters = inspect.signature(cli.cmd_sweep_all).parameters
    kwargs = nightly_sweep_kwargs(
        _namespace(), lake_root=None, start_default="2020-01-01", end_default="2026-07-06"
    )
    for key in kwargs:
        assert key in parameters, key
    # The pinned trio is STRUCTURALLY absent from every produced kwargs dict.
    for flag in UNATTENDED_ESCAPE_FLAGS:
        assert flag not in kwargs
        assert flag in parameters  # ...while remaining REAL cmd_sweep_all params
    assert set(kwargs) == set(SWEEP_FORWARDED_KEYS)


@given(
    escapes=st.dictionaries(
        st.sampled_from(list(UNATTENDED_ESCAPE_FLAGS)), st.booleans(), max_size=3
    ),
    start=st.none() | st.just("") | st.just("2024-01-01"),
    end=st.none() | st.just("") | st.just("2024-12-31"),
)
def test_escape_flags_never_forwarded_even_when_present(
    escapes: dict[str, bool], start: str | None, end: str | None
) -> None:
    """Argv fuzz: a Namespace CARRYING the escape flags contributes nothing."""
    namespace = _namespace(start=start, end=end, **escapes)
    kwargs = nightly_sweep_kwargs(
        namespace, lake_root=None, start_default="2020-01-01", end_default="2026-07-06"
    )
    assert not any(flag in kwargs for flag in UNATTENDED_ESCAPE_FLAGS)
    assert kwargs["start"] == (start if start else "2020-01-01")
    assert kwargs["end"] == (end if end else "2026-07-06")


def test_explicit_dates_survive_defaults() -> None:
    kwargs = nightly_sweep_kwargs(
        _namespace(start="2023-02-01", end="2023-03-01"),
        lake_root=None,
        start_default="2020-01-01",
        end_default="2026-07-06",
    )
    assert kwargs["start"] == "2023-02-01"
    assert kwargs["end"] == "2023-03-01"


# ── read_sweep_tail_counts ───────────────────────────────────────────────


def _write_audit(path: Path, *, payload: dict[str, Any]) -> None:
    with SweepAuditWriter.open(path, actor="sweep") as audit:
        audit.append(action="SWEEP_START", subject="sweep:x", payload={"total": 2})
        audit.append(action="SUPERVISOR_ACTION_COMPLETE", subject="sweep:x", payload=payload)


def test_tail_counts_happy_path(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit" / "sweep-all.jsonl"
    payload = {
        "tuples_total": 6,
        "tuples_run": 4,
        "skipped_cache": 1,
        "skipped_resume": 1,
        "tuples_failed": 0,
        "exit_hint": True,  # a bool is NOT a counter — filtered
        "note": "n/a",  # a string is NOT a counter — filtered
    }
    _write_audit(audit_path, payload=payload)
    counts = read_sweep_tail_counts(audit_path)
    assert counts == {
        "tuples_total": 6,
        "tuples_run": 4,
        "skipped_cache": 1,
        "skipped_resume": 1,
        "tuples_failed": 0,
    }


def test_tail_counts_last_completion_wins(tmp_path: Path) -> None:
    audit_path = tmp_path / "sweep-all.jsonl"
    with SweepAuditWriter.open(audit_path, actor="sweep") as audit:
        audit.append(action="SUPERVISOR_ACTION_COMPLETE", subject="s", payload={"tuples_total": 1})
        audit.append(action="SUPERVISOR_ACTION_COMPLETE", subject="s", payload={"tuples_total": 9})
    assert read_sweep_tail_counts(audit_path) == {"tuples_total": 9}


def test_tail_counts_torn_tail_scans_upward(tmp_path: Path) -> None:
    audit_path = tmp_path / "sweep-all.jsonl"
    _write_audit(audit_path, payload={"tuples_total": 3})
    with open(audit_path, "a", encoding="utf-8") as fp:
        fp.write('{"action": "SUPERVISOR_ACTION_COMPL')  # torn mid-write
    assert read_sweep_tail_counts(audit_path) == {"tuples_total": 3}


def test_tail_counts_honest_none_arms(tmp_path: Path) -> None:
    # Missing file.
    assert read_sweep_tail_counts(tmp_path / "absent.jsonl") is None
    # No completion entry.
    only_start = tmp_path / "start-only.jsonl"
    with SweepAuditWriter.open(only_start, actor="sweep") as audit:
        audit.append(action="SWEEP_START", subject="s", payload={})
    assert read_sweep_tail_counts(only_start) is None
    # Blank lines + non-dict JSON lines are skipped, not crashes.
    weird = tmp_path / "weird.jsonl"
    weird.write_text('\n[1, 2]\n{"action": "OTHER"}\n\n', encoding="utf-8")
    assert read_sweep_tail_counts(weird) is None
    # A completion entry whose payload is not a mapping → honest None.
    bad_payload = tmp_path / "bad-payload.jsonl"
    bad_payload.write_text(
        json.dumps({"action": "SUPERVISOR_ACTION_COMPLETE", "payload": [1]}) + "\n",
        encoding="utf-8",
    )
    assert read_sweep_tail_counts(bad_payload) is None


# ── sweep_link_result ────────────────────────────────────────────────────


def test_sweep_link_result_mapping(tmp_path: Path) -> None:
    audit = tmp_path / "sweep-all.jsonl"
    ok = sweep_link_result(0, audit_path=audit, counts={"tuples_run": 2})
    assert ok.status is LinkStatus.OK
    assert ok.counts == {"tuples_run": 2}

    refused = sweep_link_result(1, audit_path=audit, counts=None)
    assert refused.status is LinkStatus.REFUSED
    assert "sweep preflight refused (exit 1)" in refused.detail
    assert str(audit) in refused.detail
    assert refused.counts == {}  # unknown is EMPTY (rendered "?"), never -1

    failed6 = sweep_link_result(6, audit_path=audit, counts={"tuples_failed": 2})
    assert failed6.status is LinkStatus.FAILED
    assert failed6.exit_hint == EXIT_SWEEP_TUPLE_FAILED

    failed2 = sweep_link_result(2, audit_path=audit, counts=None)
    assert failed2.status is LinkStatus.FAILED
    assert failed2.exit_hint is None
    assert "exited 2" in failed2.detail


# ── probe_contract_artifacts ─────────────────────────────────────────────


def test_probe_no_runs_dir_reads_both_absent(tmp_path: Path) -> None:
    probe = probe_contract_artifacts(tmp_path, since_utc=0.0)
    assert probe.found == ()
    assert probe.absent == ("run_manifest", "top100")
    assert probe.paths == {}


def test_probe_stale_run_dir_is_ignored(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "run-old"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text("{}", encoding="utf-8")
    os.utime(run, (100.0, 100.0))
    probe = probe_contract_artifacts(tmp_path, since_utc=200.0)
    assert probe.found == ()
    assert probe.absent == ("run_manifest", "top100")


def test_probe_fresh_run_reports_presence_honestly(tmp_path: Path) -> None:
    """The documented P2 manifest shape is found; the absent top100 stays absent."""
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    manifest = {
        "run_id": "run-1",
        "selection": {},
        "estimate": {},
        "actual": {},
        "per_stage_timings": {},
        "per_door_kills": {},
        "dedup_skips": 3,
        "tripwires": [],
        "suspect": False,
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    os.utime(run, (5_000.0, 5_000.0))
    probe = probe_contract_artifacts(tmp_path, since_utc=1_000.0)
    assert probe.found == ("run_manifest",)
    assert probe.absent == ("top100",)
    assert probe.paths == {"run_manifest": str(run / "manifest.json")}


def test_probe_newest_fresh_run_wins_and_files_ignored(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    older = runs / "run-a"
    newer = runs / "run-b"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (runs / "stray-file.txt").write_text("x", encoding="utf-8")  # ignored: not a dir
    (older / "manifest.json").write_text("{}", encoding="utf-8")
    (newer / "manifest.json").write_text("{}", encoding="utf-8")
    (newer / "top100.json").write_text("[]", encoding="utf-8")
    os.utime(older, (2_000.0, 2_000.0))
    os.utime(newer, (3_000.0, 3_000.0))
    probe = probe_contract_artifacts(tmp_path, since_utc=1_000.0)
    assert probe.found == ("run_manifest", "top100")
    assert probe.absent == ()
    assert probe.paths["top100"] == str(newer / "top100.json")
