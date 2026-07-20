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

DEC-CI-SHARD-REBALANCE-001 (issue #1658): the partition heuristic is now
weighted greedy LPT over the committed measured-duration hints file
``scripts/ci/shard_weights.json`` (size-proxy fallback for unhinted
files). The additional pins here: the LPT balance bound, hinted whales
never co-locating, stale hint entries ignored harmlessly, and a
corrupt/misshapen hints file refusing LOUDLY (fail-closed).

The stronger TEST-level zero-skip/zero-dup law (sum of per-shard
collected counts == full collection count) is enforced in CI by the
`test` fan-in job's integrity guard; these unit pins cover the FILE
partition it builds on.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "shard_tests.py"
_WEIGHTS = _REPO_ROOT / "scripts" / "ci" / "shard_weights.json"


def _load_shard_module() -> ModuleType:
    """Import the real script as a module (for the pure-function pins)."""
    spec = importlib.util.spec_from_file_location("_shard_tests_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class TestWeightedPartition:
    """DEC-CI-SHARD-REBALANCE-001: the weighted-LPT law + the hints file."""

    @staticmethod
    def _write_hints(path: Path, weights: dict[str, float], spb: float = 6.6e-05) -> None:
        path.write_text(
            json.dumps(
                {"schema_version": 1, "default_seconds_per_byte": spb, "weights_s": weights}
            ),
            encoding="utf-8",
        )

    def test_hinted_whales_never_colocate(self, tmp_path: Path) -> None:
        """Two hinted heavyweights must land on DIFFERENT shards (LPT seeds
        the N heaviest files into N distinct bins)."""
        _make_tree(tmp_path, 12)
        hints = tmp_path / "hints.json"
        self._write_hints(
            hints,
            {
                "tests/unit/test_mod_000.py": 1400.0,
                "tests/unit/test_mod_001.py": 420.0,
            },
        )
        shard_of: dict[str, int] = {}
        for idx in range(3):
            result = _run(
                "--shard-index",
                str(idx),
                "--shard-count",
                "3",
                "--root",
                str(tmp_path),
                "--dirs",
                "tests/unit",
                "--weights",
                str(hints),
            )
            assert result.returncode == 0, result.stderr
            for line in result.stdout.splitlines():
                if line:
                    shard_of[line] = idx
        assert len(shard_of) == 12
        assert shard_of["tests/unit/test_mod_000.py"] != shard_of["tests/unit/test_mod_001.py"], (
            "hinted whale files must be spread across shards, never co-located"
        )

    def test_partition_with_hints_is_deterministic(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 9)
        hints = tmp_path / "hints.json"
        self._write_hints(hints, {"tests/unit/test_mod_004.py": 99.5})
        args = (
            "--shard-index",
            "2",
            "--shard-count",
            "3",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
            "--weights",
            str(hints),
        )
        first = _run(*args)
        second = _run(*args)
        assert first.returncode == 0 and second.returncode == 0
        assert first.stdout == second.stdout

    def test_stale_hint_entry_is_ignored_harmlessly(self, tmp_path: Path) -> None:
        """A hint whose file was renamed/deleted just loses its hint."""
        _make_tree(tmp_path, 5)
        hints = tmp_path / "hints.json"
        self._write_hints(hints, {"tests/unit/test_deleted_long_ago.py": 5000.0})
        seen: list[str] = []
        for idx in range(2):
            result = _run(
                "--shard-index",
                str(idx),
                "--shard-count",
                "2",
                "--root",
                str(tmp_path),
                "--dirs",
                "tests/unit",
                "--weights",
                str(hints),
            )
            assert result.returncode == 0, result.stderr
            seen.extend(line for line in result.stdout.splitlines() if line)
        assert len(seen) == 5 and len(set(seen)) == 5, "stale hint must not break the partition"

    def test_absent_hints_file_is_fine(self, tmp_path: Path) -> None:
        """An absent hints file degrades to the size proxy (synthetic trees)."""
        _make_tree(tmp_path, 4)
        result = _run(
            "--self-check",
            "--shard-count",
            "2",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
            "--weights",
            str(tmp_path / "nonexistent-hints.json"),
        )
        assert result.returncode == 0, result.stderr
        assert "ABSENT, size-proxy only" in result.stdout

    def test_corrupt_hints_file_fails_loud(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, 3)
        hints = tmp_path / "hints.json"
        hints.write_text("{not json", encoding="utf-8")
        result = _run(
            "--shard-index",
            "0",
            "--shard-count",
            "1",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
            "--weights",
            str(hints),
        )
        assert result.returncode != 0
        assert "unreadable/unparseable" in result.stderr

    @pytest.mark.parametrize(
        ("doc", "message"),
        [
            pytest.param([1, 2], "root must be an object", id="root-not-object"),
            pytest.param(
                {"weights_s": ["tests/unit/test_mod_000.py"]},
                "weights_s must be an object",
                id="weights-not-object",
            ),
            pytest.param(
                {"weights_s": {"tests/unit/test_mod_000.py": -1.0}},
                "finite non-negative",
                id="negative-weight",
            ),
            pytest.param(
                {"weights_s": {"tests/unit/test_mod_000.py": True}},
                "finite non-negative",
                id="bool-weight",
            ),
            pytest.param(
                {"weights_s": {}, "default_seconds_per_byte": 0},
                "finite positive",
                id="zero-spb",
            ),
            pytest.param(
                {"weights_s": {}, "default_seconds_per_byte": "fast"},
                "finite positive",
                id="string-spb",
            ),
        ],
    )
    def test_misshapen_hints_fail_loud(self, tmp_path: Path, doc: object, message: str) -> None:
        _make_tree(tmp_path, 3)
        hints = tmp_path / "hints.json"
        hints.write_text(json.dumps(doc), encoding="utf-8")
        result = _run(
            "--shard-index",
            "0",
            "--shard-count",
            "1",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
            "--weights",
            str(hints),
        )
        assert result.returncode != 0
        assert message in result.stderr

    def test_nan_weight_fails_loud(self, tmp_path: Path) -> None:
        """Python json accepts NaN literals — the schema gate must refuse them."""
        _make_tree(tmp_path, 3)
        hints = tmp_path / "hints.json"
        hints.write_text('{"weights_s": {"tests/unit/test_mod_000.py": NaN}}', encoding="utf-8")
        result = _run(
            "--shard-index",
            "0",
            "--shard-count",
            "1",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
            "--weights",
            str(hints),
        )
        assert result.returncode != 0
        assert "finite non-negative" in result.stderr

    def test_lpt_balance_bound(self, tmp_path: Path) -> None:
        """The greedy-LPT guarantee: max bin <= total/n + max single weight."""
        mod = _load_shard_module()
        _make_tree(tmp_path, 40)
        hints = {
            f"tests/unit/test_mod_{i:03d}.py": float(w)
            for i, w in enumerate([900, 500, 300, 120, 60, 45, 30, 15, 8, 8, 5, 5])
        }
        files = mod.collect_test_files(tmp_path, ("tests/unit",))
        shard_count = 5
        shards = mod.partition(
            files, shard_count, root=tmp_path, hints=hints, seconds_per_byte=6.6e-05
        )
        totals = mod.shard_weight_totals(
            shards, root=tmp_path, hints=hints, seconds_per_byte=6.6e-05
        )
        weights = [mod.file_weight(p, tmp_path, hints, 6.6e-05) for p in files]
        assert max(totals) <= sum(weights) / shard_count + max(weights) + 1e-9, (
            "greedy LPT must honor the makespan bound max <= ideal + max single"
        )
        assert all(shard for shard in shards), "no shard may be empty"

    def test_zero_size_files_still_fill_every_shard(self, tmp_path: Path) -> None:
        """The MIN_WEIGHT_S floor keeps LPT from stacking weight-0 files on
        one bin (an empty shard would be pytest exit 5 in CI)."""
        unit = tmp_path / "tests" / "unit"
        unit.mkdir(parents=True)
        for i in range(6):
            (unit / f"test_empty_{i}.py").write_text("", encoding="utf-8")
        result = _run(
            "--self-check",
            "--shard-count",
            "3",
            "--root",
            str(tmp_path),
            "--dirs",
            "tests/unit",
        )
        assert result.returncode == 0, result.stderr
        assert "self-check OK" in result.stdout
        for idx in range(3):
            shard = _run(
                "--shard-index",
                str(idx),
                "--shard-count",
                "3",
                "--root",
                str(tmp_path),
                "--dirs",
                "tests/unit",
            )
            assert shard.returncode == 0, shard.stderr
            assert [line for line in shard.stdout.splitlines() if line], (
                f"shard {idx} must not be empty even for all-zero-size files"
            )


class TestRealRepoWiring:
    def test_real_repo_self_check_passes_at_ci_shard_count(self) -> None:
        """The exact CI invocation (6 shards over the three coverage dirs)."""
        result = _run("--self-check", "--shard-count", "6")
        assert result.returncode == 0, result.stderr
        assert "self-check OK" in result.stdout

    def test_shipped_weights_file_is_committed_and_valid(self) -> None:
        """The committed hints file parses under the script's own schema gate."""
        assert _WEIGHTS.is_file(), "scripts/ci/shard_weights.json must be committed"
        mod = _load_shard_module()
        hints, spb = mod.load_weights(_WEIGHTS)
        assert hints, "the shipped hints file must carry measured entries"
        assert spb > 0
        assert all(k.startswith("tests/") for k in hints)

    def test_real_repo_whale_files_spread_across_distinct_shards(self) -> None:
        """Regression for run 29644876109's 43-min shard 3: the heaviest
        hinted files (still alive in the tree) must each land on a DIFFERENT
        shard at the CI shard count — LPT seeds the N heaviest into N
        distinct bins, so the whale co-location class is structurally dead.
        """
        mod = _load_shard_module()
        hints, _spb = mod.load_weights(_WEIGHTS)
        alive = sorted(
            (rel for rel in hints if (_REPO_ROOT / rel).is_file()),
            key=lambda rel: (-hints[rel], rel),
        )
        shard_count = 6
        top = alive[:shard_count]
        assert len(top) >= 2, "hints must cover at least two alive whale files"
        shard_of: dict[str, int] = {}
        for idx in range(shard_count):
            result = _run("--shard-index", str(idx), "--shard-count", str(shard_count))
            assert result.returncode == 0, result.stderr
            for line in result.stdout.splitlines():
                if line in hints:
                    shard_of[line] = idx
        shards_used = [shard_of[rel] for rel in top]
        assert len(set(shards_used)) == len(top), (
            f"the top hinted files must occupy distinct shards, got {shards_used} for {top}"
        )

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
