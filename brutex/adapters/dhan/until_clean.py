"""Re-pull-until-clean — drive bounded pull rounds until the coverage ledger is CLEAN.

The merged auto-retry (DEC-INGEST-AUTO-RETRY-001 #483) re-pulls leftover failed
windows in a bounded round loop and STOPS — clean, token-dead, or budget-spent —
returning whatever residual remains. A sustained, multi-minute Dhan 429 storm can
outlive even that loop, leaving the operator to MANUALLY re-run the pull again
and again (the "why again & again N failed windows" report). This module adds the
operator-facing **re-pull-until-clean** capability on top: keep running bounded
auto-retry rounds (REUSING ``retry_failed_windows`` from #483 inside each round —
this module NEVER re-implements the per-window round loop) until ONE of three
terminal states is reached:

* **stop_clean** — the coverage ledger is clean (zero transient residual);
* **stop_dead** — the token died mid-round (re-auth needed; emit the loud
  ``auth_expired`` :class:`~brutex.adapters.dhan.pull_verdict.PullVerdict`);
* **stop_budget** — the bounded outer round budget is spent (residual remains).

Design (CLAUDE.md §11 COMMON + Golden Rule #1/#10):

* :func:`decide_until_clean` is the **pure** 4-way controller (continue /
  stop_clean / stop_dead / stop_budget) — no I/O, no clock, O(1), 100%
  line+branch.
* :func:`run_until_clean` is the thin driver: it calls an **injected**
  ``round_runner`` (one full pull-pass-with-auto-retry over the SHARED coverage
  ledger) and an **injected** ``sleep`` between rounds, so it is driven in tests
  by a fake clock + a fake round-runner with NO real network and NO real sleeps.

**Honest envelope (Golden Rule #10):**

* The work is **O(failed_windows) per round, NOT O(1)-total** — every round
  re-fetches whatever transient residual the previous round left; the OUTER round
  COUNT is bounded by ``max_rounds`` (never infinite), and each round itself
  inherits the bounded inner ``retry_failed_windows`` budget.
* **Rate-safe by construction** — a round IS one paced ingest pass governed by the
  SAME shared ``≤ 5 r/s`` token bucket (CLAUDE.md §2 Rule 6); the inter-round
  ``sleep`` waits a throttle out rather than fighting it.
* It **cannot revive a dead token** — a token death halts the loop immediately
  (``stop_dead``) for the operator to re-auth.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import sleep as _time_sleep
from typing import TYPE_CHECKING, Any, Literal

from brutex.adapters.dhan.pull_verdict import PullVerdict, classify_pull

if TYPE_CHECKING:
    from collections.abc import Callable

#: The pure controller's four verdicts: keep going, or one of three terminal stops.
LoopVerdict = Literal["continue", "stop_clean", "stop_dead", "stop_budget"]

#: Default bounded OUTER round budget. Small + finite (Golden Rule #10 — never an
#: infinite loop): a handful of full re-pull rounds drains a multi-minute 429 storm
#: once the burst is over. Each round itself runs the bounded inner
#: ``retry_failed_windows`` loop, so the worst case is strictly bounded.
DEFAULT_MAX_ROUNDS: int = 5

#: Base inter-round backoff (seconds): round *k* (1-based, only rounds ≥ 2 wait)
#: sleeps ``base × 2**(k-2)`` clamped to :data:`_MAX_ROUND_BACKOFF_S` — a
#: throttle-recovery-scale gap so a sustained Dhan throttle clears between
#: full re-pull rounds (mirrors ``auto_retry._default_round_backoff``).
_DEFAULT_ROUND_BACKOFF_S: float = 5.0

#: Hard ceiling (seconds) on a single inter-round backoff — defends an
#: operator-raised round budget from an unbounded sleep.
_MAX_ROUND_BACKOFF_S: float = 60.0


def _default_round_backoff(round_index: int) -> None:
    """Sleep the bounded exponential backoff BEFORE re-pull round *round_index* (1-based).

    Only rounds ``≥ 2`` ever call this (the first round is the initial pull — no
    pre-wait), so round 2 waits ``base × 2**0``, round 3 ``base × 2**1``, … clamped
    to :data:`_MAX_ROUND_BACKOFF_S`. Injected as the default ``sleep`` hook so
    production waits a real, throttle-recovery-scale gap while tests pass a no-op
    recorder (no real sleep) — the same inject-the-sleep pattern as
    :func:`~brutex.adapters.dhan.auto_retry._default_round_backoff`.
    """
    _time_sleep(min(_DEFAULT_ROUND_BACKOFF_S * (2.0 ** (round_index - 2)), _MAX_ROUND_BACKOFF_S))


@dataclass(frozen=True, slots=True)
class UntilCleanRound:
    """One outer round's residual accounting — what :func:`run_until_clean`'s runner returns.

    A round is one full pull-pass-with-auto-retry; these counts come straight off
    its report (the still-failed transient windows + whether the token died + the
    first dead-token cause).
    """

    #: Transient windows still failing after this round (0 ⇒ this round was clean).
    still_failed_windows: int
    #: Whether the token died mid-round (a 401/403 or ``DH-906``) — halts the loop.
    auth_aborted: bool
    #: The dead-token cause string (only set when ``auth_aborted``), for the verdict.
    auth_cause: str | None = None


@dataclass(frozen=True, slots=True)
class UntilCleanOutcome:
    """Outcome of a :func:`run_until_clean` drive (MANUAL ``--until-clean`` or AUTOMATIC).

    The same value object the CLI ``brutex fetch --until-clean`` trigger and the
    dashboard / ``pull_all_spot`` automatic path receive, so the two report
    identically (CLAUDE.md §11 COMMON).
    """

    #: The terminal verdict the loop ended on (never ``"continue"``).
    verdict: LoopVerdict
    #: Number of OUTER rounds actually run (0 when ``max_rounds <= 0`` disabled it).
    rounds_run: int
    #: Per-round residual accounting, in run order.
    rounds: tuple[UntilCleanRound, ...]

    @property
    def clean(self) -> bool:
        """True iff the loop reached a CLEAN coverage ledger (``stop_clean``)."""
        return self.verdict == "stop_clean"

    @property
    def auth_aborted(self) -> bool:
        """True iff the loop halted on a dead token (``stop_dead``) — re-auth needed."""
        return self.verdict == "stop_dead"

    @property
    def stopped_on_budget(self) -> bool:
        """True iff the loop stopped with residual after spending the round budget."""
        return self.verdict == "stop_budget"

    @property
    def final_still_failed(self) -> int:
        """Transient residual after the LAST round ran (0 when none ran / clean)."""
        return self.rounds[-1].still_failed_windows if self.rounds else 0

    @property
    def auth_cause(self) -> str | None:
        """The dead-token cause from the last round (``None`` unless ``stop_dead``)."""
        return self.rounds[-1].auth_cause if self.rounds else None

    def pull_verdict(self) -> PullVerdict:
        """Map the final state to the shared :class:`PullVerdict` (DEC-PULL-VERDICT-001).

        Reuses :func:`~brutex.adapters.dhan.pull_verdict.classify_pull` (#485) —
        the SAME verdict vocabulary a finished pull emits (CLAUDE.md §11 COMMON):
        ``auth_expired`` when the token died, ``transient_residual`` when the
        budget was spent with residual left, ``clean`` when the ledger is clean.
        """
        return classify_pull(
            auth_aborted=self.auth_aborted,
            still_failed_windows=self.final_still_failed,
            failed_pairs=0,
            no_data=0,
            completed=0,
            auth_cause=self.auth_cause,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for a route / structured log."""
        return {
            "verdict": self.verdict,
            "rounds_run": self.rounds_run,
            "clean": self.clean,
            "auth_aborted": self.auth_aborted,
            "stopped_on_budget": self.stopped_on_budget,
            "final_still_failed": self.final_still_failed,
        }


def decide_until_clean(
    *,
    still_failed_windows: int,
    auth_aborted: bool,
    rounds_used: int,
    max_rounds: int,
) -> LoopVerdict:
    """Decide what the until-clean loop does after a round — the pure 4-way controller.

    Priority (matching ``auto_retry`` + ``classify_pull``, worst first):

    1. **stop_dead** — ``auth_aborted``: the token died; retrying is futile
       (``RenewToken`` cannot revive a dead token) — halt for re-auth.
    2. **stop_clean** — ``still_failed_windows == 0``: the coverage ledger is clean.
    3. **stop_budget** — ``rounds_used >= max_rounds``: residual remains but the
       bounded outer budget is spent.
    4. **continue** — residual remains and budget is left: run another round.

    Pure + O(1) (a constant number of comparisons). No I/O, no clock.
    """
    if auth_aborted:
        return "stop_dead"
    if still_failed_windows == 0:
        return "stop_clean"
    if rounds_used >= max_rounds:
        return "stop_budget"
    return "continue"


def run_until_clean(
    round_runner: Callable[[], UntilCleanRound],
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    sleep: Callable[[int], None] | None = None,
    decide: Callable[..., LoopVerdict] = decide_until_clean,
) -> UntilCleanOutcome:
    """Run bounded re-pull rounds until CLEAN, the token dies, or the budget is spent.

    The single driver BOTH the MANUAL (``brutex fetch --until-clean``) and the
    AUTOMATIC (default-off ``pull_all_spot(until_clean=True)`` / dashboard) paths
    call (CLAUDE.md §11 COMMON). Each round invokes *round_runner* — ONE full
    pull-pass-with-auto-retry over the SHARED coverage ledger, which itself reuses
    :func:`~brutex.adapters.dhan.auto_retry.retry_failed_windows` (this driver
    NEVER re-implements the per-window round loop). After each round
    :func:`decide_until_clean` says continue / stop_clean / stop_dead / stop_budget;
    the loop sleeps the injected *sleep* backoff BEFORE every round after the first
    (the first round is the initial pull — no pre-wait).

    **Honest envelope (Golden Rule #10):** O(failed_windows) per round, NOT
    O(1)-total — a round re-fetches its residual; the OUTER round COUNT is bounded
    by *max_rounds*, never infinite. Rate-safety is inherited from *round_runner*
    (the shared ``≤ 5 r/s`` bucket); *sleep* waits a throttle out.

    Parameters
    ----------
    round_runner
        Zero-arg callable that runs ONE full pull pass (with the inner auto-retry)
        over the leftover windows and returns its :class:`UntilCleanRound`. The
        caller binds the adapter / sink / instruments / SHARED coverage ledger +
        pace bucket into it (so each round is rate-safe + re-fetches only failed
        windows). Called once per round.
    max_rounds
        Bounded OUTER round budget (default :data:`DEFAULT_MAX_ROUNDS`). ``<= 0``
        runs the loop ZERO times (a disabled no-op) and returns ``stop_budget``
        with ``rounds_run=0`` — never infinite regardless of residual.
    sleep
        Injected ``(round_index) -> None`` backoff hook called BEFORE each round
        AFTER the first (``None`` → :func:`_default_round_backoff`, a bounded
        exponential seconds-scale wait). Tests pass a no-op recorder so no real
        sleep occurs.
    decide
        The verdict function (default :func:`decide_until_clean`); injectable for
        tests.

    Returns
    -------
    UntilCleanOutcome
        The terminal verdict + the rounds run + their per-round accounting.
    """
    wait = sleep if sleep is not None else _default_round_backoff
    rounds: list[UntilCleanRound] = []
    if max_rounds <= 0:
        # Disabled — run nothing, report a spent budget (mirrors auto_retry's
        # max_rounds=0 opt-out, never infinite).
        return UntilCleanOutcome(verdict="stop_budget", rounds_run=0, rounds=())
    round_index = 0
    verdict: LoopVerdict = "continue"
    while True:
        round_index += 1
        if round_index > 1:
            # The first round IS the initial pull (no pre-wait); subsequent rounds
            # wait a bounded backoff so a sustained throttle clears between pulls.
            wait(round_index)
        outcome = round_runner()
        rounds.append(outcome)
        verdict = decide(
            still_failed_windows=outcome.still_failed_windows,
            auth_aborted=outcome.auth_aborted,
            rounds_used=round_index,
            max_rounds=max_rounds,
        )
        if verdict != "continue":
            break
    return UntilCleanOutcome(verdict=verdict, rounds_run=round_index, rounds=tuple(rounds))


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "LoopVerdict",
    "UntilCleanOutcome",
    "UntilCleanRound",
    "decide_until_clean",
    "run_until_clean",
]
