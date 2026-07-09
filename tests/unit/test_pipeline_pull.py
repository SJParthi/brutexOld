"""Unit tests for the one-click spot→metadata→historical pull pipeline.

DEC-ONE-CLICK-PIPELINE-001. Exercises every gate, every stop-early branch, the
``ok`` property, ``to_dict``, and the note synthesis at 100% line + branch — all
with trivial in-memory report fakes and ZERO network (the sequencer takes injected
callables, so the entire control flow is testable offline; Golden Rule #1 — the
fakes return the REAL report dataclasses, nothing fabricated).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pipeline_pull import (
    MAX_QUARANTINED_NAMES,
    PHASE_MARKET_HOURS,
    PHASE_METADATA,
    PHASE_PREBUILD,
    PHASE_SPOT,
    STRICT_GATE_ENV,
    PipelinePullReport,
    PriorStepsVerdict,
    StepSettleVerdict,
    derive_quarantined_name_set,
    derive_quarantined_symbols,
    historical_phase_ok,
    metadata_phase_ok,
    run_full_pipeline,
    spot_phase_ok,
    step_settle_verdict,
    strict_gate_enabled,
)
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from hypothesis import given
from hypothesis import strategies as st


def _outcome(
    *,
    symbols_built: int = 4,
    total_bars: int = 100,
    failed: tuple[str, ...] = (),
    failed_count: int | None = None,
    ok: bool | None = None,
    crashed: str | None = None,
    fs_note: str | None = None,
    unchanged_skips: int = 0,
    opens_substituted: int = 0,
) -> PrebuildOutcome:
    """Build a :class:`PrebuildOutcome` (the run_prebuild contract's REAL dataclass)."""
    count = len(failed) if failed_count is None else failed_count
    return PrebuildOutcome(
        symbols_built=symbols_built,
        total_bars=total_bars,
        failed=failed,
        failed_count=count,
        ok=(count == 0) if ok is None else ok,
        crashed=crashed,
        fs_note=fs_note,
        unchanged_skips=unchanged_skips,
        opens_substituted=opens_substituted,
    )


# --------------------------------------------------------------------------- #
# Report builders (the REAL dataclasses, populated minimally per Golden Rule #1)
# --------------------------------------------------------------------------- #


def _spot(
    *,
    auth_aborted: bool = False,
    quota_exhausted: bool = False,
    still_failed_windows: int = 0,
    completed: int = 3,
    mass_empty_cause: str | None = None,
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
        mass_empty_cause=mass_empty_cause,
    )


def _metadata(
    *,
    expiry_failures: int = 0,
    contract_failures: int = 0,
    store_write_deferrals: int = 0,
) -> MetadataPullReport:
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
        store_write_deferrals=store_write_deferrals,
    )


def _historical_inner(
    *,
    auth_aborted: bool = False,
    still_failed_windows: int = 0,
    failed_pairs: int = 0,
    expiries_out_of_scope: int = 0,
    contracts_out_of_scope: int = 0,
    phase1_complete: tuple[str, ...] = ("RELIANCE25JAN3000CE",),
    start_dates: dict[str, str] | None = None,
    greeks_enrich_ok: bool | None = None,
    greeks_enrich_error: str | None = None,
    mass_empty_cause: str | None = None,
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
        phase1_complete=phase1_complete,
        start_dates=({"RELIANCE25JAN3000CE": "2024-10-01"} if start_dates is None else start_dates),
        minute_requested=True,
        minute_skipped=False,
        expiries_out_of_scope=expiries_out_of_scope,
        contracts_out_of_scope=contracts_out_of_scope,
        greeks_enrich_ok=greeks_enrich_ok,
        greeks_enrich_error=greeks_enrich_error,
        mass_empty_cause=mass_empty_cause,
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


def test_spot_phase_not_ok_on_mass_empty_halt() -> None:
    """A spot mass-empty circuit-breaker halt is a BLOCKING gate failure.

    Review fix (DEC-PULL-SIX-STEP-ORDER-001 round 1 MAJOR): the aborted pass
    never appends, so a `MassEmptySettleHalt` left ``still_failed_windows == 0``
    and this gate read a HALTED spot step as clean — the chain proceeded to
    metadata + F&O vendor calls and the strict all-or-nothing barrier fired the
    post-steps after a needs-operator pull-wide halt. The exact live-reproduced
    shape: an otherwise-zero report whose only failure signal is the cause.
    """
    report = _spot(
        completed=0,
        mass_empty_cause="mass clean-empty responses — pull halted before settling",
    )
    assert report.verdict.kind == "mass_empty_halt"
    assert spot_phase_ok(report) is False


# --------------------------------------------------------------------------- #
# metadata_phase_ok
# --------------------------------------------------------------------------- #


def test_metadata_phase_ok_clean() -> None:
    assert metadata_phase_ok(_metadata()) is True


def test_metadata_phase_not_ok_on_expiry_failures() -> None:
    assert metadata_phase_ok(_metadata(expiry_failures=1)) is False


def test_metadata_phase_not_ok_on_contract_failures() -> None:
    assert metadata_phase_ok(_metadata(contract_failures=1)) is False


def test_metadata_phase_not_ok_on_store_write_deferrals() -> None:
    """A wedged-store metadata phase (DEC-STORE-WRITE-DEADLINE-001) stops the
    chain GRACEFULLY after metadata — the deferred units re-attempt next pull."""
    assert metadata_phase_ok(_metadata(store_write_deferrals=1)) is False


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


def test_historical_phase_not_ok_on_mass_empty_halt() -> None:
    """The F&O mass-empty halt is a BLOCKING gate failure (spot-arm symmetry).

    Review fix: an F&O ``MassEmptySettleHalt`` with every OTHER signal clean
    (no failed pairs, no still-failing windows — the halt aborts before
    settling) previously read clean here; only the ``start_dates`` −
    ``phase1_complete`` quarantine arm accidentally rescued the gate. The gate
    now reads the halt directly.
    """
    report = _composite(
        historical=_historical_inner(
            mass_empty_cause="mass clean-empty responses — F&O pull halted before settling",
        )
    )
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
    """A typed callable recording invocation (+ any positional args), returns *value*.

    Accepts ``*args`` so ONE spy shape serves both the zero-arg phases
    (``run_spot`` / ``run_metadata``) and ``run_historical``, which receives
    the sequencer's :class:`PriorStepsVerdict` (DEC-PULL-SIX-STEP-ORDER-001);
    the received args are captured for the threading assertions.
    """

    def __init__(self, value: T) -> None:
        self.value = value
        self.called = False
        self.args: tuple[object, ...] = ()

    def __call__(self, *args: object) -> T:
        self.called = True
        self.args = args
        return self.value


class _PrebuildSpy:
    """A ``run_prebuild`` spy recording the EXCLUSION set it received.

    DEC-PREBUILD-CLEANSET-001: ``run_prebuild`` is
    ``Callable[[frozenset[str]], PrebuildOutcome]`` — the sequencer passes the
    UNCAPPED quarantined-name set; this spy captures it for assertion.
    """

    def __init__(self, value: PrebuildOutcome) -> None:
        self.value = value
        self.called = False
        self.exclude_symbols: frozenset[str] | None = None

    def __call__(self, exclude_symbols: frozenset[str]) -> PrebuildOutcome:
        self.called = True
        self.exclude_symbols = exclude_symbols
        return self.value


def test_pipeline_defers_whole_chain_inside_market_hours() -> None:
    """DEC-GROWW-MARKET-HOURS-GUARD-001: an active guard skips the ENTIRE vendor chain."""
    spot = _Spy(_spot())
    meta = _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))
    prebuild = _PrebuildSpy(_outcome())

    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        run_prebuild=prebuild,
        market_closed=lambda: True,
    )

    assert report.stopped_after == PHASE_MARKET_HOURS
    assert report.spot is None
    assert report.metadata is None
    assert report.historical is None
    assert report.ok is False
    # NO phase ran — not even spot; and the non-vendor prebuild is skipped too.
    assert spot.called is False
    assert meta.called is False
    assert hist.called is False
    assert prebuild.called is False
    assert "market-hours" in report.note.lower()


