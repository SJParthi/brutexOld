"""Functional pins for the citation-accuracy scanner (CI gate, row 17 upgrade).

DEC-TEST-APPARATUS-DEBTS-001(d): these tests invoke the REAL
``scripts/ci/validate_citation_accuracy.py`` as a subprocess against
tmp_path fixture git repos (the ``test_ci_no_hallucination_scanner.py``
harness pattern) — scripts/ci/ is outside the ``brutex`` coverage scope, so
the subprocess pins ARE the enforcement, matching every sibling scanner.

Covered contract: Tier 1 (file resolves — root-relative, unique suffix,
``...`` glob abbreviation), Tier 2 (every comma-separated line segment in
range), Tier 3 (anchored symbol within ±5, mechanical "found at :NNN" hint),
MIGRATION #NNN ledger refs via the imported HEADER_RE (plus the
digit-initial DEC-ID number fallback), CLAUDE.md ledger-mirror + docs/adr/
Tier-3 exclusions, fenced-block immunity, the escape env, and the error exit.

The final test runs the scanner over the REAL repo docs and asserts exit 0 —
the permanent anti-re-drift pin on the #609/#669/#846 recurrence class.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "validate_citation_accuracy.py"

#: A 20-line target module: ``CEILING_RPS`` on line 3, ``DEEP_FLOOR_RPS`` on
#: line 20 — far enough apart that the ±5 anchor slack cannot bridge them.
_CONSTANTS_PY = (
    '"""Fixture constants."""\n'
    "\n"
    "CEILING_RPS = 5.0\n" + "# padding\n" * 16 + "DEEP_FLOOR_RPS = 1.0\n"
)

_MIGRATION_MD = (
    "# Ledger\n"
    "\n"
    "613a. **DEC-FOO-001 — first holder of the renumbered 613.**\n"
    "613b. **DEC-BAR-001 — second holder.**\n"
    "853. **DEC-1MINUTE-ONLY-DERIVE-1DAY-001 — digit-initial DEC-ID entry.**\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a git fixture repo (files staged so ``git ls-files`` sees them)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    return repo


def _run(
    cwd: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("CITATION_ACCURACY_ALLOW_DRIFT", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )


class TestTierOneResolution:
    def test_accurate_root_relative_cite_passes(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": (
                    "# Doc\n\nThe ceiling is `CEILING_RPS` at `brutex/core/constants.py:3`.\n"
                ),
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "anchored-pass" in result.stdout

    def test_unique_suffix_resolves(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": ("# Doc\n\nThe ceiling is `CEILING_RPS` at `core/constants.py:3`.\n"),
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_ambiguous_suffix_fails_loudly(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "a/dup.py": "x = 1\n",
                "b/dup.py": "x = 1\n",
                "CLAUDE.md": "# Doc\n\nSee `dup.py:1` for details.\n",
            },
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "AMBIGUOUS" in result.stderr
        assert "a/dup.py" in result.stderr
        assert "b/dup.py" in result.stderr

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {"CLAUDE.md": "# Doc\n\nSee `brutex/nope/gone.py:1`.\n"},
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "file not found" in result.stderr

    def test_abbreviated_ellipsis_path_glob_resolves(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "docs/adr/0009-dhan-to-groww-migration.md": "# ADR\ncontent\nmore\n",
                "CLAUDE.md": "# Doc\n\nPer `adr/0009-...-migration.md:2` records.\n",
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 abbreviated path(s) glob-resolved" in result.stdout


class TestPathContainment:
    """Cited paths must resolve INSIDE the repo root (traversal regression).

    CITATION_RE's charset admits ``.`` and ``/``, so ``../outside.py:1`` and
    an absolute ``/abs/path.py:1`` (pathlib lets a leading ``/`` OVERRIDE
    ``root /``) used to resolve, be READ, and pass as "accurate" — a
    non-repo file blessed by the gate whose whole purpose is catching
    citations that don't trace to the repo, plus an out-of-tree file read.
    """

    def test_parent_traversal_cite_fails_loudly(self, tmp_path: Path) -> None:
        # outside.py exists ONE LEVEL ABOVE the fixture repo root.
        (tmp_path / "outside.py").write_text("SECRET_TOKEN = 1\n", encoding="utf-8")
        repo = _make_repo(
            tmp_path,
            {"CLAUDE.md": "# Doc\n\nSee `../outside.py:1` for details.\n"},
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "OUTSIDE the repository root" in result.stderr

    def test_absolute_path_cite_fails_loudly(self, tmp_path: Path) -> None:
        outside = tmp_path / "abs_target.py"
        outside.write_text("SECRET_TOKEN = 1\n", encoding="utf-8")
        repo = _make_repo(
            tmp_path,
            {"CLAUDE.md": f"# Doc\n\nSee `{outside}:1` for details.\n"},
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "OUTSIDE the repository root" in result.stderr

    def test_in_root_untracked_file_still_resolves(self, tmp_path: Path) -> None:
        # The containment gate rejects OUT-of-root only: an in-root file not
        # yet staged (e.g. cited by the same PR that adds it) keeps resolving.
        repo = _make_repo(
            tmp_path,
            {"CLAUDE.md": "# Doc\n\nSee `notes/new_file.py:1` for details.\n"},
        )
        untracked = repo / "notes" / "new_file.py"
        untracked.parent.mkdir()
        untracked.write_text("VALUE = 1\n", encoding="utf-8")
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr


class TestTierTwoLineRanges:
    def test_past_eof_segment_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": "# Doc\n\nSee `brutex/core/constants.py:999`.\n",
            },
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "out of range" in result.stderr

    def test_comma_list_with_one_stale_segment_fails(self, tmp_path: Path) -> None:
        # The MF-2 fold: `constants.py:3,999` — the first segment is fine,
        # the ,999 leg is past EOF and must be validated, never dropped.
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": ("# Doc\n\n`CEILING_RPS` at `brutex/core/constants.py:3,999`.\n"),
            },
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "999" in result.stderr
        assert "out of range" in result.stderr


class TestTierThreeAnchors:
    def test_drifted_cite_fails_naming_found_at_line(self, tmp_path: Path) -> None:
        # DEEP_FLOOR_RPS lives at :20; citing :3 is > ±5 away — the failure
        # must print the ACTUAL location so the fix is mechanical.
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": (
                    "# Doc\n\nThe floor is `DEEP_FLOOR_RPS` at `brutex/core/constants.py:3`.\n"
                ),
            },
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "DEEP_FLOOR_RPS at :20" in result.stderr
        assert "update the citation" in result.stderr

    def test_comma_list_anchor_near_any_segment_passes(self, tmp_path: Path) -> None:
        # Tier 3 over a comma list: the anchor sits near the SECOND segment.
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": (
                    "# Doc\n\nThe floor is `DEEP_FLOOR_RPS` at `brutex/core/constants.py:3,20`.\n"
                ),
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_unanchored_cite_is_counted_not_failed(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": "# Doc\n\nSee brutex/core/constants.py:3 for details.\n",
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 unanchored" in result.stdout

    def test_anchor_absent_from_file_is_counted_not_failed(self, tmp_path: Path) -> None:
        # Multi-citation contexts make the anchor<->file assignment ambiguous;
        # a nowhere-in-file anchor has no "found at" line to fix toward
        # (documented residual limit, module docstring).
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": (
                    "# Doc\n\nSee `TOTALLY_ELSEWHERE_TOKEN` per `brutex/core/constants.py:3`.\n"
                ),
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 anchor-not-in-file" in result.stdout

    def test_fenced_block_cites_are_ignored(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "CLAUDE.md": (
                    "# Doc\n\n```\nbrutex/nope/gone.py:1\n"
                    "MIGRATION.md #99999\n```\n\nProse without citations.\n"
                ),
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "0 citation(s)" in result.stdout


class TestLedgerRefs:
    def test_bare_number_resolves_suffixed_forms(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "docs/MIGRATION.md": _MIGRATION_MD,
                "CLAUDE.md": "# Doc\n\nPer MIGRATION.md entry #613.\n",
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_digit_initial_dec_id_entry_number_resolves(self, tmp_path: Path) -> None:
        # Regression pin for the number-harvest fallback: entry 853 heads a
        # DEC-ID starting with a digit (DEC-1MINUTE-...), which the imported
        # HEADER_RE alone does not parse — #853 must still resolve.
        repo = _make_repo(
            tmp_path,
            {
                "docs/MIGRATION.md": _MIGRATION_MD,
                "CLAUDE.md": "# Doc\n\nPer MIGRATION.md #853.\n",
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_missing_entry_number_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {
                "docs/MIGRATION.md": _MIGRATION_MD,
                "CLAUDE.md": "# Doc\n\nPer MIGRATION.md entry #999.\n",
            },
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "#999" in result.stderr
        assert "no ledger entry" in result.stderr

    def test_ref_without_ledger_file_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {"CLAUDE.md": "# Doc\n\nPer MIGRATION.md #613.\n"},
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "was not found" in result.stderr


class TestTierThreeExclusions:
    _MIRROR_CLAUDE_MD = (
        "# BruteX\n"
        "\n"
        "_Last updated: 2026-07-06 (`DEEP_FLOOR_RPS` per\n"
        "`brutex/core/constants.py:3` — deliberately point-in-time).\n"
        "\n"
        "## 1. Project Identity\n"
        "\n"
        "- **Name:** BruteX\n"
        "- **Active branch:** `claude/current` (current blurb).\n"
        "  Earlier: `claude/old` (`DEEP_FLOOR_RPS` at `brutex/core/constants.py:3`).\n"
        "\n"
        "## 2. Golden Rules\n"
        "\n"
        "Normative prose without citations.\n"
    )

    def test_mirror_regions_skip_tier3(self, tmp_path: Path) -> None:
        # Both cites are Tier-3-drifted (DEEP_FLOOR_RPS is at :20, cited :3)
        # but sit in the header blurb / §1 Earlier: mirror regions — skipped.
        repo = _make_repo(
            tmp_path,
            {
                "brutex/core/constants.py": _CONSTANTS_PY,
                "CLAUDE.md": self._MIRROR_CLAUDE_MD,
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 mirror-region Tier-3 skips" in result.stdout

    def test_same_drift_outside_mirror_regions_fails(self, tmp_path: Path) -> None:
        # Teeth twin of the skip test: the identical drifted cite placed in
        # a NORMATIVE section fails — the exclusion is region-scoped, not a
        # blanket CLAUDE.md pass.
        doc = self._MIRROR_CLAUDE_MD + (
            "\nThe floor is `DEEP_FLOOR_RPS` at `brutex/core/constants.py:3`.\n"
        )
        repo = _make_repo(
            tmp_path,
            {"brutex/core/constants.py": _CONSTANTS_PY, "CLAUDE.md": doc},
        )
        result = _run(repo)
        assert result.returncode == 1
        assert "DEEP_FLOOR_RPS at :20" in result.stderr

    def test_tier1_still_applies_inside_mirror_region(self, tmp_path: Path) -> None:
        # The exclusion is Tier-3 ONLY: a mirror-region cite to a file that
        # does not exist at all still fails (spec: Tier 1/2 stay live).
        doc = (
            "# BruteX\n\n_Last updated: 2026-07-06 (see `brutex/gone.py:1`).\n\n"
            "## 1. Project Identity\n\n- **Name:** BruteX\n"
        )
        repo = _make_repo(tmp_path, {"CLAUDE.md": doc})
        result = _run(repo)
        assert result.returncode == 1
        assert "file not found" in result.stderr

    def test_adr_target_skips_tier3(self, tmp_path: Path) -> None:
        # docs/adr/ targets are immutable records — Tier 3 skipped even
        # though the anchor is nowhere near (Tier 1/2 still ran: :2 is in
        # range).
        repo = _make_repo(
            tmp_path,
            {
                "docs/adr/0001-example.md": "# ADR-0001\nAccepted.\nContext.\n",
                "CLAUDE.md": (
                    "# Doc\n\nPer `SOME_DECISION_TOKEN` in `docs/adr/0001-example.md:2`.\n"
                ),
            },
        )
        result = _run(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 ADR-target Tier-3 skips" in result.stdout


class TestEscapeAndErrors:
    def test_escape_env_downgrades_to_warn(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            {"CLAUDE.md": "# Doc\n\nSee `brutex/nope/gone.py:1`.\n"},
        )
        result = _run(repo, extra_env={"CITATION_ACCURACY_ALLOW_DRIFT": "1"})
        assert result.returncode == 0
        assert "override active" in result.stderr
        # The finding is still PRINTED (warn-only, never silent).
        assert "file not found" in result.stderr

    def test_unreadable_extra_document_is_script_error(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, {"CLAUDE.md": "# Doc\n\nclean.\n"})
        result = _run(repo, str(tmp_path / "does-not-exist.md"))
        assert result.returncode == 2
        assert "not readable" in result.stderr

    def test_outside_a_git_repo_is_script_error(self, tmp_path: Path) -> None:
        bare_dir = tmp_path / "not-a-repo"
        bare_dir.mkdir()
        result = _run(bare_dir)
        assert result.returncode == 2
        assert "git repo discovery failed" in result.stderr

    def test_extra_argv_document_is_scanned(self, tmp_path: Path) -> None:
        # The PR-body use case: an extra doc with a drifted cite fails even
        # though the in-repo doc set is clean.
        repo = _make_repo(tmp_path, {"CLAUDE.md": "# Doc\n\nclean.\n"})
        body = tmp_path / "pr-body.md"
        body.write_text("See `brutex/nope/gone.py:1`.\n", encoding="utf-8")
        result = _run(repo, str(body))
        assert result.returncode == 1
        assert "file not found" in result.stderr


class TestRuntimeArchComboHashClaim:
    """Regression pin for the scanner-blessed FALSE-prose class (defect D4).

    Tier 3 is an any-anchor × any-segment disjunction (see the scanner's
    honest-envelope docstring), so a citation refresh can "bless" false
    PROSE around a correct line range — exactly what happened when
    ``docs/TRACK1_RUNTIME_ARCHITECTURE.md`` §3.8 re-pointed its citation at
    the RESOLVED 9-field ``combo_hash`` while keeping the stale "uses only
    4 inputs" claim (the ``strategy_id`` anchor landed inside ±5 of the
    refreshed range, so the scan stayed green). The scanner cannot validate
    prose; these pins do — for this specific recurrence class.

    The signature check parses the source with ``ast`` (never imports) so
    the pin runs without the ``fno_math`` Rust wheel installed.
    """

    _DOC = _REPO_ROOT / "docs" / "TRACK1_RUNTIME_ARCHITECTURE.md"
    _COMBO = _REPO_ROOT / "brutex" / "strategies" / "combo.py"

    #: The 9 identity fields per spec §14 / CLAUDE.md Golden Rule #4.
    _NINE_FIELDS: ClassVar[list[str]] = [
        "strategy_id",
        "direction",
        "instrument",
        "tf",
        "mode",
        "params",
        "data_snapshot_hash",
        "indicator_version",
        "code_commit_sha",
    ]

    def test_stale_four_input_claim_is_gone(self) -> None:
        text = self._DOC.read_text(encoding="utf-8")
        assert "uses only 4 inputs" not in text, (
            "TRACK1_RUNTIME_ARCHITECTURE.md re-asserts the pre-DEC-P0-060 "
            "4-input combo_hash claim — C-04 is RESOLVED (MIGRATION.md "
            "entry #197); fix the prose, do not re-point the citation."
        )
        # The §8 cross-ref row must not present-tense the resolved drift.
        assert "(code 4 inputs)" not in text

    def test_resolved_claim_tracks_the_real_signature(self) -> None:
        """The doc claims 9 fields; the code must actually take exactly them."""
        tree = ast.parse(self._COMBO.read_text(encoding="utf-8"))
        fns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "combo_hash"
        ]
        assert len(fns) == 1
        (fn,) = fns
        assert fn.args.args == [], "combo_hash fields must be keyword-only"
        assert [a.arg for a in fn.args.kwonlyargs] == self._NINE_FIELDS
        # And the doc's §3.8 resolution note names the closing decision.
        text = self._DOC.read_text(encoding="utf-8")
        assert "DEC-P0-060" in text


class TestRealRepoFixpoint:
    def test_real_repo_docs_are_citation_accurate(self) -> None:
        """The permanent anti-re-drift pin on the #609/#669/#846 class.

        Runs the scanner over the ACTUAL checked-out normative docs
        (CLAUDE.md + docs/TRACK1_*.md + docs/STANDARDS_100PCT.md) and
        requires exit 0. Red here means a doc citation drifted from the
        code — fix the citation using the scanner's own "found at :NNN"
        output; never weaken the scanner.
        """
        result = _run(_REPO_ROOT)
        assert result.returncode == 0, (
            "citation drift in the normative docs:\n" + result.stdout + result.stderr
        )
