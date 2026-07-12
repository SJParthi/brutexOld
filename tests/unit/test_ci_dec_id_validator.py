"""Regression tests for the DEC-ID PR-body validator (CI gate, row 18).

DEC-TEXTSCAN-COMMON-001: ``scripts/ci/validate_dec_ids.py`` had no
dedicated test file — its behaviour was only exercised indirectly by
live PR bodies. These tests invoke the REAL script as a subprocess
(exactly as the CI job does, and exactly like the sibling
``test_ci_no_hallucination_scanner.py``) and pin both directions plus
the code-span exclusions, so the consolidation of the span-stripping
helpers into ``scripts/ci/_textscan.py`` is regression-locked.

DEC-TEST-APPARATUS-DEBTS-001(e): the APPROPRIATENESS extension —
bot-exempt × behavioral-surface files × non-{docs,chore,test,style,ci}
title × zero resolving citation AND no MIGRATION.md touch → block —
is pinned below via the PR_FILES/PR_TITLE/PR_AUTHOR env triple,
including the DOCUMENTED ``docs:``-title bypass envelope (pinned as a
test so it can never silently change) and the ``--sweep-open-prs``
helper (driven against a stubbed ``gh`` on PATH).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "validate_dec_ids.py"

# Env vars the validator consumes — stripped from the ambient env so the
# harness is hermetic (a CI job exporting PR_TITLE could otherwise flip
# the pre-existing existence-only cases into appropriateness mode).
_VALIDATOR_ENV_VARS = (
    "PR_TITLE",
    "PR_FILES",
    "PR_AUTHOR",
    "PR_AUTHOR_EXEMPT_PATTERN",
    "DEC_ID_VALIDATOR_ALLOW_MISSING",
    "DEC_ID_APPROPRIATENESS_ALLOW_MISSING",
)


def _run(
    body: str,
    tmp_path: Path,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real validator over *body* with a hermetic env; return the result."""
    body_file = tmp_path / "pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in _VALIDATOR_ENV_VARS}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(body_file)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )


def _scan(body: str, tmp_path: Path) -> int:
    """Run the real validator over *body* and return its exit code."""
    return _run(body, tmp_path).returncode


def test_real_dec_id_resolves(tmp_path: Path) -> None:
    """A DEC-ID with a ledger entry passes."""
    assert _scan("Per DEC-COV-001 the coverage arc is tracked.", tmp_path) == 0


def test_bogus_dec_id_is_blocked(tmp_path: Path) -> None:
    """A DEC-ID without a ledger entry fails closed."""
    assert _scan("Per DEC-NONEXISTENT-999 this should block.", tmp_path) == 1


def test_plus_joined_multi_dec_header_member_resolves(tmp_path: Path) -> None:
    """A NON-FIRST member of a plus-joined multi-DEC entry header resolves.

    MIGRATION.md #1759 is the one-line/multi-DEC umbrella shape ("NNN. **DEC-A
    + DEC-B + DEC-C — …", the #300/#1297/#1529 precedent): the entry IS each
    member's ledger record, so citing the second/third member must pass — the
    pre-fix header regex admitted only the FIRST ID of the chain.
    """
    body = (
        "Per DEC-DOCTOR-RATE-PROBE-SINGLEFLIGHT-001 and "
        "DEC-SWEEP-BLOOM-EXACT-BOUNDARY-001 the locks/boundary behavior is ledgered."
    )
    assert _scan(body, tmp_path) == 0


