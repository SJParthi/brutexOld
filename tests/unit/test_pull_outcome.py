"""Unit suite for :mod:`brutex.dashboard.pull_outcome` (DEC-OPERATOR-HEALTH-TRUTH-001).

100% line+branch on the classifier, plus DRIFT PINS proving the string
classification keys match the REAL recorded evidence:

* the market-hours deferral prefix matches the exact message
  :meth:`BackgroundPullJob.start` records on an in-window refusal;
* every :func:`~brutex.dashboard.pull_jobs.is_pull_halt` exception class,
  formatted exactly as the worker records it (``f"{type(exc).__name__}:
  {exc}"``), classifies to a NON-green, NON-idle outcome — a halt can never
  read as a clean completion again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt
from brutex.adapters.error_storm_breaker import VendorDownError
from brutex.adapters.groww.errors import GrowwCredentialDeadError
from brutex.adapters.groww.pull_singleflight import PullAlreadyRunningError
from brutex.dashboard import pull_jobs
from brutex.dashboard.pull_jobs import JOB_KIND_PULL, JOB_KIND_SWEEP, BackgroundPullJob
from brutex.dashboard.pull_outcome import (
    MARKET_HOURS_DEFERRAL_PREFIX,
    OUTCOME_CLEAN,
    OUTCOME_COMPLETED,
    OUTCOME_COMPLETED_WITH_FAILURES,
    OUTCOME_DEFERRED_MARKET_HOURS,
    OUTCOME_ERROR,
    OUTCOME_NEVER_RUN,
    OUTCOME_RUNNING,
    OUTCOME_RUNNING_STALLED,
    OUTCOME_UNKNOWN,
    OUTCOME_WEDGED,
    TONE_BAD,
    TONE_IDLE,
    TONE_OK,
    TONE_WARN,
    LastPullOutcome,
    classify_last_pull_outcome,
)
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt, MarketHoursHalt
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError


def _classify(
    *,
    state: str = "idle",
    detail: str = "",
    failed: int = 0,
    stalled: bool = False,
    wedged: bool = False,
    job_kind: str = JOB_KIND_PULL,
    verdict: dict[str, Any] | None = None,
) -> LastPullOutcome:
    """Call the classifier with defaults (each test overrides its own axis)."""
    return classify_last_pull_outcome(
        state=state,
        detail=detail,
        failed=failed,
        stalled=stalled,
        wedged=wedged,
        job_kind=job_kind,
        verdict=verdict,
    )


def _verdict(kind: str, *, action: str = "do the thing") -> dict[str, Any]:
    """A minimal :meth:`PullVerdict.to_dict`-shaped mapping."""
    return {
        "kind": kind,
        "headline": f"headline for {kind}",
        "action": action,
        "pending_windows": 3,
        "needs_operator": kind != "clean",
    }


class TestIdleState:
    """``idle`` — nothing has run on the slot this process lifetime."""

    def test_never_run_without_verdict(self) -> None:
        out = _classify()
        assert out.outcome == OUTCOME_NEVER_RUN
        assert out.tone == TONE_IDLE
        assert "no pull has run" in out.headline
        assert out.detail == ""

    def test_pre_pull_refusal_verdict_surfaces_red(self) -> None:
        """The credentials REFUSAL is stamped BEFORE any start — never hidden."""
        out = _classify(verdict=_verdict("credentials_missing"))
        assert out.outcome == "verdict_credentials_missing"
        assert out.tone == TONE_BAD
        assert out.headline == "headline for credentials_missing"
        assert "stamped before any pull started" in out.detail


class TestRunningState:
    """``running`` — live / stalled-suspect / wedged, plus the sweep guard."""

    def test_running_pull_is_ok(self) -> None:
        out = _classify(state="running", detail="spot 3/80")
        assert (out.outcome, out.tone) == (OUTCOME_RUNNING, TONE_OK)
        assert out.detail == "spot 3/80"

    def test_running_stalled_suspect_is_warn(self) -> None:
        out = _classify(state="running", stalled=True)
        assert (out.outcome, out.tone) == (OUTCOME_RUNNING_STALLED, TONE_WARN)

    def test_running_wedged_is_bad_and_outranks_stalled(self) -> None:
        out = _classify(state="running", stalled=True, wedged=True)
        assert (out.outcome, out.tone) == (OUTCOME_WEDGED, TONE_BAD)
        assert "restart required" in out.headline

    def test_running_sweep_with_verdict_reports_last_pull_verdict(self) -> None:
        out = _classify(state="running", job_kind=JOB_KIND_SWEEP, verdict=_verdict("clean"))
        assert (out.outcome, out.tone) == (OUTCOME_CLEAN, TONE_OK)
        assert "from the last completed pull's verdict" in out.detail

    def test_running_sweep_without_verdict_is_unknown_idle(self) -> None:
        out = _classify(state="running", job_kind=JOB_KIND_SWEEP)
        assert (out.outcome, out.tone) == (OUTCOME_UNKNOWN, TONE_IDLE)
        assert "sweep" in out.detail


class TestErrorState:
    """``error`` — halts / deferrals / refusals / generic failures, by recorded detail."""

    def test_market_hours_start_deferral_is_warn(self) -> None:
        detail = (
            "deferred — inside NSE market-hours window (08:45-15:35 IST); "
            "pulls run outside market hours only"
        )
        out = _classify(state="error", detail=detail)
        assert (out.outcome, out.tone) == (OUTCOME_DEFERRED_MARKET_HOURS, TONE_WARN)
        assert "market hours" in out.headline
        assert out.detail == detail

    def test_lake_write_halt_disk_full(self) -> None:
        out = _classify(state="error", detail="LakeWriteHalt: lake write halted: disk_full")
        assert (out.outcome, out.tone) == ("halted_disk_full", TONE_BAD)
        assert "disk full" in out.headline

    def test_lake_write_halt_oom(self) -> None:
        out = _classify(state="error", detail="LakeWriteHalt: lake write halted: out_of_memory")
        assert (out.outcome, out.tone) == ("halted_oom", TONE_BAD)

    def test_lake_write_halt_other_reason(self) -> None:
        out = _classify(state="error", detail="LakeWriteHalt: lake write halted: mystery")
        assert (out.outcome, out.tone) == ("halted_lake_write", TONE_BAD)

    @pytest.mark.parametrize(
        ("name", "outcome", "tone"),
        [
            ("MarketHoursHalt", "halted_market_hours", TONE_WARN),
            ("DailyQuotaExceeded", "halted_quota", TONE_BAD),
            ("QuotaInvariantError", "halted_quota", TONE_BAD),
            ("GreeksEnrichPoolBroken", "halted_pool_broken", TONE_BAD),
            ("MassEmptySettleHalt", "halted_mass_empty", TONE_BAD),
            ("VendorDownError", "halted_vendor_down", TONE_BAD),
            ("GrowwCredentialDeadError", "halted_auth", TONE_BAD),
            ("PullAlreadyRunningError", "refused_busy", TONE_WARN),
        ],
    )
    def test_named_halts_classify(self, name: str, outcome: str, tone: str) -> None:
        out = _classify(state="error", detail=f"{name}: something happened")
        assert (out.outcome, out.tone) == (outcome, tone)

    def test_generic_failure_is_error_bad(self) -> None:
        out = _classify(state="error", detail="ValueError: boom")
        assert (out.outcome, out.tone) == (OUTCOME_ERROR, TONE_BAD)
        assert out.headline == "pull FAILED"

    def test_error_from_sweep_kind_is_not_a_pull_outcome(self) -> None:
        out = _classify(state="error", detail="ValueError: boom", job_kind=JOB_KIND_SWEEP)
        assert (out.outcome, out.tone) == (OUTCOME_UNKNOWN, TONE_IDLE)
        assert "sweep" in out.detail


class TestDoneState:
    """``done`` — clean / verdict-refined / with-failures / unclassified / sweep."""

    def test_done_with_failures_is_warn_even_with_clean_verdict(self) -> None:
        """THIS run's own quarantine tally outranks the (possibly older) verdict."""
        out = _classify(state="done", failed=2, verdict=_verdict("clean"))
        assert (out.outcome, out.tone) == (OUTCOME_COMPLETED_WITH_FAILURES, TONE_WARN)
        assert "2 unit(s) failed" in out.headline

    def test_done_clean_verdict_is_green(self) -> None:
        out = _classify(state="done", verdict=_verdict("clean"))
        assert (out.outcome, out.tone) == (OUTCOME_CLEAN, TONE_OK)
        assert out.headline == "headline for clean"
        assert out.detail == "do the thing"

    @pytest.mark.parametrize(
        ("kind", "tone"),
        [
            ("quota_exhausted", TONE_WARN),  # amber
            ("transient_residual", TONE_WARN),  # amber
            ("quarantined_symbols", TONE_WARN),  # grey folds to warn — never green
            ("known_gap_only", TONE_WARN),  # grey
            ("auth_expired", TONE_BAD),  # red
            ("mass_empty_halt", TONE_BAD),  # red
            ("credentials_missing", TONE_BAD),  # red
        ],
    )
    def test_done_nonclean_verdict_colours(self, kind: str, tone: str) -> None:
        out = _classify(state="done", verdict=_verdict(kind))
        assert out.outcome == f"verdict_{kind}"
        assert out.tone == tone

    def test_done_unknown_verdict_kind_degrades_named(self) -> None:
        out = _classify(state="done", verdict=_verdict("martian_kind"))
        assert (out.outcome, out.tone) == (OUTCOME_UNKNOWN, TONE_WARN)
        assert "unreadable" in out.headline

    def test_done_malformed_verdict_shape_degrades_named(self) -> None:
        out = _classify(state="done", verdict={"kind": "clean"})  # missing headline etc.
        assert (out.outcome, out.tone) == (OUTCOME_UNKNOWN, TONE_WARN)

    def test_done_without_verdict_is_completed_not_cleanly(self) -> None:
        out = _classify(state="done")
        assert (out.outcome, out.tone) == (OUTCOME_COMPLETED, TONE_OK)
        assert out.headline == "pull finished"
        assert "no classified verdict" in out.detail
        assert "cleanly" not in out.headline  # the over-claim word is never used here

    def test_done_sweep_with_verdict_carries_provenance(self) -> None:
        out = _classify(state="done", job_kind=JOB_KIND_SWEEP, verdict=_verdict("quota_exhausted"))
        assert out.outcome == "verdict_quota_exhausted"
        assert "from the last completed pull's verdict" in out.detail

    def test_done_sweep_verdict_with_empty_action_detail_is_provenance_only(self) -> None:
        out = _classify(state="done", job_kind=JOB_KIND_SWEEP, verdict=_verdict("clean", action=""))
        assert out.detail == "from the last completed pull's verdict"

    def test_done_sweep_without_verdict_is_unknown_idle(self) -> None:
        out = _classify(state="done", job_kind=JOB_KIND_SWEEP)
        assert (out.outcome, out.tone) == (OUTCOME_UNKNOWN, TONE_IDLE)


