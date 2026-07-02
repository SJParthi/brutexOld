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
    PHASE_PREBUILD,
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
# run_full_pipeline — the PER-SEGMENT movers warm (DEC-PIPELINE-SEGMENT-MOVERS-001)
# --------------------------------------------------------------------------- #


def _clean_phases() -> tuple[
    _Spy[BulkSpotReport], _Spy[MetadataPullReport], _Spy[CompositePullReport]
]:
    """Three clean phase spies (spot / metadata / historical) for a fully-successful run."""
    return (
        _Spy(_spot()),
        _Spy(_metadata()),
        _Spy(_composite(historical=_historical_inner())),
    )


def test_pipeline_warms_each_segment_as_it_finishes() -> None:
    # All three fetch phases clean + all three per-segment warmers injected → each
    # segment warms, the per-segment map carries each count, the total is the sum,
    # and the note names every warmed segment in order.
    spot, meta, hist = _clean_phases()
    w_spot, w_fut, w_opt = _Spy(3), _Spy(1), _Spy(4)
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
    )
    assert report.ok is True
    assert w_spot.called and w_fut.called and w_opt.called
    assert report.segment_movers_warmed == {"spot": 3, "futures": 1, "options": 4}
    assert report.movers_warmed == 8
    assert "movers spot 3 + futures 1 + options 4 snapshot(s) warmed" in report.note
    payload = report.to_dict()
    assert payload["movers_warmed"] == 8
    assert payload["segment_movers_warmed"] == {"spot": 3, "futures": 1, "options": 4}


def test_pipeline_spot_only_success_warms_spot_only() -> None:
    # The operator's headline: spot succeeds → SPOT movers warm the moment the spot
    # phase finishes; a LATER metadata failure stops the chain, but the already-clean
    # spot movers stay warmed (and futures/options are NEVER warmed).
    spot = _Spy(_spot())
    meta = _Spy(_metadata(contract_failures=1))  # metadata FAILS after spot
    hist = _Spy(_composite(historical=_historical_inner()))
    w_spot, w_fut, w_opt = _Spy(5), _Spy(9), _Spy(9)
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
    )
    assert report.stopped_after == PHASE_METADATA
    assert hist.called is False  # historical never ran
    assert w_spot.called is True  # spot warmed (it succeeded)
    assert w_fut.called is False and w_opt.called is False  # historical never succeeded
    assert report.segment_movers_warmed == {"spot": 5}
    assert report.movers_warmed == 5
    assert report.to_dict()["segment_movers_warmed"] == {"spot": 5}


def test_pipeline_futures_and_options_warm_only_after_historical_success() -> None:
    # spot + metadata clean, then historical succeeds → futures + options warm; spot
    # (no warmer here) stays absent, proving per-segment independence.
    spot, meta, hist = _clean_phases()
    w_fut, w_opt = _Spy(2), _Spy(6)
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
    )
    assert report.ok is True
    assert w_fut.called and w_opt.called
    assert report.segment_movers_warmed == {"futures": 2, "options": 6}
    assert report.movers_warmed == 8
    assert "movers futures 2 + options 6 snapshot(s) warmed" in report.note


def test_pipeline_segment_warm_zero_is_honest_not_absent() -> None:
    # A segment whose warmer ran but warmed nothing (empty lake) records 0 — present
    # in the map (ran, warmed nothing), DISTINCT from absent (did not run).
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=_Spy(0),
    )
    assert report.segment_movers_warmed == {"spot": 0}
    assert report.movers_warmed == 0
    assert "movers spot 0 snapshot(s) warmed" in report.note


def test_pipeline_skips_futures_options_when_historical_not_clean() -> None:
    # The HARD gate: a historical fetch that completed-with-failures (failed_pairs)
    # must NEVER warm futures/options ("a segment that did not fully succeed must NOT
    # warm its movers"). Spot still warmed (it succeeded earlier).
    spot = _Spy(_spot())
    meta = _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner(failed_pairs=1)))
    w_spot, w_fut, w_opt = _Spy(3), _Spy(5), _Spy(5)
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
    )
    assert report.ok is False  # historical gate failed
    assert w_spot.called is True  # spot succeeded → warmed
    assert w_fut.called is False and w_opt.called is False  # historical not clean → NOT warmed
    assert report.segment_movers_warmed == {"spot": 3}
    assert report.movers_warmed == 3
    assert "futures" not in report.note and "options" not in report.note


def test_pipeline_skips_all_warms_when_stopped_at_spot() -> None:
    # An early spot stop never warms ANY segment — no warmer is consulted.
    w_spot, w_fut, w_opt = _Spy(3), _Spy(3), _Spy(3)
    report = run_full_pipeline(
        run_spot=_Spy(_spot(auth_aborted=True)),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
    )
    assert report.stopped_after == PHASE_SPOT
    assert not (w_spot.called or w_fut.called or w_opt.called)
    assert report.segment_movers_warmed == {}
    assert report.movers_warmed is None


def test_pipeline_no_movers_phase_when_no_warmers_injected() -> None:
    # Backward-compatible: with no warmers (the whole-universe pipeline), no segment
    # warms and movers_warmed is None — never a fabricated count.
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(run_spot=spot, run_metadata=meta, run_historical=hist)
    assert report.ok is True
    assert report.movers_warmed is None
    assert report.segment_movers_warmed == {}
    assert "warmed" not in report.note
    assert report.to_dict()["movers_warmed"] is None
    assert report.to_dict()["segment_movers_warmed"] == {}


# --------------------------------------------------------------------------- #
# run_full_pipeline — the GATED FINAL pre-build phase (DEC-UNIVERSE-PULL-CONSOLIDATE-001)
# --------------------------------------------------------------------------- #


