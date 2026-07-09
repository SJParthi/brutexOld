# pyright: reportPrivateUsage=none
"""Unit tests for the CLI boot-time auto-resume decision layer.

DEC-AUTO-RESUME-ON-BOOT-001 (the gated decision) + DEC-PULL-INTENT-RESUME-001
(the ONE decision point with the intent resume) + DEC-AUTO-RESUME-INPROCESS-001
(the in-process legacy resume): the legacy boot resume used to SPAWN
``python -m brutex fetch --until-clean --lake-root <root>`` — a child that was
DOA on every opted-in boot, because the dashboard holds ``instruments.duckdb``
READ-WRITE for its lifetime and the child's own ``InstrumentStore.open`` hit
DuckDB's exclusive cross-process file lock (uncaught ``CatalogError`` → exit 1
in ~0.3 s, stderr inherited and never captured — the 2026-07-09T02:47:24Z
production incident). The child machinery
(``_spawn_auto_resume_pull`` / ``_auto_resume_child_argv`` /
``_validate_auto_resume_argv``) is DELETED; the legacy arm now fires the
injected IN-PROCESS ``resume_legacy`` seam
(``app.state.brutex_resume_legacy`` →
:func:`brutex.dashboard.pull_intent.run_legacy_resume`), which needs no second
store handle — so the lock class is structurally gone.

These tests pin, through the REAL ``_start_auto_resume_on_boot`` daemon-thread
seam: (a) the incident class itself (a REAL rw-held ``instruments.duckdb``
makes a child-process store open exit non-zero — the documented DOA shape);
(b) the fix (the in-process resume fires with ZERO ``subprocess.Popen`` calls,
even while the same store is held); (c) the honest ``error`` verdict when the
in-process seam is unwired (never a false ``resumed``, never a DOA child); and
(d) the intent-exclusivity decision point, unchanged.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex import cli


class _OutstandingLedger:
    """A minimal LedgerLike double reporting one outstanding (FAILED) window."""

    class _Counts:
        total = 4
        planned = 0
        failed = 1
        settled = 3

    def progress_counts(self) -> _Counts:
        return self._Counts()


def _wire_legacy_wants_to_fire(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    """Make the LEGACY arm eager (env ON + outstanding ledger) + spy every Popen.

    Returns the recorded ``subprocess.Popen`` argv list — the behavioural pin
    that the auto-resume path never spawns a child again
    (DEC-AUTO-RESUME-INPROCESS-001). Patching the stdlib ``subprocess.Popen``
    covers both a module-level and a function-local ``import subprocess``.
    """
    import brutex.data.window_ledger_store as ledger_store

    monkeypatch.setenv("BRUTEX_AUTO_RESUME_ON_BOOT", "1")
    monkeypatch.setattr(ledger_store, "load_window_ledger", lambda _root: _OutstandingLedger())
    popen_calls: list[list[Any]] = []

    def _spy_popen(*args: Any, **kwargs: Any) -> Any:
        popen_calls.append(list(args))
        raise AssertionError("auto-resume must never spawn a subprocess (in-process by design)")

    monkeypatch.setattr(subprocess, "Popen", _spy_popen)
    return popen_calls


def _wait_for_verdict(log: mock.Mock, *, timeout: float = 10.0) -> str:
    """Block until the daemon thread logs ``auto_resume.verdict``; return it."""
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        for call in log.info.call_args_list:
            if call.args and call.args[0] == "auto_resume.verdict":
                verdict = call.kwargs["verdict"]
                assert isinstance(verdict, str)
                return verdict
        deadline.wait(0.01)
    raise AssertionError("the auto-resume daemon thread never logged its verdict")


class TestInProcessLegacyResume:
    """DEC-AUTO-RESUME-INPROCESS-001 — the legacy arm fires IN-PROCESS, never a child.

    FAILS-BEFORE / PASSES-AFTER: the pre-fix ``_start_auto_resume_on_boot`` had
    no ``resume_legacy`` parameter (calling it below raises ``TypeError``) and
    its legacy arm called ``_spawn_auto_resume_pull`` — a ``subprocess.Popen``
    the spy here turns into a loud failure.
    """

    def test_legacy_arm_fires_injected_inprocess_resume_no_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        popen_calls = _wire_legacy_wants_to_fire(monkeypatch)
        fired = threading.Event()
        log = mock.Mock()
        cli._start_auto_resume_on_boot(
            tmp_path,
            log,
            intent_store=None,
            resume_pipeline=None,
            resume_legacy=fired.set,
        )
        assert fired.wait(timeout=10.0), "the in-process legacy resume must fire"
        assert _wait_for_verdict(log) == "resumed"
        assert popen_calls == [], "auto-resume must never spawn a subprocess"
        # The in-process arm logs its own honest launch event with the counts.
        events = [c.args[0] for c in log.info.call_args_list if c.args]
        assert "auto_resume.inprocess" in events

    def test_unwired_seam_reads_error_verdict_never_false_resumed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resume_legacy=None (intent-store build failed) ⇒ honest ``error`` verdict.

        The legacy arm raises into ``maybe_resume_on_boot``'s guard, which logs
        ``auto_resume.error`` naming why — never a false ``resumed`` and never
        a DOA subprocess child.
        """
        popen_calls = _wire_legacy_wants_to_fire(monkeypatch)
        log = mock.Mock()
        cli._start_auto_resume_on_boot(
            tmp_path,
            log,
            intent_store=None,
            resume_pipeline=None,
            resume_legacy=None,
        )
        assert _wait_for_verdict(log) == "error"
        assert popen_calls == [], "an unwired seam must not fall back to a subprocess"