def test_plus_joined_chain_stays_conservative(tmp_path: Path) -> None:
    """The plus-chain extension admits ONLY the leading chain, never body text.

    A synthetic ledger proves both directions against the REAL script: chain
    members resolve; an ID merely MENTIONED after the title separator (or
    plus-joined to a bogus continuation) still fails closed.
    """
    ledger = tmp_path / "MIGRATION.md"
    ledger.write_text(
        "1. **DEC-ALPHA-001 + DEC-BETA-001 — a pack that supersedes "
        "DEC-GAMMA-001 in passing.** Body mentioning DEC-DELTA-001.\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(_SCRIPT.parent))
    try:
        import importlib

        mod = importlib.import_module("validate_dec_ids")
        known = mod.load_known_dec_ids(ledger.read_text(encoding="utf-8"))
    finally:
        sys.path.remove(str(_SCRIPT.parent))
    assert known == {"DEC-ALPHA-001", "DEC-BETA-001"}  # gamma/delta NOT admitted


def test_inline_code_dec_id_is_ignored(tmp_path: Path) -> None:
    """An example ID inside inline backticks is documentation, not a citation."""
    assert _scan("The validator rejects e.g. `DEC-NONEXISTENT-999`.", tmp_path) == 0


def test_fenced_block_dec_id_is_ignored(tmp_path: Path) -> None:
    """An example ID inside a fenced block is documentation, not a citation."""
    body = "Sample output:\n\n```\nmissing: DEC-NONEXISTENT-999\n```\n"
    assert _scan(body, tmp_path) == 0


def test_body_without_dec_ids_passes(tmp_path: Path) -> None:
    """A body citing nothing has nothing to validate."""
    assert _scan("Pure internal refactor of the results store.", tmp_path) == 0


# ─────────────────────────────────────────────────────────────────────
# APPROPRIATENESS mode (DEC-TEST-APPARATUS-DEBTS-001(e)) — driven by
# the PR_FILES / PR_TITLE / PR_AUTHOR env triple.
# ─────────────────────────────────────────────────────────────────────

_HUMAN = "SJParthi"


def _appropriateness_env(
    *,
    title: str,
    files: str,
    author: str = _HUMAN,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {"PR_TITLE": title, "PR_FILES": files, "PR_AUTHOR": author}
    if extra:
        env.update(extra)
    return env


def test_source_touching_feat_without_citation_blocks(tmp_path: Path) -> None:
    """A feat: PR touching brutex source with no citation fails closed."""
    result = _run(
        "No decision cited anywhere in this body.",
        tmp_path,
        _appropriateness_env(title="feat(data): add widget", files="brutex/data/widget.py"),
    )
    assert result.returncode == 1
    assert "MERGE BLOCKED" in result.stderr
    assert "brutex/data/widget.py" in result.stderr


def test_source_touching_feat_with_resolving_citation_passes(tmp_path: Path) -> None:
    """The same PR carrying a ledger-resolving DEC-ID passes."""
    result = _run(
        "Executes DEC-COV-001.",
        tmp_path,
        _appropriateness_env(title="feat(data): add widget", files="brutex/data/widget.py"),
    )
    assert result.returncode == 0


def test_source_touching_feat_with_migration_touch_passes(tmp_path: Path) -> None:
    """Touching docs/MIGRATION.md satisfies the citation-OR-ledger-touch predicate."""
    result = _run(
        "New decision entry added in this PR.",
        tmp_path,
        _appropriateness_env(
            title="feat(data): add widget",
            files="brutex/data/widget.py\ndocs/MIGRATION.md",
        ),
    )
    assert result.returncode == 0


def test_tests_only_diff_passes(tmp_path: Path) -> None:
    """A tests-only diff is never on the behavioral surface."""
    result = _run(
        "No citation.",
        tmp_path,
        _appropriateness_env(title="feat(tests): tighten pins", files="tests/unit/test_widget.py"),
    )
    assert result.returncode == 0


def test_docs_title_bypass_envelope_is_pinned(tmp_path: Path) -> None:
    """DOCUMENTED bypass: a docs:-titled PR touching brutex source passes.

    This is the deliberate title-only trade-off (the sibling scanner's
    post-PR-#120 stance) pinned as a test so the envelope can never
    silently change — widening OR narrowing it must edit this pin.
    """
    result = _run(
        "No citation.",
        tmp_path,
        _appropriateness_env(title="docs(data): fix docstring typo", files="brutex/data/widget.py"),
    )
    assert result.returncode == 0


def test_ci_title_is_exempt(tmp_path: Path) -> None:
    """ci: is in the exempt title-type set."""
    result = _run(
        "No citation.",
        tmp_path,
        _appropriateness_env(title="ci: tune a workflow", files=".github/workflows/ci.yml"),
    )
    assert result.returncode == 0


def test_bot_author_is_exempt(tmp_path: Path) -> None:
    """A dependabot-authored source-touching PR is exempt."""
    result = _run(
        "No citation.",
        tmp_path,
        _appropriateness_env(
            title="feat(deps): bump something",
            files="brutex/data/widget.py",
            author="dependabot[bot]",
        ),
    )
    assert result.returncode == 0


def test_missing_pr_files_skips_with_note(tmp_path: Path) -> None:
    """PR_FILES unset → appropriateness skips (exit 0) with an ℹ️ note."""
    result = _run("No citation.", tmp_path, {"PR_TITLE": "feat(data): add widget"})
    assert result.returncode == 0
    assert "skipping DEC-ID appropriateness" in result.stderr


def test_appropriateness_escape_env_warns_and_passes(tmp_path: Path) -> None:
    """DEC_ID_APPROPRIATENESS_ALLOW_MISSING=1 → warn-and-continue."""
    result = _run(
        "No citation.",
        tmp_path,
        _appropriateness_env(
            title="feat(data): add widget",
            files="brutex/data/widget.py",
            extra={"DEC_ID_APPROPRIATENESS_ALLOW_MISSING": "1"},
        ),
    )
    assert result.returncode == 0
    assert "not failing" in result.stderr


def test_template_stub_dec_dots_does_not_count_as_citation(tmp_path: Path) -> None:
    """The unfilled template's literal ``DEC-...`` stub is no citation → block."""
    result = _run(
        "Decision ID: DEC-...",
        tmp_path,
        _appropriateness_env(title="feat(data): add widget", files="brutex/data/widget.py"),
    )
    assert result.returncode == 1
    assert "MERGE BLOCKED" in result.stderr


def test_workflows_only_diff_with_feat_title_blocks(tmp_path: Path) -> None:
    """.github/workflows/** is behavioral surface."""
    result = _run(
        "No citation.",
        tmp_path,
        _appropriateness_env(title="feat(ci): new gate job", files=".github/workflows/ci.yml"),
    )
    assert result.returncode == 1


def test_rust_only_diff_with_fix_title_blocks(tmp_path: Path) -> None:
    """rust/**/*.rs is behavioral surface."""
    result = _run(
        "No citation.",
        tmp_path,
        _appropriateness_env(
            title="fix(core): kernel off-by-one",
            files="rust/trading-core/src/indicators/pivots.rs",
        ),
    )
    assert result.returncode == 1


def test_nonconventional_title_is_fail_closed(tmp_path: Path) -> None:
    """A title with no parseable conventional-commit type is NON-exempt."""
    result = _run(
        "No citation.",
        tmp_path,
        _appropriateness_env(title="Update the widget module", files="brutex/data/widget.py"),
    )
    assert result.returncode == 1
    assert "unparseable" in result.stderr


def test_bogus_citation_still_blocks_before_appropriateness(tmp_path: Path) -> None:
    """A non-resolving DEC-ID fails the existence check even in env-triple mode."""
    result = _run(
        "Per DEC-NONEXISTENT-999.",
        tmp_path,
        _appropriateness_env(title="feat(data): add widget", files="brutex/data/widget.py"),
    )
    assert result.returncode == 1
    assert "no matching ledger entry" in result.stderr


# ─────────────────────────────────────────────────────────────────────
# --sweep-open-prs helper — driven against a stubbed ``gh`` on PATH.
# ─────────────────────────────────────────────────────────────────────


def _run_sweep(tmp_path: Path, gh_script: str) -> subprocess.CompletedProcess[str]:
    """Run ``--sweep-open-prs`` with a stub ``gh`` prepended to PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(gh_script, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {k: v for k, v in os.environ.items() if k not in _VALIDATOR_ENV_VARS}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--sweep-open-prs"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )


def _gh_stub_emitting(json_payload: str) -> str:
    return f"#!/bin/sh\ncat <<'JSON'\n{json_payload}\nJSON\n"


def test_sweep_clean_open_prs_pass(tmp_path: Path) -> None:
    """A docs-titled PR and a citing PR both pass the sweep."""
    payload = (
        '[{"number": 7, "title": "docs: refresh runbook", "body": "",'
        ' "files": [{"path": "docs/runbooks/x.md"}], "author": {"login": "SJParthi"}},'
        ' {"number": 8, "title": "feat(data): widget", "body": "Executes DEC-COV-001.",'
        ' "files": [{"path": "brutex/data/widget.py"}], "author": {"login": "SJParthi"}}]'
    )
    result = _run_sweep(tmp_path, _gh_stub_emitting(payload))
    assert result.returncode == 0
    assert "sweep clean" in result.stdout


def test_sweep_reports_tripping_pr(tmp_path: Path) -> None:
    """An uncited behavioral-surface open PR trips the sweep, named by number."""
    payload = (
        '[{"number": 42, "title": "feat(data): widget", "body": "no citation",'
        ' "files": [{"path": "brutex/data/widget.py"}], "author": {"login": "SJParthi"}}]'
    )
    result = _run_sweep(tmp_path, _gh_stub_emitting(payload))
    assert result.returncode == 1
    assert "PR #42" in result.stderr


def test_sweep_reports_nonresolving_citation(tmp_path: Path) -> None:
    """An open PR citing a bogus DEC-ID trips the sweep's existence arm."""
    payload = (
        '[{"number": 43, "title": "docs: notes", "body": "Per DEC-NONEXISTENT-999.",'
        ' "files": [{"path": "docs/notes.md"}], "author": {"login": "SJParthi"}}]'
    )
    result = _run_sweep(tmp_path, _gh_stub_emitting(payload))
    assert result.returncode == 1
    assert "DEC-NONEXISTENT-999" in result.stderr


def test_sweep_excuses_nonresolving_id_when_pr_adds_its_own_ledger_entry(tmp_path: Path) -> None:
    """A PR touching docs/MIGRATION.md may cite the DEC-ID it itself adds.

    The sweep resolves against the LOCAL checkout's ledger, which
    predates the open PR's own entry — its per-PR CI run (merged tree)
    is the authoritative existence check for that ID.
    """
    payload = (
        '[{"number": 44, "title": "feat(data): widget", "body": "Per DEC-NONEXISTENT-999.",'
        ' "files": [{"path": "brutex/data/widget.py"}, {"path": "docs/MIGRATION.md"}],'
        ' "author": {"login": "SJParthi"}}]'
    )
    result = _run_sweep(tmp_path, _gh_stub_emitting(payload))
    assert result.returncode == 0


def test_sweep_gh_failure_is_loud(tmp_path: Path) -> None:
    """gh exiting non-zero → exit 2, never a silent pass."""
    result = _run_sweep(tmp_path, "#!/bin/sh\necho 'auth required' >&2\nexit 1\n")
    assert result.returncode == 2
    assert "gh pr list exited 1" in result.stderr


def test_sweep_invalid_json_is_loud(tmp_path: Path) -> None:
    """gh emitting non-JSON → exit 2."""
    result = _run_sweep(tmp_path, "#!/bin/sh\necho 'not json'\n")
    assert result.returncode == 2
    assert "invalid JSON" in result.stderr
