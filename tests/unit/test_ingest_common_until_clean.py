"""Tests for `brutex.adapters.ingest_common` — the pure re-pull-until-clean controller.

100% line+branch on the wheel-free pure controller + driver (DEC-TOKEN-PREFLIGHT-001)
via a fake ``round_runner`` (a scripted sequence of :class:`UntilCleanRound`) + an
injected no-op ``sleep`` (no real sleeps). Covers the 4-way ``decide_until_clean``
(continue / stop_clean / stop_dead / stop_budget), the bounded driver loop (the
``max_rounds <= 0`` disable, the first-round no-pre-wait branch, the
break-vs-continue branch), the default bounded exponential backoff, and every
:class:`UntilCleanOutcome` accessor incl. the shared ``pull_verdict`` mapping
(which REUSES ``classify_pull`` from DEC-PULL-VERDICT-001 #485).
"""

from __future__ import annotations

# Whitebox test of module-private backoff constants/helpers (sanctioned per pyproject.toml).
# pyright: reportPrivateUsage=none
from collections.abc import Callable, Sequence

import pytest
from brutex.adapters import ingest_common as mod
from brutex.adapters.ingest_common import (
    DEFAULT_MAX_ROUNDS,
    UntilCleanOutcome,
    UntilCleanRound,
    decide_until_clean,
    run_until_clean,
)
from structlog.typing import EventDict


def _scripted_runner(
    rounds: Sequence[UntilCleanRound],
) -> tuple[Callable[[], UntilCleanRound], list[int]]:
    """Return a ``round_runner`` yielding *rounds* in order + a call-count recorder."""
    calls: list[int] = []

    def _run() -> UntilCleanRound:
        """One scripted pull round — returns the next pre-canned residual."""
        idx = len(calls)
        calls.append(idx)
        return rounds[idx]

    return _run, calls


def _no_sleep() -> tuple[Callable[[int], None], list[int]]:
    """Return a no-op backoff hook + the list of round indices it was called with."""
    seen: list[int] = []

    def _sleep(round_index: int) -> None:
        """Record the round index instead of sleeping."""
        seen.append(round_index)

    return _sleep, seen


# ── decide_until_clean — the pure 4-way controller ──────────────────────────


def test_decide_stop_dead_takes_priority() -> None:
    """A dead token halts even if the residual happens to read 0 (worst-first priority)."""
    assert (
        decide_until_clean(still_failed_windows=0, auth_aborted=True, rounds_used=1, max_rounds=5)
        == "stop_dead"
    )


def test_decide_stop_clean() -> None:
    """Zero residual with a live token ⇒ stop_clean."""
    assert (
        decide_until_clean(still_failed_windows=0, auth_aborted=False, rounds_used=2, max_rounds=5)
        == "stop_clean"
    )


def test_decide_stop_budget() -> None:
    """Residual remaining with the budget spent ⇒ stop_budget."""
    assert (
        decide_until_clean(still_failed_windows=7, auth_aborted=False, rounds_used=5, max_rounds=5)
        == "stop_budget"
    )


def test_decide_continue() -> None:
    """Residual remaining with budget left ⇒ continue."""
    assert (
        decide_until_clean(still_failed_windows=7, auth_aborted=False, rounds_used=1, max_rounds=5)
        == "continue"
    )


# ── decide_until_clean — the auto-resume guards (DEC-AUTO-RESUME-QUOTA-001) ──


def test_decide_stop_quota() -> None:
    """Quota exhausted with residual ⇒ stop_quota (the mandatory auto-resume brake)."""
    assert (
        decide_until_clean(
            still_failed_windows=12,
            auth_aborted=False,
            rounds_used=2,
            max_rounds=50,
            quota_exhausted=True,
        )
        == "stop_quota"
    )


def test_decide_dead_outranks_quota() -> None:
    """A dead token outranks an exhausted quota (worst-first: re-auth is the headline)."""
    assert (
        decide_until_clean(
            still_failed_windows=5,
            auth_aborted=True,
            rounds_used=2,
            max_rounds=50,
            quota_exhausted=True,
        )
        == "stop_dead"
    )


def test_decide_clean_outranks_quota() -> None:
    """A clean ledger wins over an exhausted quota — if we're done, we're done."""
    assert (
        decide_until_clean(
            still_failed_windows=0,
            auth_aborted=False,
            rounds_used=2,
            max_rounds=50,
            quota_exhausted=True,
        )
        == "stop_clean"
    )


