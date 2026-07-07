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


def _long_bars() -> list[CandleBar]:
    """A window LONGER than the 182-day lockbox — the stage-3 train slice is real.

    ``run_stage3_pass`` refines over the lockbox-EXCLUDED train slice
    (ADR-0007 condition 7): a window spanning no more than
    ``LOCKBOX_DEFAULT_WINDOW_DAYS`` yields an EMPTY train slice and the pass
    honestly skips every cell (``skipped_empty_train``) — so the
    cells-conflicted relaunch test needs this longer fixture. 30 bars/day
    keeps the two-launch runtime bounded.
    """
    from brutex.metrics.lockbox import LOCKBOX_DEFAULT_WINDOW_DAYS

    bars: list[CandleBar] = []
    for day in range(LOCKBOX_DEFAULT_WINDOW_DAYS + 8):
        price = 100.0
        for minute in range(30):
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


def _plant_vix_months(lake_root: Path, bars: list[CandleBar], base: float) -> None:
    """RAW-plant NSE/INDEX/INDIAVIX month partitions covering EVERY bar instant.

    The multi-month twin of :func:`_plant_vix_partition` (same level law
    ``base + (i % 7)/100``, same raw-pyarrow overwrite semantics) for the
    long stage-3 fixture, whose window spans several UTC months.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    from brutex.data.partition import build_partition_path

    by_month: dict[tuple[int, int], list[tuple[Any, float]]] = {}
    for i, bar in enumerate(bars):
        key = (bar.timestamp.year, bar.timestamp.month)
        by_month.setdefault(key, []).append((bar.timestamp, base + (i % 7) / 100))
    for (year, month), rows in by_month.items():
        dest = build_partition_path(
            lake_root=lake_root,
            exchange="NSE",
            segment="INDEX",
            symbol="INDIAVIX",
            interval="1minute",
            year=year,
            month=month,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        prices = [price for _, price in rows]
        table = pa.table(
            {
                "timestamp": pa.array([ts for ts, _ in rows], type=pa.timestamp("us", tz="UTC")),
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


def _sweep_all(lake_root: Path, catalog_path: Path, results_root: Path, **extra: Any) -> int:
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
        **extra,
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


def test_cmd_sweep_all_relaunch_after_vix_move_warns_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cells_conflicted CLI-verdict fold executes for REAL (round-4 panel).

    The engine-level cell skip (``TestVixConflictCellSkip``) and the pure
    ``sweep_all_exit_code`` mapping are unit-tested, but their COMPOSITION in
    the ``# pragma: no cover`` ``cmd_sweep_all`` shim — the ``cells_conflicted=``
    summary field, the stderr WARNING block, and folding ``cells_conflicted``
    into the exit verdict — shipped with zero executing test (the exact
    honesty-surface-riding-a-pragma class the ETC precedent lifted). This
    drives the REAL command twice around a VIX-lake move:

    launch 1 (stage-3 enabled) persists stamped refined cells → the VIX
    partition is re-planted at new levels → launch 2's stage-3 re-derivation
    conflicts on every refined cell's trio → the relaunch must exit NON-zero
    (``EXIT_SWEEP_TUPLE_FAILED``), print the operator WARNING naming the
    quarantine remediation, and leave every launch-1 store byte untouched.
    A regression to ``sweep_all_exit_code(result.tuples_failed)`` alone — or
    a dropped WARNING block — fails here and nowhere else.
    """
    import hashlib
    import re
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # cma emits a matplotlib UserWarning at import
        pytest.importorskip("cma")

    # The LONG fixture: the stage-3 train slice must be non-empty (the 15-day
    # default window sits entirely inside the 182-day lockbox → the pass
    # would honestly refine NOTHING and this test would be vacuous).
    long_bars = _long_bars()
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
            bars=long_bars,
        )
    _plant_vix_months(lake_root, long_bars, base=14.0)
    results_root = tmp_path / "sweeps"
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    window = {"start": "2024-01-02", "end": "2024-08-01"}
    stage3_kwargs: dict[str, Any] = {
        "stage3_enable": True,
        "stage3_top_k": 1,
        "stage3_fevals": 4,
        "stage3_restarts": 1,
        **window,
    }
    assert _sweep_all(lake_root, catalog_path, results_root, **stage3_kwargs) == 0
    out_1 = capsys.readouterr().out  # the pins below are launch-2's
    refined_1 = re.search(r"masks_refined=(\d+)", out_1)
    assert refined_1 is not None and int(refined_1.group(1)) > 0, (
        "launch 1 must genuinely refine stage-3 cells or the conflict pin is vacuous"
    )

    def _tree_hashes() -> dict[Path, str]:
        return {
            p: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(results_root.rglob("*.parquet"))
        }

    before = _tree_hashes()
    assert before, "launch 1 must persist stores for the byte pin to bite"

    # The sanctioned VIX-lake movement between launches: launch 2's stage-3
    # re-derivation re-produces the SAME refined run_ids with a DIFFERENT
    # REFERENCE-ONLY trio → VixTrioConflictError → per-cell skip-and-count.
    _plant_vix_months(lake_root, long_bars, base=20.0)

    rc = _sweep_all(lake_root, catalog_path, results_root, **stage3_kwargs)

    captured = capsys.readouterr()
    assert rc == cli.EXIT_SWEEP_TUPLE_FAILED  # cells_conflicted folded into the verdict
    # The stage-3 summary line carries a REAL non-zero cells_conflicted tally.
    assert "cells_conflicted=0" not in captured.out
    assert "cells_conflicted=" in captured.out
    # The operator WARNING block (stderr) names the skip + the remediation.
    assert "stage-3 cell(s) hold a REFERENCE-ONLY VIX trio" in captured.err
    assert "quarantine/delete those cell stores" in captured.err
    # Launch-1 bytes are UNTOUCHED — the conflicted flush was skipped, the
    # resume-skipped sweep tuples never re-appended (Golden Rule #7).
    assert _tree_hashes() == before