def test_pipeline_market_closed_false_runs_normally() -> None:
    """market_closed returning False (guard inactive) → the pipeline runs as before."""
    spot = _Spy(_spot())
    meta = _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))

    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        market_closed=lambda: False,
    )

    assert report.stopped_after is None
    assert spot.called is True


def test_pipeline_skips_metadata_when_belt_closes_after_spot() -> None:
    """DEC-GROWW-MARKET-HOURS-GUARD-001 MAJOR-2: a belt that CLOSES between the spot
    and metadata phases skips metadata + historical — spot's committed work is kept.

    The nightly pull started off-hours (belt open at the pre-spot check), spot ran to
    completion, and by the time the sequencer re-checks the belt BEFORE metadata the
    clock has crossed 08:45 — so no metadata/historical vendor calls fire.
    """
    calls = {"n": 0}

    def _belt() -> bool:
        # False before spot (call 1), True before metadata (call 2+) — the clock
        # crossed 08:45 during the spot phase.
        calls["n"] += 1
        return calls["n"] >= 2

    spot = _Spy(_spot())
    meta = _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))

    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        market_closed=_belt,
    )

    assert report.stopped_after == PHASE_MARKET_HOURS
    assert report.spot is not None  # spot's committed work is kept
    assert report.metadata is None
    assert report.historical is None
    assert report.ok is False
    assert spot.called is True
    assert meta.called is False  # the metadata vendor phase never fired
    assert hist.called is False
    assert "market-hours" in report.note.lower()


def test_pipeline_skips_historical_when_belt_closes_after_metadata() -> None:
    """DEC-GROWW-MARKET-HOURS-GUARD-001 MAJOR-2: a belt that CLOSES between the metadata
    and historical phases skips the F&O historical vendor phase — spot + metadata kept."""
    calls = {"n": 0}

    def _belt() -> bool:
        # False before spot (1) and before metadata (2); True before historical (3).
        calls["n"] += 1
        return calls["n"] >= 3

    spot = _Spy(_spot())
    meta = _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))

    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        market_closed=_belt,
    )

    assert report.stopped_after == PHASE_MARKET_HOURS
    assert report.spot is not None
    assert report.metadata is not None  # metadata's committed work is kept
    assert report.historical is None
    assert report.ok is False
    assert spot.called is True
    assert meta.called is True
    assert hist.called is False  # the F&O historical vendor phase never fired
    assert "market-hours" in report.note.lower()


def test_pipeline_halts_when_metadata_phase_crosses_market_hours() -> None:
    """DEC-GROWW-MARKET-HOURS-GUARD-001 MAJOR-2: a metadata phase whose clock CROSSES
    08:45 mid-phase halts — its per-cell belt raises MarketHoursHalt, re-raised by
    run_two_phase, and the sequencer catches it, keeping spot's work + resuming later.

    The belt reads OPEN at every between-phase check (it opened AFTER those reads),
    so only the metadata phase's own per-cell guard can stop it — modelled here by a
    run_metadata that raises :class:`MarketHoursHalt`.
    """
    from brutex.data.errors import MarketHoursHalt

    def _metadata_that_crosses() -> MetadataPullReport:
        raise MarketHoursHalt()

    spot = _Spy(_spot())
    hist = _Spy(_composite(historical=_historical_inner()))

    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=_metadata_that_crosses,
        run_historical=hist,
        market_closed=lambda: False,  # every between-phase check reads OPEN
    )

    assert report.stopped_after == PHASE_MARKET_HOURS
    assert report.spot is not None  # spot's committed work is kept
    assert report.metadata is None
    assert report.historical is None
    assert report.ok is False
    assert hist.called is False  # the historical vendor phase never fired
    assert "market-hours" in report.note.lower()


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


def test_pipeline_stops_after_spot_mass_empty_halt() -> None:
    """A spot mass-empty halt STOPS the chain like the sibling auth/quota halts.

    Review fix (round 1 MAJOR): the sequencer previously proceeded to the
    metadata + F&O vendor calls after a needs-operator spot halt (a suspected
    vendor soft-throttle episode — the LAST thing to hammer with more calls),
    and under ``BRUTEX_PIPELINE_STRICT_GATE=1`` the "all-or-nothing" barrier
    fired the post-steps over it.
    """
    spot = _Spy(_spot(completed=0, mass_empty_cause="mass clean-empty responses"))
    meta = _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))

    report = run_full_pipeline(run_spot=spot, run_metadata=meta, run_historical=hist)

    assert report.stopped_after == PHASE_SPOT
    assert report.metadata is None
    assert report.historical is None
    assert report.ok is False
    assert meta.called is False
    assert hist.called is False
    # The stop note names the loud needs-operator verdict, never a generic stop.
    assert "mass_empty_halt" in report.note


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
    # DEC-GREEKS-ENRICH-OBSERVE-001 hist-is-None guard: no candle sub-report →
    # no Greeks pass ran → honestly no clause (never a fabricated verdict).
    assert "Greeks enrichment FAILED" not in report.note


def test_pipeline_complete_note_names_greeks_enrich_failure() -> None:
    """A WHOLE-pass Greeks failure on the candle sub-report is NAMED in the note.

    DEC-GREEKS-ENRICH-OBSERVE-001 — the pipeline's completion headline (the job
    finish detail) must carry the failure clause, never a swallowed log line.
    """
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(
            _composite(
                historical=_historical_inner(
                    greeks_enrich_ok=False,
                    greeks_enrich_error="RuntimeError: misconfigured risk-free source",
                )
            )
        ),
    )
    assert report.stopped_after is None
    # The clause carries the ErrorType class, uniform with the historical note.
    assert (
        ", Greeks enrichment FAILED (RuntimeError) — overlay re-attempted next pull" in report.note
    )


def test_pipeline_complete_note_has_no_greeks_clause_when_pass_completed() -> None:
    """A completed Greeks pass (ok=True) leaves the pipeline note byte-identical."""
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner(greeks_enrich_ok=True))),
    )
    assert "Greeks enrichment FAILED" not in report.note


# --------------------------------------------------------------------------- #
# run_full_pipeline — movers warm AFTER the prebuild (DEC-PIPELINE-AUTOPREBUILD-001)
# --------------------------------------------------------------------------- #


def _warm(n: int, *, failed: int = 0) -> MoversWarmResult:
    """A warm-result literal for the warm spies (DEC-MOVERS-WARM-PROGRESS-001)."""
    return MoversWarmResult(warmed=n, failed=failed, total=n + failed)


def _clean_phases() -> tuple[
    _Spy[BulkSpotReport], _Spy[MetadataPullReport], _Spy[CompositePullReport]
]:
    """Three clean phase spies (spot / metadata / historical) for a fully-successful run."""
    return (
        _Spy(_spot()),
        _Spy(_metadata()),
        _Spy(_composite(historical=_historical_inner())),
    )


def test_pipeline_warms_all_segments_after_prebuild() -> None:
    # All three fetch phases clean + prebuild + all three warmers injected → the
    # prebuild runs, THEN each segment warms; the per-segment map carries each
    # count, the total is the sum, and the note names every warmed segment.
    spot, meta, hist = _clean_phases()
    w_spot, w_fut, w_opt = _Spy(_warm(3)), _Spy(_warm(1)), _Spy(_warm(4))
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=2, total_bars=50)),
    )
    assert report.ok is True
    assert w_spot.called and w_fut.called and w_opt.called
    assert report.segment_movers_warmed == {"spot": 3, "futures": 1, "options": 4}
    assert report.movers_warmed == 8
    assert "movers spot 3 + futures 1 + options 4 snapshot(s) warmed" in report.note
    payload = report.to_dict()
    assert payload["movers_warmed"] == 8
    assert payload["segment_movers_warmed"] == {"spot": 3, "futures": 1, "options": 4}


