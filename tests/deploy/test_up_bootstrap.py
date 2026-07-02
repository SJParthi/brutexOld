"""Content-assertion tests for the one-command bootstrap ``up.sh``.

DEC-ONE-COMMAND-UP-001 (docs/MIGRATION.md). ``./up.sh`` takes the operator from
ANY starting state (fresh clone, stale checkout, half-broken venv) to a RUNNING
dashboard ready to pull, idempotently. These tests pin the script's contract so
a regression cannot silently drop a step, reorder the chain, reintroduce a
bash-4-only construct (the operator's macOS ships bash 3.2), weaken a
loud-failure path, mint a token, or stop persisting the config.

They follow the proven ``tests/deploy`` pattern (see
``test_deploy_remote_state_backend.py``): pure static assertions over the shell
source — no shell step is executed against the machine, no network / AWS is
touched. The single subprocess is ``bash -n`` (a parse-only syntax check).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UP_SH = _REPO_ROOT / "up.sh"
_README = _REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def up_text() -> str:
    """Return the full source of ``up.sh``."""
    assert _UP_SH.is_file(), f"missing bootstrap script: {_UP_SH}"
    return _UP_SH.read_text(encoding="utf-8")


def _executable_lines(script_text: str) -> list[str]:
    """Return the executable (non-comment, non-blank) lines of a shell script."""
    out: list[str] = []
    for raw in script_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(raw)
    return out


# --------------------------------------------------------------------------- #
# Existence, shebang, and parseability.
# --------------------------------------------------------------------------- #
def test_up_sh_exists_and_is_executable() -> None:
    """``up.sh`` exists at the repo root with the executable bit set."""
    assert _UP_SH.is_file()
    assert os.access(_UP_SH, os.X_OK), "up.sh must be chmod +x (one command, no `bash` prefix)"


def test_shebang_is_env_bash(up_text: str) -> None:
    """The shebang is ``#!/usr/bin/env bash`` (PATH bash, not a hardcoded /bin/bash)."""
    assert up_text.splitlines()[0] == "#!/usr/bin/env bash"


