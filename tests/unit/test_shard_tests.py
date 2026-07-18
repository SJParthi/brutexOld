"""Partition-invariant tests for ``scripts/ci/shard_tests.py``.

DEC-SWEEP-COVERAGE-SHARD-001: the CI whole-package coverage pass is split
across parallel shards whose file partition is computed by
``scripts/ci/shard_tests.py``. These tests pin the partition LAW the
sharded gate depends on — total (every test file assigned), disjoint
(no file assigned twice), deterministic (byte-identical output across
invocations), and loud on every degenerate (empty shard, out-of-range
index, missing target dir) — by invoking the REAL script as a
subprocess (the ``test_ci_dec_id_validator.py`` convention) against
both a synthetic tree and the real repository tree.

The stronger TEST-level zero-skip/zero-dup law (sum of per-shard
collected counts == full collection count) is enforced in CI by the
`test` fan-in job's integrity guard; these unit pins cover the FILE
partition it builds on.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "shard_tests.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _make_tree(root: Path, n_files: int) -> None:
    unit = root / "tests" / "unit"
    unit.mkdir(parents=True)
    for i in range(n_files):
        # varied sizes so the size-desc ordering is exercised
        (unit / f"test_mod_{i:03d}.py").write_text("x = 1\n" * (i + 1), encoding="utf-8")


class TestPartitionInvariants:
    def test_partition_is_total_and_disjoint(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 17)
        shard_count = 4
        seen: list[str] = []
        for idx in range(shard_count):
            result = _run(
                "--shard-index",
                str(idx),
                "--shard-count",
                str(shard_count),
                "--root",
                str(tmp_path),
                "--dirs",
                "tests/unit",
            )
            assert result.returncode == 0, result.stderr
            seen.extend(line for line in result.stdout.splitlines() if line)
        assert len(seen) == 17, "partition must be TOTAL — every file assigned"
        assert len(set(seen)) == 17, "partition must be DISJOINT — no file twice"
        expected = {
            p.relative_to(tmp_path).as_posix()
            for p in (tmp_path / "tests" / "unit").glob("test_*.py")
        }
        assert set(seen) == expected

    def test_partition_is_deterministic(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 9)
        first = _run(
            "--shard-index",
            "1",
            "--shard-count",
            "3",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
        )
        second = _run(
            "--shard-index",
            "1",
            "--shard-count",
            "3",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
        )
        assert first.returncode == 0 and second.returncode == 0
        assert first.stdout == second.stdout, "same inputs must yield byte-identical shards"

    def test_self_check_reports_ok(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 7)
        result = _run(
            "--self-check", "--shard-count", "3", "--root", str(tmp_path), "--dirs", "tests/unit"
        )
        assert result.returncode == 0, result.stderr
        assert "self-check OK" in result.stdout
        assert "7" in result.stdout

    def test_non_test_files_are_never_collected(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 3)
        (tmp_path / "tests" / "unit" / "conftest.py").write_text("", encoding="utf-8")
        (tmp_path / "tests" / "unit" / "helper_mod.py").write_text("", encoding="utf-8")
        result = _run(
            "--shard-index",
            "0",
            "--shard-count",
            "1",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
        )
        assert result.returncode == 0, result.stderr
        files = [line for line in result.stdout.splitlines() if line]
        assert len(files) == 3
        assert all(f.rsplit("/", 1)[-1].startswith("test_") for f in files)


class TestLoudDegenerates:
    def test_empty_shard_is_refused(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 2)
        result = _run(
            "--shard-index",
            "0",
            "--shard-count",
            "3",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
        )
        assert result.returncode != 0
        assert "exceeds" in result.stderr

    def test_out_of_range_index_is_refused(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 5)
        result = _run(
            "--shard-index",
            "5",
            "--shard-count",
            "5",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
        )
        assert result.returncode != 0
        assert "out of range" in result.stderr

    def test_missing_target_dir_is_refused(self, tmp_path: Path) -> None:
        result = _run(
            "--shard-index",
            "0",
            "--shard-count",
            "1",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
        )
        assert result.returncode != 0
        assert "target dir missing" in result.stderr

    def test_missing_index_without_self_check_is_refused(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 2)
        result = _run("--shard-count", "2", "--root", str(tmp_path), "--dirs", "tests/unit")
        assert result.returncode != 0
        assert "--shard-index is required" in result.stderr


class TestRealRepoWiring:
    def test_real_repo_self_check_passes_at_ci_shard_count(self) -> None:
        """The exact CI invocation (6 shards over the three coverage dirs)."""
        result = _run("--self-check", "--shard-count", "6")
        assert result.returncode == 0, result.stderr
        assert "self-check OK" in result.stdout

    def test_glob_mirrors_pyproject_python_files(self) -> None:
        """Drift pin: the script's glob must equal pytest's python_files."""
        pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert 'python_files = ["test_*.py"]' in pyproject, (
            "pyproject python_files changed — update scripts/ci/shard_tests.py's "
            "TEST_FILE_GLOB to match, or the shard universe silently diverges "
            "from pytest collection"
        )
        script = _SCRIPT.read_text(encoding="utf-8")
        assert 'TEST_FILE_GLOB = "test_*.py"' in script

    def test_ci_yml_wires_the_shard_script(self) -> None:
        """The shard matrix + fan-in actually consume this script + count law."""
        ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "scripts/ci/shard_tests.py --self-check" in ci
        assert "scripts/ci/shard_tests.py --shard-index" in ci
        assert ci.count("grep -cE '^tests/.+::'") >= 2, (
            "the zero-skip/zero-dup law needs the IDENTICAL counting pipeline "
            "on both the shard side and the fan-in side"
        )
