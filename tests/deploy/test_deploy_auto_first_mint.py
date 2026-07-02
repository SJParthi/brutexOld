"""Tests for the ``deploy.sh`` zero-touch FIRST token mint (post-apply invoke).

DEC-DEPLOY-AUTO-FIRST-MINT-001 (docs/MIGRATION.md). Before this change the
first token appeared only at the next 06:05 IST EventBridge cron
(``cron(35 0 * * ? *)``, main.tf), so a just-deployed stack had an EMPTY
access-token param until then and the operator was told to run a manual
``aws lambda invoke`` once. ``deploy.sh`` now performs that first mint ITSELF
as its final step: after a successful ``terraform apply`` it invokes the minter
Lambda once, verifies the response payload reports the handler's ``ok: true``
status, prints an honest ``FIRST MINT: PASS``/``FAIL`` line, and exits NON-ZERO
on a mint failure so the operator SEES it. Idempotent: a re-run re-mints once
per deploy — harmless, the Lambda is the SOLE token writer and the SSM write is
last-writer-wins (``PutParameter Overwrite=True``, ADR-0014).

Static content assertions over the shell source (post-apply invoke present,
name resolution + fallback, ok-payload verification, both failure paths exit
non-zero, bash-3.2-safe constructs) plus behavioural checks of the extracted
ok-verification idiom in a real bash subshell — no network / AWS / terraform is
touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_SCRIPT = _REPO_ROOT / "deploy" / "aws_token_minter" / "deploy.sh"

#: The exact ok-verification grep the script runs over the invoke payload —
#: tolerant of JSON whitespace, strict about the value being ``true``.
_OK_PATTERN = '"ok"[[:space:]]*:[[:space:]]*true'

#: bash-4+-only constructs that would break macOS's default bash 3.2 (the
#: project's reference operator host — project rules §8).
_BASH4_ONLY_TOKENS = ("mapfile", "readarray", "declare -A", "&>>", ";;&", "local -n")


@pytest.fixture(scope="module")
def deploy_script_text() -> str:
    """Return the full source of ``deploy.sh``."""
    assert _DEPLOY_SCRIPT.is_file(), f"missing deploy script: {_DEPLOY_SCRIPT}"
    return _DEPLOY_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def first_mint_section(deploy_script_text: str) -> str:
    """Return the step-6 first-mint section of ``deploy.sh``."""
    marker = "first token mint"
    assert marker in deploy_script_text, "missing the first-token-mint step"
    return deploy_script_text[deploy_script_text.index("== 6/6") :]


def _executable_lines(script_text: str) -> list[str]:
    """Return the executable (non-comment, non-blank) lines of a shell script."""
    out: list[str] = []
    for raw in script_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(raw)
    return out


def _bash() -> str:
    """Locate a ``bash`` interpreter or skip (bash is required to reproduce)."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - CI always has bash
        pytest.skip("bash interpreter not available")
    return bash


