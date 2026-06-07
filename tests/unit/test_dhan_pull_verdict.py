"""Tests for `brutex.adapters.dhan.pull_verdict` — the actionable pull verdict.

100% line+branch on the pure classifier: the four prioritised kinds
(auth_expired > transient_residual > known_gap_only > clean), the priority
ordering, ``pending_windows`` arithmetic, ``needs_operator``, ``auth_cause``
preservation, and the JSON ``to_dict`` shape (DEC-PULL-VERDICT-001).
"""

from __future__ import annotations

from brutex.adapters.dhan.pull_verdict import PullVerdict, classify_pull


class TestClassifyPull:
    def test_auth_expired_is_highest_priority(self) -> None:
        # A dead token wins even when windows are also still failing.
        v = classify_pull(
            auth_aborted=True,
            still_failed_windows=7,
            failed_pairs=3,
            no_data=2,
            completed=0,
            auth_cause="HTTP 400 DH-906 Invalid Token",
        )
        assert v.kind == "auth_expired"
        assert v.needs_operator is True
        assert v.pending_windows == 10  # still_failed (7) + failed_pairs (3)
        assert v.auth_cause == "HTTP 400 DH-906 Invalid Token"
        assert "web.dhan.co" in v.action

    def test_transient_residual_on_still_failed_windows(self) -> None:
        v = classify_pull(
            auth_aborted=False, still_failed_windows=4, failed_pairs=0, no_data=0, completed=100
        )
        assert v.kind == "transient_residual"
        assert v.needs_operator is True
        assert v.pending_windows == 4
        assert v.auth_cause is None  # never carried off the auth path

    def test_transient_residual_on_failed_pairs_only(self) -> None:
        # Covers the second operand of the `still_failed or failed_pairs` branch.
        v = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=2, no_data=0, completed=5
        )
        assert v.kind == "transient_residual"
        assert v.pending_windows == 2

    def test_known_gap_only_when_nothing_completed_but_some_empty(self) -> None:
        v = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=15, completed=0
        )
        assert v.kind == "known_gap_only"
        assert v.needs_operator is False
        assert v.pending_windows == 0
        assert "No action needed" in v.action

    def test_clean_when_work_completed(self) -> None:
        # Completed work with some incidental no-data is still clean (not a gap-only).
        v = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=3, completed=780
        )
        assert v.kind == "clean"
        assert v.needs_operator is False

    def test_clean_when_everything_zero(self) -> None:
        # No completed, no failures, no empties → still clean (nothing to do).
        v = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=0, completed=0
        )
        assert v.kind == "clean"


class TestPullVerdictSerialisation:
    def test_to_dict_round_trips_every_field(self) -> None:
        v = PullVerdict(
            kind="auth_expired",
            headline="🔑 ...",
            action="Regenerate ...",
            pending_windows=10,
            failed_pairs=3,
            no_data=2,
            completed=0,
            auth_cause="DH-906",
        )
        d = v.to_dict()
        assert d == {
            "kind": "auth_expired",
            "headline": "🔑 ...",
            "action": "Regenerate ...",
            "needs_operator": True,
            "pending_windows": 10,
            "failed_pairs": 3,
            "no_data": 2,
            "completed": 0,
            "auth_cause": "DH-906",
        }

    def test_needs_operator_false_for_clean_and_known_gap(self) -> None:
        clean = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=0, completed=1
        )
        gap = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=1, completed=0
        )
        assert clean.to_dict()["needs_operator"] is False
        assert gap.to_dict()["needs_operator"] is False
