"""MOOT pins — the dashboard quota-verdict phone alert stays closed as MOOT.

DEC-QUOTA-VERDICT-MOOT-001 established the dormancy; DEC-ORCH-DEAD-SCAFFOLD-
DELETE-001 (MIGRATION.md #1969) then DELETED the dormant
``DailyQuotaTracker`` implementation outright — the zero-construction AST pin
this file once carried is now satisfied STRUCTURALLY (the class no longer
exists to construct). What remains load-bearing, and stays pinned here:

* ``DailyQuotaExceeded`` / ``QuotaInvariantError`` keep their byte-stable
  import path and still classify as pull halts (#1591's "exception sets are
  NOT narrowed" pin — both ingest spines' global-stop sets + the Telegram
  halt matcher depend on it);
* the ``stop_quota`` until-clean brake stays quota-silent under the
  production ``quota_exhausted=None`` wiring;
* the quota verdict, were it ever reached, returns via the SUCCESS branch —
  display-only, no phone alert (the #889 resolution);
* the deleted informational quota surface (route/gauge/panel keys) stays
  deleted;
* the ONE re-wire point survives as the structural ``QuotaGate`` Protocol on
  ``RateController(quota_tracker=)`` / ``AdaptiveTokenBucket.quota_tracker``
  — a future capped vendor implements the Protocol (this file pins its
  member surface), never resurrects the deleted class.

This file is ALSO the ci.yml per-module covering suite for the trimmed
``brutex.orchestrator.quota_tracker`` (exceptions + Protocol).
"""

from __future__ import annotations

import pytest
from brutex.adapters.ingest_common import UntilCleanRound, run_until_clean
from brutex.dashboard import pull_jobs
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.pull_progress import STATE_DONE, PullProgress
from brutex.orchestrator import quota_tracker
from brutex.orchestrator.quota_tracker import (
    DailyQuotaExceeded,
    QuotaGate,
    QuotaInvariantError,
)


