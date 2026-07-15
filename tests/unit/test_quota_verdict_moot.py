"""MOOT pins — the dashboard quota-verdict phone alert is closed as MOOT.

DEC-QUOTA-VERDICT-MOOT-001. Groww backtesting has NO daily quota
(``GROWW_DAILY_QUOTA_UNVERIFIED=None``, operator-confirmed 2026-06-13 —
CLAUDE.md §2 Rule 6 / §6), so the whole quota ENFORCEMENT chain is
production-inert by construction:

* ZERO ``DailyQuotaTracker(...)`` construction sites exist in ``brutex/``
  production source (both controller build sites —
  ``pull_controller._build_live_controller`` per DEC-GROWW-NO-DAILY-QUOTA-001
  and the CLI ``cmd_fetch`` — omit the tracker);
* therefore ``RateController.quota_exhausted()`` is constant ``False`` and
  ``DailyQuotaExceeded`` / ``QuotaInvariantError`` can never be raised by a
  live pull;
* therefore the ``stop_quota`` until-clean brake and the
  ``kind="quota_exhausted"`` verdict are unreachable from any live dashboard
  pull, and the #889-flagged "verdict-driven phone alert" follow-up guards a
  doubly-dead path — resolved MOOT, not wired.

These pins convert that dormancy into a permanent CI invariant: a future PR
that re-wires a tracker fails the AST pin LOUDLY and must consciously revisit
DEC-QUOTA-VERDICT-MOOT-001 (the raise-path phone alert —
``is_pull_halt`` → ``emit_pull_halt_alert``, #889 — then covers the quota
exception with zero new code, which is why no verdict-path alert is added).
The vendor-neutral enforcement seam (``RateController(quota_tracker=)`` +
the verdict kind + the amber banner lane) is KEPT dormant by decision.
"""

from __future__ import annotations

import ast
from pathlib import Path

import brutex
import pytest
from brutex.adapters.ingest_common import UntilCleanRound, run_until_clean
from brutex.dashboard import pull_jobs
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.pull_progress import STATE_DONE, PullProgress
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError

_PACKAGE_ROOT = Path(brutex.__file__).resolve().parent

#: The ONE sanctioned production ``DailyQuotaTracker`` construction site — the
#: Dhan feed's session factory (DEC-DHAN-SPOT-PULL-001 / ADR-0022; MIGRATION.md
#: entry #1794). Dhan, unlike Groww, HAS a real vendor-documented Data-API cap
#: (100,000 req/day — ``DHAN_DAILY_QUOTA``), so its tracker is a REAL quota not
#: a phantom: the Groww MOOT proof chain below is untouched (the Groww
#: controller build sites still wire NO tracker), and a Dhan
#: ``DailyQuotaExceeded`` raise phone-alerts with zero new code via the
#: retained raise-path net (``is_pull_halt`` → ``emit_pull_halt_alert``, #889 —
#: pinned by ``test_is_pull_halt_still_matches_quota_raises``).
_SANCTIONED_QUOTA_TRACKER_SITES = frozenset({"adapters/dhan/live_controller.py"})


@pytest.fixture(autouse=True)
def _disable_account_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the cross-process pull lock (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).

    These pins are about the quota verdict, not the flock guard — mirror the
    ``test_pull_jobs.py`` fixture so no real ``~/.brutex/lake`` lock is taken.
    """
    monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)


def _constructs_daily_quota_tracker(tree: ast.AST) -> bool:
    """True iff the module contains a ``DailyQuotaTracker(...)`` Call node."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "DailyQuotaTracker") or (
                isinstance(func, ast.Attribute) and func.attr == "DailyQuotaTracker"
            ):
                return True
    return False


class TestQuotaVerdictMootPins:
    """The inertness proof chain, pinned link by link (DEC-QUOTA-VERDICT-MOOT-001)."""

    def test_no_production_quota_tracker_construction(self) -> None:
        """AST pin (the G3 #1492 idiom): NO GROWW-path module constructs a tracker.

        The root of the whole proof: with zero Groww-path construction sites,
        ``RateController.quota_exhausted()`` is constant ``False`` (pinned by
        ``test_default_factory_builds_a_real_controller``) and the quota
        exceptions have zero Groww raise sites, so the ``quota_exhausted``
        verdict is unreachable from any live Groww pull. The ONE sanctioned
        exception is the Dhan feed's session factory
        (``_SANCTIONED_QUOTA_TRACKER_SITES`` — Dhan has a REAL documented
        100k/day cap, DEC-DHAN-SPOT-PULL-001); the pin additionally asserts
        that sanctioned site still constructs one, so a stale carve-out cannot
        silently widen the allowlist.
        """
        offenders: list[str] = []
        sanctioned_seen: list[str] = []
        for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if _constructs_daily_quota_tracker(tree):
                rel = path.relative_to(_PACKAGE_ROOT).as_posix()
                if rel in _SANCTIONED_QUOTA_TRACKER_SITES:
                    sanctioned_seen.append(rel)
                else:
                    offenders.append(rel)
        assert offenders == [], (
            f"production modules construct DailyQuotaTracker: {offenders} — a tracker "
            "has been re-wired, so the quota-verdict MOOT decision "
            "(DEC-QUOTA-VERDICT-MOOT-001) must be consciously revisited: the "
            "stop_quota brake + the quota_exhausted verdict come back to life, and "
            "the raise-path phone alert (is_pull_halt → emit_pull_halt_alert, #889) "
            "covers the DailyQuotaExceeded raise automatically."
        )
        assert sorted(sanctioned_seen) == sorted(_SANCTIONED_QUOTA_TRACKER_SITES), (
            "the sanctioned Dhan tracker site list is stale — "
            f"expected {sorted(_SANCTIONED_QUOTA_TRACKER_SITES)}, saw {sorted(sanctioned_seen)}; "
            "trim the allowlist if the Dhan factory moved or was deleted."
        )

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
        the verdict is unreachable in production (the AST pin above), wiring a
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