def test_bash_syntax_parses() -> None:
    """``bash -n up.sh`` parses cleanly (a hermetic, execute-nothing check)."""
    proc = subprocess.run(  # fixed argv, parse-only, no shell, no network
        ["bash", "-n", str(_UP_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"


# --------------------------------------------------------------------------- #
# The six steps exist, in order, behind the fast path.
# --------------------------------------------------------------------------- #
def test_step_banners_exist_in_order(up_text: str) -> None:
    """The six step banners appear exactly once each, in strictly increasing order."""
    banners = [f"Step {i}/6" for i in range(1, 7)]
    positions: list[int] = []
    for banner in banners:
        # The banner text appears once in the `say` call (comments use a
        # distinct wording), so count only the executable occurrences.
        exec_hits = [ln for ln in _executable_lines(up_text) if banner in ln]
        assert len(exec_hits) == 1, f"banner {banner!r} must appear exactly once: {exec_hits}"
        positions.append(up_text.index(banner))
    assert positions == sorted(positions), "step banners are out of order"


def test_already_ready_fast_path_precedes_setup(up_text: str) -> None:
    """The runtime-file PID fast path runs BEFORE any setup step (re-run = seconds)."""
    assert "already ready" in up_text
    assert "runtime.json" in up_text
    assert "kill -0" in up_text, "PID liveness must use kill -0 (no process spawned)"
    assert up_text.index("already ready") < up_text.index("Step 1/6")


def test_fast_path_rechecked_before_start(up_text: str) -> None:
    """The fast path is re-checked at Step 6 so a concurrent up.sh never double-boots."""
    step6 = up_text.index("Step 6/6")
    assert "runtime_pid_alive" in up_text[step6:], "Step 6 must re-check the live-PID fast path"


# --------------------------------------------------------------------------- #
# Step 1 — Python discovery + venv self-heal.
# --------------------------------------------------------------------------- #
def test_missing_python_prints_exact_per_os_install_hint(up_text: str) -> None:
    """A missing python3.12 fails loudly with the exact per-OS install command."""
    assert "brew install python@3.12" in up_text
    assert "sudo apt-get install -y python3.12 python3.12-venv" in up_text
    assert 'failhard "python3.12 NOT found' in up_text


def test_python3_fallback_requires_312(up_text: str) -> None:
    """The ``python3`` fallback is version-gated to >= 3.12 (never a silent 3.11 venv)."""
    assert "sys.version_info[:2] >= (3, 12)" in up_text


def test_venv_self_heal_rebuilds_only_the_venv(up_text: str) -> None:
    """Every ``rm -rf`` targets the in-repo ``.venv`` ONLY — user data is never wiped."""
    rm_rf_lines = [ln for ln in _executable_lines(up_text) if "rm -rf" in ln]
    assert rm_rf_lines, "the self-heal rebuild path must exist"
    for line in rm_rf_lines:
        assert '"$VENV"' in line, f"rm -rf may only target the venv: {line!r}"
    for line in _executable_lines(up_text):
        if line.lstrip().startswith("rm "):
            assert "BRUTEX_HOME" not in line and "LAKE_ROOT/" not in line.replace(
                "$LAKE_ROOT/.up-write-probe", ""
            ), f"rm must never target user data: {line!r}"


def test_venv_heal_probes_pip_health(up_text: str) -> None:
    """A venv with a broken pip (stale .pth / half-install) is detected and rebuilt."""
    assert '"$VPY" -m pip --version' in up_text


# --------------------------------------------------------------------------- #
# Step 2 — dependency install (guarded) + per-dep PASS/FAIL verification.
# --------------------------------------------------------------------------- #
def test_pip_install_is_guarded_by_import_probe(up_text: str) -> None:
    """``pip install`` runs only when the import probe fails (idempotent re-run)."""
    assert "probe_deps" in up_text
    assert up_text.index("if probe_deps; then") < up_text.index("pip install --quiet -e")


def test_installs_the_pull_capable_extras(up_text: str) -> None:
    """The install covers the pull-critical extras — groww (SDK+TOTP) + token-broker (boto3)."""
    assert ".[dev,data,ui,groww,token-broker]" in up_text


def test_pull_critical_deps_verified_individually(up_text: str) -> None:
    """boto3 / growwapi / pyotp (the real-pull deps) each get a PASS/FAIL line + exact fix."""
    deps_line = next(ln for ln in up_text.splitlines() if ln.startswith("DEPS="))
    for dep in ("boto3", "growwapi", "pyotp", "fastapi", "uvicorn", "polars", "duckdb"):
        assert dep in deps_line, f"{dep} missing from the per-dep verification list"
    assert 'FAIL  import %s — fix: %s -m pip install -e "%s"' in up_text


# --------------------------------------------------------------------------- #
# Step 3 — Rust wheels: probe-first build, honest hard stop without cargo.
# --------------------------------------------------------------------------- #
def test_wheels_built_only_when_missing(up_text: str) -> None:
    """Both wheels are import-probed BEFORE any maturin build (idempotent re-run)."""
    build = up_text.index('"$VENV/bin/maturin" develop --release')
    assert up_text.index('"$VPY" -c "import fno_math"') < build
    assert up_text.index('"$VPY" -c "import trading_core"') < build
    assert "rust/fno-math" in up_text
    assert "rust/trading-core" in up_text
    assert "develop --release" in up_text


def test_missing_cargo_with_missing_wheels_is_an_honest_hard_stop(up_text: str) -> None:
    """Wheels-missing + cargo-absent fails LOUDLY with the rustup one-liner.

    The wheels ARE required for the dashboard + pull path
    (``brutex.indicators.*`` import ``trading_core`` at module top), so a
    "clean skip" would be an illusion — the script must say so and stop.
    """
    assert "sh.rustup.rs" in up_text
    assert "cargo NOT found" in up_text
    assert "the dashboard cannot import without them" in up_text


# --------------------------------------------------------------------------- #
# Step 4 — config persisted ONCE (no per-session env exports ever again).
# --------------------------------------------------------------------------- #
def test_lake_root_persisted_once_in_brutex_env(up_text: str) -> None:
    """The lake root is recorded once in ``~/.brutex/.env`` behind a presence guard."""
    assert 'CONF="$BRUTEX_HOME/.env"' in up_text
    assert "sed -n 's/^BRUTEX_LAKE_ROOT=//p'" in up_text, "persisted value must be read first"
    assert "printf 'BRUTEX_LAKE_ROOT=%s\\n'" in up_text, "append the value when absent"


def test_lake_root_precedence_persisted_env_default(up_text: str) -> None:
    """Precedence: persisted value → exported ``BRUTEX_LAKE_ROOT`` → ``~/.brutex/lake``."""
    persisted = up_text.index('if [ -n "$persisted_lake" ]; then')
    env_branch = up_text.index('elif [ -n "${BRUTEX_LAKE_ROOT:-}" ]; then')
    default = up_text.index('LAKE_ROOT="$BRUTEX_HOME/lake"')
    assert persisted < env_branch < default
    # `${BRUTEX_LAKE_ROOT:-...}` guards mean the script NEVER requires an export.
    assert "${BRUTEX_LAKE_ROOT:-" in up_text


def test_auto_resume_enabled_by_default_via_persisted_config(up_text: str) -> None:
    """``BRUTEX_AUTO_RESUME_ON_BOOT=1`` is persisted once; an explicit setting is respected."""
    assert "grep -q '^BRUTEX_AUTO_RESUME_ON_BOOT='" in up_text, "presence guard required"
    assert "BRUTEX_AUTO_RESUME_ON_BOOT=1" in up_text
    # Honest wiring: the flag resumes ONLY when the ledger shows unfinished work.
    assert "unfinished work" in up_text


# --------------------------------------------------------------------------- #
# Step 5 — self-check chain: order, honesty, and the never-mint guarantee.
# --------------------------------------------------------------------------- #
def test_self_check_chain_order(up_text: str) -> None:
    """The chain runs deps → AWS identity → SSM token read → lake-root write probe."""
    deps = up_text.index("deps importable")
    sts = up_text.index("get_caller_identity")
    ssm = up_text.index("get_parameter(Name=")
    lake = up_text.index(".up-write-probe")
    assert deps < sts < ssm < lake, "self-check chain out of order"


def test_missing_aws_credentials_prints_aws_configure_hint(up_text: str) -> None:
    """An unusable AWS identity degrades with the exact ``aws configure`` fix."""
    assert "aws configure" in up_text
    assert "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY" in up_text


def test_ssm_token_check_is_read_only_and_never_mints(up_text: str) -> None:
    """The token check is ONE read-only GetParameter — up.sh can never mint or write.

    The never-mint guarantee, statically enforced: no ``get_access_token`` (the
    SDK mint call), no ``put_parameter`` (an SSM write), and no TOTP generation
    anywhere in the script. The only SSM verb is ``get_parameter``.
    """
    lowered = up_text.lower()
    assert "get_access_token" not in lowered
    assert "put_parameter" not in lowered
    assert "totp(" not in lowered
    assert "get_parameter(Name=" in up_text
    assert "WithDecryption=True" in up_text
    assert up_text.count("NEVER mints") >= 1


def test_token_value_is_never_printed(up_text: str) -> None:
    """Only the token LENGTH is ever echoed — the secret value never reaches a log."""
    assert 'print("OK %d" % len(value))' in up_text
    assert "never printed" in up_text.lower()
    assert not re.search(r"print\((?:\"OK \" ?\+ ?)?value", up_text), "raw token print found"


def test_unreadable_ssm_names_the_minter_deploy_fix(up_text: str) -> None:
    """A missing/denied token parameter names the exact cause + the minter deploy fix."""
    assert '"ERR ParameterNotFound")' in up_text
    assert "deploy/aws_token_minter/deploy.sh" in up_text
    assert "AccessDenied" in up_text
    assert "/tickvault/prod/groww/access-token" in up_text


def test_unwritable_lake_root_is_a_hard_stop(up_text: str) -> None:
    """An unwritable lake root fails hard (exit non-zero) with the exact fix."""
    assert 'failhard "lake root NOT writable' in up_text


def test_aws_degradation_does_not_block_the_dashboard(up_text: str) -> None:
    """AWS/SSM failures DEGRADE honestly — the dashboard still starts, loudly labelled."""
    assert "degrade()" in up_text.replace("degrade() ", "degrade()")
    assert "DEGRADED=1" in up_text
    assert "DEGRADED: an AWS/SSM check failed above" in up_text


def test_loud_failures_exit_nonzero(up_text: str) -> None:
    """Every hard failure path exits non-zero via the one ``failhard`` helper."""
    helper = up_text[up_text.index("failhard()") :]
    helper = helper[: helper.index("}")]
    assert "exit 1" in helper
    assert up_text.count("failhard ") >= 8, "each hard-failure site must use failhard"


# --------------------------------------------------------------------------- #
# Step 6 — start: reuse the existing one-click entry, never a duplicate server.
# --------------------------------------------------------------------------- #
def test_start_reuses_the_existing_oneclick_entry(up_text: str) -> None:
    """The dashboard boots via ``python -m brutex run`` — the same entry ``make run`` uses."""
    assert 'exec "$VPY" -m brutex run' in up_text
    assert "uvicorn.run" not in up_text, "up.sh must not duplicate the server entry"
    assert "http://127.0.0.1:9080" in up_text


def test_no_start_flag_supports_ci_and_tests(up_text: str) -> None:
    """``--no-start`` runs the full setup + self-checks without booting the server."""
    assert "--no-start" in up_text
    assert "NO_START=1" in up_text


# --------------------------------------------------------------------------- #
# bash 3.2 (macOS default) compatibility — no bash-4-only constructs.
# --------------------------------------------------------------------------- #
def test_no_bash4_only_constructs(up_text: str) -> None:
    """No mapfile/readarray, no associative arrays, no ``${var,,}``/``${var^^}``, no ``|&``.

    Scans EXECUTABLE lines only (the header comment may legitimately *name*
    the banned constructs when documenting the bash-3.2 envelope).
    """
    code = "\n".join(_executable_lines(up_text))
    for banned in ("mapfile", "readarray", "declare -A", "|&"):
        assert banned not in code, f"bash-4-only construct found: {banned!r}"
    assert not re.search(r"\$\{[A-Za-z_]+(,,|\^\^)", code), "bash-4 case expansion found"


def test_no_arrays_at_all(up_text: str) -> None:
    """No shell arrays — ``${arr[@]}`` under ``set -u`` breaks on empty arrays in bash 3.2."""
    code = "\n".join(_executable_lines(up_text))
    assert "[@]" not in code
    assert "[*]" not in code


def test_set_u_with_guarded_expansions(up_text: str) -> None:
    """``set -u`` is on, and every optional env var is ``${VAR:-}``-guarded."""
    assert "set -u" in up_text
    for var in ("BRUTEX_LAKE_ROOT", "BRUTEX_RUNTIME_FILE", "BRUTEX_TOKEN_AWS_REGION"):
        assert f"${{{var}:-" in up_text, f"unguarded optional env var: {var}"


# --------------------------------------------------------------------------- #
# Documentation — the quickstart IS the one command.
# --------------------------------------------------------------------------- #
def test_readme_quickstart_points_at_up_sh() -> None:
    """The README quickstart leads with ``./up.sh`` (manual steps stay as an appendix)."""
    text = _README.read_text(encoding="utf-8")
    assert "./up.sh" in text
    assert text.index("./up.sh") < text.index("make run"), "up.sh must lead the quickstart"
