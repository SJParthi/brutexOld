"""Unit tests for the one-click spot→metadata→historical pull pipeline.

DEC-ONE-CLICK-PIPELINE-001. Exercises every gate, every stop-early branch, the
``ok`` property, ``to_dict``, and the note synthesis at 100% line + branch — all
with trivial in-memory report fakes and ZERO network (the sequencer takes injected
callables, so the entire control flow is testable offline; Golden Rule #1 — the
fakes return the REAL report dataclasses, nothing fabricated).
"""

from __future__ import annotations

import pytest
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pipeline_pull import (
    PHASE_METADATA,
    PHASE_SPOT,
    PipelinePullReport,
    historical_phase_ok,
    metadata_phase_ok,
    run_full_pipeline,
    spot_phase_ok,
)
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult

# --------------------------------------------------------------------------- #
# Report builders (the REAL dataclasses, populated minimally per Golden Rule #1)
# --------------------------------------------------------------------------- #


def _spot(
    *,
    auth_aborted: bool = False,
    quota_exhausted: bool = False,
    still_failed_windows: int = 0,
    completed: int = 3,
) -> BulkSpotReport:
    """Build a :class:`BulkSpotReport` with one symbol carrying *completed* pairs."""
    return BulkSpotReport(
        dry_run=False,
        start_date="2020-01-01",
        end_date="2026-06-20",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=completed, failed=0, no_data=0)],
        auth_aborted=auth_aborted,
        auth_cause="token dead" if auth_aborted else None,
        still_failed_windows=still_failed_windows,
        quota_exhausted=quota_exhausted,
    )


def _metadata(*, expiry_failures: int = 0, contract_failures: int = 0) -> MetadataPullReport:
    """Build a :class:`MetadataPullReport` with the given failure tallies."""
    return MetadataPullReport(
        dry_run=False,
        years=(2024, 2025),
        underlyings=("RELIANCE",),
        expiry_cells=2,
        cells_skipped=0,
        expiry_failures=expiry_failures,
        expiries_persisted=12,
        contract_jobs=4,
        contract_failures=contract_failures,
        contracts_persisted=400,
        note="ok",
    )


def _historical_inner(
    *, auth_aborted: bool = False, still_failed_windows: int = 0, failed_pairs: int = 0
) -> HistoricalPullReport:
    """Build the inner :class:`HistoricalPullReport` (the candle sub-report)."""
    return HistoricalPullReport(
        dry_run=False,
        intervals=("1minute",),
        contracts_selected=10,
        contracts_completed=10,
        bars_appended=5000,
        windows=20,
        failed_pairs=failed_pairs,
        still_failed_windows=still_failed_windows,
        auth_aborted=auth_aborted,
        note="ok",
        phase1_clean=not (auth_aborted or still_failed_windows or failed_pairs),
        phase1_complete=("RELIANCE25JAN3000CE",),
        start_dates={"RELIANCE25JAN3000CE": "2024-10-01"},
        minute_requested=True,
        minute_skipped=False,
    )


_NO_METADATA = MetadataPullReport(
    dry_run=False,
    years=(),
    underlyings=(),
    expiry_cells=0,
    cells_skipped=0,
    expiry_failures=0,
    expiries_persisted=0,
    contract_jobs=0,
    contract_failures=0,
    contracts_persisted=0,
    note="sentinel: use the clean default",
)


def _composite(
    *,
    historical: HistoricalPullReport | None = None,
    metadata: MetadataPullReport | None = _NO_METADATA,
) -> CompositePullReport:
    """Build a :class:`CompositePullReport` wrapping *historical* + *metadata*.

    ``metadata`` defaults (via the ``_NO_METADATA`` sentinel) to a clean inner
    metadata sub-report; pass an explicit failing :class:`MetadataPullReport` to
    exercise the inner-metadata gate, or ``None`` to model a composite that
    enumerated nothing.
    """
    inner_meta = _metadata() if metadata is _NO_METADATA else metadata
    return CompositePullReport(
        dry_run=False,
        requested=("RELIANCE",),
        tradable=("RELIANCE",),
        skipped_reference_only=(),
        metadata=inner_meta,
        historical=historical,
        note="ok",
    )


# --------------------------------------------------------------------------- #
# spot_phase_ok
# --------------------------------------------------------------------------- #


def test_spot_phase_ok_clean() -> None:
    assert spot_phase_ok(_spot()) is True


def test_spot_phase_not_ok_on_auth_aborted() -> None:
    assert spot_phase_ok(_spot(auth_aborted=True)) is False


def test_spot_phase_not_ok_on_quota_exhausted() -> None:
    assert spot_phase_ok(_spot(quota_exhausted=True)) is False


def test_spot_phase_not_ok_on_still_failed_windows() -> None:
    assert spot_phase_ok(_spot(still_failed_windows=2)) is False


# --------------------------------------------------------------------------- #
# metadata_phase_ok
# --------------------------------------------------------------------------- #


