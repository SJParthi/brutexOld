"""Unit tests for the COMMON current-environment identity leaf (REPL-01).

:mod:`brutex.engine.run_environment` is the ONE home of the
``current_code_commit_sha`` / ``current_indicator_version`` builders (moved
out of the CLI shims so the cascade-replay vocabulary gate reads the SAME
strings). 100% line + branch: the git success / failure / empty-output arms
and the trading_core present / broken / absent arms.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import __version__
from brutex.engine import run_environment as re_mod
from brutex.engine.run_environment import current_code_commit_sha, current_indicator_version


class TestCurrentCodeCommitSha:
    def test_real_checkout_returns_the_git_head(self) -> None:
        # This repo IS a checkout — the builder must return the real HEAD.
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        assert current_code_commit_sha() == expected
        assert len(expected) == 40

    def test_git_failure_falls_back_to_the_package_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("no git")

        monkeypatch.setattr(subprocess, "run", _boom)
        assert current_code_commit_sha() == f"brutex-{__version__}"

    def test_git_error_exit_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.CalledProcessError(returncode=128, cmd=args[0])

        monkeypatch.setattr(subprocess, "run", _fail)
        assert current_code_commit_sha() == f"brutex-{__version__}"

    def test_empty_git_output_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="  \n"))
        assert current_code_commit_sha() == f"brutex-{__version__}"


class TestCurrentIndicatorVersion:
    def test_wheel_present_folds_the_crate_version(self) -> None:
        import trading_core

        assert (
            current_indicator_version()
            == f"brutex-{__version__}+trading_core-{trading_core.__version__}"
        )

    def test_versionless_wheel_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A module object with no __version__ → the AttributeError arm.
        monkeypatch.setitem(sys.modules, "trading_core", SimpleNamespace())  # type: ignore[arg-type]
        assert current_indicator_version() == f"brutex-{__version__}"

    def test_absent_wheel_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # sys.modules[name] = None makes `import trading_core` raise ImportError.
        monkeypatch.setitem(sys.modules, "trading_core", None)  # type: ignore[arg-type]
        assert current_indicator_version() == f"brutex-{__version__}"

    def test_moves_with_a_crate_bump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The vocabulary identity: a crate bump (e.g. the 0.3.2 → 0.4.0
        # reshape) MOVES the string — exactly what the REPL-01 gate compares.
        monkeypatch.setitem(
            sys.modules,
            "trading_core",
            SimpleNamespace(__version__="9.9.9"),  # type: ignore[arg-type]
        )
        assert current_indicator_version() == f"brutex-{__version__}+trading_core-9.9.9"


def test_module_exports() -> None:
    assert set(re_mod.__all__) == {"current_code_commit_sha", "current_indicator_version"}