def test_pipeline_order_prebuild_runs_before_any_movers_warm() -> None:
    """ORDER REGRESSION (DEC-PIPELINE-AUTOPREBUILD-001): prebuild BEFORE every warm.

    Movers read the derived timeframes only the prebuild writes now, so a warm
    firing before the prebuild would cache stale/absent partitions.
    """
    order: list[str] = []

    def _prebuild(_exclude_symbols: frozenset[str]) -> PrebuildOutcome:
        order.append("prebuild")
        return _outcome()

    def _warm_seg(segment: str) -> Callable[[], MoversWarmResult]:
        def _w() -> MoversWarmResult:
            order.append(f"warm_{segment}")
            return _warm(1)

        return _w

    spot, meta, hist = _clean_phases()
    run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=_warm_seg("spot"),
        warm_futures_movers=_warm_seg("futures"),
        warm_options_movers=_warm_seg("options"),
        run_prebuild=_prebuild,
    )
    assert order == ["prebuild", "warm_spot", "warm_futures", "warm_options"]


def test_pipeline_early_spot_warm_removed_on_metadata_stop() -> None:
    """REGRESSION FLIP (supersedes DEC-PIPELINE-SEGMENT-MOVERS-001's early warm):
    a clean spot phase followed by a metadata failure warms NOTHING — the old
    behaviour warmed the spot segment the moment spot succeeded, but derived
    data is now written only by the prebuild (which never ran here), so an
    early warm would read stale/absent partitions."""
    spot = _Spy(_spot())
    meta = _Spy(_metadata(contract_failures=1))  # metadata FAILS after spot
    hist = _Spy(_composite(historical=_historical_inner()))
    w_spot, w_fut, w_opt = _Spy(_warm(5)), _Spy(_warm(9)), _Spy(_warm(9))
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
        run_prebuild=_PrebuildSpy(_outcome()),
    )
    assert report.stopped_after == PHASE_METADATA
    assert hist.called is False  # historical never ran
    assert not (w_spot.called or w_fut.called or w_opt.called)  # NO early warm
    assert report.segment_movers_warmed == {}
    assert report.movers_warmed is None


def test_pipeline_no_warm_when_prebuild_absent() -> None:
    """Warmers injected but NO run_prebuild ⇒ no segment warms — the movers gate
    is "the prebuild ran AND succeeded" (there is no fresh derived data to
    warm over without the prebuild; DEC-PREBUILD-CLEANSET-001)."""
    spot, meta, hist = _clean_phases()
    w_spot, w_fut, w_opt = _Spy(_warm(2)), _Spy(_warm(2)), _Spy(_warm(2))
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
    )
    assert report.ok is True
    assert not (w_spot.called or w_fut.called or w_opt.called)
    assert report.segment_movers_warmed == {}
    assert report.movers_warmed is None
    assert "warmed" not in report.note


def test_pipeline_partial_warmers_warm_only_injected_segments() -> None:
    # Only futures + options warmers injected → spot absent from the map,
    # proving per-segment independence of the injection.
    spot, meta, hist = _clean_phases()
    w_fut, w_opt = _Spy(_warm(2)), _Spy(_warm(6))
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
        run_prebuild=_PrebuildSpy(_outcome()),
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
        warm_spot_movers=_Spy(_warm(0)),
        run_prebuild=_PrebuildSpy(_outcome()),
    )
    assert report.segment_movers_warmed == {"spot": 0}
    assert report.movers_warmed == 0
    assert "movers spot 0 snapshot(s) warmed" in report.note


def test_pipeline_dirty_fetch_still_prebuilds_clean_set_and_warms() -> None:
    """DECOUPLE REGRESSION (DEC-PREBUILD-CLEANSET-001 — supersedes the #1145
    fetch_clean all-or-nothing gate this test previously pinned): a historical
    fetch with ONE quarantined symbol still runs the prebuild — over the CLEAN
    set, receiving the quarantined underlying as its exclusion set — and a
    successful prebuild still warms the movers. One bad symbol out of N no
    longer leaves ZERO derived timeframes and cold movers for everything."""
    spot = _Spy(_spot())
    meta = _Spy(_metadata())
    hist = _Spy(
        _composite(
            historical=_historical_inner(
                failed_pairs=1,
                phase1_complete=("NSE-NIFTY-25Jan24-21000-CE",),
                start_dates={
                    "NSE-NIFTY-25Jan24-21000-CE": "2023-12-26",  # clean
                    "NSE-RELIANCE-29Feb24-2800-CE": "2024-01-30",  # quarantined → RELIANCE
                },
            )
        )
    )
    w_spot, w_fut, w_opt = _Spy(_warm(3)), _Spy(_warm(5)), _Spy(_warm(5))
    prebuild = _PrebuildSpy(_outcome())
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
        run_prebuild=prebuild,
    )
    assert report.ok is False  # the historical gate still reports the dirty fetch
    assert prebuild.called is True  # …but the prebuild RAN (the decouple)
    assert prebuild.exclude_symbols == frozenset({"RELIANCE"})  # over the clean set
    assert w_spot.called and w_fut.called and w_opt.called  # movers warmed (ok=True)
    assert report.segment_movers_warmed == {"spot": 3, "futures": 5, "options": 5}
    assert report.movers_warmed == 13
    assert "1 symbol(s) quarantined: RELIANCE — " in report.note  # named, never hidden


def test_pipeline_skips_all_warms_when_stopped_at_spot() -> None:
    # An early spot stop never warms ANY segment — no warmer is consulted.
    w_spot, w_fut, w_opt = _Spy(_warm(3)), _Spy(_warm(3)), _Spy(_warm(3))
    report = run_full_pipeline(
        run_spot=_Spy(_spot(auth_aborted=True)),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        warm_spot_movers=w_spot,
        warm_futures_movers=w_fut,
        warm_options_movers=w_opt,
        run_prebuild=_PrebuildSpy(_outcome()),
    )
    assert report.stopped_after == PHASE_SPOT
    assert not (w_spot.called or w_fut.called or w_opt.called)
    assert report.segment_movers_warmed == {}
    assert report.movers_warmed is None


def test_pipeline_no_movers_phase_when_no_warmers_injected() -> None:
    # Backward-compatible: with no warmers (and no prebuild), no segment warms
    # and movers_warmed is None — never a fabricated count.
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(run_spot=spot, run_metadata=meta, run_historical=hist)
    assert report.ok is True
    assert report.movers_warmed is None
    assert report.segment_movers_warmed == {}
    assert "warmed" not in report.note
    assert report.to_dict()["movers_warmed"] is None
    assert report.to_dict()["segment_movers_warmed"] == {}


# --------------------------------------------------------------------------- #
# run_full_pipeline — the GATED prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001)
# --------------------------------------------------------------------------- #


def test_pipeline_prebuilds_timeframes_on_clean_run() -> None:
    # All three fetch phases clean + a prebuild callable injected → the phase
    # runs, the outcome is folded into the report, and the note appends the
    # pre-build clause with the symbol AND bar counts.
    spot, meta = _Spy(_spot()), _Spy(_metadata())
    hist = _Spy(_composite(historical=_historical_inner()))
    prebuild = _PrebuildSpy(_outcome(symbols_built=4, total_bars=1234))
    report = run_full_pipeline(
        run_spot=spot, run_metadata=meta, run_historical=hist, run_prebuild=prebuild
    )
    assert report.ok is True
    assert prebuild.called is True
    assert report.prebuild_built == 4
    assert report.prebuild_total_bars == 1234
    assert report.prebuild_failed == ()
    assert "timeframes pre-built for 4 symbol(s) (1234 bars)" in report.note
    payload = report.to_dict()
    assert payload["prebuild_built"] == 4
    assert payload["prebuild_total_bars"] == 1234
    assert payload["prebuild_failed"] == []
    assert payload["prebuild_crashed"] is None  # completed run — never a crash marker


