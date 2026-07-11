"""%-baseline file pruning — manifest / month-grammar routing (R2/R2b).

DEC-DB-BASELINE-MANIFEST-ROUTE-001. FAILS-BEFORE (captured against the
pre-fix readers): ``_intraday_prev_close_by_page_date`` bound the WHOLE
multi-year 1-minute partition glob TWICE per paging click and
``_underlying_spot_close_by_ts`` once per moneyness page — DuckDB row-group
pruning does NOT prune FILES, so every stored month's footer was opened per
click. Now both reads bind a pruned file list (:func:`_bars_file_windows`:
manifest-exact bounds, else the canonical UTC-month filename grammar), with
byte-identical results pinned manifest-on vs grammar vs the legacy whole
glob (the pruner only SELECTS files, never changes values).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import errno
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path, PosixPath
from typing import Any

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.dashboard import db_view
from brutex.dashboard.db_view import (
    _bars_file_windows,
    _execute_parquet_resilient,
    _files_intersecting,
    _intraday_prev_close_by_page_date,
    _predecessor_files,
    _underlying_spot_close_by_ts,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.lake_stats_manifest import KIND_BARS, StatsFileRow, seed_dataset_stats
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_KEY = {"exchange": "NSE", "segment": "CASH", "symbol": "NSE-NIFTY", "interval": "1minute"}


@pytest.fixture(autouse=True)
def _fresh_subset_cache() -> Any:
    db_view._reset_readable_subset_cache()
    return


@pytest.fixture
def store(tmp_path: Path) -> InstrumentStore:
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def _bar(ts: datetime, close: str) -> CandleBar:
    c = Decimal(close)
    return CandleBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=10, open_interest=None)


def _write_months(
    lake: Path, months: int = 24, *, segment: str = "CASH", symbol: str = "NSE-NIFTY"
) -> Path:
    """One file per month (2023-01 …), two trading days each, closes encode the month."""
    writer = ParquetWriter(lake_root=lake)
    year, month = 2023, 1
    for i in range(months):
        base_close = 100 + i
        writer.write(
            exchange="NSE",
            segment=segment,
            symbol=symbol,
            interval="1minute",
            bars=[
                _bar(datetime(year, month, 3, 4, 1, tzinfo=UTC), f"{base_close}.10"),
                _bar(datetime(year, month, 4, 4, 1, tzinfo=UTC), f"{base_close}.20"),
            ],
        )
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return lake / "bars" / "NSE" / segment / symbol / "1minute"


def _seed_manifest(lake: Path, base: Path, key: dict[str, str]) -> None:
    """Seed a REAL validated stats shard from the on-disk files."""
    rows: list[StatsFileRow] = []
    for file in sorted(base.rglob("*.parquet")):
        st = file.stat()
        ts = pl.read_parquet(file, columns=["timestamp"]).get_column("timestamp")
        ts_min, ts_max = ts.min(), ts.max()
        assert isinstance(ts_min, datetime) and isinstance(ts_max, datetime)
        rows.append(
            StatsFileRow(
                rel=file.relative_to(base).as_posix(),
                rows=ts.n_unique(),
                min_ts=ts_min,
                max_ts=ts_max,
                mtime_ns=st.st_mtime_ns,
                size=st.st_size,
                raw_hash=None,
            )
        )
    assert seed_dataset_stats(lake, kind=KIND_BARS, key=key, files=rows)


class _BindSpy:
    """Delegating connection recording every ``read_parquet`` source bind."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.binds: list[Any] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        if params and "read_parquet" in sql:
            self.binds.append(params[0])
        return self._conn.execute(sql, params)

    def bound_files(self) -> set[str]:
        out: set[str] = set()
        for bind in self.binds:
            if isinstance(bind, list):
                out.update(Path(p).name for p in bind)
        return out