def test_decide_stop_no_progress() -> None:
    """Residual unchanged (no progress) with budget + quota left ⇒ stop_no_progress."""
    assert (
        decide_until_clean(
            still_failed_windows=10,
            auth_aborted=False,
            rounds_used=2,
            max_rounds=50,
            made_progress=False,
        )
        == "stop_no_progress"
    )


def test_decide_quota_outranks_no_progress() -> None:
    """Quota is the SPECIFIC reason no progress is possible ⇒ stop_quota wins."""
    assert (
        decide_until_clean(
            still_failed_windows=10,
            auth_aborted=False,
            rounds_used=2,
            max_rounds=50,
            quota_exhausted=True,
            made_progress=False,
        )
        == "stop_quota"
    )


def test_decide_no_progress_outranks_budget() -> None:
    """A stuck round stops on no-progress BEFORE churning to the budget cap."""
    assert (
        decide_until_clean(
            still_failed_windows=10,
            auth_aborted=False,
            rounds_used=5,
            max_rounds=5,
            made_progress=False,
        )
        == "stop_no_progress"
    )


def test_decide_new_kwargs_default_to_prior_behaviour() -> None:
    """Omitting the new kwargs reproduces the pre-feature 4-way controller exactly."""
    # made_progress defaults True + quota_exhausted defaults False ⇒ continue.
    assert (
        decide_until_clean(still_failed_windows=3, auth_aborted=False, rounds_used=1, max_rounds=5)
        == "continue"
    )


# ── run_until_clean — the bounded driver ────────────────────────────────────


def test_run_clean_after_one_round() -> None:
    """A clean first round stops immediately with NO pre-wait (first round = initial pull)."""
    runner, calls = _scripted_runner([UntilCleanRound(0, False)])
    sleep, seen = _no_sleep()

    out = run_until_clean(runner, max_rounds=5, sleep=sleep)

    assert calls == [0]
    assert seen == []  # the first round never pre-waits
    assert out.verdict == "stop_clean"
    assert out.rounds_run == 1
    assert out.clean is True
    assert out.auth_aborted is False
    assert out.stopped_on_budget is False
    assert out.final_still_failed == 0
    assert out.auth_cause is None
    assert out.rounds == (UntilCleanRound(0, False),)


def test_run_recovers_over_multiple_rounds() -> None:
    """Residual shrinks round-by-round and clears; rounds ≥ 2 pre-wait the backoff."""
    runner, calls = _scripted_runner(
        [UntilCleanRound(10, False), UntilCleanRound(3, False), UntilCleanRound(0, False)]
    )
    sleep, seen = _no_sleep()

    out = run_until_clean(runner, max_rounds=5, sleep=sleep)

    assert calls == [0, 1, 2]
    assert seen == [2, 3]  # backoff fired before rounds 2 and 3 only
    assert out.verdict == "stop_clean"
    assert out.rounds_run == 3
    assert out.final_still_failed == 0


def test_run_stop_budget_with_residual() -> None:
    """A storm that never clears within the budget ends stop_budget with residual."""
    runner, calls = _scripted_runner([UntilCleanRound(84, False), UntilCleanRound(40, False)])
    sleep, _seen = _no_sleep()

    out = run_until_clean(runner, max_rounds=2, sleep=sleep)

    assert calls == [0, 1]  # ran the full budget
    assert out.verdict == "stop_budget"
    assert out.rounds_run == 2
    assert out.stopped_on_budget is True
    assert out.clean is False
    assert out.final_still_failed == 40
    assert out.pull_verdict().kind == "transient_residual"


# ── run_until_clean — auto-resume guards (DEC-AUTO-RESUME-QUOTA-001) ─────────


def test_run_stop_quota_pauses_auto_resume() -> None:
    """A quota that runs out after a round halts with stop_quota → quota_exhausted verdict."""
    runner, calls = _scripted_runner(
        [UntilCleanRound(50, False), UntilCleanRound(20, False), UntilCleanRound(0, False)]
    )
    sleep, _seen = _no_sleep()
    # Quota becomes exhausted on the 2nd check (after round 2), before the clean round 3.
    checks = iter([False, True])

    out = run_until_clean(
        runner,
        max_rounds=50,
        sleep=sleep,
        quota_exhausted=lambda: next(checks),
        stop_when_stuck=True,
    )

    assert calls == [0, 1]  # stopped after round 2 — never fired the doomed round 3
    assert out.verdict == "stop_quota"
    assert out.stopped_on_quota is True
    assert out.final_still_failed == 20
    assert out.pull_verdict().kind == "quota_exhausted"
    assert out.pull_verdict().needs_operator is True