def test_pipeline_prebuild_crash_marker_renders_crashed_note_not_built_zero() -> None:
    """A guarded whole-run prebuild CRASH (hostile-review F2) reads
    ``", prebuild CRASHED (ErrorType)"`` — NEVER the benign "pre-built for 0
    symbol(s)" clause — and warms nothing (ok=False fails the warm gate)."""
    warmed: list[str] = []
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        warm_spot_movers=lambda: warmed.append("spot") or _warm(0),
        run_prebuild=_PrebuildSpy(
            _outcome(symbols_built=0, total_bars=0, ok=False, crashed="ValueError")
        ),
    )
    assert ", prebuild CRASHED (ValueError)" in report.note
    assert "pre-built for" not in report.note  # never the benign built-0 clause
    assert report.prebuild_crashed == "ValueError"
    assert report.prebuild_built == 0  # the raw counts still recorded honestly
    assert warmed == []  # a crashed prebuild warms NOTHING
    assert report.movers_warmed is None
    assert report.to_dict()["prebuild_crashed"] == "ValueError"


def test_pipeline_prebuild_zero_is_honest_not_none() -> None:
    # A prebuild that built nothing (e.g. an empty lake) returns 0 — recorded as 0
    # (ran, built nothing), DISTINCT from None (did not run).
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=0, total_bars=0)),
    )
    assert report.prebuild_built == 0
    assert report.prebuild_total_bars == 0
    assert "timeframes pre-built for 0 symbol(s) (0 bars)" in report.note


def test_pipeline_prebuild_failures_named_in_note() -> None:
    # Per-symbol prebuild failures are folded into the report AND named in the
    # note (up to 3 names) — honestly reported, never hidden.
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=7, total_bars=90, failed=("AAA", "BBB"))),
    )
    assert report.prebuild_failed == ("AAA", "BBB")
    assert "timeframes pre-built for 7 symbol(s) (90 bars)" in report.note
    assert "prebuild failures: AAA, BBB" in report.note
    assert "more)" not in report.note  # ≤ 3 names → no remainder suffix
    assert report.to_dict()["prebuild_failed"] == ["AAA", "BBB"]


def test_pipeline_prebuild_failure_names_capped_with_true_remainder() -> None:
    # The note shows at most 3 names + the TRUE remainder from failed_count —
    # here the producer capped 12 real failures to 10 names, so the note reads
    # "(+9 more)" (12 − 3), NOT "(+7 more)" (10 − 3): the uncapped total wins.
    failed = tuple(f"SYM{i}" for i in range(10))  # producer-capped names
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_prebuild=_PrebuildSpy(
            _outcome(symbols_built=1, total_bars=5, failed=failed, failed_count=12)
        ),
    )
    assert "prebuild failures: SYM0, SYM1, SYM2 (+9 more)" in report.note


def test_pipeline_prebuild_runs_on_dirty_fetch_completed_chain() -> None:
    """FLIP of ``test_pipeline_skips_prebuild_when_historical_not_clean``
    (DEC-PREBUILD-CLEANSET-001): a historical fetch that completed-with-failures
    (``failed_pairs``) now STILL pre-builds — the fetch_clean gate is dropped;
    the outcome is folded into the report and the note carries the clause."""
    hist = _Spy(_composite(historical=_historical_inner(failed_pairs=1)))
    prebuild = _PrebuildSpy(_outcome(symbols_built=4, total_bars=100))
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=hist,
        run_prebuild=prebuild,
    )
    assert report.ok is False  # historical gate failed — reported honestly
    assert prebuild.called is True  # …but the prebuild RAN over the clean set
    # This fixture's failed_pairs residual names no unclean contract
    # (start_dates == phase1_complete), so the exclusion set is empty.
    assert prebuild.exclude_symbols == frozenset()
    assert report.prebuild_built == 4
    assert report.prebuild_total_bars == 100
    assert "timeframes pre-built for 4 symbol(s) (100 bars)" in report.note


def test_pipeline_clean_run_passes_empty_exclusion_set() -> None:
    """CLEAN-RUN PRESERVATION (DEC-PREBUILD-CLEANSET-001): a fully-clean fetch
    prebuilds exactly as before — the exclusion set it receives is EMPTY, so
    the prebuild's coverage is behaviour-identical to the pre-cleanset run."""
    prebuild = _PrebuildSpy(_outcome(symbols_built=4, total_bars=1234))
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_prebuild=prebuild,
    )
    assert report.ok is True
    assert prebuild.called is True
    assert prebuild.exclude_symbols == frozenset()  # nothing quarantined → nothing excluded
    assert "timeframes pre-built for 4 symbol(s) (1234 bars)" in report.note


def test_pipeline_prebuild_receives_uncapped_exclusion_beyond_display_cap() -> None:
    """THE CAP TRAP (DEC-PREBUILD-CLEANSET-001, implementer trap #4): with MORE
    than :data:`MAX_QUARANTINED_NAMES` quarantined symbols, the prebuild's
    exclusion set carries ALL of them (here 12) — the 10-name cap is a DISPLAY
    concern only (headline + report tuple + honest ``(+2 more)`` remainder).
    Excluding by the capped tuple would silently prebuild the 11th + 12th
    quarantined symbols over dirty/absent 1-minute data."""
    spot = _spot_with_failures(
        *(
            SymbolPullResult(symbol=f"SYM{i:02d}", completed=0, failed=1, no_data=0)
            for i in range(12)
        )
    )
    prebuild = _PrebuildSpy(_outcome())
    report = run_full_pipeline(
        run_spot=_Spy(spot),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        run_prebuild=prebuild,
    )
    assert prebuild.called is True
    assert prebuild.exclude_symbols == frozenset(f"SYM{i:02d}" for i in range(12))  # ALL 12
    assert len(report.quarantined_symbols) == MAX_QUARANTINED_NAMES == 10  # display capped
    assert report.quarantined_count == 12  # the TRUE total
    assert "12 symbol(s) quarantined: " in report.note
    assert "(+2 more)" in report.note


def test_pipeline_skips_prebuild_when_stopped_at_spot() -> None:
    # An early stop never reaches the final phase — the prebuild is not consulted.
    prebuild = _PrebuildSpy(_outcome())
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
    # Backward-compatible: with no prebuild callable, the phase is skipped
    # entirely and prebuild_built is None — never a fabricated count.
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
    )
    assert report.ok is True
    assert report.prebuild_built is None
    assert report.prebuild_total_bars is None
    assert "pre-built" not in report.note
    assert report.to_dict()["prebuild_built"] is None
    assert report.to_dict()["prebuild_total_bars"] is None


def test_pipeline_failed_prebuild_outcome_recorded_but_no_warm() -> None:
    # CHUNK-2 FLIP of `test_pipeline_failed_prebuild_outcome_still_recorded_and_warms`
    # (DEC-PIPELINE-AUTOPREBUILD-001): the warm gate is TIGHTENED from "the prebuild
    # RAN" to "the prebuild SUCCEEDED" (`PrebuildOutcome.ok`). An ok=False outcome
    # (the guarded crash path, or per-symbol failures) is STILL folded into the
    # report honestly — but NO movers warm fires (a failed prebuild's derived
    # partitions may be stale/absent, so warming would cache a wrong snapshot).
    w_spot = _Spy(_warm(2))
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        warm_spot_movers=w_spot,
        run_prebuild=_PrebuildSpy(
            _outcome(symbols_built=0, total_bars=0, failed=(), failed_count=0, ok=False)
        ),
    )
    assert report.prebuild_built == 0  # the ok=False outcome is still RECORDED
    assert report.prebuild_total_bars == 0
    assert w_spot.called is False  # …but the warm did NOT fire (chunk-2 gate)
    assert report.segment_movers_warmed == {}
    assert report.movers_warmed is None


def test_pipeline_successful_prebuild_warms() -> None:
    # The positive twin of the chunk-2 gate: an ok=True prebuild DOES warm.
    w_spot = _Spy(_warm(2))
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        warm_spot_movers=w_spot,
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=3, total_bars=30, ok=True)),
    )
    assert w_spot.called is True
    assert report.segment_movers_warmed == {"spot": 2}
    assert report.movers_warmed == 2


