"""Sibling-contract + exit-code tests for scripts/ci/bench_baseline_gate.py.

DEC-BENCH-ABS-BASELINE-001. The script is loaded standalone (the
``spec_from_file_location`` sibling-contract idiom) for unit-level coverage
of its git/gh wrappers with a recorded fake ``run`` callable, and driven
END-TO-END via subprocess over tmpdir fixtures to pin EVERY documented exit
code (validate 0/2 · compare 0-abstain/0-ok/1/2 · capture 0/1/3 · govern 0
· key 0 · usage 3).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from brutex.observability import bench_baseline as bb

from tests.unit.test_bench_baseline import (
    FILENAME,
    KEY,
    NEW_DEC,
    REPO_SLUG,
    make_baseline_doc,
    make_entry_doc,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "bench_baseline_gate.py"


def load_script() -> ModuleType:
    """Standalone-load the gate script (the sibling-contract idiom)."""
    spec = importlib.util.spec_from_file_location("bench_baseline_gate_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = load_script()


def run_gate(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the gate CLI in a subprocess (exit-code contract under test)."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def write_fragments_dir(
    path: Path,
    *,
    value: float = 100.0,
    key: str = KEY,
    entries: frozenset[str] = bb.REQUIRED_ENTRY_KEYS,
    python: str = "3.12.8",
) -> Path:
    """A measurement-output dir: one fragment per entry + fingerprint.json."""
    path.mkdir(parents=True, exist_ok=True)
    for entry in sorted(entries):
        doc = {
            "entry": entry,
            "unit": bb.UNIT,
            "stat": bb.STAT,
            "value_ns_per_op": value,
            "samples_ns_per_op": [value, value * 1.1, value * 1.05],
            "spread_pct": 2.0,
            "ops_per_round": 1000,
            "rounds": 3,
            "workload_hash": bb.workload_hash(entry),
            "wheels": {"fno_math": True, "trading_core": True},
            "context": {"python": python, "numpy": "2.2.1", "os_version": "15.5"},
        }
        (path / f"{entry}.json").write_text(json.dumps(doc), encoding="utf-8")
    (path / "fingerprint.json").write_text(
        json.dumps(
            {
                "hardware_key": key,
                "fingerprint": {
                    "model_slug": "testbox",
                    "arch": "x86-64",
                    "os_family": "linux",
                    "physical_cores": 4,
                    "performance_cores": 2,
                    "efficiency_cores": 2,
                    "total_ram_gib": 8,
                },
                "python": python,
                "commit": "0" * 40,
                "runner_name": "script-test",
            }
        ),
        encoding="utf-8",
    )
    return path


def write_baseline_dir(path: Path, doc: Mapping[str, Any] | None = None) -> Path:
    """A baselines dir holding one committed baseline for the test key."""
    path.mkdir(parents=True, exist_ok=True)
    (path / FILENAME).write_text(
        json.dumps(doc if doc is not None else make_baseline_doc()), encoding="utf-8"
    )
    return path


class TestKeySubcommand:
    def test_key_prints_valid_slug(self) -> None:
        proc = run_gate("key")
        assert proc.returncode == 0
        # The key is the LAST stdout line (the hardware leaf may emit one
        # structlog detection INFO line above it on first probe).
        assert bb.KEY_SLUG_RE.fullmatch(proc.stdout.strip().splitlines()[-1])

    def test_cmd_key_in_process_happy_path(self, capsys: pytest.CaptureFixture[str]) -> None:
        """cmd_key resolves the key via its OWN lazy brutex import (the
        lookup moved here from the core — CodeQL py/cyclic-import 670/671)."""
        assert GATE.cmd_key() == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert bb.KEY_SLUG_RE.fullmatch(out[-1])

    def test_cmd_key_without_brutex_exits_two_with_remediation(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The no-brutex branch names the remediation (the old core
        ``runtime_hardware_key`` contract, relocated verbatim)."""
        monkeypatch.setitem(sys.modules, "brutex.config.hardware_envelope", None)
        assert GATE.cmd_key() == 2
        err = capsys.readouterr().err
        assert "requires an installed brutex" in err
        assert "BRUTEX_BENCH_BASELINE_KEY" in err

    def test_cmd_key_invalid_env_override_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A slug-law-violating BRUTEX_BENCH_BASELINE_KEY fails loud (exit 2)."""
        monkeypatch.setenv("BRUTEX_BENCH_BASELINE_KEY", "Bad Key!")
        assert GATE.cmd_key() == 2
        assert "violates the hardware-key slug law" in capsys.readouterr().err


class TestValidateSubcommand:
    def test_empty_dir_notice_exit_zero(self, tmp_path: Path) -> None:
        proc = run_gate("validate", "--baselines-dir", str(tmp_path))
        assert proc.returncode == 0
        assert "0 baseline files" in proc.stdout
        assert "ABSTAIN loudly" in proc.stdout

    def test_missing_dir_treated_as_empty(self, tmp_path: Path) -> None:
        proc = run_gate("validate", "--baselines-dir", str(tmp_path / "absent"))
        assert proc.returncode == 0

    def test_sound_file_exit_zero(self, tmp_path: Path) -> None:
        write_baseline_dir(tmp_path)
        proc = run_gate("validate", "--baselines-dir", str(tmp_path))
        assert proc.returncode == 0
        assert "1 baseline file(s) structurally sound" in proc.stdout

    def test_schema_violation_exit_two(self, tmp_path: Path) -> None:
        entries = {"bar_lookup": make_entry_doc("bar_lookup", tolerance_pct=250.0)}
        write_baseline_dir(tmp_path, make_baseline_doc(entries=entries))
        proc = run_gate("validate", "--baselines-dir", str(tmp_path))
        assert proc.returncode == 2
        assert "::error::" in proc.stdout

    def test_hash_drift_is_warning_exit_zero(self, tmp_path: Path) -> None:
        entries = {"bar_lookup": make_entry_doc("bar_lookup", workload_hash="sha256:" + "ab" * 32)}
        write_baseline_dir(tmp_path, make_baseline_doc(entries=entries))
        proc = run_gate("validate", "--baselines-dir", str(tmp_path))
        assert proc.returncode == 0
        assert "::warning::" in proc.stdout


class TestCompareSubcommand:
    def test_abstain_no_baseline_exit_zero_with_outputs(self, tmp_path: Path) -> None:
        fragments = write_fragments_dir(tmp_path / "frags")
        out = tmp_path / "gh_output.txt"
        proc = run_gate(
            "compare",
            "--fragments-dir",
            str(fragments),
            "--baselines-dir",
            str(tmp_path / "empty"),
            "--github-output",
            str(out),
        )
        assert proc.returncode == 0
        assert "NO BASELINE" in proc.stdout
        written = out.read_text(encoding="utf-8")
        assert f"hardware_key={KEY}" in written
        assert "baseline_missing=1" in written
        assert "needs_capture=1" in written
        assert "abstain_entries=" in written

    def test_all_ok_exit_zero_with_attestation(self, tmp_path: Path) -> None:
        fragments = write_fragments_dir(tmp_path / "frags")
        baselines = write_baseline_dir(tmp_path / "bl")
        proc = run_gate(
            "compare", "--fragments-dir", str(fragments), "--baselines-dir", str(baselines)
        )
        assert proc.returncode == 0
        assert "BENCH-ABS GATE RAN: compared 6/6" in proc.stdout

    def test_regression_exit_one(self, tmp_path: Path) -> None:
        fragments = write_fragments_dir(tmp_path / "frags", value=1000.0)
        baselines = write_baseline_dir(tmp_path / "bl")
        out = tmp_path / "gh_output.txt"
        proc = run_gate(
            "compare",
            "--fragments-dir",
            str(fragments),
            "--baselines-dir",
            str(baselines),
            "--github-output",
            str(out),
        )
        assert proc.returncode == 1
        assert "REGRESSION" in proc.stdout
        assert "needs_capture=0" in out.read_text(encoding="utf-8")

    def test_suspicious_speedup_exit_one(self, tmp_path: Path) -> None:
        fragments = write_fragments_dir(tmp_path / "frags", value=1.0)
        baselines = write_baseline_dir(tmp_path / "bl")
        proc = run_gate(
            "compare", "--fragments-dir", str(fragments), "--baselines-dir", str(baselines)
        )
        assert proc.returncode == 1
        assert "SUSPICIOUS_SPEEDUP" in proc.stdout

    def test_structural_fragment_gap_exit_two(self, tmp_path: Path) -> None:
        fragments = write_fragments_dir(
            tmp_path / "frags", entries=bb.REQUIRED_ENTRY_KEYS - {"bar_lookup"}
        )
        baselines = write_baseline_dir(tmp_path / "bl")
        proc = run_gate(
            "compare", "--fragments-dir", str(fragments), "--baselines-dir", str(baselines)
        )
        assert proc.returncode == 2
        assert "fragment-set" in proc.stdout

    def test_corrupt_baseline_exit_two(self, tmp_path: Path) -> None:
        fragments = write_fragments_dir(tmp_path / "frags")
        baselines = tmp_path / "bl"
        baselines.mkdir()
        (baselines / FILENAME).write_text("{corrupt", encoding="utf-8")
        proc = run_gate(
            "compare", "--fragments-dir", str(fragments), "--baselines-dir", str(baselines)
        )
        assert proc.returncode == 2

    def test_belt_breach_exit_two(self, tmp_path: Path) -> None:
        """A blessed decision id absent from docs/MIGRATION.md → belt exit 2."""
        fragments = write_fragments_dir(tmp_path / "frags")
        entries = {
            entry: make_entry_doc(entry, decision_id="DEC-SMUGGLED-BASELINE-999")
            for entry in sorted(bb.REQUIRED_ENTRY_KEYS)
        }
        baselines = write_baseline_dir(tmp_path / "bl", make_baseline_doc(entries=entries))
        proc = run_gate(
            "compare", "--fragments-dir", str(fragments), "--baselines-dir", str(baselines)
        )
        assert proc.returncode == 2
        assert "smuggled" in proc.stderr

    def test_missing_fingerprint_exit_two(self, tmp_path: Path) -> None:
        (tmp_path / "frags").mkdir()
        proc = run_gate(
            "compare",
            "--fragments-dir",
            str(tmp_path / "frags"),
            "--baselines-dir",
            str(tmp_path / "bl"),
        )
        assert proc.returncode == 2
        assert "fingerprint.json" in proc.stderr


class TestCaptureSubcommand:
    def _capture_args(self, tmp_path: Path, runs: list[Path], **over: str) -> list[str]:
        args = {
            "--baselines-dir": str(tmp_path / "bl"),
            "--candidate-out": str(tmp_path / "candidate.json"),
            "--pr-body-out": str(tmp_path / "pr-body.md"),
            "--decision-id": bb.BOOTSTRAP_DECISION_ID,
            "--commit": "0" * 40,
            "--run-url": "https://github.com/SJParthi/bruteX/actions/runs/42",
            "--runner-name": "script-test",
        }
        args.update(over)
        out = ["capture", "--runs", *[str(r) for r in runs]]
        for flag, value in args.items():
            out += [flag, value]
        return out

    def test_happy_bootstrap_capture_exit_zero(self, tmp_path: Path) -> None:
        runs = [write_fragments_dir(tmp_path / f"run{i}", value=100.0 + i) for i in range(3)]
        proc = run_gate(*self._capture_args(tmp_path, runs))
        assert proc.returncode == 0
        candidate = json.loads((tmp_path / "candidate.json").read_text(encoding="utf-8"))
        assert candidate["hardware_key"] == KEY
        body = (tmp_path / "pr-body.md").read_text(encoding="utf-8")
        assert "close/reopen this PR" in body
        # The candidate re-parses through the ONE schema law.
        bb.parse_baseline(
            (tmp_path / "candidate.json").read_text(encoding="utf-8"), filename=FILENAME
        )

    def test_noise_refusal_exit_one(self, tmp_path: Path) -> None:
        runs = [
            write_fragments_dir(tmp_path / "run0", value=100.0),
            write_fragments_dir(tmp_path / "run1", value=110.0),
            write_fragments_dir(tmp_path / "run2", value=140.0),
        ]
        proc = run_gate(*self._capture_args(tmp_path, runs))
        assert proc.returncode == 1
        assert "REFUSED" in proc.stderr
        assert "quiet the machine" in proc.stderr

    def test_capture_over_blessed_baseline_carries_exit_zero(self, tmp_path: Path) -> None:
        """Review round 1 (MAJOR): a full-registry capture against an
        already-blessed baseline CARRIES the blessed cells byte-identical
        (exit 0) — the production pipeline always feeds full runs, so this
        must never refuse."""
        write_baseline_dir(tmp_path / "bl")
        runs = [write_fragments_dir(tmp_path / f"run{i}", value=10.0) for i in range(3)]
        proc = run_gate(*self._capture_args(tmp_path, runs))
        assert proc.returncode == 0, proc.stderr
        assert "carried unchanged=" in proc.stdout
        candidate = json.loads((tmp_path / "candidate.json").read_text(encoding="utf-8"))
        # Carried, NOT re-minted from the (deliberately faster) fresh runs.
        assert candidate["entries"]["bar_lookup"]["value_ns_per_op"] == 100.0
        body = (tmp_path / "pr-body.md").read_text(encoding="utf-8")
        assert "Carried unchanged" in body

    def test_overwrite_refusal_exit_one(self, tmp_path: Path) -> None:
        """--replace-entries with NO existing baseline is still a refusal."""
        runs = [write_fragments_dir(tmp_path / f"run{i}", value=100.0) for i in range(3)]
        proc = run_gate(
            *self._capture_args(
                tmp_path,
                runs,
                **{"--replace-entries": "bar_lookup", "--decision-id": NEW_DEC},
            )
        )
        assert proc.returncode == 1
        assert "no existing baseline" in proc.stderr

    def test_unreadable_run_dir_exit_three(self, tmp_path: Path) -> None:
        proc = run_gate(*self._capture_args(tmp_path, [tmp_path / "nowhere"]))
        assert proc.returncode == 3


class TestGovernSubcommand:
    def test_no_baseline_changes_exit_zero(self) -> None:
        proc = run_gate("govern", "--base", "HEAD", cwd=REPO_ROOT)
        assert proc.returncode == 0
        assert "nothing to govern" in proc.stdout

    def test_bad_base_exit_two(self) -> None:
        proc = run_gate("govern", "--base", "no-such-ref-xyz", cwd=REPO_ROOT)
        assert proc.returncode == 2


class TestUsage:
    def test_unknown_subcommand_exit_three(self) -> None:
        proc = run_gate("frobnicate")
        assert proc.returncode == 3

    def test_missing_required_arg_exit_three(self) -> None:
        proc = run_gate("validate")
        assert proc.returncode == 3


# ────────────────────────────────────────────────────────────────────────
# Loaded-module unit tests (recorded fake `run` callables — no real git/gh)
# ────────────────────────────────────────────────────────────────────────


def fake_proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestGitWrappers:
    def test_changed_baselines_parses_and_expands_renames(self) -> None:
        diff = (
            "A\ttests/bench/baselines/new-key.json\n"
            "M\ttests/bench/baselines/old-key.json\n"
            "R100\ttests/bench/baselines/a.json\ttests/bench/baselines/b.json\n"
            "C50\ttests/bench/baselines/a.json\ttests/bench/baselines/c.json\n"
            "\n"
        )
        changed = GATE._git_changed_baselines("origin/main", lambda _argv: fake_proc(diff))
        assert changed == [
            ("A", "new-key.json"),
            ("M", "old-key.json"),
            ("D", "a.json"),
            ("A", "b.json"),
            ("A", "c.json"),
        ]

    def test_changed_baselines_git_failure_raises(self) -> None:
        with pytest.raises(RuntimeError, match="git diff failed"):
            GATE._git_changed_baselines(
                "origin/main", lambda _argv: fake_proc(returncode=128, stderr="boom")
            )

    def test_git_show_present_and_absent(self) -> None:
        assert GATE._git_show("base", "x.json", lambda _argv: fake_proc("{}")) == "{}"
        assert GATE._git_show("base", "x.json", lambda _argv: fake_proc(returncode=128)) is None

    def test_migration_added_lines_strips_markers(self) -> None:
        diff = "+++ b/docs/MIGRATION.md\n+1522. **DEC-X-100**\n-removed\n context\n"
        added = GATE._git_migration_added_lines("base", lambda _argv: fake_proc(diff))
        assert added == "1522. **DEC-X-100**"

    def test_migration_diff_failure_raises(self) -> None:
        with pytest.raises(RuntimeError, match="MIGRATION.md failed"):
            GATE._git_migration_added_lines(
                "base", lambda _argv: fake_proc(returncode=1, stderr="x")
            )


class TestGhRunVerifier:
    URL = "https://github.com/SJParthi/bruteX/actions/runs/99"

    def test_success_parses_and_memoises(self) -> None:
        calls: list[list[str]] = []

        def run(argv: list[str]) -> Any:
            calls.append(argv)
            return fake_proc(
                json.dumps(
                    {
                        "path": bb.BENCH_WORKFLOW_PATH,
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": "a" * 40,
                    }
                )
            )

        verify = GATE.make_gh_run_verifier(run, repo_slug=REPO_SLUG)
        first = verify(self.URL)
        assert first is not None and first["status"] == "completed"
        assert verify(self.URL) == first
        assert len(calls) == 1  # memoised
        # The API path is built from the PINNED slug, and the jq projection
        # carries the run conclusion for the success check.
        assert f"repos/{REPO_SLUG}/actions/runs/99" in calls[0]
        assert any("conclusion" in part for part in calls[0])

    def test_malformed_url_is_none_without_api_call(self) -> None:
        def run(_argv: list[str]) -> Any:  # pragma: no cover — must not be called
            raise AssertionError("gh api must not be called for a malformed URL")

        assert GATE.make_gh_run_verifier(run, repo_slug=REPO_SLUG)("https://nope") is None

    def test_foreign_repo_url_is_none_without_api_call(self) -> None:
        """Review round 1 (BLOCKER belt): the verifier never queries a slug
        other than THIS repo's — a foreign run can never be confirmed."""

        def run(_argv: list[str]) -> Any:  # pragma: no cover — must not be called
            raise AssertionError("gh api must not be called for a foreign-repo URL")

        verify = GATE.make_gh_run_verifier(run, repo_slug=REPO_SLUG)
        assert verify("https://github.com/evil-attacker/bruteX/actions/runs/99") is None

    def test_404_is_none(self) -> None:
        verify = GATE.make_gh_run_verifier(
            lambda _argv: fake_proc(returncode=1, stderr="HTTP 404: Not Found"),
            repo_slug=REPO_SLUG,
        )
        assert verify(self.URL) is None

    def test_transport_failure_retries_then_fails_closed(self) -> None:
        attempts: list[int] = []
        naps: list[float] = []

        def run(_argv: list[str]) -> Any:
            attempts.append(1)
            return fake_proc(returncode=1, stderr="connection reset")

        verify = GATE.make_gh_run_verifier(run, repo_slug=REPO_SLUG, sleeper=naps.append)
        with pytest.raises(RuntimeError, match="fails CLOSED"):
            verify(self.URL)
        assert len(attempts) == GATE.GH_API_ATTEMPTS
        assert naps == [GATE.GH_API_BACKOFF_S] * (GATE.GH_API_ATTEMPTS - 1)

    def test_non_json_success_body_raises(self) -> None:
        verify = GATE.make_gh_run_verifier(lambda _argv: fake_proc("not json"), repo_slug=REPO_SLUG)
        with pytest.raises(RuntimeError, match="non-JSON"):
            verify(self.URL)

    def test_non_dict_json_is_none(self) -> None:
        verify = GATE.make_gh_run_verifier(lambda _argv: fake_proc("[1]"), repo_slug=REPO_SLUG)
        assert verify(self.URL) is None


class TestGithubOutputWriter:
    def test_writes_key_values(self, tmp_path: Path) -> None:
        out = tmp_path / "out.txt"
        GATE._write_github_output(str(out), {"a": "1", "b": "x,y"})
        assert out.read_text(encoding="utf-8") == "a=1\nb=x,y\n"

    def test_none_path_is_noop(self) -> None:
        GATE._write_github_output(None, {"a": "1"})
