"""Tests for `brutex.adapters.ingest_common` — post-run leftover-window retry.

100% line+branch on the vendor-neutral auto-retry round loop (DEC-INGEST-AUTO-RETRY-001)
via a fake ``run_pass`` (a scripted sequence of fake ingest reports) + an injected
no-op backoff recorder (no real sleeps). Covers:

* clean-after-one-round (residual drains in the first round),
* residual-after-budget-exhausted (storm never clears within the budget),
* known-gap NOT retried via an INJECTED ``is_known_gap`` predicate (the Dhan
  ``DH-905`` path) AND the DEFAULT (``is_known_gap=None``) treating nothing as a
  known gap (the Groww path),
* auth-death halts the loop early,
* ``max_rounds=0`` disables the loop while keeping the same code path,
* the default backoff hook is bounded + exponential,
* MANUAL == AUTOMATIC: both drive the SAME ``retry_failed_windows`` engine,
* a property test: ``final_still_failed`` always equals the LAST round's residual
  and ``rounds_run`` never exceeds ``max_rounds``.

The fake report duck-types the three fields the engine reads
(``still_failed_windows`` / ``failures`` / ``auth_aborted``), and a small
``DH-905``-marker predicate stands in for the Dhan known-gap injection, so these
tests stay wheel-free (they never import the PEP-695 ``run_ingest`` chain).
"""

from __future__ import annotations

# The fake report duck-types the report's 3 read fields (sanctioned: keeps these
# tests wheel-free, never importing the PEP-695 run_ingest chain). pyright can't
# see the structural match against the nominal Protocol, so the engine-call
# arg-type is intentionally relaxed for this whitebox test module.
# pyright: reportArgumentType=false
# Whitebox test of module-private backoff constants/helpers (sanctioned per pyproject.toml).
# pyright: reportPrivateUsage=none
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest
from brutex.adapters import ingest_common as mod
from brutex.adapters.ingest_common import (
    RetryOutcome,
    RetryRound,
    retry_failed_windows,
)
from hypothesis import given
from hypothesis import strategies as st


def _is_dh905(failure: object) -> bool:
    """Stand-in Dhan known-gap predicate: a fake failure whose error carries DH-905.

    Mirrors what ``run_dhan_spot_ingest_with_retry`` injects (an
    ``is_no_data_message``-based classifier over a ``PairFailure.error``), without
    importing the PEP-695 run_ingest chain.
    """
    error = getattr(failure, "error", "")
    return "DH-905" in error or "no data present" in error


@dataclass(frozen=True)
class _FakeFailure:
    """A stand-in for a quarantined ``PairFailure`` — only ``.error`` is read."""

    error: str


@dataclass(frozen=True)
class _FakeReport:
    """Duck-typed stand-in for ``DhanIngestReport`` (the 3 fields the engine reads)."""

    still_failed_windows: int = 0
    failures: tuple[_FakeFailure, ...] = field(default_factory=tuple)
    auth_aborted: bool = False


def _scripted_run_pass(
    reports: Sequence[_FakeReport],
) -> tuple[Callable[[], _FakeReport], list[int]]:
    """Return a ``run_pass`` that yields *reports* in order + a list recording call count.

    The returned callable pops the next scripted report on each call (raising if
    the loop over-calls, which would be a budget bug) and appends a marker to the
    shared list so a test can assert exactly how many rounds ran.
    """
    calls: list[int] = []

    def _run_pass() -> _FakeReport:
        """One scripted ingest pass — returns the next pre-canned report."""
        idx = len(calls)
        calls.append(idx)
        return reports[idx]

    return _run_pass, calls


def _no_sleep() -> tuple[Callable[[int], None], list[int]]:
    """Return a no-op backoff hook + the list of round indices it was called with."""
    seen: list[int] = []

    def _backoff(round_index: int) -> None:
        """Record the round index instead of sleeping."""
        seen.append(round_index)

    return _backoff, seen


def test_clean_after_one_round() -> None:
    """A residual that drains in round 1 stops the loop early + reports clean."""
    run_pass, calls = _scripted_run_pass([_FakeReport(still_failed_windows=0)])
    backoff, seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=3, backoff=backoff)

    assert calls == [0]  # exactly one round ran (stopped on clean)
    assert seen == [1]  # backoff fired once, before round 1
    assert outcome.rounds_run == 1
    assert outcome.initial_still_failed == 0
    assert outcome.final_still_failed == 0
    assert outcome.clean is True
    assert outcome.auth_aborted is False
    assert outcome.recovered_windows == 0
    assert outcome.rounds == (
        RetryRound(round_index=1, still_failed_windows=0, known_gaps=0, auth_aborted=False),
    )


