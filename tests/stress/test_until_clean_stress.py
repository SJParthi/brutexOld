"""Sustained stress for the until-clean controller (DEC-TOKEN-PREFLIGHT-001).

Drives :func:`~brutex.adapters.dhan.until_clean.run_until_clean` over many
high-budget loops with adversarial residual scripts (slow drains, never-clearing
storms, late token deaths) to prove the honest-envelope guarantee holds at scale:
the loop ALWAYS terminates, ``rounds_run`` is ALWAYS bounded by the budget, and
the bounded memory footprint (one :class:`UntilCleanRound` per round, never more
than the budget) never blows up. No real network, no real sleeps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from brutex.adapters.dhan.until_clean import UntilCleanRound, run_until_clean

_CYCLES = 20_000


def _no_sleep(_round_index: int) -> None:
    """No-op backoff — the stress loop must spend zero real wall-time sleeping."""


def _drain(it: Iterator[UntilCleanRound]) -> Callable[[], UntilCleanRound]:
    """Bind *it* into a zero-arg round-runner (avoids a loop-var closure capture)."""
    return lambda: next(it)


def test_slow_drain_terminates_clean_under_budget() -> None:
    """A residual that drains exactly one window/round clears within a generous budget."""
    for _ in range(_CYCLES):
        residual = list(range(8, -1, -1))  # 8,7,...,1,0
        it = iter(UntilCleanRound(r, False) for r in residual)
        out = run_until_clean(_drain(it), max_rounds=20, sleep=_no_sleep)
        assert out.verdict == "stop_clean"
        assert out.rounds_run == 9  # 8 non-clean rounds + the clean one
        assert out.final_still_failed == 0


def test_never_clearing_storm_stops_on_budget() -> None:
    """A storm that never clears is ALWAYS bounded by max_rounds (never infinite)."""
    budget = 7
    for _ in range(_CYCLES):
        out = run_until_clean(
            lambda: UntilCleanRound(99, False), max_rounds=budget, sleep=_no_sleep
        )
        assert out.verdict == "stop_budget"
        assert out.rounds_run == budget
        assert len(out.rounds) == budget  # bounded memory: one round per budget unit
        assert out.final_still_failed == 99


def test_late_token_death_halts_within_budget() -> None:
    """A token death on the final allowed round halts as stop_dead, never overruns."""
    budget = 5
    for _ in range(_CYCLES):
        script = [UntilCleanRound(10, False)] * (budget - 1) + [
            UntilCleanRound(10, True, auth_cause="DH-906 Invalid Token")
        ]
        it = iter(script)
        out = run_until_clean(_drain(it), max_rounds=budget, sleep=_no_sleep)
        assert out.verdict == "stop_dead"
        assert out.rounds_run == budget
        assert out.pull_verdict().kind == "auth_expired"