def test_pipeline_prebuilds_timeframes_on_clean_run() -> None:
    # All three fetch phases clean + a prebuild callable injected → the FINAL phase
    # runs, the symbol count is recorded, and the note appends the pre-build clause.
    spot, meta = _Spy(_spot()), _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))
    prebuild = _Spy(4)
    report = run_full_pipeline(
        run_spot=spot, run_metadata=meta, run_historical=hist, run_prebuild=prebuild
    )
    assert report.ok is True
    assert prebuild.called is True
    assert report.prebuild_built == 4
    assert "timeframes pre-built for 4 symbol(s)" in report.note
    assert report.to_dict()["prebuild_built"] == 4


def test_pipeline_prebuild_zero_is_honest_not_none() -> None:
    # A prebuild that built nothing (e.g. an empty lake) returns 0 — recorded as 0
    # (ran, built nothing), DISTINCT from None (did not run).
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_prebuild=_Spy(0),
    )
    assert report.prebuild_built == 0
    assert "timeframes pre-built for 0 symbol(s)" in report.note


def test_pipeline_skips_prebuild_when_historical_not_clean() -> None:
    # The HARD gate: a historical fetch that completed-with-failures (failed_pairs)
    # must NEVER pre-build timeframes (same rule as movers).
    hist = _Spy(_composite(historical=_historical_inner(failed_pairs=1)))
    prebuild = _Spy(3)
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=hist,
        run_prebuild=prebuild,
    )
    assert report.ok is False  # historical gate failed
    assert prebuild.called is False  # pre-build NOT run
    assert report.prebuild_built is None
    assert "pre-built" not in report.note


def test_pipeline_skips_prebuild_when_stopped_at_spot() -> None:
    # An early stop never reaches the final phase — the prebuild is not consulted.
    prebuild = _Spy(3)
    report = run_full_pipeline(
        run_spot=_Spy(_spot(auth_aborted=True)),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_prebuild=prebuild,
    )
    assert report.stopped_after == PHASE_SPOT
    assert prebuild.called is False
    assert report.prebuild_built is None


def test_pipeline_no_prebuild_phase_when_not_injected() -> None:
    # Backward-compatible: with no prebuild callable, the final phase is skipped
    # entirely and prebuild_built is None — never a fabricated count.
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
    )
    assert report.ok is True
    assert report.prebuild_built is None
    assert "pre-built" not in report.note
    assert report.to_dict()["prebuild_built"] is None


def test_pipeline_warms_movers_and_prebuilds_together() -> None:
    # Both gated post-fetch phases run on a clean fetch; the per-segment movers
    # counts + the prebuild count + both note clauses are present (the shared
    # historical gate is evaluated once, every post-fetch phase fires).
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        warm_spot_movers=_Spy(2),
        warm_futures_movers=_Spy(1),
        warm_options_movers=_Spy(1),
        run_prebuild=_Spy(5),
    )
    assert report.movers_warmed == 4  # 2 (spot) + 1 (futures) + 1 (options)
    assert report.segment_movers_warmed == {"spot": 2, "futures": 1, "options": 1}
    assert report.prebuild_built == 5
    assert "movers spot 2 + futures 1 + options 1 snapshot(s) warmed" in report.note
    assert "timeframes pre-built for 5 symbol(s)" in report.note


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


@pytest.mark.parametrize("phase", [PHASE_SPOT, PHASE_METADATA, PHASE_PREBUILD])
def test_phase_constants_are_distinct_labels(phase: str) -> None:
    assert phase in {PHASE_SPOT, PHASE_METADATA, PHASE_PREBUILD}
    assert len({PHASE_SPOT, PHASE_METADATA, PHASE_PREBUILD}) == 3


# --------------------------------------------------------------------------- #
# DEC-WINDOW-LEDGER-RESCUE-001 — the /universe note: visible skips + loud reload failure
# --------------------------------------------------------------------------- #


def test_pipeline_note_folds_skipped_windows() -> None:
    # Spot 5 + F&O 7 settled-window skips → ONE combined clause on the /universe note.
    from dataclasses import replace

    spot = replace(_spot(), windows_skipped=5)
    hist = replace(_historical_inner(), windows_skipped=7)
    report = run_full_pipeline(
        run_spot=_Spy(spot),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=hist)),
    )
    assert "skipped 12 already-pulled window(s) (no vendor hit)" in report.note


def test_pipeline_note_zero_skips_renders_no_clause() -> None:
    # Default (0 skips) is byte-identical to the pre-feature note — no clause.
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
    )
    assert "already-pulled window(s)" not in report.note
    assert "window ledger reload FAILED" not in report.note


def test_pipeline_note_spot_only_skips_when_no_candle_subreport() -> None:
    # A None candle sub-report contributes 0 — the spot count still shows alone.
    from dataclasses import replace

    spot = replace(_spot(), windows_skipped=3)
    report = run_full_pipeline(
        run_spot=_Spy(spot),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=None)),
    )
    assert "skipped 3 already-pulled window(s) (no vendor hit)" in report.note


def test_pipeline_note_announces_spot_ledger_reload_failure() -> None:
    from dataclasses import replace

    spot = replace(_spot(), ledger_load_failed=True)
    report = run_full_pipeline(
        run_spot=_Spy(spot),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
    )
    assert report.note.startswith(
        "window ledger reload FAILED — running full re-verify pull (no skip); "
    )


def test_pipeline_note_announces_historical_ledger_reload_failure() -> None:
    from dataclasses import replace

    hist = replace(_historical_inner(), ledger_load_failed=True)
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=hist)),
    )
    assert report.note.startswith(
        "window ledger reload FAILED — running full re-verify pull (no skip); "
    )
