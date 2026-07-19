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
    proves END-TO-END that the harness PINS the grid-derived default over it
    in both arm envs (fix-round-2 MAJOR-1 — the pin, unlike the defeated R1
    pop, also survives the child's dotenv re-read) — the report must still
    measure the DEFAULT capacity (1 on the smoke grid).
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
    # Fix-round-2 MAJOR-1: the report states the EFFECTIVE capacity it
    # measured (the smoke grid's single-anchor/single-source default = 1) +
    # the pin mechanism + the parent-ambient presence flag.
    assert report["provenance"]["worker_kernel_capacity_effective"] == 1
    assert report["provenance"]["capacity_env_child_pinned"] is True
    assert report["provenance"]["capacity_env_ambient_present"] is True  # planted + overridden

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


def test_non_smoke_refusal_retry_and_sweep_runs_identity(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The NON-smoke end-to-end proof (fix-round-2, refuter-2 MAJOR + MINOR).

    Previously the non-smoke path had never executed in CI (refuter-2
    MINOR-2 — smoke masks results-depth entirely). This drives it for real
    on a tiny fixture lake via ``--min-free-gib 1``:

    1. a preflight-REFUSED run (absent lake) writes NOTHING into ``--out``,
    2. the documented retry — SAME ``--out``, corrected ``--lake-root`` —
       then runs BOTH arms green (no fresh-out trip),
    3. the identity compare COVERS the results-depth ``sweep_runs/`` sibling
       root (top100.json + sidecars; ``.stamp.json`` masked) and passes,
    4. the sandbox (non-M4-Pro) verdict reads NOT-DECISION-GRADE, never
       FLIP-READY.
    """
    import importlib.util

    base = tmp_path_factory.mktemp("kc-nonsmoke")
    out = base / "out"

    # 1) refusal: absent lake → rc 2, nothing written into --out.
    refusal = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--out",
            str(out),
            "--lake-root",
            str(base / "no-such-lake"),
            "--min-free-gib",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert refusal.returncode == 2
    assert "lake preflight refused" in refusal.stderr
    assert not out.exists() or not any(out.iterdir())

    # Build the real-writer fixture lake via the script's own builder.
    spec = importlib.util.spec_from_file_location("kernel_cache_soak_nonsmoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve via sys.modules
    spec.loader.exec_module(module)
    lake = module.build_smoke_lake(base)

    # 2) retry on the SAME --out with the real lake — the documented flow.
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--out",
            str(out),
            "--lake-root",
            str(lake),
            "--min-free-gib",
            "1",
            "--symbols",
            "NIFTY",
            "--intervals",
            "1minute",
            "--start",
            "2026-01-05",
            "--end",
            "2026-01-17",
            "--exchange",
            "NSE",
            "--segment",
            "INDEX",
            "--k",
            "1,2",
            "--conditions",
            "0,1,2,3,4",
            "--sl",
            "80",
            "--tp",
            "50",
            "--top",
            "10",
            "--workers",
            "2",
            "--budget-minutes",
            "10",
            # A seconds-scale fixture arm outruns the default 5 s poll — a
            # fast cadence keeps the per-worker RSS bar measurable.
            "--sample-interval-s",
            "0.2",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    report = _report(out)
    acceptance = report["acceptance"]
    # Sandbox wall times are seconds-scale, so the judged ON/OFF wall ratio
    # is noise-exposed — a wall_neutral FAIL (rc 1) is tolerated; every
    # OTHER bar must be green.
    judged = [
        name
        for name, status in acceptance.items()
        if name != "verdict" and str(status).startswith("FAIL")
    ]
    assert judged in ([], ["wall_neutral"]), f"{acceptance}\nstderr:\n{proc.stderr}"
    assert proc.returncode == (1 if judged else 0), proc.stderr

    # 3) sweep_runs/ (results-depth) is INSIDE the identity compare.
    assert report["provenance"]["smoke"] is False
    compare = report["compare"]
    assert compare["sweep_runs_files_compared"] > 0
    assert compare["identity_ok"] is True
    assert compare["diff_paths"] == []
    assert (out / "arm-on" / "sweep_runs").is_dir()  # results-depth really ran

    # 4) the sandbox verdict can never read FLIP-READY (non-M4-Pro probes).
    assert acceptance["verdict"].startswith(("NOT-DECISION-GRADE", "NOT-READY"))
    assert acceptance["verdict"] != "FLIP-READY"
