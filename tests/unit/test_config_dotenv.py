"""Unit tests for :mod:`brutex.config.dotenv` — the minimal ``.env`` loader.

GAP C6 — env persistence (DEC-ENV-PERSIST-SLA-001). The loader must:

* parse a ``KEY=VALUE`` file (ignoring comments / blank lines, stripping quotes),
* NEVER override an already-set process env var (real env wins over file),
* be a no-op when no ``.env`` file exists,
* never log a secret VALUE (only the masked key names).

Everything is injected — the search paths AND the target environment mapping —
so the tests touch no real process env and run with zero side effects.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from brutex.config.dotenv import (
    DEFAULT_DOTENV_NAME,
    apply_dotenv,
    bootstrap_env,
    candidate_dotenv_paths,
    load_dotenv_file,
    parse_dotenv,
)


def test_parse_basic_key_value() -> None:
    """A plain ``KEY=VALUE`` line parses into a single mapping entry."""
    assert parse_dotenv("BRUTEX_GROWW_API_KEY=abc123") == {"BRUTEX_GROWW_API_KEY": "abc123"}


def test_parse_ignores_comments_and_blanks() -> None:
    """Comment lines (``#``) and blank lines are skipped."""
    text = "\n# a comment\n\nBRUTEX_X=1\n   # indented comment\nBRUTEX_Y=2\n"
    assert parse_dotenv(text) == {"BRUTEX_X": "1", "BRUTEX_Y": "2"}


def test_parse_strips_surrounding_quotes() -> None:
    """Single- and double-quoted values have the quotes stripped."""
    text = "A=\"hello world\"\nB='single'\nC=bare\n"
    assert parse_dotenv(text) == {"A": "hello world", "B": "single", "C": "bare"}


def test_parse_strips_export_prefix_and_whitespace() -> None:
    """A leading ``export`` and surrounding whitespace are tolerated."""
    text = "export BRUTEX_FOO = bar \n"
    assert parse_dotenv(text) == {"BRUTEX_FOO": "bar"}


def test_parse_skips_lines_without_equals() -> None:
    """A malformed line with no ``=`` is ignored, not an error."""
    text = "JUST_A_WORD\nGOOD=1\n"
    assert parse_dotenv(text) == {"GOOD": "1"}


def test_parse_skips_empty_key() -> None:
    """A line whose key is empty (``=value``) is ignored."""
    assert parse_dotenv("=novalue\nGOOD=1\n") == {"GOOD": "1"}


def test_parse_keeps_equals_in_value() -> None:
    """Only the FIRST ``=`` splits — equals signs in the value are preserved."""
    assert parse_dotenv("TOKEN=a=b=c") == {"TOKEN": "a=b=c"}


def test_parse_unbalanced_quote_kept_verbatim() -> None:
    """A single leading quote with no matching trailing quote is NOT stripped."""
    assert parse_dotenv('A="oops\n') == {"A": '"oops'}


def test_apply_does_not_override_existing() -> None:
    """An already-set key in the target env WINS over the file value."""
    env = {"BRUTEX_GROWW_API_KEY": "real-from-shell"}
    applied = apply_dotenv({"BRUTEX_GROWW_API_KEY": "file-value", "NEW": "x"}, env=env)
    assert env["BRUTEX_GROWW_API_KEY"] == "real-from-shell"
    assert env["NEW"] == "x"
    assert applied == ("NEW",)


def test_apply_sets_only_missing_keys() -> None:
    """Only keys absent from the target env are set; the applied tuple is sorted."""
    env: dict[str, str] = {"B": "keep"}
    applied = apply_dotenv({"A": "1", "B": "2", "C": "3"}, env=env)
    assert env == {"A": "1", "B": "keep", "C": "3"}
    assert applied == ("A", "C")


def test_apply_empty_mapping() -> None:
    """An empty parsed mapping applies nothing."""
    env: dict[str, str] = {"X": "1"}
    assert apply_dotenv({}, env=env) == ()
    assert env == {"X": "1"}


def test_load_dotenv_file_missing_is_noop(tmp_path: Path) -> None:
    """A non-existent path yields an empty mapping (no error)."""
    assert load_dotenv_file(tmp_path / "nope.env") == {}


def test_load_dotenv_file_reads_existing(tmp_path: Path) -> None:
    """An existing file is read and parsed."""
    p = tmp_path / ".env"
    p.write_text("BRUTEX_X=42\n", encoding="utf-8")
    assert load_dotenv_file(p) == {"BRUTEX_X": "42"}


def test_load_dotenv_file_directory_is_noop(tmp_path: Path) -> None:
    """A path that exists but is a directory is treated as absent (no crash)."""
    d = tmp_path / "as_dir.env"
    d.mkdir()
    assert load_dotenv_file(d) == {}


def test_candidate_paths_order(tmp_path: Path) -> None:
    """Candidate paths are repo-root, cwd, then ``~/.brutex/.env``."""
    repo = tmp_path / "repo"
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    paths = candidate_dotenv_paths(repo_root=repo, cwd=cwd, home=home)
    assert paths == (
        repo / DEFAULT_DOTENV_NAME,
        cwd / DEFAULT_DOTENV_NAME,
        home / ".brutex" / DEFAULT_DOTENV_NAME,
    )


def test_candidate_paths_dedup(tmp_path: Path) -> None:
    """When repo_root == cwd the duplicate candidate is collapsed."""
    same = tmp_path / "same"
    home = tmp_path / "home"
    paths = candidate_dotenv_paths(repo_root=same, cwd=same, home=home)
    assert paths == (
        same / DEFAULT_DOTENV_NAME,
        home / ".brutex" / DEFAULT_DOTENV_NAME,
    )


def test_bootstrap_env_noop_when_absent(tmp_path: Path) -> None:
    """No ``.env`` anywhere ⇒ bootstrap applies nothing and returns ()."""
    env: dict[str, str] = {}
    applied = bootstrap_env(
        env=env,
        repo_root=tmp_path / "repo",
        cwd=tmp_path / "cwd",
        home=tmp_path / "home",
    )
    assert applied == ()
    assert env == {}


def test_bootstrap_env_loads_and_respects_real_env(tmp_path: Path) -> None:
    """Earlier candidates win over later ones, and the real env wins over all files."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    (home / ".brutex").mkdir(parents=True)
    (repo / DEFAULT_DOTENV_NAME).write_text("SHARED=repo\nONLY_REPO=r\n", encoding="utf-8")
    (home / ".brutex" / DEFAULT_DOTENV_NAME).write_text(
        "SHARED=home\nONLY_HOME=h\nREAL=fromfile\n", encoding="utf-8"
    )
    env = {"REAL": "fromshell"}
    applied = bootstrap_env(env=env, repo_root=repo, cwd=repo, home=home)
    # repo wins over home for SHARED; real env wins for REAL.
    assert env["SHARED"] == "repo"
    assert env["ONLY_REPO"] == "r"
    assert env["ONLY_HOME"] == "h"
    assert env["REAL"] == "fromshell"
    assert applied == ("ONLY_HOME", "ONLY_REPO", "SHARED")


def test_bootstrap_env_defaults_to_os_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no ``env`` injected it targets ``os.environ`` (no .env ⇒ no-op)."""
    monkeypatch.delenv("BRUTEX_DOTENV_PROBE", raising=False)
    applied = bootstrap_env(repo_root=tmp_path / "a", cwd=tmp_path / "b", home=tmp_path / "c")
    assert applied == ()
    assert "BRUTEX_DOTENV_PROBE" not in os.environ


def test_bootstrap_env_defaults_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no paths injected it derives cwd/home/repo-root and stays a safe no-op."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "empty-home"))
    env: dict[str, str] = {}
    # No .env file exists under tmp_path → no-op regardless of the derived repo root.
    assert bootstrap_env(env=env) == ()
    assert env == {}


def test_bootstrap_logs_masked_keys_never_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The applied keys are logged but the secret VALUES never appear in any log."""
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = "super-secret-totp-value-do-not-leak"
    (repo / DEFAULT_DOTENV_NAME).write_text(
        f"BRUTEX_GROWW_TOTP_SECRET={secret}\n", encoding="utf-8"
    )
    env: dict[str, str] = {}
    with caplog.at_level(logging.DEBUG):
        applied = bootstrap_env(env=env, repo_root=repo, cwd=repo, home=tmp_path / "h")
    assert applied == ("BRUTEX_GROWW_TOTP_SECRET",)
    assert env["BRUTEX_GROWW_TOTP_SECRET"] == secret
    blob = caplog.text
    assert secret not in blob