def test_pipeline_warms_movers_and_prebuilds_together() -> None:
    # Both gated post-fetch phases run on a clean fetch; the per-segment movers
    # counts + the prebuild counts + both note clauses are present.
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
        warm_spot_movers=_Spy(_warm(2)),
        warm_futures_movers=_Spy(_warm(1)),
        warm_options_movers=_Spy(_warm(1)),
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=5, total_bars=77)),
    )
    assert report.movers_warmed == 4  # 2 (spot) + 1 (futures) + 1 (options)
    assert report.segment_movers_warmed == {"spot": 2, "futures": 1, "options": 1}
    assert report.prebuild_built == 5
    assert report.prebuild_total_bars == 77
    assert "movers spot 2 + futures 1 + options 1 snapshot(s) warmed" in report.note
    assert "timeframes pre-built for 5 symbol(s) (77 bars)" in report.note


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


# --------------------------------------------------------------------------- #
# pipeline note — expiry scope visibility (DEC-FNO-EXPIRY-SCOPE-001)
# --------------------------------------------------------------------------- #


def test_pipeline_note_names_out_of_scope_expiries() -> None:
    """A scoped-out expiry count is NAMED in the pipeline complete-note."""
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(
            _composite(
                historical=_historical_inner(expiries_out_of_scope=3, contracts_out_of_scope=9)
            )
        ),
    )
    assert report.stopped_after is None
    assert ", 3 expiries out of scope skipped" in report.note


def test_pipeline_note_zero_out_of_scope_renders_no_clause() -> None:
    """The unscoped default renders byte-identically — no scope clause."""
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
    )
    assert "out of scope" not in report.note


# --------------------------------------------------------------------------- #
# quarantined-symbol headline honesty (DEC-PIPELINE-AUTOPREBUILD-001 chunk 3)
# --------------------------------------------------------------------------- #


def _spot_with_failures(*symbols: SymbolPullResult) -> BulkSpotReport:
    """A spot report carrying an explicit per-symbol result list (gate-clean)."""
    return BulkSpotReport(
        dry_run=False,
        start_date="2020-01-01",
        end_date="2026-06-20",
        symbols=list(symbols),
    )


class TestDeriveQuarantinedSymbols:
    """The pure name derivation — spot / historical / both / dedupe / cap."""

    def test_clean_run_is_empty(self) -> None:
        assert derive_quarantined_symbols(_spot(), _composite(historical=_historical_inner())) == (
            (),
            0,
        )

    def test_spot_only_failed_symbols_named(self) -> None:
        spot = _spot_with_failures(
            SymbolPullResult(symbol="NIFTY", completed=1, failed=1, no_data=0),
            SymbolPullResult(symbol="SENSEX", completed=2, failed=0, no_data=0),
        )
        assert derive_quarantined_symbols(spot, None) == (("NIFTY",), 1)

    def test_historical_only_unclean_contracts_fold_to_underlyings(self) -> None:
        hist = _historical_inner(
            failed_pairs=1,
            phase1_complete=("NSE-NIFTY-25Jan24-21000-CE",),
            start_dates={
                "NSE-NIFTY-25Jan24-21000-CE": "2023-12-26",  # clean — not named
                "NSE-RELIANCE-29Feb24-2800-CE": "2024-01-30",  # unclean → RELIANCE
                "NSE-RELIANCE-29Feb24-2800-PE": "2024-01-30",  # same underlying — deduped
            },
        )
        names, count = derive_quarantined_symbols(_spot(), _composite(historical=hist))
        assert names == ("RELIANCE",)
        assert count == 1

    def test_undecodable_contract_symbol_kept_raw(self) -> None:
        hist = _historical_inner(
            phase1_complete=(),
            start_dates={"RELIANCE25JAN3000CE": "2024-10-01"},  # no hyphens → undecodable
        )
        names, count = derive_quarantined_symbols(_spot(), _composite(historical=hist))
        assert names == ("RELIANCE25JAN3000CE",)  # reported raw, never dropped
        assert count == 1

    def test_both_phases_deduped_and_sorted(self) -> None:
        spot = _spot_with_failures(
            SymbolPullResult(symbol="RELIANCE", completed=0, failed=1, no_data=0),
            SymbolPullResult(symbol="AXISBANK", completed=0, failed=2, no_data=0),
        )
        hist = _historical_inner(
            phase1_complete=(),
            start_dates={"NSE-RELIANCE-29Feb24-2800-CE": "2024-01-30"},
        )
        names, count = derive_quarantined_symbols(spot, _composite(historical=hist))
        assert names == ("AXISBANK", "RELIANCE")  # deduped across phases, sorted
        assert count == 2

    def test_cap_at_ten_names_with_true_total(self) -> None:
        spot = _spot_with_failures(
            *(
                SymbolPullResult(symbol=f"SYM{i:02d}", completed=0, failed=1, no_data=0)
                for i in range(12)
            )
        )
        names, count = derive_quarantined_symbols(spot, None)
        assert len(names) == MAX_QUARANTINED_NAMES == 10
        assert names == tuple(f"SYM{i:02d}" for i in range(10))  # sorted, capped
        assert count == 12  # the TRUE uncapped total

    def test_none_reports_are_empty(self) -> None:
        assert derive_quarantined_symbols(None, None) == ((), 0)

    def test_composite_without_candle_subreport_is_empty(self) -> None:
        assert derive_quarantined_symbols(_spot(), _composite(historical=None)) == ((), 0)


class TestDeriveQuarantinedNameSet:
    """The UNCAPPED exclusion-set twin (DEC-PREBUILD-CLEANSET-001)."""

    def test_clean_run_is_empty_frozenset(self) -> None:
        assert (
            derive_quarantined_name_set(_spot(), _composite(historical=_historical_inner()))
            == frozenset()
        )

    def test_none_reports_are_empty(self) -> None:
        assert derive_quarantined_name_set(None, None) == frozenset()

    def test_uncapped_beyond_display_cap(self) -> None:
        spot = _spot_with_failures(
            *(
                SymbolPullResult(symbol=f"SYM{i:02d}", completed=0, failed=1, no_data=0)
                for i in range(12)
            )
        )
        names = derive_quarantined_name_set(spot, None)
        assert names == frozenset(f"SYM{i:02d}" for i in range(12))  # NEVER capped
        assert len(names) == 12 > MAX_QUARANTINED_NAMES

    def test_folds_and_dedupes_across_phases(self) -> None:
        spot = _spot_with_failures(
            SymbolPullResult(symbol="RELIANCE", completed=0, failed=1, no_data=0),
            SymbolPullResult(symbol="AXISBANK", completed=0, failed=2, no_data=0),
        )
        hist = _historical_inner(
            phase1_complete=(),
            start_dates={"NSE-RELIANCE-29Feb24-2800-CE": "2024-01-30"},
        )
        names = derive_quarantined_name_set(spot, _composite(historical=hist))
        assert names == frozenset({"AXISBANK", "RELIANCE"})  # folded + deduped

    def test_capped_variant_is_sorted_prefix_of_the_set(self) -> None:
        """ONE-SOURCE-OF-TRUTH pin: ``derive_quarantined_symbols`` is exactly
        the sorted 10-name prefix of this set + its TRUE length."""
        spot = _spot_with_failures(
            *(
                SymbolPullResult(symbol=f"Z{i:02d}", completed=0, failed=1, no_data=0)
                for i in range(15)
            )
        )
        hist = _historical_inner(
            phase1_complete=(),
            start_dates={"NSE-RELIANCE-29Feb24-2800-CE": "2024-01-30"},
        )
        composite = _composite(historical=hist)
        names = derive_quarantined_name_set(spot, composite)
        capped, count = derive_quarantined_symbols(spot, composite)
        assert capped == tuple(sorted(names))[:MAX_QUARANTINED_NAMES]
        assert count == len(names)
        assert set(capped) <= names  # the capped view never invents a name

    @given(
        symbols=st.lists(
            st.text(
                alphabet=st.characters(min_codepoint=48, max_codepoint=90),
                min_size=1,
                max_size=12,
            ),
            max_size=25,
        )
    )
    def test_property_capped_is_sorted_prefix_and_set_is_superset(self, symbols: list[str]) -> None:
        """PROPERTY (DEC-PREBUILD-CLEANSET-001): for ANY spot failure list, the
        capped display view is the sorted deduped prefix of the UNCAPPED set,
        the TRUE count is the set's size, and the set is a superset of the
        capped names — the cap can never leak into the exclusion set."""
        spot = _spot_with_failures(
            *(
                SymbolPullResult(symbol=symbol, completed=0, failed=1, no_data=0)
                for symbol in symbols
            )
        )
        names = derive_quarantined_name_set(spot, None)
        capped, count = derive_quarantined_symbols(spot, None)
        assert names == frozenset(symbols)  # deduped, uncapped
        assert count == len(names)  # TRUE total
        assert capped == tuple(sorted(names))[:MAX_QUARANTINED_NAMES]  # sorted prefix
        assert set(capped) <= names  # superset invariant
        assert len(capped) <= MAX_QUARANTINED_NAMES


