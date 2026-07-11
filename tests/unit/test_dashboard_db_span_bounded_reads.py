"""Tests — the span-bounded lake reads (DEC-DB-LATENCY-HARDENING-001, MIGRATION.md #1719).

Evidence (profiling worker, 2026-07-11): the pre-open export's minute basis
bound EVERY 1-minute month file of EVERY symbol per click (616 files at 103
symbols) where a 3-week report needs ~1–2 month files per symbol. The IEP
(open) readers (``_daily_open_by_date`` / ``_intraday_open_by_date``) now
accept the report's own inclusive ``[start, end]`` *span* and bind ONLY the
existing month files that can hold an in-span bar
(:func:`_existing_month_files_in_span` — the ±1-day pad law of
``ist_span_partition_months``), through the COMMON
``_execute_parquet_resilient``'s new bounded *files* arm.

Pins: exhaustive BYTE-PARITY of the bounded reads vs the unbounded whole-glob
reads over a multi-month fixture (the #863 proof pattern); the per-statement
file-bind op-count (O(span months), not O(all)); the pad/saturation/grammar
laws of the month enumerator; the bounded corrupt-skip degrade (probes ONLY
the candidates — a corrupt file outside the span neither errors nor blanks).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

import duckdb
from brutex.dashboard.db_view import (
    _daily_open_by_date,
    _execute_parquet_resilient,
    _existing_month_files_in_span,
    _intraday_open_by_date,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from structlog.testing import capture_logs


def _bar(ts: datetime, *, open_: str = "100.00") -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _seed_months(lake: Path, *, interval: str, months: int = 8) -> Path:
    """One symbol × *months* month files (2024-01..) via the real writer."""
    writer = ParquetWriter(lake_root=lake)
    for m in range(1, months + 1):
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval=interval,
            bars=[
                # Distinct in-range opens per (month, day) so a mis-mapped
                # bounded read can never pass parity by coincidence.
                _bar(
                    datetime(2024, m, d, 3, 45, tzinfo=UTC), open_=f"{99 + m * 0.1 + d * 0.001:.2f}"
                )
                for d in (2, 15, 27)
            ],
        )
    return lake / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / interval


class _BindSpy:
    """Records the FILE COUNT each ``read_parquet`` statement binds."""

    def __init__(self) -> None:
        self._real = duckdb.connect()
        self.binds: list[int] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        if params and "read_parquet" in sql:
            src = params[0]
            if isinstance(src, list):
                self.binds.append(len(src))
            else:
                import glob as globmod

                self.binds.append(len(globmod.glob(str(src), recursive=True)))
        return self._real.execute(sql, params)


class TestExistingMonthFilesInSpan:
    def test_span_filter_keeps_padded_months_only(self, tmp_path: Path) -> None:
        base = _seed_months(tmp_path, interval="1day", months=8)
        files = _existing_month_files_in_span(base, date(2024, 3, 5), date(2024, 4, 20))
        # ±1-day pad: [Mar 4, Apr 21] → months 3..4 only (the pad crosses no
        # month boundary here); 2 of the 8 stored files.
        assert [Path(f).parent.name + "/" + Path(f).name for f in files] == [
            "2024/03.parquet",
            "2024/04.parquet",
        ]
        # A month-boundary span day pads INTO the adjacent month.
        first = _existing_month_files_in_span(base, date(2024, 3, 1), date(2024, 3, 31))
        assert [Path(f).name for f in first] == ["02.parquet", "03.parquet", "04.parquet"]

    def test_date_domain_edges_saturate_never_overflow(self, tmp_path: Path) -> None:
        base = _seed_months(tmp_path, interval="1day", months=3)
        # A degenerate whole-domain span keeps every file — no OverflowError.
        files = _existing_month_files_in_span(base, date.min, date.max)
        assert len(files) == 3

    def test_non_grammar_file_is_conservatively_kept(self, tmp_path: Path) -> None:
        """A stray parquet outside the <yyyy>/<mm> grammar has an unknowable
        month — it is KEPT so the bounded read can never exclude a file the
        whole-glob read would have seen (byte-parity, Golden Rule #1)."""
        base = _seed_months(tmp_path, interval="1day", months=2)
        stray = base / "stray.parquet"
        stray.write_bytes((base / "2024" / "01.parquet").read_bytes())
        files = _existing_month_files_in_span(base, date(2024, 2, 5), date(2024, 2, 20))
        assert stray.as_posix() in files
        assert any(f.endswith("2024/02.parquet") for f in files)
        assert not any(f.endswith("2024/01.parquet") for f in files)


class TestSpanBoundedOpenReaders:
    @pytest.mark.parametrize("interval", ["1day", "1minute"])
    def test_bounded_equals_unbounded_exhaustively(self, tmp_path: Path, interval: str) -> None:
        """THE #863 proof pattern: for EVERY consumed (in-span) day, the bounded
        read's opens equal the unbounded whole-glob read's — over every span of
        a multi-month fixture."""
        _seed_months(tmp_path, interval=interval, months=8)
        reader = _daily_open_by_date if interval == "1day" else _intraday_open_by_date
        with duckdb.connect() as conn:
            unbounded = reader(conn, tmp_path, exchange="NSE", segment="CASH", symbol="NSE-NIFTY")
            assert len(unbounded) == 24  # 8 months × 3 days (sanity)
            spans = [
                (date(2024, 1, 1), date(2024, 1, 31)),
                (date(2024, 2, 10), date(2024, 3, 20)),
                (date(2024, 1, 2), date(2024, 8, 27)),
                (date(2024, 5, 1), date(2024, 5, 1)),
                (date(2023, 1, 1), date(2023, 12, 31)),  # fully before the lake
                (date(2025, 1, 1), date(2025, 12, 31)),  # fully after the lake
            ]
            for span in spans:
                bounded = reader(
                    conn,
                    tmp_path,
                    exchange="NSE",
                    segment="CASH",
                    symbol="NSE-NIFTY",
                    span=span,
                )
                consumed = {d: v for d, v in unbounded.items() if span[0] <= d <= span[1]}
                bounded_consumed = {d: v for d, v in bounded.items() if span[0] <= d <= span[1]}
                assert bounded_consumed == consumed

    def test_click_binds_o_span_files_not_o_all(self, tmp_path: Path) -> None:
        """THE fix-2(b) op-count teeth (fails-before): a one-month report span
        binds ≤ 3 month files per statement instead of all 8 stored months —
        the pre-fix whole-glob read bound every file (616 measured at the
        profiling lake's 103 symbols, minute basis)."""
        _seed_months(tmp_path, interval="1minute", months=8)
        spy = _BindSpy()
        bounded = _intraday_open_by_date(
            spy,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            span=(date(2024, 3, 1), date(2024, 3, 31)),
        )
        assert bounded  # rows really served
        assert spy.binds and max(spy.binds) <= 3  # Feb+Mar+Apr (the ±1-day pad)
        unbounded_spy = _BindSpy()
        _intraday_open_by_date(
            unbounded_spy, tmp_path, exchange="NSE", segment="CASH", symbol="NSE-NIFTY"
        )
        assert max(unbounded_spy.binds) == 8  # the pre-fix cost, for contrast

    def test_empty_span_candidate_set_short_circuits(self, tmp_path: Path) -> None:
        """A span with NO existing month file returns ``{}`` with ZERO queries
        (no candidate file ⇒ no matchable bar — identical to the whole-glob
        read's empty answer for that span)."""
        _seed_months(tmp_path, interval="1day", months=2)
        spy = _BindSpy()
        out = _daily_open_by_date(
            spy,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            span=(date(2030, 1, 5), date(2030, 1, 20)),
        )
        assert out == {} and spy.binds == []


class TestBoundedCorruptSkip:
    def test_corrupt_candidate_probed_and_skipped(self, tmp_path: Path) -> None:
        """A corrupt file INSIDE the bounded set degrades to the readable
        candidate subset (probing ONLY the candidates), never a 500."""
        base = _seed_months(tmp_path, interval="1day", months=3)
        (base / "2024" / "02.parquet").write_bytes(b"garbage")
        with duckdb.connect() as conn, capture_logs() as logs:
            out = _daily_open_by_date(
                conn,
                tmp_path,
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                span=(date(2024, 1, 1), date(2024, 2, 28)),
            )
        # January's opens survive; February's are honestly absent.
        assert {d.month for d in out} == {1}
        skip_paths = [e["path"] for e in logs if e["event"] == "db.parquet_skip"]
        assert skip_paths == [(base / "2024" / "02.parquet").as_posix()]

    def test_corrupt_file_outside_span_never_touched(self, tmp_path: Path) -> None:
        """The narrowed degrade blast radius (named in MIGRATION.md #1719): a
        corrupt file provably OUTSIDE the bounded candidates neither errors the
        read nor blanks the span's values — it was never part of the statement."""
        base = _seed_months(tmp_path, interval="1day", months=3)
        (base / "2024" / "03.parquet").write_bytes(b"garbage")
        with duckdb.connect() as conn, capture_logs() as logs:
            out = _daily_open_by_date(
                conn,
                tmp_path,
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                span=(date(2024, 1, 5), date(2024, 1, 20)),
            )
        assert {d.month for d in out} == {1}  # served, not blanked
        assert not [e for e in logs if e["event"] == "db.parquet_skip"]

    def test_all_bounded_candidates_unreadable_degrades_empty(self, tmp_path: Path) -> None:
        """Every bounded candidate corrupt → the honest empty result + ONE
        unreadable log (the whole-glob contract, candidate-scoped)."""
        base = _seed_months(tmp_path, interval="1day", months=2)
        (base / "2024" / "01.parquet").write_bytes(b"garbage")
        with duckdb.connect() as conn, capture_logs() as logs:
            out = _daily_open_by_date(
                conn,
                tmp_path,
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                span=(date(2024, 1, 5), date(2024, 1, 20)),
            )
        assert out == {}
        assert [e["event"] for e in logs if "unreadable" in e["event"]] == ["db.parquet_unreadable"]


class TestExecuteParquetResilientFilesArm:
    def test_empty_files_short_circuits_with_zero_queries(self, tmp_path: Path) -> None:
        spy = _BindSpy()
        assert (
            _execute_parquet_resilient(
                spy,
                tmp_path,
                "SELECT 1 FROM read_parquet(?, union_by_name=true)",
                [],
                source="test",
                files=[],
            )
            == []
        )
        assert spy.binds == []
