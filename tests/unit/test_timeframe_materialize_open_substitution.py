"""Daily-open substitution wiring in the materialiser (DEC-DAILY-OPEN-SUBSTITUTION-FLAG-001).

Pins the ONE production wiring point of the observe-only scan:
:func:`brutex.data.timeframe_materialize.materialize_lake_timeframes` runs
:func:`~brutex.data.spot_daily_resample.observe_daily_open_substitutions`
over the SAME in-memory 1-minute frame the daily derive just consumed —
SPOT + ``include_daily=True`` ONLY:

* a SPOT target with a 09:15-absent complete session folds
  ``opens_substituted`` into :class:`MaterializeResult` and (``dense_symbol``)
  emits the per-session WARN;
* FUTURES and OPTIONS targets over the SAME-shaped data are NEVER scanned
  (``opens_substituted == 0``, ZERO ``daily_open.*`` events) — the spot-only
  pin: this test FAILS if someone later wires the scan into the F&O
  ``resample_fn`` path without a decision;
* ``include_daily=False`` (the lake-heal recall-rebuild shape —
  ``lake_heal.py`` calls the materialiser with ``include_daily`` defaulted
  False) never scans — no daily bar is derived, so there is no substituted
  open to observe.

Real tmp-path lake through the production sink/builders (mirrors
``tests/integration/test_timeframe_materialize_roundtrip.py``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import (
    PrebuildKind,
    materialize_lake_timeframes,
)
from structlog.testing import capture_logs

#: 09:16 IST == 03:46 UTC — a session whose 09:15 bar is ABSENT, so the
#: derived daily open silently substitutes the 09:16 open.
_SESSION_0916_UTC = datetime(2021, 1, 4, 3, 46, tzinfo=UTC)

#: Forming-session cutoff far after the fixture session (it is COMPLETE).
_BEFORE = date(2021, 6, 1)


def _write_1m(lake_root: Path, *, segment: str, symbol: str, n: int = 10) -> None:
    bars = [
        CandleBar(
            timestamp=_SESSION_0916_UTC + timedelta(minutes=i),
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(n)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange="NSE", segment=segment, symbol=symbol, interval="1minute", bars=bars)


def test_materialize_spot_folds_scan_into_result_and_warns(tmp_path: Path) -> None:
    _write_1m(tmp_path, segment="INDEX", symbol="NIFTY")
    with capture_logs() as logs:
        result = materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            include_daily=True,
            daily_before=_BEFORE,
            incremental=False,
            dense_symbol=True,
        )
    assert result.opens_substituted == 1
    per_date = [e for e in logs if e["event"] == "daily_open.substituted"]
    assert len(per_date) == 1
    assert per_date[0]["symbol"] == "NIFTY"
    assert per_date[0]["first_minute_ist"] == "09:16"


def test_materialize_spot_non_dense_aggregate_only(tmp_path: Path) -> None:
    """dense_symbol=False (a non-index spot) → count folded, aggregate-only logs."""
    _write_1m(tmp_path, segment="CASH", symbol="RELIANCE")
    with capture_logs() as logs:
        result = materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="RELIANCE",
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            include_daily=True,
            daily_before=_BEFORE,
            incremental=False,
        )
    assert result.opens_substituted == 1
    assert [e for e in logs if e["event"] == "daily_open.substituted"] == []
    agg = [e for e in logs if e["event"] == "daily_open.substituted_aggregate"]
    assert len(agg) == 1
    assert agg[0]["log_level"] == "info"


@pytest.mark.parametrize("kind", [PrebuildKind.FUTURES, PrebuildKind.OPTIONS])
def test_materialize_fno_kinds_never_scan(tmp_path: Path, kind: PrebuildKind) -> None:
    """The SPOT-ONLY pin: F&O kinds over the SAME 09:15-absent shape are never
    scanned — a deep-OTM contract's 09:15-absence is expected illiquidity."""
    symbol = "NSE-NIFTY-30Jan25-FUT" if kind is PrebuildKind.FUTURES else "NSE-NIFTY-30Jan25-20000-CE"
    _write_1m(tmp_path, segment="FNO", symbol=symbol)
    with capture_logs() as logs:
        result = materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=symbol,
            kind=kind,
            tfs=("5m",),
            include_daily=True,
            daily_before=_BEFORE,
            incremental=False,
            dense_symbol=True,  # even a (mis)set dense flag must not scan F&O
        )
    assert result.opens_substituted == 0
    assert [e for e in logs if e["event"].startswith("daily_open.")] == []


def test_materialize_include_daily_false_never_scans(tmp_path: Path) -> None:
    """The lake-heal recall-rebuild shape (``include_daily`` defaulted False —
    ``lake_heal.py``'s call site): no daily derive → no scan, no events."""
    _write_1m(tmp_path, segment="INDEX", symbol="NIFTY")
    with capture_logs() as logs:
        result = materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
            incremental=False,
            dense_symbol=True,
        )
    assert result.opens_substituted == 0
    assert [e for e in logs if e["event"].startswith("daily_open.")] == []
