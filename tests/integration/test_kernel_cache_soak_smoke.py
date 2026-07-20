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

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "kernel_cache_soak.py"


def _load_script(name: str) -> ModuleType:
    """Standalone-load the soak script (the sibling-contract idiom)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve via sys.modules
    spec.loader.exec_module(module)
    return module


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
    module = _load_script("kernel_cache_soak_nonsmoke")
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

    # Fix-round-4 MAJOR: the echoed pinned budget IS the engine default at
    # the eval-pool divisor (workers=2 → pool size 1), never a ÷N figure.
    module = _load_script("kernel_cache_soak_nonsmoke_budget")
    assert report["provenance"]["mem_budget_bytes_effective"] == module.resolve_pinned_mem_budget(2)
    assert report["provenance"]["tripwire_env_pinned"] == module.resolve_pinned_tripwires()

    # 4) the sandbox verdict can never read FLIP-READY (non-M4-Pro probes).
    assert acceptance["verdict"].startswith(("NOT-DECISION-GRADE", "NOT-READY"))
    assert acceptance["verdict"] != "FLIP-READY"


def test_real_child_default_budget_matches_pin(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Fix-round-4 MAJOR (refuter-A): the NON-TAUTOLOGICAL budget drift pin.

    A REAL, UN-pinned ``brutex sweep-all`` child at ``BRUTEX_SWEEP_WORKERS=3``
    traverses the ENGINE's own derivation chain — ``ParallelGridDriver.run``
    sets ``budget_workers = pool_size = 3 − 1 = 2``, ``_worker_init`` exports
    ``BRUTEX_SWEEP_BUDGET_WORKERS=2`` into each spawn worker, and the worker's
    ``resolve_mem_budget_bytes()`` logs ONE
    ``hardware_envelope.budget_derived … workers=2 derived_bytes=X`` line.
    X (the CHILD-OBSERVED engine default) must equal the harness pin
    (``resolve_pinned_mem_budget(3)`` — the value the report echoes) and the
    engine default at the explicit N−1 divisor: echo == engine default ==
    child-observed. A divisor drift on EITHER side — the harness re-adopting
    ÷N (the round-3 defect) or the engine changing its pool law / export —
    fails this pin; the round-3 unit pin passed the SAME divisor to both
    sides and was structurally blind to exactly that.
    """
    from brutex.config.hardware_envelope import memory_budget_bytes
    from brutex.engine.sweep_mem_budget import (
        DEFAULT_MEM_BUDGET_BYTES,
        MEM_BUDGET_FRACTION,
    )

    module = _load_script("kernel_cache_soak_childprobe")
    expected = int(module.resolve_pinned_mem_budget(3))
    if expected == DEFAULT_MEM_BUDGET_BYTES:
        pytest.skip(
            "derived budget equals the 8 GiB floor (RAM undetected or exactly "
            "96 GiB) — the once-per-process budget_derived line deliberately "
            "does not fire, so the child observation is unavailable here"
        )
    # Independent reference: the COMMON derivation at the EXPLICIT divisor.
    assert expected == memory_budget_bytes(
        fraction=MEM_BUDGET_FRACTION,
        floor_bytes=DEFAULT_MEM_BUDGET_BYTES,
        workers=2,
        env={},
    )

    base = tmp_path_factory.mktemp("kc-childprobe")
    lake = module.build_smoke_lake(base)
    home = base / "home"
    (home / ".brutex").mkdir(parents=True)
    cwd = base / "cwd"
    cwd.mkdir()
    env = dict(os.environ)
    # UN-pinned on purpose: the child must resolve the DEFAULT law itself.
    for name in ("BRUTEX_SWEEP_MEM_BUDGET_BYTES", "BRUTEX_SWEEP_BUDGET_WORKERS"):
        env.pop(name, None)
    env.update(
        {
            "BRUTEX_SWEEP_WORKERS": "3",
            "BRUTEX_LOG_LEVEL": "INFO",
            "BRUTEX_RESULTS_DEPTH": "0",
            "PYTHONHASHSEED": "0",
            "HOME": str(home),  # dotenv isolation — no ~/.brutex/.env leaks
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "brutex.cli",
            "sweep-all",
            "--lake-root",
            str(lake),
            "--results-root",
            str(base / "results"),
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
            "--directions",
            "BUY",
            "--k",
            "1",
            "--conditions",
            "0,1,2,3",
            "--sl",
            "none",
            "--tp",
            "none",
            "--top",
            "5",
            "--no-options-sim",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=env,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    observed: set[int] = set()
    for raw in (proc.stderr + "\n" + proc.stdout).splitlines():
        line = module.strip_ansi(raw)
        if "hardware_envelope.budget_derived" not in line or "workers=2" not in line:
            continue  # parent-side (workers=1) lines are a different resolver call
        match = re.search(r"derived_bytes=(\d+)", line)
        assert match is not None, line
        observed.add(int(match.group(1)))
    assert observed == {expected}, (
        f"child-observed engine-default budget {observed or '{}'} != harness pin "
        f"{expected} (echo == engine default == child-observed must hold)"
    )
