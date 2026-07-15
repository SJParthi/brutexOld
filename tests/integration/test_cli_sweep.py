"""End-to-end proof for ``brutex sweep`` (DEC-SWEEP-CLI-001).

The CLI shell is measured at 100% line+branch (DEC-COV-CLI-HARNESS-001 — no
longer coverage-omitted), and this INTEGRATION test is the honest validation
that the command actually loads real
bars from a Parquet lake, runs the assembled funnel, and prints the ranked report
— not just that the pure core works. It persists a tiny lake with
``ParquetWriter`` / ``ParquetBarSink`` (the same writers ingest uses), then runs
the command and inspects stdout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.cli import cmd_sweep, main
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")


pytestmark = pytest.mark.usefixtures("_inert_sweep_logging_guard")  # see tests/conftest.py


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


def test_sweep_loads_lake_and_prints_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake_root, catalog_path = _persist(tmp_path)
    rc = cmd_sweep(
        lake_root=lake_root,
        catalog_path=catalog_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval=_INTERVAL,
        start="2024-01-02",
        end="2024-01-03",
        direction="BUY",
        k=2,
        conditions="0,1,2",
        stop_loss_paisa=None,
        take_profit_paisa=None,
        top=10,
        min_hits=None,
        sort_by="composite",
        validate_oos=False,
        oos_only=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The report actually rendered from lake-loaded bars (6 of them).
    assert "BruteX sweep — NIFTY 1minute BUY" in out
    assert "bars=6" in out
    assert "combos=3" in out  # C(3, 2)


def test_sweep_argv_path_prints_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Exercise the full argparse + dispatch via main(argv).
    lake_root, catalog_path = _persist(tmp_path)
    rc = main(
        [
            "sweep",
            _SYMBOL,
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-03",
            "--exchange",
            _EXCHANGE,
            "--segment",
            _SEGMENT,
            "--interval",
            _INTERVAL,
            "--k",
            "1",
            "--conditions",
            "5,6",
            "--sort-by",
            "pnl",
            "--oos",
            "--lake-root",
            str(lake_root),
            "--catalog",
            str(catalog_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "BruteX sweep — NIFTY 1minute BUY" in out
    assert "sort=pnl" in out  # the --sort-by flag reaches the report
    assert "Out-of-sample" in out  # the --oos flag reaches the report


def test_sweep_no_bars_is_user_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lake_root, catalog_path = _persist(tmp_path)
    # A window with no data → honest message + non-zero exit, never a crash.
    rc = cmd_sweep(
        lake_root=lake_root,
        catalog_path=catalog_path,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval=_INTERVAL,
        start="2030-01-01",
        end="2030-01-02",
        direction="BUY",
        k=2,
        conditions=None,
        stop_loss_paisa=None,
        take_profit_paisa=None,
        top=20,
        min_hits=None,
        sort_by="composite",
        validate_oos=False,
        oos_only=False,
    )
    assert rc == 1
    assert "no bars for NIFTY" in capsys.readouterr().err


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


def test_sweep_oos_only_filters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Single-day lake → no gate survivors → the filter runs over an empty board.
    rc = _sweep(tmp_path, oos_only=True)
    assert rc == 0
    assert "oos-only=0/0 held" in capsys.readouterr().out


def test_sweep_invalid_k_is_user_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # k > number of conditions → sweep_report raises → clear message + non-zero exit.
    rc = _sweep(tmp_path, conditions="0,1", k=9)
    assert rc == 1
    assert "sweep error" in capsys.readouterr().err


# ── NIFTY-futures series alias through cmd_sweep ─────────────────────────────
# DEC-ENGINE-SPOT-ONLY-001 (supersedes the DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001
# default admit): `brutex sweep NSE-NIFTY-FUT` is REFUSED by default — the
# engine is spot-only. The retained below-the-gate machinery (resolver +
# alias-aware loader + roll-schedule threading) is exercised via the loud,
# audit-stamped --allow-out-of-scope research escape, the ONLY admit path.

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


def test_sweep_futures_alias_refused_by_default_spot_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """DEC-ENGINE-SPOT-ONLY-001: the default alias sweep is a loud user error.

    Even over a REAL futures lake the production entry refuses BEFORE any
    futures-series resolution — citing the DEC + the remediation, never a
    silent skip, never a traceback.
    """
    lake_root, catalog_path = _persist_futures(tmp_path)
    rc = cmd_sweep(
        lake_root=lake_root,
        catalog_path=catalog_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-FUT",
        interval=_INTERVAL,
        start="2025-06-24",
        end="2025-07-01",
        direction="BUY",
        k=2,
        conditions="0,1,2",
        stop_loss_paisa=None,
        take_profit_paisa=None,
        top=10,
        min_hits=None,
        sort_by="composite",
        validate_oos=False,
        oos_only=False,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "sweep REFUSED" in err
    assert "DEC-ENGINE-SPOT-ONLY-001" in err
    assert "spot-only" in err
    assert "lake/REFERENCE_ONLY" in err


def test_sweep_futures_alias_resolves_and_reports_under_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The RETAINED alias machinery stitches BOTH contracts under the override.

    DEC-ENGINE-SPOT-ONLY-001 keeps the resolver + alias-aware loader as
    below-the-gate internals — reachable ONLY via the loud, audit-stamped
    --allow-out-of-scope research escape.
    """
    lake_root, catalog_path = _persist_futures(tmp_path)
    rc = cmd_sweep(
        lake_root=lake_root,
        catalog_path=catalog_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-FUT",
        allow_out_of_scope=True,
        interval=_INTERVAL,
        start="2025-06-24",
        end="2025-07-01",
        direction="BUY",
        k=2,
        conditions="0,1,2",
        stop_loss_paisa=None,
        take_profit_paisa=None,
        top=10,
        min_hits=None,
        sort_by="composite",
        validate_oos=False,
        oos_only=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The research escape is LOUD — the override warning precedes the report.
    assert "--allow-out-of-scope OVERRIDE" in out
    assert "BruteX sweep — NSE-NIFTY-FUT 1minute BUY" in out
    # 3 Jun-contract days + 2 Jul-contract days × 40 bars — the stitched slice.
    assert "bars=200" in out


def test_sweep_futures_alias_resolver_refusal_is_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lake with no NIFTY-futures dirs refuses loudly — never a crash."""
    # _sweep persists only the spot NIFTY lake: no bars/NSE/FNO tree exists,
    # so resolve_futures_series raises FuturesSeriesError → EXIT_USER_ERROR.
    # (allow_out_of_scope: the DEC-ENGINE-SPOT-ONLY-001 gate refuses FIRST
    # otherwise — this test pins the RESOLVER refusal below the gate.)
    rc = _sweep(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-FUT",
        start="2025-06-24",
        end="2025-07-01",
        allow_out_of_scope=True,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "sweep error" in err
    assert "NSE-NIFTY-FUT" in err or "FNO" in err