def test_run_stop_no_progress_when_stuck() -> None:
    """With stop_when_stuck, a round that shrinks the residual by zero halts the loop."""
    runner, calls = _scripted_runner(
        [UntilCleanRound(10, False), UntilCleanRound(10, False), UntilCleanRound(10, False)]
    )
    sleep, _seen = _no_sleep()

    out = run_until_clean(runner, max_rounds=50, sleep=sleep, stop_when_stuck=True)

    assert calls == [0, 1]  # round 1 (baseline) + round 2 (no shrink) → stop; not 50 rounds
    assert out.verdict == "stop_no_progress"
    assert out.stopped_no_progress is True
    assert out.final_still_failed == 10
    # A stuck-but-live residual still maps to transient_residual (re-pull mops it up).
    assert out.pull_verdict().kind == "transient_residual"


def test_run_no_progress_off_by_default() -> None:
    """Without stop_when_stuck, a constant residual runs to the budget (byte-unchanged)."""
    runner, calls = _scripted_runner([UntilCleanRound(10, False)] * 4)
    sleep, _seen = _no_sleep()

    out = run_until_clean(runner, max_rounds=4, sleep=sleep)

    assert calls == [0, 1, 2, 3]  # the no-progress guard never fired (opt-in only)
    assert out.verdict == "stop_budget"
    assert out.rounds_run == 4


def test_run_auto_resume_drains_over_many_rounds() -> None:
    """Auto-resume keeps re-firing (zero clicks) while making progress, until clean."""
    script = [UntilCleanRound(r, False) for r in (40, 30, 20, 10, 0)]
    runner, calls = _scripted_runner(script)
    sleep, _seen = _no_sleep()

    out = run_until_clean(
        runner,
        max_rounds=50,
        sleep=sleep,
        quota_exhausted=lambda: False,  # quota never the limit
        stop_when_stuck=True,
    )

    assert calls == [0, 1, 2, 3, 4]  # drained across 5 progress rounds, well under the cap
    assert out.verdict == "stop_clean"
    assert out.clean is True
    assert out.final_still_failed == 0


def test_run_stop_dead_halts_loop() -> None:
    """A mid-loop token death halts immediately and surfaces the auth_expired verdict."""
    runner, calls = _scripted_runner(
        [
            UntilCleanRound(20, True, auth_cause="HTTP 401 token expired"),
            UntilCleanRound(0, False),  # never reached
        ]
    )
    sleep, _seen = _no_sleep()

    out = run_until_clean(runner, max_rounds=5, sleep=sleep)

    assert calls == [0]  # halted after the first round
    assert out.verdict == "stop_dead"
    assert out.auth_aborted is True
    assert out.clean is False
    assert out.final_still_failed == 20
    assert out.auth_cause == "HTTP 401 token expired"
    verdict = out.pull_verdict()
    assert verdict.kind == "auth_expired"
    assert verdict.auth_cause == "HTTP 401 token expired"


def test_run_max_rounds_zero_disables() -> None:
    """``max_rounds <= 0`` runs zero rounds and reports a spent budget (disabled no-op)."""
    runner, calls = _scripted_runner([])  # never called
    sleep, seen = _no_sleep()

    out = run_until_clean(runner, max_rounds=0, sleep=sleep)

    assert calls == []
    assert seen == []
    assert out.verdict == "stop_budget"
    assert out.rounds_run == 0
    assert out.rounds == ()
    assert out.final_still_failed == 0  # empty-rounds accessor branch
    assert out.auth_cause is None  # empty-rounds accessor branch
    assert out.pull_verdict().kind == "clean"  # nothing failed → clean tally