class TestPrevClosePruning:
    def test_page_month_plus_one_predecessor_file(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The headline spy pin: 24 stored month files; a June-2024 page must
        # bind ONLY the June file (span) + the May file (predecessor) — never
        # the whole-partition glob (FAILS-BEFORE: both reads bound
        # '<base>/**/*.parquet', opening all 24 footers per click).
        _write_months(tmp_path, 24)
        spy = _BindSpy(store.connection)
        out = _intraday_prev_close_by_page_date(
            spy,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            page_dates={date(2024, 6, 3), date(2024, 6, 4)},
        )
        assert out[date(2024, 6, 4)] == Decimal("117.10")  # June 3's close
        assert out[date(2024, 6, 3)] == Decimal("116.20")  # May 4's close (the pred file)
        assert all(isinstance(b, list) for b in spy.binds)  # never the whole glob
        assert spy.bound_files() == {"06.parquet", "05.parquet"}  # 2 of 24 files

    def test_parity_manifest_vs_grammar_vs_legacy(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The byte-identity law: the pruner only SELECTS files. All three arms
        # (validated manifest shard / month-grammar listing / legacy whole
        # glob) produce the identical baseline map.
        base = _write_months(tmp_path, 24)
        pages = [
            {date(2023, 1, 3)},  # the very first trading day — no predecessor
            {date(2023, 1, 4)},  # pred inside the same (first) month
            {date(2024, 6, 3), date(2024, 6, 4)},  # pred in the prior month
            {date(2025, 6, 3)},  # after ALL data — empty span, deep pred
        ]

        def run() -> list[dict[date, Decimal]]:
            return [
                _intraday_prev_close_by_page_date(
                    store.connection,
                    tmp_path,
                    exchange="NSE",
                    segment="CASH",
                    symbol="NSE-NIFTY",
                    page_dates=p,
                )
                for p in pages
            ]

        grammar = run()  # no shard on disk → the month-grammar arm
        _seed_manifest(tmp_path, base, _KEY)
        manifest = run()  # validated shard → the manifest arm
        monkeypatch.setattr(db_view, "_bars_file_windows", lambda *a, **k: None)
        legacy = run()  # cannot bound → the legacy whole-glob arm
        assert grammar == manifest == legacy
        assert grammar[0] == {}  # first day: honestly no baseline
        assert grammar[1] == {date(2023, 1, 4): Decimal("100.10")}

    def test_grammar_step_back_when_preceding_month_holds_no_earlier_bar(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The documented grammar-arm step-back: the page's OWN month file is a
        # pred candidate (month start < lo) but holds no bar before the page —
        # the loop steps back to the prior month file and lands the true pred.
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(datetime(2024, 5, 31, 4, 1, tzinfo=UTC), "99.50")],
        )
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(datetime(2024, 6, 3, 4, 1, tzinfo=UTC), "100.00")],
        )
        spy = _BindSpy(store.connection)
        out = _intraday_prev_close_by_page_date(
            spy,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            page_dates={date(2024, 6, 3)},
        )
        assert out == {date(2024, 6, 3): Decimal("99.50")}  # May 31's close
        assert spy.bound_files() == {"06.parquet", "05.parquet"}

    def test_corrupt_pred_candidate_still_blanks_adjacency(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The DEC-DB-CORRUPT-SKIP-001 adjacency law survives pruning: a corrupt
        # file among the CONSULTED pred candidates blanks the baseline.
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(datetime(2024, 5, 31, 4, 1, tzinfo=UTC), "99.50")],
        )
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(datetime(2024, 6, 3, 4, 1, tzinfo=UTC), "100.00")],
        )
        base = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
        (base / "2024" / "05.parquet").write_bytes(b"garbage \r")
        out = _intraday_prev_close_by_page_date(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            page_dates={date(2024, 6, 3)},
        )
        assert out == {}  # honest blank — never a bridge past the corrupt file

    def test_non_canonical_filename_degrades_to_legacy_glob(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # A non-month-grammar filename cannot be bounded → the whole read
        # degrades to the legacy glob (which serves the stray file's bars —
        # byte-identical to pre-fix behaviour).
        base = _write_months(tmp_path, 2)
        stray = base / "2023" / "stray.parquet"
        stray.write_bytes((base / "2023" / "01.parquet").read_bytes())
        assert (
            _bars_file_windows(
                tmp_path,
                base,
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1minute",
            )
            is None
        )
        spy = _BindSpy(store.connection)
        out = _intraday_prev_close_by_page_date(
            spy,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            page_dates={date(2023, 2, 3)},
        )
        assert out == {date(2023, 2, 3): Decimal("100.20")}
        assert any(isinstance(b, str) and b.endswith("/**/*.parquet") for b in spy.binds)

    def test_listing_fault_degrades_to_legacy(self, tmp_path: Path) -> None:
        class _RglobRaises(PosixPath):
            def rglob(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError(errno.EIO, "wedged", str(self))

        base = _write_months(tmp_path, 1)
        assert (
            _bars_file_windows(
                tmp_path,
                _RglobRaises(base),
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1minute",
            )
            is None
        )


class TestSpotJoinPruning:
    def test_moneyness_spot_read_binds_page_months_only(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _write_months(tmp_path, 24, segment="INDEX", symbol="NIFTY")
        spy = _BindSpy(store.connection)
        ts = datetime(2024, 6, 3, 4, 1, tzinfo=UTC)
        out, served_interval = _underlying_spot_close_by_ts(
            spy,
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            ts_values=[ts],
        )
        assert out == {ts: Decimal("117.10")}
        assert served_interval == "1minute"  # no TF fallback engaged (#1367 shape)
        assert all(isinstance(b, list) for b in spy.binds)
        assert spy.bound_files() == {"06.parquet"}  # 1 of 24 files

    def test_spot_join_parity_pruned_vs_legacy(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_months(tmp_path, 6, segment="INDEX", symbol="NIFTY")
        ts_values = [
            datetime(2023, 2, 3, 4, 1, tzinfo=UTC),
            datetime(2023, 4, 4, 4, 1, tzinfo=UTC),
        ]

        def run() -> dict[datetime, Decimal]:
            spots, _served_interval = _underlying_spot_close_by_ts(
                store.connection,
                tmp_path,
                exchange="NSE",
                underlying="NIFTY",
                interval="1minute",
                ts_values=ts_values,
            )
            return spots

        pruned = run()
        monkeypatch.setattr(db_view, "_bars_file_windows", lambda *a, **k: None)
        legacy = run()
        assert pruned == legacy
        assert pruned[ts_values[0]] == Decimal("101.10")
        assert pruned[ts_values[1]] == Decimal("103.20")


class TestPrunerPrimitives:
    def test_empty_pruned_list_is_zero_io(self, store: InstrumentStore, tmp_path: Path) -> None:
        spy = _BindSpy(store.connection)
        rows = _execute_parquet_resilient(
            spy,
            tmp_path,
            "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true)",
            [],
            source="unit",
            files=[],
        )
        assert rows == []
        assert spy.binds == []  # short-circuit — DuckDB never sees an empty list

    def test_window_selection_half_open_edges(self) -> None:
        w = [
            ("a", datetime(2024, 5, 1, tzinfo=UTC), datetime(2024, 6, 1, tzinfo=UTC)),
            ("b", datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 7, 1, tzinfo=UTC)),
        ]
        june = datetime(2024, 6, 1, tzinfo=UTC)
        # [june, july): 'a' ends exactly AT june (exclusive) — never selected.
        assert _files_intersecting(w, june, datetime(2024, 7, 1, tzinfo=UTC)) == ["b"]
        # Predecessor of june: only 'a' STARTS before it, newest-first order.
        assert _predecessor_files(w, june) == ["a"]
        assert _predecessor_files(w, datetime(2024, 5, 1, tzinfo=UTC)) == []
