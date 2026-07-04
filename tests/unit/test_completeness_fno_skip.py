"""The FNO-pruned spot verdict scan (DEC-COMPLETENESS-FNO-SKIP-001).

After the pipeline prebuild (DEC-PIPELINE-AUTOPREBUILD-001 #1145) a
fully-materialised lake holds ~10⁵-10⁶ F&O contract × interval datasets, and
the UNFILTERED ``summarize_spot_datasets`` walked + Parquet-footer-aggregated
every one on the first ``/completeness`` / ``/coverage/spot`` open. The SPOT
verdicts only read the spot segments at ``1minute`` + the derived ``1day``, so
the dashboard now scans through ``summarize_spot_verdict_datasets`` — the FNO
segment pruned at the WALK level, spot bounded to the verdict intervals.

Pins: (a) the pruned rows are BYTE-IDENTICAL to the unfiltered scan's spot
1minute/1day rows on a mixed lake (the verdicts can never change); (b) the
completeness scoreboard built from either row set is EQUAL (golden); (c) a
dataset-walk counter proves NO FNO dataset and NO derived-intraday spot dataset
is ever aggregated (the actual latency win); (d) the unfiltered default stays
byte-identical (the F&O ledger⇄lake reconcile depends on it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the lake summary scan needs duckdb")
import duckdb
from brutex.dashboard import db_view
from brutex.dashboard.completeness import build_completeness_scoreboard
from brutex.dashboard.db_view import (
    SPOT_VERDICT_INTERVALS,
    list_candle_datasets,
    summarize_spot_datasets,
    summarize_spot_verdict_datasets,
)
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_FNO_SYMBOL = "NSE-NIFTY-25Sep25-21000-CE"


def _bar(ts: datetime) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=10,
        open_interest=None,
    )


def _write(lake: Path, *, segment: str, symbol: str, interval: str) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment=segment,
        symbol=symbol,
        interval=interval,
        bars=[
            _bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC)),
            _bar(datetime(2024, 3, 2, 9, 15, tzinfo=UTC)),
        ],
    )


@pytest.fixture
def mixed_lake(tmp_path: Path) -> Path:
    """A lake holding spot 1m + 1d, a DERIVED spot intraday TF, and F&O datasets."""
    lake = tmp_path / "lake"
    _write(lake, segment="CASH", symbol="NSE-NIFTY", interval="1minute")
    _write(lake, segment="CASH", symbol="NSE-NIFTY", interval="1day")
    # A prebuild-derived spot intraday TF — display-only, never a verdict input.
    _write(lake, segment="CASH", symbol="NSE-NIFTY", interval="5minute")
    # Two F&O contract datasets (the ~10⁵-10⁶-dataset class the prune skips).
    _write(lake, segment=FNO_CANDLE_LAKE_SEGMENT, symbol=_FNO_SYMBOL, interval="1minute")
    _write(lake, segment=FNO_CANDLE_LAKE_SEGMENT, symbol=_FNO_SYMBOL, interval="5minute")
    return lake


def test_verdict_scan_returns_only_spot_verdict_intervals(mixed_lake: Path) -> None:
    """The pruned scan lists ONLY the spot 1minute/1day rows — no FNO, no 5minute."""
    conn = duckdb.connect()
    try:
        rows = summarize_spot_verdict_datasets(conn, mixed_lake)
    finally:
        conn.close()
    assert [(r["segment"], r["interval"]) for r in rows] == [
        ("CASH", "1day"),
        ("CASH", "1minute"),
    ]


def test_verdict_rows_byte_identical_to_unfiltered_spot_rows(mixed_lake: Path) -> None:
    """GOLDEN: the pruned rows == the unfiltered scan's spot 1m/1d rows, cell-for-cell.

    The prune may only SKIP datasets the verdicts never read — every surviving
    row (bars / first_date / last_date included) must be byte-identical, so no
    verdict can move (Golden Rule #1).
    """
    conn = duckdb.connect()
    try:
        pruned = summarize_spot_verdict_datasets(conn, mixed_lake)
        unfiltered = summarize_spot_datasets(conn, mixed_lake)
    finally:
        conn.close()
    expected = [
        r
        for r in unfiltered
        if r["segment"] != FNO_CANDLE_LAKE_SEGMENT and r["interval"] in SPOT_VERDICT_INTERVALS
    ]
    assert pruned == expected
    # The unfiltered scan genuinely saw MORE (the mixed fixture has teeth).
    assert len(unfiltered) == 5
    assert len(pruned) == 2


def test_completeness_scoreboard_identical_before_and_after_prune(mixed_lake: Path) -> None:
    """GOLDEN: the scoreboard built from pruned vs unfiltered rows is EQUAL.

    The verdicts read only the spot ``1minute`` dimension (+ the segments in
    ``SPOT_PROBE_SEGMENTS``), so pruning FNO + derived TFs from the SCAN must
    leave the whole payload byte-identical on a mixed lake.
    """
    categories = {"NSE-NIFTY": "index"}
    conn = duckdb.connect()
    try:
        pruned = summarize_spot_verdict_datasets(conn, mixed_lake)
        unfiltered = summarize_spot_datasets(conn, mixed_lake)
    finally:
        conn.close()
    board_pruned = build_completeness_scoreboard(pruned, categories, [], categories)
    board_unfiltered = build_completeness_scoreboard(unfiltered, categories, [], categories)
    assert board_pruned == board_unfiltered


def test_walk_counter_proves_fno_datasets_never_aggregated(
    mixed_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bench-style teeth: NO FNO / derived-TF dataset ever reaches the footer read.

    The per-dataset ``_aggregate_parquet_resilient`` footer aggregate is the
    O(datasets) cost the register measured at 20-50 min on a materialised lake
    — count its invocations: the pruned scan pays exactly the 2 verdict
    datasets; the unfiltered scan pays all 5 (the counter has teeth).
    """
    sources: list[str] = []
    real = db_view._aggregate_parquet_resilient  # pyright: ignore[reportPrivateUsage] — whitebox counter

    def _counting(conn: Any, directory: Path, sql: str, *, source: str) -> list[tuple[Any, ...]]:
        sources.append(source)
        return real(conn, directory, sql, source=source)

    monkeypatch.setattr(db_view, "_aggregate_parquet_resilient", _counting)
    conn = duckdb.connect()
    try:
        summarize_spot_verdict_datasets(conn, mixed_lake)
        pruned_sources, sources[:] = list(sources), []
        summarize_spot_datasets(conn, mixed_lake)
        unfiltered_sources = list(sources)
    finally:
        conn.close()
    assert len(pruned_sources) == 2
    assert all("FNO" not in s and "5minute" not in s for s in pruned_sources)
    assert len(unfiltered_sources) == 5, "the counter must have teeth on the unfiltered scan"


def test_list_candle_datasets_prunes_excluded_segment_at_walk_level(mixed_lake: Path) -> None:
    """The exclude prunes the SEGMENT subtree — no FNO dataset is even listed."""
    listed = list_candle_datasets(mixed_lake, exclude_segments=frozenset({FNO_CANDLE_LAKE_SEGMENT}))
    assert all(d["segment"] != FNO_CANDLE_LAKE_SEGMENT for d in listed)
    assert len(listed) == 3  # the three CASH datasets (1m + 1d + 5m)


def test_list_candle_datasets_default_is_unfiltered(mixed_lake: Path) -> None:
    """Back-compat pin: the DEFAULT walk still lists everything (the F&O
    ledger⇄lake cold-start reconcile via ``load_lake_spans`` depends on it)."""
    listed = list_candle_datasets(mixed_lake)
    segments = {d["segment"] for d in listed}
    assert FNO_CANDLE_LAKE_SEGMENT in segments
    assert len(listed) == 5


def test_list_candle_datasets_interval_bound(mixed_lake: Path) -> None:
    """The interval bound keeps only the requested interval directories."""
    listed = list_candle_datasets(mixed_lake, intervals=SPOT_VERDICT_INTERVALS)
    assert {d["interval"] for d in listed} == {"1minute", "1day"}
