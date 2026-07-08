"""Byte-stable goldens for the bench-baseline dump + report (format pins).

DEC-BENCH-ABS-BASELINE-001. EVERY number in these fixtures is EXPLICITLY
SYNTHETIC (test-only hardware key ``testos-x86-2c1p1e-1g-x86-64``, hand
values like ``123.0``) — the goldens pin the SERIALIZATION FORMAT and the
attestation line, never a performance claim (Golden Rules #1/#10; the
bootstrap PR ships zero measured baselines).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from brutex.observability import bench_baseline as bb

from tests.unit.test_bench_baseline import CONTEXT, make_fragment

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_KEY = "testos-x86-2c1p1e-1g-x86-64"
GOLDEN_FILENAME = f"{GOLDEN_KEY}.json"


def synthetic_baseline() -> bb.Baseline:
    """The one explicitly-synthetic golden baseline document."""
    provenance = bb.Provenance(
        run_url="https://github.com/SJParthi/bruteX/actions/runs/100000001",
        commit="0123456789abcdef0123456789abcdef01234567",
        captured_utc="2026-07-06T00:00:00Z",
        runner_name="golden-synthetic",
    )
    entries = {
        entry: bb.EntryRecord(
            value_ns_per_op=123.0,
            samples_ns_per_op=(123.0, 130.0, 125.0),
            spread_pct=1.5,
            tolerance_pct=50.0,
            improvement_warn_ratio=2.0,
            suspicious_speedup_ratio=4.0,
            workload_hash=bb.workload_hash(entry),
            decision_id=bb.BOOTSTRAP_DECISION_ID,
            provenance=provenance,
            note="SYNTHETIC golden fixture value — never a measurement",
        )
        for entry in sorted(bb.REQUIRED_ENTRY_KEYS)
    }
    return bb.Baseline(
        schema_version=bb.SCHEMA_VERSION,
        hardware_key=GOLDEN_KEY,
        fingerprint={
            "model_slug": "testos-x86",
            "arch": "x86-64",
            "os_family": "linux",
            "physical_cores": 2,
            "performance_cores": 1,
            "efficiency_cores": 1,
            "total_ram_gib": 1,
        },
        context=dict(CONTEXT),
        entries=entries,
    )


def synthetic_report() -> bb.CompareReport:
    """A deterministic synthetic compare (one regression, one warn, rest OK)."""
    fragments = {entry: make_fragment(entry, 123.0) for entry in sorted(bb.REQUIRED_ENTRY_KEYS)}
    fragments["bar_lookup"] = make_fragment("bar_lookup", 246.0)  # 2x — REGRESSION
    fragments["result_append"] = make_fragment("result_append", 41.0)  # 3x faster — WARN
    return bb.compare(
        synthetic_baseline(),
        fragments,
        runtime_key=GOLDEN_KEY,
        runtime_python="3.12.8",
    )


class TestDumpGolden:
    def test_dump_matches_fixture_byte_for_byte(self) -> None:
        expected = (FIXTURES / "bench_baseline_v1.json").read_text(encoding="utf-8")
        assert bb.dump_baseline(synthetic_baseline()) == expected

    def test_fixture_parses_back_identically(self) -> None:
        text = (FIXTURES / "bench_baseline_v1.json").read_text(encoding="utf-8")
        assert bb.parse_baseline(text, filename=GOLDEN_FILENAME) == synthetic_baseline()


class TestReportGolden:
    def test_report_matches_fixture_byte_for_byte(self) -> None:
        expected = (FIXTURES / "bench_baseline_report.txt").read_text(encoding="utf-8")
        assert bb.render_report(synthetic_report()) == expected

    def test_attestation_line_pinned(self) -> None:
        report = synthetic_report()
        assert report.attestation_line == (
            "BENCH-ABS GATE RAN: compared 6/6 entries against "
            f"tests/bench/baselines/{GOLDEN_KEY}.json (captured 2026-07-06, "
            f"decision ids {bb.BOOTSTRAP_DECISION_ID}, max blessed spread 1.5%)"
        )


class TestBlessPrBodyMeritGates:
    """The rendered bless PR body passes the REAL non-exempt merit gates.

    The bot bless PR is zplus/DoR author-exempt (github-actions[bot]) but the
    3 merit gates still read it — a bless PR must never be born red on them.
    """

    def test_pr_body_passes_the_three_merit_gates(self, tmp_path: Path) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        body = bb.render_pr_body(
            synthetic_baseline(),
            added=sorted(bb.REQUIRED_ENTRY_KEYS),
            replaced=["bar_lookup"],
            run_url="https://github.com/SJParthi/bruteX/actions/runs/100000001",
        )
        body_file = tmp_path / "bless-body.md"
        body_file.write_text(body, encoding="utf-8")
        gates = [
            ["scripts/ci/validate_dec_ids.py"],
            ["scripts/ci/validate_no_hallucination.py"],
        ]
        for gate in gates:
            proc = subprocess.run(
                [sys.executable, str(repo_root / gate[0]), str(body_file)],
                capture_output=True,
                text=True,
                check=False,
                cwd=repo_root,
            )
            assert proc.returncode == 0, f"{gate[0]}: {proc.stdout}{proc.stderr}"
        proc = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts/ci/validate_bugfix_regression.py"),
                str(body_file),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=repo_root,
            env={
                "PR_TITLE": "feat(bench): bless absolute benchmark baseline for testos",
                "PR_FILES": "tests/bench/baselines/testos.json",
                "PATH": "/usr/bin:/bin",
            },
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
