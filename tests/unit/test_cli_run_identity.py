"""Run-identity completeness for the CLI sweep helpers.

DEC-SWEEP-RUN-IDENTITY-COMPLETE-001 — two confirmed gaps in the fields that
feed ``mask_run_id`` / ``_tuple_digest`` (and therefore the dedup lane):

* ``_git_identity`` / ``_resolve_code_commit_sha`` must resolve THIS repo's HEAD
  regardless of the process working directory (R3-RUNID-01) and must append a
  ``-dirty`` suffix for an uncommitted tree (RIF-2 / audit C9).
* ``_indicator_version`` must encode the ``fno_math`` sweep-sim kernel so an
  ``fno_math`` rebuild re-keys run identity (R3-RUNID-02).

The git subprocess is exercised through an injected ``runner`` seam so every
branch is covered without a live repository; a complementary real-git test
proves working-directory invariance end-to-end.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from brutex import __version__
from brutex.cli import (
    _git_identity,
    _indicator_version,
    _resolve_code_commit_sha,
)


class _FakeProc:
    """Minimal ``subprocess.run`` result stand-in exposing ``.stdout``."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _make_runner(
    responses: dict[tuple[str, ...], _FakeProc | Exception],
    *,
    calls: list[dict[str, Any]] | None = None,
) -> Any:
    """Build a fake runner keyed by the git subcommand tuple.

    Records every call's kwargs into *calls* so tests can assert ``cwd`` was
    passed. A mapped ``Exception`` value is raised to simulate a git failure.
    """

    def _runner(argv: list[str], **kwargs: Any) -> _FakeProc:
        if calls is not None:
            calls.append({"argv": argv, **kwargs})
        key = tuple(argv)
        result = responses[key]
        if isinstance(result, Exception):
            raise result
        return result

    return _runner


_HEAD = ("git", "rev-parse", "HEAD")
_STATUS = ("git", "status", "--porcelain")
_SHA = "a" * 40


# ────────────────────────── _git_identity ──────────────────────────


def test_git_identity_clean_tree_has_no_dirty_suffix() -> None:
    runner = _make_runner({_HEAD: _FakeProc(_SHA + "\n"), _STATUS: _FakeProc("")})
    assert _git_identity(cwd=Path("/repo"), runner=runner) == _SHA


def test_git_identity_dirty_tree_is_suffixed() -> None:
    runner = _make_runner({_HEAD: _FakeProc(_SHA + "\n"), _STATUS: _FakeProc(" M brutex/cli.py\n")})
    assert _git_identity(cwd=Path("/repo"), runner=runner) == f"{_SHA}-dirty"


def test_git_identity_passes_requested_cwd_to_git() -> None:
    calls: list[dict[str, Any]] = []
    runner = _make_runner({_HEAD: _FakeProc(_SHA), _STATUS: _FakeProc("")}, calls=calls)
    _git_identity(cwd=Path("/some/repo/root"), runner=runner)
    # BOTH git invocations run in the requested repo dir, never the process CWD.
    assert [c["cwd"] for c in calls] == [Path("/some/repo/root"), Path("/some/repo/root")]


def test_git_identity_is_invariant_to_process_cwd(tmp_path: Path) -> None:
    seen: list[Path] = []

    def _capturing(argv: list[str], **kwargs: Any) -> _FakeProc:
        seen.append(kwargs["cwd"])
        return _FakeProc(_SHA) if tuple(argv) == _HEAD else _FakeProc("")

    original = os.getcwd()
    try:
        os.chdir(tmp_path)
        a = _git_identity(cwd=Path("/repo"), runner=_capturing)
        sub = tmp_path / "elsewhere"
        sub.mkdir()
        os.chdir(sub)
        b = _git_identity(cwd=Path("/repo"), runner=_capturing)
    finally:
        os.chdir(original)
    assert a == b == _SHA  # identity did not fork with the process CWD
    assert all(p == Path("/repo") for p in seen)


