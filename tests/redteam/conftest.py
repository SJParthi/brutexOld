"""Pytest fixtures for the red-team harness."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
FIXTURES_DIR: Path = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return REPO_ROOT


@pytest.fixture()
def fixtures_dir() -> Path:
    """Absolute path to the redteam fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture()
def temp_brutex_module(tmp_path: Path) -> Path:
    """Create a temporary brutex-like module path for scanner tests.

    Some scanners (e.g., scan-banned-patterns.sh) only check files
    under specific package paths. This fixture places a planted-bug
    fixture under a temp `brutex/_redteam_temp/` path so the scanner
    will inspect it.

    Returns the path to the temp `brutex/` root.
    """
    temp_brutex = tmp_path / "brutex" / "_redteam_temp"
    temp_brutex.mkdir(parents=True)
    (temp_brutex / "__init__.py").touch()
    return tmp_path / "brutex"


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and capture stdout+stderr.

    Returns the CompletedProcess for inspection. Never raises on
    non-zero exit — the caller asserts on `returncode`.
    """
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def tool_available(name: str) -> bool:
    """Check if a CLI tool is on PATH (skip tests that need it)."""
    return shutil.which(name) is not None