class TestQuarantinedHeadline:
    """The complete-note wording + the report fields through run_full_pipeline."""

    def test_complete_note_names_quarantined_symbols(self) -> None:
        spot = _spot_with_failures(
            SymbolPullResult(symbol="NIFTY", completed=1, failed=1, no_data=0)
        )
        report = run_full_pipeline(
            run_spot=_Spy(spot),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=_historical_inner())),
        )
        assert report.stopped_after is None
        assert "pipeline complete — 1 symbol(s) quarantined: NIFTY — " in report.note
        assert report.quarantined_symbols == ("NIFTY",)
        assert report.quarantined_count == 1
        assert report.to_dict()["quarantined_symbols"] == ["NIFTY"]
        assert report.to_dict()["quarantined_count"] == 1

    def test_complete_note_caps_names_with_true_remainder(self) -> None:
        spot = _spot_with_failures(
            *(
                SymbolPullResult(symbol=f"SYM{i:02d}", completed=0, failed=1, no_data=0)
                for i in range(12)
            )
        )
        report = run_full_pipeline(
            run_spot=_Spy(spot),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=_historical_inner())),
        )
        assert "12 symbol(s) quarantined: " in report.note
        assert "(+2 more)" in report.note  # true remainder beyond the 10-name cap
        assert len(report.quarantined_symbols) == 10
        assert report.quarantined_count == 12

    def test_clean_pull_keeps_current_wording(self) -> None:
        report = run_full_pipeline(
            run_spot=_Spy(_spot()),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=_historical_inner())),
        )
        assert "quarantined" not in report.note  # byte-identical clean wording
        assert report.note.startswith("pipeline complete — spot ")
        assert report.quarantined_symbols == ()
        assert report.quarantined_count == 0

    def test_historical_quarantine_reaches_the_note(self) -> None:
        hist = _historical_inner(
            failed_pairs=0,  # gate-clean: a still-failing TRANSIENT residual is 0 too —
            phase1_complete=(),  # …but the contract did NOT land clean (not marked)
            start_dates={"NSE-RELIANCE-29Feb24-2800-CE": "2024-01-30"},
        )
        report = run_full_pipeline(
            run_spot=_Spy(_spot()),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=hist)),
        )
        assert "1 symbol(s) quarantined: RELIANCE — " in report.note

    def test_spot_stop_report_still_carries_quarantined_fields(self) -> None:
        spot = BulkSpotReport(
            dry_run=False,
            start_date="2020-01-01",
            end_date="2026-06-20",
            symbols=[SymbolPullResult(symbol="NIFTY", completed=0, failed=1, no_data=0)],
            still_failed_windows=2,  # gate-failing → the chain stops after spot
        )
        report = run_full_pipeline(
            run_spot=_Spy(spot),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=_historical_inner())),
        )
        assert report.stopped_after == PHASE_SPOT
        assert report.note.startswith("stopped after spot")  # the loud stop note wins
        assert report.quarantined_symbols == ("NIFTY",)  # the fields still report
        assert report.quarantined_count == 1

    def test_metadata_stop_report_still_carries_quarantined_fields(self) -> None:
        spot = _spot_with_failures(
            SymbolPullResult(symbol="NIFTY", completed=1, failed=1, no_data=0)
        )
        report = run_full_pipeline(
            run_spot=_Spy(spot),
            run_metadata=_Spy(_metadata(expiry_failures=1)),
            run_historical=_Spy(_composite(historical=_historical_inner())),
        )
        assert report.stopped_after == PHASE_METADATA
        assert report.quarantined_symbols == ("NIFTY",)
        assert report.quarantined_count == 1


# --------------------------------------------------------------------------- #
# LOUD movers-warm logs (DEC-PIPELINE-AUTOPREBUILD-001 — part D completeness)
# --------------------------------------------------------------------------- #


def test_pipeline_movers_warm_logs_started_and_done() -> None:
    """The movers-warm phase logs ``pipeline.movers_warm.started`` + ``.done``."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        run_full_pipeline(
            run_spot=_Spy(_spot()),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=_historical_inner())),
            warm_spot_movers=lambda: _warm(3),
            warm_futures_movers=lambda: _warm(1),
            run_prebuild=lambda _exclude_symbols: _outcome(),
        )
    started = [e for e in logs if e["event"] == "pipeline.movers_warm.started"]
    done = [e for e in logs if e["event"] == "pipeline.movers_warm.done"]
    assert len(started) == 1
    assert started[0]["segments"] == ["spot", "futures"]
    assert len(done) == 1
    assert done[0]["warmed"] == {"spot": 3, "futures": 1}


# ── coarse-allocation note (DEC-LAKE-FS-PROBE-001) ───────────────────────────


def test_pipeline_report_carries_fs_allocation_note_and_names_it() -> None:
    """A COARSE prebuild fs_note rides the report + the ⚠-prefixed note clause."""
    spot, meta, hist = _clean_phases()
    note = "COARSE filesystem allocation at /lake: one tiny file consumes 128 KiB"
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        run_prebuild=lambda _exclude_symbols: _outcome(fs_note=note),
    )
    assert report.fs_allocation_note == note
    assert f", ⚠ {note}" in report.note
    assert report.to_dict()["fs_allocation_note"] == note


def test_pipeline_report_fs_note_absent_when_fine_grained() -> None:
    """DEFAULT-PRESERVATION: no fs_note ⇒ None field, no ⚠ clause, key present."""
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        run_prebuild=lambda _exclude_symbols: _outcome(),
    )
    assert report.fs_allocation_note is None
    assert "⚠ COARSE" not in report.note
    assert report.to_dict()["fs_allocation_note"] is None


def test_pipeline_movers_warm_failed_jobs_named_in_note() -> None:
    """DEC-MOVERS-WARM-PROGRESS-001: swallowed best-effort warm failures are
    COUNTED per segment + NAMED in the completion note — never hidden behind
    the warmed counts (Golden Rule #1)."""
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=_Spy(_warm(2, failed=1)),
        warm_futures_movers=_Spy(_warm(1, failed=2)),
        warm_options_movers=_Spy(_warm(4)),
        run_prebuild=_PrebuildSpy(_outcome()),
    )
    assert report.segment_movers_failed == {"spot": 1, "futures": 2}
    assert report.segment_movers_warmed == {"spot": 2, "futures": 1, "options": 4}
    assert "movers spot 2 + futures 1 + options 4 snapshot(s) warmed" in report.note
    assert "(3 warm job(s) FAILED)" in report.note
    assert report.to_dict()["segment_movers_failed"] == {"spot": 1, "futures": 2}