class TestWireShape:
    """``to_dict`` — the 4 stable additive wire keys."""

    def test_to_dict_keys_and_values(self) -> None:
        out = _classify()
        data = out.to_dict()
        assert set(data) == {"outcome", "tone", "headline", "detail"}
        assert data["outcome"] == out.outcome
        assert data["tone"] == out.tone
        assert data["headline"] == out.headline
        assert data["detail"] == out.detail


class TestDriftPins:
    """The string keys match the REAL recorded evidence (never re-minted guesses)."""

    def test_market_hours_prefix_matches_real_start_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A REAL in-window ``start()`` records a detail our prefix matches."""
        monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)
        job = BackgroundPullJob(
            lake_root_resolver=lambda: None,
            account_key_resolver=lambda: None,
            market_hours_guard=lambda: True,
        )
        started = job.start(lambda hook: None, total=1)
        assert started is False
        snap = job.snapshot()
        assert snap.state == "error"
        assert snap.detail.startswith(MARKET_HOURS_DEFERRAL_PREFIX)
        out = _classify(state=snap.state, detail=snap.detail)
        assert out.outcome == OUTCOME_DEFERRED_MARKET_HOURS

    @pytest.mark.parametrize(
        "exc",
        [
            LakeWriteHalt("disk_full"),
            LakeWriteHalt("out_of_memory"),
            MarketHoursHalt(),
            DailyQuotaExceeded("daily quota spent"),
            QuotaInvariantError("corrupt persisted state"),
            GreeksEnrichPoolBroken("worker died"),
            MassEmptySettleHalt("mass-empty breaker tripped"),
            VendorDownError("vendor down"),
            GrowwCredentialDeadError("credential dead"),
            PullAlreadyRunningError(Path("/tmp/pull.lock")),
        ],
        ids=lambda exc: type(exc).__name__,
    )
    def test_every_pull_halt_class_reads_degraded_never_green(self, exc: Exception) -> None:
        """Each halt class, recorded exactly as the worker records it, is NON-green."""
        assert pull_jobs.is_pull_halt(exc) or isinstance(exc, PullAlreadyRunningError)
        detail = f"{type(exc).__name__}: {exc}"  # the exact _run fail() format
        out = _classify(state="error", detail=detail)
        assert out.tone in (TONE_WARN, TONE_BAD)
        assert out.outcome not in (OUTCOME_CLEAN, OUTCOME_COMPLETED, OUTCOME_NEVER_RUN)
        # Every mapped halt keeps the recorded evidence in the wire detail.
        assert out.detail == detail
