"""Prebuild wiring of the >9% daily-range detector (DEC-HALT-DAY-TREATMENT-001).

End-to-end over a REAL tmp lake through the production
``materialize_lake_timeframes`` glue: the detector fires at the SPOT
daily-derive seam for a DENSE (index) symbol only, files a known annotated
halt day INFO vs an unknown extreme day WARN, and the derived/stored lake
bytes are PROVEN identical detector-on vs detector-monkeypatched-out
(observe-only by construction).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.data import timeframe_materialize as tm_mod
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_daily_resample import (
    DAILY_RANGE_EVENT,
    DAILY_RANGE_KNOWN_EVENT,
)
from brutex.data.timeframe_materialize import (
    MaterializeResult,
    PrebuildKind,
    materialize_lake_timeframes,
)
from structlog.testing import capture_logs

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"


def _session_bars(d: date, *, low: str, high: str) -> list[CandleBar]:
    """A complete 375-bar session on ``d`` whose day range is [low, high]."""
    open_utc = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)  # 09:15 IST
    bars: list[CandleBar] = []
    for i in range(375):
        # The first bar prints the extremes; the rest are flat mid-prints, so
        # the day's (max(high), min(low)) is exactly (high, low).
        hi = Decimal(high) if i == 0 else Decimal(low) + 1
        lo = Decimal(low) if i == 0 else Decimal(low) + 1
        mid = Decimal(low) + 1
        bars.append(
            CandleBar(
                timestamp=open_utc + timedelta(minutes=i),
                open=mid,
                high=hi,
                low=lo,
                close=mid,
                volume=10,
                open_interest=0,
            )
        )
    return bars


def _write_1m(lake_root: Path, bars: list[CandleBar]) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars)


def _derive(lake_root: Path, *, dense_symbol: bool = True) -> MaterializeResult:
    return materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m",),
        include_daily=True,
        daily_before=date(2026, 12, 31),
        dense_symbol=dense_symbol,
        incremental=False,
    )


def _tree_digest(lake_root: Path) -> dict[str, str]:
    """sha256 of every parquet under the lake, keyed by relative path."""
    return {
        str(p.relative_to(lake_root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(lake_root.rglob("*.parquet"))
    }


class TestDailyRangePrebuildWiring:
    def test_known_halt_day_fires_info_and_unknown_day_warns(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        # 2020-03-23 (annotated halt, ~13% range) + a fabricated extreme
        # 2024-01-03 (9.5%, un-annotated) + a calm 2024-01-04 (1%).
        _write_1m(lake, _session_bars(date(2020, 3, 23), low="87", high="100"))
        _write_1m(lake, _session_bars(date(2024, 1, 3), low="100", high="109.5"))
        _write_1m(lake, _session_bars(date(2024, 1, 4), low="100", high="101"))
        with capture_logs() as logs:
            _derive(lake)
        known = [e for e in logs if e["event"] == DAILY_RANGE_KNOWN_EVENT]
        assert [e["session_date"] for e in known] == ["2020-03-23"]
        assert known[0]["log_level"] == "info"
        assert known[0]["reason"] == "market_wide_circuit_halt"
        warns = [e for e in logs if e["event"] == DAILY_RANGE_EVENT]
        assert [e["session_date"] for e in warns] == ["2024-01-03"]

    def test_non_dense_symbol_is_never_scanned(self, tmp_path: Path) -> None:
        # INDEX-only by decree: a non-dense (stock-shaped) symbol skips the
        # detector entirely — breakers are an index phenomenon.
        lake = tmp_path / "lake"
        _write_1m(lake, _session_bars(date(2024, 1, 3), low="100", high="120"))
        with capture_logs() as logs:
            _derive(lake, dense_symbol=False)
        assert [e for e in logs if e["event"] == DAILY_RANGE_EVENT] == []
        assert [e for e in logs if e["event"] == DAILY_RANGE_KNOWN_EVENT] == []

    def test_derived_bytes_identical_detector_on_vs_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OBSERVE-ONLY proof: two fresh identical lakes, one derived with the
        # real detector and one with it monkeypatched out — every parquet byte
        # (1-minute + derived 5m + derived 1day) is identical.
        bars = _session_bars(date(2024, 1, 3), low="100", high="112")
        lake_on = tmp_path / "on"
        _write_1m(lake_on, bars)
        _derive(lake_on)
        lake_off = tmp_path / "off"
        _write_1m(lake_off, bars)
        monkeypatch.setattr(
            "brutex.data.spot_daily_resample.observe_extreme_daily_ranges",
            lambda *args, **kwargs: 0,
        )
        _derive(lake_off)
        on_digest = _tree_digest(lake_on)
        off_digest = _tree_digest(lake_off)
        assert on_digest == off_digest
        assert len(on_digest) >= 3  # 1minute + 5m + 1day partitions really exist

    def test_wiring_calls_the_guarded_observer(self, tmp_path: Path) -> None:
        # The seam imports the module lazily — pin the call-site name so a
        # silently-severed wire fails here, not only via log absence.
        import inspect

        src = inspect.getsource(tm_mod)
        assert "observe_extreme_daily_ranges(" in src
        assert "if dense_symbol:" in src