def test_run_default_sleep_used_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sleep=None`` wires the default bounded backoff (covers the None branch)."""
    slept: list[float] = []
    monkeypatch.setattr(mod, "_time_sleep", slept.append)
    runner, _calls = _scripted_runner([UntilCleanRound(5, False), UntilCleanRound(0, False)])

    out = run_until_clean(runner, max_rounds=3, sleep=None)

    assert out.rounds_run == 2
    assert len(slept) == 1  # default backoff slept once, before round 2


def test_default_backoff_is_bounded_and_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default round backoff is exponential from round 2 and clamped to the ceiling."""
    slept: list[float] = []
    monkeypatch.setattr(mod, "_time_sleep", slept.append)

    mod._until_clean_default_backoff(2)  # base × 2**0 = 5.0 (first pre-wait, round 2)
    mod._until_clean_default_backoff(3)  # base × 2**1 = 10.0
    mod._until_clean_default_backoff(100)  # clamped to the 60.0 ceiling

    assert slept[0] == pytest.approx(mod._UC_DEFAULT_ROUND_BACKOFF_S)
    assert slept[1] == pytest.approx(mod._UC_DEFAULT_ROUND_BACKOFF_S * 2.0)
    assert slept[2] == pytest.approx(mod._UC_MAX_ROUND_BACKOFF_S)


def test_custom_decide_is_honoured() -> None:
    """An injected *decide* drives the loop (covers the decide-injection seam)."""
    runner, calls = _scripted_runner([UntilCleanRound(99, False)])
    sleep, _seen = _no_sleep()

    # A decide that always stops-clean ends after exactly one round despite residual.
    out = run_until_clean(runner, max_rounds=5, sleep=sleep, decide=lambda **_: "stop_clean")

    assert calls == [0]
    assert out.verdict == "stop_clean"


def test_outcome_to_dict_shape() -> None:
    """``to_dict`` is the stable JSON contract."""
    out = UntilCleanOutcome(
        verdict="stop_budget",
        rounds_run=2,
        rounds=(UntilCleanRound(5, False), UntilCleanRound(3, False)),
    )
    assert out.to_dict() == {
        "verdict": "stop_budget",
        "rounds_run": 2,
        "clean": False,
        "auth_aborted": False,
        "stopped_on_budget": True,
        "stopped_on_quota": False,
        "stopped_no_progress": False,
        "final_still_failed": 3,
    }


def test_outcome_and_round_are_frozen() -> None:
    """``UntilCleanOutcome`` / ``UntilCleanRound`` are immutable value objects."""
    out = UntilCleanOutcome(verdict="stop_clean", rounds_run=0, rounds=())
    rnd = UntilCleanRound(0, False)
    with pytest.raises(AttributeError):
        out.verdict = "stop_dead"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        rnd.still_failed_windows = 9  # type: ignore[misc]


def test_default_max_rounds_is_bounded() -> None:
    """The default outer budget is small + finite (Golden Rule #10 — never infinite)."""
    assert DEFAULT_MAX_ROUNDS == 5


def test_max_rounds_covers_the_empty_terminal_threshold() -> None:
    """INVARIANT (DEC-REALDATA-ONLY-NO-1M-TO-1D-001, MIGRATION.md #852): the until-clean
    OUTER round budget must be >= the cross-pull EMPTY→SKIPPED terminal threshold, so a
    RECENT empty window can actually reach its SKIPPED settlement WITHIN the budget (streak
    bumps once per round; it needs ``EMPTY_TERMINAL_THRESHOLD`` rounds to settle). If the
    two constants are ever re-tuned independently so ``threshold > rounds``, a recent empty
    would hit the round budget BEFORE settling and read ``pending`` forever on the
    completeness chip — this guard pins the coupling the hostile review flagged (PR #804).
    The constants were reverted 10→5 / 10→3 here (still 5 >= 3) when the 1m→1d resample was
    removed (real Groww data only), so the invariant is preserved while the budgets shrink."""
    from brutex.data.window_ledger import EMPTY_TERMINAL_THRESHOLD

    assert DEFAULT_MAX_ROUNDS >= EMPTY_TERMINAL_THRESHOLD


# ── DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001 — the outstanding-windows path ────────


def test_decide_outstanding_continues_when_planned_remain() -> None:
    """outstanding>0 with NO transient failures → continue (the core Fix #2 change).

    The old behaviour stopped CLEAN here (still_failed_windows==0), leaving PLANNED +
    EMPTY windows unfetched. With ``outstanding_windows`` provided, the clean test keys
    on it, so the loop keeps re-pulling.
    """
    assert (
        decide_until_clean(
            still_failed_windows=0,
            auth_aborted=False,
            rounds_used=1,
            max_rounds=10,
            outstanding_windows=2,
        )
        == "continue"
    )


