"""Unit tests for the COMMON current-environment identity leaf (REPL-01).

:mod:`brutex.engine.run_environment` is the ONE home of the ``git_identity``
seam + the ``current_code_commit_sha`` / ``current_indicator_version``
builders (moved out of the CLI shims so the cascade-replay vocabulary gate
reads the SAME strings; since the 2026-07-19 merge-reconcile the leaf carries
main's DEC-SWEEP-RUN-IDENTITY-COMPLETE-001 semantics — explicit ``cwd``,
``-dirty`` suffix, the enclosing-repo tracked-path guard, and the
``fno_math`` token in the indicator string). 100% line + branch: the
git success / failure / empty-output / untracked / dirty arms and the
kernel present / broken / absent arms. The exhaustive ``git_identity``
matrix (runner-injected) lives in ``tests/unit/test_cli_run_identity.py`` —
the CLI seam delegates here, so those tests drive this body too.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import __version__
from brutex.engine import run_environment as re_mod
from brutex.engine.run_environment import (
    current_code_commit_sha,
    current_indicator_version,
    git_identity,
)


class TestCurrentCodeCommitSha:
    def test_real_checkout_returns_the_git_head(self) -> None:
        # This repo IS a checkout — the builder must return the real HEAD,
        # possibly dirty-suffixed (DEC-SWEEP-RUN-IDENTITY-COMPLETE-001: the
        # working tree during a test run may legitimately carry edits).
        pkg_dir = re_mod.__file__.rsplit("/", 1)[0]
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            cwd=pkg_dir,
        ).stdout.strip()
        value = current_code_commit_sha()
        assert value in {expected, f"{expected}-dirty"}
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

    def test_probe_runs_in_the_package_dir_with_tracked_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # R3-RUNID-01: the SHA probe binds THIS module's directory as cwd and
        # passes THIS file as the tracked_path (the enclosing-repo guard).
        from pathlib import Path

        calls: list[dict[str, Any]] = []

        def _capture(argv: list[str], **kwargs: Any) -> Any:
            calls.append({"argv": argv, **kwargs})
            # The status probe answers EMPTY (clean tree); every other probe
            # answers the SHA.
            stdout = "" if tuple(argv)[:2] == ("git", "status") else "a" * 40 + "\n"
            return SimpleNamespace(stdout=stdout)

        monkeypatch.setattr(subprocess, "run", _capture)
        value = current_code_commit_sha()
        assert value == "a" * 40  # clean status stdout "" → no -dirty
        pkg_file = Path(re_mod.__file__).resolve()
        assert calls[0]["cwd"] == pkg_file.parent
        ls = [c for c in calls if tuple(c["argv"])[:2] == ("git", "ls-files")]
        assert ls and str(pkg_file) in ls[0]["argv"]


class _FakeProc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _runner(responses: dict[tuple[str, ...], Any]) -> Any:
    def _run(argv: list[str], **kwargs: Any) -> Any:
        result = responses[tuple(argv)]
        if isinstance(result, Exception):
            raise result
        return result

    return _run


_HEAD = ("git", "rev-parse", "HEAD")
_STATUS = ("git", "status", "--porcelain")
_SHA = "b" * 40


class TestGitIdentity:
    def test_clean_tree_no_dirty_suffix(self) -> None:
        from pathlib import Path

        runner = _runner({_HEAD: _FakeProc(_SHA + "\n"), _STATUS: _FakeProc("")})
        assert git_identity(cwd=Path("/repo"), runner=runner) == _SHA

    def test_dirty_tree_is_suffixed(self) -> None:
        from pathlib import Path

        runner = _runner({_HEAD: _FakeProc(_SHA), _STATUS: _FakeProc(" M x.py\n")})
        assert git_identity(cwd=Path("/repo"), runner=runner) == f"{_SHA}-dirty"

    def test_head_failure_returns_none(self) -> None:
        from pathlib import Path

        runner = _runner({_HEAD: OSError("no git")})
        assert git_identity(cwd=Path("/repo"), runner=runner) is None

    def test_empty_head_returns_none(self) -> None:
        from pathlib import Path

        runner = _runner({_HEAD: _FakeProc("  \n")})
        assert git_identity(cwd=Path("/repo"), runner=runner) is None

    def test_status_failure_returns_clean_sha(self) -> None:
        from pathlib import Path

        runner = _runner(
            {_HEAD: _FakeProc(_SHA), _STATUS: subprocess.TimeoutExpired(list(_STATUS), 5)}
        )
        assert git_identity(cwd=Path("/repo"), runner=runner) == _SHA

    def test_untracked_path_returns_none(self) -> None:
        from pathlib import Path

        tracked = "/enclosing/site-packages/brutex/engine/run_environment.py"
        ls = ("git", "ls-files", "--error-unmatch", "--", tracked)
        runner = _runner(
            {
                _HEAD: _FakeProc(_SHA),
                ls: subprocess.CalledProcessError(1, list(ls)),
                _STATUS: _FakeProc(""),
            }
        )
        assert (
            git_identity(cwd=Path("/enclosing"), tracked_path=Path(tracked), runner=runner) is None
        )

    def test_tracked_path_returns_sha(self) -> None:
        from pathlib import Path

        tracked = "/repo/brutex/engine/run_environment.py"
        ls = ("git", "ls-files", "--error-unmatch", "--", tracked)
        runner = _runner({_HEAD: _FakeProc(_SHA), ls: _FakeProc(""), _STATUS: _FakeProc("")})
        assert git_identity(cwd=Path("/repo"), tracked_path=Path(tracked), runner=runner) == _SHA


class TestCurrentIndicatorVersion:
    def test_wheels_present_fold_both_kernel_tokens(self) -> None:
        import fno_math
        import trading_core

        assert current_indicator_version() == (
            f"brutex-{__version__}"
            f"+fno_math-{fno_math.__version__}"
            f"+trading_core-{trading_core.__version__}"
        )

    def test_versionless_trading_core_drops_only_its_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fno_math

        # A module object with no __version__ → the AttributeError arm.
        monkeypatch.setitem(sys.modules, "trading_core", SimpleNamespace())  # type: ignore[arg-type]
        assert current_indicator_version() == (
            f"brutex-{__version__}+fno_math-{fno_math.__version__}"
        )

    def test_absent_trading_core_drops_only_its_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fno_math

        # sys.modules[name] = None makes `import trading_core` raise ImportError.
        monkeypatch.setitem(sys.modules, "trading_core", None)  # type: ignore[arg-type]
        assert current_indicator_version() == (
            f"brutex-{__version__}+fno_math-{fno_math.__version__}"
        )

    def test_absent_fno_math_drops_only_its_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import trading_core

        monkeypatch.setitem(sys.modules, "fno_math", None)  # type: ignore[arg-type]
        assert current_indicator_version() == (
            f"brutex-{__version__}+trading_core-{trading_core.__version__}"
        )

    def test_versionless_fno_math_drops_only_its_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import trading_core

        monkeypatch.setitem(sys.modules, "fno_math", SimpleNamespace())  # type: ignore[arg-type]
        assert current_indicator_version() == (
            f"brutex-{__version__}+trading_core-{trading_core.__version__}"
        )

    def test_both_absent_falls_back_to_bare_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)  # type: ignore[arg-type]
        monkeypatch.setitem(sys.modules, "trading_core", None)  # type: ignore[arg-type]
        assert current_indicator_version() == f"brutex-{__version__}"

    def test_moves_with_a_crate_bump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The vocabulary identity: a crate bump (e.g. the 0.3.2 → 0.4.0
        # reshape) MOVES the string — exactly what the REPL-01 gate compares.
        monkeypatch.setitem(sys.modules, "fno_math", None)  # type: ignore[arg-type]
        monkeypatch.setitem(
            sys.modules,
            "trading_core",
            SimpleNamespace(__version__="9.9.9"),  # type: ignore[arg-type]
        )
        assert current_indicator_version() == f"brutex-{__version__}+trading_core-9.9.9"


def test_module_exports() -> None:
    assert set(re_mod.__all__) == {
        "current_code_commit_sha",
        "current_indicator_version",
        "git_identity",
    }


class TestCliLeafIdentityParity:
    """The ONE-SOURCE pin (2026-07-19 merge-reconcile fold hazard).

    Main's DEC-SWEEP-RUN-IDENTITY-COMPLETE-001 rewrote the CLI identity
    plumbing while this branch extracted the identity builders into THIS
    leaf for the REPL-01 cascade-replay gate. The reconcile re-pointed the
    CLI bodies at the leaf (``brutex.cli._git_identity`` is a thin delegator
    over :func:`git_identity`; ``_indicator_version`` delegates to
    :func:`current_indicator_version`) — these pins make a silent
    re-divergence of the two sources (the drift class the replay gate exists
    to refuse) fail loudly.
    """

    def test_cli_and_leaf_resolve_the_same_identity_pair(self) -> None:
        from brutex.cli import _indicator_version, _resolve_code_commit_sha

        # Same repo, same instant: the CLI (rooted at brutex/cli.py) and the
        # leaf (rooted at brutex/engine/run_environment.py) must resolve the
        # SAME (code_commit_sha, indicator_version) pair — the run manifest's
        # recorded identity and the cascade replay's current identity read
        # from one implementation.
        assert _resolve_code_commit_sha() == current_code_commit_sha()
        assert _indicator_version() == current_indicator_version()

    def test_cli_git_identity_is_the_leaf_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The CLI-level name delegates to the leaf's git_identity — patching
        # the LEAF changes the CLI answer (one implementation, never two).
        from pathlib import Path

        from brutex import cli as cli_mod

        monkeypatch.setattr(
            re_mod, "git_identity", lambda *, cwd, tracked_path=None, runner=None: "LEAF-SENTINEL"
        )
        assert cli_mod._git_identity(cwd=Path("/anywhere")) == "LEAF-SENTINEL"