def _grep_ok(payload: str) -> int:
    """Run the script's exact ok-verification grep over *payload*; return rc."""
    result = subprocess.run(
        [_bash(), "-c", f"printf '%s' \"$1\" | grep -Eq '{_OK_PATTERN}'", "sh", payload],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    return result.returncode


# --------------------------------------------------------------------------- #
# Static: the post-apply invoke exists and runs AFTER a successful apply.
# --------------------------------------------------------------------------- #
def test_first_mint_invoke_present(first_mint_section: str) -> None:
    """The deploy itself invokes the Lambda once (no manual invoke needed)."""
    assert "aws lambda invoke" in first_mint_section


def test_first_mint_runs_after_apply(deploy_script_text: str) -> None:
    """The invoke comes AFTER ``terraform apply`` — the success guard.

    Under ``set -euo pipefail`` a failed (or operator-declined) apply aborts the
    script BEFORE this step, so the mint runs only on a successful apply.
    """
    apply_pos = deploy_script_text.index('terraform apply "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"')
    # The EXECUTABLE invoke line (the header comment also NAMES the command).
    invoke_pos = deploy_script_text.index('INVOKE_META="$(aws lambda invoke')
    assert apply_pos < invoke_pos, "the first mint must run only after terraform apply"


def test_first_mint_resolves_name_from_output_with_fallback(first_mint_section: str) -> None:
    """The Lambda name comes from the post-apply terraform output, with fallback."""
    assert "terraform output -raw minter_lambda_name" in first_mint_section
    assert '|| echo "$LAMBDA_FUNCTION_NAME"' in first_mint_section


def test_first_mint_is_region_scoped(first_mint_section: str) -> None:
    """The invoke targets the resolved deploy region (operator -var honored)."""
    assert '--region "$REGION"' in first_mint_section


# --------------------------------------------------------------------------- #
# Static: the ok-verification + honest PASS/FAIL + non-zero exit semantics.
# --------------------------------------------------------------------------- #
def test_first_mint_verifies_ok_payload(first_mint_section: str) -> None:
    """The response payload is checked for the handler's ``ok: true`` status."""
    assert f"grep -Eq '{_OK_PATTERN}'" in first_mint_section


def test_first_mint_prints_honest_pass_line(first_mint_section: str) -> None:
    """A successful mint prints one honest PASS line carrying the payload."""
    assert "FIRST MINT: PASS" in first_mint_section


def test_first_mint_failure_paths_exit_nonzero(first_mint_section: str) -> None:
    """BOTH failure paths (invoke error, not-ok payload) print FAIL and exit 1.

    A silent mint failure would recreate the empty-token-param trap this step
    exists to close, so each FAIL branch must carry the exact error text and a
    non-zero exit.
    """
    assert first_mint_section.count("FIRST MINT: FAIL") == 2
    fail_exits = 0
    for chunk in first_mint_section.split("FIRST MINT: FAIL")[1:]:
        # Each FAIL branch must exit 1 before its fi closes.
        branch = chunk.split("fi\n", 1)[0]
        assert "exit 1" in branch, "every FIRST MINT: FAIL branch must exit non-zero"
        fail_exits += 1
    assert fail_exits == 2


def test_first_mint_surfaces_exact_error_text(first_mint_section: str) -> None:
    """Failures echo the captured invoke error / response payload verbatim."""
    assert 'echo "$INVOKE_META" >&2' in first_mint_section
    assert 'cat "$MINT_RESPONSE"' in first_mint_section


def test_first_mint_never_auto_approves_or_migrates(first_mint_section: str) -> None:
    """The mint step adds no terraform surface (no apply/approve/migrate)."""
    for line in _executable_lines(first_mint_section):
        assert "-auto-approve" not in line
        assert "-migrate-state" not in line
        assert "terraform apply" not in line


# --------------------------------------------------------------------------- #
# Static: bash 3.2 (macOS default /bin/bash) compatibility.
# --------------------------------------------------------------------------- #
def test_deploy_avoids_bash4_only_constructs(deploy_script_text: str) -> None:
    """No bash-4+-only construct anywhere in executable code (macOS bash 3.2)."""
    for line in _executable_lines(deploy_script_text):
        for token in _BASH4_ONLY_TOKENS:
            assert token not in line, f"bash-4-only construct {token!r} in: {line!r}"


def test_deploy_avoids_case_modification_expansions(deploy_script_text: str) -> None:
    """No ``${var,,}`` / ``${var^^}`` case-modification (bash 4+) in the script."""
    for line in _executable_lines(deploy_script_text):
        assert ",,}" not in line, f"bash-4-only lowercase expansion in: {line!r}"
        assert "^^}" not in line, f"bash-4-only uppercase expansion in: {line!r}"


def test_only_array_is_the_guarded_extra_args(deploy_script_text: str) -> None:
    """The ONLY array in the script stays ``EXTRA_ARGS`` (the guarded idiom).

    The new steps deliberately use plain scalars + ``case`` so no new empty-array
    ``set -u`` trap (DEC-DEPLOY-EMPTY-ARGS-FIX-001, docs/MIGRATION.md #1075) can
    ever be reintroduced.
    """
    for line in _executable_lines(deploy_script_text):
        if "=(" in line:
            assert line.strip().startswith("EXTRA_ARGS"), (
                f"unexpected array initialisation (bash-3.2 set -u trap risk): {line!r}"
            )


# --------------------------------------------------------------------------- #
# Behavioural: the extracted ok-verification idiom against real payloads.
# --------------------------------------------------------------------------- #
def test_ok_check_accepts_success_payload() -> None:
    """The handler's success response (``ok: true``) PASSES the check."""
    payload = '{"ok": true, "attempts": 1, "token_param": "/tickvault/prod/groww/access-token"}'
    assert _grep_ok(payload) == 0


def test_ok_check_accepts_compact_json() -> None:
    """A compact (no-space) serialisation of ``ok:true`` also passes."""
    assert _grep_ok('{"ok":true,"attempts":2}') == 0


def test_ok_check_rejects_lambda_error_payload() -> None:
    """An unhandled-handler-error payload (errorMessage/errorType) FAILS."""
    payload = (
        '{"errorMessage": "Groww access-token issuance failed: ***", '
        '"errorType": "TokenMintError", "stackTrace": []}'
    )
    assert _grep_ok(payload) != 0


def test_ok_check_rejects_ok_false() -> None:
    """An explicit ``ok: false`` FAILS (never a fake PASS)."""
    assert _grep_ok('{"ok": false, "attempts": 4, "token_param": "x"}') != 0


def test_ok_check_rejects_empty_payload() -> None:
    """An empty/garbage payload FAILS (never a fake PASS)."""
    assert _grep_ok("") != 0
    assert _grep_ok("null") != 0
