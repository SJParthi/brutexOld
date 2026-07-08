"""End-to-end CLI wiring tests for the sweep lock / heal / verify guards (FIX C).

The three touched shims are ``# pragma: no cover`` orchestration wrappers over
100%-tested cores, so these integration tests are the HONEST proof that the
wiring itself holds end to end:

* **FIX A — heal is mutually exclusive with a live sweep.** ``cmd_sweep_heal``
  holds the SAME cross-process single-flight flock ``cmd_sweep_all`` holds for
  its entire run (supervised AND parallel — the path-agnostic C6 guarantee the
  progress-file freshness alone could not give for the parallel path). We
  simulate a live sweep by holding ``sweep_singleflight_lock(out_root)`` in the
  test, then drive the real heal command against that root and assert it REFUSES
  with :data:`cli.EXIT_HEAL_REFUSED` + a remediation-hinting message.
* **DEFECT 1b — ``sweep-verify`` folds the audit-chain verdict into its exit.**
  A forked ``sweep-all.jsonl`` (the damage a missing single-flight lock
  produces) is DETECTED by ``verify_audit_chains`` and flips the CLI exit
  non-zero, proving the Tier-0 verdict is wired into the exit path (not just the
  printed report).
"""

from __future__ import annotations

from pathlib import Path

from brutex import cli
from brutex.engine.sweep_heal import HealPlan, write_heal_plan
from brutex.engine.sweep_singleflight import (
    sweep_singleflight_lock,
    sweep_singleflight_lock_path,
)
from brutex.engine.sweep_verify import RecordedContext
from brutex.orchestrator.audit import AuditWriter


def _write_empty_heal_plan(out_root: Path) -> Path:
    """Write a VALID (zero-entry) heal plan under *out_root* and return its path.

    The heal refuses at the LOCK acquire before ever assessing the plan, so an
    empty plan is enough — it only needs to satisfy ``read_heal_plan``.
    """
    plan = HealPlan(
        created_utc="20260101T000000Z",
        grid_identity="0" * 64,
        context=RecordedContext(
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        ),
        n_expected_stores=1,
        entries=(),
    )
    return write_heal_plan(out_root, plan)


def test_sweep_heal_refuses_while_a_sweep_holds_the_lock(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """FIX A: a heal REFUSES (EXIT_HEAL_REFUSED) while the sweep flock is held.

    Holding the single-flight lock in-test stands in for a LIVE sweep (of any
    path — supervised or parallel; both hold this same lock). The heal's
    non-blocking acquire raises, and the shim maps it to a clean refusal with a
    remediation hint — never a traceback, never proceeding over a live sweep.
    """
    out_root = tmp_path / "sweeps"
    out_root.mkdir()
    plan_path = _write_empty_heal_plan(out_root)
    lock_path = sweep_singleflight_lock_path(out_root)

    # A LIVE sweep holds the lock for its whole run — simulated here.
    with sweep_singleflight_lock(out_root):
        rc = cli.cmd_sweep_heal(
            plan=plan_path,
            lake_root=tmp_path / "lake",
            results_root=out_root,
            execute=True,  # even --execute must refuse while a sweep runs
            allow_mass_quarantine=False,
        )

    assert rc == cli.EXIT_HEAL_REFUSED
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "C6" in err
    # The message names the contended lock path for an actionable remediation.
    assert str(lock_path) in err
    assert "remove the stale lock" in err


def test_sweep_heal_proceeds_when_no_sweep_lock_held(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """FIX A control: with the lock FREE, the same heal reaches its dry-run path.

    Proves the refusal is specifically the held-lock contention, not an
    unconditional block: an empty plan with no live sweep dry-runs to EXIT_OK.
    """
    out_root = tmp_path / "sweeps"
    out_root.mkdir()
    plan_path = _write_empty_heal_plan(out_root)

    rc = cli.cmd_sweep_heal(
        plan=plan_path,
        lake_root=tmp_path / "lake",
        results_root=out_root,
        execute=False,
        allow_mass_quarantine=False,
    )
    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "DRY-RUN" in out


def _forked_ingest_chain(out_root: Path) -> None:
    """Write a clean ``sweep-all.jsonl`` then fork it (duplicate last line).

    Uses generic (non-``SWEEP_START``) actions so the tree carries NO recorded
    reproducibility context — ``cmd_sweep_verify`` then takes its
    structural-only early-return path, whose exit folds ``verify_audit_chains``.
    """
    audit_path = out_root / "audit" / "sweep-all.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with AuditWriter.open(audit_path) as w:
        w.append(action="STEP", subject="s0", payload={"i": 0})
        w.append(action="STEP", subject="s1", payload={"i": 1})
    # A second interleaved resume-writer re-commits the last line (duplicate seq
    # + prev_hash) — exactly the fork a missing CLI sweep single-flight produces.
    last = audit_path.read_bytes().splitlines(keepends=True)[-1]
    with audit_path.open("ab") as f:
        f.write(last)


def _verify(out_root: Path, tmp_path: Path) -> int:
    """Drive ``cli.cmd_sweep_verify`` over *out_root* (structural-only path)."""
    return cli.cmd_sweep_verify(
        lake_root=tmp_path / "lake",
        catalog_path=None,
        results_root=out_root,
        exchange="NSE",
        segment="INDEX",
        symbols="NIFTY",
        intervals="1minute",
        start="2024-01-02",
        end="2024-01-03",
        directions="BUY",
        k="2",
        conditions="0,1,2,3",
        sl="none",
        tp="none",
        top=10,
        mode="A",
        max_tuples=None,
        max_combos=None,
        data_snapshot_hash=None,
        indicator_version=None,
        code_commit_sha=None,
        recount=False,
        scratch_root=None,
        allow_context_mismatch=False,
    )


def test_sweep_verify_exits_nonzero_on_forked_audit_chain(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """DEFECT 1b: a forked audit chain flips the CLI verify exit non-zero.

    Proves ``verify_audit_chains`` is actually FOLDED into the exit path — a
    tree with a clean structural verdict but a forked ``sweep-all.jsonl`` must
    NOT exit 0; the corruption is reported in the rendered audit-chain verdict.
    """
    out_root = tmp_path / "sweeps"
    out_root.mkdir()
    _forked_ingest_chain(out_root)

    rc = _verify(out_root, tmp_path)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_VERIFY_FINDINGS
    assert rc != cli.EXIT_OK
    assert "CORRUPT" in out
    assert "sweep-all.jsonl" in out


def test_sweep_verify_clean_chain_exits_ok(
    tmp_path: Path,
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """DEFECT 1b control: a clean (unforked) chain on an empty tree exits 0.

    Proves the non-zero above is specifically the fork, not the structural-only
    early return itself.
    """
    out_root = tmp_path / "sweeps"
    out_root.mkdir()
    audit_path = out_root / "audit" / "sweep-all.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with AuditWriter.open(audit_path) as w:
        w.append(action="STEP", subject="s0", payload={"i": 0})
        w.append(action="STEP", subject="s1", payload={"i": 1})

    rc = _verify(out_root, tmp_path)
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "CLEAN" in out