def test_decide_outstanding_zero_is_clean_regardless_of_transient() -> None:
    """outstanding==0 → stop_clean even if the stale transient count is non-zero."""
    assert (
        decide_until_clean(
            still_failed_windows=5,
            auth_aborted=False,
            rounds_used=1,
            max_rounds=10,
            outstanding_windows=0,
        )
        == "stop_clean"
    )


def test_decide_outstanding_none_falls_back_to_transient() -> None:
    """``outstanding_windows=None`` keeps the legacy still_failed_windows clean test."""
    assert (
        decide_until_clean(
            still_failed_windows=0,
            auth_aborted=False,
            rounds_used=1,
            max_rounds=10,
            outstanding_windows=None,
        )
        == "stop_clean"
    )


def _outstanding_round(
    *, outstanding: int, settled: int, streak: int, failed: int = 0
) -> UntilCleanRound:
    return UntilCleanRound(
        still_failed_windows=failed,
        auth_aborted=False,
        outstanding_windows=outstanding,
        settled_windows=settled,
        streak_total=streak,
    )


def test_run_until_clean_drives_planned_to_clean() -> None:
    """PLANNED/EMPTY windows are driven across rounds until outstanding hits 0."""
    sleep, _seen = _no_sleep()
    runner, calls = _scripted_runner(
        [
            _outstanding_round(outstanding=3, settled=0, streak=0),
            _outstanding_round(outstanding=1, settled=2, streak=0),  # settled rose → progress
            _outstanding_round(outstanding=0, settled=3, streak=0),  # clean
        ]
    )
    out = run_until_clean(runner, max_rounds=10, sleep=sleep, stop_when_stuck=True)
    assert out.verdict == "stop_clean"
    assert len(calls) == 3


def test_run_until_clean_empty_streak_counts_as_progress() -> None:
    """A round that only advanced an EMPTY window's streak is NOT a stall.

    Without the streak signal the loop would ``stop_no_progress`` at round 2 (settled
    flat), BEFORE the recent EMPTY window settles to SKIPPED on round 3.
    """
    sleep, _seen = _no_sleep()
    runner, calls = _scripted_runner(
        [
            _outstanding_round(outstanding=1, settled=0, streak=1),
            _outstanding_round(outstanding=1, settled=0, streak=2),  # settled flat, streak rose
            _outstanding_round(outstanding=0, settled=1, streak=0),  # EMPTY→SKIPPED, clean
        ]
    )
    out = run_until_clean(runner, max_rounds=10, sleep=sleep, stop_when_stuck=True)
    assert out.verdict == "stop_clean"
    assert len(calls) == 3


def test_run_until_clean_stops_when_settled_and_streak_flat() -> None:
    """A genuinely-stuck window (e.g. a quarantined symbol's PLANNED) self-terminates.

    settled flat AND streak flat with outstanding>0 → stop_no_progress (bounded, never
    churning to the budget cap — the futile-loop guarantee Fix #3 then excludes at
    resolution time).
    """
    sleep, _seen = _no_sleep()
    runner, calls = _scripted_runner(
        [
            _outstanding_round(outstanding=2, settled=1, streak=0),
            _outstanding_round(outstanding=2, settled=1, streak=0),  # flat → no progress
        ]
    )
    out = run_until_clean(runner, max_rounds=50, sleep=sleep, stop_when_stuck=True)
    assert out.verdict == "stop_no_progress"
    assert len(calls) == 2  # stopped at round 2, NOT 50 rounds of budget churn


def test_run_until_clean_outstanding_runs_to_budget_when_not_stuck_opted_in() -> None:
    """stop_when_stuck=False keeps the budget bound even on the outstanding path."""
    sleep, _seen = _no_sleep()
    runner, calls = _scripted_runner(
        [
            _outstanding_round(outstanding=2, settled=1, streak=0),
            _outstanding_round(outstanding=2, settled=1, streak=0),
        ]
    )
    out = run_until_clean(runner, max_rounds=2, sleep=sleep, stop_when_stuck=False)
    assert out.verdict == "stop_budget"
    assert len(calls) == 2


