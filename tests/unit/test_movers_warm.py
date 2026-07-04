"""Unit tests for the post-pull movers warm (DEC-PULL-MOVERS-WARM-001).

`latest_movers_date` over a real seeded Parquet+DuckDB lake (latest day / empty → None),
plus the pure `warm_movers_snapshots` best-effort job runner (counts non-empty, a 0-count
job is not counted, a raising job is logged + counted 0 — never re-raised).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.dashboard.movers_warm import (
    WARM_EXCHANGES,
    WARM_OPTIONS_TIERS,
    WARM_OPTIONS_TRACKS,
    MoversWarmResult,
    futures_warm_grid,
    latest_movers_date,
    options_warm_grid,
    spot_warm_grid,
    warm_coordinate_label,
    warm_movers_snapshots,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter


def _day_bar(day: date, close: str) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),  # 09:15 IST
        open=c,
        high=c,
        low=c,
        close=c,
        volume=100,
        open_interest=None,
    )


def test_latest_movers_date_returns_newest_ist_date(tmp_path: Path) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 12), "20000"), _day_bar(date(2024, 6, 13), "20100")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-13"


def test_latest_movers_date_spans_index_and_cash(tmp_path: Path) -> None:
    # The newest day may live in the CASH segment (a stock), not INDEX — both are scanned.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 10), "20000")],
    )
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 14), "1300")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-14"


def test_latest_movers_date_none_on_empty_lake(tmp_path: Path) -> None:
    assert latest_movers_date(tmp_path) is None


def test_latest_movers_date_none_when_only_non_spot_segments(tmp_path: Path) -> None:
    # An FNO-only lake (no INDEX/CASH 1day) → no spot movers date.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="FNO",
        symbol="NIFTY24JUN20000CE",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 13), "100")],
    )
    assert latest_movers_date(tmp_path) is None


def test_latest_movers_date_none_on_corrupt_lake(tmp_path: Path) -> None:
    """GAP #4: a corrupt / unreadable spot 1day Parquet → None (degrade), never a raise.

    `latest_movers_date` backs the DEFAULT `/movers` poll, so an unguarded DuckDB error on
    a corrupt file would 500 the whole page. Treat an unreadable lake the same as an empty
    one — return None so the route degrades to an honest empty/today payload.
    """
    # Lay down a file at the spot 1day glob path that is GARBAGE bytes (not Parquet), so
    # DuckDB's read_parquet raises an InvalidInputException ("No magic bytes …").
    corrupt = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1day" / "2024" / "06.parquet"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"this is not a parquet file at all")
    assert latest_movers_date(tmp_path) is None


def test_warm_counts_only_non_empty_jobs() -> None:
    # 3 jobs: spot warmed 5, futures warmed 0 (empty), options warmed 2 → 2 non-empty.
    result = warm_movers_snapshots([lambda: 5, lambda: 0, lambda: 2])
    assert result == MoversWarmResult(warmed=2, failed=0, total=3)


def test_warm_empty_job_list_is_zero() -> None:
    assert warm_movers_snapshots([]) == MoversWarmResult(warmed=0, failed=0, total=0)


def test_warm_swallows_a_raising_job_counts_it_failed() -> None:
    # A transient lake-read race in one warmer is logged + counted FAILED (never
    # re-raised — the already-successful pull is never failed by a warm hiccup;
    # the failed count reaches the completion note, DEC-MOVERS-WARM-PROGRESS-001).
    def _boom() -> int:
        raise RuntimeError("transient lake read race")

    result = warm_movers_snapshots([lambda: 3, _boom, lambda: 1])
    assert result == MoversWarmResult(warmed=2, failed=1, total=3)


# ── DEC-MOVERS-WARM-PROGRESS-001: per-coordinate progress + observer seam ──


def test_warm_emits_progress_log_per_coordinate() -> None:
    # One `movers.warm.progress` line per completed coordinate — key i/N +
    # segment + per-job/cumulative elapsed — a BOUNDED cadence (grid ≤ 39).
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        warm_movers_snapshots([lambda: 2, lambda: 0], segment="spot", labels=["all", "NSE"])
    progress = [e for e in logs if e["event"] == "movers.warm.progress"]
    assert [(e["key"], e["index"], e["total"]) for e in progress] == [
        ("all", 1, 2),
        ("NSE", 2, 2),
    ]
    assert all(e["segment"] == "spot" for e in progress)
    assert all(e["job_elapsed_s"] >= 0 and e["elapsed_s"] >= 0 for e in progress)
    done = [e for e in logs if e["event"] == "movers.warm.segment_done"]
    assert len(done) == 1
    assert done[0]["warmed"] == 1 and done[0]["failed"] == 0 and done[0]["total"] == 2


def test_warm_failed_job_logs_key_and_counts() -> None:
    from structlog.testing import capture_logs

    def _boom() -> int:
        raise RuntimeError("boom")

    with capture_logs() as logs:
        result = warm_movers_snapshots([_boom], segment="options", labels=["BSE|far|weekly"])
    assert result == MoversWarmResult(warmed=0, failed=1, total=1)
    failed = [e for e in logs if e["event"] == "movers.warm.job_failed"]
    assert len(failed) == 1
    assert failed[0]["key"] == "BSE|far|weekly"
    assert failed[0]["segment"] == "options"
    assert failed[0]["index"] == 1 and failed[0]["total"] == 1


def test_warm_on_progress_observer_sees_i_of_n() -> None:
    # The gate seam (DEC-PREBUILD-GATE-HONEST-NOTE-001): (0,N) before the first
    # job, advancing per coordinate, (N,N) after the last.
    seen: list[tuple[int, int]] = []
    warm_movers_snapshots(
        [lambda: 1, lambda: 0, lambda: 2],
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_warm_on_progress_observer_error_never_breaks_the_warm() -> None:
    # A raising observer is a caller bug worth a log line — never an abort.
    def _bad(done: int, total: int) -> None:
        raise RuntimeError("observer bug")

    result = warm_movers_snapshots([lambda: 4], on_progress=_bad)
    assert result == MoversWarmResult(warmed=1, failed=0, total=1)


def test_warm_short_labels_fall_back_to_index() -> None:
    # A labels list shorter than jobs never crashes — the tail falls back to
    # the 1-based index (defensive; the callers zip from the same grid).
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        warm_movers_snapshots([lambda: 1, lambda: 1], segment="spot", labels=["only-one"])
    progress = [e for e in logs if e["event"] == "movers.warm.progress"]
    assert [e["key"] for e in progress] == ["only-one", "2"]


def test_warm_coordinate_label_renders_grid_dicts() -> None:
    assert warm_coordinate_label({"exchange": None}) == "all"
    assert warm_coordinate_label({"exchange": "NSE", "expiry_tier": "near"}) == "NSE|near"
    assert (
        warm_coordinate_label({"exchange": "BSE", "expiry_tier": "far", "expiry_track": "monthly"})
        == "BSE|far|monthly"
    )
    assert (
        warm_coordinate_label({"exchange": None, "expiry_tier": "next", "expiry_track": "all"})
        == "all|next|all"
    )


# ── DEC-MOVERS-WARM-GRID-001: the post-pull warm grids (pure, exhaustive) ──


def test_spot_warm_grid_one_coord_per_exchange() -> None:
    grid = spot_warm_grid()
    assert [c["exchange"] for c in grid] == list(WARM_EXCHANGES)
    assert len(grid) == 3  # None + NSE + BSE


def test_futures_warm_grid_is_exchange_times_tier() -> None:
    grid = futures_warm_grid()
    assert len(grid) == len(WARM_EXCHANGES) * len(WARM_OPTIONS_TIERS)  # 3 × 3 = 9
    # every (exchange, tier) pair appears exactly once
    pairs = {(c["exchange"], c["expiry_tier"]) for c in grid}
    assert pairs == {(e, t) for e in WARM_EXCHANGES for t in WARM_OPTIONS_TIERS}


def test_options_warm_grid_is_exchange_times_tier_times_track() -> None:
    grid = options_warm_grid()
    assert len(grid) == len(WARM_EXCHANGES) * len(WARM_OPTIONS_TIERS) * len(WARM_OPTIONS_TRACKS)
    assert len(grid) == 27
    triples = {(c["exchange"], c["expiry_tier"], c["expiry_track"]) for c in grid}
    assert triples == {
        (e, t, k) for e in WARM_EXCHANGES for t in WARM_OPTIONS_TIERS for k in WARM_OPTIONS_TRACKS
    }


def test_grid_tokens_are_lowercase_canonical() -> None:
    # The cache key uses lower-cased tier/track tokens; the grid must match.
    assert WARM_OPTIONS_TIERS == ("near", "next", "far")
    assert WARM_OPTIONS_TRACKS == ("all", "weekly", "monthly")
    assert WARM_EXCHANGES == (None, "NSE", "BSE")
