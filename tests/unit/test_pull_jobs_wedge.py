"""BackgroundPullJob wedge-escalation wiring (DEC-PULL-WEDGE-ESCALATE-001, STAGE 1).

The job-LEVEL tests for the WEDGED verdict — the ``_build_revision_probe`` +
``_on_wedged`` wiring in ``brutex/dashboard/pull_jobs.py``. They live in a
``test_pull_jobs*``-stemmed file (moved verbatim out of
``test_dashboard_pull_stall.py``) so the auto-discovery coverage gate
(DEC-CICD-GATE-HONESTY-001), which maps ``pull_jobs.py`` to its stem-matched
``test_pull_jobs*.py`` suite, measures those wiring lines — under the old home the
discovery pairing read them as uncovered (only the static ci.yml gate co-ran the
stall suite). The watchdog spy/factory doubles are IMPORTED from the sibling suite
(§11 COMMON — one source, no copy; precedent:
``test_groww_option_ingest_self_paced``). The watchdog-SIDE wedge verdict tests
(``TestPullWatchdogWedgeEscalation``) stay in ``test_dashboard_pull_stall.py`` —
they cover ``pull_stall.py``, not this module.
"""

# Whitebox tests of module-private state (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from brutex.dashboard.pull_jobs import BackgroundPullJob
from brutex.dashboard.pull_progress import STATE_RUNNING, PullProgress

from tests.unit.test_dashboard_pull_stall import _job, _spy_factory, _sync_spawn


class TestBackgroundPullJobWedge:
    """DEC-PULL-WEDGE-ESCALATE-001 — the job-level STAGE 1 wiring."""

    @pytest.fixture(autouse=True)
    def _disable_account_singleflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.dashboard import pull_jobs

        monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)

    def test_start_wires_wedge_hook_and_probe(self) -> None:
        built, factory = _spy_factory()
        job = _job(factory)
        assert job.start(lambda _h: None, total=1) is True
        spy = built[0]
        assert spy.on_wedged is not None
        # No resolvable lake root (the autouse fixture) → the probe is honestly None
        # (the escalation abstains — never a blind heartbeat-only WEDGED).
        assert spy.revision_probe is None

    def test_wedged_edge_marks_terminal_and_alerts_once_without_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wedge verdict: WEDGED + restart_required + ONE CRITICAL alert; flight HELD."""
        halts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        assert job.start(lambda _h: None, total=1) is True
        built[0].trigger_wedged(last_beat_seconds_ago=3700.0, timeout=600.0)

        assert len(halts) == 1  # exactly ONE CRITICAL alert
        assert isinstance(halts[0], TimeoutError)
        assert "WEDGED" in str(halts[0])
        assert "restart required" in str(halts[0])
        assert "auto-restart is a named follow-up" in str(halts[0])
        snap = job.snapshot()
        assert snap.state == STATE_RUNNING  # the worker is NOT killed
        assert snap.wedged is True
        assert "restart required" in snap.wedge_detail
        assert snap.to_dict()["restart_required"] is True
        # The single-flight stays HELD (the double-run-race fix preserved).
        assert job.running is True
        assert job.start(lambda _h: None, total=1) is False

    def test_late_wedged_edge_for_prior_run_does_not_mark_new_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()
        job = _job(factory)  # sync spawn: the first run completes immediately
        job.start(lambda _h: None, total=1)
        job.start(lambda _h: None, total=1)  # a SECOND run supersedes the first
        assert len(built) == 2
        built[0].trigger_wedged()  # a stale timer edge for the SUPERSEDED run
        assert job.snapshot().wedged is False  # the new run is untouched

    def test_on_wedged_swallows_a_raising_progress_mark(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)

        class _BoomProgress(PullProgress):
            def mark_wedged(self, detail: str) -> None:
                raise RuntimeError("progress boom")

        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=_BoomProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)
        built[0].trigger_wedged()  # must NOT raise out of the timer path

    def test_build_revision_probe_with_lake_root(self, tmp_path: object) -> None:
        """A resolvable lake root builds a live probe over ``lake_revision``."""
        assert isinstance(tmp_path, Path)
        job = BackgroundPullJob(
            progress=PullProgress(),
            spawn=_sync_spawn,
            lake_root_resolver=lambda: tmp_path,
        )
        probe = job._build_revision_probe()
        assert probe is not None
        # An empty lake (no ledger sidecar) reads the honest absent token — the probe is
        # LIVE (it consulted lake_revision), not a fabricated string.
        from brutex.dashboard.lake_revision import lake_revision

        assert probe() == lake_revision(tmp_path)
