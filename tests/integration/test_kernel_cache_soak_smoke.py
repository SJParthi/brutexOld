"""Sandbox smoke of the kernel-cache soak harness — the end-to-end MECHANICS proof.

DEC-SWEEP-WORKER-KERNEL-CACHE-SOAK-001. Drives
``scripts/ci/kernel_cache_soak.py run --smoke`` as a REAL subprocess: the
script builds a tiny real-writer NIFTY 1m fixture lake, runs BOTH
``brutex sweep-all`` arms (OFF then ON, workers=2, fresh per-arm results
roots), samples RSS, harvests the #1729 telemetry, byte-compares the trees
ts-masked, and writes soak-report.json + summary.md. This proves the script
MECHANICS end-to-end; the numbers are NEVER decision-grade (the report
stamps ``smoke: true`` and the verdict can never read FLIP-READY —
asserted below, Golden Rule #10).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "kernel_cache_soak.py"


@pytest.fixture(scope="module")
def smoke_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """ONE shared end-to-end ``run --smoke`` (two real sweep subprocesses).

    An ambient ``BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY`` is planted so the run
    proves END-TO-END that the harness POPS it from both arm envs and echoes
    the pop (R2 MAJOR-1) — the report must still measure the DEFAULT
    capacity (1 on the smoke grid).
    """
    out = tmp_path_factory.mktemp("kc-soak")
    env = dict(os.environ)
    env["BRUTEX_SWEEP_WORKER_KERNEL_CAPACITY"] = "7"  # must never reach an arm
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--smoke",
            "--out",
            str(out),
            "--budget-minutes",
            "10",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env=env,
    )
    return proc, out


def _report(out: Path) -> dict[str, Any]:
    payload = json.loads((out / "report" / "soak-report.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_smoke_run_completes_green(
    smoke_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    proc, _out = smoke_run
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "arm OFF done rc=0" in proc.stdout
    assert "arm ON done rc=0" in proc.stdout


def test_smoke_report_mechanics(smoke_run: tuple[subprocess.CompletedProcess[str], Path]) -> None:
    _proc, out = smoke_run
    report = _report(out)
    assert report["kind"] == "kernel-cache-soak"
    assert report["provenance"]["smoke"] is True  # numbers unusable for the flip
    # R2 MAJOR-1: the report states the EFFECTIVE capacity it measured (the
    # smoke grid's single-anchor/single-source default = 1) + the pop flag.
    assert report["provenance"]["worker_kernel_capacity_effective"] == 1
    assert report["provenance"]["capacity_env_ambient_popped"] is True  # planted + popped

    # Byte identity held across the OFF/ON arms (the PR-A T3 law).
    compare = report["compare"]
    assert compare["identity_ok"] is True
    assert compare["diff_paths"] == []
    assert compare["result_files_compared"] > 0
    assert compare["audit_ts_masked_ok"] is True
    assert compare["exit_status_structural_ok"] is True
    assert compare["excluded_sidecars"]  # structural exclusions listed, never silent

    # Telemetry invariants: ON exactly one summary, OFF silent, sum law holds.
    off, on = report["arms"]["off"], report["arms"]["on"]
    assert off["kernel_reuse_summaries"] == 0
    assert on["kernel_reuse_summaries"] == 1
    assert on["builds_total"] + on["hits_total"] == on["tuples_done"]
    assert on["builds_total"] >= 1
    assert off["exit_state"] == "complete"
    assert on["exit_state"] == "complete"
    assert off["quarantine_present"] is False
    assert on["quarantine_present"] is False

    # The RSS sampler produced per-PID peaks + a pool series for both arms.
    for arm in (off, on):
        assert arm["rss_samples"] > 0
        assert arm["pool_peak_rss_kib"] > 0
        assert isinstance(arm["parent_peak_rss_kib"], int)


def test_smoke_acceptance_is_mechanics_only(
    smoke_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    _proc, out = smoke_run
    acceptance = _report(out)["acceptance"]
    assert acceptance["identity"] == "PASS"
    assert acceptance["clean_exit"] == "PASS"
    assert acceptance["telemetry"] == "PASS"
    assert acceptance["builds_bound"] == "PASS"
    # A fixture grid can NEVER judge the RSS/wall/provenance bars nor read
    # FLIP-READY (the R2 MINOR-4 provenance bar leaves smoke unaffected).
    assert acceptance["rss_envelope"] == "SMOKE-UNJUDGED"
    assert acceptance["wall_neutral"] == "SMOKE-UNJUDGED"
    assert acceptance["provenance"] == "SMOKE-UNJUDGED"
    assert acceptance["verdict"].startswith("SMOKE-ONLY")


def test_smoke_summary_and_artifact_bounds(
    smoke_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    _proc, out = smoke_run
    summary = (out / "report" / "summary.md").read_text(encoding="utf-8")
    assert "MEASURED" in summary
    assert "## VALUE" in summary
    assert "NOT decision-grade" in summary
    assert "NOT pre-approved" in summary
    # Bounded artifact: log tails present and capped at 64 KiB each.
    for arm in ("off", "on"):
        tail = out / "report" / f"arm-{arm}-sweep.tail.log"
        assert tail.is_file()
        assert tail.stat().st_size <= 65536