@pytest.fixture(autouse=True)
def _disable_account_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the cross-process pull lock (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).

    These pins are about the quota verdict, not the flock guard — mirror the
    ``test_pull_jobs.py`` fixture so no real ``~/.brutex/lake`` lock is taken.
    """
    monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)


class TestQuotaVerdictMootPins:
    """The inertness proof chain, pinned link by link (DEC-QUOTA-VERDICT-MOOT-001)."""

    def test_tracker_class_deleted_structurally(self) -> None:
        """The dormant ``DailyQuotaTracker`` no longer exists to construct.

        DEC-ORCH-DEAD-SCAFFOLD-DELETE-001: the AST zero-construction pin this
        file once carried is superseded by structure — the module exports
        EXACTLY the two live exception types + the ``QuotaGate`` re-wire
        Protocol, nothing else. A future capped vendor implements the
        Protocol; resurrecting a tracker class here fails this pin LOUDLY and
        must consciously revisit DEC-QUOTA-VERDICT-MOOT-001 + #1969.
        """
        assert not hasattr(quota_tracker, "DailyQuotaTracker")
        assert sorted(quota_tracker.__all__) == [
            "DailyQuotaExceeded",
            "QuotaGate",
            "QuotaInvariantError",
        ]

    def test_quota_gate_protocol_surface(self) -> None:
        """The ``QuotaGate`` Protocol carries EXACTLY the consumed members.

        ``acquire_or_raise`` (the bucket's gate call), ``current_count`` /
        ``remaining`` / ``daily_limit`` (the controller's snapshot gauge +
        exhaustion brake) — the deleted tracker's consumed surface, verbatim.
        """
        for member in ("acquire_or_raise", "current_count", "remaining"):
            assert callable(getattr(QuotaGate, member)), member
        assert "daily_limit" in QuotaGate.__annotations__

    def test_exception_shapes_are_byte_stable(self) -> None:
        """The two live exceptions keep their safe-message discipline.

        The halt chain (and the MarkdownV2 alert bodies) read ``str(exc)`` —
        the categorical safe message must survive the module trim unchanged.
        """
        exceeded = DailyQuotaExceeded("daily_limit=3 already reached on 2026-06-13")
        assert str(exceeded) == "Daily vendor API quota exceeded"
        assert DailyQuotaExceeded().safe_message == "Daily vendor API quota exceeded"
        invariant = QuotaInvariantError("persisted quota count is negative: -4")
        assert str(invariant) == "Persisted quota state is corrupt or malformed"

    def test_until_clean_none_hook_never_stops_on_quota(self) -> None:
        """A ``quota_exhausted=None`` hook (the production wiring) never yields stop_quota.

        Closes the composition: no tracker ⇒ no hook is ever bound (or the
        bound hook is constant ``False``) ⇒ ``run_until_clean`` over a
        still-dirty runner exhausts its budget rather than braking on quota.
        """
        rounds = [UntilCleanRound(7, False), UntilCleanRound(7, False), UntilCleanRound(7, False)]
        calls: list[int] = []

        def _runner() -> UntilCleanRound:
            calls.append(len(calls))
            return rounds[len(calls) - 1]

        out = run_until_clean(_runner, max_rounds=3, sleep=lambda _i: None)
        assert out.stopped_on_quota is False
        assert out.verdict == "stop_budget"  # the budget, never the quota, is the brake
        assert calls == [0, 1, 2]

    def test_quota_verdict_report_fires_no_phone_alert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A completed job whose report carries the quota verdict pushes NO alert.

        The exact #889-flagged follow-up, resolved MOOT BY DECISION: the
        dashboard's phone alert fires only on a RAISED global stop
        (``BackgroundPullJob._run``'s except branch → ``is_pull_halt`` →
        ``emit_pull_halt_alert``); a quota VERDICT returns through the success
        branch and is display-only (the amber ``verdict_banner`` lane). Since
        the verdict is unreachable in production (no gate is wired — the
        structural pin above), wiring a
        verdict-path alert would ship an AUTOMATED surface that can never fire
        (Golden Rules #10/#11) — hence display-only is the decided behaviour.
        """
        halts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
        job = BackgroundPullJob(progress=PullProgress(), spawn=lambda fn: fn())

        def work(_on_progress: ProgressHook) -> dict[str, object]:
            # The shape a finished pull report takes on the success branch — an
            # INJECTED quota_exhausted=True report (unreachable live; fabricated
            # here to pin the success-branch no-alert contract).
            return {
                "quota_exhausted": True,
                "verdict": {"kind": "quota_exhausted"},
                "note": "daily quota reached — auto-resume paused",
            }

        assert job.start(work, total=1) is True
        snap = job.snapshot()
        assert snap.state == STATE_DONE  # the verdict returns via the SUCCESS branch
        assert halts == []  # display-only BY DECISION — zero Telegram pushes

    def test_is_pull_halt_still_matches_quota_raises(self) -> None:
        """The retained raise-path net: quota exceptions still classify as halts.

        Guarantees the MOOT cleanup did NOT narrow the exception sets — if a
        future capped vendor re-wires a tracker, its ``DailyQuotaExceeded``
        raise propagates to ``is_pull_halt`` and phone-alerts with zero new
        code (``QuotaInvariantError`` likewise; note the asymmetry: only
        ``DailyQuotaExceeded`` is caught mid-round by ``pull_controller`` —
        an invariant error propagates straight to the halt alert).
        """
        assert pull_jobs.is_pull_halt(DailyQuotaExceeded("daily quota spent")) is True
        assert pull_jobs.is_pull_halt(QuotaInvariantError("corrupt persisted state")) is True


class TestQuotaInformationalSurfaceDeleted:
    """The doubly-dead informational quota surface is GONE (DEC-QUOTA-VERDICT-MOOT-001).

    ``GET /api/v1/quota`` + the ``build_app(quota_tracker=)`` param + the
    /universe gauge + the ``RateLimitPanel`` quota fields only ever served the
    constant ``(0, 0, -1)`` "no tracker" sentinel in production (nothing
    injected a tracker since DEC-GROWW-NO-DAILY-QUOTA-UI-001 #718 removed the
    lazy route build) — deleted as the second, divergent quota implementation.
    """

    def test_quota_route_removed(self) -> None:
        """``GET /api/v1/quota`` is gone and ``build_app`` takes no tracker."""
        pytest.importorskip("fastapi")
        import inspect

        from brutex.dashboard.app import DashboardState, build_app
        from fastapi.testclient import TestClient

        assert "quota_tracker" not in inspect.signature(build_app).parameters
        with TestClient(build_app(DashboardState())) as client:
            assert client.get("/api/v1/quota").status_code == 404

    def test_universe_template_has_no_quota_gauge(self) -> None:
        """The /universe page carries no gauge markup, CSS, or quota poller."""
        from brutex.dashboard.app import (
            _render_universe_html,  # pyright: ignore[reportPrivateUsage]
        )

        html = _render_universe_html()
        for needle in ("quotaGauge", ".quota-gauge", "/api/v1/quota"):
            assert needle not in html, f"deleted quota-gauge surface resurfaced: {needle!r}"

    def test_rate_payloads_carry_no_quota_keys(self) -> None:
        """Neither /api/v1/rate nor /api/v1/pull/rate serialises a quota_* key.

        The wire pin for the panel-field deletion: the payload delta versus the
        pre-decision shape is EXACTLY the three constant ``(0, 0, -1)`` keys.
        """
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import DashboardState, build_app
        from fastapi.testclient import TestClient

        with TestClient(build_app(DashboardState())) as client:
            for route in ("/api/v1/rate", "/api/v1/pull/rate"):
                resp = client.get(route)
                assert resp.status_code == 200
                quota_keys = [k for k in resp.json() if k.startswith("quota_")]
                assert quota_keys == [], f"{route} still serialises {quota_keys}"
