"""Regression tests for the ``deploy.sh`` empty-array ``EXTRA_ARGS`` bug.

DEC-DEPLOY-EMPTY-ARGS-FIX-001 (docs/MIGRATION.md #1075). The bug: under
``set -euo pipefail`` (nounset), macOS's default **bash 3.2** treats an EMPTY
array expanded as ``"${arr[@]}"`` as an UNBOUND VARIABLE and ABORTS the script
(a documented bash 3.2 behaviour fixed in bash 4.4). ``deploy.sh`` expanded
``EXTRA_ARGS`` bare at two sites, so ``./deploy.sh`` with no extra args died with

    ./deploy.sh: line 77: EXTRA_ARGS[@]: unbound variable

right before ``terraform apply`` — the deploy never ran. CI (and this sandbox)
run bash **5.x**, where an empty-array ``"${arr[@]}"`` is HARMLESS — which is
exactly why shellcheck + CI never caught it, and why the historical *abort*
cannot be reproduced on the CI runner (``BASH_COMPAT``/``compat32`` does NOT
restore the pre-4.4 unbound-empty-array behaviour; that was a genuine bug fix,
not a compat-gated quirk). Honest envelope: the abort is bash-3.2-specific, so
the regression *teeth* are the STATIC source assertions below (a regression back
to the bare ``"${EXTRA_ARGS[@]}"`` form fails this suite on any bash), backed by
a behavioural check that the guarded idiom works + preserves arguments.

The FIX guards every expansion with the portable
``${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}`` alternate-value idiom (expands to
NOTHING when the array is unset/empty, to the quoted elements otherwise) —
bash 3.2 AND 4+ safe.

Pure shell-source assertions + a self-contained ``bash`` subshell over the
extracted idiom — no network / pip / AWS / terraform is touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_SCRIPT = _REPO_ROOT / "deploy" / "aws_token_minter" / "deploy.sh"

# The portable guarded expansion the fix introduced (both sites use it).
_GUARDED_EXPANSION = '${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}'
# The old, unguarded expansion that aborts on an empty array under bash 3.2.
_BARE_EXPANSION = '"${EXTRA_ARGS[@]}"'


@pytest.fixture(scope="module")
def deploy_script_text() -> str:
    """Return the full source of ``deploy.sh``."""
    assert _DEPLOY_SCRIPT.is_file(), f"missing deploy script: {_DEPLOY_SCRIPT}"
    return _DEPLOY_SCRIPT.read_text(encoding="utf-8")


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


def _run_snippet(snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet under ``set -u`` with an empty ``EXTRA_ARGS`` array."""
    prelude = "set -u\nEXTRA_ARGS=()\n"
    return subprocess.run(
        [_bash(), "-c", prelude + snippet],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )


# --------------------------------------------------------------------------- #
# Behavioural: the guarded idiom works under nounset + preserves arguments.
# --------------------------------------------------------------------------- #
def test_guarded_empty_array_expansion_does_not_abort() -> None:
    """The FIX's guarded idiom does not trip ``nounset`` on an EMPTY array."""
    result = _run_snippet(f"printf 'ok:%s\\n' {_GUARDED_EXPANSION}")
    assert result.returncode == 0, (
        f"guarded expansion must not abort on an empty array; "
        f"rc={result.returncode} stderr={result.stderr!r}"
    )
    assert "unbound variable" not in result.stderr


def test_guarded_expansion_preserves_elements_with_spaces() -> None:
    """The guarded idiom expands each element verbatim, spaces preserved."""
    snippet = (
        "EXTRA_ARGS=(-var 'schedule_expression=cron(35 0 * * ? *)')\n"
        f"for a in {_GUARDED_EXPANSION}; do printf '[%s]' \"$a\"; done\n"
    )
    result = _run_snippet(snippet)
    assert result.returncode == 0, result.stderr
    # The quoted [@] keeps the space-containing -var as ONE element.
    assert "[-var][schedule_expression=cron(35 0 * * ? *)]" in result.stdout


# --------------------------------------------------------------------------- #
# Static (the regression teeth): deploy.sh uses the guarded form at both sites
# and NEVER the bare form in executable code — a regression to the unguarded
# expansion fails these on any bash.
# --------------------------------------------------------------------------- #
def test_deploy_uses_guarded_extra_args_expansion(deploy_script_text: str) -> None:
    """``deploy.sh`` expands ``EXTRA_ARGS`` only via the guarded idiom."""
    assert _GUARDED_EXPANSION in deploy_script_text, (
        f"deploy.sh must use the portable guarded expansion {_GUARDED_EXPANSION!r} for EXTRA_ARGS"
    )
    # Both expansion sites (the ALERT_EMAIL scan + the terraform apply) guarded.
    assert deploy_script_text.count(_GUARDED_EXPANSION) >= 2, (
        "both EXTRA_ARGS expansion sites (the ALERT_EMAIL scan and the "
        "`terraform apply`) must use the guarded idiom"
    )


def test_deploy_never_uses_bare_extra_args_expansion(deploy_script_text: str) -> None:
    """No un-guarded ``"${EXTRA_ARGS[@]}"`` survives in EXECUTABLE code.

    The guarded idiom CONTAINS the bare token, so each executable line is first
    stripped of every guarded occurrence, then checked for a residual bare form.
    Comment lines (which quote the bare form in the explanatory note) are
    excluded — this asserts on runnable code only.
    """
    for line in _executable_lines(deploy_script_text):
        residual = line.replace(_GUARDED_EXPANSION, "")
        assert _BARE_EXPANSION not in residual, (
            "deploy.sh must not expand EXTRA_ARGS with the bare "
            f"{_BARE_EXPANSION} form (aborts on empty array under bash 3.2): {line!r}"
        )


def test_deploy_initialises_extra_args_as_empty_array(deploy_script_text: str) -> None:
    """``EXTRA_ARGS`` is initialised empty and filled only when args are given."""
    assert "EXTRA_ARGS=()" in deploy_script_text
    assert 'if [[ "$#" -gt 0 ]]; then' in deploy_script_text