# ── until_clean.round / until_clean.done observability (DEC-PULL-TELEMETRY-TRIAD-001) ──


def _until_clean_events(logs: list[EventDict], event: str) -> list[EventDict]:
    """The captured structlog events named *event*."""
    return [e for e in logs if e["event"] == event]


def test_round_events_emitted_per_round_with_terminal_done() -> None:
    """A 3-round drive (2 progressing + a clean finish) logs one ``until_clean.round``
    per round (round index, outstanding, made_progress, verdict) + ONE terminal
    ``until_clean.done`` (rounds, final outstanding, verdict)."""
    from structlog.testing import capture_logs

    sleep, _seen = _no_sleep()
    runner, calls = _scripted_runner(
        [
            UntilCleanRound(still_failed_windows=111, auth_aborted=False),
            UntilCleanRound(still_failed_windows=40, auth_aborted=False),
            UntilCleanRound(still_failed_windows=0, auth_aborted=False),
        ]
    )
    with capture_logs() as logs:
        out = run_until_clean(runner, max_rounds=5, sleep=sleep)
    rounds = _until_clean_events(logs, "until_clean.round")
    assert [e["round"] for e in rounds] == [1, 2, 3]
    assert [e["outstanding"] for e in rounds] == [111, 40, 0]
    # The default path never opts into stuck detection → made_progress stays True.
    assert [e["made_progress"] for e in rounds] == [True, True, True]
    assert [e["verdict"] for e in rounds] == ["continue", "continue", "stop_clean"]
    done = _until_clean_events(logs, "until_clean.done")
    assert done == [
        {
            "event": "until_clean.done",
            "log_level": "info",
            "rounds": 3,
            "outstanding": 0,
            "verdict": "stop_clean",
        }
    ]
    # PARITY PIN (Session-B contract): the returned outcome is byte-identical to the
    # pre-logging controller for the same scripted rounds — logging is the only delta.
    assert out.verdict == "stop_clean"
    assert out.rounds_run == 3
    assert len(calls) == 3


def test_round_event_outstanding_prefers_the_ledger_count() -> None:
    """A round carrying ``outstanding_windows`` logs THAT (the genuinely-outstanding
    ledger count), not the legacy still-failed count; made_progress reflects the
    stuck detection when opted in."""
    from structlog.testing import capture_logs

    sleep, _seen = _no_sleep()
    runner, _calls = _scripted_runner(
        [
            _outstanding_round(outstanding=5, settled=1, streak=0, failed=2),
            _outstanding_round(outstanding=5, settled=1, streak=0, failed=2),  # flat → stuck
        ]
    )
    with capture_logs() as logs:
        out = run_until_clean(runner, max_rounds=50, sleep=sleep, stop_when_stuck=True)
    rounds = _until_clean_events(logs, "until_clean.round")
    assert [e["outstanding"] for e in rounds] == [5, 5]  # the ledger count, not failed=2
    assert [e["made_progress"] for e in rounds] == [True, False]
    assert rounds[-1]["verdict"] == "stop_no_progress"
    done = _until_clean_events(logs, "until_clean.done")
    assert len(done) == 1
    assert done[0]["rounds"] == 2
    assert done[0]["outstanding"] == 5
    assert done[0]["verdict"] == "stop_no_progress"
    assert out.verdict == "stop_no_progress"  # parity: verdict unchanged


def test_disabled_budget_still_emits_a_terminal_done() -> None:
    """``max_rounds <= 0`` runs ZERO rounds yet still terminally observes itself:
    one ``until_clean.done`` with rounds=0, outstanding=None (nothing measured)."""
    from structlog.testing import capture_logs

    sleep, _seen = _no_sleep()
    runner, calls = _scripted_runner([])
    with capture_logs() as logs:
        out = run_until_clean(runner, max_rounds=0, sleep=sleep)
    assert not _until_clean_events(logs, "until_clean.round")  # no round ran → no round log
    done = _until_clean_events(logs, "until_clean.done")
    assert len(done) == 1
    assert done[0]["rounds"] == 0
    assert done[0]["outstanding"] is None
    assert done[0]["verdict"] == "stop_budget"
    # Parity: the disabled-budget outcome is byte-identical (no rounds, spent budget).
    assert out == UntilCleanOutcome(verdict="stop_budget", rounds_run=0, rounds=())
    assert calls == []
