"""DEC-PULL-PROGRESS-HONESTY-001 — the FetchPipeline ``on_bars`` live-progress seam.

Fails-before framing: in async mode the per-pair ``IngestReport`` is
producer-resolved with ``bars_appended == 0`` while the REAL counts are folded by
the background consumer via :meth:`FetchPipeline.fold_totals` — so a dashboard bars
counter had NO per-window truth to read and froze for the whole F&O phase. The
``on_bars`` hook is that truth: invoked once per fold with the window's delta,
guarded so a hook error can never trip the pipeline, and never fired by a
tail-transient fold (no bars landed).
"""

from __future__ import annotations

import pytest
import structlog.testing
from brutex.adapters.groww.fetch_pipeline import FetchPipeline


@pytest.fixture
def pipeline_recorder() -> tuple[FetchPipeline, list[int]]:
    seen: list[int] = []
    pipeline = FetchPipeline(workers=1, queue_maxsize=4, on_bars=seen.append)
    return pipeline, seen


def test_on_bars_receives_each_folded_delta_exactly_once(
    pipeline_recorder: tuple[FetchPipeline, list[int]],
) -> None:
    pipeline, seen = pipeline_recorder
    try:
        pipeline.fold_totals(bars_appended=375, conflicts=0)
        pipeline.fold_totals(bars_appended=0, conflicts=0)
        pipeline.fold_totals(
            bars_appended=42, conflicts=1, series_key=("NIFTY24JAN22000CE", "1minute")
        )
    finally:
        pipeline.drain()
    # Exactly the folded deltas, in order, once each — the no-double-count invariant
    # at the seam (the producer-side per-pair report adds 0 in async mode).
    assert seen == [375, 0, 42]
    assert pipeline.totals == (3, 417, 1)


def test_on_bars_default_none_is_byte_identical(  # fails-before: seam absent entirely
) -> None:
    pipeline = FetchPipeline(workers=1, queue_maxsize=4)
    try:
        pipeline.fold_totals(bars_appended=10, conflicts=0)
    finally:
        pipeline.drain()
    assert pipeline.totals == (1, 10, 0)


def test_on_bars_error_is_logged_and_never_trips_the_fold() -> None:
    def _boom(_delta: int) -> None:
        raise RuntimeError("hook blew up")

    pipeline = FetchPipeline(workers=1, queue_maxsize=4, on_bars=_boom)
    try:
        with structlog.testing.capture_logs() as logs:
            pipeline.fold_totals(bars_appended=7, conflicts=0)
    finally:
        pipeline.drain()
    # The fold committed (totals advanced) and the error surfaced as ONE log event.
    assert pipeline.totals == (1, 7, 0)
    assert any(log["event"] == "fetch_pipeline.on_bars_error" for log in logs)


def test_tail_transient_fold_never_fires_on_bars(
    pipeline_recorder: tuple[FetchPipeline, list[int]],
) -> None:
    pipeline, seen = pipeline_recorder
    try:
        pipeline.fold_tail_transient(series_key=("SYM", "1minute"))
    finally:
        pipeline.drain()
    assert seen == []  # a deferred window landed no bars — nothing to report
    assert pipeline.tail_transients == 1
