"""Pipeline headline + alert surfacing of vendor conflicts / duplicate drops.

DEC-VENDOR-CONFLICT-SURFACE-001 (headline + ONE per-pull alert) and
DEC-LAKE-FOOTPRINT-MEASURED-001 (the disk-guard SKIP clause + the consequent
movers-warm skip). Fakes are the REAL report dataclasses (Golden Rule #1).

Fail-before proof: on origin/main ``BulkSpotReport``/``HistoricalPullReport``
carry none of the three counters (``TypeError`` on construction),
``vendor_surface_totals``/``emit_vendor_conflict_alert`` do not exist
(``ImportError``), and the completion note never contains
``"vendor conflicts:"`` — this suite cannot pass there.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pipeline_pull import (
    run_full_pipeline,
    vendor_surface_totals,
)
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult


def _spot(
    *,
    conflicts: int = 0,
    vendor_dup_rows_dropped: int = 0,
    max_close_delta: Decimal | None = None,
) -> BulkSpotReport:
    return BulkSpotReport(
        dry_run=False,
        start_date="2020-01-01",
        end_date="2026-07-03",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0)],
        conflicts=conflicts,
        vendor_dup_rows_dropped=vendor_dup_rows_dropped,
        max_close_delta=max_close_delta,
    )


def _metadata() -> MetadataPullReport:
    return MetadataPullReport(
        dry_run=False,
        years=(2025,),
        underlyings=("NIFTY",),
        expiry_cells=1,
        cells_skipped=0,
        expiry_failures=0,
        expiries_persisted=2,
        contract_jobs=1,
        contract_failures=0,
        contracts_persisted=10,
        note="ok",
    )


def _hist(
    *,
    conflicts: int = 0,
    vendor_dup_rows_dropped: int = 0,
    max_close_delta: Decimal | None = None,
) -> HistoricalPullReport:
    return HistoricalPullReport(
        dry_run=False,
        intervals=("1minute",),
        contracts_selected=2,
        contracts_completed=2,
        bars_appended=100,
        windows=2,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
        note="ok",
        phase1_clean=True,
        phase1_complete=("NSE-NIFTY-26Jun25-26000-CE",),
        start_dates={"NSE-NIFTY-26Jun25-26000-CE": "2025-05-27"},
        minute_requested=True,
        minute_skipped=False,
        conflicts=conflicts,
        vendor_dup_rows_dropped=vendor_dup_rows_dropped,
        max_close_delta=max_close_delta,
    )


def _composite(historical: HistoricalPullReport | None) -> CompositePullReport:
    return CompositePullReport(
        dry_run=False,
        requested=("NIFTY",),
        tradable=("NIFTY",),
        skipped_reference_only=(),
        metadata=_metadata(),
        historical=historical,
        note="ok",
    )


class TestVendorSurfaceTotals:
    def test_folds_both_phases(self) -> None:
        totals = vendor_surface_totals(
            _spot(conflicts=2, vendor_dup_rows_dropped=3, max_close_delta=Decimal("0.05")),
            _composite(
                _hist(conflicts=23, vendor_dup_rows_dropped=9138, max_close_delta=Decimal("44.70"))
            ),
        )
        assert totals == (25, 9141, Decimal("44.70"))

    def test_absent_phases_contribute_nothing(self) -> None:
        assert vendor_surface_totals(None, None) == (0, 0, None)
        assert vendor_surface_totals(_spot(), _composite(None)) == (0, 0, None)


class TestHeadlineClause:
    def _note(
        self,
        spot: BulkSpotReport,
        hist: HistoricalPullReport,
    ) -> str:
        report = run_full_pipeline(
            run_spot=lambda: spot,
            run_metadata=_metadata,
            run_historical=lambda: _composite(hist),
        )
        return report.note

    def test_clean_pull_note_has_no_conflict_clause(self) -> None:
        """Byte-honesty: an all-zero pull renders NO vendor clause at all."""
        note = self._note(_spot(), _hist())
        assert "vendor conflicts" not in note
        assert "duplicate vendor rows" not in note

    def test_conflicts_and_dups_named_with_rupee_delta(self) -> None:
        """The operator's real shape: 25 conflicts max ₹44.70 + 9,141 dup rows."""
        note = self._note(
            _spot(conflicts=2, vendor_dup_rows_dropped=3, max_close_delta=Decimal("0.05")),
            _hist(conflicts=23, vendor_dup_rows_dropped=9138, max_close_delta=Decimal("44.70")),
        )
        assert "vendor conflicts: 25 (max close delta ₹44.70)" in note
        assert "duplicate vendor rows dropped: 9141" in note

    def test_dups_only_renders_without_conflict_part(self) -> None:
        note = self._note(_spot(vendor_dup_rows_dropped=7), _hist())
        assert "duplicate vendor rows dropped: 7" in note
        assert "vendor conflicts" not in note

    def test_conflicts_without_delta_render_countonly(self) -> None:
        """A conflict whose closes matched (another field drifted) has no delta part."""
        note = self._note(_spot(conflicts=4, max_close_delta=None), _hist())
        assert "vendor conflicts: 4" in note
        assert "max close delta" not in note


