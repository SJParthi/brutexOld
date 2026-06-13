"""Property invariants for `brutex.adapters.ingest_common` (DEC-TOKEN-PREFLIGHT-001).

The re-pull-until-clean driver must ALWAYS reach a terminal state within a bounded
number of rounds — the honest-envelope guarantee (Golden Rule #10: bounded, never
infinite). For ANY scripted residual/auth sequence + outer budget:

* the loop terminates with a NON-``continue`` verdict (terminal state reached),
* ``rounds_run`` never exceeds ``max_rounds`` (bounded),
* the verdict is the correct terminal class for the round it stopped on, and
* ``final_still_failed`` equals the LAST round's residual.
"""

from __future__ import annotations

from brutex.adapters.ingest_common import UntilCleanRound, run_until_clean
from hypothesis import given
from hypothesis import strategies as st


def _no_sleep(_round_index: int) -> None:
    """No-op backoff so the property runs with zero real sleeps."""


@given(
    residuals=st.lists(st.integers(min_value=0, max_value=200), min_size=1, max_size=10),
    auth_flags=st.lists(st.booleans(), min_size=1, max_size=10),
    max_rounds=st.integers(min_value=1, max_value=12),
)
def test_terminal_state_reached_within_budget(
    residuals: list[int], auth_flags: list[bool], max_rounds: int
) -> None:
    """Terminal state ALWAYS reached ∧ rounds_run ≤ budget, for any script + budget."""
    # Pad both scripts so a long budget never over-reads (the loop stops on the
    # first clean/dead round or at the budget). A trailing all-clean tail
    # guarantees a terminating script exists for every branch.
    n = max_rounds + len(residuals) + 2
    res = (residuals + [0] * n)[:n]
    auth = (auth_flags + [False] * n)[:n]
    rounds = [UntilCleanRound(r, a) for r, a in zip(res, auth, strict=True)]
    it = iter(rounds)

    out = run_until_clean(lambda: next(it), max_rounds=max_rounds, sleep=_no_sleep)

    # Terminal: never left the loop on "continue".
    assert out.verdict in ("stop_clean", "stop_dead", "stop_budget")
    # Bounded: never more rounds than the budget, always at least one.
    assert 1 <= out.rounds_run <= max_rounds
    assert out.rounds_run == len(out.rounds)
    # The verdict is the correct terminal class for the LAST round it ran.
    last = out.rounds[-1]
    if last.auth_aborted:
        assert out.verdict == "stop_dead"
    elif last.still_failed_windows == 0:
        assert out.verdict == "stop_clean"
    else:
        # Stopped with residual ⇒ budget must be spent.
        assert out.verdict == "stop_budget"
        assert out.rounds_run == max_rounds
    assert out.final_still_failed == last.still_failed_windows
