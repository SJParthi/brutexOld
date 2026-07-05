"""Unit tests for the auto-discovery coverage gate (DEC-CICD-GATE-HONESTY-001).

``scripts/ci/coverage_gate_discovery.py`` COMPLEMENTS the hardcoded
per-module 100% coverage allowlist in ``ci.yml`` (and the whole-package
floor) with a deterministic discovery of the brutex modules a PR TOUCHED +
their import-verified tests, so a newly added module can never escape the
per-file independence gate merely for being absent from a static list,
while a legacy modification with no dedicated test is never newly blocked.

These tests pin the NEW design at 100% line + branch, with the real
subprocess (git / pytest / coverage) calls monkeypatched (no nested pytest
run, no reliance on git state). They include explicit REGRESSION tests for
the two blockers the redesign fixes:

* **B1** — a MODIFIED brutex module with no dedicated test is WARN-skipped
  (exit 0), NOT failed; an ADDED module with no test FAILS (exit 1).
* **B2** — a same-stem candidate test that does NOT import the module (a
  foreign test) is REJECTED, so it is never mistaken for the module's own
  coverage.

The import check is an :func:`ast.parse` walk (NOT a substring scan), so
the regression set also pins both flaws that scan had: the SUBSTRING
false-accept (``import brutex.feed.lookahead_extra`` /
``import other.brutex.feed.lookahead`` / the dotted path only in a
``# comment``) is REJECTED, and a multi-line parenthesized package import
(``from brutex.feed import (\\n    lookahead,\\n)``) is ACCEPTED; a
syntactically broken candidate is caught as a ``SyntaxError`` and rejected.

Also covered: import-verified isolation PASS / FAIL (<100% / not-measured),
zero-touched PASS, the curated exclude-allowlist, deterministic ordering,
Added-vs-Modified handling, renamed (R) path resolution, git-error
fail-closed exit 2, and multi-file curated-suite discovery. No
``# pragma: no cover`` is needed — every branch is reached honestly.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "coverage_gate_discovery.py"


def _load() -> ModuleType:
    """Import the gate script as a top-level module.

    ``scripts/ci`` is put on ``sys.path`` and the script imported by name
    (rather than ``spec_from_file_location``) so coverage's module-name
    targeting (``--cov=coverage_gate_discovery``) actually traces it — a
    dynamically spec-loaded module is not matched by coverage's source
    filter, which would silently report 0% (the exact masking the gate
    itself outlaws).
    """
    script_dir = str(_SCRIPT.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    return importlib.import_module("coverage_gate_discovery")


@pytest.fixture(name="gate")
def _gate() -> ModuleType:
    """The loaded discovery-gate module."""
    return _load()


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """A minimal stand-in for ``subprocess.CompletedProcess``."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestGitDiffNameStatus:
    """``_git_diff_name_status``: status parsing + the three git outcomes."""

    def test_added_and_modified_rows_parsed(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_a: Any, **_k: Any) -> SimpleNamespace:
            return _completed(stdout="A\tbrutex/a.py\nM\tbrutex/b.py\n")

        monkeypatch.setattr(gate.subprocess, "run", fake_run)
        assert gate._git_diff_name_status("origin/main") == [
            ("A", "brutex/a.py"),
            ("M", "brutex/b.py"),
        ]

    def test_rename_row_uses_new_path_and_leading_letter(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A rename row: `R100<TAB>old<TAB>new`. Only the leading R and the
        # final (new) path are kept.
        def fake_run(*_a: Any, **_k: Any) -> SimpleNamespace:
            return _completed(stdout="R100\tbrutex/old.py\tbrutex/new.py\n")

        monkeypatch.setattr(gate.subprocess, "run", fake_run)
        assert gate._git_diff_name_status("origin/main") == [("R", "brutex/new.py")]

    def test_blank_lines_and_empty_path_dropped(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A blank line is skipped; a status row whose final field is empty
        # (degenerate) is also skipped (the `if path` guard's False side).
        def fake_run(*_a: Any, **_k: Any) -> SimpleNamespace:
            return _completed(stdout="\nM\tbrutex/real.py\nA\t\n")

        monkeypatch.setattr(gate.subprocess, "run", fake_run)
        assert gate._git_diff_name_status("origin/main") == [("M", "brutex/real.py")]

    def test_nonzero_exit_raises_discovery_error(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gate.subprocess,
            "run",
            lambda *_a, **_k: _completed(returncode=128, stderr="bad revision"),
        )
        with pytest.raises(gate.DiscoveryError, match="git diff failed"):
            gate._git_diff_name_status("nope")

    def test_git_missing_raises_discovery_error(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> SimpleNamespace:
            raise OSError("git: not found")

        monkeypatch.setattr(gate.subprocess, "run", boom)
        with pytest.raises(gate.DiscoveryError, match="cannot run git"):
            gate._git_diff_name_status("origin/main")


class TestDiscoverTouchedModules:
    """``discover_touched_modules``: filtering + status + determinism."""

    def test_filters_and_sorts_with_status(self, gate: ModuleType) -> None:
        rows = [
            ("M", "brutex/z/last.py"),
            ("A", "brutex/a/first.py"),
            ("M", "scripts/ci/foo.py"),  # dropped: out of brutex scope
            ("A", "brutex/pkg/__init__.py"),  # dropped: __init__
            ("M", "tests/unit/test_x.py"),  # dropped: tests/
            ("M", "docs/MIGRATION.md"),  # dropped: not .py
            ("A", "README.md"),  # dropped: not .py
            ("M", "brutex/note.txt"),  # dropped: not .py
            ("A", "other/thing.py"),  # dropped: out of brutex scope
        ]
        assert gate.discover_touched_modules(rows) == [
            ("brutex/a/first.py", "A"),
            ("brutex/z/last.py", "M"),
        ]

    def test_status_letter_is_normalised_to_leading_char(self, gate: ModuleType) -> None:
        # A rename/copy carries a similarity score (R100); only the leading
        # letter is kept.
        assert gate.discover_touched_modules([("R100", "brutex/x.py")]) == [("brutex/x.py", "R")]

    def test_exclude_allowlist_is_dropped(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Inject a single-entry allowlist (independent of the shipped set) to
        # exercise the drop branch deterministically.
        monkeypatch.setattr(gate, "COVERAGE_DISCOVERY_EXCLUDE", frozenset({"brutex/excluded.py"}))
        assert gate.discover_touched_modules(
            [("M", "brutex/excluded.py"), ("M", "brutex/real.py")]
        ) == [("brutex/real.py", "M")]

    def test_empty_diff_is_empty_plan(self, gate: ModuleType) -> None:
        assert gate.discover_touched_modules([]) == []

    def test_duplicate_paths_keep_first_status(self, gate: ModuleType) -> None:
        # setdefault keeps the FIRST-seen status for a repeated path.
        assert gate.discover_touched_modules([("A", "brutex/a.py"), ("M", "brutex/a.py")]) == [
            ("brutex/a.py", "A")
        ]

    def test_shipped_exclude_allowlist_is_curated_multifile_modules(self, gate: ModuleType) -> None:
        # Pin the documented invariant: the allowlist holds ONLY the modules whose
        # 100% coverage comes from a CURATED multi-file suite (no single name-matched
        # per-file test reaches 100% standalone), each gated by the whole-package
        # floor + a dedicated static per-module gate instead:
        #   * brutex/dashboard/app.py — the route-glue (DEC-DASHBOARD-RESULTS-PAGE-001);
        #   * brutex/dashboard/pull_controller.py — the SPOT pull controller, covered by
        #     the 5-file pull-controller/pool/health/stall/write-batch suite
        #     (DEC-WRITE-BATCH-COALESCE-001);
        #   * brutex/dashboard/db_view.py — the ~720-stmt /db viewer, covered by the
        #     six-file db_view test family (DEC-DASHBOARD-EMPTY-STATE-001);
        #   * brutex/engine/stage1a.py — the Stage-1A matrix builder, covered by
        #     test_engine_stage1a.py (builder + VWAP/BOS bits) +
        #     test_engine_sweep_driver_walk_forward.py (the walk-forward helpers that
        #     live in stage1a.py — the suite is stem-named for sweep_driver.py, whose
        #     walk-forward entry points it gates); the static engine gate runs the
        #     whole tests/unit/test_engine_*.py suite at 100%
        #     (DEC-COVERAGE-DISCOVERY-STAGE1A-001);
        #   * brutex/dashboard/movers_snapshot.py — the /movers spot-snapshot module
        #     whose spot-candidate readers only execute against a real seeded lake
        #     partition (the integration suite), with no test_dashboard_movers_snapshot.py
        #     stem match; gated by the whole-package floor at 100.00% over the movers
        #     unit+integration suites (DEC-COVERAGE-DISCOVERY-MOVERS-SNAPSHOT-001).
        #   * brutex/config/credentials.py — the Groww/token/AWS credentials module
        #     whose 100% is a curated two-file property (test_credentials.py +
        #     test_credentials_aws.py — DEC-AWS-ONLY-CREDS-001 removed the Groww file/env
        #     path, so the old test_groww_credentials.py is gone). Both stem-match
        #     test_credentials*, so it CAN reach 100% standalone; kept excluded as a
        #     COMPLEMENT, gated by the dedicated static per-module gate (that exact pair)
        #     + the whole-package floor (DEC-AWS-ONLY-CREDS-001).
        #   * brutex/engine/sweep_driver.py — the Stage-2 sweep driver, covered by
        #     test_engine_sweep_driver.py (evaluate/sweep/rank + the public metrics
        #     seams) + test_engine_walk_forward.py (the walk-forward half that lives in
        #     sweep_driver.py — the EXACT stage1a.py shape); ~83% standalone against its
        #     stem match, 100.00% with the pair; the static whole-engine gate runs
        #     tests/unit/test_engine_*.py together at 100%
        #     (DEC-SWEEP-RUST-TRADESIM-WIRE-001).
        # Keep it tiny + justified.
        assert (
            frozenset(
                {
                    "brutex/dashboard/app.py",
                    "brutex/dashboard/pull_controller.py",
                    "brutex/dashboard/db_view.py",
                    "brutex/engine/stage1a.py",
                    "brutex/dashboard/movers_snapshot.py",
                    "brutex/config/credentials.py",
                    "brutex/engine/sweep_driver.py",
                }
            )
            == gate.COVERAGE_DISCOVERY_EXCLUDE
        )

    def test_excluded_db_view_module_is_dropped_from_discovery(self, gate: ModuleType) -> None:
        # The shipped exclude really removes db_view.py from the gated set (a touch
        # of it rides the whole-package floor + its six-file static gate instead of
        # failing the discovery gate on an unreachable single-file 100%).
        assert gate.discover_touched_modules(
            [("M", "brutex/dashboard/db_view.py"), ("M", "brutex/dashboard/nav.py")]
        ) == [("brutex/dashboard/nav.py", "M")]

    def test_excluded_app_module_is_dropped_from_discovery(self, gate: ModuleType) -> None:
        # The shipped exclude really removes app.py from the gated set (a touch of it
        # rides the whole-package floor instead of failing the discovery gate).
        assert gate.discover_touched_modules(
            [("M", "brutex/dashboard/app.py"), ("M", "brutex/dashboard/nav.py")]
        ) == [("brutex/dashboard/nav.py", "M")]

    def test_excluded_movers_snapshot_module_is_dropped_from_discovery(
        self, gate: ModuleType
    ) -> None:
        # The shipped exclude really removes movers_snapshot.py from the gated set (its
        # spot-candidate readers only execute against a real seeded lake partition in
        # the integration suite, so it rides the whole-package floor instead of failing
        # the discovery gate on an unreachable single-file 100%).
        assert gate.discover_touched_modules(
            [
                ("M", "brutex/dashboard/movers_snapshot.py"),
                ("M", "brutex/dashboard/nav.py"),
            ]
        ) == [("brutex/dashboard/nav.py", "M")]


class TestCandidateTestGlobs:
    """``candidate_test_globs``: the module -> test-glob convention."""

    def test_nested_module_prefixes_parent_dirs(self, gate: ModuleType) -> None:
        globs = gate.candidate_test_globs("brutex/data/partition.py")
        # Most specific first, then less specific, each with a trailing *.
        assert globs == ["test_data_partition*.py", "test_partition*.py"]

    def test_top_level_module_is_bare(self, gate: ModuleType) -> None:
        assert gate.candidate_test_globs("brutex/doctor.py") == ["test_doctor*.py"]

    def test_deeply_nested_module_is_ordered_and_distinct(self, gate: ModuleType) -> None:
        globs = gate.candidate_test_globs("brutex/a/b/c/foo.py")
        assert globs == [
            "test_a_b_c_foo*.py",
            "test_b_c_foo*.py",
            "test_c_foo*.py",
            "test_foo*.py",
        ]
        # Each cut yields a strictly shorter suffix -> all candidates distinct.
        assert len(globs) == len(set(globs))

    def test_bare_filename_without_package_root(self, gate: ModuleType) -> None:
        # A path with no parent segment (the `parts` falsy branch of the
        # root-strip guard): only the bare candidate is produced.
        assert gate.candidate_test_globs("foo.py") == ["test_foo*.py"]

    def test_non_brutex_root_parent_is_kept(self, gate: ModuleType) -> None:
        # A parent dir that is NOT `brutex` is retained as a prefix (the
        # `parts[0] == "brutex"` False side of the root-strip guard).
        assert gate.candidate_test_globs("other/foo.py") == [
            "test_other_foo*.py",
            "test_foo*.py",
        ]


class TestModuleImportTokens:
    """``module_import_tokens``: the dotted identifiers proving coverage."""

    def test_tokens_for_nested_module(self, gate: ModuleType) -> None:
        # (module_dotted, parent_pkg, stem) for the ast-based check.
        assert gate.module_import_tokens("brutex/feed/lookahead.py") == (
            "brutex.feed.lookahead",
            "brutex.feed",
            "lookahead",
        )


class TestFileImportsModule:
    """``_file_imports_module``: ast-precise accept/reject of importers.

    The matcher parses the candidate with :func:`ast.parse` (NOT a
    substring scan), so it accepts only genuine import statements and is
    immune to comments, string contents, prefix/suffix collisions, and
    line wrapping. These tests pin both the accept paths and the two flaws
    the ast rewrite eliminates: the SUBSTRING false-accept (``..._extra`` /
    ``other.brutex...`` / comment-only) and the multi-line parenthesized
    false-reject.
    """

    def _write(self, tmp_path: Path, body: str) -> Path:
        f = tmp_path / "test_x.py"
        f.write_text(body, encoding="utf-8")
        return f

    def test_dotted_import_accepted(self, gate: ModuleType, tmp_path: Path) -> None:
        f = self._write(tmp_path, "import brutex.feed.lookahead\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is True

    def test_from_dotted_import_accepted(self, gate: ModuleType, tmp_path: Path) -> None:
        f = self._write(tmp_path, "from brutex.feed.lookahead import LookAheadBarrier\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is True

    def test_from_package_import_stem_accepted(self, gate: ModuleType, tmp_path: Path) -> None:
        f = self._write(tmp_path, "from brutex.feed import lookahead\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is True

    def test_from_package_import_stem_as_alias_accepted(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        # `from brutex.feed import lookahead as lh` — the imported NAME
        # (`lookahead`) is matched, never the asname.
        f = self._write(tmp_path, "from brutex.feed import lookahead as lh\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is True

    def test_multiline_parenthesized_package_import_accepted(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        # MINOR-2 regression: a parenthesized multi-line package import
        # lands the stem on a DIFFERENT line than the `from <pkg> import`
        # prefix. The old same-line substring scan REJECTED this valid
        # import; the ast walk accepts it.
        f = self._write(
            tmp_path,
            "from brutex.feed import (\n    lookahead,\n    other,\n)\n",
        )
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is True

    def test_stem_prefix_collision_rejected(self, gate: ModuleType, tmp_path: Path) -> None:
        # FALSE-ACCEPT regression: `import brutex.feed.lookahead_extra`
        # contains the dotted path as a substring but is a DIFFERENT
        # module — the ast exact-name match rejects it.
        f = self._write(tmp_path, "import brutex.feed.lookahead_extra\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is False

    def test_foreign_prefix_dotted_path_rejected(self, gate: ModuleType, tmp_path: Path) -> None:
        # FALSE-ACCEPT regression: `import other.brutex.feed.lookahead`
        # ENDS with the dotted path but is a foreign module — rejected.
        f = self._write(tmp_path, "import other.brutex.feed.lookahead\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is False

    def test_dotted_path_only_in_comment_rejected(self, gate: ModuleType, tmp_path: Path) -> None:
        # FALSE-ACCEPT regression: the dotted path appears ONLY in a
        # comment — no import node references it, so it is rejected.
        f = self._write(tmp_path, "# imports brutex.feed.lookahead indirectly\nimport os\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is False

    def test_relative_import_is_non_match(self, gate: ModuleType, tmp_path: Path) -> None:
        # A relative `from . import x` has node.module is None — never a
        # match (the `node.module is None` continue branch).
        f = self._write(tmp_path, "from . import lookahead\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is False

    def test_foreign_same_stem_rejected(self, gate: ModuleType, tmp_path: Path) -> None:
        # B2 regression: a same-stem test importing a DIFFERENT package's
        # `models` must NOT be accepted for brutex/data/models.py. Neither
        # `from brutex import models` (module != parent_pkg) nor a bare
        # `import models` (name != module_dotted) matches.
        f = self._write(tmp_path, "from brutex import models\nimport models\n")
        assert gate._file_imports_module(f, "brutex/data/models.py") is False

    def test_unrelated_import_and_comment_rejected(self, gate: ModuleType, tmp_path: Path) -> None:
        # An UNRELATED sibling import from the right package plus the stem
        # only in a comment — no node imports `lookahead`, so rejected.
        f = self._write(
            tmp_path,
            "from brutex.feed import other\n# lookahead mentioned in a comment\n",
        )
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is False

    def test_syntax_error_is_non_match(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A syntactically broken candidate cannot be parsed — caught as a
        # SyntaxError specifically, warned on stdout, and treated as a
        # non-match (a broken test can't be relied on to cover the module).
        f = self._write(tmp_path, "def broken(:\n    pass\n")
        assert gate._file_imports_module(f, "brutex/feed/lookahead.py") is False
        assert "cannot parse" in capsys.readouterr().out

    def test_unreadable_file_is_non_match(self, gate: ModuleType, tmp_path: Path) -> None:
        # A directory path raises OSError on read_text -> treated as non-match.
        missing = tmp_path / "definitely_a_dir"
        missing.mkdir()
        assert gate._file_imports_module(missing, "brutex/feed/lookahead.py") is False


class TestDiscoverTestsForModule:
    """``discover_tests_for_module``: glob match + import verification."""

    def _make(self, tmp_path: Path, rel: str, body: str) -> Path:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def test_matches_importing_test(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        f = self._make(
            tmp_path,
            "tests/unit/test_data_partition.py",
            "import brutex.data.partition\n",
        )
        out = gate.discover_tests_for_module("brutex/data/partition.py", [f])
        assert out == ["tests/unit/test_data_partition.py"]

    def test_glob_match_but_no_import_rejected(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # B2: the basename matches the stem glob but the file imports a
        # different module -> rejected (treated as no test).
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        f = self._make(
            tmp_path,
            "tests/unit/test_models.py",
            "import brutex.models\n",  # NOT brutex.data.models
        )
        assert gate.discover_tests_for_module("brutex/data/models.py", [f]) == []

    def test_no_basename_match_skipped(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        f = self._make(tmp_path, "tests/unit/test_unrelated.py", "import brutex.data.partition\n")
        assert gate.discover_tests_for_module("brutex/data/partition.py", [f]) == []

    def test_multi_file_curated_suite_both_run(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A curated suite: test_lookahead.py + test_lookahead_barrier.py
        # both match `test_lookahead*.py` AND import the module -> both
        # returned, sorted.
        monkeypatch.setattr(gate, "ROOT", tmp_path)
        a = self._make(tmp_path, "tests/unit/test_lookahead.py", "import brutex.feed.lookahead\n")
        b = self._make(
            tmp_path,
            "tests/unit/test_lookahead_barrier.py",
            "from brutex.feed.lookahead import Barrier\n",
        )
        out = gate.discover_tests_for_module("brutex/feed/lookahead.py", [a, b])
        assert out == [
            "tests/unit/test_lookahead.py",
            "tests/unit/test_lookahead_barrier.py",
        ]


class TestAllTestFiles:
    """``all_test_files``: globs the real tree, sorted, and finds itself."""

    def test_returns_sorted_existing_tests(self, gate: ModuleType) -> None:
        files = gate.all_test_files()
        assert files == sorted(files)
        names = {p.name for p in files}
        assert "test_coverage_gate_discovery.py" in names


class TestBuildPlan:
    """``build_plan``: partition touched modules into entries/missing/skipped."""

    def test_partitions_by_test_and_status(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            gate,
            "_git_diff_name_status",
            lambda _base: [
                ("M", "brutex/has_test.py"),
                ("A", "brutex/added_no_test.py"),
                ("M", "brutex/modified_no_test.py"),
            ],
        )
        monkeypatch.setattr(
            gate,
            "all_test_files",
            lambda: [gate.ROOT / "tests" / "unit" / "test_has_test.py"],
        )
        monkeypatch.setattr(
            gate,
            "discover_tests_for_module",
            lambda module, _files: (
                ["tests/unit/test_has_test.py"] if module == "brutex/has_test.py" else []
            ),
        )
        plan = gate.build_plan("origin/main")
        assert plan.entries == [("brutex/has_test.py", ["tests/unit/test_has_test.py"])]
        assert plan.missing_added == ["brutex/added_no_test.py"]
        assert plan.skipped_modified == ["brutex/modified_no_test.py"]


class TestPlan:
    """``Plan``: the partitioned-result holder + ``is_empty``."""

    def test_is_empty_true_when_all_empty(self, gate: ModuleType) -> None:
        assert gate.Plan([], [], []).is_empty() is True

    def test_is_empty_false_with_entries(self, gate: ModuleType) -> None:
        assert gate.Plan([("brutex/a.py", ["t"])], [], []).is_empty() is False

    def test_is_empty_false_with_missing(self, gate: ModuleType) -> None:
        assert gate.Plan([], ["brutex/a.py"], []).is_empty() is False

    def test_is_empty_false_with_skipped(self, gate: ModuleType) -> None:
        assert gate.Plan([], [], ["brutex/a.py"]).is_empty() is False


class TestRunCoverage:
    """``_run_coverage``: every outcome with subprocess.run faked."""

    def _patch_runs(
        self,
        gate: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        results: list[SimpleNamespace],
    ) -> list[list[str]]:
        """Make subprocess.run return *results* in order; record argvs."""
        calls: list[list[str]] = []
        seq = iter(results)

        def fake_run(cmd: list[str], **_k: Any) -> SimpleNamespace:
            calls.append(cmd)
            return next(seq)

        monkeypatch.setattr(gate.subprocess, "run", fake_run)
        return calls

    def test_module_passes(self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_runs(
            gate,
            monkeypatch,
            [
                _completed(returncode=0),  # pytest
                _completed(returncode=0, stdout="brutex/data/x.py 10 0 100%"),  # measured?
                _completed(returncode=0),  # report --fail-under=100
            ],
        )
        assert gate._run_coverage("brutex/data/x.py", ["tests/unit/test_data_x.py"]) is True
        # The pytest invocation uses the numpy-safe --cov=brutex form.
        assert "--cov=brutex" in calls[0]

    def test_pytest_error_fails(self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_runs(gate, monkeypatch, [_completed(returncode=1)])
        assert gate._run_coverage("brutex/x.py", ["tests/unit/test_x.py"]) is False

    def test_not_measured_fails(self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_runs(
            gate,
            monkeypatch,
            [
                _completed(returncode=0),
                _completed(returncode=0, stdout="some other file only"),  # path absent
            ],
        )
        assert gate._run_coverage("brutex/x.py", ["tests/unit/test_x.py"]) is False

    def test_below_100_fails(self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_runs(
            gate,
            monkeypatch,
            [
                _completed(returncode=0),
                _completed(returncode=0, stdout="brutex/x.py 10 2 80%"),
                _completed(returncode=2),  # report --fail-under=100 -> non-zero
            ],
        )
        assert gate._run_coverage("brutex/x.py", ["tests/unit/test_x.py"]) is False


class TestPrintPlan:
    """``_print_plan``: renders every section."""

    def test_prints_all_three_sections(
        self, gate: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = gate.Plan(
            [("brutex/a.py", ["tests/unit/test_a.py"])],
            ["brutex/added.py"],
            ["brutex/modified.py"],
        )
        gate._print_plan(plan)
        out = capsys.readouterr().out
        assert "brutex/a.py -> tests/unit/test_a.py" in out
        assert "NEW modules with NO import-verified test" in out
        assert "brutex/added.py" in out
        assert "WARN + skip" in out
        assert "brutex/modified.py" in out

    def test_omits_empty_sections(
        self, gate: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gate._print_plan(gate.Plan([("brutex/a.py", ["t"])], [], []))
        out = capsys.readouterr().out
        assert "NEW modules with NO" not in out
        assert "WARN + skip" not in out


class TestMainCli:
    """The ``main`` CLI contract: exit codes + every output branch."""

    def test_no_touched_modules_passes(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(gate, "build_plan", lambda _b: gate.Plan([], [], []))
        assert gate.main([]) == 0
        assert "no touched brutex modules" in capsys.readouterr().out

    def test_list_mode_prints_plan_and_exits_zero(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = gate.Plan(
            [("brutex/a.py", ["tests/unit/test_a.py"])],
            ["brutex/added.py"],
            [],
        )
        monkeypatch.setattr(gate, "build_plan", lambda _b: plan)
        # --list short-circuits BEFORE the missing-test FAIL even though a
        # module is missing.
        assert gate.main(["--list"]) == 0
        out = capsys.readouterr().out
        assert "brutex/a.py -> tests/unit/test_a.py" in out
        assert "NEW modules with NO import-verified test" in out

    def test_added_missing_test_fails(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # B1 regression (the FAIL half): an ADDED module with no test FAILS.
        plan = gate.Plan([], ["brutex/no_test.py"], [])
        monkeypatch.setattr(gate, "build_plan", lambda _b: plan)
        assert gate.main([]) == 1
        out = capsys.readouterr().out
        assert "a NEW module must ship a dedicated test" in out
        assert "brutex/no_test.py" in out

    def test_modified_no_test_warn_skips_passes(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # B1 regression (the WARN-SKIP half): a MODIFIED module with no
        # dedicated test does NOT fail — it is warned and skipped (exit 0).
        plan = gate.Plan([], [], ["brutex/legacy.py"])
        monkeypatch.setattr(gate, "build_plan", lambda _b: plan)
        # Guard: _run_coverage must NOT be invoked for a skipped module.
        monkeypatch.setattr(
            gate,
            "_run_coverage",
            lambda _m, _t: pytest.fail("coverage must not run for a skipped modification"),
        )
        assert gate.main([]) == 0
        out = capsys.readouterr().out
        assert "WARN + skip" in out
        assert "brutex/legacy.py" in out

    def test_below_100_module_fails(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = gate.Plan([("brutex/a.py", ["tests/unit/test_a.py"])], [], [])
        monkeypatch.setattr(gate, "build_plan", lambda _b: plan)
        monkeypatch.setattr(gate, "_run_coverage", lambda _m, _t: False)
        assert gate.main([]) == 1
        assert "coverage-gate FAILURES" in capsys.readouterr().out

    def test_all_pass_returns_zero(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan = gate.Plan([("brutex/a.py", ["tests/unit/test_a.py"])], [], [])
        monkeypatch.setattr(gate, "build_plan", lambda _b: plan)
        monkeypatch.setattr(gate, "_run_coverage", lambda _m, _t: True)
        assert gate.main([]) == 0
        assert "discovery OK: 1 touched module(s) at 100%" in capsys.readouterr().out

    def test_discovery_error_exits_two(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def boom(_b: str) -> None:
            raise gate.DiscoveryError("git is unavailable")

        monkeypatch.setattr(gate, "build_plan", boom)
        assert gate.main([]) == 2
        assert "git is unavailable" in capsys.readouterr().out

    def test_base_flag_is_threaded_through(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def fake_build_plan(base: str) -> Any:
            seen.append(base)
            return gate.Plan([], [], [])

        monkeypatch.setattr(gate, "build_plan", fake_build_plan)
        assert gate.main(["--base", "upstream/dev"]) == 0
        assert seen == ["upstream/dev"]


def test_script_is_executable_module_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the file as ``__main__`` invokes ``main`` and exits its code.

    Exercises the ``if __name__ == "__main__":`` guard by executing the
    source under a patched ``subprocess.run`` (and ``--list``) so no real
    git/pytest runs.
    """
    source = _SCRIPT.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {"__name__": "__main__", "__file__": str(_SCRIPT)}

    def fake_run(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["coverage_gate_discovery.py", "--list"])
    with pytest.raises(SystemExit) as excinfo:
        exec(compile(source, str(_SCRIPT), "exec"), namespace)  # noqa: S102
    assert excinfo.value.code == 0
