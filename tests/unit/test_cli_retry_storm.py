"""Tests for the ``brutex fetch --max-retry-minutes`` flag wiring (DEC-RATE-STORM-WATCHDOG-001).

``brutex/cli.py`` is coverage-omitted, but the retry-storm flag carries real
logic — argument parsing, dispatch threading, validation, and the
:func:`brutex.cli._arm_retry_storm_guard` factory that builds the guard, opens
the hash-chained audit writer, and returns the ingest progress hook — so it is
unit-tested here for correctness.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from brutex import cli
from brutex.orchestrator.audit import read_entries, verify_chain


@dataclass(frozen=True)
class FakeInstrument:
    """Minimal stand-in exposing the ``symbol_name`` the guard registers."""

    symbol_name: str


# ── Argument parsing ────────────────────────────────────────────────────


def test_parser_defaults_max_retry_minutes_to_none() -> None:
    parser = cli.build_parser()
    without = parser.parse_args(["fetch", "--dry-run"])
    with_flag = parser.parse_args(["fetch", "--dry-run", "--max-retry-minutes", "30"])
    assert without.max_retry_minutes is None
    assert with_flag.max_retry_minutes == 30


def test_main_dispatch_threads_max_retry_minutes_into_cmd_fetch() -> None:
    with mock.patch.object(cli, "cmd_fetch", return_value=0) as fetch:
        cli.main(["fetch", "--dry-run", "--max-retry-minutes", "45"])
    _, kwargs = fetch.call_args
    assert kwargs["max_retry_minutes"] == 45


def test_main_dispatch_max_retry_minutes_defaults_none() -> None:
    with mock.patch.object(cli, "cmd_fetch", return_value=0) as fetch:
        cli.main(["fetch", "--dry-run"])
    _, kwargs = fetch.call_args
    assert kwargs["max_retry_minutes"] is None


# ── Validation ──────────────────────────────────────────────────────────


def test_cmd_fetch_rejects_non_positive_minutes(tmp_path: Path) -> None:
    # Validation runs before any heavy ingest import, so a bad value returns
    # the user-error code immediately.
    rc = cli.cmd_fetch(
        tmp_path,
        None,
        None,
        "all",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=5,
        max_retry_minutes=0,
    )
    assert rc == cli.EXIT_USER_ERROR


# ── _arm_retry_storm_guard factory ──────────────────────────────────────


def test_arm_guard_returns_none_when_flag_unset(tmp_path: Path) -> None:
    with contextlib.ExitStack() as stack:
        hook = cli._arm_retry_storm_guard(
            max_retry_minutes=None,
            lake_root=tmp_path,
            instruments=[FakeInstrument("NIFTY")],
            intervals=["1minute"],
            stack=stack,
        )
    assert hook is None


def test_arm_guard_builds_hook_audits_terminal_failure(tmp_path: Path) -> None:
    with contextlib.ExitStack() as stack:
        hook = cli._arm_retry_storm_guard(
            max_retry_minutes=30,
            lake_root=tmp_path,
            instruments=[FakeInstrument("NIFTY")],
            intervals=["1minute", "1day"],
            stack=stack,
        )
        assert hook is not None
        # A success outcome must not give up; a terminal failure must.
        hook("NIFTY 1minute", 375, False, False)
        hook("NIFTY 1minute", 0, True, False)
        hook("NIFTY 1day", 0, True, False)  # 2/2 windows of NIFTY → 100% → symbol row

    # The audit file was opened under <lake>/audit/ and its chain verifies.
    audit_files = list((tmp_path / "audit").glob("retry-storm-*.jsonl"))
    assert len(audit_files) == 1
    audit_path = audit_files[0]
    assert verify_chain(audit_path) == 3  # 2 give-ups + 1 symbol-failure
    actions = [e.action for e in read_entries(audit_path)]
    assert actions == ["RETRY_GIVE_UP", "RETRY_GIVE_UP", "SYMBOL_WINDOW_FAILURE"]
