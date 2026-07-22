# pyright: reportPrivateUsage=none
"""The StageOutcome structural protocol + staged vocabulary (DEC-STAGED-SEQUENCER-SEAMS-001).

ADR-0021 §12 invariant 4 (the feed-track F3 ruling): the five shipped Groww
report shapes are SATISFYING INSTANCES of the structural protocol — pinned
here both statically (mypy-checked ``StageOutcome`` assignments — a shape
regression fails ``mypy --strict``) and at runtime (``isinstance`` via
``@runtime_checkable``). Plus the vocabulary pins (canonical stage order,
distinct status tokens) and the ONE gate law ``stage_outcome_ok``.
"""

from __future__ import annotations

from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pull_controller import BulkSpotReport
from brutex.dashboard.stage_outcome import (
    STAGE_FUTURES,
    STAGE_FUTURES_PREBUILD,
    STAGE_GREEKS,
    STAGE_METADATA,
    STAGE_OPTIONS,
    STAGE_SPOT,
    STAGE_SPOT_DAILY,
    STAGE_SPOT_PREBUILD,
    STAGE_STATUS_ABSENT,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_SETTLED,
    STAGE_STATUS_SKIPPED,
    STAGED_STAGE_ORDER,
    StageOutcome,
    stage_outcome_ok,
)
from brutex.data.greeks_enrich_pass import GREEKS_NOT_RUN, GreeksEnrichOutcome

# The test factories build the REAL report dataclasses (Golden Rule #1 — the
# established cross-test import pattern, e.g. tests/golden/test_bench_baseline_golden).
from tests.unit.test_pipeline_pull import _composite, _metadata, _spot


def _prebuild_outcome(*, ok: bool = True) -> PrebuildOutcome:
    """A minimal real :class:`PrebuildOutcome`."""
    return PrebuildOutcome(
        symbols_built=3, total_bars=100, failed=(), failed_count=0 if ok else 1, ok=ok
    )


class TestProtocolSatisfaction:
    """The five shipped Groww shapes satisfy the STRUCTURAL protocol.

    The typed assignments below are the STATIC pins — ``mypy --strict``
    verifies each concrete type against the protocol at gate time (a dropped
    ``ok`` member on any shape fails the type check, not just this suite);
    the ``isinstance`` arm is the runtime belt (``@runtime_checkable``
    checks member EXISTENCE).
    """

    def test_bulk_spot_report_satisfies(self) -> None:
        report: BulkSpotReport = _spot()
        outcome: StageOutcome = report  # the mypy-checked static pin
        assert isinstance(outcome, StageOutcome)
        assert outcome.ok is True

    def test_metadata_report_satisfies(self) -> None:
        report: MetadataPullReport = _metadata()
        outcome: StageOutcome = report
        assert isinstance(outcome, StageOutcome)
        assert outcome.ok is True

    def test_composite_report_satisfies(self) -> None:
        report: CompositePullReport = _composite()
        outcome: StageOutcome = report
        assert isinstance(outcome, StageOutcome)
        assert outcome.ok is True

    def test_prebuild_outcome_satisfies(self) -> None:
        report: PrebuildOutcome = _prebuild_outcome()
        outcome: StageOutcome = report
        assert isinstance(outcome, StageOutcome)
        assert outcome.ok is True

    def test_greeks_outcome_satisfies(self) -> None:
        outcome: StageOutcome = GreeksEnrichOutcome(ok=True, error=None, errored_targets=0)
        assert isinstance(outcome, StageOutcome)
        assert outcome.ok is True

    def test_failing_shapes_read_ok_false(self) -> None:
        """The delegated `.ok` homes carry the FULL gate predicates."""
        assert _spot(auth_aborted=True).ok is False
        assert _spot(quota_exhausted=True).ok is False
        assert _spot(still_failed_windows=1).ok is False
        assert _spot(mass_empty_cause="halted").ok is False
        assert _metadata(expiry_failures=1).ok is False
        assert _metadata(contract_failures=1).ok is False
        assert _metadata(store_write_deferrals=1).ok is False
        assert _composite(metadata=_metadata(expiry_failures=1)).ok is False
        assert _prebuild_outcome(ok=False).ok is False
        assert GreeksEnrichOutcome(ok=False, error="X: boom", errored_targets=0).ok is False

    def test_composite_none_subreports_are_noop_not_failure(self) -> None:
        assert _composite(metadata=None, historical=None).ok is True


class TestGateLaw:
    """``stage_outcome_ok``: only an explicit ``ok is False`` fails a stage."""

    def test_true_passes(self) -> None:
        assert stage_outcome_ok(_spot()) is True

    def test_false_fails(self) -> None:
        assert stage_outcome_ok(_spot(auth_aborted=True)) is False

    def test_none_is_not_a_failure(self) -> None:
        """The GreeksEnrichOutcome 'did not run' semantics — never a failure."""
        assert GREEKS_NOT_RUN.ok is None
        assert stage_outcome_ok(GREEKS_NOT_RUN) is True


class TestVocabulary:
    """The canonical order + the distinct status tokens (ADR-0021 Decisions 2/3)."""

    def test_canonical_stage_order(self) -> None:
        assert STAGED_STAGE_ORDER == (
            STAGE_SPOT_DAILY,
            STAGE_SPOT,
            STAGE_SPOT_PREBUILD,
            STAGE_METADATA,
            STAGE_FUTURES,
            STAGE_FUTURES_PREBUILD,
            STAGE_OPTIONS,
            STAGE_GREEKS,
        )

    def test_spot_and_metadata_tokens_equal_the_legacy_phase_tokens(self) -> None:
        """One vocabulary, no synonyms — the staged stopped_after tokens for the
        shared stages ARE the legacy phase tokens."""
        from brutex.dashboard.pipeline_pull import PHASE_METADATA, PHASE_SPOT

        assert STAGE_SPOT == PHASE_SPOT
        assert STAGE_METADATA == PHASE_METADATA

    def test_status_tokens_are_pairwise_distinct(self) -> None:
        """The ABSENT-vs-FAILED contract's structural floor: the tokens can
        never be conflated because they are pairwise-distinct strings."""
        tokens = {
            STAGE_STATUS_ABSENT,
            STAGE_STATUS_SETTLED,
            STAGE_STATUS_FAILED,
            STAGE_STATUS_SKIPPED,
        }
        assert len(tokens) == 4
        assert STAGE_STATUS_ABSENT == "absent"
        assert STAGE_STATUS_FAILED == "failed"
