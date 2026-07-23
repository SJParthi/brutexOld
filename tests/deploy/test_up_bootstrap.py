"""Content-assertion tests for the one-command bootstrap ``up.sh``.

DEC-ONE-COMMAND-UP-001 (docs/MIGRATION.md). ``./up.sh`` takes the operator from
ANY starting state (fresh clone, stale checkout, half-broken venv) to a RUNNING
dashboard ready to pull, idempotently. These tests pin the script's contract so
a regression cannot silently drop a step, reorder the chain, reintroduce a
bash-4-only construct (the operator's macOS ships bash 3.2), weaken a
loud-failure path, mint a token, or stop persisting the config.

They follow the proven ``tests/deploy`` pattern (see
``test_deploy_remote_state_backend.py``): static assertions over the shell
source — no setup step is executed against the machine, no network / AWS is
touched. The only subprocesses are ``bash -n`` (a parse-only syntax check) and
the hermetic ``--print-lake-detect`` hook, which runs ONLY the zero-decision
lake-detection matrix against a tmp ``$HOME`` (no pip, no cargo, no network) —
the operator-never-types-a-path guarantee is exercised for real.
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
def _up_wheel_specs(up_text: str) -> dict[str, tuple[str, bool]]:
    """Parse up.sh's ``WHEEL_SPECS`` block → ``{crate_dir: (module, required)}``.

    Each spec line is ``<module> <rel-path> <required|optional>``; the whole
    set is the single-quoted heredoc string assigned to ``WHEEL_SPECS=``. This
    is the source both up.sh (build loop) and the drift pins read. The
    ``module`` (import name) is captured too so the drift pin asserts it against
    ``WheelSpec.name`` — a module-name typo in up.sh (e.g. ``brutex_pullstate``)
    is then caught at CI time, not only at runtime by the import-smoke failhard.
    """
    block = up_text.split("WHEEL_SPECS='", 1)[1].split("'", 1)[0]
    specs: dict[str, tuple[str, bool]] = {}
    for line in block.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        module, rel_path, tier = parts
        specs[rel_path] = (module, tier == "required")
    return specs


def test_wheels_import_probed_before_any_build(up_text: str) -> None:
    """Every kernel is import-probed BEFORE any maturin build (idempotent re-run).

    The Step-3 loop probes ``"$VPY" -c "import $module"`` for each spec first,
    then builds only the missing ones — so a re-run on an all-importable box
    skips the whole build (``all Rust kernels already importable``).
    """
    build = up_text.index('"$VENV/bin/maturin" develop --release')
    assert up_text.index('"$VPY" -c "import $module"') < build
    assert "all Rust kernels already importable" in up_text
    # Every required crate path is present in the spec block, verbatim.
    for crate in (
        "rust/fno-math",
        "rust/trading-core",
        "rust/brutex-lake",
        "rust/brutex-identity",
        "rust/brutex-refine",
        "rust/brutex-pull-state",
    ):
        assert crate in up_text, f"{crate} missing from up.sh WHEEL_SPECS"
    assert "develop --release" in up_text


def test_up_sh_wheel_set_mirrors_box_update(up_text: str) -> None:
    """up.sh's WHEEL_SPECS == the canonical ``box_update.WHEEL_SET`` (crate → required).

    The single source of truth for what the turnkey path builds is
    ``brutex.nightly.box_update.WHEEL_SET`` (itself Makefile-mirrored, per
    ``test_wheel_set_is_the_makefile_mirror``). This anti-drift pin makes up.sh
    move with it FOREVER — the exact class of bug that crashed a stale box with
    ``ModuleNotFoundError: No module named 'brutex_identity'`` (up.sh built only
    fno_math + trading_core while ``brutex.engine.results_store`` hard-imports
    ``brutex_identity``, DEC-RUST-RUNID-STORES-001) can never recur silently.
    """
    from brutex.nightly.box_update import WHEEL_SET

    up_specs = _up_wheel_specs(up_text)
    canonical = {spec.crate_dir: (spec.name, spec.required) for spec in WHEEL_SET}
    assert up_specs == canonical, (
        "up.sh WHEEL_SPECS drifted from the canonical box_update.WHEEL_SET — "
        f"up.sh has {up_specs}, WHEEL_SET has {canonical}. Each entry is "
        "(import-module, required); a mismatch is a crate add/remove, a wrong "
        "required/optional tier, OR a module-name typo (which would otherwise "
        "only surface at runtime via the import-smoke failhard). Update up.sh "
        "Step 3 (and the Makefile `run` target) so all three move together."
    )


def test_required_wheel_build_is_a_hard_stop_optional_degrades(up_text: str) -> None:
    """A REQUIRED wheel build failure hard-stops; the OPTIONAL one degrades.

    A required-crate build failure must call ``failhard`` (the box cannot boot
    without it); brutex_feed's must ``degrade`` (BRUTEX_FEED_RUST is default-OFF,
    the feed falls back to the Python oracle and the boot continues).
    """
    step3 = up_text.split('say "Step 3/6', 1)[1].split('say "Step 4/6', 1)[0]
    # The required loop hard-stops on a maturin failure.
    assert "for spec in $missing_required" in step3
    assert 'failhard "maturin develop --release failed for $crate' in step3
    # The optional loop degrades (never failhard).
    assert "for spec in $missing_optional" in step3
    assert 'degrade "optional Rust kernel $crate failed to build' in step3
    # Every required kernel is import-smoked after the build (no half-built boot).
    assert "smoke_failed" in step3
    assert "required wheels built but still not importable" in step3


def test_missing_cargo_with_missing_wheels_is_an_honest_hard_stop(up_text: str) -> None:
    """Required-wheels-missing + cargo-absent fails LOUDLY with the rustup one-liner.

    The required wheels ARE needed for the dashboard + pull path
    (``brutex.engine.results_store`` imports ``brutex_identity`` and
    ``brutex.indicators.*`` import ``trading_core`` at module top), so a
    "clean skip" would be an illusion — the script must say so and stop.
    """
    assert "sh.rustup.rs" in up_text
    assert "cargo NOT found" in up_text
    assert "the dashboard cannot import without them" in up_text
    # The hard stop is gated on a REQUIRED wheel being missing (a box with only
    # the optional brutex_feed missing + no cargo still boots — degraded).
    assert '[ -n "$missing_required" ] && [ -z "$CARGO_BIN" ]' in up_text


# --------------------------------------------------------------------------- #
# Step 4 — ZERO-DECISION lake detection + config persisted ONCE.
# --------------------------------------------------------------------------- #
def test_lake_root_persisted_once_in_brutex_env(up_text: str) -> None:
    """The lake root is recorded once in ``~/.brutex/.env`` behind a presence guard."""
    assert 'CONF="$BRUTEX_HOME/.env"' in up_text
    assert "sed -n 's/^BRUTEX_LAKE_ROOT=//p'" in up_text, "persisted value must be read first"
    assert "grep -q '^BRUTEX_LAKE_ROOT='" in up_text, "presence guard before the append"
    assert "printf 'BRUTEX_LAKE_ROOT=%s\\n'" in up_text, "append the value when absent"


def test_lake_detection_is_zero_decision(up_text: str) -> None:
    """The lake is decided with ZERO operator input: persisted → auto-detect → create.

    Branch order inside ``resolve_and_record_lake``: a persisted config line
    first, then an EXISTING lake with real data (environment-pointed path, then
    the app default ``~/.brutex/lake``), then an environment-pointed path, then
    create-the-default. Never a prompt, never a required export.
    """
    assert "lake_has_data()" in up_text
    assert "lake_dataset_count()" in up_text
    assert "resolve_and_record_lake()" in up_text
    body = up_text[up_text.index("resolve_and_record_lake()") :]
    persisted = body.index('if [ -n "$persisted_lake" ]; then')
    env_data = body.index('lake_has_data "${BRUTEX_LAKE_ROOT}"')
    default_data = body.index('lake_has_data "$BRUTEX_HOME/lake"')
    env_only = body.index('LAKE_SOURCE="from-environment"')
    created = body.index('LAKE_SOURCE="created"')
    assert persisted < env_data < default_data < env_only < created
    # `${BRUTEX_LAKE_ROOT:-}` guards mean the script NEVER requires an export.
    assert "${BRUTEX_LAKE_ROOT:-" in up_text
    # No interactive input anywhere — the operator never types a path (no
    # `read` builtin at a command position on any executable line).
    code = "\n".join(_executable_lines(up_text))
    assert not re.search(r"(?m)^\s*read\b", code), "interactive `read` found"
    assert not re.search(r"[;&|]\s*read\b", code), "interactive `read` found"


def test_lake_messages_never_ask_for_a_path(up_text: str) -> None:
    """No operator-facing line mentions the env var or asks for a path.

    The env var name stays a MECHANISM (the persisted-file key + the read of an
    already-exported value) — every printed ``pass``/``note``/``failhard`` line
    describes the decision informatively instead of instructing the operator to
    set anything.
    """
    for line in _executable_lines(up_text):
        stripped = line.strip()
        if stripped.startswith(("pass ", "note ", "failhard ", "degrade ")):
            assert "BRUTEX_LAKE_ROOT" not in line, f"operator-facing env-var mention: {line!r}"


def test_informative_lake_line_with_dataset_count(up_text: str) -> None:
    """The lake line reads like ``lake: … (auto-detected, N datasets)`` — zero-decision."""
    assert "auto-detected existing data, $LAKE_DATASETS datasets" in up_text
    assert "lake: $LAKE_ROOT ($LAKE_SOURCE, $LAKE_DATASETS datasets)" in up_text


# --------------------------------------------------------------------------- #
# Step 4 — the detection MATRIX, executed hermetically (tmp HOME, no network).
# --------------------------------------------------------------------------- #
def _run_lake_detect(home: Path, env_lake: str | None = None) -> tuple[str, str, str]:
    """Run ``up.sh --print-lake-detect`` with *home* as ``$HOME`` and parse the verdict.

    Hermetic: only the detection + recording logic runs (no pip, no cargo, no
    network); returns ``(source, path, dataset_count)``.
    """
    env: dict[str, str] = {k: v for k, v in os.environ.items() if k != "BRUTEX_LAKE_ROOT"}
    env["HOME"] = str(home)
    if env_lake is not None:
        env["BRUTEX_LAKE_ROOT"] = env_lake
    proc = subprocess.run(  # fixed argv, detection-only hook, no shell, no network
        ["bash", str(_UP_SH), "--print-lake-detect"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"--print-lake-detect failed: {proc.stderr}"
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("LAKE_DECISION="))
    source, rest = line[len("LAKE_DECISION=") :].split(" ", 1)
    path, count = rest.rsplit(" datasets=", 1)
    return source, path, count


def _seed_lake(root: Path) -> None:
    """Create a minimal real-data lake: one Parquet partition under ``bars/``."""
    partition = root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024"
    partition.mkdir(parents=True)
    (partition / "01.parquet").touch()


def test_matrix_nothing_anywhere_creates_default_then_persists(tmp_path: Path) -> None:
    """No config, no env, no data → the app default is created + recorded; re-run reads it."""
    source, path, count = _run_lake_detect(tmp_path)
    assert source == "created"
    assert path == str(tmp_path / ".brutex" / "lake")
    assert count == "0"
    conf = (tmp_path / ".brutex" / ".env").read_text(encoding="utf-8")
    assert f"BRUTEX_LAKE_ROOT={path}\n" in conf
    # Idempotent re-run: the persisted line wins, nothing is appended twice.
    source2, path2, _ = _run_lake_detect(tmp_path)
    assert (source2, path2) == ("persisted", path)
    conf2 = (tmp_path / ".brutex" / ".env").read_text(encoding="utf-8")
    assert conf2.count("BRUTEX_LAKE_ROOT=") == 1


def test_matrix_existing_data_at_default_is_auto_detected(tmp_path: Path) -> None:
    """Real data at the app default ``~/.brutex/lake`` → auto-detected + recorded."""
    _seed_lake(tmp_path / ".brutex" / "lake")
    source, path, count = _run_lake_detect(tmp_path)
    assert source == "auto-detected"
    assert path == str(tmp_path / ".brutex" / "lake")
    assert count == "1"


def test_matrix_env_pointed_existing_lake_wins_auto_detection(tmp_path: Path) -> None:
    """An environment-pointed path WITH data is auto-detected and recorded permanently."""
    external = tmp_path / "external-ssd"
    _seed_lake(external)
    source, path, count = _run_lake_detect(tmp_path, env_lake=str(external))
    assert source == "auto-detected"
    assert path == str(external)
    assert count == "1"
    conf = (tmp_path / ".brutex" / ".env").read_text(encoding="utf-8")
    assert f"BRUTEX_LAKE_ROOT={external}\n" in conf


def test_matrix_env_set_without_data_is_recorded(tmp_path: Path) -> None:
    """An environment-pointed path with NO data yet is still recorded (data will live there)."""
    new_disk = tmp_path / "new-disk"
    source, path, count = _run_lake_detect(tmp_path, env_lake=str(new_disk))
    assert source == "from-environment"
    assert path == str(new_disk)
    assert count == "0"


def test_matrix_env_override_of_persisted_never_rewrites_config(tmp_path: Path) -> None:
    """A persisted config + a differing exported env → env wins for the RUN, config untouched."""
    _run_lake_detect(tmp_path)  # records the default
    other = tmp_path / "other"
    source, path, _ = _run_lake_detect(tmp_path, env_lake=str(other))
    assert source == "environment-override"
    assert path == str(other)
    conf = (tmp_path / ".brutex" / ".env").read_text(encoding="utf-8")
    assert conf.count("BRUTEX_LAKE_ROOT=") == 1
    assert str(other) not in conf, "an env override must never rewrite the persisted line"


def test_auto_resume_master_gate_documented_never_armed(up_text: str) -> None:
    """Boot auto-resume ships OFF: the master gate is written COMMENTED, never armed.

    DEC-AUTO-RESUME-DEFAULT-OFF-001 (operator directive): an auto-pull must
    never launch without explicit opt-in. up.sh pre-arms ONLY the legacy
    SUB-gate (``BRUTEX_AUTO_RESUME_ON_BOOT=1`` — inert on its own) so that one
    flip (``BRUTEX_AUTO_RESUME=1``) restores the full prior behaviour, and
    writes the master gate itself as a COMMENTED documentation line. The pass
    message is honest — OFF by default, the exact opt-in named — never the
    retired "auto-resume enabled" claim.
    """
    # The legacy sub-gate line is still written once (presence-guarded).
    assert "grep -q '^BRUTEX_AUTO_RESUME_ON_BOOT='" in up_text, "presence guard required"
    assert "printf 'BRUTEX_AUTO_RESUME_ON_BOOT=1" in up_text
    # The master gate is written ONLY as a commented documentation line.
    assert "printf '# BRUTEX_AUTO_RESUME=1" in up_text
    assert "printf 'BRUTEX_AUTO_RESUME=1" not in up_text, (
        "the master gate must NEVER be persisted uncommented — boot auto-resume "
        "is OFF by default by operator directive (DEC-AUTO-RESUME-DEFAULT-OFF-001)"
    )
    # The pass message tells the truth: OFF by default + the exact opt-in flip.
    assert "auto-resume is OFF by default" in up_text
    assert "set BRUTEX_AUTO_RESUME=1" in up_text
    # Honest wiring: even opted in, the resume fires ONLY on unfinished work.
    assert "unfinished work" in up_text
    # The retired false claim must not resurface.
    assert "auto-resume-on-boot enabled" not in up_text


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
    # The DEGRADED summary honestly surfaces an AWS/SSM failure (a LIVE pull
    # will fail) without claiming AWS/SSM is the ONLY degrade source — an
    # optional Rust kernel (brutex_feed) degrade routes through the same helper
    # and is named distinctly (MINOR-1 cross-wire fix).
    assert "An AWS/SSM failure means a LIVE pull will fail" in up_text


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
