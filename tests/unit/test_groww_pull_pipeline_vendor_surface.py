"""Spot post-drain reconcile of the vendor surface (DEC-VENDOR-CONFLICT-SURFACE-001).

In async-pipeline mode the producer's per-pair :class:`IngestReport` reads
``0``/``None`` for the vendor-behaviour counters (the consumer did the durable
write); :func:`reconcile_pairs_with_pipeline` must fold the consumer's real
per-series ``vendor_dup_rows_dropped`` / ``max_close_delta`` back in, exactly
like ``bars_appended`` / ``conflicts`` — otherwise every ASYNC pull (the
production default) would surface 0/None while only the sync path told the
truth.

Fail-before proof: on origin/main ``SeriesTotals`` lacks both fields and the
reconcile ``replace`` never touches them — the assertions below fail there
(``AttributeError`` on ``SeriesTotals(vendor_dup_rows_dropped=…)``).
"""

from __future__ import annotations

from decimal import Decimal

from brutex.adapters.groww.fetch_pipeline import FetchPipeline
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.pull_pipeline import reconcile_pairs_with_pipeline


def test_reconcile_overrides_vendor_surface_from_consumer_totals() -> None:
    pipeline = FetchPipeline(workers=1)
    try:
        pipeline.fold_totals(
            bars_appended=30,
            conflicts=2,
            series_key=("NIFTY", "1minute"),
            vendor_dup_rows_dropped=5,
            max_close_delta=Decimal("44.70"),
        )
        deferred = {
            ("NIFTY", "1minute"): IngestReport(windows=2, bars_appended=0, transient_failures=0),
            # No consumer fold for this series (all windows producer-skipped):
            # the producer report must be kept verbatim.
            ("SENSEX", "1minute"): IngestReport(windows=1, bars_appended=0, transient_failures=0),
        }
        out = reconcile_pairs_with_pipeline(deferred, pipeline)  # type: ignore[arg-type]
    finally:
        pipeline.drain()
    folded = out[("NIFTY", "1minute")]
    assert folded.bars_appended == 30
    assert folded.conflicts == 2
    assert folded.vendor_dup_rows_dropped == 5
    assert folded.max_close_delta == Decimal("44.70")
    untouched = out[("SENSEX", "1minute")]
    assert untouched.vendor_dup_rows_dropped == 0
    assert untouched.max_close_delta is None
