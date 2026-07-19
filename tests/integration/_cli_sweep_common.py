"""Shared harness for the ``brutex sweep`` CLI integration suites.

DEC-CLI-SWEEP-SUITE-SPLIT-001: ``test_cli_sweep.py`` was split into two
sibling files of roughly equal measured runtime (``test_cli_sweep.py`` +
``test_cli_sweep_errors_alias.py``) so the CI coverage shards can place
them independently — the one file bounded the slowest shard leg
(DEC-CI-SHARD-REBALANCE-001, run 29644876109). The module-level lake
builders + constants BOTH halves consume are hoisted here VERBATIM (a
pure move, zero behavior change); this module deliberately carries no
``test_`` prefix so pytest never collects it (the §11 COMMON rule — the
``tests/fakes`` helper-module precedent).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from brutex.cli import cmd_sweep
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


def _bars() -> list[CandleBar]:
    """A short in-session NIFTY minute window (stored UTC), valid OHLC."""
    out: list[CandleBar] = []
    for i in range(6):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        close = Decimal("100.00") + Decimal(i) / 10
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _persist(tmp_path: Path) -> tuple[Path, Path]:
    """Write the NIFTY bars to a temp lake; return (lake_root, catalog_path)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            bars=_bars(),
        )
    return lake_root, catalog_path


def _sweep(tmp_path: Path, **over: object) -> int:
    """Persist the lake + run cmd_sweep with sensible defaults, overridable per test."""
    lake_root, catalog_path = _persist(tmp_path)
    kwargs: dict[str, object] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbol": _SYMBOL,
        "interval": _INTERVAL,
        "start": "2024-01-02",
        "end": "2024-01-03",
        "direction": "BUY",
        "k": 2,
        "conditions": "0,1,2",
        "stop_loss_paisa": None,
        "take_profit_paisa": None,
        "top": 10,
        "min_hits": None,
        "sort_by": "composite",
        "validate_oos": False,
        "oos_only": False,
    }
    kwargs.update(over)
    return cmd_sweep(**kwargs)  # type: ignore[arg-type]


_FUT_CONTRACTS: tuple[tuple[str, tuple[tuple[int, int, int], ...], float], ...] = (
    ("NSE-NIFTY-26Jun25-FUT", ((2025, 6, 24), (2025, 6, 25), (2025, 6, 26)), 100.0),
    ("NSE-NIFTY-31Jul25-FUT", ((2025, 6, 27), (2025, 6, 30)), 140.0),
)


def _fut_day_bars(day: tuple[int, int, int], base: float, n: int = 40) -> list[CandleBar]:
    """A drifting volume-bearing intraday session anchored on *day* (IST)."""
    out: list[CandleBar] = []
    price = base
    for m in range(n):
        close = price + 0.6 if m % 3 else price - 0.3
        ts = datetime(*day, 9, 16, tzinfo=_IST) + timedelta(minutes=m)
        out.append(
            CandleBar(
                timestamp=ts.astimezone(UTC),
                open=Decimal(str(round(price, 2))),
                high=Decimal(str(round(max(price, close) + 0.5, 2))),
                low=Decimal(str(round(min(price, close) - 0.5, 2))),
                close=Decimal(str(round(close, 2))),
                volume=100,
                open_interest=None,
            )
        )
        price = close
    return out


def _persist_futures(tmp_path: Path) -> tuple[Path, Path]:
    """Write a 2-contract NIFTY-futures mini-lake THROUGH a catalog."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        for symbol, days, base in _FUT_CONTRACTS:
            bars = [b for day in days for b in _fut_day_bars(day, base)]
            sink.append(exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=bars)
    return lake_root, catalog_path