def test_recovers_over_multiple_rounds() -> None:
    """Residual shrinks round-by-round and clears within budget → clean."""
    run_pass, calls = _scripted_run_pass(
        [
            _FakeReport(still_failed_windows=10),
            _FakeReport(still_failed_windows=3),
            _FakeReport(still_failed_windows=0),
        ]
    )
    backoff, seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=5, backoff=backoff)

    assert calls == [0, 1, 2]
    assert seen == [1, 2, 3]
    assert outcome.rounds_run == 3
    assert outcome.initial_still_failed == 10
    assert outcome.final_still_failed == 0
    assert outcome.recovered_windows == 10
    assert outcome.clean is True


def test_residual_after_budget_exhausted() -> None:
    """A storm that never clears within the budget leaves a residual, not clean."""
    run_pass, calls = _scripted_run_pass(
        [
            _FakeReport(still_failed_windows=84),
            _FakeReport(still_failed_windows=40),
        ]
    )
    backoff, seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=2, backoff=backoff)

    assert calls == [0, 1]  # ran the full budget
    assert seen == [1, 2]
    assert outcome.rounds_run == 2
    assert outcome.initial_still_failed == 84
    assert outcome.final_still_failed == 40
    assert outcome.recovered_windows == 44
    assert outcome.clean is False
    assert outcome.auth_aborted is False


def test_known_gap_not_retried() -> None:
    """A ``DH-905`` known-gap quarantine is counted, NEVER drives another round.

    The Dhan path: ``is_known_gap`` is the injected DH-905 predicate.
    """
    run_pass, calls = _scripted_run_pass(
        [
            _FakeReport(
                still_failed_windows=0,
                failures=(
                    _FakeFailure(error="HTTP 400 bad request: DH-905 no data present"),
                    _FakeFailure(error="HTTP 400 bad request: no data present for range"),
                ),
            )
        ]
    )
    backoff, _seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=3, backoff=backoff, is_known_gap=_is_dh905)

    # still_failed is 0, so the loop stops after round 1 even though 2 known-gaps exist.
    assert calls == [0]
    assert outcome.rounds_run == 1
    assert outcome.final_still_failed == 0
    assert outcome.clean is True
    assert outcome.rounds[0].known_gaps == 2


def test_known_gap_does_not_count_a_transient() -> None:
    """A transient residual co-existing with a known-gap still drives a round; gap excluded."""
    run_pass, _calls = _scripted_run_pass(
        [
            _FakeReport(
                still_failed_windows=5,
                failures=(_FakeFailure(error="HTTP 429 rate limited: slow down"),),
            ),
            _FakeReport(
                still_failed_windows=0,
                failures=(_FakeFailure(error="DH-905 no data present"),),
            ),
        ]
    )
    backoff, _seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=3, backoff=backoff, is_known_gap=_is_dh905)

    assert outcome.rounds_run == 2
    assert outcome.rounds[0].known_gaps == 0  # the 429 failure is NOT a known-gap
    assert outcome.rounds[1].known_gaps == 1
    assert outcome.clean is True


def test_default_predicate_treats_nothing_as_known_gap() -> None:
    """The DEFAULT (``is_known_gap=None``, the Groww path) counts ZERO known-gaps.

    Even a failure that LOOKS like a Dhan no-data marker is NOT a known gap when no
    predicate is injected — the engine stays vendor-neutral (a vendor with no
    no-data concept never has a known gap).
    """
    run_pass, _calls = _scripted_run_pass(
        [
            _FakeReport(
                still_failed_windows=0,
                failures=(
                    _FakeFailure(error="DH-905 no data present"),
                    _FakeFailure(error="some terminal groww error"),
                ),
            )
        ]
    )
    backoff, _seen = _no_sleep()

    outcome = retry_failed_windows(
        run_pass, max_rounds=3, backoff=backoff
    )  # is_known_gap defaults None

    assert outcome.rounds_run == 1
    assert outcome.rounds[0].known_gaps == 0  # nothing is a known-gap without a predicate
    assert outcome.clean is True


def test_auth_death_halts_loop() -> None:
    """A mid-round token death stops the loop immediately (re-auth needed)."""
    run_pass, calls = _scripted_run_pass(
        [
            _FakeReport(still_failed_windows=20, auth_aborted=True),
            _FakeReport(still_failed_windows=0),  # never reached
        ]
    )
    backoff, _seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=5, backoff=backoff)

    assert calls == [0]  # halted after the first round
    assert outcome.rounds_run == 1
    assert outcome.final_still_failed == 20
    assert outcome.auth_aborted is True
    assert outcome.clean is False  # auth-aborted is never "clean"
    assert outcome.rounds[0].auth_aborted is True


def test_max_rounds_zero_disables_loop() -> None:
    """``max_rounds=0`` runs zero rounds (the ``--no-auto-retry`` opt-out path)."""
    run_pass, calls = _scripted_run_pass([])  # never called
    backoff, seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=0, backoff=backoff)

    assert calls == []  # run_pass never invoked
    assert seen == []  # backoff never invoked
    assert outcome.rounds_run == 0
    assert outcome.rounds == ()
    assert outcome.initial_still_failed == 0
    assert outcome.final_still_failed == 0
    assert outcome.clean is False  # nothing probed → no clean claim
    assert outcome.auth_aborted is False
    assert outcome.recovered_windows == 0


