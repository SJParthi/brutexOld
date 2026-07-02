"""Content-assertion tests for ``build_lambda_package.sh`` + ``deploy.sh``.

DEC-TOKEN-MINTER-CENTRAL-001 (docs/MIGRATION.md #1060) +
DEC-MINTER-PURE-PYTHON-001 (docs/MIGRATION.md #1069). The Lambda deploy bundle is
built with a SINGLE deterministic ``pip install`` and **no Docker**: the only
bundled third-party dep is ``pyotp``, a pure-Python UNIVERSAL wheel
(``py3-none-any``, zero runtime deps), so ``pip install --target`` produces zero
platform-specific artifacts and the bundle is byte-portable from the operator's
macOS/ARM64 build host (project rules §8: Apple M4 Pro) to the Lambda's x86_64
Linux / cpython 3.12 runtime. The token mint is a pure-stdlib HTTPS POST in
``handler.py`` (a byte-for-byte reimplementation of ``growwapi`` 1.5.0
``GrowwAPI.get_access_token``), so ``growwapi`` — and its heavy native-extension
transitive tree that once forced the Docker build — is NOT bundled.

These tests pin those behaviours so a regression cannot silently reintroduce
Docker, ``growwapi``, or the cross-compile gymnastics. They are pure static
assertions over the shell source — no shell is executed, no network / pip / AWS
is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _REPO_ROOT / "deploy" / "aws_token_minter" / "build_lambda_package.sh"
_DEPLOY_SCRIPT = _REPO_ROOT / "deploy" / "aws_token_minter" / "deploy.sh"
_REQUIREMENTS = _REPO_ROOT / "deploy" / "aws_token_minter" / "requirements.txt"


@pytest.fixture(scope="module")
def build_script_text() -> str:
    """Return the full source of ``build_lambda_package.sh``."""
    assert _BUILD_SCRIPT.is_file(), f"missing build script: {_BUILD_SCRIPT}"
    return _BUILD_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def deploy_script_text() -> str:
    """Return the full source of ``deploy.sh``."""
    assert _DEPLOY_SCRIPT.is_file(), f"missing deploy script: {_DEPLOY_SCRIPT}"
    return _DEPLOY_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def requirements_text() -> str:
    """Return the full source of ``requirements.txt``."""
    assert _REQUIREMENTS.is_file(), f"missing requirements: {_REQUIREMENTS}"
    return _REQUIREMENTS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# pyotp is the ONLY bundled dep, PINNED — the pure-Python universal wheel.
# --------------------------------------------------------------------------- #
def test_pyotp_is_pinned_not_bare(build_script_text: str) -> None:
    """pyotp is version-PINNED (``pyotp==2.10.0``), the latest within ``>=2.9,<3.0``."""
    assert 'PYOTP_VERSION="${PYOTP_VERSION:-pyotp==2.10.0}"' in build_script_text, (
        "pyotp must default to a PINNED version (pyotp==2.10.0), not a bare 'pyotp'."
    )


def test_pinned_default_carries_no_bare_pyotp_default(build_script_text: str) -> None:
    """The old bare ``:-pyotp}`` default must be gone (regression guard)."""
    assert "${PYOTP_VERSION:-pyotp}" not in build_script_text


def test_build_installs_pyotp(build_script_text: str) -> None:
    """The single install line installs pyotp via its pinned-version variable."""
    assert '"$PYOTP_VERSION"' in build_script_text
    assert "-m pip install" in build_script_text
    assert '--target "$BUILD_DIR"' in build_script_text


# --------------------------------------------------------------------------- #
# NO DOCKER — the whole point of DEC-MINTER-PURE-PYTHON-001.
# --------------------------------------------------------------------------- #
def test_build_script_has_no_docker_logic(build_script_text: str) -> None:
    """The build must NOT invoke or probe Docker anywhere in executable code.

    ``docker`` may be NAMED in a comment (explaining that no Docker is needed) or
    an ``echo`` progress line (a user-facing "no Docker needed" message); this
    asserts no executable line references it — no ``docker run``, no
    ``docker version`` daemon probe, no ``docker_usable`` function.
    """
    assert "docker run" not in build_script_text
    assert "docker version" not in build_script_text
    assert "docker_usable" not in build_script_text
    for raw in build_script_text.splitlines():
        code = raw.split("#", 1)[0]  # strip any trailing/leading comment
        if code.lstrip().startswith("echo"):
            continue  # a user-facing "no Docker" message is fine
        assert "docker" not in code.lower(), f"docker must not be used: {raw!r}"


def test_build_script_states_no_docker_needed(build_script_text: str) -> None:
    """A comment states + explains why no Docker is required (operator clarity)."""
    lowered = build_script_text.lower()
    assert "no docker" in lowered
    # grounded in WHY: pyotp is a pure-Python universal wheel
    assert "universal wheel" in lowered
    assert "py3-none-any" in build_script_text


def test_build_script_has_no_cross_compile_flags(build_script_text: str) -> None:
    """No ``--platform`` / ``--only-binary`` / cross-abi flags in executable code.

    pyotp is a ``py3-none-any`` universal wheel, so no cross-compile flags are
    needed. The flag names may appear in a comment (explaining why they are gone);
    this asserts none is on an executable (non-comment) line.
    """
    for raw in build_script_text.splitlines():
        code = raw.split("#", 1)[0]  # strip any trailing/leading comment
        for flag in (
            "--platform",
            "--only-binary",
            "--python-version",
            "--implementation",
            "--abi",
        ):
            assert flag not in code, (
                f"{flag!r} must be gone from executable code: pyotp is a "
                "py3-none-any universal wheel (DEC-MINTER-PURE-PYTHON-001)."
            )


def test_build_script_has_no_two_path_branching(build_script_text: str) -> None:
    """The old two-path A/B build branching must be gone (single deterministic build)."""
    for token in ("Path A", "Path B", "FORCE_BUILD_PATH", "BUILD_PATH", "BUILD_IMAGE"):
        assert token not in build_script_text, f"two-path artifact {token!r} must be removed"


# --------------------------------------------------------------------------- #
# growwapi must NEVER be bundled (the mint is pure-stdlib now).
# --------------------------------------------------------------------------- #
def test_build_script_does_not_install_growwapi(build_script_text: str) -> None:
    """growwapi is NOT bundled — the mint is a pure-stdlib HTTPS POST.

    growwapi is legitimately NAMED in a comment (explaining why it is not
    bundled); this asserts it is never a pip-install ARGUMENT — no non-comment,
    non-echo line references it.
    """
    for raw in build_script_text.splitlines():
        code = raw.split("#", 1)[0]  # strip any trailing/leading comment
        if code.lstrip().startswith("echo"):
            continue
        assert "growwapi" not in code.lower(), f"growwapi must not be installed: {raw!r}"


# --------------------------------------------------------------------------- #
# boto3 must NEVER be bundled (it is on the AWS runtime).
# --------------------------------------------------------------------------- #
def test_build_script_does_not_pip_install_boto3(build_script_text: str) -> None:
    """boto3 is on the AWS runtime and must NOT be bundled into the zip.

    boto3 may be legitimately NAMED in a comment ("boto3 is on the AWS Lambda
    runtime"); this asserts it is never a pip-install ARGUMENT — no non-comment,
    non-echo line references it.
    """
    for raw in build_script_text.splitlines():
        code = raw.split("#", 1)[0]  # strip any trailing/leading comment
        if code.lstrip().startswith("echo"):
            continue
        assert "boto3" not in code, f"boto3 must not be installed: {raw!r}"


def test_build_stages_the_handler_source(build_script_text: str) -> None:
    """The handler + __init__ are copied into the flat, importable bundle."""
    assert '"$SCRIPT_DIR/handler.py" "$BUILD_DIR/handler.py"' in build_script_text
    assert '"$SCRIPT_DIR/__init__.py" "$BUILD_DIR/__init__.py"' in build_script_text


# --------------------------------------------------------------------------- #
# requirements.txt declares exactly the one bundled dep.
# --------------------------------------------------------------------------- #
def test_requirements_pins_only_pyotp(requirements_text: str) -> None:
    """requirements.txt declares exactly ``pyotp==2.10.0`` and nothing else."""
    deps = [
        line.strip()
        for line in requirements_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert deps == ["pyotp==2.10.0"], f"requirements must be exactly [pyotp==2.10.0], got {deps}"


def test_requirements_does_not_list_growwapi_or_boto3(requirements_text: str) -> None:
    """Neither growwapi nor boto3 is a declared bundle dependency."""
    for raw in requirements_text.splitlines():
        code = raw.split("#", 1)[0]
        assert "growwapi" not in code.lower(), f"growwapi must not be a requirement: {raw!r}"
        assert "boto3" not in code, f"boto3 must not be a requirement: {raw!r}"


# --------------------------------------------------------------------------- #
# deploy.sh no longer probes Docker; it states the pure-Python build.
# --------------------------------------------------------------------------- #
def test_deploy_has_no_docker_logic(deploy_script_text: str) -> None:
    """deploy.sh must NOT probe Docker anywhere in executable code.

    A user-facing ``echo`` "no Docker" message is fine; this asserts no
    executable (non-comment, non-echo) line references Docker — the old
    ``docker version`` daemon probe is gone.
    """
    assert "docker version" not in deploy_script_text
    for raw in deploy_script_text.splitlines():
        code = raw.split("#", 1)[0]
        if code.lstrip().startswith("echo"):
            continue  # a user-facing "no Docker" message is fine
        assert "docker" not in code.lower(), f"docker must not be used in deploy.sh: {raw!r}"


def test_deploy_states_no_docker_build(deploy_script_text: str) -> None:
    """deploy.sh tells the operator the build is pure-Python, no Docker."""
    lowered = deploy_script_text.lower()
    assert "no docker" in lowered
    assert "pure python" in lowered


def test_deploy_expands_extra_args_safely_under_set_u(deploy_script_text: str) -> None:
    """deploy.sh expands EXTRA_ARGS with the empty-safe idiom (bash 3.2 + ``set -u``).

    Under ``set -euo pipefail``, bash 3.2 — the macOS system ``/bin/bash`` on the
    project's reference host (project rules §8) — treats a bare
    ``"${EXTRA_ARGS[@]}"`` on an EMPTY array as an "unbound variable" and aborts
    the deploy when the operator passes no extra ``-var`` args. The
    ``"${arr[@]+"${arr[@]}"}"`` alternate-value idiom expands to nothing on an
    empty array and to the quoted elements otherwise, on bash 3.2 AND 4.4+. This
    pins the fix (DEC-MINTER-PURE-PYTHON-001 #1069 hostile-review MINOR).
    """
    safe = '"${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"'
    # All three expansion sites (the ALERT_EMAIL scan loop + the adopt-preflight
    # `terraform import` + the terraform apply) use the empty-safe idiom.
    assert deploy_script_text.count(safe) == 3, (
        f"all EXTRA_ARGS expansions must use the empty-safe {safe} idiom"
    )
    # The bare, empty-unsafe forms must be gone.
    assert 'in "${EXTRA_ARGS[@]}"; do' not in deploy_script_text
    assert 'apply "${EXTRA_ARGS[@]}"' not in deploy_script_text
    assert 'import -input=false "${EXTRA_ARGS[@]}"' not in deploy_script_text
