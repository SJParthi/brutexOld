# pyright: reportPrivateUsage=none
"""Tests for `brutex.nightly.sweep_binding` (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The kwargs golden for a representative Namespace, the signature-drift pin
against ``inspect.signature(cli.cmd_sweep_all)``, the structural absence of
the UNATTENDED_ESCAPE_FLAGS (argv fuzz), the date defaults, the audit-tail
counts reader (happy / torn / missing → None — never a ``-1`` sentinel; the
round-3 freshness gate: a PRIOR run's completion in the append-across-runs
chain is never reported as tonight's), the exit→LinkResult mapping, and both
arms of the P2/P3 contract-artifact probe.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
from pathlib import Path
from typing import Any

from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_supervisor import SUPERVISOR_ACTION_COMPLETE
from brutex.nightly.chain import EXIT_SWEEP_TUPLE_FAILED, LinkStatus
from brutex.nightly.sweep_binding import (
    SWEEP_FORWARDED_KEYS,
    SWEEP_SUPERVISOR_AUDIT_RELPATH,
    UNATTENDED_ESCAPE_FLAGS,
    nightly_sweep_invocations,
    nightly_sweep_kwargs,
    probe_contract_artifacts,
    read_sweep_tail_counts,
    sweep_link_result,
    sweep_supervisor_audit_path,
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


def _write_audit(path: Path, *, payload: dict[str, Any], ts_utc: float | None = None) -> None:
    """Write the completion the way PRODUCTION does — the REAL engine action
    constant (value ``"SUPERVISOR_COMPLETE"``), never the alias NAME literal
    the review-round-1 reader drifted to. *ts_utc* overrides the entry clock
    (default: the writer's real ``time.time()`` stamp — production shape)."""
    with SweepAuditWriter.open(path, actor="sweep") as audit:
        audit.append(action="SWEEP_START", subject="sweep:x", payload={"total": 2})
        audit.append(
            action=SUPERVISOR_ACTION_COMPLETE, subject="sweep:x", payload=payload, ts_utc=ts_utc
        )


def test_supervisor_audit_path_matches_cli_wiring() -> None:
    """The counts live in the SUPERVISOR chain (``SweepGuards.audit``), NOT the
    ingest ``sweep-all.jsonl`` — pin the helper against cli.py's literal wiring
    so the path can never silently drift again (review round 1, finding 2)."""
    root = Path("/some/results")
    assert sweep_supervisor_audit_path(root) == root / "audit" / "sweep-all-supervisor.jsonl"
    assert SWEEP_SUPERVISOR_AUDIT_RELPATH == ("audit", "sweep-all-supervisor.jsonl")
    from brutex import cli

    assert 'out_root / "audit" / "sweep-all-supervisor.jsonl"' in inspect.getsource(cli)


def test_reader_keys_on_the_real_supervisor_action_value(tmp_path: Path) -> None:
    """The completion action VALUE is ``"SUPERVISOR_COMPLETE"``; an entry whose
    action is the alias NAME ``"SUPERVISOR_ACTION_COMPLETE"`` (the exact
    review-round-1 drift) is NOT a completion — honest None."""
    assert SUPERVISOR_ACTION_COMPLETE == "SUPERVISOR_COMPLETE"
    path = tmp_path / "sweep-all-supervisor.jsonl"
    path.write_text(
        json.dumps({"action": "SUPERVISOR_ACTION_COMPLETE", "payload": {"tuples_total": 5}}) + "\n",
        encoding="utf-8",
    )
    assert read_sweep_tail_counts(path, since_utc=0.0) is None


def test_tail_counts_happy_path(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit" / "sweep-all-supervisor.jsonl"
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
    counts = read_sweep_tail_counts(audit_path, since_utc=0.0)
    assert counts == {
        "tuples_total": 6,
        "tuples_run": 4,
        "skipped_cache": 1,
        "skipped_resume": 1,
        "tuples_failed": 0,
    }


def test_tail_counts_last_completion_wins(tmp_path: Path) -> None:
    audit_path = tmp_path / "sweep-all-supervisor.jsonl"
    with SweepAuditWriter.open(audit_path, actor="sweep") as audit:
        audit.append(action=SUPERVISOR_ACTION_COMPLETE, subject="s", payload={"tuples_total": 1})
        audit.append(action=SUPERVISOR_ACTION_COMPLETE, subject="s", payload={"tuples_total": 9})
    assert read_sweep_tail_counts(audit_path, since_utc=0.0) == {"tuples_total": 9}


def test_tail_counts_torn_tail_scans_upward(tmp_path: Path) -> None:
    audit_path = tmp_path / "sweep-all-supervisor.jsonl"
    _write_audit(audit_path, payload={"tuples_total": 3})
    with open(audit_path, "a", encoding="utf-8") as fp:
        fp.write('{"action": "SUPERVISOR_COMPL')  # torn mid-write
    assert read_sweep_tail_counts(audit_path, since_utc=0.0) == {"tuples_total": 3}


def test_tail_counts_honest_none_arms(tmp_path: Path) -> None:
    # Missing file.
    assert read_sweep_tail_counts(tmp_path / "absent.jsonl", since_utc=0.0) is None
    # No completion entry.
    only_start = tmp_path / "start-only.jsonl"
    with SweepAuditWriter.open(only_start, actor="sweep") as audit:
        audit.append(action="SWEEP_START", subject="s", payload={})
    assert read_sweep_tail_counts(only_start, since_utc=0.0) is None
    # Blank lines + non-dict JSON lines are skipped, not crashes.
    weird = tmp_path / "weird.jsonl"
    weird.write_text('\n[1, 2]\n{"action": "OTHER"}\n\n', encoding="utf-8")
    assert read_sweep_tail_counts(weird, since_utc=0.0) is None
    # A FRESH completion entry whose payload is not a mapping → honest None.
    bad_payload = tmp_path / "bad-payload.jsonl"
    bad_payload.write_text(
        json.dumps({"action": SUPERVISOR_ACTION_COMPLETE, "ts_utc": 1.0, "payload": [1]}) + "\n",
        encoding="utf-8",
    )
    assert read_sweep_tail_counts(bad_payload, since_utc=0.0) is None


# ── read_sweep_tail_counts — the round-3 freshness gate ─────────────────
#
# The supervisor chain is opened resume/APPEND across sweep runs (cli.py), so
# it accumulates SUPERVISOR_COMPLETE entries from prior nights. A run that
# writes no completion of its own (preflight refusal exit 1 — returned BEFORE
# the supervisor audit is even opened; SUPERVISOR_ABORT exit 2; a crash) must
# NOT be served the PRIOR run's tally as tonight's (review round 3, findings
# 1+2 — Golden Rule #1).


def test_tail_counts_stale_prior_run_completion_is_none(tmp_path: Path) -> None:
    """A completion stamped BEFORE this run's pre-sweep `since` is a PRIOR
    run's tally — honest None, never fabricated as tonight's counts."""
    audit_path = tmp_path / "sweep-all-supervisor.jsonl"
    _write_audit(audit_path, payload={"tuples_total": 100, "tuples_run": 100}, ts_utc=100.0)
    assert read_sweep_tail_counts(audit_path, since_utc=200.0) is None


def test_tail_counts_fresh_completion_at_boundary_is_accepted(tmp_path: Path) -> None:
    """`ts_utc == since_utc` is fresh (>=): a completion written within the
    same clock tick as the pre-sweep capture is this run's."""
    audit_path = tmp_path / "sweep-all-supervisor.jsonl"
    _write_audit(audit_path, payload={"tuples_total": 3}, ts_utc=200.0)
    assert read_sweep_tail_counts(audit_path, since_utc=200.0) == {"tuples_total": 3}


def test_tail_counts_stale_then_fresh_completion_reads_the_fresh_one(tmp_path: Path) -> None:
    """The append-across-runs shape: last night's completion + tonight's —
    the reversed scan finds tonight's first and the gate accepts it."""
    audit_path = tmp_path / "sweep-all-supervisor.jsonl"
    with SweepAuditWriter.open(audit_path, actor="sweep") as audit:
        audit.append(
            action=SUPERVISOR_ACTION_COMPLETE,
            subject="s",
            payload={"tuples_total": 100},
            ts_utc=100.0,
        )
        audit.append(
            action=SUPERVISOR_ACTION_COMPLETE,
            subject="s",
            payload={"tuples_total": 7},
            ts_utc=300.0,
        )
    assert read_sweep_tail_counts(audit_path, since_utc=200.0) == {"tuples_total": 7}


def test_tail_counts_timestampless_or_junk_ts_completion_is_none(tmp_path: Path) -> None:
    """Freshness must be VERIFIABLE: a completion with a missing or non-numeric
    ``ts_utc`` (a bool is not a timestamp) is an honest None — the reader never
    guesses a tally is tonight's."""
    missing_ts = tmp_path / "missing-ts.jsonl"
    missing_ts.write_text(
        json.dumps({"action": SUPERVISOR_ACTION_COMPLETE, "payload": {"tuples_total": 5}}) + "\n",
        encoding="utf-8",
    )
    assert read_sweep_tail_counts(missing_ts, since_utc=0.0) is None
    junk_ts = tmp_path / "junk-ts.jsonl"
    junk_ts.write_text(
        json.dumps(
            {"action": SUPERVISOR_ACTION_COMPLETE, "ts_utc": "yesterday", "payload": {"n": 1}}
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_sweep_tail_counts(junk_ts, since_utc=0.0) is None
    bool_ts = tmp_path / "bool-ts.jsonl"
    bool_ts.write_text(
        json.dumps({"action": SUPERVISOR_ACTION_COMPLETE, "ts_utc": True, "payload": {"n": 1}})
        + "\n",
        encoding="utf-8",
    )
    assert read_sweep_tail_counts(bool_ts, since_utc=0.0) is None


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


def test_probe_sweep_runs_in_tree_root_wins(tmp_path: Path) -> None:
    """Ruling 4: `sweep_runs/` supersedes the legacy `runs/` sketch — an
    in-tree `<results_root>/sweep_runs/<id>/` run dir is probed."""
    results = tmp_path / "sweeps"
    run = results / "sweep_runs" / "grid-1"
    run.mkdir(parents=True)
    (run / "top100.json").write_text("{}", encoding="utf-8")
    os.utime(run, (5_000.0, 5_000.0))
    probe = probe_contract_artifacts(results, since_utc=1_000.0)
    assert probe.found == ("top100",)
    assert probe.paths == {"top100": str(run / "top100.json")}


def test_probe_sweep_runs_sibling_root_probed(tmp_path: Path) -> None:
    """The SHIPPED P3 `trade_store.sweep_run_dir` law places run artifacts at
    `<results_root parent>/sweep_runs/` — probed too, newest-fresh-dir-wins
    ACROSS roots (a fresher sibling run beats an older legacy `runs/` one)."""
    results = tmp_path / "sweeps"
    legacy = results / "runs" / "run-legacy"
    legacy.mkdir(parents=True)
    (legacy / "manifest.json").write_text("{}", encoding="utf-8")
    sibling_run = tmp_path / "sweep_runs" / "grid-2"
    sibling_run.mkdir(parents=True)
    (sibling_run / "manifest.json").write_text("{}", encoding="utf-8")
    (sibling_run / "top100.json").write_text("{}", encoding="utf-8")
    os.utime(legacy, (2_000.0, 2_000.0))
    os.utime(sibling_run, (3_000.0, 3_000.0))
    probe = probe_contract_artifacts(results, since_utc=1_000.0)
    assert probe.found == ("run_manifest", "top100")
    assert probe.paths["top100"] == str(sibling_run / "top100.json")


# ── DEC-STAGE3-SLTP-INSTRUMENT-SCALE-001 (LANE W3): the default-OFF coarse
# instrument-scaled SL/TP axis PLAN face (nightly_sweep_invocations) —
# refuter round 2's pairing-preservation respecification. ─────────────────────


def test_sltp_scale_default_none_is_one_byte_identical_invocation(tmp_path: Path) -> None:
    args = _namespace(sl="80", tp="50")
    base = nightly_sweep_kwargs(
        args, lake_root=tmp_path, start_default="2020-01-01", end_default="2026-07-17"
    )
    plan = nightly_sweep_invocations(
        args,
        lake_root=tmp_path,
        start_default="2020-01-01",
        end_default="2026-07-17",
        sltp_scale_cells=None,
    )
    assert plan == (base,)
    assert plan[0]["sl"] == "80"
    assert plan[0]["tp"] == "50"


def test_sltp_scale_armed_one_invocation_per_cell_single_value_csvs(tmp_path: Path) -> None:
    from brutex.engine.stage3_scale import coarse_sltp_cells

    args = _namespace(sl="80", tp="50")
    base = nightly_sweep_kwargs(
        args, lake_root=tmp_path, start_default="2020-01-01", end_default="2026-07-17"
    )
    cells = coarse_sltp_cells(15_000)
    plan = nightly_sweep_invocations(
        args,
        lake_root=tmp_path,
        start_default="2020-01-01",
        end_default="2026-07-17",
        sltp_scale_cells=cells,
    )
    # ONE invocation per cell — never a flattened multi-value CSV pair
    # (refuter round 2: the flatten cross-multiplied 5 paired cells into 25).
    assert len(plan) == len(cells) == 5
    for invocation, (sl, tp) in zip(plan, cells, strict=True):
        assert invocation["sl"] == str(sl)  # single-value CSV
        assert invocation["tp"] == str(tp)
        assert "," not in invocation["sl"] and "," not in invocation["tp"]
        for key in SWEEP_FORWARDED_KEYS:
            if key not in ("sl", "tp"):
                assert invocation[key] == base[key]
        assert set(invocation) == set(base)
    # The invocations are independent dicts (a later mutation cannot alias).
    assert len({id(invocation) for invocation in plan}) == len(plan)


def test_sltp_scale_junk_plan_fails_loud(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        nightly_sweep_invocations(
            _namespace(sl="80", tp="50"),
            lake_root=tmp_path,
            start_default="2020-01-01",
            end_default="2026-07-17",
            sltp_scale_cells=(),
        )


def test_armed_plan_preserves_pairing_through_real_grid_enumeration(tmp_path: Path) -> None:
    """The refuter-round-2 MAJOR's downstream-semantics pin (the M-c killer).

    Drives the armed plan through the REAL consumption chain — the CLI's
    ``_parse_paisa_list`` CSV vocabulary into a REAL ``SweepGridConfig`` and
    the REAL ``iter_sweep_tuples`` enumeration (``itertools.product`` over
    ``stop_losses`` × ``take_profits``) — and pins the EXACT resulting cell
    census: each invocation enumerates EXACTLY its one (SL, TP) cell, the
    whole plan enumerates EXACTLY the 5 paired cells (never the 25-cell
    cross product), and every enumerated tuple keeps ``tp == 2·sl`` (zero
    inverted ``tp < sl`` cells). Round 1 shipped a conduit this test refutes:
    one invocation carrying 5-value SL/TP CSVs enumerated 25 cells, 6 of
    them inverted.
    """
    from brutex.cli import _parse_paisa_list
    from brutex.engine.stage3_scale import coarse_sltp_cells
    from brutex.engine.sweep_orchestrator import (
        SweepGridConfig,
        Timeframe,
        iter_sweep_tuples,
    )
    from brutex.strategies.types import Side

    cells = coarse_sltp_cells(15_000)
    assert len(cells) == 5  # the NIFTY-class 5-rung plan (anti-vacuous)
    plan = nightly_sweep_invocations(
        _namespace(sl="80", tp="50"),
        lake_root=tmp_path,
        start_default="2020-01-01",
        end_default="2026-07-17",
        sltp_scale_cells=cells,
    )
    enumerated: list[tuple[int, int]] = []
    for invocation, cell in zip(plan, cells, strict=True):
        stop_losses = _parse_paisa_list(invocation["sl"])
        take_profits = _parse_paisa_list(invocation["tp"])
        grid = SweepGridConfig(
            instruments=["NIFTY"],
            timeframes=[Timeframe("1minute", 1)],
            directions=[Side.BUY],
            k_values=[1],
            condition_bits=[0],
            stop_losses=stop_losses,
            take_profits=take_profits,
            proximities=[None],
            mode="A",
            data_snapshot_hash="snap",
            indicator_version="iv",
            code_commit_sha="sha",
        )
        tuples = list(iter_sweep_tuples(grid))
        assert len(tuples) == 1
        for t in tuples:
            assert t.sl_paisa is not None and t.tp_paisa is not None  # narrow int | None
            enumerated.append((t.sl_paisa, t.tp_paisa))
        # EXACTLY one (SL, TP) cell per invocation — the 1×1 product.
        assert {(t.sl_paisa, t.tp_paisa) for t in tuples} == {cell}
    # The whole plan: exactly the 5 PAIRED cells — never 25, never inverted.
    assert enumerated == list(cells)
    assert all(tp == 2 * sl for sl, tp in enumerated)
    assert all(tp > sl for sl, tp in enumerated)  # zero inverted cells
