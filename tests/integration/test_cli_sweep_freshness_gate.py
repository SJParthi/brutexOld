"""Integration — the sweep-all DERIVED-TF FRESHNESS gate (DEC-SUSPICION-SWEEP-FIXES-001 G1).

``cmd_sweep_all`` pre-loads every ``(symbol, interval)`` slice from the STORED
lake partitions, so a stored derived timeframe that drifted from a fresh
re-derivation of its 1-minute base (a 1m backfill after the last prebuild) would
feed the engine stale bars AND hash them into the run identity as if canonical
(Golden Rule #4). The freshness gate — ``lake_derived_freshness_verdicts`` folded
through ``preflight_derived_freshness`` at the sweep_preflight seam — REFUSES the
launch loudly, naming every stale pair + the exact prebuild remedy. These tests
drive the REAL ``cmd_sweep_all`` over real Parquet lakes: a STALE derived
partition refuses (fails before the fix existed: the sweep launched over the
stale bars); a FRESH one launches; a 1minute-only request abstains (no gate).
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


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential sweep path (ADR-0016 §6) — determinism over the tiny grid."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _bars(n: int = 20) -> list[CandleBar]:
    out: list[CandleBar] = []
    for i in range(n):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        close = Decimal("100.00") + Decimal(i) / 10
        out.append(
            CandleBar(
                timestamp=ts,
                open=close - Decimal("0.05"),
                high=close + Decimal("0.05"),
                low=close - Decimal("0.10"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _seed(tmp_path: Path, *, derived: str) -> tuple[Path, Path]:
    """Seed a lake with 1minute NIFTY bars + a 5minute partition (fresh|stale)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        bars = _bars()
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=bars,
        )
        if derived == "stale":
            # The drift shape: the 5minute partition holds RAW 1-minute bars
            # (wrong grid — what a stray write / pre-anchor-law derive leaves).
            sink.append(
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol="NIFTY",
                interval="5minute",
                bars=bars,
            )
        elif derived == "fresh":
            from brutex.data.spot_daily_resample import frame_to_candle_bars
            from brutex.data.tf_autoderive import read_lake_bars_frame
            from brutex.data.tf_resampler import resample_1m_to_tf

            symbol_dir = lake_root / "bars" / _EXCHANGE / _SEGMENT / "NIFTY"
            df_1m = read_lake_bars_frame(symbol_dir, "1minute")
            sink.append(
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol="NIFTY",
                interval="5minute",
                bars=frame_to_candle_bars(resample_1m_to_tf(df_1m, 5)),
            )
    return lake_root, catalog_path


def _kwargs(lake_root: Path, catalog_path: Path, tmp_path: Path, intervals: str) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "intervals": intervals,
        "start": "2024-01-02",
        "end": "2024-01-03",
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


def test_stale_derived_tf_refuses_launch_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE G1 pin: a stale stored 5minute partition REFUSES the sweep with the
    stale pair named + the prebuild remedy (pre-gate: the sweep silently launched
    over the stale bars and hashed them into the run identity)."""
    lake_root, catalog_path = _seed(tmp_path, derived="stale")
    rc = cmd_sweep_all(symbols="NIFTY", **_kwargs(lake_root, catalog_path, tmp_path, "5minute"))
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "REFUSED" in err
    assert "STALE" in err
    assert "NIFTY/5minute" in err
    assert "prebuild" in err


def test_fresh_derived_tf_launches(tmp_path: Path) -> None:
    """A byte-fresh derived partition passes the gate — the sweep runs to completion (EXIT_OK)."""
    lake_root, catalog_path = _seed(tmp_path, derived="fresh")
    rc = cmd_sweep_all(symbols="NIFTY", **_kwargs(lake_root, catalog_path, tmp_path, "5minute"))
    assert rc == EXIT_OK


def test_fetched_interval_only_request_abstains(tmp_path: Path) -> None:
    """A 1minute-only sweep has nothing to verify — the gate abstains and the
    launch proceeds exactly as before the gate existed (byte-preserved default)."""
    lake_root, catalog_path = _seed(tmp_path, derived="stale")  # stale 5m present but UNREQUESTED
    rc = cmd_sweep_all(symbols="NIFTY", **_kwargs(lake_root, catalog_path, tmp_path, "1minute"))
    assert rc == EXIT_OK


def test_screened_spike_lake_production_prebuild_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILS-BEFORE (DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001, the r10 e2e): a lake
    whose stored 1-minute legitimately holds ONE self-consistent >=4x in-session
    spike — exactly the class the bad-tick screen exists for; every write-side
    guard accepts it — with derived TFs written by the REAL production prebuild
    (which screens its 1-minute read, DEC-READ-GUARD-PARITY-001) previously
    HARD-REFUSED `cmd_sweep_all` with no escape flag, and the printed remedy
    (re-run the prebuild) could NEVER clear it: the freshness gate re-derived
    from the UNSCREENED raw 1-minute. With screen parity the gate compares
    screened-vs-screened and the sweep launches (EXIT_OK)."""
    from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

    monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    bars = _bars()
    # The ~5x self-consistent scaled bar (09:25 IST): internally ordered,
    # strictly positive — stored by the production writer, screened at read.
    spike_close = Decimal("500.00")
    bars[9] = CandleBar(
        timestamp=bars[9].timestamp,
        open=spike_close - Decimal("0.05"),
        high=spike_close + Decimal("0.05"),
        low=spike_close - Decimal("0.10"),
        close=spike_close,
        volume=1000,
        open_interest=None,
    )
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE, segment=_SEGMENT, symbol="NIFTY", interval="1minute", bars=bars
        )
    # THE PRODUCTION PREBUILD (bad-tick screen ON by default) — the exact
    # producer of every real lake's derived TFs.
    materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol="NIFTY",
        kind=PrebuildKind.SPOT,
        tfs=["5m"],
        incremental=False,
    )
    rc = cmd_sweep_all(symbols="NIFTY", **_kwargs(lake_root, catalog_path, tmp_path, "5minute"))
    assert rc == EXIT_OK
