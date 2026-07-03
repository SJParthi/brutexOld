"""Unit tests for the read-side dedup chokepoint (``brutex.data.lake_read``).

DEC-LAKE-READ-UNIQUE-001 — 100% line + branch coverage of the ONE COMMON
keep-FIRST read gate: :func:`dedup_bars` / :func:`bars_by_ts_first` (sequence),
:func:`dedup_bars_frame` (polars frame), the DuckDB SQL fragment builders
(:func:`dedup_qualify_sql` / :func:`distinct_count_sql` — validated against the
REAL pinned DuckDB engine, not string-asserted alone), and the aggregated
:data:`DUPLICATES_DROPPED_EVENT` WARNING contract (source label + dropped +
conflict_count; silent on a clean read).

Fails-before discipline: on unfixed main this module does not exist; the
behavioural pins live in the wired-call-site suites (catalog / replay / movers /
options / pagers) — see ``tests/integration/test_lake_read_poison.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest
import structlog
from brutex.data.lake_read import (
    DEDUP_PARQUET_SOURCE_SQL,
    DEDUP_STAR_SELECT_SQL,
    DUPLICATES_DROPPED_EVENT,
    bars_by_ts_first,
    dedup_bars,
    dedup_bars_frame,
    dedup_qualify_sql,
    distinct_count_sql,
    log_read_duplicates,
)
from brutex.data.models import CandleBar
from hypothesis import given
from hypothesis import strategies as st
from structlog.typing import EventDict

_T0 = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)  # 09:15 IST


def _bar(minute: int, close: str = "100.00", volume: int = 10) -> CandleBar:
    value = Decimal(close)
    return CandleBar(
        timestamp=_T0 + timedelta(minutes=minute),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=volume,
        open_interest=None,
    )


class _capture_logs:
    """Context manager capturing structlog events into a list (configure/restore)."""

    def __init__(self) -> None:
        self._cap = structlog.testing.LogCapture()
        self._saved: dict[str, object] | None = None

    def __enter__(self) -> list[EventDict]:
        self._saved = structlog.get_config()
        structlog.configure(processors=[self._cap])
        return self._cap.entries

    def __exit__(self, *_exc: object) -> None:
        assert self._saved is not None
        structlog.configure(**self._saved)  # type: ignore[arg-type]


def _dedup_events(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == DUPLICATES_DROPPED_EVENT]


# ────────────────────────────────────────────────────────────────────────
# log_read_duplicates
# ────────────────────────────────────────────────────────────────────────


class TestLogReadDuplicates:
    def test_positive_dropped_emits_one_warning(self) -> None:
        with _capture_logs() as logs:
            log_read_duplicates(source="unit:test", dropped=3, conflicts=1)
        events = _dedup_events(logs)
        assert len(events) == 1
        assert events[0]["source"] == "unit:test"
        assert events[0]["dropped"] == 3
        assert events[0]["conflict_count"] == 1
        assert events[0]["log_level"] == "warning"

    def test_zero_dropped_is_silent(self) -> None:
        with _capture_logs() as logs:
            log_read_duplicates(source="unit:test", dropped=0, conflicts=0)
        assert _dedup_events(logs) == []

    def test_negative_dropped_is_silent(self) -> None:
        with _capture_logs() as logs:
            log_read_duplicates(source="unit:test", dropped=-1, conflicts=0)
        assert _dedup_events(logs) == []


# ────────────────────────────────────────────────────────────────────────
# dedup_bars / bars_by_ts_first
# ────────────────────────────────────────────────────────────────────────


class TestDedupBars:
    def test_clean_stream_passes_through_silent(self) -> None:
        bars = [_bar(0), _bar(1), _bar(2)]
        with _capture_logs() as logs:
            clean, dropped = dedup_bars(bars, source="unit:clean")
        assert clean == bars
        assert dropped == 0
        assert _dedup_events(logs) == []

    def test_exact_duplicate_keeps_first_and_warns_without_conflict(self) -> None:
        bars = [_bar(0), _bar(0), _bar(1)]
        with _capture_logs() as logs:
            clean, dropped = dedup_bars(bars, source="unit:dup")
        assert clean == [_bar(0), _bar(1)]
        assert dropped == 1
        events = _dedup_events(logs)
        assert len(events) == 1
        assert events[0]["dropped"] == 1
        assert events[0]["conflict_count"] == 0

    def test_conflict_duplicate_keeps_first_and_counts_conflict(self) -> None:
        first = _bar(0, close="100.00")
        restated = _bar(0, close="999.99")
        with _capture_logs() as logs:
            clean, dropped = dedup_bars([first, restated, _bar(1)], source="unit:conflict")
        assert clean == [first, _bar(1)]  # the FIRST occurrence wins, never the re-statement
        assert dropped == 1
        events = _dedup_events(logs)
        assert events[0]["conflict_count"] == 1

    def test_out_of_order_duplicate_preserves_input_order(self) -> None:
        # Dedup does NOT sort — order preservation is the caller's contract
        # (dedup BEFORE sort so "first" is deterministic).
        bars = [_bar(2), _bar(0), _bar(2, close="5.00"), _bar(1)]
        clean, dropped = dedup_bars(bars, source="unit:order")
        assert [b.timestamp for b in clean] == [
            _T0 + timedelta(minutes=2),
            _T0,
            _T0 + timedelta(minutes=1),
        ]
        assert clean[0].close == Decimal("100.00")  # the first minute-2 bar won
        assert dropped == 1

    def test_empty_input(self) -> None:
        empty: list[CandleBar] = []
        clean, dropped = dedup_bars(empty, source="unit:empty")
        assert clean == []
        assert dropped == 0

    def test_bars_by_ts_first_is_keep_first(self) -> None:
        first = _bar(0, close="1.00")
        with _capture_logs() as logs:
            mapping = bars_by_ts_first([first, _bar(0, close="2.00")], source="unit:map")
        assert mapping == {first.timestamp: first}
        assert len(_dedup_events(logs)) == 1

    @given(
        st.lists(
            st.tuples(st.integers(min_value=0, max_value=30), st.integers(min_value=1, max_value=5))
        )
    )
    def test_property_idempotent_and_unique(self, spec: list[tuple[int, int]]) -> None:
        """Dedup output has unique timestamps and re-dedup is a fixed point."""
        bars = [_bar(minute, close=f"{val}.00") for minute, val in spec]
        clean, dropped = dedup_bars(bars, source="unit:prop")
        timestamps = [b.timestamp for b in clean]
        assert len(set(timestamps)) == len(timestamps)
        assert len(clean) + dropped == len(bars)
        again, dropped_again = dedup_bars(clean, source="unit:prop")
        assert again == clean
        assert dropped_again == 0


# ────────────────────────────────────────────────────────────────────────
# dedup_bars_frame
# ────────────────────────────────────────────────────────────────────────


def _frame(rows: list[tuple[int, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "timestamp": [_T0 + timedelta(minutes=m) for m, _ in rows],
            "close": [c for _, c in rows],
        }
    )


class TestDedupBarsFrame:
    def test_clean_frame_passes_through_silent(self) -> None:
        df = _frame([(0, 1.0), (1, 2.0)])
        with _capture_logs() as logs:
            clean, dropped = dedup_bars_frame(df, source="unit:frame")
        assert clean.equals(df)
        assert dropped == 0
        assert _dedup_events(logs) == []

    def test_exact_duplicate_row_drops_without_conflict(self) -> None:
        df = _frame([(0, 1.0), (0, 1.0), (1, 2.0)])
        with _capture_logs() as logs:
            clean, dropped = dedup_bars_frame(df, source="unit:frame")
        assert clean.equals(_frame([(0, 1.0), (1, 2.0)]))
        assert dropped == 1
        events = _dedup_events(logs)
        assert events[0]["dropped"] == 1
        assert events[0]["conflict_count"] == 0

    def test_conflict_duplicate_keeps_first_physical_row(self) -> None:
        df = _frame([(0, 1.0), (0, 9.0), (1, 2.0)])
        with _capture_logs() as logs:
            clean, dropped = dedup_bars_frame(df, source="unit:frame")
        assert clean.equals(_frame([(0, 1.0), (1, 2.0)]))
        assert dropped == 1
        assert _dedup_events(logs)[0]["conflict_count"] == 1

    def test_composite_subset_key(self) -> None:
        df = pl.DataFrame(
            {
                "timestamp": [_T0, _T0, _T0],
                "side": ["CALL", "CALL", "PUT"],
                "close": [1.0, 9.0, 3.0],
            }
        )
        clean, dropped = dedup_bars_frame(df, source="unit:composite", subset=("side", "timestamp"))
        # (CALL, T0) collapses keep-first; (PUT, T0) is a distinct composite key.
        assert clean.get_column("close").to_list() == [1.0, 3.0]
        assert dropped == 1

    def test_empty_frame(self) -> None:
        df = _frame([])
        clean, dropped = dedup_bars_frame(df, source="unit:frame")
        assert clean.height == 0
        assert dropped == 0


# ────────────────────────────────────────────────────────────────────────
# DuckDB SQL fragments — validated against the REAL pinned engine
# ────────────────────────────────────────────────────────────────────────


class TestSqlFragments:
    def test_qualify_fragment_shape(self) -> None:
        assert dedup_qualify_sql(("timestamp",)) == (
            ' QUALIFY row_number() OVER (PARTITION BY "timestamp" '
            "ORDER BY filename, file_row_number) = 1"
        )

    def test_qualify_composite_keys(self) -> None:
        fragment = dedup_qualify_sql(("side", "timestamp"))
        assert 'PARTITION BY "side", "timestamp"' in fragment

    def test_distinct_count_single_key(self) -> None:
        assert distinct_count_sql(("timestamp",)) == 'COUNT(DISTINCT "timestamp")'

    def test_distinct_count_composite_key(self) -> None:
        assert distinct_count_sql(("side", "timestamp")) == (
            'COUNT(DISTINCT ("side", "timestamp"))'
        )

    def test_empty_keys_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            dedup_qualify_sql(())
        with pytest.raises(ValueError, match="non-empty"):
            distinct_count_sql(())

    def test_non_identifier_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a bare SQL identifier"):
            dedup_qualify_sql(('ts"; DROP TABLE x; --',))
        with pytest.raises(ValueError, match="not a bare SQL identifier"):
            distinct_count_sql(("a b",))

    def test_fragments_execute_on_pinned_duckdb(self, tmp_path: object) -> None:
        """The QUALIFY + file_row_number + EXCLUDE grammar runs on the REAL engine.

        This is the version pin the module docstring claims: if a future DuckDB
        bump drops ``QUALIFY`` / ``file_row_number`` / ``EXCLUDE`` support, this
        test fails loudly instead of the /db pagers 500ing in production.
        """
        import pathlib

        import duckdb

        directory = pathlib.Path(str(tmp_path))
        pl.DataFrame(
            {
                "timestamp": [_T0, _T0, _T0 + timedelta(minutes=1)],
                "side": ["CALL", "CALL", "CALL"],
                "close": [1.0, 9.0, 2.0],
            }
        ).write_parquet(directory / "a.parquet")
        conn = duckdb.connect()
        glob = directory.as_posix() + "/*.parquet"
        page = conn.execute(
            f"SELECT {DEDUP_STAR_SELECT_SQL} FROM {DEDUP_PARQUET_SOURCE_SQL}"  # noqa: S608
            f'{dedup_qualify_sql(("side", "timestamp"))} ORDER BY "timestamp"',
            [glob],
        )
        columns = [d[0] for d in page.description or []]
        rows = page.fetchall()
        assert columns == ["timestamp", "side", "close"]  # synthetic columns excluded
        assert [r[2] for r in rows] == [1.0, 2.0]  # keep-FIRST physical row won
        total = conn.execute(
            f"SELECT {distinct_count_sql(('side', 'timestamp'))} FROM read_parquet(?)",  # noqa: S608
            [glob],
        ).fetchone()
        assert total is not None and int(total[0]) == 2
