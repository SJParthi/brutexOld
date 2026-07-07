"""End-to-end proof of the CLI VIX-stamp binds over a REAL India-VIX lake.

DEC-VIX-STAMP-WIRE-001 round-2 panel (BLOCKER + MAJOR): the production entry
points (``cmd_sweep_all`` / ``cmd_sweep_verify``) are ``# pragma: no cover``
orchestration shims, so their ``reference_stamper`` binding lines shipped with
ZERO bind-asserting execution — deleting a bind kept every test green (the
#935/#1014 dormant-binding trap one level up). This suite drives BOTH commands
for real over a tmp lake carrying an actual ``NSE/INDEX/INDIAVIX`` 1-minute
partition:

* **the positive bind** — ``cmd_sweep_all`` persists finalist rows whose
  ``vix_mean``/``vix_min``/``vix_max`` trio is non-``None``, ordered, and
  inside the planted level band (an omitted bind would persist all-``None``,
  indistinguishable at the store level from a VIX-less lake);
* **the recount BLOCKER** — ``cmd_sweep_verify --recount`` over that stamped
  tree re-sweeps WITH the same stamper build law and verifies CLEAN (before
  the fix the unstamped shadow re-sweep byte-diverged on the trio and
  false-condemned every stamped store as verdict-grade ``recount_divergence``
  corruption);
* **the residual envelope** — a VIX lake that MOVES between the run and the
  recount yields the precise non-corrupt ``recount_vix_divergence`` warning
  (exit 0, never heal-grade), because VIX sits outside the recorded context.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine.results_store import ResultsStore
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"
_START = "2024-01-02"
_END = "2024-02-01"


def _bars() -> list[CandleBar]:
    """A multi-day upward-drifting minute window — BUY combos clear the gates."""
    bars: list[CandleBar] = []
    for day in range(15):
        price = 100.0
        for minute in range(120):
            close = price + 0.6 if minute % 3 else price - 0.3
            ts = (
                datetime(2024, 1, 2, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
            ).astimezone(UTC)
            hi, lo = max(price, close) + 0.5, min(price, close) - 0.5
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=1000,
                    open_interest=None,
                )
            )
            price = close
    return bars


_INDEX_BARS = _bars()


def _plant_vix_partition(lake_root: Path, base: float) -> None:
    """RAW-plant the NSE/INDEX/INDIAVIX 1-minute month partition (2024-01).

    Levels are ``base + (i % 7)/100`` at EVERY index-bar instant, so the
    persisted trio is checkable against the planted band. Raw pyarrow (not
    the sink) so a later test can REWRITE the partition to model the
    sanctioned VIX-lake movement (the sink's keep-first merge would keep the
    old levels).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    from brutex.data.partition import build_partition_path

    dest = build_partition_path(
        lake_root=lake_root,
        exchange="NSE",
        segment="INDEX",
        symbol="INDIAVIX",
        interval="1minute",
        year=2024,
        month=1,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [base + (i % 7) / 100 for i in range(len(_INDEX_BARS))]
    table = pa.table(
        {
            "timestamp": pa.array(
                [bar.timestamp for bar in _INDEX_BARS], type=pa.timestamp("us", tz="UTC")
            ),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([0] * len(prices), type=pa.int64()),
            "open_interest": pa.array([None] * len(prices), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _persist(tmp_path: Path) -> tuple[Path, Path]:
    """Write the NIFTY bars + the VIX partition; return (lake_root, catalog)."""
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
            bars=_INDEX_BARS,
        )
    _plant_vix_partition(lake_root, base=14.0)
    return lake_root, catalog_path


def _sweep_all(lake_root: Path, catalog_path: Path, results_root: Path) -> int:
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": _START,
        "end": _END,
        "directions": "BUY",
        "k": "1",
        "conditions": "0,1,2",
        "sl": "80",
        "tp": "50",
        "top": 10,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }
    return cli.cmd_sweep_all(**kwargs)


def _sweep_verify_recount(
    lake_root: Path, catalog_path: Path, results_root: Path, scratch_root: Path
) -> int:
    return cli.cmd_sweep_verify(
        lake_root=lake_root,
        catalog_path=catalog_path,
        results_root=results_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbols=_SYMBOL,
        intervals=_INTERVAL,
        start=_START,
        end=_END,
        directions="BUY",
        k="1",
        conditions="0,1,2",
        sl="80",
        tp="50",
        top=10,
        mode="A",
        max_tuples=None,
        max_combos=None,
        data_snapshot_hash=None,
        indicator_version=None,
        code_commit_sha=None,
        recount=True,
        scratch_root=scratch_root,
        allow_context_mismatch=False,
    )


def _stamped_rows(results_root: Path) -> list[Any]:
    rows: list[Any] = []
    for store_path in sorted(results_root.rglob("*.parquet")):
        rows.extend(ResultsStore.load(store_path).rows())
    return rows


def test_cmd_sweep_all_persists_non_none_ordered_vix_trios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive-bind pin: the launch stamper genuinely reaches the rows.

    Also pins the once-per-launch lake-read law DYNAMICALLY (round-3 panel):
    the REAL ``cmd_sweep_all`` launch emits EXACTLY ONE
    ``reference_wire.vix_stamper_built`` — the net the static builder-call-count
    AST pin cannot provide against a build drifting inside a per-tuple loop
    (round-1 CONFIRMED the VIX lake read TWICE per launch; nothing counted
    builds until now).
    """
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    with capture_logs() as logs:
        rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    built = [e for e in logs if e["event"] == "reference_wire.vix_stamper_built"]
    assert len(built) == 1, f"expected exactly ONE stamper build per launch, saw {len(built)}"
    rows = _stamped_rows(results_root)
    assert rows, "the sweep must persist finalists for the trio pin to bite"
    for row in rows:
        # Deleting any reference_stamper= bind in cmd_sweep_all turns these
        # into all-None — the exact silent re-dormanting this test guards.
        assert row.vix_mean is not None
        assert row.vix_min is not None
        assert row.vix_max is not None
        assert row.vix_min <= row.vix_mean <= row.vix_max
        assert 14.0 <= row.vix_min <= row.vix_max <= 14.06  # the planted band


def test_recount_over_a_stamped_tree_verifies_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE BLOCKER regression: a stamped tree recounts CLEAN on the same lake."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    assert _sweep_all(lake_root, catalog_path, results_root) == 0
    assert _stamped_rows(results_root)[0].vix_mean is not None  # a stamped tree

    rc = _sweep_verify_recount(lake_root, catalog_path, results_root, tmp_path / "scratch")

    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    # Before the fix the unstamped shadow re-sweep condemned every store.
    assert "recount_divergence" not in out
    assert "recount_vix_divergence" not in out  # same lake — nothing moved
    assert "CORRUPT" not in out


def test_recount_after_a_vix_lake_move_warns_precisely_never_condemns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The residual envelope: a moved VIX lake is a warn, never heal-grade."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    assert _sweep_all(lake_root, catalog_path, results_root) == 0

    # The sanctioned lake movement: the VIX series is re-pulled at new levels
    # AFTER the sweep ran (VIX sits outside the recorded context, so the
    # recount context still fully matches).
    _plant_vix_partition(lake_root, base=20.0)

    rc = _sweep_verify_recount(lake_root, catalog_path, results_root, tmp_path / "scratch")

    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK  # warn-grade — the tree is NOT condemned
    assert "recount_vix_divergence" in out
    assert "India-VIX lake moved" in out
    assert "CORRUPT" not in out
