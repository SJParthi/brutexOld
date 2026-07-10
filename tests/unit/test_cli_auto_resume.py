# pyright: reportPrivateUsage=none
"""Unit tests for the CLI boot-time auto-resume decision layer.

DEC-AUTO-RESUME-ON-BOOT-001 (the gated decision) + DEC-PULL-INTENT-RESUME-001
(the ONE decision point with the intent resume) + DEC-AUTO-RESUME-INPROCESS-001
(the in-process legacy resume): the legacy boot resume used to SPAWN
``python -m brutex fetch --until-clean --lake-root <root>`` — a child that was
DOA on every opted-in boot, because BOTH DuckDB files its ``cmd_fetch`` must
open are exclusively locked against it: ``<lake>/catalog.duckdb`` FIRST
(``Catalog.open`` before ``_resolve_brute_force_instruments``; held by the
dashboard's boot-window L3 lake-heal sweeper — the 2026-07-09T02:47:24Z
production traceback's actual first-raising site, ``catalog.py``
``duckdb.connect``), and one step later ``<lake>/instruments.duckdb``, which
the dashboard holds READ-WRITE for its LIFETIME (uncaught ``CatalogError`` →
exit 1 in ~0.3 s either way, stderr inherited and never captured). The child
machinery (``_spawn_auto_resume_pull`` / ``_auto_resume_child_argv`` /
``_validate_auto_resume_argv``) is DELETED; the legacy arm now fires the
injected IN-PROCESS ``resume_legacy`` seam
(``app.state.brutex_resume_legacy`` →
:func:`brutex.dashboard.pull_intent.run_legacy_resume`), which opens no second
catalog/store handle — so the whole lock class is structurally gone.

These tests pin, through the REAL ``_start_auto_resume_on_boot`` daemon-thread
seam: (a) the incident class itself (a REAL rw-held ``catalog.duckdb`` AND a
REAL rw-held ``instruments.duckdb`` each make a child-process open exit
non-zero — BOTH documented DOA arms); (b) the fix (the in-process resume fires
with ZERO ``subprocess.Popen`` calls, even while the store is held); (c) the
honest ``error`` verdict when the in-process seam is unwired (never a false
``resumed``, never a DOA child); and (d) the intent-exclusivity decision
point, unchanged.
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
    """The 2026-07-09 incident shape: dashboard-held DuckDB locks vs the resume.

    (a)+(b) document the DELETED child's death class on BOTH files its
    ``cmd_fetch`` had to open — ``catalog.duckdb`` (opened FIRST; the incident
    traceback's actual first-raising site, held by the dashboard's boot-window
    L3 lake-heal sweeper) and ``instruments.duckdb`` (opened one step later;
    held by the dashboard READ-WRITE for its lifetime) — a SECOND process
    opening either while a dashboard-shaped parent holds it exits non-zero on
    the DuckDB exclusive lock; (c) proves the NEW in-process path fires under
    the SAME held lock with zero subprocess spawns.
    """

    @staticmethod
    def _run_child(child_code: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", child_code],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_child_process_catalog_open_is_doa_while_dashboard_sweeps(self, tmp_path: Path) -> None:
        """The incident's FIRST-raising arm: ``Catalog.open(<lake>/catalog.duckdb)``.

        ``cmd_fetch`` opens the catalog BEFORE ``_resolve_brute_force_instruments``;
        the dashboard's boot-window L3 lake-heal sweeper holds the same file —
        the production traceback died here (``catalog.py`` ``duckdb.connect``).
        """
        pytest.importorskip("duckdb", reason="the incident class needs the real DuckDB lock")
        from brutex.data.catalog import Catalog

        db = tmp_path / "catalog.duckdb"
        # The L3 sweeper's boot-window handle — HELD across the child spawn.
        with Catalog.open(db):
            proc = self._run_child(
                "from pathlib import Path\n"
                "from brutex.data.catalog import Catalog\n"
                f"Catalog.open(Path({str(db)!r}))\n"
            )
        assert proc.returncode != 0, "the child MUST die on the exclusive DuckDB lock"
        assert "CatalogError" in proc.stderr or "lock" in proc.stderr.lower()

    def test_child_process_store_open_is_doa_while_dashboard_holds_store(
        self, tmp_path: Path
    ) -> None:
        """The DETERMINISTIC arm: the dashboard's LIFETIME-held ``instruments.duckdb``.

        Even when the catalog is momentarily free, the child dies one step
        later here — the store handle is held for the whole dashboard process.
        """
        pytest.importorskip("duckdb", reason="the incident class needs the real DuckDB lock")
        from brutex.data.instrument_store import InstrumentStore

        db = tmp_path / "instruments.duckdb"
        # The dashboard's lifetime rw handle — HELD across the child spawn.
        with InstrumentStore.open(db):
            proc = self._run_child(
                "from pathlib import Path\n"
                "from brutex.data.instrument_store import InstrumentStore\n"
                f"InstrumentStore.open(Path({str(db)!r}))\n"
            )
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
        # The dashboard's own rw handle — HELD while the in-process resume fires.
        with InstrumentStore.open(tmp_path / "instruments.duckdb"):
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