def test_git_identity_head_failure_returns_none() -> None:
    runner = _make_runner(
        {_HEAD: subprocess.CalledProcessError(128, _HEAD), _STATUS: _FakeProc("")}
    )
    assert _git_identity(cwd=Path("/repo"), runner=runner) is None


def test_git_identity_missing_git_returns_none() -> None:
    runner = _make_runner({_HEAD: FileNotFoundError("git"), _STATUS: _FakeProc("")})
    assert _git_identity(cwd=Path("/repo"), runner=runner) is None


def test_git_identity_empty_head_returns_none() -> None:
    runner = _make_runner({_HEAD: _FakeProc("   \n"), _STATUS: _FakeProc("")})
    assert _git_identity(cwd=Path("/repo"), runner=runner) is None


def test_git_identity_status_probe_failure_returns_clean_sha() -> None:
    # HEAD resolved but the dirty probe failed — return the clean SHA rather
    # than fabricating an unverified dirty flag.
    runner = _make_runner({_HEAD: _FakeProc(_SHA), _STATUS: subprocess.TimeoutExpired(_STATUS, 5)})
    assert _git_identity(cwd=Path("/repo"), runner=runner) == _SHA


# ────────────────────── _resolve_code_commit_sha ──────────────────────


def test_resolve_code_commit_sha_uses_package_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Path] = {}

    def _fake(*, cwd: Path, runner: Any = None) -> str:
        captured["cwd"] = cwd
        return _SHA

    monkeypatch.setattr("brutex.cli._git_identity", _fake)
    # Invariant to the process CWD: chdir away and still resolve the brutex pkg dir.
    original = os.getcwd()
    try:
        os.chdir("/")
        assert _resolve_code_commit_sha() == _SHA
    finally:
        os.chdir(original)
    import brutex.cli as cli_mod

    assert captured["cwd"] == Path(cli_mod.__file__).resolve().parent


def test_resolve_code_commit_sha_falls_back_when_no_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brutex.cli._git_identity", lambda *, cwd, runner=None: None)
    assert _resolve_code_commit_sha() == f"brutex-{__version__}"


def test_resolve_code_commit_sha_returns_git_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brutex.cli._git_identity", lambda *, cwd, runner=None: f"{_SHA}-dirty")
    assert _resolve_code_commit_sha() == f"{_SHA}-dirty"


def test_resolve_code_commit_sha_real_git_is_cwd_invariant(tmp_path: Path) -> None:
    """End-to-end: the real git seam yields the SAME identity from any CWD."""
    import brutex.cli as cli_mod

    pkg_dir = Path(cli_mod.__file__).resolve().parent
    if _git_identity(cwd=pkg_dir) is None:
        pytest.skip("not a git checkout — real-git invariance not applicable")
    original = os.getcwd()
    try:
        os.chdir(tmp_path)
        a = _resolve_code_commit_sha()
        os.chdir("/")
        b = _resolve_code_commit_sha()
    finally:
        os.chdir(original)
    assert a == b
    assert a and not a.startswith("brutex-")  # a real SHA, not the fallback


# ─────────────────────────── _indicator_version ───────────────────────────


def test_indicator_version_includes_all_three_kernels() -> None:
    value = _indicator_version()
    assert value.startswith(f"brutex-{__version__}+")
    assert "+fno_math-" in value
    assert "+trading_core-" in value
    # Deterministic order: fno_math token precedes trading_core.
    assert value.index("+fno_math-") < value.index("+trading_core-")


def test_indicator_version_rekeys_vs_the_old_two_token_format() -> None:
    import trading_core

    old_format = f"brutex-{__version__}+trading_core-{trading_core.__version__}"
    assert _indicator_version() != old_format  # fno_math now re-keys identity


def test_indicator_version_drops_missing_fno_math(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "fno_math", None)  # → ImportError on import
    value = _indicator_version()
    assert "fno_math" not in value
    assert value.startswith(f"brutex-{__version__}")
    assert "+trading_core-" in value


def test_indicator_version_drops_missing_trading_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "trading_core", None)  # → ImportError on import
    value = _indicator_version()
    assert "trading_core" not in value
    assert "+fno_math-" in value