class TestConflictAlertSeam:
    def test_alert_called_exactly_once_with_folded_totals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The completion seam fires ONE alert per pull with the folded totals."""
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            "brutex.dashboard.pipeline_pull.emit_vendor_conflict_alert",
            lambda **kw: calls.append(kw) or True,
        )
        run_full_pipeline(
            run_spot=lambda: _spot(conflicts=1, max_close_delta=Decimal("2.00")),
            run_metadata=_metadata,
            run_historical=lambda: _composite(_hist(vendor_dup_rows_dropped=5)),
        )
        assert calls == [{"conflicts": 1, "max_close_delta": Decimal("2.00"), "dup_rows": 5}]

    def test_gate_stopped_run_does_not_hit_the_completion_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spot-gate stop returns before the completion-headline seam (no alert)."""
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            "brutex.dashboard.pipeline_pull.emit_vendor_conflict_alert",
            lambda **kw: calls.append(kw) or True,
        )
        failing = _spot(conflicts=9, max_close_delta=Decimal("99.00"))
        failing.still_failed_windows = 5  # fails spot_phase_ok
        run_full_pipeline(
            run_spot=lambda: failing,
            run_metadata=_metadata,
            run_historical=lambda: _composite(_hist()),
        )
        assert calls == []


class TestDiskGuardHeadline:
    """DEC-LAKE-FOOTPRINT-MEASURED-001: the SKIPPED prebuild is named + warms nothing."""

    def _guarded_outcome(self, _excluded: frozenset[str]) -> PrebuildOutcome:
        return PrebuildOutcome(
            symbols_built=0,
            total_bars=0,
            failed=(),
            failed_count=0,
            ok=False,
            disk_guard_blocked=True,
            disk_guard_detail=(
                "disk guard: est. need ~12.0 GiB (x6.5 fallback multiplier, +10% margin), "
                "free 3.0 GiB — prebuild skipped; free space or set "
                "BRUTEX_PREBUILD_DISK_GUARD=off to override"
            ),
        )

    def test_note_names_the_skip_and_movers_do_not_warm(self) -> None:
        warmed: list[str] = []

        def _warm_spot() -> MoversWarmResult:
            warmed.append("spot")
            return MoversWarmResult(warmed=1, failed=0, total=1)

        report = run_full_pipeline(
            run_spot=_spot,
            run_metadata=_metadata,
            run_historical=lambda: _composite(_hist()),
            warm_spot_movers=_warm_spot,
            run_prebuild=self._guarded_outcome,
        )
        assert "prebuild SKIPPED — disk guard: est. need ~12.0 GiB" in report.note
        assert "BRUTEX_PREBUILD_DISK_GUARD=off" in report.note
        assert warmed == []  # the warm gate (PrebuildOutcome.ok) held
        assert report.prebuild_disk_guarded is True
        assert report.to_dict()["prebuild_disk_guarded"] is True

    def test_unguarded_prebuild_reports_false(self) -> None:
        report = run_full_pipeline(
            run_spot=_spot,
            run_metadata=_metadata,
            run_historical=lambda: _composite(_hist()),
            run_prebuild=lambda _excluded: PrebuildOutcome(
                symbols_built=1, total_bars=10, failed=(), failed_count=0, ok=True
            ),
        )
        assert report.prebuild_disk_guarded is False
        assert "prebuild SKIPPED" not in report.note