def test_pipeline_movers_warm_no_failures_keeps_note_clean() -> None:
    """Zero failed warm jobs ⇒ no FAILED clause (the clean note is byte-stable)."""
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        warm_spot_movers=_Spy(_warm(3)),
        run_prebuild=_PrebuildSpy(_outcome()),
    )
    assert report.segment_movers_failed == {}
    assert "FAILED" not in report.note
    assert report.to_dict()["segment_movers_failed"] == {}


def test_pipeline_movers_warm_done_log_carries_failed_map() -> None:
    """The pipeline.movers_warm.done log names the per-segment failed counts."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        run_full_pipeline(
            run_spot=_Spy(_spot()),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=_historical_inner())),
            warm_spot_movers=lambda: _warm(3, failed=2),
            run_prebuild=lambda _exclude_symbols: _outcome(),
        )
    done = [e for e in logs if e["event"] == "pipeline.movers_warm.done"]
    assert len(done) == 1
    assert done[0]["warmed"] == {"spot": 3}
    assert done[0]["failed"] == {"spot": 2}


def test_pipeline_note_names_incremental_unchanged_skips() -> None:
    """DEC-PREBUILD-INCREMENTAL-001: the prebuild clause names the skipped-
    unchanged count so the incremental saving is visible, never a fake
    "built with 0 bars"."""
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=5, total_bars=40, unchanged_skips=3)),
    )
    assert "timeframes pre-built for 5 symbol(s) (40 bars)" in report.note
    assert ", 3 unchanged symbol(s) skipped (incremental)" in report.note


def test_pipeline_note_omits_skip_clause_when_none_skipped() -> None:
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=5, total_bars=40)),
    )
    assert "unchanged symbol(s) skipped" not in report.note


# ── daily-open substitution headline + report (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001) ──


def test_pipeline_note_names_daily_opens_substituted() -> None:
    """The completion headline names the observe-only substitution total."""
    spot, meta, hist = _clean_phases()
    report = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=5, total_bars=40, opens_substituted=4)),
    )
    assert ", 4 daily open(s) substituted" in report.note
    assert report.prebuild_opens_substituted == 4
    assert report.to_dict()["prebuild_opens_substituted"] == 4


def test_pipeline_note_zero_substituted_renders_no_clause() -> None:
    """Zero substitutions render NO substitution clause (a substring-ABSENCE
    pin — precise about what it asserts: the clause append is the only note
    change and is gated on truthiness, so the zero path leaves the note
    structurally identical to main; the pre-existing note goldens pin the
    remainder of the string). The feature is invisible on a healthy lake
    (Golden Rule #1 default preservation)."""
    spot, meta, hist = _clean_phases()
    zero = run_full_pipeline(
        run_spot=spot,
        run_metadata=meta,
        run_historical=hist,
        run_prebuild=_PrebuildSpy(_outcome(symbols_built=5, total_bars=40)),
    )
    assert "daily open(s) substituted" not in zero.note
    assert zero.prebuild_opens_substituted == 0
    assert zero.to_dict()["prebuild_opens_substituted"] == 0


def test_pipeline_report_substituted_none_when_prebuild_not_run() -> None:
    """No prebuild injected → the field is honestly ``None`` (did not run),
    never a fabricated 0 (ran, observed nothing)."""
    report = run_full_pipeline(
        run_spot=_Spy(_spot()),
        run_metadata=_Spy(_metadata()),
        run_historical=_Spy(_composite(historical=_historical_inner())),
    )
    assert report.prebuild_opens_substituted is None
    assert report.to_dict()["prebuild_opens_substituted"] is None


# --------------------------------------------------------------------------- #
# strict post-step gate (DEC-PULL-SIX-STEP-ORDER-001) — T6
# --------------------------------------------------------------------------- #


class TestStrictGateEnabled:
    """The fail-loud ``BRUTEX_PIPELINE_STRICT_GATE`` parse matrix."""

    def test_unset_is_false(self) -> None:
        assert strict_gate_enabled(env={}) is False

    def test_blank_is_false(self) -> None:
        assert strict_gate_enabled(env={STRICT_GATE_ENV: ""}) is False

    def test_whitespace_is_false(self) -> None:
        assert strict_gate_enabled(env={STRICT_GATE_ENV: "   "}) is False

    def test_zero_is_false(self) -> None:
        assert strict_gate_enabled(env={STRICT_GATE_ENV: "0"}) is False

    def test_one_is_true(self) -> None:
        assert strict_gate_enabled(env={STRICT_GATE_ENV: "1"}) is True

    def test_padded_one_is_true(self) -> None:
        assert strict_gate_enabled(env={STRICT_GATE_ENV: " 1 "}) is True

    @pytest.mark.parametrize("garbage", ["true", "yes", "on", "2", "strict", "01"])
    def test_garbage_raises_value_error(self, garbage: str) -> None:
        # Fail-LOUD (Golden Rule #1): a broken knob must never silently fall back.
        with pytest.raises(ValueError, match=STRICT_GATE_ENV):
            strict_gate_enabled(env={STRICT_GATE_ENV: garbage})

    def test_none_env_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STRICT_GATE_ENV, "1")
        assert strict_gate_enabled() is True
        monkeypatch.delenv(STRICT_GATE_ENV)
        assert strict_gate_enabled() is False


class TestStepSettleVerdict:
    """The pure barrier predicate over ALL input combinations."""

    @pytest.mark.parametrize("spot_ok", [True, False])
    @pytest.mark.parametrize("metadata_ok", [True, False])
    @pytest.mark.parametrize("historical_ok", [True, False])
    @pytest.mark.parametrize("quarantined", [frozenset(), frozenset({"NIFTY", "ADANIENT"})])
    def test_full_matrix(
        self,
        spot_ok: bool,
        metadata_ok: bool,
        historical_ok: bool,
        quarantined: frozenset[str],
    ) -> None:
        verdict = step_settle_verdict(
            spot_ok=spot_ok,
            metadata_ok=metadata_ok,
            historical_ok=historical_ok,
            quarantined=quarantined,
        )
        expected_settled = spot_ok and metadata_ok and historical_ok
        assert verdict.settled is expected_settled
        assert verdict.clean is (expected_settled and not quarantined)
        # blocking is empty IFF clean, and names every failing signal.
        assert (verdict.blocking == ()) is verdict.clean
        assert ("spot unclean" in verdict.blocking) is (not spot_ok)
        assert ("metadata unclean" in verdict.blocking) is (not metadata_ok)
        assert ("historical unclean" in verdict.blocking) is (not historical_ok)
        assert ("2 quarantined symbol(s)" in verdict.blocking) is bool(quarantined)

    def test_is_frozen_value_object(self) -> None:
        verdict = step_settle_verdict(
            spot_ok=True, metadata_ok=True, historical_ok=True, quarantined=frozenset()
        )
        assert verdict == StepSettleVerdict(settled=True, clean=True, blocking=())
        with pytest.raises(AttributeError):
            verdict.clean = False  # type: ignore[misc]


class TestPriorStepsVerdict:
    """The prior-step verdict value object + its sequencer threading."""

    @pytest.mark.parametrize("spot_clean", [True, False])
    @pytest.mark.parametrize("metadata_clean", [True, False])
    def test_prior_clean_truth_table(self, spot_clean: bool, metadata_clean: bool) -> None:
        prior = PriorStepsVerdict(spot_clean=spot_clean, metadata_clean=metadata_clean, strict=True)
        assert prior.prior_clean is (spot_clean and metadata_clean)

    def test_clean_run_threads_clean_verdict(self) -> None:
        hist = _Spy(_composite(historical=_historical_inner()))
        run_full_pipeline(
            run_spot=_Spy(_spot()), run_metadata=_Spy(_metadata()), run_historical=hist
        )
        assert hist.args == (PriorStepsVerdict(spot_clean=True, metadata_clean=True, strict=False),)

    def test_strict_flag_echoed_into_verdict(self) -> None:
        hist = _Spy(_composite(historical=_historical_inner()))
        run_full_pipeline(
            run_spot=_Spy(_spot()),
            run_metadata=_Spy(_metadata()),
            run_historical=hist,
            strict_gate=True,
        )
        assert hist.args == (PriorStepsVerdict(spot_clean=True, metadata_clean=True, strict=True),)

    def test_spot_quarantine_reads_spot_unclean(self) -> None:
        # The spot GATE passes (pull-wide signals clean) but one symbol was
        # quarantined — the threaded verdict must read spot_clean=False so the
        # strict Greeks seam can withhold (the F&O-local gate alone cannot see it).
        spot = _spot_with_failures(
            SymbolPullResult(symbol="NIFTY", completed=1, failed=1, no_data=0),
        )
        hist = _Spy(_composite(historical=_historical_inner()))
        run_full_pipeline(
            run_spot=_Spy(spot),
            run_metadata=_Spy(_metadata()),
            run_historical=hist,
            strict_gate=True,
        )
        assert hist.args == (PriorStepsVerdict(spot_clean=False, metadata_clean=True, strict=True),)


