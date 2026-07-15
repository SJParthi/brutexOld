"""Unit gate for :meth:`ParquetBarSink.append_frame` (DEC-PREBUILD-FRAME-WRITE-001).

The SINK-owned contracts of the frame-direct sibling (never bypassed — the
judge-flagged throughput-max hole, closed at Seam B):

* the C13 empty-input early-return (byte-identical to ``append([])``);
* ``bars_appended = frame.height`` — the INPUT count (the historical ``> 0``
  "did this call carry data?" gate contract);
* vectorized UTC-month grouping iterated in ASCENDING month order (the
  legacy ``sorted(groups.items())`` law);
* the catalog upsert arm (parity for any future catalog-wired caller);
* the ts-column pre-check degrade: an untrustworthy ``ts_utc`` routes the
  WHOLE call through the verbatim legacy ``append`` (same grouping, same
  failure classes as the flag-OFF path);
* sink-level byte parity vs ``append(frame_to_candle_bars(...))``.

Stem-named ``test_data_sink_*`` so the ci coverage discovery gate maps it to
``brutex/data/sink.py`` (the writer-side twin lives in
``tests/unit/test_parquet_writer_frame_write.py``; the small frame helpers are
deliberately local per test-file convention).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TypedDict

import pytest
from brutex.data import frame_write_gate as gate
from brutex.data.frame_write_gate import PREBUILD_FRAME_WRITE_ENV, FrameWriteStats
from brutex.data.models import CatalogEntry
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_daily_resample import frame_to_candle_bars

pytest.importorskip("polars")

import polars as pl

_T0 = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)  # 09:15 IST, a regular session


class _Coords(TypedDict):
    """Exact-key coordinate kwargs — a plain dict[str, str] would make pyright
    reject ``**_COORDS`` unpacking against ``append_frame``'s non-str keyword
    params (``absent_open_interest`` / ``stats``); the TypedDict pins the key
    set so the unpack type-checks precisely."""

    exchange: str
    segment: str
    symbol: str
    interval: str


_COORDS: Final[_Coords] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "5minute",
}


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh gate state + the flag defaulting OFF; tests opt in per case."""
    monkeypatch.delenv(PREBUILD_FRAME_WRITE_ENV, raising=False)
    gate.reset_gate_for_tests()
    gate.reset_observability_memo_for_tests()


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    return tmp_path / "lake"


@pytest.fixture
def writer(lake: Path) -> ParquetWriter:
    return ParquetWriter(lake_root=lake, partition_log_level="debug")


def _frame(
    n: int = 4,
    *,
    start: datetime = _T0,
    step_minutes: int = 5,
    base: float = 100.0,
    with_oi: bool = False,
) -> pl.DataFrame:
    """A healthy resampler-shaped frame (sorted, unique, positive, ordered)."""
    data: dict[str, Any] = {
        "ts_utc": [start + timedelta(minutes=step_minutes * i) for i in range(n)],
        "open": [base + i for i in range(n)],
        "high": [base + 10 + i for i in range(n)],
        "low": [base - 5 + i for i in range(n)],
        "close": [base + 2 + i for i in range(n)],
        "volume": [10 + i for i in range(n)],
    }
    schema: dict[str, Any] = {
        "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
    }
    if with_oi:
        data["open_interest"] = [500 + i for i in range(n)]
        schema["open_interest"] = pl.Int64
    return pl.DataFrame(data, schema=schema)


def _dest(lake: Path) -> Path:
    return lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "5minute" / "2024" / "03.parquet"


