"""FetchPipeline vendor-surface folds (DEC-VENDOR-CONFLICT-SURFACE-001).

The async consumer folds each window's ``vendor_dup_rows_dropped`` (sum) +
``max_close_delta`` (exact Decimal max, ``None`` identity) into BOTH the
pull-level totals AND the per-``(symbol, interval)`` :class:`SeriesTotals` the
post-drain reconcile reads — exactly like ``bars_appended``/``conflicts``.
Cross-thread correctness rides the SAME ``_totals_lock`` those folds already
hold; the fold algebra itself is property-pinned in
``tests/property/test_conflict_delta_fold.py``.

Fail-before proof: on origin/main ``fold_totals`` has no
``vendor_dup_rows_dropped``/``max_close_delta`` kwargs (``TypeError``) and
``SeriesTotals`` has neither field (``AttributeError``) — this suite cannot
pass there.
"""

from __future__ import annotations

from decimal import Decimal

from brutex.adapters.groww.fetch_pipeline import FetchPipeline, SeriesTotals


class TestPullLevelVendorSurface:
    def test_defaults_keep_legacy_callers_byte_identical(self) -> None:
        """A legacy fold (no new kwargs) leaves the vendor surface at 0/None."""
        pipeline = FetchPipeline(workers=1)
        try:
            pipeline.fold_totals(bars_appended=5, conflicts=1)
        finally:
            pipeline.drain()
        dup, delta = pipeline.vendor_surface_totals
        assert dup == 0
        assert delta is None

    def test_pull_totals_sum_and_max_fold(self) -> None:
        pipeline = FetchPipeline(workers=1)
        try:
            pipeline.fold_totals(
                bars_appended=5,
                conflicts=1,
                vendor_dup_rows_dropped=3,
                max_close_delta=Decimal("0.05"),
            )
            pipeline.fold_totals(
                bars_appended=2,
                conflicts=2,
                vendor_dup_rows_dropped=4,
                max_close_delta=Decimal("44.70"),
            )
            pipeline.fold_totals(bars_appended=1, conflicts=0)  # None delta = identity
        finally:
            pipeline.drain()
        dup, delta = pipeline.vendor_surface_totals
        assert dup == 7
        assert delta == Decimal("44.70")


class TestPerSeriesVendorSurface:
    def test_series_totals_accumulate_and_max_fold(self) -> None:
        """Per-series: dups SUM, delta MAX-folds, other series untouched."""
        key = ("NSE-NIFTY-26Jun25-26000-CE", "1minute")
        other = ("NSE-NIFTY-26Jun25-26000-PE", "1minute")
        pipeline = FetchPipeline(workers=1)
        try:
            pipeline.fold_totals(
                bars_appended=10,
                conflicts=1,
                series_key=key,
                vendor_dup_rows_dropped=2,
                max_close_delta=Decimal("1.25"),
            )
            pipeline.fold_totals(
                bars_appended=4,
                conflicts=0,
                series_key=key,
                vendor_dup_rows_dropped=1,
                max_close_delta=Decimal("0.10"),
            )
            pipeline.fold_totals(bars_appended=7, conflicts=0, series_key=other)
        finally:
            pipeline.drain()
        totals = pipeline.per_series_totals()
        assert totals[key].vendor_dup_rows_dropped == 3
        assert totals[key].max_close_delta == Decimal("1.25")
        assert totals[other].vendor_dup_rows_dropped == 0
        assert totals[other].max_close_delta is None

    def test_tail_transient_preserves_series_vendor_surface(self) -> None:
        """A later tail-transient fold never resets the series' folded counters."""
        key = ("NSE-NIFTY", "1minute")
        pipeline = FetchPipeline(workers=1)
        try:
            pipeline.fold_totals(
                bars_appended=10,
                conflicts=1,
                series_key=key,
                vendor_dup_rows_dropped=2,
                max_close_delta=Decimal("0.55"),
            )
            pipeline.fold_tail_transient(key)
        finally:
            pipeline.drain()
        totals = pipeline.per_series_totals()
        assert totals[key].vendor_dup_rows_dropped == 2
        assert totals[key].max_close_delta == Decimal("0.55")
        assert totals[key].transient == 1

    def test_series_totals_defaults(self) -> None:
        totals = SeriesTotals()
        assert totals.vendor_dup_rows_dropped == 0
        assert totals.max_close_delta is None