class TestStrictGateSequencer:
    """Strict + cleanset behavior of the post-step barrier in run_full_pipeline."""

    def test_strict_with_quarantine_skips_prebuild_and_movers(self) -> None:
        from structlog.testing import capture_logs

        spot = _spot_with_failures(
            SymbolPullResult(symbol="NIFTY", completed=1, failed=1, no_data=0),
        )
        prebuild = _PrebuildSpy(_outcome())
        warm = _Spy(_warm(3))
        with capture_logs() as logs:
            report = run_full_pipeline(
                run_spot=_Spy(spot),
                run_metadata=_Spy(_metadata()),
                run_historical=_Spy(_composite(historical=_historical_inner())),
                warm_spot_movers=warm,
                run_prebuild=prebuild,
                strict_gate=True,
            )
        # Post-steps WITHHELD: prebuild never called, movers never fire.
        assert prebuild.called is False
        assert warm.called is False
        assert report.post_steps_strict_skipped is True
        assert report.to_dict()["post_steps_strict_skipped"] is True
        assert report.prebuild_built is None
        assert report.movers_warmed is None
        # The note NAMES the strict skip + the blocking signal, loudly.
        assert ", post-steps SKIPPED (strict gate: 1 quarantined symbol(s))" in report.note
        skipped = [e for e in logs if e["event"] == "pipeline.post_steps.strict_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["blocking"] == ["1 quarantined symbol(s)"]
        assert skipped[0]["quarantined"] == ["NIFTY"]

    def test_strict_with_unclean_historical_skips_post_steps(self) -> None:
        prebuild = _PrebuildSpy(_outcome())
        report = run_full_pipeline(
            run_spot=_Spy(_spot()),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=_historical_inner(still_failed_windows=2))),
            run_prebuild=prebuild,
            strict_gate=True,
        )
        assert prebuild.called is False
        assert report.post_steps_strict_skipped is True
        assert "historical unclean" in report.note

    def test_strict_clean_run_identical_to_cleanset(self) -> None:
        # A fully-clean strict run behaves EXACTLY like the cleanset default.
        def _run(*, strict: bool) -> PipelinePullReport:
            return run_full_pipeline(
                run_spot=_Spy(_spot()),
                run_metadata=_Spy(_metadata()),
                run_historical=_Spy(_composite(historical=_historical_inner())),
                warm_spot_movers=lambda: _warm(2),
                run_prebuild=_PrebuildSpy(_outcome()),
                strict_gate=strict,
            )

        strict_report = _run(strict=True)
        cleanset_report = _run(strict=False)
        assert strict_report == cleanset_report
        assert strict_report.post_steps_strict_skipped is False
        assert "post-steps SKIPPED" not in strict_report.note

    def test_cleanset_default_byte_identical_pin(self) -> None:
        # DEFAULT-PRESERVATION PIN (DEC-PULL-SIX-STEP-ORDER-001): a QUARANTINED
        # cleanset run with strict_gate OMITTED equals strict_gate=False —
        # prebuild still fires with the exclusion set, names headlined, no
        # strict flag/clause anywhere (DEC-PREBUILD-CLEANSET-001 semantics).
        spot = _spot_with_failures(
            SymbolPullResult(symbol="NIFTY", completed=1, failed=1, no_data=0),
        )

        def _run(strict_gate: bool | None) -> tuple[PipelinePullReport, _PrebuildSpy]:
            prebuild = _PrebuildSpy(_outcome())
            if strict_gate is None:  # the OMITTED-kwarg default shape
                report = run_full_pipeline(
                    run_spot=_Spy(spot),
                    run_metadata=_Spy(_metadata()),
                    run_historical=_Spy(_composite(historical=_historical_inner())),
                    run_prebuild=prebuild,
                )
            else:
                report = run_full_pipeline(
                    run_spot=_Spy(spot),
                    run_metadata=_Spy(_metadata()),
                    run_historical=_Spy(_composite(historical=_historical_inner())),
                    run_prebuild=prebuild,
                    strict_gate=strict_gate,
                )
            return report, prebuild

        default_report, default_prebuild = _run(None)
        explicit_report, explicit_prebuild = _run(False)
        assert default_report == explicit_report
        assert default_prebuild.called is True
        assert explicit_prebuild.called is True
        assert default_prebuild.exclude_symbols == frozenset({"NIFTY"})
        assert default_report.post_steps_strict_skipped is False
        assert default_report.to_dict()["post_steps_strict_skipped"] is False
        assert "post-steps SKIPPED" not in default_report.note
        assert "1 symbol(s) quarantined: NIFTY" in default_report.note  # headline kept

    @pytest.mark.parametrize("strict", [True, False])
    @pytest.mark.parametrize(
        "step",
        [
            "indices-spot",
            "stocks-spot",
            "indices-futures",
            "stocks-futures",
            "indices-options",
            "stocks-options",
        ],
    )
    def test_failure_at_each_step_strict_vs_cleanset(self, strict: bool, step: str) -> None:
        """Edge 8 (complete): a per-symbol failure shaped at EACH of the six
        steps × {strict, cleanset}. Spot-side (steps 1-2) and F&O-side (steps
        3-6) quarantines are modelled with shaped fake reports; strict
        withholds the post-steps, cleanset runs the prebuild with the
        quarantined name excluded + headlined."""
        contract_by_step = {
            "indices-futures": "NSE-NIFTY-26Mar26-FUT",
            "stocks-futures": "NSE-ADANIENT-26Mar26-FUT",
            "indices-options": "NSE-NIFTY-26Mar26-21000-CE",
            "stocks-options": "NSE-ADANIENT-26Mar26-2400-PE",
        }
        if step in ("indices-spot", "stocks-spot"):
            symbol = "NIFTY" if step == "indices-spot" else "ADANIENT"
            spot = _spot_with_failures(
                SymbolPullResult(symbol=symbol, completed=1, failed=1, no_data=0),
            )
            hist_inner = _historical_inner()
            quarantined_name = symbol
        else:
            spot = _spot()
            contract = contract_by_step[step]
            hist_inner = _historical_inner(
                failed_pairs=1,
                phase1_complete=(),
                start_dates={contract: "2026-01-01"},
            )
            quarantined_name = "NIFTY" if "NIFTY" in contract_by_step[step] else "ADANIENT"
        prebuild = _PrebuildSpy(_outcome())
        report = run_full_pipeline(
            run_spot=_Spy(spot),
            run_metadata=_Spy(_metadata()),
            run_historical=_Spy(_composite(historical=hist_inner)),
            run_prebuild=prebuild,
            strict_gate=strict,
        )
        assert quarantined_name in report.quarantined_symbols
        if strict:
            assert prebuild.called is False
            assert report.post_steps_strict_skipped is True
            assert "post-steps SKIPPED (strict gate:" in report.note
        else:
            assert prebuild.called is True
            assert prebuild.exclude_symbols is not None
            assert quarantined_name in prebuild.exclude_symbols
            assert report.post_steps_strict_skipped is False
            assert "post-steps SKIPPED" not in report.note
        assert f"quarantined: {quarantined_name}" in report.note  # headlined either way
