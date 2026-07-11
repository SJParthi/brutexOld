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
from brutex.dashboard.pull_jobs import BackgroundPullJob, RetainedPullState
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
    classify_retained_pull_outcome,
)
from brutex.dashboard.pull_progress import ProgressSnapshot
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt, MarketHoursHalt
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError


def _classify(
    *,
    state: str = "idle",
    detail: str = "",
    failed: int = 0,
    stalled: bool = False,
    wedged: bool = False,
    verdict: dict[str, Any] | None = None,
) -> LastPullOutcome:
    """Call the classifier with defaults (each test overrides its own axis)."""
    return classify_last_pull_outcome(
        state=state,
        detail=detail,
        failed=failed,
        stalled=stalled,
        wedged=wedged,
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

    def test_done_with_market_hours_note_is_deferred_never_green(self) -> None:
        """Hostile-review M3: the pipeline's CAUGHT market-hours stop finishes
        ``done`` with the deferral note as its finish detail — it must read
        deferred even when a (spot-phase) CLEAN verdict was stamped."""
        from brutex.dashboard.pipeline_pull import _market_hours_stop_note

        note = _market_hours_stop_note()
        out = _classify(state="done", detail=note, verdict=_verdict("clean"))
        assert (out.outcome, out.tone) == (OUTCOME_DEFERRED_MARKET_HOURS, TONE_WARN)
        assert out.detail == note

    def test_done_carries_its_own_note_when_no_verdict(self) -> None:
        """The run's own recorded note is never discarded for a canned line."""
        out = _classify(state="done", detail="pipeline stopped after metadata — gate failed")
        assert (out.outcome, out.tone) == (OUTCOME_COMPLETED, TONE_OK)
        assert out.detail == "pipeline stopped after metadata — gate failed"


class TestIdleVerdictDetail:
    """The idle-arm provenance detail law (the pre-pull refusal surface)."""

    def test_idle_refusal_verdict_with_empty_action_reads_provenance_only(self) -> None:
        out = _classify(verdict=_verdict("credentials_missing", action=""))
        assert out.detail == "stamped before any pull started"


def _snap(
    *,
    state: str = "done",
    detail: str = "",
    failed: int = 0,
    stalled: bool = False,
    wedged: bool = False,
) -> ProgressSnapshot:
    """A minimal :class:`ProgressSnapshot` for retained-state tests."""
    return ProgressSnapshot(
        state=state,
        total=1,
        completed=1,
        failed=failed,
        bars=0,
        current="",
        detail=detail,
        updated_at=0.0,
        stalled=stalled,
        wedged=wedged,
    )


class TestRetainedCorrelation:
    """``classify_retained_pull_outcome`` — the ONE retained-state → outcome law
    (hostile-review M2: a verdict colours a run ONLY when their run ids match)."""

    def test_no_retained_state_reads_never_run(self) -> None:
        out = classify_retained_pull_outcome(None, verdict=None, verdict_pull_id="")
        assert out.outcome == OUTCOME_NEVER_RUN
        assert out.tone == TONE_IDLE

    def test_no_retained_state_still_surfaces_a_prepull_refusal_verdict(self) -> None:
        """The never-ran slot's ONE legitimate verdict writer is the pre-pull
        credentials-refusal gate — surfaced with its provenance."""
        out = classify_retained_pull_outcome(
            None, verdict=_verdict("credentials_missing"), verdict_pull_id=""
        )
        assert out.outcome == OUTCOME_UNKNOWN or out.tone in (TONE_WARN, TONE_BAD)
        assert "stamped before any pull started" in out.detail

    def test_matching_run_id_consults_the_verdict(self) -> None:
        retained = RetainedPullState(snapshot=_snap(), pull_id="run-1")
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id="run-1"
        )
        assert out.outcome == OUTCOME_CLEAN
        assert out.tone == TONE_OK

    def test_mismatched_run_id_drops_the_stale_verdict(self) -> None:
        """M2 core: a verdict stamped by an EARLIER run never colours a later
        run that recorded no verdict of its own (the metadata-only shape)."""
        retained = RetainedPullState(snapshot=_snap(), pull_id="run-2")
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id="run-1"
        )
        assert out.outcome == OUTCOME_COMPLETED
        assert out.outcome != OUTCOME_CLEAN

    def test_runless_retained_record_correlates_with_nothing(self) -> None:
        """A deferral/refusal record carries pull_id ``""`` — even an
        empty-for-empty id match is REFUSED (the ``retained.pull_id and``
        guard), so a stale verdict can never colour a run-less record."""
        retained = RetainedPullState(
            snapshot=_snap(
                state="error", detail=f"{MARKET_HOURS_DEFERRAL_PREFIX} (08:45-15:35 IST)"
            ),
            pull_id="",
        )
        out = classify_retained_pull_outcome(
            retained, verdict=_verdict("clean"), verdict_pull_id=""
        )
        assert out.outcome == OUTCOME_DEFERRED_MARKET_HOURS
        assert out.tone == TONE_WARN

    def test_retained_snapshot_axes_thread_through(self) -> None:
        retained = RetainedPullState(
            snapshot=_snap(failed=3, detail="3 unit(s) quarantined"), pull_id="run-3"
        )
        out = classify_retained_pull_outcome(retained, verdict=None, verdict_pull_id="")
        assert out.outcome == OUTCOME_COMPLETED_WITH_FAILURES
        assert out.detail == "3 unit(s) quarantined"


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
        # m5: the deferral is ALSO retained as the last PULL terminal (no run
        # id — no verdict can ever correlate with it).
        retained = job.last_pull_state()
        assert retained is not None
        assert retained.pull_id == ""
        assert retained.snapshot.detail.startswith(MARKET_HOURS_DEFERRAL_PREFIX)

    def test_pipeline_stop_note_matches_the_deferral_prefix(self) -> None:
        """Hostile-review M3 drift pin: the pipeline's CAUGHT market-hours stop
        note (which becomes the ``done`` finish detail via ``_finish_detail``)
        must keep matching the classifier's deferral prefix."""
        from brutex.dashboard.pipeline_pull import _market_hours_stop_note

        assert _market_hours_stop_note().startswith(MARKET_HOURS_DEFERRAL_PREFIX)

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
