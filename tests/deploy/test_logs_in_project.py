"""Tests for the in-project log symlink (DEC-LOGS-IN-PROJECT-001).

``./up.sh`` creates/refreshes an idempotent symlink ``<repo>/logs`` → the
resolved log DIRECTORY (the same ``resolve_log_file`` precedence the runtime
uses), so clicking ▶ BruteX in IntelliJ shows the live log folder + file
INSIDE the project tree — the default log home ``~/.brutex/logs`` is invisible
to the IDE. The contract:

- a fresh link is created (and the log dir mkdir'd so it never dangles);
- a re-run is a no-op (idempotent);
- a stale symlink (a changed ``BRUTEX_LOG_DIR``) is REPOINTED;
- a REAL directory/file named ``logs`` is NEVER clobbered (warn + leave it);
- ``<repo>/logs`` is git-ignored;
- one honest ``logs → <repo>/logs → <real dir>`` line is printed.

Static assertions follow the proven ``tests/deploy`` pattern
(``test_up_bootstrap.py``); the BEHAVIOURAL cases run the hermetic internal
``--link-logs`` hook (the ``--print-lake-detect`` twin) against a COPY of
``up.sh`` in a tmp "repo" with a tmp ``$HOME`` — no pip, no cargo, no network,
nothing outside the tmp dirs is touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UP_SH = _REPO_ROOT / "up.sh"
_GITIGNORE = _REPO_ROOT / ".gitignore"


@pytest.fixture(scope="module")
def up_text() -> str:
    """Return the full source of ``up.sh``."""
    return _UP_SH.read_text(encoding="utf-8")


def _run_link_logs(
    repo: Path, home: Path, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the hermetic ``--link-logs`` hook of a COPIED up.sh in *repo*."""
    script = repo / "up.sh"
    if not script.exists():
        script.write_text(_UP_SH.read_text(encoding="utf-8"), encoding="utf-8")
        script.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(  # fixed argv, hermetic tmp dirs, no network
        ["bash", str(script), "--link-logs"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Behavioural — the hermetic --link-logs hook against a tmp repo + tmp HOME.
# --------------------------------------------------------------------------- #
def test_fresh_link_created_and_never_dangles(tmp_path: Path) -> None:
    """A fresh run creates ``<repo>/logs`` → ``$HOME/.brutex/logs`` (dir pre-made)."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    proc = _run_link_logs(repo, home)
    assert proc.returncode == 0, proc.stderr
    link = repo / "logs"
    assert link.is_symlink()
    target = Path(os.readlink(link))
    assert target == home / ".brutex" / "logs"
    assert target.is_dir(), "the log dir is mkdir'd first so the link never dangles"
    assert f"logs → {link} → {target}" in proc.stdout


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    """A second run leaves the correct link untouched and prints the same line."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    first = _run_link_logs(repo, home)
    second = _run_link_logs(repo, home)
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert Path(os.readlink(repo / "logs")) == home / ".brutex" / "logs"


def test_stale_symlink_is_repointed(tmp_path: Path) -> None:
    """An existing symlink pointing elsewhere is repointed to the resolved dir."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    stale_target = tmp_path / "somewhere-else"
    stale_target.mkdir()
    (repo / "logs").symlink_to(stale_target)
    proc = _run_link_logs(repo, home)
    assert proc.returncode == 0, proc.stderr
    assert Path(os.readlink(repo / "logs")) == home / ".brutex" / "logs"
    assert "logs → " in proc.stdout


def test_real_directory_is_never_clobbered(tmp_path: Path) -> None:
    """A REAL dir named ``logs`` is left untouched — warn + honest plain path line."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    real = repo / "logs"
    real.mkdir()
    keep = real / "keep.txt"
    keep.write_text("precious", encoding="utf-8")
    proc = _run_link_logs(repo, home)
    assert proc.returncode == 0, proc.stderr
    assert not real.is_symlink(), "a real directory must never be replaced"
    assert keep.read_text(encoding="utf-8") == "precious"
    assert "WARNING" in proc.stdout and "NOT a symlink" in proc.stdout
    assert f"logs → {home / '.brutex' / 'logs' / 'brutex.log'}" in proc.stdout


def test_real_file_is_never_clobbered(tmp_path: Path) -> None:
    """A REAL plain file named ``logs`` is left untouched too."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    real = repo / "logs"
    real.write_text("not a dir", encoding="utf-8")
    proc = _run_link_logs(repo, home)
    assert proc.returncode == 0, proc.stderr
    assert not real.is_symlink()
    assert real.read_text(encoding="utf-8") == "not a dir"
    assert "WARNING" in proc.stdout


def test_env_override_wins_and_repoints(tmp_path: Path) -> None:
    """``BRUTEX_LOG_DIR`` redirects the link — same precedence as the runtime."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    custom = tmp_path / "custom-logs"
    proc = _run_link_logs(repo, home, {"BRUTEX_LOG_DIR": str(custom)})
    assert proc.returncode == 0, proc.stderr
    assert Path(os.readlink(repo / "logs")) == custom
    # A later default run repoints back (the stale-symlink path end to end).
    proc2 = _run_link_logs(repo, home)
    assert Path(os.readlink(repo / "logs")) == home / ".brutex" / "logs"
    assert proc2.returncode == 0


def test_persisted_dotenv_log_dir_is_honoured(tmp_path: Path) -> None:
    """A ``~/.brutex/.env`` ``BRUTEX_LOG_DIR`` line drives the link target."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    (home / ".brutex").mkdir(parents=True)
    persisted = tmp_path / "persisted-logs"
    (home / ".brutex" / ".env").write_text(f"BRUTEX_LOG_DIR={persisted}\n", encoding="utf-8")
    proc = _run_link_logs(repo, home)
    assert proc.returncode == 0, proc.stderr
    assert Path(os.readlink(repo / "logs")) == persisted


# --------------------------------------------------------------------------- #
# Static — wiring, gitignore, bash-3.2 discipline.
# --------------------------------------------------------------------------- #
def test_link_refresh_runs_on_every_up_path(up_text: str) -> None:
    """``link_repo_logs`` fires on BOTH fast paths + the boot/no-start handoff."""
    # 1 definition + 1 internal hook + 2 fast paths + 1 pre-boot call.
    calls = [
        ln
        for ln in up_text.splitlines()
        if ln.strip() == "link_repo_logs" or ln.strip().startswith("link_repo_logs()")
    ]
    assert len(calls) == 5, (
        "link_repo_logs must be defined once and invoked on the internal hook, "
        f"both 'already ready' fast paths, and before the boot handoff — got {calls}"
    )
    # The pre-boot call sits BEFORE the NO_START branch so --no-start gets it too.
    assert up_text.index('if [ "$NO_START" -eq 1 ]') > up_text.rindex("\nlink_repo_logs\n")


def test_link_helper_uses_resolve_log_file_precedence(up_text: str) -> None:
    """The link target derives from resolve_log_file — ONE precedence, no fork."""
    helper = up_text[up_text.index("link_repo_logs()") :]
    helper = helper[: helper.index("\n}")]
    assert 'log_file="$(resolve_log_file)"' in helper
    assert 'log_dir="$(dirname "$log_file")"' in helper
    assert '"$REPO_ROOT/logs"' in helper
    assert "mkdir -p" in helper, "the log dir is created first so the link never dangles"


def test_link_helper_never_hard_fails(up_text: str) -> None:
    """A failed ln/rm degrades to a plain logs line — the boot is never broken."""
    helper = up_text[up_text.index("link_repo_logs()") :]
    helper = helper[: helper.index("\n}")]
    assert "failhard" not in helper, "in-project visibility is a courtesy, not a gate"
    assert helper.count("return 0") >= 3, "every degrade path returns cleanly"


def test_repo_logs_symlink_is_gitignored() -> None:
    """``/logs`` is ignored so the machine-local pointer can never be committed."""
    lines = [ln.strip() for ln in _GITIGNORE.read_text(encoding="utf-8").splitlines()]
    assert "/logs" in lines


def test_no_bash4_constructs_in_new_helper(up_text: str) -> None:
    """The helper stays bash-3.2-safe (macOS default) — no arrays, no bash-4isms."""
    helper = up_text[up_text.index("link_repo_logs()") :]
    helper = helper[: helper.index("\n}")]
    for banned in ("mapfile", "readarray", "declare -A", "[@]", "[*]", "|&"):
        assert banned not in helper, f"bash-4-only construct found: {banned!r}"