class TestIncidentClassRegression:
    """The 2026-07-09 incident shape: a rw-held ``instruments.duckdb`` vs the resume.

    (a) documents the DELETED child's death class — a SECOND process opening the
    store while the dashboard-shaped parent holds it rw exits non-zero on the
    DuckDB exclusive lock; (b) proves the NEW in-process path fires under the
    SAME held lock with zero subprocess spawns.
    """

    def test_child_process_store_open_is_doa_while_dashboard_holds_store(
        self, tmp_path: Path
    ) -> None:
        pytest.importorskip("duckdb", reason="the incident class needs the real DuckDB lock")
        from brutex.data.instrument_store import InstrumentStore

        db = tmp_path / "instruments.duckdb"
        store = InstrumentStore.open(db)  # the dashboard's lifetime rw handle
        try:
            child_code = (
                "from pathlib import Path\n"
                "from brutex.data.instrument_store import InstrumentStore\n"
                f"InstrumentStore.open(Path({str(db)!r}))\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", child_code],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        finally:
            store.close()
        assert proc.returncode != 0, "the child MUST die on the exclusive DuckDB lock"
        # The uncaught CatalogError names the lock conflict — the exact stderr the
        # old fire-and-forget child threw away (inherited, never captured).
        assert "CatalogError" in proc.stderr or "lock" in proc.stderr.lower()

    def test_inprocess_resume_fires_while_dashboard_holds_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("duckdb", reason="the incident class needs the real DuckDB lock")
        from brutex.data.instrument_store import InstrumentStore

        popen_calls = _wire_legacy_wants_to_fire(monkeypatch)
        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        try:
            fired = threading.Event()
            log = mock.Mock()
            cli._start_auto_resume_on_boot(
                tmp_path,
                log,
                resume_legacy=fired.set,
            )
            assert fired.wait(timeout=10.0), (
                "the in-process resume must fire even while the dashboard's own "
                "rw store handle is held — no second store open, no DuckDB lock"
            )
            assert _wait_for_verdict(log) == "resumed"
        finally:
            store.close()
        assert popen_calls == [], "zero subprocess spawns while the store is held"


class TestIntentDecisionPointAtBootSeam:
    """DEC-PULL-INTENT-RESUME-001 — the extended ``_start_auto_resume_on_boot``:
    ONE decision point, driven end-to-end through the REAL daemon-thread seam
    with a REAL ``PullIntentStore`` in a tmp lake root. Intent present ⇒ the
    pipeline resume fires and the legacy in-process resume NEVER fires; no
    intent ⇒ the legacy in-process arm runs as before."""

    def test_intent_present_fires_pipeline_and_never_fires_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard.pull_intent import PullIntentStore, persist_intent_for_launch

        popen_calls = _wire_legacy_wants_to_fire(monkeypatch)
        store = PullIntentStore(tmp_path)
        persist_intent_for_launch(
            store, dry_run=False, symbols="NIFTY", symbol=None, from_date=None, to_date=None
        )
        fired = threading.Event()
        legacy_fired: list[object] = []
        log = mock.Mock()
        cli._start_auto_resume_on_boot(
            tmp_path,
            log,
            intent_store=store,
            resume_pipeline=fired.set,
            resume_legacy=lambda: legacy_fired.append(True),
        )
        assert fired.wait(timeout=10.0), "the pipeline resume must fire on an intent boot"
        assert _wait_for_verdict(log) == "intent_resumed"
        assert legacy_fired == [], "the legacy resume must NOT fire on an intent boot"
        assert popen_calls == []

    def test_no_intent_keeps_legacy_inprocess_arm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard.pull_intent import PullIntentStore

        popen_calls = _wire_legacy_wants_to_fire(monkeypatch)
        pipeline_fired = threading.Event()
        legacy_fired = threading.Event()
        log = mock.Mock()
        cli._start_auto_resume_on_boot(
            tmp_path,
            log,
            intent_store=PullIntentStore(tmp_path),  # no intent file on disk
            resume_pipeline=pipeline_fired.set,
            resume_legacy=legacy_fired.set,
        )
        assert legacy_fired.wait(timeout=10.0), "no intent ⇒ the legacy in-process resume fires"
        assert _wait_for_verdict(log) == "resumed"
        assert not pipeline_fired.is_set(), "the pipeline resume must not fire without an intent"
        assert popen_calls == []