def test_metadata_phase_ok_clean() -> None:
    assert metadata_phase_ok(_metadata()) is True


def test_metadata_phase_not_ok_on_expiry_failures() -> None:
    assert metadata_phase_ok(_metadata(expiry_failures=1)) is False


def test_metadata_phase_not_ok_on_contract_failures() -> None:
    assert metadata_phase_ok(_metadata(contract_failures=1)) is False


# --------------------------------------------------------------------------- #
# historical_phase_ok
# --------------------------------------------------------------------------- #


def test_historical_phase_ok_when_no_candle_subreport() -> None:
    # A None candle sub-report (dry-run / all-reference-only) is a no-op, not a failure.
    assert historical_phase_ok(_composite(historical=None)) is True


def test_historical_phase_ok_when_clean() -> None:
    assert historical_phase_ok(_composite(historical=_historical_inner())) is True


def test_historical_phase_not_ok_on_auth_aborted() -> None:
    report = _composite(historical=_historical_inner(auth_aborted=True))
    assert historical_phase_ok(report) is False


def test_historical_phase_not_ok_on_still_failed_windows() -> None:
    report = _composite(historical=_historical_inner(still_failed_windows=3))
    assert historical_phase_ok(report) is False


def test_historical_phase_not_ok_on_failed_pairs() -> None:
    report = _composite(historical=_historical_inner(failed_pairs=1))
    assert historical_phase_ok(report) is False


def test_historical_phase_not_ok_on_inner_metadata_failure() -> None:
    # Phase 3 re-runs the chain enumeration; an open-cell failure during it must
    # NOT read as clean even when the candle sub-report itself is clean.
    report = _composite(
        historical=_historical_inner(),
        metadata=_metadata(expiry_failures=1),
    )
    assert historical_phase_ok(report) is False


def test_historical_phase_ok_when_inner_metadata_absent() -> None:
    # A composite that enumerated nothing (metadata None) + no candles is a no-op.
    report = _composite(historical=None, metadata=None)
    assert historical_phase_ok(report) is True


# --------------------------------------------------------------------------- #
# run_full_pipeline — sequencing + stop-early gates
# --------------------------------------------------------------------------- #


class _Spy[T]:
    """A typed zero-arg callable that records whether it was invoked, returns *value*."""

    def __init__(self, value: T) -> None:
        self.value = value
        self.called = False

    def __call__(self) -> T:
        self.called = True
        return self.value


def test_pipeline_stops_after_spot_failure() -> None:
    spot = _Spy(_spot(auth_aborted=True))
    meta = _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))

    report = run_full_pipeline(run_spot=spot, run_metadata=meta, run_historical=hist)

    assert report.stopped_after == PHASE_SPOT
    assert report.metadata is None
    assert report.historical is None
    assert report.ok is False
    # The later phases must NOT have been invoked (no doomed vendor calls).
    assert meta.called is False
    assert hist.called is False
    assert "stopped after spot" in report.note


def test_pipeline_stops_after_metadata_failure() -> None:
    spot = _Spy(_spot())
    meta = _Spy(_metadata(contract_failures=2))
    hist = _Spy(_composite(historical=_historical_inner()))

    report = run_full_pipeline(run_spot=spot, run_metadata=meta, run_historical=hist)

    assert report.stopped_after == PHASE_METADATA
    assert report.spot is not None
    assert report.metadata is not None
    assert report.historical is None
    assert report.ok is False
    assert hist.called is False
    assert "stopped after metadata" in report.note


def test_pipeline_runs_all_phases_when_clean() -> None:
    spot = _Spy(_spot())
    meta = _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))

    report = run_full_pipeline(run_spot=spot, run_metadata=meta, run_historical=hist)

    assert report.stopped_after is None
    assert report.spot is not None
    assert report.metadata is not None
    assert report.historical is not None
    assert report.ok is True
    assert spot.called and meta.called and hist.called
    assert "pipeline complete" in report.note


def test_pipeline_complete_note_with_no_candle_subreport() -> None:
    # Phase 3 ran but pulled no candles (None inner) — the complete-note's else
    # branch (0 bars / 0 contracts) is exercised, and ok stays True (no failure).
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=None)),
    )
    assert report.stopped_after is None
    assert report.ok is True
    assert "0 bars over 0 contract(s)" in report.note


# --------------------------------------------------------------------------- #
# run_full_pipeline — the GATED phase-4 movers warm (DEC-PULL-SELECTED-PIPELINE-001)
# --------------------------------------------------------------------------- #


def test_pipeline_warms_movers_on_clean_run() -> None:
    # All three fetch phases clean + a warmer injected → phase 4 runs, the count is
    # recorded, and the note appends the warm clause.
    spot, meta = _Spy(_spot()), _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))
    movers = _Spy(7)
    report = run_full_pipeline(
        run_spot=spot, run_metadata=meta, run_historical=hist, run_movers=movers
    )
    assert report.ok is True
    assert movers.called is True
    assert report.movers_warmed == 7
    assert "movers 7 snapshot(s) warmed" in report.note
    assert report.to_dict()["movers_warmed"] == 7


