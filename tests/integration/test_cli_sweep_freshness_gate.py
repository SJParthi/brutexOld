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

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
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


_DAY1 = date(2024, 1, 2)
_DAY2 = date(2024, 1, 3)


def _bars(n: int = 20, *, day: date = _DAY1) -> list[CandleBar]:
    out: list[CandleBar] = []
    for i in range(n):
        ts = datetime(day.year, day.month, day.day, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
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


def _seed_daily(tmp_path: Path, *, shape: str) -> tuple[Path, Path]:
    """Seed a lake with 1minute bars + a derived ``1day`` partition, in one shape:

    * ``fresh`` — 1m for BOTH days, ``1day`` derived over both via the canonical
      midnight-IST law + the real writer (a healthy post-prebuild lake);
    * ``crash-grown`` — the BND-5b kill-9 / CLI-fetch-no-prebuild shape: ``1day``
      derived through DAY1, then a 1m fetch adds DAY2 with NO re-derive;
    * ``volume-drift`` — the stored DAY1 ``1day`` bar carries a drifted volume
      vs the canonical re-derive (a torn incremental re-derive).
    """
    from brutex.data.spot_daily_resample import resample_1m_to_daily
    from brutex.data.tf_autoderive import read_lake_bars_frame

    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    symbol_dir = lake_root / "bars" / _EXCHANGE / _SEGMENT / "NIFTY"
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval="1minute",
            bars=_bars(day=_DAY1),
        )
        if shape == "fresh":
            sink.append(
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol="NIFTY",
                interval="1minute",
                bars=_bars(day=_DAY2),
            )
            daily = resample_1m_to_daily(
                read_lake_bars_frame(symbol_dir, "1minute"), before=_DAY2 + timedelta(days=1)
            )
        elif shape == "crash-grown":
            daily = resample_1m_to_daily(read_lake_bars_frame(symbol_dir, "1minute"), before=_DAY2)
        else:  # volume-drift
            derived = resample_1m_to_daily(
                read_lake_bars_frame(symbol_dir, "1minute"), before=_DAY2
            )
            daily = [replace(bar, volume=bar.volume + 1) for bar in derived]
        sink.append(
            exchange=_EXCHANGE, segment=_SEGMENT, symbol="NIFTY", interval="1day", bars=daily
        )
        if shape == "crash-grown":
            # The post-derive growth: the crash / missing prebuild left the
            # stored `1day` provably behind its own 1-minute base.
            sink.append(
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbol="NIFTY",
                interval="1minute",
                bars=_bars(day=_DAY2),
            )
    return lake_root, catalog_path


def _kwargs(
    lake_root: Path,
    catalog_path: Path,
    tmp_path: Path,
    intervals: str,
    *,
    start: str = "2024-01-02",
    end: str = "2024-01-03",
) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "intervals": intervals,
        "start": start,
        "end": end,
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


# ── the 1day arm (DEC-MOVERS-STORED-FRESHNESS-001 fold-in — the BND-5b closure) ──


def test_stale_1day_post_crash_refuses_launch_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE BND-5b pin: a kill-9 mid-incremental-re-derive (or a CLI 1m fetch of a
    new day with no prebuild) leaves the stored ``1day`` durably behind its own
    1-minute base — the ``1minute,1day`` sweep now REFUSES loudly naming
    ``NIFTY/1day`` + the prebuild remedy, through the PRODUCTION default cutoff
    path (no injected ``daily_before``). Pre-fold-in the 1day arm ABSTAINED and
    the sweep launched ungated, hashing the stale daily bar into run identity."""
    lake_root, catalog_path = _seed_daily(tmp_path, shape="crash-grown")
    rc = cmd_sweep_all(
        symbols="NIFTY",
        **_kwargs(
            lake_root, catalog_path, tmp_path, "1minute,1day", start="2024-01-01", end="2024-01-04"
        ),
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "REFUSED" in err
    assert "STALE" in err
    assert "NIFTY/1day" in err
    assert "prebuild" in err


def test_fresh_1day_launches(tmp_path: Path) -> None:
    """A canonically-derived ``1day`` passes the gate through the production
    default cutoff path — no false refusal on a healthy post-prebuild lake."""
    lake_root, catalog_path = _seed_daily(tmp_path, shape="fresh")
    rc = cmd_sweep_all(
        symbols="NIFTY",
        **_kwargs(
            lake_root, catalog_path, tmp_path, "1minute,1day", start="2024-01-01", end="2024-01-04"
        ),
    )
    assert rc == EXIT_OK


def test_stale_1day_sweep_refuses_while_movers_falls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The coordinator's combined regression, ONE fixture (a drifted stored
    ``1day`` bar): (a) the sweep REFUSES loudly naming ``NIFTY/1day``; (b) the
    SAME lake through the movers daily read serves the canonical re-derived bar
    (``resampled_reads``, one capped ``movers.stored_stale_fallback``) — the
    sweep refuses, the movers screen falls back; neither consumes the stale bar."""
    from brutex.dashboard.movers_snapshot import compute_spot_candidates
    from structlog.testing import capture_logs

    lake_root, catalog_path = _seed_daily(tmp_path, shape="volume-drift")
    rc = cmd_sweep_all(
        symbols="NIFTY",
        **_kwargs(
            lake_root, catalog_path, tmp_path, "1minute,1day", start="2024-01-01", end="2024-01-04"
        ),
    )
    err = capsys.readouterr().err
    assert rc == EXIT_USER_ERROR
    assert "NIFTY/1day" in err

    with capture_logs() as logs:
        cands = compute_spot_candidates(
            lake_root=lake_root, interval="1day", date=_DAY1.isoformat(), time=None
        )
    assert len(cands.snapshots) == 1
    assert cands.resampled_reads == 1
    assert cands.stored_reads == 0
    # The served bar is the 1-minute truth (20 bars × 1000), not the drifted 20001.
    assert cands.snapshots[0].volume == 20_000
    warns = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
    assert len(warns) == 1
    assert warns[0]["interval"] == "1day"
