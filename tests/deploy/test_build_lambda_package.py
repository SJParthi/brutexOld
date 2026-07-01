"""Content-assertion tests for ``build_lambda_package.sh``.

DEC-TOKEN-MINTER-CENTRAL-001 (docs/MIGRATION.md #1060). The Lambda deploy bundle
must be CROSS-COMPILED for the Lambda's runtime (x86_64 Linux / cpython 3.12), NOT
the build host — the operator builds on macOS/ARM64 (project rules §8: Apple M4
Pro), so a plain ``pip install --target`` would vendor macOS/ARM wheels and the
handler's ``from growwapi import GrowwAPI`` (which drags in native-extension
transitive deps: pynacl, pydantic-core, pandas, aiohttp, protobuf) would
``ImportError`` on the first real mint in the x86_64 Linux Lambda runtime.

These tests pin the cross-compile ``pip`` flags in ``build_lambda_package.sh`` so a
regression cannot silently drop them (which would ship a broken, host-native zip
that ``terraform apply`` accepts but that fails on the first mint). They are pure
static assertions over the shell source — no shell is executed, no network / pip /
AWS is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD_SCRIPT = _REPO_ROOT / "deploy" / "aws_token_minter" / "build_lambda_package.sh"


@pytest.fixture(scope="module")
def build_script_text() -> str:
    """Return the full source of ``build_lambda_package.sh``."""
    assert _BUILD_SCRIPT.is_file(), f"missing build script: {_BUILD_SCRIPT}"
    return _BUILD_SCRIPT.read_text(encoding="utf-8")


# The exact cross-compile flags the pip install MUST carry so the bundle targets
# the Lambda runtime (x86_64 Linux, cpython 3.12) regardless of the build host.
_REQUIRED_FLAGS = (
    "--platform manylinux2014_x86_64",
    "--only-binary=:all:",
    "--python-version 3.12",
    "--implementation cp",
    "--abi cp312",
)


@pytest.mark.parametrize("flag", _REQUIRED_FLAGS)
def test_build_script_pins_cross_compile_flag(build_script_text: str, flag: str) -> None:
    """Each cross-compile ``pip`` flag is present in the build script."""
    assert flag in build_script_text, (
        f"build_lambda_package.sh must pin {flag!r} so the Lambda bundle is "
        "cross-compiled for x86_64 Linux / cpython 3.12 (project rules §8: the "
        "operator builds on macOS/ARM64)."
    )


def test_build_script_targets_the_staging_dir(build_script_text: str) -> None:
    """The install still targets the staging dir (a flat, importable bundle)."""
    assert '--target "$BUILD_DIR"' in build_script_text


def test_build_script_installs_both_pinned_deps(build_script_text: str) -> None:
    """growwapi + pyotp are installed via their pinned-version variables."""
    assert '"$GROWWAPI_VERSION"' in build_script_text
    assert '"$PYOTP_VERSION"' in build_script_text


def test_build_script_does_not_pip_install_boto3(build_script_text: str) -> None:
    """boto3 is on the AWS runtime and must NOT be bundled into the zip.

    boto3 is legitimately NAMED in a comment ("boto3 is deliberately NOT
    installed"); this asserts it is never a pip-install ARGUMENT — no
    non-comment, non-echo line references it.
    """
    for raw in build_script_text.splitlines():
        code = raw.split("#", 1)[0]  # strip any trailing/leading comment
        if code.lstrip().startswith("echo"):
            continue
        assert "boto3" not in code, f"boto3 must not be installed: {raw!r}"