def test_pipeline_warms_movers_zero_is_honest_not_none() -> None:
    # A warmer that warmed nothing (e.g. an empty lake) returns 0 — recorded as 0
    # (ran, warmed nothing), DISTINCT from None (did not run).
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_movers=_Spy(0),
    )
    assert report.movers_warmed == 0
    assert "movers 0 snapshot(s) warmed" in report.note


def test_pipeline_skips_movers_when_historical_not_clean() -> None:
    # The HARD gate: a historical fetch that completed-with-failures (failed_pairs)
    # must NEVER warm movers ("until/unless successful fetches happen, never run").
    hist = _Spy(_composite(historical=_historical_inner(failed_pairs=1)))
    movers = _Spy(5)
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=hist,
        run_movers=movers,
    )
    assert report.ok is False  # historical gate failed
    assert movers.called is False  # movers NOT warmed
    assert report.movers_warmed is None
    assert "warmed" not in report.note


def test_pipeline_skips_movers_when_stopped_at_spot() -> None:
    # An early stop never reaches phase 4 — the warmer is not even consulted.
    movers = _Spy(3)
    report = run_full_pipeline(
        run_spot=_Spy(_spot(auth_aborted=True)),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_movers=movers,
    )
    assert report.stopped_after == PHASE_SPOT
    assert movers.called is False
    assert report.movers_warmed is None


def test_pipeline_no_movers_phase_when_not_injected() -> None:
    # Backward-compatible: with no warmer (the whole-universe pipeline), phase 4 is
    # skipped entirely and movers_warmed is None — never a fabricated count.
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
    )
    assert report.ok is True
    assert report.movers_warmed is None
    assert "warmed" not in report.note
    assert report.to_dict()["movers_warmed"] is None


# --------------------------------------------------------------------------- #
# PipelinePullReport.ok — branch coverage of the conjunction chain
# --------------------------------------------------------------------------- #


def test_ok_false_when_stopped() -> None:
    report = PipelinePullReport(
        spot=_spot(), metadata=None, historical=None, stopped_after=PHASE_SPOT, note="x"
    )
    assert report.ok is False


def test_ok_false_when_spot_missing() -> None:
    # Defensive: a (constructed) report with no spot is never ok.
    report = PipelinePullReport(
        spot=None, metadata=_metadata(), historical=_composite(), stopped_after=None, note="x"
    )
    assert report.ok is False


def test_ok_false_when_metadata_missing() -> None:
    report = PipelinePullReport(
        spot=_spot(), metadata=None, historical=_composite(), stopped_after=None, note="x"
    )
    assert report.ok is False


def test_ok_false_when_metadata_gate_fails() -> None:
    report = PipelinePullReport(
        spot=_spot(),
        metadata=_metadata(expiry_failures=1),
        historical=_composite(historical=_historical_inner()),
        stopped_after=None,
        note="x",
    )
    assert report.ok is False


def test_ok_false_when_historical_missing() -> None:
    report = PipelinePullReport(
        spot=_spot(), metadata=_metadata(), historical=None, stopped_after=None, note="x"
    )
    assert report.ok is False


def test_ok_false_when_historical_gate_fails() -> None:
    report = PipelinePullReport(
        spot=_spot(),
        metadata=_metadata(),
        historical=_composite(historical=_historical_inner(failed_pairs=1)),
        stopped_after=None,
        note="x",
    )
    assert report.ok is False


def test_ok_false_when_spot_gate_fails_but_not_stopped() -> None:
    # Defensive: a hand-built report whose spot gate fails yet stopped_after is None.
    report = PipelinePullReport(
        spot=_spot(auth_aborted=True),
        metadata=_metadata(),
        historical=_composite(historical=_historical_inner()),
        stopped_after=None,
        note="x",
    )
    assert report.ok is False


# --------------------------------------------------------------------------- #
# to_dict
# --------------------------------------------------------------------------- #


def test_to_dict_full_run() -> None:
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
    )
    payload = report.to_dict()
    assert payload["ok"] is True
    assert payload["stopped_after"] is None
    assert payload["spot"] is not None
    assert payload["metadata"] is not None
    assert payload["historical"] is not None
    assert isinstance(payload["note"], str)


def test_to_dict_stopped_after_spot() -> None:
    report = run_full_pipeline(
        run_spot=_Spy(_spot(still_failed_windows=1)),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite()),
    )
    payload = report.to_dict()
    assert payload["ok"] is False
    assert payload["stopped_after"] == PHASE_SPOT
    assert payload["spot"] is not None
    assert payload["metadata"] is None
    assert payload["historical"] is None


@pytest.mark.parametrize("phase", [PHASE_SPOT, PHASE_METADATA])
def test_phase_constants_are_distinct_labels(phase: str) -> None:
    assert phase in {PHASE_SPOT, PHASE_METADATA}
    assert PHASE_SPOT != PHASE_METADATA