def _steady_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-burn-in ON mode: the fast lane SERVES bytes."""
    monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "1")
    gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)


class _RecordingCatalog:
    def __init__(self) -> None:
        self.entries: list[CatalogEntry] = []

    def upsert(self, entry: CatalogEntry) -> None:
        self.entries.append(entry)


class TestAppendFrame:
    def test_empty_frame_early_return(self, writer: ParquetWriter) -> None:
        """The C13 law — byte-identical to append([])."""
        sink = ParquetBarSink(writer=writer)
        result = sink.append_frame(**_COORDS, frame=_frame(0))
        assert result.bars_appended == 0
        assert result.partitions_written == 0
        assert result.write_results == ()

    def test_bars_appended_is_input_count(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        result = sink.append_frame(**_COORDS, frame=_frame(4))
        assert result.bars_appended == 4
        assert result.bars_net_new == 4
        assert result.partitions_written == 1

    def test_multi_month_grouping_ascending(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A month-straddling frame becomes one write per month, ascending."""
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        straddle = _frame(4, start=datetime(2024, 3, 31, 23, 50, tzinfo=UTC), step_minutes=5)
        result = sink.append_frame(**_COORDS, frame=straddle)
        assert result.partitions_written == 2
        months = [wr.path.name for wr in result.write_results]
        assert months == ["03.parquet", "04.parquet"]  # ascending — the sink law
        assert result.bars_appended == 4
        assert result.bars_net_new == 4

    def test_ts_dtype_pre_check_degrades_whole_frame(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An untrustworthy ts column routes the WHOLE call through legacy append."""
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        naive = _frame().with_columns(pl.col("ts_utc").dt.replace_time_zone(None))
        stats = FrameWriteStats()
        # frame_to_candle_bars on a NAIVE ts builds bars CandleBar refuses —
        # the exact flag-OFF failure (same class, same site).
        with pytest.raises(ValueError, match="timezone-aware"):
            sink.append_frame(**_COORDS, frame=naive, stats=stats)
        assert stats.degrade_reasons == {"schema_mismatch": 1}

    def test_ts_null_pre_check_degrades_whole_frame(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        bad = _frame(3).with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(None)
            .otherwise(pl.col("ts_utc"))
            .alias("ts_utc")
        )
        stats = FrameWriteStats()
        # frame_to_candle_bars hands CandleBar a None timestamp — TODAY's exact
        # legacy failure class (AttributeError from the tz check), reproduced.
        with pytest.raises(AttributeError):
            sink.append_frame(**_COORDS, frame=bad, stats=stats)
        assert stats.degrade_reasons == {"schema_mismatch": 1}

    def test_catalog_arm_parity(
        self, writer: ParquetWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The catalog upsert arm mirrors append() (parity for future callers)."""
        _steady_on(monkeypatch)
        catalog = _RecordingCatalog()
        sink = ParquetBarSink(writer=writer, catalog=catalog)  # type: ignore[arg-type]
        sink.append_frame(**_COORDS, frame=_frame(4))
        assert len(catalog.entries) == 1
        assert catalog.entries[0].row_count == 4

    def test_append_frame_bytes_equal_legacy_append(
        self, writer: ParquetWriter, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sink-level parity: append_frame ≡ append(frame_to_candle_bars(...))."""
        _steady_on(monkeypatch)
        sink = ParquetBarSink(writer=writer)
        sink.append_frame(**_COORDS, frame=_frame(with_oi=True))
        twin_root = lake.parent / "twin"
        twin_sink = ParquetBarSink(
            writer=ParquetWriter(lake_root=twin_root, partition_log_level="debug")
        )
        twin_sink.append(
            **_COORDS,
            bars=frame_to_candle_bars(_frame(with_oi=True), absent_open_interest=0),
            merge_mode="latest-wins",
        )
        assert _dest(lake).read_bytes() == _dest(twin_root).read_bytes()


class TestImportCycleDriftPin:
    """AST drift pin: the `sink ↔ spot_daily_resample` CodeQL `py/cyclic-import`
    cycle stays BROKEN (DEC-PREBUILD-FRAME-WRITE-001 review fold).

    `frame_to_candle_bars` lives in the `brutex.data.frame_bars` LEAF; the
    frame-write degrade paths (`sink.py` / `parquet_writer.py`) must import it
    from THERE — an import of `spot_daily_resample` (whose daily-derive writer
    lazily imports the sink + writer back) would re-form the cycle. The leaf
    itself must import none of its consumers.
    """

    @staticmethod
    def _imported_modules(path: Path) -> set[str]:
        """Every module named by ANY import statement (module or function level)."""
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"))
        out: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
        return out

    _SRC = Path(__file__).resolve().parents[2] / "brutex" / "data"

    def test_sink_never_imports_spot_daily_resample(self) -> None:
        assert "brutex.data.spot_daily_resample" not in self._imported_modules(
            self._SRC / "sink.py"
        )

    def test_parquet_writer_never_imports_spot_daily_resample(self) -> None:
        assert "brutex.data.spot_daily_resample" not in self._imported_modules(
            self._SRC / "parquet_writer.py"
        )

    def test_frame_bars_is_a_leaf(self) -> None:
        """The converter home imports NONE of its consumers (either level)."""
        forbidden = {
            "brutex.data.sink",
            "brutex.data.parquet_writer",
            "brutex.data.spot_daily_resample",
            "brutex.data.timeframe_materialize",
        }
        assert not forbidden & self._imported_modules(self._SRC / "frame_bars.py")

    def test_import_order_smoke_both_directions(self) -> None:
        """Real import-order smoke: either module first, in a fresh interpreter."""
        import subprocess
        import sys

        for first, second in (
            ("brutex.data.sink", "brutex.data.spot_daily_resample"),
            ("brutex.data.spot_daily_resample", "brutex.data.sink"),
        ):
            proc = subprocess.run(
                [sys.executable, "-c", f"import {first}; import {second}"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr
