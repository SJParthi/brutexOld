"""Integration — the sweep-all CORRUPT-PARTITION launch refusal.

DEC-SWEEP-RECONCILE-CORRUPT-REFUSE-001 (MIGRATION.md #1635). ``cmd_sweep_all``
reconciles the DuckDB catalog from the on-disk Parquet lake BEFORE the ReplayFeed
preload. A CORRUPT / version-incompatible month partition the reconcile SKIPS
(GAP #9) is an INTERIOR data hole invisible in the swept output — Stage-1A rolls
``_prev_day_ohlc`` from whatever bar precedes the next session, so day N's "prev
day" silently becomes day N-k, poisoning the PDH/PDL/pivot/Fib anchors (bits
6-29+48-51) while ``data_snapshot_hash`` stamps the holed series as canonical
(Golden Rule #4). The launch now REFUSES loudly, naming the corrupt file(s) + the
remedy, with NO escape flag (this is the freshness-gate class; absent != corrupt).

These tests drive the REAL ``cmd_sweep_all`` over real Parquet lakes:
* a corrupt Feb month refuses the launch (fails-before: the sweep launched over
  the silently-holed data and exited EXIT_OK with no stderr mention of the hole);
* benign skips (a zero-row partition + an unparseable stray file) do NOT refuse —
  the sweep proceeds (the false-refuse twin);
* the corrupt-file list caps at 10 shown + "(+N more)";
* a clean lake is byte-neutral (no corrupt/REFUSED mention on stderr).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

from brutex.cli import EXIT_OK, EXIT_USER_ERROR, cmd_sweep_all
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential sweep path (ADR-0016 §6) — determinism over the tiny grid."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _bars() -> list[CandleBar]:
    """Trending bars spanning Jan 2-5 AND Feb 1-2 2024 (two month partitions)."""
    out: list[CandleBar] = []
    for day in (
        datetime(2024, 1, 2, tzinfo=_IST),
        datetime(2024, 1, 3, tzinfo=_IST),
        datetime(2024, 1, 4, tzinfo=_IST),
        datetime(2024, 1, 5, tzinfo=_IST),
        datetime(2024, 2, 1, tzinfo=_IST),
        datetime(2024, 2, 2, tzinfo=_IST),
    ):
        price = 100.0
        for m in range(40):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = day.replace(hour=9, minute=16) + timedelta(minutes=m)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts.astimezone(UTC),
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = close
    return out


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """Write a real 2-month NIFTY 1-minute lake; return (lake_root, catalog_path)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        ParquetBarSink(writer=writer, catalog=cat).append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            bars=_bars(),
        )
    # Delete the catalog so the sweep REBUILDS it from the on-disk Parquet truth —
    # the real production path (a CLI-`fetch` lake historically wrote catalog=None,
    # so a later corrupt month is invisible to `get_bars` and the reconcile is the
    # ONLY registration path). This makes the pre-fix behaviour a SILENT green (the
    # corrupt month is skipped + never loaded), which the launch refusal converts
    # into a loud EXIT_USER_ERROR.
    catalog_path.unlink()
    return lake_root, catalog_path


def _partition(lake_root: Path, *, year: int, month: int) -> Path:
    return (
        lake_root
        / "bars"
        / _EXCHANGE
        / _SEGMENT
        / _SYMBOL
        / _INTERVAL
        / str(year)
        / f"{month:02d}.parquet"
    )


def _kwargs(lake_root: Path, catalog_path: Path, tmp_path: Path) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": "2024-01-02",
        "end": "2024-02-29",
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1",
        "sl": "none",
        "tp": "none",
        "top": 5,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }


def test_corrupt_month_refuses_launch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """THE #1635 pin: a corrupt Feb partition REFUSES the sweep loudly + logs the
    ERROR event (pre-gate: the sweep launched over the silently-holed data)."""
    from structlog.testing import capture_logs

    lake_root, catalog_path = _seed(tmp_path)
    corrupt = _partition(lake_root, year=2024, month=2)
    corrupt.write_bytes(b"not a parquet file at all")
    with capture_logs() as logs:
        rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path))
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "REFUSED" in err
    assert "corrupt" in err
    assert "02.parquet" in err  # the Feb file is named
    events = [e for e in logs if e["event"] == "sweep_launch.corrupt_partitions_refused"]
    assert len(events) == 1
    assert any("02.parquet" in f for f in events[0]["files"])


def test_benign_skips_do_not_refuse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The false-refuse twin: a zero-row partition + an unparseable-path stray file
    are DATA ABSENCE, not data LOSS — the sweep proceeds to completion (EXIT_OK)."""
    import polars as pl

    lake_root, catalog_path = _seed(tmp_path)
    # (a) a zero-row partition inside the requested coordinates (benign skip).
    empty = _partition(lake_root, year=2024, month=3)
    empty.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"timestamp": pl.Series([], dtype=pl.Datetime("us", "UTC"))}).write_parquet(empty)
    # (b) an unparseable-path stray file (month segment is not an int) → benign skip
    #     BEFORE any read (its garbage bytes are never opened).
    stray = (
        lake_root
        / "bars"
        / _EXCHANGE
        / _SEGMENT
        / _SYMBOL
        / _INTERVAL
        / "2024"
        / "notamonth.parquet"
    )
    stray.write_bytes(b"garbage that is never read")
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path))
    err = capsys.readouterr().err
    assert rc == EXIT_OK
    assert "REFUSED" not in err
    assert "corrupt" not in err


def test_vanished_file_does_not_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1635 review round 1 M1: a partition file that VANISHES between the
    reconcile's glob listing and its read (an ``OSError`` — ENOENT from a
    concurrent heal/fetch, an NFS blip) is data ABSENCE, not corruption — the
    launch PROCEEDS (EXIT_OK, no refusal, "absent != corrupt").

    Fails-before: the combined ``parquet_read_skip_errors()`` arm recorded the
    vanished file on ``unreadable`` and the escape-less refusal aborted the
    launch with EXIT_USER_ERROR on a transient FS glitch.
    """
    import errno
    from types import SimpleNamespace

    import brutex.data.catalog_reconcile as cr
    import polars as pl

    lake_root, catalog_path = _seed(tmp_path)
    # An EXTRA March partition (real Parquet, outside the swept Jan-Feb bars)
    # that vanishes exactly at the reconcile's read.
    extra = _partition(lake_root, year=2024, month=3)
    extra.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "timestamp": pl.Series(
                [datetime(2024, 3, 1, 9, 16, tzinfo=UTC)], dtype=pl.Datetime("us", "UTC")
            )
        }
    ).write_parquet(extra)
    real_read = pl.read_parquet

    def _vanishing_read(path: object, *args: object, **kwargs: object) -> object:
        if Path(str(path)) == extra:
            extra.unlink(missing_ok=True)  # the file genuinely vanishes
            raise FileNotFoundError(errno.ENOENT, "vanished mid-reconcile", str(extra))
        return real_read(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        cr, "pl", SimpleNamespace(read_parquet=_vanishing_read, exceptions=pl.exceptions)
    )
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path))
    err = capsys.readouterr().err
    assert rc == EXIT_OK
    assert "REFUSED" not in err
    assert "corrupt" not in err


def test_corrupt_list_caps_at_ten_shown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """12 corrupt partitions → 10 named + a "(+2 more)" suffix (the log-volume cap)."""
    lake_root, catalog_path = _seed(tmp_path)
    for month in range(1, 13):  # 12 corrupt months, all garbage
        part = _partition(lake_root, year=2024, month=month)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"corrupt")
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path))
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "REFUSED" in err
    assert "(+2 more)" in err


def test_clean_lake_is_byte_neutral(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A fully clean lake never mentions corruption/refusal and runs to completion."""
    lake_root, catalog_path = _seed(tmp_path)
    rc = cmd_sweep_all(**_kwargs(lake_root, catalog_path, tmp_path))
    err = capsys.readouterr().err
    assert rc == EXIT_OK
    assert "REFUSED" not in err
    assert "corrupt" not in err
