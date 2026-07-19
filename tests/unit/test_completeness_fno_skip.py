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


# ────────────────────────────────────────────────────────────────────────
# R2 read-scope screen on the verdict scan (DEC-UNIVERSE-INDEX-ONLY-001, #1934)
# ────────────────────────────────────────────────────────────────────────

from brutex.config.feed_scope import SCOPE_FULL, FeedScope, resolve_feed_scope  # noqa: E402
from brutex.dashboard.db_view import (  # noqa: E402
    ScopedSummaryRows,
    summary_rows_scope_excluded,
)

_FULL_SCOPE = FeedScope(
    feed="test", token=SCOPE_FULL, admitted_underlyings=None, fno_underlyings=None
)


@pytest.fixture
def stock_lake(mixed_lake: Path) -> Path:
    """The mixed lake + a stored-only STOCK spot dataset (the R2 target class)."""
    _write(mixed_lake, segment="CASH", symbol="RELIANCE", interval="1minute")
    _write(mixed_lake, segment="CASH", symbol="RELIANCE", interval="1day")
    return mixed_lake


class TestVerdictScanReadScope:
    """R2: the verdict wrapper's bounded-scope screen (design §3 R2)."""

    def test_bounded_scope_excludes_stock_datasets_and_counts(self, stock_lake: Path) -> None:
        """[FB] a stored stock spot dataset is excluded + counted under index4.

        At merge-base the wrapper had no ``scope`` seam and the stock rows
        were served (the route-level fails-before twin drives the REAL
        ``/coverage/spot``); at head the bounded scope excludes them at
        DISCOVERY and the census rides the returned rows.
        """
        conn = duckdb.connect()
        try:
            rows = summarize_spot_verdict_datasets(
                conn, stock_lake, scope=resolve_feed_scope("groww")
            )
        finally:
            conn.close()
        symbols = {r["symbol"] for r in rows}
        assert "RELIANCE" not in symbols
        # The DRIFTED index dir (own-exchange prefix) is recovered THEN
        # admitted — the recovery-ordering pin (§6#6).
        assert symbols == {"NSE-NIFTY"}
        assert isinstance(rows, ScopedSummaryRows)
        # DISTINCT symbols, not datasets: RELIANCE holds 1minute + 1day (two
        # datasets) yet counts ONCE.
        assert rows.scope_excluded == 1
        assert summary_rows_scope_excluded(rows) == 1

    def test_bounded_scope_skips_excluded_aggregates(
        self, stock_lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The R2 economy: an excluded dataset never reaches the footer read."""
        sources: list[str] = []
        real = db_view._aggregate_parquet_resilient  # pyright: ignore[reportPrivateUsage] — whitebox counter

        def _counting(
            conn: Any, directory: Path, sql: str, *, source: str
        ) -> list[tuple[Any, ...]]:
            sources.append(source)
            return real(conn, directory, sql, source=source)

        monkeypatch.setattr(db_view, "_aggregate_parquet_resilient", _counting)
        conn = duckdb.connect()
        try:
            summarize_spot_verdict_datasets(conn, stock_lake, scope=resolve_feed_scope("groww"))
        finally:
            conn.close()
        assert len(sources) == 2, sources  # the two NSE-NIFTY verdict datasets only
        assert all("RELIANCE" not in s for s in sources)

    def test_default_none_is_unfiltered_byte_identical(self, stock_lake: Path) -> None:
        """Law 0: ``scope=None`` (every legacy caller) serves the stock rows."""
        conn = duckdb.connect()
        try:
            default_rows = summarize_spot_verdict_datasets(conn, stock_lake)
            explicit_none = summarize_spot_verdict_datasets(conn, stock_lake, scope=None)
        finally:
            conn.close()
        assert default_rows == explicit_none
        assert {r["symbol"] for r in default_rows} == {"NSE-NIFTY", "RELIANCE"}
        assert not isinstance(default_rows, ScopedSummaryRows)
        assert summary_rows_scope_excluded(default_rows) == 0

    def test_unbounded_scope_is_pass_through(self, stock_lake: Path) -> None:
        """The widen arm: a ``full`` scope equals the unfiltered default."""
        conn = duckdb.connect()
        try:
            default_rows = summarize_spot_verdict_datasets(conn, stock_lake)
            full_rows = summarize_spot_verdict_datasets(conn, stock_lake, scope=_FULL_SCOPE)
        finally:
            conn.close()
        assert full_rows == default_rows
        assert not isinstance(full_rows, ScopedSummaryRows)

    def test_index_only_lake_bounded_scope_zero_census(self, mixed_lake: Path) -> None:
        """An index-only lake under the bounded scope: same rows, zero census."""
        conn = duckdb.connect()
        try:
            unfiltered = summarize_spot_verdict_datasets(conn, mixed_lake)
            bounded = summarize_spot_verdict_datasets(
                conn, mixed_lake, scope=resolve_feed_scope("groww")
            )
        finally:
            conn.close()
        assert list(bounded) == list(unfiltered)
        assert isinstance(bounded, ScopedSummaryRows)
        assert bounded.scope_excluded == 0

    def test_walk_admit_seam_default_none_never_consults_scope(self, stock_lake: Path) -> None:
        """`summarize_spot_datasets(admit=None)` is parameter-driven — the
        conftest-forced index4 env can never filter a defaulted walk (the
        load_lake_spans cold-start contract's structural half)."""
        conn = duckdb.connect()
        try:
            rows = summarize_spot_datasets(conn, stock_lake)
        finally:
            conn.close()
        assert "RELIANCE" in {r["symbol"] for r in rows}


class TestVerdictScanSessionDateSegments:
    """The r4 BLOCKER fold: the wrapper forwards ``session_date_segments``.

    The /coverage/missing-sessions drill (a merge-window census surface that
    landed calling the RAW scan unscoped) now rides the ONE R2 wrapper — its
    ``session_dates`` enrichment must thread through BOTH wrapper arms
    verbatim, or the drill's audit silently loses every gap row (every row
    would carry no ``session_dates`` and the audit would abstain).
    """

    def test_bounded_arm_forwards_session_dates(self, stock_lake: Path) -> None:
        """The bounded arm: admitted 1minute rows carry the enrichment AND the
        stock dataset is still excluded + counted (the drill's exact shape)."""
        conn = duckdb.connect()
        try:
            rows = summarize_spot_verdict_datasets(
                conn,
                stock_lake,
                scope=resolve_feed_scope("groww"),
                session_date_segments=frozenset({"CASH"}),
            )
        finally:
            conn.close()
        assert {r["symbol"] for r in rows} == {"NSE-NIFTY"}
        assert summary_rows_scope_excluded(rows) == 1
        minute = next(r for r in rows if r["interval"] == "1minute")
        assert isinstance(minute["session_dates"], frozenset)
        assert len(minute["session_dates"]) == 2

    def test_unbounded_arm_forwards_session_dates(self, stock_lake: Path) -> None:
        """The pass-through arm equals the raw scan with the same opt-in."""
        conn = duckdb.connect()
        try:
            wrapped = summarize_spot_verdict_datasets(
                conn,
                stock_lake,
                scope=_FULL_SCOPE,
                session_date_segments=frozenset({"CASH"}),
            )
            raw = summarize_spot_datasets(
                conn,
                stock_lake,
                exclude_segments=frozenset({FNO_CANDLE_LAKE_SEGMENT}),
                intervals=SPOT_VERDICT_INTERVALS,
                session_date_segments=frozenset({"CASH"}),
            )
        finally:
            conn.close()
        assert wrapped == raw
        assert any("session_dates" in r for r in wrapped)
