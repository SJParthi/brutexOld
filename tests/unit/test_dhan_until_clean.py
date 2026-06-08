"""Tests for `brutex.adapters.dhan.until_clean` — the pure re-pull-until-clean controller.

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
from brutex.adapters.dhan import until_clean as mod
from brutex.adapters.dhan.until_clean import (
    DEFAULT_MAX_ROUNDS,
    UntilCleanOutcome,
    UntilCleanRound,
    decide_until_clean,
    run_until_clean,
)


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

    mod._default_round_backoff(2)  # base × 2**0 = 5.0 (first pre-wait, round 2)
    mod._default_round_backoff(3)  # base × 2**1 = 10.0
    mod._default_round_backoff(100)  # clamped to the 60.0 ceiling

    assert slept[0] == pytest.approx(mod._DEFAULT_ROUND_BACKOFF_S)
    assert slept[1] == pytest.approx(mod._DEFAULT_ROUND_BACKOFF_S * 2.0)
    assert slept[2] == pytest.approx(mod._MAX_ROUND_BACKOFF_S)


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