def test_recovered_windows_clamped_non_negative() -> None:
    """A later round surfacing MORE residual reports recovered=0, never negative."""
    run_pass, _calls = _scripted_run_pass(
        [
            _FakeReport(still_failed_windows=2),
            _FakeReport(still_failed_windows=9),  # grew (new windows reached)
        ]
    )
    backoff, _seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=2, backoff=backoff)

    assert outcome.initial_still_failed == 2
    assert outcome.final_still_failed == 9
    assert outcome.recovered_windows == 0  # clamped, not -7


def test_default_backoff_is_bounded_and_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default round backoff is exponential and clamped to the ceiling (no real sleep)."""
    slept: list[float] = []
    monkeypatch.setattr(mod, "_time_sleep", slept.append)

    mod._default_round_backoff(1)  # base × 2**0 = 5.0
    mod._default_round_backoff(2)  # base × 2**1 = 10.0
    mod._default_round_backoff(100)  # clamped to the 60.0 ceiling

    assert slept[0] == pytest.approx(mod._DEFAULT_ROUND_BACKOFF_S)
    assert slept[1] == pytest.approx(mod._DEFAULT_ROUND_BACKOFF_S * 2.0)
    assert slept[2] == pytest.approx(mod._MAX_ROUND_BACKOFF_S)


def test_default_backoff_used_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing ``backoff=None`` wires the default bounded backoff (covers the None branch)."""
    slept: list[float] = []
    monkeypatch.setattr(mod, "_time_sleep", slept.append)
    run_pass, _calls = _scripted_run_pass([_FakeReport(still_failed_windows=0)])

    outcome = retry_failed_windows(run_pass, max_rounds=1, backoff=None)

    assert outcome.rounds_run == 1
    assert len(slept) == 1  # the default backoff slept once, before round 1


def test_manual_and_automatic_share_one_engine() -> None:
    """MANUAL and AUTOMATIC callers hit the SAME function for the SAME inputs (CLAUDE.md §11)."""
    script = [_FakeReport(still_failed_windows=4), _FakeReport(still_failed_windows=0)]
    auto_pass, _ac = _scripted_run_pass(list(script))
    manual_pass, _mc = _scripted_run_pass(list(script))
    backoff_a, _sa = _no_sleep()
    backoff_m, _sm = _no_sleep()

    automatic = retry_failed_windows(auto_pass, max_rounds=3, backoff=backoff_a)
    manual = retry_failed_windows(manual_pass, max_rounds=3, backoff=backoff_m)

    assert automatic == manual  # identical RetryOutcome from the one engine


def test_outcome_is_frozen() -> None:
    """``RetryOutcome`` is an immutable value object (no accidental post-hoc mutation)."""
    outcome = RetryOutcome(
        initial_still_failed=0,
        final_still_failed=0,
        rounds_run=0,
        rounds=(),
        clean=False,
        auth_aborted=False,
    )
    with pytest.raises(AttributeError):
        outcome.clean = True  # type: ignore[misc]


@given(
    residuals=st.lists(st.integers(min_value=0, max_value=200), min_size=1, max_size=8),
    max_rounds=st.integers(min_value=1, max_value=12),
)
def test_property_final_residual_and_round_bound(residuals: list[int], max_rounds: int) -> None:
    """Property: final residual == last round's residual; rounds_run ≤ max_rounds.

    For ANY scripted residual sequence + budget, the loop (a) never runs more than
    ``max_rounds`` rounds, (b) reports ``final_still_failed`` equal to the residual
    of the LAST round it actually ran, and (c) declares ``clean`` iff that final
    residual is 0 (no auth-abort in this property's scripts).
    """
    # Pad the script so a long budget never over-reads (extra reports are harmless;
    # the loop stops on the first 0 or at the budget). A trailing 0 guarantees a
    # terminating clean script exists for the "drains" arm.
    reports = [_FakeReport(still_failed_windows=r) for r in residuals] + [
        _FakeReport(still_failed_windows=0) for _ in range(max_rounds)
    ]
    run_pass, calls = _scripted_run_pass(reports)
    backoff, seen = _no_sleep()

    outcome = retry_failed_windows(run_pass, max_rounds=max_rounds, backoff=backoff)

    assert outcome.rounds_run <= max_rounds
    assert outcome.rounds_run == len(calls) == len(seen)
    assert outcome.rounds_run >= 1
    last_residual = outcome.rounds[-1].still_failed_windows
    assert outcome.final_still_failed == last_residual
    assert outcome.clean is (last_residual == 0)
