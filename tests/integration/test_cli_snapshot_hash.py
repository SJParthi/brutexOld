"""End-to-end money tests — the full-content snapshot identity (DEC-SNAPSHOT-HASH-FULL-001).

The production consequence of the v1→v2 upgrade, proven through the REAL
``cmd_sweep_all`` / ``sweep-verify`` paths over a REAL Parquet lake:

* an INTERIOR-bar edit in a month partition (the re-pull scenario the v1
  sampled fingerprint was structurally blind to) now re-keys the launch
  identity, so the second sweep is NOT dedup/resume-skipped and persists to a
  NEW tuple store path — stale results can no longer be served as fresh
  (Golden Rules #4/#7);
* ``sweep-verify --recount`` over the edited lake reads an honest snapshot
  MISMATCH → recount REFUSED (the ladder itself unchanged);
* a LEGACY tree whose recorded hash is the retired v1 derivation gets the new
  named diagnostic ("LEGACY sampled derivation … re-sweep to mint a v2
  identity") instead of a bare mismatch.

``cmd_sweep_all`` / ``cmd_sweep_verify`` are ``# pragma: no cover``
orchestration shims, so this suite is their honest functional validation —
mirrors ``test_cli_sweep_all_dedup.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.cli import EXIT_OK, cmd_sweep_all, main
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine.stage3_wire import _data_snapshot_hash_v1

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

import polars as pl

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"
_START = "2024-01-02"
_END = "2024-01-03"


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential path EXPLICITLY (ADR-0016 §6).

    This suite's determinism previously rode the small-grid fallback only
    incidentally (its grids are 1-tuple); the explicit ``=1`` opt-out makes the
    sequential requirement survive any future grid widening.
    """
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _bars() -> list[CandleBar]:
    out: list[CandleBar] = []
    for i in range(8):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=Decimal("100.00") + Decimal(i) / 10,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _persist(tmp_path: Path) -> tuple[Path, Path]:
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


def _edit_interior_bar(lake_root: Path) -> None:
    """Rewrite ONE interior bar's close in the month partition, count + endpoints kept."""
    partition = (
        lake_root / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / _INTERVAL / "2024" / "01.parquet"
    )
    frame = pl.read_parquet(partition)
    closes = frame["close"].to_list()
    closes[3] = 100.55  # interior row only — first/last closes + row count unchanged
    frame = frame.with_columns(pl.Series("close", closes, dtype=frame["close"].dtype))
    frame.write_parquet(partition)


def _sweep_all_kwargs(lake_root: Path, catalog_path: Path, tmp_path: Path) -> dict[str, Any]:
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": tmp_path / "sweeps",
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": _START,
        "end": _END,
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


def _tuple_stores(results_root: Path) -> list[Path]:
    """The per-tuple Parquet stores (excludes audit/checkpoint/progress dirs)."""
    return sorted((results_root / _SYMBOL).rglob("*.parquet"))


def test_interior_lake_edit_re_keys_the_sweep_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE money regression: an interior re-pull edit must NOT be dedup-skipped."""
    lake_root, catalog_path = _persist(tmp_path)
    kwargs = _sweep_all_kwargs(lake_root, catalog_path, tmp_path)

    assert cmd_sweep_all(**kwargs) == EXIT_OK
    first_stores = _tuple_stores(tmp_path / "sweeps")
    assert len(first_stores) == 1  # one tuple → one store

    _edit_interior_bar(lake_root)

    assert cmd_sweep_all(**kwargs) == EXIT_OK
    second_stores = _tuple_stores(tmp_path / "sweeps")
    # The edited lake mints a NEW snapshot identity → a NEW tuple digest →
    # a NEW store path alongside the old one (old + new coexist, never
    # clobbered) — under v1 the second run resume/dedup-skipped and the stale
    # first store kept masquerading as current.
    assert len(second_stores) == 2
    assert first_stores[0] in second_stores
    out = capsys.readouterr().out
    assert out.count("sweep-all done") == 2


def test_recount_on_interior_edited_lake_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake_root, catalog_path = _persist(tmp_path)
    assert cmd_sweep_all(**_sweep_all_kwargs(lake_root, catalog_path, tmp_path)) == EXIT_OK
    _edit_interior_bar(lake_root)
    capsys.readouterr()

    main(
        [
            "sweep-verify",
            "--symbols",
            _SYMBOL,
            "--intervals",
            _INTERVAL,
            "--start",
            _START,
            "--end",
            _END,
            "--directions",
            "BUY",
            "--k",
            "1",
            "--conditions",
            "0,1",
            "--top",
            "5",
            "--lake-root",
            str(lake_root),
            "--catalog",
            str(catalog_path),
            "--results-root",
            str(tmp_path / "sweeps"),
            "--recount",
        ]
    )
    out = capsys.readouterr().out
    assert "data_snapshot_hash: recorded" in out
    assert "recount REFUSED" in out
    # The edited lake is NOT a legacy v1 tree — the diagnostic must not fire.
    assert "LEGACY sampled derivation" not in out


def test_recount_names_a_legacy_v1_recorded_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-upgrade tree (recorded hash == the v1 derivation) is named as such."""
    lake_root, catalog_path = _persist(tmp_path)
    # Simulate the pre-DEC-SNAPSHOT-HASH-FULL-001 tree via the verbatim
    # --data-snapshot-hash override: stamp the launch with the v1 hash of the
    # exact slices the recount will re-load (loaded through the SAME
    # Catalog/ReplayFeed path, so the Decimal rendering round-trips exactly).
    from brutex.feed.replay import ReplayFeed

    with Catalog.open(catalog_path) as cat:
        feed = ReplayFeed(
            catalog=cat,
            lake_root=lake_root,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbols=[_SYMBOL],
            interval=_INTERVAL,
            start=datetime(2024, 1, 2, tzinfo=UTC),
            end=datetime(2024, 1, 3, tzinfo=UTC),
        )
        loaded = [event.bar for event in feed]
    v1_hash = _data_snapshot_hash_v1(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        start=_START,
        end=_END,
        slices={(_SYMBOL, _INTERVAL): loaded},
    )
    kwargs = _sweep_all_kwargs(lake_root, catalog_path, tmp_path)
    kwargs["data_snapshot_hash"] = v1_hash
    assert cmd_sweep_all(**kwargs) == EXIT_OK
    capsys.readouterr()

    main(
        [
            "sweep-verify",
            "--symbols",
            _SYMBOL,
            "--intervals",
            _INTERVAL,
            "--start",
            _START,
            "--end",
            _END,
            "--directions",
            "BUY",
            "--k",
            "1",
            "--conditions",
            "0,1",
            "--top",
            "5",
            "--lake-root",
            str(lake_root),
            "--catalog",
            str(catalog_path),
            "--results-root",
            str(tmp_path / "sweeps"),
            "--recount",
        ]
    )
    out = capsys.readouterr().out
    assert "LEGACY sampled derivation" in out
    assert "re-sweep to mint a v2 identity" in out
    assert "recount REFUSED" in out  # the refusal ladder itself is UNCHANGED
